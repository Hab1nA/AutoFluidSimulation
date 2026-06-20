from __future__ import annotations

"""
===============================================================================
DAG 任务调度器 (Pipeline Scheduler)
实现了基于文件监控的 Producer-Consumer 异步队列和全局同步屏障。

调度逻辑：
1. SW 阶段：启动一次宏 → 文件监控 → 发现完成的 STEP → 推入 SC 队列
2. SC → Transfer → Meshing：异步流水线（每个构型独立推进）
3. 全局屏障：所有构型 Meshing 全部 Completed 后 → 解锁 Solver
4. Solver：所有构型并行启动求解

架构设计：
- Producer: StepFileMonitor 检测 STEP 文件 → 产出待处理构型
- Consumer: 多个工作线程从队列取构型，依次执行 SC → Transfer → Meshing
- Barrier Monitor: 独立线程检查全局屏障条件
- Solver Dispatcher: 屏障通过后统一启动所有求解任务
===============================================================================
"""
import threading
import os
from typing import Any

from engine.config import (
    STEP_INDEX, STEP_NAMES, ENGINE_CONFIG, REMOTE_CONFIG, DEFAULT_WORKSTATION_ID,
    STATUS_WAITING, STATUS_RUNNING, STATUS_PAUSED, STATUS_COMPLETED, STATUS_ERROR, STATUS_RETRYING,
    LOCAL_PATHS, get_step_filename, get_workstation_config, is_server_mode,
)
from engine.state_manager import StateManager
from engine.task_runner import TaskRunner
from engine.file_monitor import StepFileMonitor
from utils.logger import setup_logger

from .worker_pool import WorkerPoolManager
from .barrier import BarrierCoordinator
from .sw_phase import SWPhaseHandler
from .retry import RetryManager
from .meshing_monitor import MeshingMonitor
from .utils import check_step_output_exists, PauseGuard
from .work_queue import UniqueWorkQueue
from .control import PipelineControl

logger = setup_logger(__name__)


class PipelineScheduler:
    """
    流水线调度器。

    管理整个仿真流水线的执行顺序和并发控制。
    实现：
    - 基于文件事件的异步流水线（SW → SC/Transfer/Meshing）
    - 全局同步屏障（Meshing 全部完成 → Solver）
    - 暂停/继续/停止控制
    - 错误重试机制
    """

    def __init__(self, state_manager: StateManager, task_runner: TaskRunner):
        """
        初始化调度器。

        Args:
            state_manager: 共享状态管理器
            task_runner: 任务执行器
        """
        self.state = state_manager
        self.runner = task_runner

        # ---- 并发控制 ----
        self._control = PipelineControl()
        self._paused = self._control.paused_event
        self._stopped = self._control.stopped_event
        self._barrier_passed = threading.Event() # 全局屏障通过事件
        self._reset_generation_lock = threading.Lock()
        self._reset_generation_by_step: dict[tuple[int, str], int] = {}

        # 将控制事件注入 TaskRunner，使长时间阻塞操作（如 SC 的 process.communicate）
        # 能够响应暂停/停止指令
        self.runner.set_control_events(self._paused, self._stopped)
        set_pipeline_control = getattr(self.runner, "set_pipeline_control", None)
        if callable(set_pipeline_control):
            set_pipeline_control(self._control)

        # 暂停/停止守卫（统一各子模块的暂停检查逻辑）
        self._guard = PauseGuard(self._paused, self._stopped)

        # ---- 工作队列 ----
        # SC 处理队列：(config_name, step_file_path)
        self._sc_queue = UniqueWorkQueue[tuple[int, str]](key=lambda item: item[0])

        # ---- 子模块 ----
        self.retry_manager = RetryManager(
            state_manager=self.state,
            paused_event=self._paused,
            stopped_event=self._stopped,
        )
        self.worker_pool = WorkerPoolManager(
            state_manager=self.state,
            task_runner=self.runner,
            sc_queue=self._sc_queue,
            paused_event=self._paused,
            stopped_event=self._stopped,
            barrier_passed_event=self._barrier_passed,
            retry_manager=self.retry_manager,
            get_reset_generation=self._current_reset_generation,
        )
        self.barrier_coordinator = BarrierCoordinator(
            state_manager=self.state,
            task_runner=self.runner,
            paused_event=self._paused,
            stopped_event=self._stopped,
            barrier_passed_event=self._barrier_passed,
            retry_manager=self.retry_manager,
            on_solver_terminal=self.finalize_pipeline,
            get_reset_generation=self._current_reset_generation,
        )
        self.meshing_monitor = MeshingMonitor(
            state_manager=self.state,
            remote_executor=self.runner.get_remote_executor(),
            paused_event=self._paused,
            stopped_event=self._stopped,
            get_reset_generation=self._current_reset_generation,
            on_meshing_completed=self._on_meshing_completed,
        )
        self.sw_phase_handler = SWPhaseHandler(
            state_manager=self.state,
            task_runner=self.runner,
            sc_queue=self._sc_queue,
            paused_event=self._paused,
            stopped_event=self._stopped,
            retry_manager=self.retry_manager,
            worker_pool_manager=self.worker_pool,
            meshing_monitor=self.meshing_monitor,
        )

        # 注入 MeshingMonitor 到 WorkerPool
        self.worker_pool.set_meshing_monitor(self.meshing_monitor)

        # ---- 工作线程 ----
        self._barrier_thread: threading.Thread | None = None
        # 预创建文件监控器并注入 SW 阶段处理器（避免双重实例）
        self._file_monitor: StepFileMonitor | None = StepFileMonitor(
            step_dir=None,
            on_file_ready=self._on_step_file_ready,
            shared_paused_event=self._paused,
        )
        self.sw_phase_handler.set_file_monitor(self._file_monitor)
        self._pipeline_thread: threading.Thread | None = None  # 主调度线程引用

        # 恢复全局屏障状态（断点续传）
        if self.state.is_global_barrier_met():
            self._barrier_passed.set()

        logger.info("流水线调度器初始化完成")

    def _current_reset_generation(self, config_name: int, step_name: str) -> int:
        """Return reset generation for a config step."""
        with self._reset_generation_lock:
            return self._reset_generation_by_step.get((int(config_name), step_name), 0)

    def _mark_reset_generation(self, config_name, step_name: str | None) -> None:
        """Bump reset generation for every config step affected by reset."""
        if config_name == "all":
            config_names = [int(cn) for cn in self.state.get_all_configs()]
        else:
            config_names = [int(config_name)]

        start_idx = STEP_INDEX.get(step_name, 0) if step_name else 0
        affected_steps = STEP_NAMES[start_idx:]
        with self._reset_generation_lock:
            for cn in config_names:
                for affected_step in affected_steps:
                    key = (cn, affected_step)
                    self._reset_generation_by_step[key] = (
                        self._reset_generation_by_step.get(key, 0) + 1
                    )

    # ------------------------------------------------------------------
    # 主调度入口
    # ------------------------------------------------------------------

    def start_pipeline(self, _recursion_depth: int = 0):
        """
        启动（或继续）流水线。

        执行流程：
        1. 检查 SW 宏是否已执行 → 未执行则启动 SW 宏
        2. 启动文件监控器
        3. 启动 SC/Transfer/Meshing 工作线程
        4. 启动全局屏障监控线程

        Args:
            _recursion_depth: 内部递归深度计数器（外部调用方不应指定）
        """
        try:
            self._start_pipeline_impl(_recursion_depth)
        except Exception as e:
            self._handle_scheduler_thread_exception(e)

    def _start_pipeline_impl(self, _recursion_depth: int = 0) -> None:
        if _recursion_depth >= 3:
            logger.error(
                f"start_pipeline 递归深度超过上限 ({_recursion_depth})，"
                f"可能存在无法自动恢复的错误，流水线中止。"
            )
            self._handle_recursion_limit_exceeded(_recursion_depth)
            return

        logger.info("=" * 60)
        logger.info("流水线调度器启动")
        logger.info("=" * 60)

        # 记录当前执行线程为主调度线程（供外部查询存活状态）
        self._pipeline_thread = threading.current_thread()

        # 若 start 命令刚创建调度线程，pause 命令可能在线程真正执行前到达。
        # 原子准备仅清除旧 stop，不会越过已到达的 pause。
        if not self._control.prepare_start():
            logger.info("调度器启动前已收到暂停指令，暂停启动流程，等待 resume 指令")
            self.state.set_engine_status("paused")
            return

        # 注意：不在此处清除 _paused。
        # - 初始启动时 _paused 应为 False，无需清除
        # - resume() 已在调用 start_pipeline 之前清除 _paused
        # - 避免与 IPC 线程的 pause() 产生 TOCTOU 竞态

        # 屏障监控可以早于 SW 完成启动：SW 边导出边触发下游流水线，
        # Meshing 可能在 SW 尾部收尾前已经运行，避免后续补启动造成日志错位。
        if (
            not self._barrier_passed.is_set()
            and not self._has_downstream_errors()
        ):
            self._ensure_barrier_monitor_running()

        # ---- 步骤 1: SW 阶段 ----
        sw_result = self.sw_phase_handler.execute_sw_phase(_recursion_depth)
        if not sw_result:
            # sw_phase_handler 显式设置了 needs_recurse 标志 → 断点续传检测到 SW Error，需要递归
            if self.sw_phase_handler.needs_recurse:
                self.sw_phase_handler.needs_recurse = False  # 清除标志避免重复
                self.start_pipeline(_recursion_depth + 1)
                return
            # SW 致命失败 / 已暂停 / 已停止 → 直接返回
            return

        # ★ 暂停检查：SW 阶段完成后，若暂停标志已置位，停止后续组件启动。
        #   避免 pause 指令在 SW 宏执行期间到达后，宏完成后仍启动文件监控、
        #   工作线程池、屏障监控等下游组件（不符合暂停语义）。
        with self._control.external_start() as can_start_downstream:
            if not can_start_downstream:
                logger.info("SW 阶段已完成，但暂停标志已置位，暂停后续组件启动，等待 resume 指令")
                if self._paused.is_set():
                    self.state.set_engine_status("paused")
                return

        # ---- 步骤 1.5: 同步下游步骤状态与文件系统 ----
        self.sw_phase_handler.scan_completed_downstream()

        # ---- 步骤 2: 断点续传扫描 ----
        # start 场景同样需要恢复内存队列。不能只依赖 STEP 文件监控：
        # 已完成 SC 但 Transfer 未完成的构型不会再产生 STEP 文件事件。
        self._resume_paused_steps(log_prefix="[Start]")

        # ---- 步骤 2.5: 按需启动文件监控 ----
        if self._needs_step_file_monitor():
            self._ensure_file_monitor_running()
        else:
            logger.info(
                "[Scheduler] STEP 文件监控无需启动：SW 阶段已无待生成 STEP，"
                "断点任务已由状态扫描恢复"
            )

        # ---- 步骤 3: 启动 MeshingMonitor（先于 Worker Pool，确保队列消费者就绪） ----
        self.meshing_monitor.start_if_needed()

        # ---- 步骤 3.5: 启动工作线程池（仅在未启动时创建） ----
        if not self.worker_pool.is_running():
            self.worker_pool.start_if_needed()

        # ---- 步骤 4: 写回运行态并收口屏障 ----
        # 与 pause() 共用控制锁，避免 pause() 返回后后台启动线程再写回 running
        # 或继续触发 Solver 分发。
        with self._control.external_start() as can_finalize:
            if not can_finalize:
                logger.info("流水线组件已就绪，但暂停/停止标志已置位，跳过后续启动")
                if self._paused.is_set():
                    self.state.set_engine_status("paused")
                return

            if self.state.get_engine_status() != "running":
                self.state.set_engine_status("running")

            # 屏障已满足时直接分发 Solver，否则启动全局屏障监控。
            self._finalize_barrier_after_downstream_start()

        logger.info("流水线调度器已启动，等待 STEP 文件...")

    def _handle_scheduler_thread_exception(self, exc: Exception) -> None:
        """调度器主线程未捕获异常时回收运行态。"""
        logger.critical(
            f"[Scheduler] 调度器线程异常退出: {type(exc).__name__}: {exc}",
            exc_info=True,
        )
        self._control.stop()
        self.state.set_engine_status("stopped")

    def finalize_pipeline(self, outcome: str) -> None:
        """流水线自然终结收尾，不复用用户 stop() 的暂停落库逻辑."""
        if outcome == "completed":
            logger.info("[Scheduler] 全部构型处理完成，开始自然收尾")
            cleanup_flags = getattr(self.runner, "cleanup_solver_runtime_flag_artifacts", None)
            if callable(cleanup_flags):
                try:
                    result = cleanup_flags()
                    logger.info(
                        "[Scheduler] Solver flags 运行产物清理完成: "
                        f"deleted={result.get('deleted', 0)}, failed={result.get('failed', 0)}"
                    )
                except Exception as e:
                    logger.warning(f"[Scheduler] Solver flags 运行产物清理异常: {e}")
        else:
            logger.error("[Scheduler] Solver 阶段终结但存在错误，开始失败收尾")

        self._control.stop()
        self.state.set_engine_status("stopped")

        if self._file_monitor is not None:
            self._file_monitor.stop()

        self.worker_pool.join_worker_threads(timeout=3)

        try:
            self.runner.disconnect_ssh()
        except Exception as e:
            logger.warning(f"[Scheduler] 自然收尾断开 SSH 异常: {e}")

        logger.info("[Scheduler] 流水线终态收尾完成")

    def _handle_recursion_limit_exceeded(self, recursion_depth: int):
        """处理递归深度超限的情况。"""
        # 1) 停止所有并行的调度线程（worker / barrier / monitor）
        self._control.stop()

        # 2) 将所有仍在非终态的构型 SW 步骤标记为 Error，附带详细诊断信息
        all_configs = self.state.get_all_configs()
        terminal_states = {STATUS_COMPLETED, STATUS_ERROR, STATUS_PAUSED}
        error_detail = (
            f"SW 宏自动重试递归深度超过上限 ({recursion_depth})，"
            f"可能存在设计表参数不匹配、模型文件损坏或 COM 通信故障。"
            f"请检查 SW 日志、Excel 参数表及模型文件后使用 reset all 重新启动。"
        )
        error_count = 0
        for cn in all_configs:
            sw_st = self.state.get_step_status(cn, "sw")
            if sw_st not in terminal_states:
                self.state.set_step_status(cn, "sw", STATUS_ERROR, error_detail)
                error_count += 1
                logger.error(f"[Scheduler] 构型{cn} SW 标记为 Error（递归深度超限）")

        logger.error(
            f"已标记 {error_count}/{len(all_configs)} 个构型的 SW 步骤为 Error，"
            "未执行的下游步骤保持 Waiting"
        )

        # 3) 清除 sw_macro_started 标志 → 允许用户直接 start 重试
        self.state.set_sw_macro_started(False)

        # 4) 引擎状态切为 stopped，TUI 可显示 "已停止"
        self.state.set_engine_status("stopped")

        # 5) 停止文件监控器（已在 _stopped 置位时由主循环感知）
        #    但显式调用 stop() 避免监控线程无限等待
        if self._file_monitor is not None:
            self._file_monitor.stop()

    def _ensure_file_monitor_running(self):
        """确保文件监控器正在运行。"""
        if self._file_monitor is not None and not self._file_monitor.is_running:
            self._file_monitor.start()

    def _needs_step_file_monitor(self) -> bool:
        """判断当前启动阶段是否仍需 STEP 文件监控。

        文件监控主要服务 SW 正在生成 STEP 的场景。若 SW 已无可继续生成
        STEP 的构型，断点恢复应由数据库状态扫描直接入队，避免旧 STEP
        文件被再次稳定性检测并触发无效回调。
        """
        if is_server_mode():
            return False
        if self._file_monitor is None:
            return False
        if self._file_monitor.is_running:
            return True

        active_sw_states = {
            STATUS_WAITING,
            STATUS_RUNNING,
            STATUS_PAUSED,
            STATUS_RETRYING,
        }
        return any(
            self.state.get_step_status(cn, "sw") in active_sw_states
            for cn in self.state.get_all_configs()
        )

    def _ensure_barrier_monitor_running(self):
        """确保全局屏障监控线程正在运行。"""
        if self._barrier_thread is None or not self._barrier_thread.is_alive():
            self._barrier_thread = threading.Thread(
                target=self.barrier_coordinator.monitor_loop,
                daemon=True,
                name="BarrierMonitor"
            )
            self._barrier_thread.start()

    def _on_meshing_completed(self, _config_name: int) -> None:
        """Meshing 完成后立即尝试收口屏障，避免等待下一轮轮询。"""
        if self._paused.is_set() or self._stopped.is_set():
            return
        self.barrier_coordinator.dispatch_solver_if_ready()

    def _finalize_barrier_after_downstream_start(self) -> None:
        """下游组件启动后，收口屏障监控和 Solver 分发。"""
        if not self.barrier_coordinator.dispatch_solver_if_ready():
            self._ensure_barrier_monitor_running()

    def _has_downstream_errors(self) -> bool:
        """检查是否存在需先由恢复扫描处理的下游 Error 状态。"""
        downstream_steps = ("sc", "transfer", "meshing", "solver", "postprocess")
        return any(
            self.state.get_step_status(cn, step) == STATUS_ERROR
            for cn in self.state.get_all_configs()
            for step in downstream_steps
        )

    # ------------------------------------------------------------------
    # 文件就绪回调（Producer 端）
    # ------------------------------------------------------------------

    def _on_step_file_ready(self, config_name: int, filepath: str) -> None:
        if self._stopped.is_set():
            logger.info(f"构型{config_name} STEP 文件就绪，但系统已停止，跳过入队")
            return
        if self._paused.is_set():
            logger.info(f"构型{config_name} STEP 文件就绪，但系统已暂停，跳过入队")
            return

        current_sw = self.state.get_step_status(config_name, "sw")
        if current_sw != STATUS_COMPLETED:
            self.state.set_step_status(config_name, "sw", STATUS_COMPLETED)

        # ★ 快速跳过：若 SC 已在队列中或正在执行/已完成，直接返回
        current_sc = self.state.get_step_status(config_name, "sc")
        if current_sc in (STATUS_RUNNING, STATUS_COMPLETED):
            logger.debug(f"构型{config_name} STEP 文件就绪，但 SC 已 {current_sc}，跳过入队")
            return

        # 断点续传防护：若 SC/Transfer/Meshing 已全部完成，跳过推入队列
        downstream_completed = all(
            self.state.get_step_status(config_name, s) == STATUS_COMPLETED
            for s in ["sc", "transfer", "meshing"]
        )
        if downstream_completed:
            logger.info(f"构型{config_name} 下游步骤已完成，跳过入队")
            return

        # 推入 SC 处理队列（通过 _enqueue_sc 实现去重）
        step_dir = LOCAL_PATHS.get("step_dir", "")
        self._enqueue_sc(config_name, step_dir)

    # ------------------------------------------------------------------
    # 控制接口
    # ------------------------------------------------------------------

    def pause(self):
        """暂停流水线。

        注意：不再立即将 Running 步骤标记为 Paused。
        正在执行的步骤会继续运行直到完成，然后根据执行结果决定最终状态
        （Completed/Error），避免 pause→resume 时步骤被重复入队。
        """
        logger.info("收到暂停指令")
        self._control.pause()
        # 不再调用 set_all_running_to_paused()，让正在运行的步骤自然完成
        self.state.set_engine_status("paused")
        if self._file_monitor is not None:
            self._file_monitor.pause()
        logger.info("流水线已暂停，正在运行的步骤将继续执行直到完成")

    @property
    def is_paused(self) -> bool:
        """公共只读属性：是否处于暂停状态（供外部模块查询）。"""
        return self._paused.is_set()

    @property
    def pipeline_alive(self) -> bool:
        """公共只读属性：主调度线程是否存活（供外部模块查询）。"""
        return self._pipeline_thread is not None and self._pipeline_thread.is_alive()

    def set_pipeline_thread(self, thread: threading.Thread) -> None:
        """设置主调度线程引用（供外部模块在启动新线程后注入）。"""
        self._pipeline_thread = thread

    def _workstation_for_config(self, config_name: int) -> str:
        """Return assigned workstation for a config, preserving legacy default."""
        get_config_workstation = getattr(self.state, "get_config_workstation", None)
        if callable(get_config_workstation):
            workstation_id = get_config_workstation(config_name)
            if workstation_id:
                return str(workstation_id)
        return DEFAULT_WORKSTATION_ID

    @staticmethod
    def _remote_config_for_workstation(workstation_id: str) -> dict[str, object]:
        """Return remote config for output checks, preserving legacy default."""
        if workstation_id == DEFAULT_WORKSTATION_ID:
            return dict(REMOTE_CONFIG)
        return dict(get_workstation_config(workstation_id))

    def _resume_paused_steps(self, log_prefix: str = "[Resume]") -> None:
        """
        断点续传扫描：对每个构型从 SW 开始逐步检查，
        找到第一个非 COMPLETED 步骤后推入对应队列。

        - COMPLETED → 继续检查下一步骤
        - RUNNING   → 跳过（正在执行）
        - PAUSED    → 检查输出文件是否存在，存在则标记完成继续，否则优先入队
        - WAITING   → 普通入队
        - ERROR/RETRYING → 检查重试次数，未达上限则重置为 WAITING 并入队，否则保留 ERROR
        """
        step_dir = LOCAL_PATHS.get("step_dir", "")
        scdoc_dir = LOCAL_PATHS.get("scdoc_dir", "")
        max_retries = ENGINE_CONFIG["max_retries"]

        # 优先队列：PAUSED 构型先入队
        priority_enqueue: list[tuple[int, str]] = []   # (cn, step)
        normal_enqueue: list[tuple[int, str]] = []     # (cn, step)

        for cn in self.state.get_all_configs():
            for step in STEP_NAMES:
                status = self.state.get_step_status(cn, step)

                if status == STATUS_COMPLETED:
                    if (
                        step in {"sw", "sc", "meshing", "solver", "postprocess"}
                        and self._completed_step_output_exists(
                            cn, step, step_dir, scdoc_dir
                        ) is False
                    ):
                        logger.warning(
                            f"{log_prefix} 构型{cn} [{step}] 状态为 Completed "
                            "但输出文件缺失，重置为 Waiting"
                        )
                        self._forget_completed_remote_task_if_tracked(cn, step)
                        self.state.reset_config_steps(cn, step)
                        if STEP_INDEX.get(step, 99) <= STEP_INDEX.get("meshing", 99):
                            workstation_id = self._workstation_for_config(cn)
                            self._barrier_passed.clear()
                            self.barrier_coordinator.clear_workstation_barrier(workstation_id)
                            self.state.set_global_barrier_met(False)
                        status = STATUS_WAITING
                    else:
                        continue

                if step == "sc" and self._has_started_downstream(cn):
                    logger.debug(
                        f"{log_prefix} 构型{cn} [sc] 状态={status}，"
                        "但下游已有执行记录，跳过 SC 恢复入队",
                        extra={"broadcast": False},
                    )
                    break

                # ★ 全局 in-flight 保护：无论步骤处于何种非 COMPLETED 状态，
                #   只要队列中有该构型的 claim 或 MeshingMonitor 正在处理，
                #   就跳过该构型（不重置状态、不重复入队）。
                if self._is_step_in_flight(cn, step):
                    logger.debug(
                        f"{log_prefix} 构型{cn} [{step}] 状态={status} "
                        f"但正在活跃处理中，跳过"
                    )
                    break  # 该构型有活跃步骤，不重置，不检查后续步骤

                # ---- 找到第一个非 COMPLETED 步骤 ----

                if status == STATUS_RUNNING:
                    if step in ("meshing", "solver", "postprocess"):
                        remote_executor = self.runner.get_remote_executor()
                        workstation_id = self._workstation_for_config(cn)
                        remote_status = remote_executor.query_remote_task_status(
                            cn,
                            step,
                            workstation_id=workstation_id,
                        )
                        if remote_status == "completed":
                            self.state.set_step_status(cn, step, STATUS_COMPLETED)
                            if step == "solver":
                                if not self.runner.register_postprocess_from_solver(cn):
                                    self.state.set_step_status(
                                        cn,
                                        "postprocess",
                                        STATUS_ERROR,
                                        "无法接管远程 Solver 任务进行后处理",
                                    )
                                    break
                                self.state.set_step_status(cn, "postprocess", STATUS_RUNNING)
                            remote_executor.forget_remote_task(
                                cn,
                                step,
                                workstation_id=workstation_id,
                            )
                            continue
                        if remote_status == "failed":
                            self.state.set_step_status(
                                cn,
                                step,
                                STATUS_ERROR,
                                f"远程 {step} 任务失败",
                            )
                            remote_executor.forget_remote_task(
                                cn,
                                step,
                                workstation_id=workstation_id,
                            )
                            break
                        if remote_status == "running":
                            logger.warning(
                                f"{log_prefix} 构型{cn} [{step}] 状态={status}，"
                                "远程状态=running，保留 Running 并跳过重启"
                            )
                            break
                        if remote_status == "unknown":
                            self._record_unknown_remote_status(cn, step, log_prefix)
                            break

                    # ★ 孤立 RUNNING 检测：Daemon 重启或 stop() 后，
                    #   步骤可能停留在 RUNNING 状态（进程已不存在）。
                    #   检查输出文件：存在则标记完成，否则重置为 Waiting 重新执行。
                    if self._check_step_output_exists(cn, step, step_dir, scdoc_dir):
                        self.state.set_step_status(cn, step, STATUS_COMPLETED)
                        continue
                    else:
                        self.state.set_step_status(cn, step, STATUS_WAITING)
                        if step in ("meshing", "solver", "postprocess"):
                            self.runner.get_remote_executor().forget_remote_task(
                                cn,
                                step,
                                workstation_id=self._workstation_for_config(cn),
                            )
                        if step == "sw":
                            # SW 步骤由 start_pipeline 统一处理
                            break
                        elif step == "sc":
                            self._enqueue_sc(cn, step_dir)
                        elif step == "transfer":
                            self.worker_pool.submit_transfer(cn)
                        elif step == "meshing":
                            if self.meshing_monitor is not None:
                                self.meshing_monitor.submit(cn)
                        break

                if status == STATUS_PAUSED:
                    if self._check_step_output_exists(cn, step, step_dir, scdoc_dir):
                        self.state.set_step_status(cn, step, STATUS_COMPLETED)
                        self._forget_completed_remote_task_if_tracked(cn, step)
                        continue
                    else:
                        priority_enqueue.append((cn, step))
                        break

                if status == STATUS_WAITING:
                    normal_enqueue.append((cn, step))
                    break

                if status in (STATUS_ERROR, STATUS_RETRYING):
                    retry_count = self.state.get_step_retry_count(cn, step)
                    if retry_count < max_retries:
                        self.state.set_step_status(cn, step, STATUS_WAITING)
                        # 注：SW 重试由 start_pipeline 的 SW 阶段统一处理，
                        # 此处重置为 WAITING 后，下次 start_pipeline 会检测到并重新执行
                        if step != "sw":
                            normal_enqueue.append((cn, step))
                    break

                break  # 未知状态，跳过

            else:
                self._forget_completed_config_remote_tasks(cn)
                logger.info(f"{log_prefix} 构型{cn} 所有步骤已完成，跳过恢复")

        # ---- 统一入队（PAUSED 优先）----
        for cn, step in priority_enqueue:
            if step == "sc":
                self._enqueue_sc(cn, step_dir)
            elif step == "transfer":
                self.worker_pool.submit_transfer(cn)
            elif step == "meshing":
                # MeshingMonitor 管理 Meshing 生命周期
                if self.meshing_monitor is not None:
                    self.state.set_step_status(cn, "meshing", STATUS_WAITING)
                    self.meshing_monitor.submit(cn)
            elif step == "solver":
                # Solver 由屏障调度器统一管理，保持当前状态等屏障通过后处理
                pass
            elif step == "postprocess":
                self.barrier_coordinator.dispatch_solver_if_ready()

        for cn, step in normal_enqueue:
            if step == "sc":
                self._enqueue_sc(cn, step_dir)
            elif step == "transfer":
                self.worker_pool.submit_transfer(cn)
            elif step == "meshing":
                if self.meshing_monitor is not None:
                    self.state.set_step_status(cn, "meshing", STATUS_WAITING)
                    self.meshing_monitor.submit(cn)
            elif step == "solver":
                pass
            elif step == "postprocess":
                self.barrier_coordinator.dispatch_solver_if_ready()

    def _record_unknown_remote_status(
        self,
        config_name: int,
        step_name: str,
        log_prefix: str,
    ) -> bool:
        """
        Count unknown remote probes and surface an error once retry budget is exhausted.

        Returns True when the step has been marked Error.
        """
        retry_count = self.state.increment_retry(config_name, step_name)
        max_retries = int(ENGINE_CONFIG["max_retries"])
        if retry_count >= max_retries:
            self.state.set_step_status(
                config_name,
                step_name,
                STATUS_ERROR,
                f"远程 {step_name} 状态连续 {retry_count} 次未知，停止等待",
            )
            logger.error(
                "%s 构型%s [%s] 远程状态 unknown 达到重试上限，标记为 Error",
                log_prefix,
                config_name,
                step_name,
            )
            return True
        logger.warning(
            "%s 构型%s [%s] 远程状态 unknown，保留 Running 等待后续重试 (%s/%s)",
            log_prefix,
            config_name,
            step_name,
            retry_count,
            max_retries,
        )
        return False

    def _check_step_output_exists(
        self, cn: int, step: str, step_dir: str, scdoc_dir: str
    ) -> bool:
        """检查某步骤的输出文件是否已存在（委托给统一函数）。"""
        ssh = None
        workstation_id = self._workstation_for_config(cn)
        remote_config = self._remote_config_for_workstation(workstation_id)
        if step in {"transfer", "meshing", "solver", "postprocess"}:
            try:
                ssh = self.runner.get_ssh(workstation_id)
            except Exception:
                pass
        return check_step_output_exists(
            cn, step, step_dir, scdoc_dir, remote_config, ssh
        )

    def _completed_step_output_exists(
        self, cn: int, step: str, step_dir: str, scdoc_dir: str
    ) -> bool | None:
        """Validate Completed status against durable artifacts, not only flags."""
        if step in {"sw", "sc"}:
            if is_server_mode():
                return True
            return self._check_step_output_exists(cn, step, step_dir, scdoc_dir)
        if step == "meshing":
            return self._remote_files_exist(cn, ("meshing",))
        if step == "solver":
            return self._remote_files_exist(cn, ("solver", "solverdata"))
        if step == "postprocess":
            return self._remote_files_exist(cn, ("postprocess",))
        return True

    def _remote_files_exist(self, cn: int, output_steps: tuple[str, ...]) -> bool | None:
        """Return True/False for confirmed remote output state, or None if SSH is indeterminate."""
        workstation_id = self._workstation_for_config(cn)
        remote_config = self._remote_config_for_workstation(workstation_id)
        ssh = None
        try:
            ssh = self.runner.get_ssh(workstation_id)
        except Exception:
            return None

        try:
            if not ssh.is_connected():
                return None
        except Exception:
            return None

        for output_step in output_steps:
            if output_step == "postprocess":
                postprocess_exists = self._postprocess_artifact_exists(cn, remote_config, ssh)
                if postprocess_exists is None:
                    return None
                if not postprocess_exists:
                    return False
                continue
            filename = get_step_filename(output_step, cn)
            if not filename:
                return False
            directory_key = "msh_dir" if output_step == "meshing" else "result_dir"
            remote_dir = str(remote_config[directory_key]).replace("\\", "/")
            remote_path = f"{remote_dir}/{filename}"
            try:
                if hasattr(ssh, "get_remote_file_size"):
                    size = ssh.get_remote_file_size(remote_path, timeout=5.0)
                    if size is None or size <= 0:
                        return False
                    continue
                if not ssh.check_remote_file(remote_path, timeout=5.0):
                    return False
            except TypeError:
                if not ssh.check_remote_file(remote_path):
                    return False
            except Exception:
                return None
        return True

    def _postprocess_artifact_exists(
        self,
        cn: int,
        remote_config: dict[str, object],
        ssh: Any,
    ) -> bool | None:
        """Return postprocess output state, or None when SSH probing cannot be trusted."""
        output_dir = str(
            remote_config.get("postprocess_output_dir")
            or ENGINE_CONFIG.get("postprocess_output_dir")
            or remote_config.get("result_dir", "")
        ).replace("\\", "/").rstrip("/")
        metrics_dir = str(
            remote_config.get("postprocess_metrics_dir")
            or ENGINE_CONFIG.get("postprocess_metrics_dir")
            or output_dir
        ).replace("\\", "/").rstrip("/")
        animation_dir = str(
            remote_config.get("postprocess_animation_dir")
            or ENGINE_CONFIG.get("postprocess_animation_dir")
            or remote_config.get("animation_dir", "")
        ).replace("\\", "/").rstrip("/")
        candidates = []
        if output_dir:
            candidates.extend([
                f"{output_dir}/model_gen4_{cn}.csv",
                f"{output_dir}/model_gen4_{cn}.json",
            ])
        if metrics_dir:
            candidates.append(f"{metrics_dir}/model_gen4_{cn}.csv")
        if animation_dir:
            candidates.extend([
                f"{animation_dir}/t_gen4_{cn}.mp4",
                f"{animation_dir}/v_gen4_{cn}.mp4",
            ])
        for remote_path in candidates:
            try:
                if hasattr(ssh, "get_remote_file_size"):
                    size = ssh.get_remote_file_size(remote_path, timeout=5.0)
                    if size is not None and size > 0:
                        return True
                    continue
                if ssh.check_remote_file(remote_path, timeout=5.0):
                    return True
            except TypeError:
                if ssh.check_remote_file(remote_path):
                    return True
            except Exception:
                return None
        return False

    def _forget_completed_config_remote_tasks(self, cn: int) -> None:
        """清理已完成构型残留的远程任务元数据。"""
        for step in ("meshing", "solver", "postprocess"):
            self._forget_completed_remote_task_if_tracked(cn, step)

    def _forget_completed_remote_task_if_tracked(self, cn: int, step: str) -> None:
        """只在状态库仍跟踪远程任务时清理完成步骤的元数据。"""
        if step not in {"meshing", "solver", "postprocess"}:
            return
        workstation_id = self._workstation_for_config(cn)
        get_remote_task = getattr(self.state, "get_remote_task", None)
        if callable(get_remote_task):
            task = get_remote_task(cn, step, workstation_id=workstation_id)
            if task is None:
                return
        self.runner.get_remote_executor().forget_remote_task(
            cn,
            step,
            workstation_id=workstation_id,
        )

    def _has_started_downstream(self, config_name: int) -> bool:
        """Return true when any step after SC has already left Waiting."""
        return any(
            self.state.get_step_status(config_name, step) != STATUS_WAITING
            for step in ("transfer", "meshing", "solver", "postprocess")
        )

    def _is_step_in_flight(self, cn: int, step: str) -> bool:
        """检查指定构型的指定步骤是否正在被活跃处理（排队或执行中）。

        用于 _resume_paused_steps() 区分"孤儿 Running"与"活跃 Running"，
        避免将正在执行的步骤误重置为 Waiting。
        """
        if step == "sw":
            is_sw_in_flight = getattr(self.runner, "is_sw_in_flight", None)
            return bool(callable(is_sw_in_flight) and is_sw_in_flight(cn))
        if step == "sc":
            return self.worker_pool.is_sc_in_flight(cn)
        elif step == "transfer":
            return self.worker_pool.is_transfer_in_flight(cn)
        elif step == "meshing":
            is_config_in_flight = getattr(self.meshing_monitor, "is_config_in_flight", None)
            if callable(is_config_in_flight):
                return bool(is_config_in_flight(cn))
            return (
                self.meshing_monitor is not None
                and self.meshing_monitor.get_in_flight_config() == cn
            )
        return False  # solver 由屏障统一管理

    def _enqueue_sc(self, cn: int, step_dir: str) -> None:
        """将构型的 SC 步骤推入处理队列（带去重）。

        队列 claim 在排队和执行期间始终有效。resume 扫描和文件监控器
        同时触发入队时，只有一个来源能成功提交。
        """
        sw_filename = get_step_filename("sw", cn)
        if sw_filename:
            step_file = os.path.join(step_dir, sw_filename)
            if os.path.exists(step_file):
                if self._sc_queue.submit((cn, step_file)):
                    logger.info(f"构型{cn} 已推入 SC 处理队列 (队列长度: {self._sc_queue.qsize()})")
                else:
                    logger.debug(f"构型{cn} 已在 SC 队列或执行中，跳过重复入队")

    def resume(self):
        logger.info("收到继续指令")

        # 断点续传扫描和开放 gate 必须处于同一 transition。
        # 若扫描期间收到 pause，pause() 会在 transition 后生效，不会被覆盖。
        with self._control.resume_transition():
            self._resume_paused_steps()
            self.state.set_engine_status("running")

        if self._stopped.is_set():
            logger.info("resume 组件启动前收到 stop 指令，跳过组件启动")
            return

        # 确保各组件线程存活（start_if_needed 内部已是幂等的，不会重复创建）
        self._ensure_file_monitor_running()
        if self._stopped.is_set():
            logger.info("resume 文件监控启动后收到 stop 指令，跳过后续组件启动")
            return
        self.meshing_monitor.start_if_needed()   # 先启动 MeshingMonitor
        if self._stopped.is_set():
            logger.info("resume MeshingMonitor 启动后收到 stop 指令，跳过后续组件启动")
            return
        self.worker_pool.start_if_needed()        # 再启动 Worker Pool
        if self._stopped.is_set():
            logger.info("resume WorkerPool 启动后收到 stop 指令，跳过线程创建")
            return

        # ★ 仅唤醒文件监控器，不重置已处理文件集合。
        #   _resume_paused_steps() 已完成断点续传扫描并入队，
        #   文件监控器只需继续检测新写入的 STEP 文件，无需重新扫描旧文件
        #   （resume_and_reset 会清空 _processed_files 导致重复入队）。
        # 与 pause() 共用控制锁，避免新的 pause() 到达后 resume() 继续唤醒
        # 或分发 Solver。
        with self._control.external_start() as can_finalize:
            if not can_finalize:
                logger.info("resume 组件启动后收到 pause/stop 指令，跳过后续启动")
                if self._paused.is_set():
                    self.state.set_engine_status("paused")
                return

            if self._file_monitor is not None:
                self._file_monitor.resume_only()

            self._finalize_barrier_after_downstream_start()

        if self._stopped.is_set():
            logger.info("resume 屏障处理后收到 stop 指令，跳过 pipeline 线程创建")
            return

        # ★ 若 pipeline 线程已退出（如 SW 失败+暂停后 start_pipeline 返回），
        #   重启 pipeline 使 _resume_paused_steps 中已重置的 Error→Waiting 构型能被
        #   SW 阶段重新处理。
        if self._pipeline_thread is None or not self._pipeline_thread.is_alive():
            t = threading.Thread(target=self.start_pipeline, daemon=True)
            t.start()
            self._pipeline_thread = t
            logger.info("[Resume] pipeline 线程已退出，已重新启动")

        logger.info("流水线已恢复运行")

    def stop(self) -> None:
        """停止流水线。"""
        logger.info("收到停止指令")
        self._control.stop()

        # ★ 将所有 Running/Retrying 步骤转为 Paused，
        #   防止重启后孤立 RUNNING 步骤导致构型卡死。
        #   注意：pause() 不再调用此方法（允许 running 步骤自然完成），
        #   但 stop() 仍需调用，因为停止意味着强制终止所有活动。
        self.state.set_all_running_to_paused()

        # ★ 立即重置引擎状态，确保无论后续清理是否挂起/异常，状态都已正确归零
        self.state.set_engine_status("stopped")

        # 清理本地 CAD 进程（容错：任何清理步骤失败不阻断整体停止流程）
        shutdown_sw_processes = getattr(self.runner, "shutdown_sw_processes", None)
        if callable(shutdown_sw_processes):
            try:
                shutdown_sw_processes()
            except Exception as e:
                logger.debug(f"SW 停止清理异常: {e}")

        try:
            self.runner.shutdown_sc_pool()
        except Exception as e:
            logger.debug(f"SCPool 停止清理异常: {e}")

        try:
            # 停止文件监控，避免 stop 清队列期间 STEP 回调重新入队。
            if self._file_monitor:
                self._file_monitor.stop()

            # 清空尚未执行的 SC 任务；正在执行的 claim 会在 worker 退出前释放。
            self._sc_queue.clear()

            # 等待关键线程退出
            self.worker_pool.join_worker_threads(timeout=3)
            self.meshing_monitor.join_worker_threads(timeout=3)
            if self._barrier_thread and self._barrier_thread.is_alive():
                self._barrier_thread.join(timeout=3)
            self.barrier_coordinator.join_solver_threads(timeout=3)
        except Exception as e:
            logger.warning(f"停止清理过程中出现异常（已忽略）: {e}")

        try:
            # 断开 SSH
            self.runner.disconnect_ssh()
        except Exception as e:
            logger.debug(f"SSH 断开异常（已忽略）: {e}")

        logger.info("流水线已停止")

    def _reset_sw_cleanup_if_needed(self, should_reset: bool) -> None:
        """按需重置 SolidWorks 全量清理状态。"""
        if not should_reset:
            return
        reset_sw_cleanup = getattr(self.runner, "reset_sw_cleanup", None)
        if callable(reset_sw_cleanup):
            reset_sw_cleanup()

    def request_file_monitor_reset(self) -> None:
        """请求 STEP 文件监控器重置追踪状态，不改变调度器暂停标志。"""
        if self._file_monitor is None:
            logger.debug("[Scheduler] STEP 文件监控器不存在，跳过重置请求")
            return

        self._file_monitor.reset_only()
        logger.info("[Scheduler] 已请求 STEP 文件监控器重置追踪状态")

    def reset_config(self, config_name, step_name: str | None = None):
        """
        重置指定构型的指定步骤（及后续步骤）。

        Args:
            config_name: 构型名称 (int) 或 "all" 表示全部构型
            step_name: 步骤名，None 或 "all" 表示重置所有步骤（自 SW 起）
        """
        # 将 "all" 统一转为 None（表示全部步骤）
        if step_name == "all":
            step_name = None

        # 判断是否需要清除全局屏障（重置范围触及 Meshing 即需重新同步）
        need_barrier_clear = (
            step_name is None
            or STEP_INDEX.get(step_name, 99) <= STEP_INDEX.get("meshing", 99)
        )

        # 判断是否需要重置文件监控器（重置范围触及 SC 或更早步骤时需要）
        need_monitor_reset = (
            step_name is None
            or step_name in ("sw", "sc")
            or STEP_INDEX.get(step_name, 99) <= STEP_INDEX.get("sc", 99)
        )
        need_sw_cleanup_reset = (
            step_name is None
            or step_name == "sw"
            or STEP_INDEX.get(step_name, 99) <= STEP_INDEX.get("sw", 99)
        )
        need_pipeline_terminal_reset = (
            step_name is None
            or STEP_INDEX.get(step_name, 99) <= STEP_INDEX.get("postprocess", 99)
        )
        self._mark_reset_generation(config_name, step_name)

        # ★ 暂停感知：reset 操作不应越过暂停标志恢复文件监控。
        #    若当前处于暂停状态，使用 reset_only() 仅清理内部状态；
        #    若未暂停，使用 resume_and_reset() 恢复扫描。
        _monitor_reset_method = (
            self._file_monitor.reset_only if self._paused.is_set()
            else self._file_monitor.resume_and_reset
        ) if self._file_monitor else None

        # 全量重置（所有构型 + 所有步骤）需要额外清除引擎全局状态
        if config_name == "all" and step_name is None:
            self.state.reset_all()
            self._barrier_passed.clear()
            self.barrier_coordinator.clear_all_workstation_barriers()
            if need_pipeline_terminal_reset:
                self.barrier_coordinator.reset_solver_terminal_reported()
            self._reset_sw_cleanup_if_needed(need_sw_cleanup_reset)
            self.runner.reset_sc_pool()
            if need_monitor_reset and _monitor_reset_method:
                _monitor_reset_method()
            self._sc_queue.clear()
        elif config_name == "all":
            for cn in self.state.get_all_configs():
                self.state.reset_config_steps(cn, step_name)
            if need_barrier_clear:
                self._barrier_passed.clear()
                self.barrier_coordinator.clear_all_workstation_barriers()
                self.state.set_global_barrier_met(False)
                self.runner.reset_sc_pool()
            if need_pipeline_terminal_reset:
                self.barrier_coordinator.reset_solver_terminal_reported()
            self._reset_sw_cleanup_if_needed(need_sw_cleanup_reset)
            if need_monitor_reset and _monitor_reset_method:
                _monitor_reset_method()
            self._sc_queue.clear()
        else:
            reset_workstation_id = self._workstation_for_config(int(config_name))
            self.state.reset_config_steps(config_name, step_name)
            if need_barrier_clear:
                self._barrier_passed.clear()
                self.barrier_coordinator.clear_workstation_barrier(reset_workstation_id)
                self.state.set_global_barrier_met(False)
                self.runner.reset_sc_pool()
            if need_pipeline_terminal_reset:
                self.barrier_coordinator.reset_solver_terminal_reported()
            self._reset_sw_cleanup_if_needed(need_sw_cleanup_reset)
            if need_monitor_reset and _monitor_reset_method:
                _monitor_reset_method()
        logger.info(f"已重置 config={config_name} step={step_name or 'all'}")

    def reset_all(self):
        """重置所有构型的所有步骤（委托给 reset_config 处理全量逻辑）。"""
        self.reset_config("all", None)
