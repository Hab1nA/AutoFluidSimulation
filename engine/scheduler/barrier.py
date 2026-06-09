"""
全局屏障监控模块。

负责监控所有构型的网格划分完成情况，并在条件满足时启动 Solver 调度。
"""

import threading
from typing import Callable

from engine.config import (
    DEFAULT_WORKSTATION_ID,
    STATUS_WAITING, STATUS_RUNNING, STATUS_PAUSED, STATUS_COMPLETED, STATUS_ERROR,
    STATUS_RETRYING,
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
        on_solver_terminal: Callable[[str], None] | None = None,
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
        self._on_solver_terminal = on_solver_terminal
        self._solver_terminal_reported = False
        self._guard = PauseGuard(paused_event, stopped_event)

        # ---- Solver 线程（串行执行：同一时刻仅一个构型求解）----
        self._solver_threads: list[threading.Thread] = []
        self._solver_dispatch_lock = threading.Lock()
        self._solver_active_config: int | None = None
        self._workstation_barriers_passed: set[str] = set()

        logger.info("全局屏障协调器初始化完成")

    def _workstation_for_config(self, config_name: int) -> str:
        """Return assigned workstation for a config, preserving legacy default."""
        get_config_workstation = getattr(self.state, "get_config_workstation", None)
        if callable(get_config_workstation):
            workstation_id = get_config_workstation(config_name)
            if workstation_id:
                return str(workstation_id)
        return DEFAULT_WORKSTATION_ID

    def _configs_by_workstation(self) -> dict[str, list[int]]:
        """Group configs by assigned workstation."""
        grouped: dict[str, list[int]] = {}
        for config_name in self.state.get_all_configs():
            workstation_id = self._workstation_for_config(config_name)
            grouped.setdefault(workstation_id, []).append(config_name)
        return grouped

    def _ready_workstations_for_solver(self) -> set[str]:
        """Return workstations whose Meshing barrier has passed."""
        ready: set[str] = set()
        for workstation_id, config_names in self._configs_by_workstation().items():
            if self.state.all_configs_completed_at_step(
                "meshing",
                workstation_id=workstation_id,
                config_names=config_names,
            ):
                ready.add(workstation_id)
        return ready

    def clear_workstation_barrier(self, workstation_id: str) -> None:
        """Clear one workstation barrier cache after reset."""
        self._workstation_barriers_passed.discard(workstation_id)

    def clear_all_workstation_barriers(self) -> None:
        """Clear all workstation barrier caches after broad reset."""
        self._workstation_barriers_passed.clear()

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
                if not pause_aware_sleep(1.0, self._paused, self._stopped):
                    break
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

            # 检查是否已有工作站的 Meshing 屏障通过
            if self.dispatch_solver_if_ready() and self.state.all_configs_completed_at_step("meshing"):
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
                # 将所有 Meshing=Error 的构型的 Solver 也标记为 Error（屏障未通过）
                for cn in all_configs:
                    if self.state.get_step_status(cn, "meshing") == STATUS_ERROR:
                        self.state.set_step_status(cn, "solver", STATUS_ERROR, "网格划分失败，屏障未通过")
                if any(
                    self.state.get_step_status(cn, "meshing") == STATUS_COMPLETED
                    for cn in all_configs
                ):
                    logger.warning("=" * 60)
                    logger.warning(">>> 部分工作站 Meshing 失败，已通过工作站继续进入 Solver <<<")
                    logger.warning("=" * 60)
                else:
                    logger.error("=" * 60)
                    logger.error(">>> 全局屏障失败！所有构型网格划分均已终结但存在错误 <<<")
                    logger.error("=" * 60)
                    self._stopped.set()
                    self.state.set_engine_status("stopped")
                break

            # 检查是否有 Meshing 失败的（仅报告新增的失败，按构型去重）
            error_configs = self.state.get_error_configs()
            meshing_error_configs = {c for c, s, _ in error_configs if s == "meshing"}
            new_errors = meshing_error_configs - _last_error_report
            if new_errors:
                for cn in sorted(new_errors):
                    logger.warning(f"[BarrierMonitor] 构型{cn} 网格划分失败")
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
        """当任一工作站 Meshing 屏障已满足时，幂等地触发 Solver 分发。

        该入口用于正常屏障通过，也用于 Daemon 重启/仅重置 Solver 后的
        断点续传场景：此时持久化的 barrier 状态可能已为 true，
        barrier monitor 不会进入轮询循环，但 Solver 仍需要重新分发。
        """
        if self._stopped.is_set():
            return False

        ready_workstations = self._ready_workstations_for_solver()
        if not ready_workstations:
            return False

        all_meshing_completed = self.state.all_configs_completed_at_step("meshing")
        new_workstations = ready_workstations - self._workstation_barriers_passed

        if all_meshing_completed and not self._barrier_passed.is_set():
            logger.info("=" * 60)
            logger.info(">>> 全局屏障通过！所有构型网格划分已完成 <<<")
            logger.info("=" * 60)
            self._barrier_passed.set()
            self.state.set_global_barrier_met(True)

            # 末次 SC 全体清理：所有 SC→Transfer→Meshing 完成后清理。
            self.runner.do_sc_final_cleanup()
        elif new_workstations:
            logger.info(
                "[BarrierMonitor] 工作站级屏障通过: %s",
                ", ".join(sorted(new_workstations)),
            )
        else:
            logger.info("[BarrierMonitor] 全局屏障已通过，检查待分发 Solver 任务")

        self._workstation_barriers_passed.update(ready_workstations)
        self._dispatch_solver_tasks(ready_workstations)
        return True

    def _dispatch_solver_tasks(self, allowed_workstations: set[str] | None = None) -> bool:
        """
        工作站屏障通过后，启动对应构型的仿真求解。

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

        allowed_snapshot = (
            set(allowed_workstations)
            if allowed_workstations is not None
            else None
        )
        with self._solver_dispatch_lock:
            self._solver_threads = [t for t in self._solver_threads if t.is_alive()]
            if self._solver_threads:
                logger.info("[Solver] 串行调度线程已在运行，跳过重复启动")
                return True

            if self._next_solver_config(allowed_snapshot) is None:
                logger.info("[Solver] 当前没有待执行的求解任务")
                self._report_solver_terminal_if_ready()
                return False

            t = threading.Thread(
                target=self._solver_dispatch_loop,
                args=(allowed_snapshot,),
                name="SolverDispatcher",
                daemon=True,
            )
            t.start()
            self._solver_threads.append(t)

        logger.info("[Solver] 串行调度线程已启动")
        return True

    def _next_solver_config(
        self,
        allowed_workstations: set[str] | None = None,
    ) -> int | None:
        """返回下一个可执行 Solver 的构型；没有则返回 None。"""
        for cn in self.state.get_all_configs():
            if self.state.get_step_status(cn, "meshing") != STATUS_COMPLETED:
                continue
            if (
                allowed_workstations is not None
                and self._workstation_for_config(cn) not in allowed_workstations
            ):
                continue
            solver_status = self.state.get_step_status(cn, "solver")
            if solver_status in (
                STATUS_WAITING,
                STATUS_RUNNING,
                STATUS_PAUSED,
                STATUS_RETRYING,
            ):
                return cn
        return None

    def _solver_dispatch_loop(
        self,
        allowed_workstations: set[str] | None = None,
    ) -> None:
        """串行消费 Solver 任务，直到无待执行构型或收到停止指令。"""
        logger.info("[Solver] 串行调度循环启动")
        try:
            while not self._stopped.is_set():
                config_name = self._next_solver_config(allowed_workstations)
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
            self._report_solver_terminal_if_ready()
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

        if self.state.get_step_status(config_name, "solver") == STATUS_RUNNING:
            remote_executor = self.runner.get_remote_executor()
            workstation_id = self._workstation_for_config(config_name)
            remote_status = remote_executor.query_remote_task_status(
                config_name,
                "solver",
                workstation_id=workstation_id,
            )
            if remote_status == "completed":
                self.state.set_step_status(config_name, "solver", STATUS_COMPLETED)
                remote_executor.forget_remote_task(
                    config_name,
                    "solver",
                    workstation_id=workstation_id,
                )
                logger.info(f"[Solver] 构型{config_name} 重启后检测到完成标志")
                return
            if remote_status == "failed":
                self.state.set_step_status(
                    config_name,
                    "solver",
                    STATUS_ERROR,
                    "远程求解任务失败",
                )
                remote_executor.forget_remote_task(
                    config_name,
                    "solver",
                    workstation_id=workstation_id,
                )
                return
            if remote_status == "running":
                logger.info(f"[Solver] 构型{config_name} 远程任务仍在运行，恢复轮询")
                self._wait_for_solver_completion(config_name)
                return
            if remote_status == "unknown":
                logger.warning(
                    f"[Solver] 构型{config_name} 远程状态未知，保留 Running 状态并跳过重启"
                )
                return
            self.state.set_step_status(config_name, "solver", STATUS_WAITING)
            remote_executor.forget_remote_task(
                config_name,
                "solver",
                workstation_id=workstation_id,
            )
            logger.warning(
                f"[Solver] 构型{config_name} 远程任务已丢失，重置为 Waiting 后重新启动"
            )

        logger.info(f"[Solver] 构型{config_name} 开始求解...")

        # 启动远程求解后台任务
        if self._retry_manager.execute_with_retry(config_name, "solver",
                                     self.runner.execute_solver):
            # ★ 启动远程求解后检查暂停标志（统一使用 PauseGuard）
            if self._guard.check_should_abort():
                return

            self._wait_for_solver_completion(config_name)

    def _wait_for_solver_completion(self, config_name: int) -> None:
        """轮询等待 Solver 完成，并按控制状态更新数据库。"""
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

    def _report_solver_terminal_if_ready(self) -> None:
        """Solver 全部终结时报告流水线自然完成或失败终态。"""
        if self._solver_terminal_reported or self._paused.is_set() or self._stopped.is_set():
            return
        all_configs = self.state.get_all_configs()
        if not all_configs:
            return

        has_error = False
        for cn in all_configs:
            status = self.state.get_step_status(cn, "solver")
            if status == STATUS_ERROR:
                has_error = True
                continue
            if status != STATUS_COMPLETED:
                return

        outcome = "failed" if has_error else "completed"
        self._solver_terminal_reported = True
        if outcome == "completed":
            logger.info("=" * 60)
            logger.info(">>> 全部构型处理完成，流水线进入收尾状态 <<<")
            logger.info("=" * 60)
        else:
            logger.error("=" * 60)
            logger.error(">>> Solver 阶段已终结但存在错误，流水线进入失败收尾状态 <<<")
            logger.error("=" * 60)
        if self._on_solver_terminal is not None:
            self._on_solver_terminal(outcome)
