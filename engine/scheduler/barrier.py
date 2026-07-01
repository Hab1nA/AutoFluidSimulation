from __future__ import annotations

"""
全局屏障监控模块。

负责监控所有构型的网格划分完成情况，并在条件满足时启动 Solver 调度。
"""

import threading
import time
from typing import Callable

from engine.config import (
    DEFAULT_WORKSTATION_ID,
    STATUS_WAITING, STATUS_RUNNING, STATUS_PAUSED, STATUS_COMPLETED, STATUS_ERROR,
    STATUS_RETRYING, STATUS_UNKNOWN_REMOTE, ENGINE_CONFIG, WORKSTATIONS,
)
from engine.state_manager import StateManager
from engine.task_runner import TaskRunner
from engine.scheduler.retry import RetryManager
from utils.infrastructure import InfrastructureUnavailableError
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
        get_reset_generation: Callable[[int, str], int] | None = None,
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
        self._get_reset_generation = get_reset_generation or (lambda _cn, _step: 0)
        self._solver_terminal_reported = False
        self._guard = PauseGuard(paused_event, stopped_event)

        # ---- Solver 线程（同一工作站串行，不同工作站可并行）----
        self._solver_threads: list[threading.Thread] = []
        self._solver_thread_workstations: dict[threading.Thread, str] = {}
        self._solver_dispatch_lock = threading.Lock()
        self._solver_active_config: int | None = None
        self._solver_active_by_workstation: dict[str, int] = {}
        self._workstation_solver_failures: dict[str, int] = {}
        self._workstation_quarantine_until: dict[str, float] = {}
        self._restore_solver_quarantine_state()
        self._workstation_barriers_passed: set[str] = set()
        self._configured_workstation_ids = {
            str(workstation.get("id"))
            for workstation in WORKSTATIONS
            if workstation.get("id")
        }
        self._multi_workstation_mode = any(
            workstation_id != DEFAULT_WORKSTATION_ID
            for workstation_id in self._configured_workstation_ids
        )

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

    def _is_dynamic_assignment_pending(self, config_name: int) -> bool:
        """Whether a config could still be claimed by any real workstation."""
        if not self._multi_workstation_mode:
            return False
        for upstream_step in ("sw", "sc", "transfer"):
            if self.state.get_step_status(config_name, upstream_step) == STATUS_ERROR:
                return False
        meshing_status = self.state.get_step_status(config_name, "meshing")
        if meshing_status in (STATUS_COMPLETED, STATUS_ERROR):
            return False
        return self._workstation_for_config(config_name) == DEFAULT_WORKSTATION_ID

    def _all_assignable_configs_assigned(self) -> bool:
        """Return True once no unclaimed config can later enter Meshing."""
        return not any(
            self._is_dynamic_assignment_pending(config_name)
            for config_name in self.state.get_all_configs()
        )

    @staticmethod
    def _counts_all_terminal(counts: dict[str, int], total: int) -> bool:
        """Whether every known config for a step is Completed or Error."""
        terminal = counts.get(STATUS_COMPLETED, 0) + counts.get(STATUS_ERROR, 0)
        return total > 0 and terminal >= total

    def _ready_workstations_for_solver(self) -> set[str]:
        """Return workstations whose Meshing barrier has passed."""
        if not self._all_assignable_configs_assigned():
            return set()
        ready: set[str] = set()
        for workstation_id, config_names in self._configs_by_workstation().items():
            if self._multi_workstation_mode and workstation_id == DEFAULT_WORKSTATION_ID:
                continue
            if self.state.all_configs_completed_at_step(
                "meshing",
                workstation_id=workstation_id,
                config_names=config_names,
            ):
                ready.add(workstation_id)
        return ready

    def workstation_barrier_snapshot(self) -> dict[str, bool]:
        """Return pass state for configured real workstation barriers."""
        if not self._multi_workstation_mode:
            return {}
        return {
            workstation_id: workstation_id in self._workstation_barriers_passed
            for workstation_id in sorted(self._configured_workstation_ids)
            if workstation_id != DEFAULT_WORKSTATION_ID
        }

    def clear_workstation_barrier(self, workstation_id: str) -> None:
        """Clear one workstation barrier cache after reset."""
        self._workstation_barriers_passed.discard(workstation_id)

    def clear_all_workstation_barriers(self) -> None:
        """Clear all workstation barrier caches after broad reset."""
        self._workstation_barriers_passed.clear()

    def reset_solver_terminal_reported(self) -> None:
        """Allow a new natural terminal report after reset touches Solver."""
        self._solver_terminal_reported = False

    def _step_generation(self, config_name: int, step_name: str) -> int:
        """Return the reset generation for a config step."""
        return int(self._get_reset_generation(config_name, step_name))

    def _is_stale_step_result(
        self,
        config_name: int,
        step_name: str,
        generation: int,
    ) -> bool:
        """Whether reset touched this step while Solver was running."""
        return generation != self._step_generation(config_name, step_name)

    def _discard_stale_step_result(self, config_name: int, step_name: str) -> None:
        """Restore reset state after an old Solver result wrote back."""
        logger.warning(
            "[Solver] 构型%s [%s] 结果已过期，丢弃旧执行结果并恢复 reset 状态",
            config_name,
            step_name,
        )
        self.state.reset_config_steps(config_name, step_name)

    def _record_unknown_remote_status(self, config_name: int, step_name: str) -> bool:
        """
        Count unknown remote probes and surface an error once retry budget is exhausted.

        Returns True when the step has been marked Error.
        """
        retry_count = self.state.increment_retry(config_name, step_name)
        max_retries = int(ENGINE_CONFIG.get("remote_unknown_max_retries", 3))
        workstation_id = self._workstation_for_config(config_name)
        if retry_count >= max_retries:
            remote_executor = self.runner.get_remote_executor()
            kill_remote_task = getattr(remote_executor, "_kill_remote_task_for_config", None)
            if callable(kill_remote_task):
                kill_remote_task(config_name, step_name, workstation_id)
            self.state.set_step_status(
                config_name,
                step_name,
                STATUS_ERROR,
                f"远程 {step_name} 状态连续 {retry_count} 次未知，停止等待",
            )
            logger.error(
                "[Solver] 构型%s 远程状态 unknown 达到重试上限，标记为 Error",
                config_name,
            )
            return True
        self.state.set_step_status(
            config_name,
            step_name,
            STATUS_UNKNOWN_REMOTE,
            f"远程 {step_name} 状态未知，等待恢复探测 ({retry_count}/{max_retries})",
        )
        logger.warning(
            "[Solver] 构型%s 远程状态未知，标记 UnknownRemote 等待后续重试 "
            "(%s/%s)",
            config_name,
            retry_count,
            max_retries,
        )
        return False

    def _restore_solver_quarantine_state(self) -> None:
        get_solver_quarantine = getattr(self.state, "get_solver_quarantine", None)
        if not callable(get_solver_quarantine):
            return
        restored = get_solver_quarantine()
        now = time.time()
        for workstation_id, data in restored.items():
            until = data.get("until")
            failure_count = data.get("failure_count")
            if not isinstance(until, int | float) or not isinstance(failure_count, int | float):
                continue
            if int(failure_count) > 0 and (float(until) <= 0.0 or float(until) >= now):
                self._workstation_solver_failures[workstation_id] = int(failure_count)
            if float(until) > now:
                self._workstation_quarantine_until[workstation_id] = float(until)
        if self._workstation_quarantine_until:
            logger.warning(
                "[Solver] 已恢复工作站 Solver 隔离状态: %s",
                self.solver_quarantine_snapshot(),
            )
        else:
            self._persist_solver_quarantine_state()

    def _persist_solver_quarantine_state(self) -> None:
        set_solver_quarantine = getattr(self.state, "set_solver_quarantine", None)
        if not callable(set_solver_quarantine):
            return
        workstation_ids = set(self._workstation_solver_failures) | set(
            self._workstation_quarantine_until
        )
        snapshot = {
            workstation_id: {
                "failure_count": self._workstation_solver_failures.get(workstation_id, 0),
                "until": self._workstation_quarantine_until.get(workstation_id, 0.0),
            }
            for workstation_id in workstation_ids
            if self._workstation_solver_failures.get(workstation_id, 0) > 0
            or workstation_id in self._workstation_quarantine_until
        }
        set_solver_quarantine(snapshot)

    def _is_workstation_quarantined(self, workstation_id: str) -> bool:
        until = self._workstation_quarantine_until.get(workstation_id)
        if until is None:
            return False
        if time.time() >= until:
            self._workstation_quarantine_until.pop(workstation_id, None)
            self._workstation_solver_failures.pop(workstation_id, None)
            self._persist_solver_quarantine_state()
            logger.info("[Solver] 工作站 %s Solver 隔离已到期，恢复调度", workstation_id)
            return False
        return True

    def _record_solver_failure_for_workstation(self, workstation_id: str) -> None:
        failures = self._workstation_solver_failures.get(workstation_id, 0) + 1
        self._workstation_solver_failures[workstation_id] = failures
        max_failures = int(ENGINE_CONFIG.get("solver_workstation_max_consecutive_failures", 2))
        if max_failures <= 0 or failures < max_failures:
            self._persist_solver_quarantine_state()
            return
        quarantine_seconds = float(
            ENGINE_CONFIG.get("solver_workstation_quarantine_minutes", 30)
        ) * 60.0
        until = time.time() + quarantine_seconds
        self._workstation_quarantine_until[workstation_id] = until
        self._persist_solver_quarantine_state()
        logger.error(
            "[Solver] 工作站 %s 连续 Solver 失败 %s 次，隔离 %.0f 秒",
            workstation_id,
            failures,
            quarantine_seconds,
        )

    def _clear_solver_failures_for_workstation(self, workstation_id: str) -> None:
        self._workstation_solver_failures.pop(workstation_id, None)
        self._workstation_quarantine_until.pop(workstation_id, None)
        self._persist_solver_quarantine_state()

    def solver_quarantine_snapshot(self) -> dict[str, dict[str, int | float]]:
        now = time.time()
        snapshot: dict[str, dict[str, int | float]] = {}
        for workstation_id, until in list(self._workstation_quarantine_until.items()):
            remaining = int(max(0.0, until - now))
            if remaining <= 0:
                self._workstation_quarantine_until.pop(workstation_id, None)
                self._workstation_solver_failures.pop(workstation_id, None)
                self._persist_solver_quarantine_state()
                continue
            snapshot[workstation_id] = {
                "remaining_seconds": remaining,
                "failure_count": self._workstation_solver_failures.get(workstation_id, 0),
                "until": until,
            }
        return snapshot

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
        self._solver_thread_workstations.clear()
        self._solver_active_by_workstation.clear()

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
            total_configs = len(all_configs)

            # ---- 前置检查：SW 阶段是否已全部终结且有错误 ----
            # 若 SW 宏执行完毕但所有构型的 STEP 均缺失，后续流程无法推进。
            sw_counts = self.state.get_step_status_counts("sw")
            sw_all_terminal = self._counts_all_terminal(sw_counts, total_configs)
            sw_has_completed = sw_counts.get(STATUS_COMPLETED, 0) > 0
            if sw_all_terminal and not sw_has_completed:
                logger.error("=" * 60)
                logger.error(">>> 流水线中止！所有构型的 SW 步骤均已失败 <<<")
                logger.error("=" * 60)
                logger.error("宏已执行但未产出任何有效 STEP 文件，无法继续。")
                logger.error("请检查：SW 宏逻辑 / 设计表参数 / STEP 输出路径。")
                self._stopped.set()
                self.state.set_engine_status("stopped")
                break

            # 检查是否已有工作站的 Meshing 屏障通过
            if self.dispatch_solver_if_ready() and self.state.all_configs_completed_at_step("meshing"):
                break

            # 检查是否所有 Meshing 均已终结（Completed 或 Error）
            meshing_counts = self.state.get_step_status_counts("meshing")
            all_terminal = self._counts_all_terminal(meshing_counts, total_configs)
            has_error = meshing_counts.get(STATUS_ERROR, 0) > 0

            if all_terminal and has_error:
                if meshing_counts.get(STATUS_COMPLETED, 0) > 0:
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

        Solver 以工作站为并发边界：同一工作站串行执行，多个已通过
        屏障的工作站可并行求解。
        若暂停标志已置位，则等待恢复后再分发。
        """
        # ★ 分发前检查暂停标志（统一使用 PauseGuard）
        if self._guard.check_should_abort():
            logger.warning("Solver 分发前检测到停止标志，取消分发")
            return False

        allowed_snapshot = (
            set(allowed_workstations)
            if allowed_workstations is not None
            else self._ready_workstations_for_solver()
        )
        if not allowed_snapshot:
            return False

        started_any = False
        live_threads_exist = False
        with self._solver_dispatch_lock:
            self._solver_threads = [t for t in self._solver_threads if t.is_alive()]
            self._solver_thread_workstations = {
                t: workstation_id
                for t, workstation_id in self._solver_thread_workstations.items()
                if t.is_alive()
            }
            live_workstations = set(self._solver_thread_workstations.values())
            live_threads_exist = bool(self._solver_threads)

            for workstation_id in sorted(allowed_snapshot):
                if workstation_id in live_workstations:
                    logger.info(
                        "[Solver] 工作站 %s 调度线程已在运行，跳过重复启动",
                        workstation_id,
                    )
                    continue

                workstation_scope = {workstation_id}
                if self._next_solver_config(workstation_scope) is None:
                    continue

                if not started_any:
                    logger.info("=" * 60)
                    logger.info("开始按工作站调度仿真求解任务...")
                    logger.info("=" * 60)
                t = threading.Thread(
                    target=self._solver_dispatch_loop,
                    args=(workstation_scope,),
                    name=f"SolverDispatcher-{workstation_id}",
                    daemon=True,
                )
                t.start()
                self._solver_threads.append(t)
                self._solver_thread_workstations[t] = workstation_id
                started_any = True

            if not started_any and not live_threads_exist:
                logger.info("[Solver] 当前没有待执行的求解任务")
                self._report_solver_terminal_if_ready()
                return False

        if started_any:
            logger.info("[Solver] 已启动 %s 个工作站调度线程", len(allowed_snapshot))
        return started_any or live_threads_exist

    def _next_solver_config(
        self,
        allowed_workstations: set[str] | None = None,
    ) -> int | None:
        """返回下一个可执行 Solver 的构型；没有则返回 None。"""
        schedulable_statuses = (
            STATUS_WAITING,
            STATUS_RUNNING,
            STATUS_PAUSED,
            STATUS_RETRYING,
            STATUS_UNKNOWN_REMOTE,
        )

        # Daemon 重启后内存中的工作站调度线程列表为空，但远程 Fluent
        # 任务可能仍在运行。必须先恢复这些 Running 任务的轮询，避免同一
        # 工作站启动新的独立 PostProcess/Solver 并争用工作目录和 Fluent 资源。
        for cn in self.state.get_all_configs():
            if self.state.get_step_status(cn, "meshing") != STATUS_COMPLETED:
                continue
            if (
                allowed_workstations is not None
                and self._workstation_for_config(cn) not in allowed_workstations
            ):
                continue
            solver_status = self.state.get_step_status(cn, "solver")
            postprocess_status = self.state.get_step_status(cn, "postprocess")
            if solver_status in {STATUS_RUNNING, STATUS_UNKNOWN_REMOTE}:
                return cn
            if solver_status == STATUS_COMPLETED and postprocess_status == STATUS_RUNNING:
                return cn

        # PostProcess 是 Solver 的尾部阶段。若某些构型已经完成 Solver，
        # 优先补齐这些后处理 backlog，再启动新的 Solver 任务。
        for cn in self.state.get_all_configs():
            if self.state.get_step_status(cn, "meshing") != STATUS_COMPLETED:
                continue
            if (
                allowed_workstations is not None
                and self._workstation_for_config(cn) not in allowed_workstations
            ):
                continue
            workstation_id = self._workstation_for_config(cn)
            if self._is_workstation_quarantined(workstation_id):
                continue
            solver_status = self.state.get_step_status(cn, "solver")
            postprocess_status = self.state.get_step_status(cn, "postprocess")
            if (
                solver_status == STATUS_COMPLETED
                and postprocess_status in schedulable_statuses
            ):
                return cn

        for cn in self.state.get_all_configs():
            if self.state.get_step_status(cn, "meshing") != STATUS_COMPLETED:
                continue
            if (
                allowed_workstations is not None
                and self._workstation_for_config(cn) not in allowed_workstations
            ):
                continue
            workstation_id = self._workstation_for_config(cn)
            if self._is_workstation_quarantined(workstation_id):
                continue
            solver_status = self.state.get_step_status(cn, "solver")
            if solver_status in schedulable_statuses:
                return cn
        return None

    def _solver_dispatch_loop(
        self,
        allowed_workstations: set[str] | None = None,
    ) -> None:
        """按工作站串行消费 Solver 任务，直到无待执行构型或收到停止指令。"""
        logger.info(
            "[Solver] 工作站调度循环启动: %s",
            ",".join(sorted(allowed_workstations)) if allowed_workstations else "all",
        )
        try:
            while not self._stopped.is_set():
                config_name = self._next_solver_config(allowed_workstations)
                if config_name is None:
                    break

                with self._solver_dispatch_lock:
                    self._solver_active_config = config_name
                    workstation_id = self._workstation_for_config(config_name)
                    self._solver_active_by_workstation[workstation_id] = config_name
                try:
                    if not self._execute_solver_for_config(config_name):
                        break
                except InfrastructureUnavailableError as e:
                    logger.warning(
                        "[Solver] 构型%s 基础设施不可用，等待恢复后重试: %s",
                        config_name,
                        e,
                    )
                    # 基础设施恢复后重新进入循环，尝试下一个可执行的构型
                    if not pause_aware_sleep(30, self._paused, self._stopped):
                        break
                finally:
                    with self._solver_dispatch_lock:
                        self._solver_active_config = None
                        workstation_id = self._workstation_for_config(config_name)
                        self._solver_active_by_workstation.pop(workstation_id, None)
        finally:
            self._report_solver_terminal_if_ready()
            logger.info("[Solver] 工作站调度循环退出")

    def _execute_solver_for_config(self, config_name: int) -> bool:
        """
        执行单个构型的仿真求解（在独立线程中运行）。

        Args:
            config_name: 构型名称
        """
        # ★ 执行前检查暂停标志（统一使用 PauseGuard）
        if self._guard.check_should_abort():
            return False

        generation = self._step_generation(config_name, "solver")
        if self.state.get_step_status(config_name, "solver") == STATUS_COMPLETED:
            self._execute_or_recover_postprocess_for_config(config_name)
            return True

        if self.state.get_step_status(config_name, "solver") in (
            STATUS_RUNNING,
            STATUS_PAUSED,
            STATUS_RETRYING,
            STATUS_UNKNOWN_REMOTE,
        ):
            remote_executor = self.runner.get_remote_executor()
            workstation_id = self._workstation_for_config(config_name)
            try:
                remote_status = remote_executor.query_remote_task_status(
                    config_name,
                    "solver",
                    workstation_id=workstation_id,
                )
            except InfrastructureUnavailableError as e:
                logger.warning(
                    f"[Solver] 构型{config_name} 查询远程状态时基础设施不可用: {e}",
                )
                self.state.set_step_status(
                    config_name, "solver", STATUS_RETRYING,
                    f"基础设施恢复中: {e}",
                )
                raise
            if remote_status == "completed":
                self.state.set_step_status(config_name, "solver", STATUS_COMPLETED)
                if self._is_stale_step_result(config_name, "solver", generation):
                    self._discard_stale_step_result(config_name, "solver")
                    return False
                self._clear_solver_failures_for_workstation(workstation_id)
                if not self.runner.register_postprocess_from_solver(config_name):
                    self.state.set_step_status(
                        config_name,
                        "postprocess",
                        STATUS_ERROR,
                        "无法接管远程 Solver 任务进行后处理",
                    )
                    return True
                self.state.set_step_status(config_name, "postprocess", STATUS_RUNNING)
                remote_executor.forget_remote_task(
                    config_name,
                    "solver",
                    workstation_id=workstation_id,
                )
                logger.info(f"[Solver] 构型{config_name} 重启后检测到完成标志")
                self._wait_for_postprocess_completion(config_name)
                return True
            if remote_status == "failed":
                self.state.set_step_status(
                    config_name,
                    "solver",
                    STATUS_ERROR,
                    "远程求解任务失败",
                )
                self._record_solver_failure_for_workstation(workstation_id)
                if self._is_stale_step_result(config_name, "solver", generation):
                    self._discard_stale_step_result(config_name, "solver")
                    return False
                remote_executor.forget_remote_task(
                    config_name,
                    "solver",
                    workstation_id=workstation_id,
                )
                return True
            if remote_status == "running":
                logger.info(f"[Solver] 构型{config_name} 远程任务仍在运行，恢复轮询")
                self._wait_for_solver_completion(config_name)
                if self._is_stale_step_result(config_name, "solver", generation):
                    self._discard_stale_step_result(config_name, "solver")
                    return False
                if self.state.get_step_status(config_name, "solver") == STATUS_COMPLETED:
                    self._clear_solver_failures_for_workstation(workstation_id)
                    if self.runner.register_postprocess_from_solver(config_name):
                        self.state.set_step_status(config_name, "postprocess", STATUS_RUNNING)
                        remote_executor.forget_remote_task(
                            config_name,
                            "solver",
                            workstation_id=workstation_id,
                        )
                        self._wait_for_postprocess_completion(config_name)
                    else:
                        self.state.set_step_status(
                            config_name,
                            "postprocess",
                            STATUS_ERROR,
                            "无法接管远程 Solver 任务进行后处理",
                        )
                return True
            if remote_status == "unknown":
                if self._record_unknown_remote_status(config_name, "solver"):
                    self._record_solver_failure_for_workstation(workstation_id)
                return False
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
                return False

            self._wait_for_solver_completion(config_name)
            if self._is_stale_step_result(config_name, "solver", generation):
                self._discard_stale_step_result(config_name, "solver")
                return False
            if self.state.get_step_status(config_name, "solver") == STATUS_COMPLETED:
                self._clear_solver_failures_for_workstation(
                    self._workstation_for_config(config_name)
                )
                if self.runner.register_postprocess_from_solver(config_name):
                    self.state.set_step_status(config_name, "postprocess", STATUS_RUNNING)
                    remote_executor = self.runner.get_remote_executor()
                    remote_executor.forget_remote_task(
                        config_name,
                        "solver",
                        workstation_id=self._workstation_for_config(config_name),
                    )
                    self._wait_for_postprocess_completion(config_name)
                else:
                    self.state.set_step_status(
                        config_name,
                        "postprocess",
                        STATUS_ERROR,
                        "无法接管远程 Solver 任务进行后处理",
                    )
            elif self.state.get_step_status(config_name, "solver") == STATUS_ERROR:
                self._record_solver_failure_for_workstation(
                    self._workstation_for_config(config_name)
                )
        return True

    def _execute_or_recover_postprocess_for_config(self, config_name: int) -> None:
        """恢复或启动 Solver 之后的 PostProcess 步骤。"""
        postprocess_status = self.state.get_step_status(config_name, "postprocess")
        if postprocess_status == STATUS_COMPLETED:
            return
        if postprocess_status == STATUS_ERROR:
            return

        if postprocess_status in (STATUS_RUNNING, STATUS_PAUSED, STATUS_RETRYING):
            remote_executor = self.runner.get_remote_executor()
            workstation_id = self._workstation_for_config(config_name)
            try:
                remote_status = remote_executor.query_remote_task_status(
                    config_name,
                    "postprocess",
                    workstation_id=workstation_id,
                )
            except InfrastructureUnavailableError as e:
                logger.warning(
                    f"[PostProcess] 构型{config_name} 查询远程状态时基础设施不可用: {e}",
                )
                self.state.set_step_status(
                    config_name, "postprocess", STATUS_RETRYING,
                    f"基础设施恢复中: {e}",
                )
                raise
            if remote_status == "completed":
                self.state.set_step_status(config_name, "postprocess", STATUS_COMPLETED)
                remote_executor.forget_remote_task(
                    config_name,
                    "postprocess",
                    workstation_id=workstation_id,
                )
                return
            if remote_status == "failed":
                self.state.set_step_status(
                    config_name,
                    "postprocess",
                    STATUS_ERROR,
                    "远程后处理任务失败",
                )
                remote_executor.forget_remote_task(
                    config_name,
                    "postprocess",
                    workstation_id=workstation_id,
                )
                return
            if remote_status == "running":
                logger.info(f"[PostProcess] 构型{config_name} 远程任务仍在运行，恢复轮询")
                self._wait_for_postprocess_completion(config_name)
                return
            if remote_status == "unknown":
                self._record_unknown_remote_status(config_name, "postprocess")
                return
            self.state.set_step_status(config_name, "postprocess", STATUS_WAITING)
            remote_executor.forget_remote_task(
                config_name,
                "postprocess",
                workstation_id=workstation_id,
            )
            logger.warning(
                f"[PostProcess] 构型{config_name} 远程任务已丢失，重置为 Waiting 后重新启动"
            )

        self._execute_postprocess_for_config(config_name)

    def _wait_for_solver_completion(self, config_name: int) -> None:
        """轮询等待 Solver 完成，并按控制状态更新数据库。"""
        try:
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
                    error_message = str(
                        getattr(self.runner, "last_solver_error", "") or "求解超时"
                    )
                    self.state.set_step_status(
                        config_name, "solver", STATUS_ERROR, error_message
                    )
        except InfrastructureUnavailableError as e:
            logger.warning(
                f"[Solver] 构型{config_name} 基础设施不可用: {e}",
            )
            self.state.set_step_status(
                config_name, "solver", STATUS_RETRYING,
                f"基础设施恢复中: {e}",
            )
            raise

    def _execute_postprocess_for_config(self, config_name: int) -> None:
        """执行单个构型的后处理步骤。"""
        if self._guard.check_should_abort():
            return
        generation = self._step_generation(config_name, "postprocess")
        logger.info(f"[PostProcess] 构型{config_name} 开始后处理...")
        if self._retry_manager.execute_with_retry(
            config_name,
            "postprocess",
            self.runner.execute_postprocess,
        ):
            if self._guard.check_should_abort():
                return
            self._wait_for_postprocess_completion(config_name)
            if self._is_stale_step_result(config_name, "postprocess", generation):
                self._discard_stale_step_result(config_name, "postprocess")

    def _wait_for_postprocess_completion(self, config_name: int) -> None:
        """轮询等待 PostProcess 完成，并按控制状态更新数据库。"""
        try:
            if self.runner.wait_postprocess_completion(
                config_name,
                paused_event=self._paused,
                stopped_event=self._stopped,
            ):
                self.state.set_step_status(config_name, "postprocess", STATUS_COMPLETED)
                cleanup = getattr(self.runner, "cleanup_completed_postprocess_task", None)
                if callable(cleanup):
                    cleanup(config_name)
                logger.info(f"[PostProcess] 构型{config_name} 后处理完成 ✓")
            else:
                if self._paused.is_set():
                    self.state.set_step_status(
                        config_name,
                        "postprocess",
                        STATUS_PAUSED,
                        "等待后处理期间暂停",
                    )
                elif self._stopped.is_set():
                    self.state.set_step_status(
                        config_name,
                        "postprocess",
                        STATUS_PAUSED,
                        "引擎已停止",
                    )
                else:
                    self.state.set_step_status(
                        config_name,
                        "postprocess",
                        STATUS_ERROR,
                        "后处理超时",
                    )
        except InfrastructureUnavailableError as e:
            logger.warning(
                f"[PostProcess] 构型{config_name} 基础设施不可用: {e}",
            )
            self.state.set_step_status(
                config_name, "postprocess", STATUS_RETRYING,
                f"基础设施恢复中: {e}",
            )
            raise

    def _report_solver_terminal_if_ready(self) -> None:
        """PostProcess 全部终结时报告流水线自然完成或失败终态。"""
        if self._solver_terminal_reported or self._paused.is_set() or self._stopped.is_set():
            return
        all_configs = self.state.get_all_configs()
        if not all_configs:
            return

        has_error = False
        for cn in all_configs:
            solver_status = self.state.get_step_status(cn, "solver")
            postprocess_status = self.state.get_step_status(cn, "postprocess")
            if solver_status == STATUS_ERROR or postprocess_status == STATUS_ERROR:
                has_error = True
                continue
            if solver_status != STATUS_COMPLETED or postprocess_status != STATUS_COMPLETED:
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
