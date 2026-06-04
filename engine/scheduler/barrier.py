"""
全局屏障监控模块。

负责监控所有构型的网格划分完成情况，并在条件满足时启动 Solver 调度。
"""

import threading
import time

from engine.config import (
    STATUS_WAITING, STATUS_PAUSED, STATUS_COMPLETED, STATUS_ERROR, STATUS_RETRYING,
)
from engine.state_manager import StateManager
from engine.task_runner import TaskRunner
from engine.scheduler.retry import RetryManager
from utils.logger import setup_logger
from engine.scheduler.utils import pause_aware_sleep, PauseGuard

logger = setup_logger(__name__)


class BarrierCoordinator:
    """
    全局屏障协调器。

    监控所有构型的 Meshing 是否全部 Completed，一旦满足条件，
    设置屏障通过标志并启动 Solver 调度。
    """

    def __init__(
        self,
        state_manager: StateManager,
        task_runner: TaskRunner,
        paused_event: threading.Event,
        stopped_event: threading.Event,
        barrier_passed_event: threading.Event,
        retry_manager: RetryManager,
    ):
        """
        初始化全局屏障协调器。

        Args:
            state_manager: 共享状态管理器
            task_runner: 任务执行器
            paused_event: 暂停事件
            stopped_event: 停止事件
            barrier_passed_event: 全局屏障通过事件
            retry_manager: 重试管理器
        """
        self.state = state_manager
        self.runner = task_runner
        self._paused = paused_event
        self._stopped = stopped_event
        self._barrier_passed = barrier_passed_event
        self._retry_manager = retry_manager
        self._guard = PauseGuard(paused_event, stopped_event)

        # ---- Solver 线程（串行执行：同一时刻仅一个构型求解）----
        self._solver_threads: list[threading.Thread] = []
        self._solver_dispatch_lock = threading.Lock()
        self._solver_active_config: int | None = None

        logger.info("全局屏障协调器初始化完成")

    def join_solver_threads(self, timeout: float = 3.0) -> None:
        """等待所有 Solver 线程退出并清空列表。

        供外部模块（如 PipelineScheduler.stop()）调用，避免直接访问私有成员。

        Args:
            timeout: 每个线程的最大等待秒数
        """
        for t in self._solver_threads:
            if t.is_alive():
                t.join(timeout=timeout)
        self._solver_threads.clear()

    # ------------------------------------------------------------------
    # 全局屏障监控
    # ------------------------------------------------------------------

    def monitor_loop(self):
        """
        全局屏障监控线程主循环。

        持续检查所有构型的 Meshing 是否全部 Completed。
        一旦满足条件，设置屏障通过标志并启动 Solver 调度。
        如果所有构型的 Meshing 均为 Error/Completed 且至少有一个 Error，
        则屏障永远无法通过，报告错误并退出。
        额外检查：若所有构型的 SW 均已终结但存在错误（表示宏执行完毕
        但未产出任何有效 STEP），则提前检测并停止。
        """
        logger.info("[BarrierMonitor] 全局屏障监控启动")
        logger.info("[BarrierMonitor] 等待所有构型的网格划分完成...")

        _last_error_report: set = set()  # 已报告过的失败构型集合，避免重复日志

        while not self._stopped.is_set() and not self._barrier_passed.is_set():
            if self._paused.is_set():
                time.sleep(1)
                continue

            all_configs = self.state.get_all_configs()

            # ---- 前置检查：SW 阶段是否已全部终结且有错误 ----
            # 若 SW 宏执行完毕但所有构型的 STEP 均缺失，后续流程无法推进。
            sw_all_terminal = True
            sw_has_completed = False
            for cn in all_configs:
                s = self.state.get_step_status(cn, "sw")
                if s not in (STATUS_COMPLETED, STATUS_ERROR):
                    sw_all_terminal = False
                    break
                if s == STATUS_COMPLETED:
                    sw_has_completed = True
            if sw_all_terminal and not sw_has_completed:
                logger.error("=" * 60)
                logger.error(">>> 流水线中止！所有构型的 SW 步骤均已失败 <<<")
                logger.error("=" * 60)
                logger.error("宏已执行但未产出任何有效 STEP 文件，无法继续。")
                logger.error("请检查：SW 宏逻辑 / 设计表参数 / STEP 输出路径。")
                for cn in all_configs:
                    for step in ["sc", "transfer", "meshing", "solver"]:
                        if self.state.get_step_status(cn, step) == STATUS_WAITING:
                            self.state.set_step_status(cn, step, STATUS_ERROR,
                                                       "SW 步骤失败，后续步骤无法执行")
                self._stopped.set()
                self.state.set_engine_status("stopped")
                break

            # 检查是否所有构型的 Meshing 都已完成
            if self.state.all_configs_completed_at_step("meshing"):
                self.dispatch_solver_if_ready()
                break

            # 检查是否所有 Meshing 均已终结（Completed 或 Error）
            all_configs = self.state.get_all_configs()
            all_terminal = True
            has_error = False
            for cn in all_configs:
                s = self.state.get_step_status(cn, "meshing")
                if s not in (STATUS_COMPLETED, STATUS_ERROR):
                    all_terminal = False
                    break
                if s == STATUS_ERROR:
                    has_error = True

            if all_terminal and has_error:
                logger.error("=" * 60)
                logger.error(">>> 全局屏障失败！所有构型网格划分均已终结但存在错误 <<<")
                logger.error("=" * 60)
                # 将所有 Meshing=Error 的构型的 Solver 也标记为 Error（屏障未通过）
                for cn in all_configs:
                    if self.state.get_step_status(cn, "meshing") == STATUS_ERROR:
                        self.state.set_step_status(cn, "solver", STATUS_ERROR, "网格划分失败，屏障未通过")
                self._stopped.set()
                self.state.set_engine_status("stopped")
                break

            # 检查是否有 Meshing 失败的（仅报告新增的失败，按构型去重）
            error_configs = self.state.get_error_configs()
            meshing_error_configs = {c for c, s, _ in error_configs if s == "meshing"}
            new_errors = meshing_error_configs - _last_error_report
            if new_errors:
                logger.warning(
                    f"[BarrierMonitor] 检测到 {len(new_errors)} 个新的网格划分失败: "
                    f"{sorted(new_errors)}"
                )
                _last_error_report = meshing_error_configs

            # 轮询间隔：网格划分通常耗时较长，不需要高频检查
            # ★ 使用暂停感知 sleep，避免暂停期间无谓轮询消耗 CPU
            if not pause_aware_sleep(5.0, self._paused, self._stopped):
                break

        logger.info("[BarrierMonitor] 全局屏障监控退出")

    # ------------------------------------------------------------------
    # Solver 调度（屏障通过后）
    # ------------------------------------------------------------------

    def dispatch_solver_if_ready(self) -> bool:
        """当 Meshing 屏障已满足时，幂等地触发 Solver 分发。

        该入口用于正常屏障通过，也用于 Daemon 重启/仅重置 Solver 后的
        断点续传场景：此时持久化的 global_barrier_met 可能已为 true，
        barrier monitor 不会进入轮询循环，但 Solver 仍需要重新分发。
        """
        if self._stopped.is_set():
            return False

        if not self.state.all_configs_completed_at_step("meshing"):
            return False

        if not self._barrier_passed.is_set():
            logger.info("=" * 60)
            logger.info(">>> 全局屏障通过！所有构型网格划分已完成 <<<")
            logger.info("=" * 60)
            self._barrier_passed.set()
            self.state.set_global_barrier_met(True)

            # 末次 SC 全体清理：所有 SC→Transfer→Meshing 完成后清理。
            self.runner.do_sc_final_cleanup()
        else:
            logger.info("[BarrierMonitor] 全局屏障已通过，检查待分发 Solver 任务")

        self._dispatch_solver_tasks()
        return True

    def _dispatch_solver_tasks(self) -> bool:
        """
        全局屏障通过后，统一启动所有构型的仿真求解。

        Solver 使用单个调度线程串行执行，确保同一时刻只有一个构型
        处于求解阶段。
        若暂停标志已置位，则等待恢复后再分发。
        """
        # ★ 分发前检查暂停标志（统一使用 PauseGuard）
        if self._guard.check_should_abort():
            logger.warning("Solver 分发前检测到停止标志，取消分发")
            return False

        logger.info("=" * 60)
        logger.info("开始串行调度仿真求解任务...")
        logger.info("=" * 60)

        with self._solver_dispatch_lock:
            self._solver_threads = [t for t in self._solver_threads if t.is_alive()]
            if self._solver_threads:
                logger.info("[Solver] 串行调度线程已在运行，跳过重复启动")
                return True

            if self._next_solver_config() is None:
                logger.info("[Solver] 当前没有待执行的求解任务")
                return False

            t = threading.Thread(
                target=self._solver_dispatch_loop,
                name="SolverDispatcher",
                daemon=True,
            )
            t.start()
            self._solver_threads.append(t)

        logger.info("[Solver] 串行调度线程已启动")
        return True

    def _next_solver_config(self) -> int | None:
        """返回下一个可执行 Solver 的构型；没有则返回 None。"""
        for cn in self.state.get_all_configs():
            solver_status = self.state.get_step_status(cn, "solver")
            if solver_status in (STATUS_WAITING, STATUS_PAUSED, STATUS_RETRYING):
                return cn
        return None

    def _solver_dispatch_loop(self) -> None:
        """串行消费 Solver 任务，直到无待执行构型或收到停止指令。"""
        logger.info("[Solver] 串行调度循环启动")
        try:
            while not self._stopped.is_set():
                config_name = self._next_solver_config()
                if config_name is None:
                    break

                with self._solver_dispatch_lock:
                    self._solver_active_config = config_name
                try:
                    self._execute_solver_for_config(config_name)
                finally:
                    with self._solver_dispatch_lock:
                        self._solver_active_config = None
        finally:
            logger.info("[Solver] 串行调度循环退出")

    def _execute_solver_for_config(self, config_name: int):
        """
        执行单个构型的仿真求解（在独立线程中运行）。

        Args:
            config_name: 构型名称
        """
        # ★ 执行前检查暂停标志（统一使用 PauseGuard）
        if self._guard.check_should_abort():
            return

        logger.info(f"[Solver] 构型{config_name} 开始求解...")

        # 启动远程求解后台任务
        if self._retry_manager.execute_with_retry(config_name, "solver",
                                     self.runner.execute_solver):
            # ★ 启动远程求解后检查暂停标志（统一使用 PauseGuard）
            if self._guard.check_should_abort():
                return

            # 轮询等待求解完成
            if self.runner.wait_solver_completion(
                config_name,
                paused_event=self._paused,
                stopped_event=self._stopped,
            ):
                self.state.set_step_status(config_name, "solver", STATUS_COMPLETED)
                logger.info(f"[Solver] 构型{config_name} 求解完成 ✓")
            else:
                # ★ 区分暂停和真正的超时
                if self._paused.is_set():
                    self.state.set_step_status(
                        config_name, "solver", STATUS_PAUSED,
                        "等待求解期间暂停"
                    )
                elif self._stopped.is_set():
                    self.state.set_step_status(
                        config_name, "solver", STATUS_PAUSED,
                        "引擎已停止"
                    )
                else:
                    self.state.set_step_status(
                        config_name, "solver", STATUS_ERROR, "求解超时"
                    )
