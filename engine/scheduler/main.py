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
import queue
import os
from typing import Optional

from engine.config import (
    STEP_INDEX, STEP_NAMES, ENGINE_CONFIG,
    STATUS_WAITING, STATUS_RUNNING, STATUS_PAUSED, STATUS_COMPLETED, STATUS_ERROR, STATUS_RETRYING,
    LOCAL_PATHS, get_step_filename,
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
from .utils import pause_aware_sleep

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
        self._paused = threading.Event()        # 暂停事件（set = 暂停）
        self._stopped = threading.Event()        # 停止事件
        self._barrier_passed = threading.Event() # 全局屏障通过事件

        # 将控制事件注入 TaskRunner，使长时间阻塞操作（如 SC 的 process.communicate）
        # 能够响应暂停/停止指令
        self.runner.set_control_events(self._paused, self._stopped)

        # ---- 工作队列 ----
        # SC 处理队列：(config_name, step_file_path)
        self._sc_queue: queue.Queue = queue.Queue()

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
        )
        self.barrier_coordinator = BarrierCoordinator(
            state_manager=self.state,
            task_runner=self.runner,
            paused_event=self._paused,
            stopped_event=self._stopped,
            barrier_passed_event=self._barrier_passed,
            retry_manager=self.retry_manager,
        )
        self.sw_phase_handler = SWPhaseHandler(
            state_manager=self.state,
            task_runner=self.runner,
            sc_queue=self._sc_queue,
            paused_event=self._paused,
            stopped_event=self._stopped,
            worker_pool_manager=self.worker_pool,
        )
        self.meshing_monitor = MeshingMonitor(
            state_manager=self.state,
            remote_executor=self.runner._remote_executor,
            paused_event=self._paused,
            stopped_event=self._stopped,
        )

        # 注入 MeshingMonitor 到 WorkerPool
        self.worker_pool.set_meshing_monitor(self.meshing_monitor)

        # ---- 工作线程 ----
        self._barrier_thread: Optional[threading.Thread] = None
        self._solver_threads: list[threading.Thread] = []   # 求解线程（屏障通过后启动）
        # 预创建文件监控器并注入 SW 阶段处理器（避免双重实例）
        self._file_monitor: Optional[StepFileMonitor] = StepFileMonitor(
            step_dir=None,
            on_file_ready=self._on_step_file_ready,
            shared_paused_event=self._paused,
        )
        self.sw_phase_handler.set_file_monitor(self._file_monitor)
        self._pipeline_thread: Optional[threading.Thread] = None  # 主调度线程引用

        # 恢复全局屏障状态（断点续传）
        if self.state.is_global_barrier_met():
            self._barrier_passed.set()

        logger.info("流水线调度器初始化完成")

    # ------------------------------------------------------------------
    # 暂停感知的 sleep 辅助方法
    # ------------------------------------------------------------------

    def _pause_aware_sleep(self, duration: float, check_interval: float = 1.0) -> bool:
        """可响应暂停/停止的 sleep 替代方法。

        委托给共享函数 pause_aware_sleep。
        """
        return pause_aware_sleep(duration, self._paused, self._stopped, check_interval)

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

        self._stopped.clear()
        # 启动（或重启）时清除暂停标志：此方法由全新启动或 resume()→重启路径调用，
        # resume() 已在调用前清除了 _paused，此处为防御性编程
        self._paused.clear()

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
        if self._paused.is_set():
            logger.info("SW 阶段已完成，但暂停标志已置位，暂停后续组件启动，等待 resume 指令")
            self.state.set_engine_status("paused")
            return

        # ---- 步骤 1.5: 预扫描下游输出文件（断点续传） ----
        self.sw_phase_handler.prescan_downstream_outputs()

        # ---- 步骤 2: 启动文件监控 ----
        self._ensure_file_monitor_running()

        # ---- 步骤 3: 启动工作线程池（仅在未启动时创建） ----
        self.worker_pool.start_if_needed()

        # ---- 步骤 3.5: 启动 MeshingMonitor ----
        self.meshing_monitor.start_if_needed()

        # ---- 步骤 4: 启动全局屏障监控 ----
        self._ensure_barrier_monitor_running()

        # 更新引擎状态：仅在未被暂停时设为 running（pause() 已将其设为 paused）
        if not self._paused.is_set():
            self.state.set_engine_status("running")
        else:
            logger.info("流水线组件已就绪，但暂停标志仍置位，等待继续指令...")

        logger.info("流水线调度器已启动，等待 STEP 文件...")

    def _handle_recursion_limit_exceeded(self, recursion_depth: int):
        """处理递归深度超限的情况。"""
        # 1) 停止所有并行的调度线程（worker / barrier / monitor）
        self._stopped.set()

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
            sw_st = self.state.get_step_status(cn, "SW")
            if sw_st not in terminal_states:
                self.state.set_step_status(cn, "SW", STATUS_ERROR, error_detail)
                error_count += 1
            # 下游步骤若处于 Waiting，也标记为 Error（阻断链条）
            for s in ["SC", "Transfer", "Meshing", "Solver"]:
                if self.state.get_step_status(cn, s) == STATUS_WAITING:
                    self.state.set_step_status(
                        cn, s, STATUS_ERROR,
                        f"上游 SW 步骤失败（递归深度超限），{s} 无法执行"
                    )

        logger.error(
            f"已标记 {error_count}/{len(all_configs)} 个构型的 SW 步骤为 Error，"
            f"{sum(1 for cn in all_configs if self.state.get_step_status(cn, 'SC') == STATUS_ERROR)} 个构型的下游步骤亦已阻断"
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

    def _ensure_barrier_monitor_running(self):
        """确保全局屏障监控线程正在运行。"""
        if self._barrier_thread is None or not self._barrier_thread.is_alive():
            self._barrier_thread = threading.Thread(
                target=self.barrier_coordinator.monitor_loop,
                daemon=True,
                name="BarrierMonitor"
            )
            self._barrier_thread.start()

    # ------------------------------------------------------------------
    # 文件就绪回调（Producer 端）
    # ------------------------------------------------------------------

    def _on_step_file_ready(self, config_name: int, filepath: str):
        if self._paused.is_set():
            logger.info(f"构型{config_name} STEP 文件就绪，但系统已暂停，跳过入队")
            return

        current_sw = self.state.get_step_status(config_name, "SW")
        if current_sw != STATUS_COMPLETED:
            self.state.set_step_status(config_name, "SW", STATUS_COMPLETED)

        # 断点续传防护：若 SC/Transfer/Meshing 已全部完成，跳过推入队列
        downstream_completed = all(
            self.state.get_step_status(config_name, s) == STATUS_COMPLETED
            for s in ["SC", "Transfer", "Meshing"]
        )
        if downstream_completed:
            logger.info(f"构型{config_name} 下游步骤已完成，跳过入队")
            return

        # 推入 SC 处理队列
        self._sc_queue.put((config_name, filepath))
        logger.info(f"构型{config_name} 已推入 SC 处理队列 (队列长度: {self._sc_queue.qsize()})")

    # ------------------------------------------------------------------
    # 控制接口
    # ------------------------------------------------------------------

    def pause(self):
        logger.info("收到暂停指令")
        self._paused.set()
        self.state.set_all_running_to_paused()
        self.state.set_engine_status("paused")
        if self._file_monitor is not None:
            self._file_monitor.pause()
        logger.info("流水线已暂停，所有运行中/重试中步骤已标记为 Paused")

    @property
    def is_paused(self) -> bool:
        """公共只读属性：是否处于暂停状态（供外部模块查询）。"""
        return self._paused.is_set()

    @property
    def pipeline_alive(self) -> bool:
        """公共只读属性：主调度线程是否存活（供外部模块查询）。"""
        return self._pipeline_thread is not None and self._pipeline_thread.is_alive()

    def _resume_paused_steps(self):
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
        completed_count = 0

        for cn in self.state.get_all_configs():
            for step in STEP_NAMES:
                status = self.state.get_step_status(cn, step)

                if status == STATUS_COMPLETED:
                    continue

                # ---- 找到第一个非 COMPLETED 步骤 ----

                if status == STATUS_RUNNING:
                    break

                if status == STATUS_PAUSED:
                    if self._check_step_output_exists(cn, step, step_dir, scdoc_dir):
                        self.state.set_step_status(cn, step, STATUS_COMPLETED)
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
                        if step != "SW":
                            normal_enqueue.append((cn, step))
                    break

                break  # 未知状态，跳过

            else:
                completed_count += 1

        # ---- 统一入队（PAUSED 优先）----
        sc_enqueued = 0
        meshing_submitted = 0
        for cn, step in priority_enqueue:
            if step == "SC":
                self._enqueue_sc(cn, step_dir)
                sc_enqueued += 1
            elif step == "Transfer":
                # Transfer 需要 SC 先完成 → 入 SC 队列让 Worker 处理
                # Worker._process_single_config 会检测 SC 已 Completed 并跳过
                self._enqueue_sc(cn, step_dir)
                sc_enqueued += 1
            elif step == "Meshing":
                # MeshingMonitor 管理 Meshing 生命周期
                if self.meshing_monitor is not None:
                    self.state.set_step_status(cn, "Meshing", STATUS_WAITING)
                    self.meshing_monitor.submit(cn)
                    meshing_submitted += 1
            elif step == "Solver":
                # Solver 由屏障调度器统一管理，保持当前状态等屏障通过后处理
                pass

        for cn, step in normal_enqueue:
            if step == "SC":
                self._enqueue_sc(cn, step_dir)
                sc_enqueued += 1
            elif step == "Transfer":
                self._enqueue_sc(cn, step_dir)
                sc_enqueued += 1
            elif step == "Meshing":
                if self.meshing_monitor is not None:
                    self.state.set_step_status(cn, "Meshing", STATUS_WAITING)
                    self.meshing_monitor.submit(cn)
                    meshing_submitted += 1
            elif step == "Solver":
                pass

        # ---- 仅输出汇总日志 ----
        total_need = len(priority_enqueue) + len(normal_enqueue)
        if total_need > 0 or completed_count > 0:
            logger.info(
                f"[Resume] 断点续传扫描完成: {completed_count} 个构型全部完成, "
                f"{len(priority_enqueue)} 个 PAUSED 优先恢复, "
                f"{len(normal_enqueue)} 个普通恢复, "
                f"{sc_enqueued} 个 SC 入队, "
                f"{meshing_submitted} 个 Meshing 提交"
            )

    def _check_step_output_exists(
        self, cn: int, step: str, step_dir: str, scdoc_dir: str
    ) -> bool:
        """检查某步骤的输出文件是否已存在于磁盘。"""
        if step == "SW":
            filename = get_step_filename("SW", cn)
            return bool(filename and os.path.exists(os.path.join(step_dir, filename)))
        if step == "SC":
            filename = get_step_filename("SC", cn)
            return bool(filename and os.path.exists(os.path.join(scdoc_dir, filename)))
        # Transfer/Meshing/Solver 的输出在远程，不做本地检查
        return False

    def _enqueue_sc(self, cn: int, step_dir: str) -> None:
        """将构型的 SC 步骤推入处理队列。"""
        sw_filename = get_step_filename("SW", cn)
        if sw_filename:
            step_file = os.path.join(step_dir, sw_filename)
            if os.path.exists(step_file):
                self._sc_queue.put((cn, step_file))

    def resume(self):
        logger.info("收到继续指令")

        # ★ 断点续传扫描：对每个构型从 SW 起逐步检查，找到断点并入队
        #    已覆盖所有步骤的 PAUSED/WAITING/ERROR 状态，无需再调用
        #    set_all_paused_to_running()
        self._resume_paused_steps()

        self._paused.clear()
        self.state.set_engine_status("running")

        pipeline_needs_init = False
        if self._file_monitor is None or not self._file_monitor.is_running:
            pipeline_needs_init = True
        else:
            alive_workers = [t for t in self.worker_pool._worker_threads if t.is_alive()]
            if not alive_workers:
                pipeline_needs_init = True

        if pipeline_needs_init:
            logger.info("检测到流水线组件未就绪，启动初始化...")
            if self.state.is_sw_macro_started():
                self._init_downstream_components()
            else:
                logger.info("SW 宏尚未完成，重新启动流水线...")
                t = threading.Thread(
                    target=self.start_pipeline,
                    daemon=True,
                    name="SchedulerMain-Resume"
                )
                t.start()
                self._pipeline_thread = t
                return

        if self._file_monitor is not None:
            self._file_monitor.resume_and_reset()
        logger.info("流水线已恢复运行")

    def _init_downstream_components(self):
        """初始化 SW 之后的下游流水线组件（预扫描、文件监控、工作线程、屏障监控）。

        在 resume() 恢复暂停时调用，处理 SW 已完成但 workers 尚未启动的场景。
        所有组件启动前均检查 _stopped 标志，避免在引擎停止时创建新线程。
        """
        if self._stopped.is_set():
            logger.info("引擎已停止，跳过下游组件初始化")
            return

        # 预扫描下游输出文件（断点续传）
        self.sw_phase_handler.prescan_downstream_outputs()

        if self._stopped.is_set():
            return

        self._ensure_file_monitor_running()

        if self._stopped.is_set():
            return

        self.worker_pool.start_if_needed()

        if self._stopped.is_set():
            return

        self.meshing_monitor.start_if_needed()

        if self._stopped.is_set():
            return

        self._ensure_barrier_monitor_running()

    def stop(self):
        """停止流水线。"""
        logger.info("收到停止指令")
        self._stopped.set()
        self._paused.clear()  # 解除暂停以便线程退出

        # ★ 立即重置引擎状态，确保无论后续清理是否挂起/异常，状态都已正确归零
        self.state.set_engine_status("stopped")

        # 清理所有 SC 进程（容错：任何清理步骤失败不阻断整体停止流程）
        try:
            self.runner._sc_pool.shutdown_all()
        except Exception as e:
            logger.debug(f"SCPool 停止清理异常: {e}")

        try:
            # 等待关键线程退出
            for t in self.worker_pool._worker_threads:
                if t.is_alive():
                    t.join(timeout=3)
            if self._barrier_thread and self._barrier_thread.is_alive():
                self._barrier_thread.join(timeout=3)
            for t in self.barrier_coordinator._solver_threads:
                if t.is_alive():
                    t.join(timeout=3)
            self.barrier_coordinator._solver_threads.clear()

            # 停止文件监控
            if self._file_monitor:
                self._file_monitor.stop()
        except Exception as e:
            logger.warning(f"停止清理过程中出现异常（已忽略）: {e}")

        try:
            # 断开 SSH
            self.runner.disconnect_ssh()
        except Exception as e:
            logger.debug(f"SSH 断开异常（已忽略）: {e}")

        logger.info("流水线已停止")

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
            or STEP_INDEX.get(step_name, 99) <= STEP_INDEX.get("Meshing", 99)
        )

        # 全量重置（所有构型 + 所有步骤）需要额外清除引擎全局状态
        if config_name == "all" and step_name is None:
            self.state.reset_all()
            self._barrier_passed.clear()
            self.runner._sc_pool.reset()
        elif config_name == "all":
            for cn in self.state.get_all_configs():
                self.state.reset_config_steps(cn, step_name)
            if need_barrier_clear:
                self._barrier_passed.clear()
                self.state.set_global_barrier_met(False)
                self.runner._sc_pool.reset()
        else:
            self.state.reset_config_steps(config_name, step_name)
            if need_barrier_clear:
                self._barrier_passed.clear()
                self.state.set_global_barrier_met(False)
                self.runner._sc_pool.reset()

        logger.info(f"已重置 config={config_name} step={step_name or 'all'}")

    def reset_all(self):
        """重置所有构型的所有步骤（委托给 reset_config 处理全量逻辑）。"""
        self.reset_config("all", None)
