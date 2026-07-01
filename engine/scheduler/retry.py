from __future__ import annotations

"""
重试机制模块。

负责管理任务执行的重试逻辑，包括状态转换和暂停感知的 sleep。
"""

import threading

from engine.config import (
    STATUS_RUNNING, STATUS_PAUSED, STATUS_COMPLETED, STATUS_ERROR, STATUS_RETRYING,
    ENGINE_CONFIG,
)
from engine.state_manager import StateManager
from utils.infrastructure import InfrastructureUnavailableError
from utils.logger import setup_logger

from .utils import pause_aware_sleep, PauseGuard

logger = setup_logger(__name__)


class RetryManager:
    """
    重试管理器。

    管理任务执行的重试逻辑，包括状态转换和暂停感知的 sleep。
    """

    def __init__(
        self,
        state_manager: StateManager,
        paused_event: threading.Event,
        stopped_event: threading.Event,
    ):
        """
        初始化重试管理器。

        Args:
            state_manager: 共享状态管理器
            paused_event: 暂停事件
            stopped_event: 停止事件
        """
        self.state = state_manager
        self._paused = paused_event
        self._stopped = stopped_event
        self._guard = PauseGuard(paused_event, stopped_event, state_manager)

        logger.info("重试管理器初始化完成")

    def execute_with_retry(self, config_name: int, step_name: str,
                           execute_func) -> bool:
        """
        带重试机制的任务执行包装器。

        状态转换逻辑：
        - 首次尝试: Running
        - 失败后、等待重试期间: Retrying（TUI 显示 🔄）
        - 到达最大重试次数的最终失败: Error

        Args:
            config_name: 构型名称
            step_name: 步骤名
            execute_func: 执行函数，签名为 func(config_name) -> bool

        Returns:
            True 表示执行成功
        """
        max_retries = ENGINE_CONFIG["max_retries"]
        last_error_message = ""

        for attempt in range(1, int(max_retries) + 1):
            # 检查是否被停止
            if self._stopped.is_set():
                return False

            # 检查暂停（在设置状态前检查，避免竞态）
            if self._guard.check_should_abort():
                return False

            # 设置 Running 状态
            self.state.set_step_status(config_name, step_name, STATUS_RUNNING)

            # 设置状态后立即再次检查暂停标志：
            # 防止 pause() 在 set_step_status 之后被调用导致的竞态窗口
            if self._paused.is_set():
                self.state.set_step_status(config_name, step_name, STATUS_PAUSED)
                if self._guard.check_should_abort():
                    return False
                # 恢复后重新设置运行状态
                self.state.set_step_status(config_name, step_name, STATUS_RUNNING)

            logger.info(f"执行 [{step_name}] 构型{config_name} (尝试 {attempt}/{max_retries})")

            try:
                success = execute_func(config_name)
                if success:
                    # ★ 执行成功后检查暂停标志：防止 pause 在 execute_func 执行期间
                    #   被触发，导致 set_all_running_to_paused() 已将状态改为 PAUSED
                    #   但此处又覆盖为 COMPLETED 的竞态。
                    #   SC 步骤通过内部轮询循环检测 pause 并返回 False 来避免此问题，
                    #   但 Transfer 等同步步骤无法中途检测 pause，因此在此统一保护。
                    if self._guard.mark_paused_on_success(config_name, step_name):
                        return False
                    # 对于远程异步步骤，状态由调用者等待完成信号后设置。
                    if step_name not in ("meshing", "solver", "postprocess"):
                        self.state.set_step_status(config_name, step_name, STATUS_COMPLETED)
                    return True
                else:
                    last_error_message = self._current_error_message(
                        config_name,
                        step_name,
                        execute_func,
                    )
                    # ★ 检查是否因暂停/停止导致执行失败
                    # （例如 execute_sc_step 在轮询中检测到暂停标志，终止了 SC 进程）
                    if self._guard.mark_paused_if_flagged(config_name, step_name):
                        return False  # 用户主动暂停，不重试
                    if self._stopped.is_set():
                        return False  # 引擎已停止，不重试

                    logger.warning(f"[{step_name}] 构型{config_name} 执行失败 (尝试 {attempt}/{max_retries})")
                    if attempt < max_retries:
                        retry_count = self.state.increment_retry(config_name, step_name)
                        # 失败后立即将状态切换为 Retrying，TUI 可显示 🔄
                        self.state.set_step_status(
                            config_name, step_name, STATUS_RETRYING,
                            f"重试 {attempt + 1}/{max_retries}（已重试 {retry_count} 次）"
                        )
                        logger.info(f"将在 {5 * attempt}s 后重试 (已重试 {retry_count} 次)")
                        # ★ 使用暂停感知 sleep：若暂停被触发，sleep 期间状态
                        #    会被 set_all_running_to_paused() 改为 Paused，
                        #    恢复后下一轮迭代会检测 _paused 并正确等待
                        if not pause_aware_sleep(5 * attempt, self._paused, self._stopped):
                            return False  # stopped
            except InfrastructureUnavailableError as e:
                self.state.set_step_status(
                    config_name,
                    step_name,
                    STATUS_RETRYING,
                    f"基础设施恢复中（不消耗业务重试次数）: {e}",
                )
                logger.warning(
                    "[%s] 构型%s 基础设施不可用，等待恢复且不消耗业务重试次数: %s",
                    step_name,
                    config_name,
                    e,
                )
                raise
            except (RuntimeError, ValueError, OSError, ConnectionError) as e:
                if attempt < max_retries:
                    retry_count = self.state.increment_retry(config_name, step_name)
                    self.state.set_step_status(
                        config_name, step_name, STATUS_RETRYING,
                        f"异常重试 {attempt + 1}/{max_retries}（已重试 {retry_count} 次）: {e}"
                    )
                    if not pause_aware_sleep(5 * attempt, self._paused, self._stopped):
                        return False  # stopped
                else:
                    # 最后一次异常重试也失败
                    self.state.set_step_status(
                        config_name, step_name, STATUS_ERROR,
                        f"异常重试 {max_retries} 次后仍然失败: {e}"
                    )
                    return False

        # 所有重试均失败
        error_message = last_error_message or f"重试 {max_retries} 次后仍然失败"
        self.state.set_step_status(config_name, step_name, STATUS_ERROR, error_message)
        return False

    def _current_error_message(self, config_name: int, step_name: str, execute_func) -> str:
        """Return the current step error message if the execute function set one."""
        owner = getattr(execute_func, "__self__", None)
        if owner is not None:
            attr_name = f"last_{step_name}_error"
            if hasattr(owner, attr_name):
                return str(getattr(owner, attr_name) or "")

        try:
            steps = self.state.get_all_steps_for_config(config_name)
        except (RuntimeError, OSError, KeyError, AttributeError):
            return ""
        step = steps.get(step_name, {})
        if step.get("status") != STATUS_ERROR:
            return ""
        return str(step.get("error_message") or "")
