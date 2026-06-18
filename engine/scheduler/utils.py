"""
调度器通用工具函数。

本模块提供跨子模块共享的辅助函数，避免代码重复。
"""

from __future__ import annotations

import os
import time
import threading
from collections.abc import Callable
from typing import Any

from engine.config import get_step_filename, STATUS_PAUSED
from utils.logger import setup_logger

_pg_logger = setup_logger(__name__)


class PauseGuard:
    """暂停/停止守卫工具。

    封装分散在各子模块中的暂停检查、状态标记和轮询等待逻辑，
    统一重试管理器、SW 阶段、屏障协调器、远程执行器中的防御性编程模式。

    Args:
        paused_event: 暂停事件（set = 已暂停）
        stopped_event: 停止事件（set = 已停止）
        state_manager: 可选的状态管理器，提供 set_step_status
    """

    def __init__(
        self,
        paused_event: threading.Event,
        stopped_event: threading.Event,
        state_manager: Any = None,
    ):
        self._paused = paused_event
        self._stopped = stopped_event
        self._state = state_manager

    # ------------------------------------------------------------------
    # 启动前守卫
    # ------------------------------------------------------------------

    def check_should_abort(self) -> bool:
        """检查是否应中止执行（暂停等待 + 停止检测）。

        若暂停标志已置位，阻塞等待直到暂停解除或收到停止信号。

        Returns:
            True 表示应中止（已停止），False 表示可继续。
        """
        return not wait_unless_paused_or_stopped(self._paused, self._stopped)

    # ------------------------------------------------------------------
    # 执行后暂停标记
    # ------------------------------------------------------------------

    def mark_paused_if_flagged(
        self,
        config_name: int,
        step_name: str,
        detail: str = "暂停——任务已中断，恢复后将重新执行",
    ) -> bool:
        """执行失败/中断后检查暂停标志：若已暂停则标记步骤为 PAUSED。

        统一 retry.py 中 "失败后检查 pause → 标记 PAUSED → return False" 模式。

        Returns:
            True 表示已标记为 Paused（调用方应立即返回 False），
            False 表示未暂停（调用方应继续正常错误处理）。
        """
        if not self._paused.is_set():
            return False
        if self._state is not None:
            self._state.set_step_status(config_name, step_name, STATUS_PAUSED, detail)
        _pg_logger.info(
            f"[{step_name}] 构型{config_name} 因暂停中断，已标记为 Paused"
        )
        return True

    def mark_paused_on_success(
        self,
        config_name: int,
        step_name: str,
    ) -> bool:
        """执行成功后检查暂停标志：若已暂停则标记为 PAUSED 而非 COMPLETED。

        防止 pause 在 execute_func 执行期间被触发，导致
        set_all_running_to_paused() 已将状态改为 PAUSED 但此处又覆盖为
        COMPLETED 的竞态。

        Returns:
            True 表示已标记为 Paused（调用方应返回 False），
            False 表示未暂停（调用方应正常标记 COMPLETED）。
        """
        if not self._paused.is_set():
            return False
        if self._state is not None:
            self._state.set_step_status(
                config_name, step_name, STATUS_PAUSED,
                "执行完成但系统已暂停",
            )
        _pg_logger.info(
            f"[{step_name}] 构型{config_name} 执行成功但系统已暂停，"
            f"标记为 Paused 而非 Completed"
        )
        return True

    # ------------------------------------------------------------------
    # 带暂停补偿的轮询等待
    # ------------------------------------------------------------------

    def poll_with_pause_compensation(
        self,
        timeout: float,
        poll_func: Callable[[], bool],
        poll_interval: float = 10.0,
    ) -> tuple[bool, float]:
        """带暂停时间补偿的轮询等待。

        将超时计时器在暂停期间冻结，防止恢复运行后立即触发超时。
        统一 remote_executor.py 中 meshing/solver 的轮询+补偿逻辑。

        Args:
            timeout: 超时时长（秒，不含暂停时间）
            poll_func: 轮询回调，返回 True 表示条件满足
            poll_interval: 轮询间隔（秒）

        Returns:
            (条件是否满足, 实际经过的非暂停时间) 元组
        """
        elapsed = 0.0
        start_time = time.time()

        while elapsed < timeout:
            # ---- 暂停响应（带时间补偿） ----
            if self._paused.is_set():
                pause_start = time.time()
                if not wait_unless_paused_or_stopped(self._paused, self._stopped):
                    return False, elapsed
                # 将暂停持续时间从已用时间中扣除
                pause_duration = time.time() - pause_start
                start_time += pause_duration

            if self._stopped.is_set():
                return False, elapsed

            # ---- 执行轮询 ----
            try:
                if poll_func():
                    return True, elapsed
            except Exception:
                pass  # 轮询异常不中断，由下一轮重试

            time.sleep(poll_interval)
            elapsed = time.time() - start_time

        return False, elapsed


def wait_unless_paused_or_stopped(
    paused_event: threading.Event,
    stopped_event: threading.Event,
) -> bool:
    """
    阻塞等待直到暂停解除或收到停止信号。

    替代重复的 ``while paused and not stopped: sleep(1)`` 模式。
    在暂停期间阻塞当前线程，每秒检查一次标志。

    Args:
        paused_event: 暂停事件（set = 已暂停）
        stopped_event: 停止事件（set = 已停止）

    Returns:
        True 表示暂停已解除且未被停止，可继续执行。
        False 表示收到停止信号，调用方应立即退出。
    """
    while paused_event.is_set() and not stopped_event.is_set():
        time.sleep(1)
    return not stopped_event.is_set()


def pause_aware_sleep(
    duration: float,
    paused_event: threading.Event,
    stopped_event: threading.Event,
    check_interval: float = 1.0,
) -> bool:
    """
    可响应暂停/停止的 sleep 替代方法。

    将 sleep 切分为 check_interval 粒度的小段，每段检查
    paused_event 和 stopped_event 标志。若检测到 stopped 则立即返回。

    Args:
        duration: 总等待时长（秒）
        paused_event: 暂停事件（set = 已暂停）
        stopped_event: 停止事件（set = 已停止）
        check_interval: 每次检查的间隔（秒）

    Returns:
        True 表示 sleep 完整结束，False 表示因 stopped 提前退出
    """
    deadline = time.monotonic() + duration
    while time.monotonic() < deadline:
        if stopped_event.is_set():
            return False
        if paused_event.is_set():
            pause_started_at = time.monotonic()
            while paused_event.is_set():
                if stopped_event.wait(check_interval):
                    return False
            deadline += time.monotonic() - pause_started_at
            continue
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        if stopped_event.wait(min(check_interval, remaining)):
            return False
    return True


def check_step_output_exists(
    config_name: int,
    step_name: str,
    step_dir: str,
    scdoc_dir: str,
    remote_config: Any,
    ssh: Any | None = None,
    remote_check_timeout: float | None = None,
) -> bool:
    """
    检查某构型某步骤的输出文件是否已存在。

    统一了 scan_completed_downstream、_resume_paused_steps、
    worker_pool._process_single_config、meshing_monitor 中的
    输出文件存在性检查逻辑。

    Args:
        config_name: 构型名称
        step_name: 步骤名（SW/SC/Transfer/Meshing/Solver）
        step_dir: 本地 STEP 文件目录
        scdoc_dir: 本地 SCDOC 文件目录
        remote_config: 远程配置字典（需含 scdoc_dir/flag_dir/msh_dir/result_dir）
        ssh: 可选的 SSH 连接对象（需有 check_remote_file 方法）
        remote_check_timeout: 远程 SFTP stat 检查超时（秒）

    Returns:
        True 表示输出文件已存在且大小 > 0
    """

    # ---- 本地文件检查（SW/SC）----
    if step_name == "sw":
        filename = get_step_filename("sw", config_name)
        if not filename:
            return False
        path = os.path.join(step_dir, filename)
        return os.path.exists(path) and os.path.getsize(path) > 0

    if step_name == "sc":
        filename = get_step_filename("sc", config_name)
        if not filename:
            return False
        path = os.path.join(scdoc_dir, filename)
        return os.path.exists(path) and os.path.getsize(path) > 0

    # ---- 远程文件检查（Transfer/Meshing/Solver）----
    if ssh is None:
        return False

    try:
        if not ssh.is_connected():
            return False
    except Exception:
        return False

    def _check_remote_file(remote_path: str) -> bool:
        if remote_check_timeout is None:
            return bool(ssh.check_remote_file(remote_path))
        try:
            return bool(ssh.check_remote_file(remote_path, timeout=remote_check_timeout))
        except TypeError:
            return bool(ssh.check_remote_file(remote_path))

    if step_name == "transfer":
        filename = get_step_filename("sc", config_name)
        if not filename:
            return False
        scdoc_dir = str(remote_config["scdoc_dir"]).replace("\\", "/")
        remote_scdoc = f"{scdoc_dir}/{filename}"
        try:
            if hasattr(ssh, "get_remote_file_size"):
                if remote_check_timeout is None:
                    size = ssh.get_remote_file_size(remote_scdoc)
                else:
                    try:
                        size = ssh.get_remote_file_size(
                            remote_scdoc,
                            timeout=remote_check_timeout,
                        )
                    except TypeError:
                        size = ssh.get_remote_file_size(remote_scdoc)
                return size is not None and size > 0
            return _check_remote_file(remote_scdoc)
        except Exception:
            return False

    if step_name == "meshing":
        flag_dir = str(remote_config["flag_dir"]).replace("\\", "/")
        error_flag = f"{flag_dir}/meshing_done_{config_name}.txt.error"
        mesh_name = get_step_filename("meshing", config_name)
        mesh_file = None
        if mesh_name:
            msh_dir = str(remote_config["msh_dir"]).replace("\\", "/")
            mesh_file = f"{msh_dir}/{mesh_name}"
        try:
            return _check_remote_file(error_flag) or (
                mesh_file is not None and _check_remote_file(mesh_file)
            )
        except Exception:
            return False

    if step_name == "solver":
        flag_dir = str(remote_config["flag_dir"]).replace("\\", "/")
        flag_file = f"{flag_dir}/solver_done_{config_name}.txt"
        error_flag = f"{flag_file}.error"
        result_dir = str(remote_config["result_dir"]).replace("\\", "/")
        cas_name = get_step_filename("solver", config_name)
        dat_name = get_step_filename("solverdata", config_name)
        cas_file = f"{result_dir}/{cas_name}" if cas_name else None
        dat_file = f"{result_dir}/{dat_name}" if dat_name else None
        try:
            if _check_remote_file(flag_file) or _check_remote_file(error_flag):
                return True
            cas_exists = cas_file is not None and _check_remote_file(cas_file)
            dat_exists = dat_file is not None and _check_remote_file(dat_file)
            return cas_exists and dat_exists
        except Exception:
            return False

    if step_name == "postprocess":
        flag_dir = str(remote_config["flag_dir"]).replace("\\", "/")
        flag_name = get_step_filename("postprocess", config_name)
        if not flag_name:
            return False
        flag_file = f"{flag_dir}/{flag_name}"
        error_flag = f"{flag_file}.error"
        try:
            return _check_remote_file(flag_file) or _check_remote_file(error_flag)
        except Exception:
            return False

    return False
