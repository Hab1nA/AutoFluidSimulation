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
import time
from typing import Dict, Optional

from engine.config import (
    STEP_NAMES, STEP_INDEX,
    STATUS_WAITING, STATUS_RUNNING, STATUS_COMPLETED, STATUS_ERROR, STATUS_RETRYING,
    ENGINE_CONFIG,
)
from engine.state_manager import StateManager
from engine.file_monitor import StepFileMonitor
from engine.task_runner import TaskRunner
from utils.logger import setup_logger

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

        # ---- 工作队列 ----
        # SC 处理队列：(config_name, step_file_path)
        self._sc_queue: queue.Queue = queue.Queue()

        # ---- 工作线程 ----
        self._worker_threads: list[threading.Thread] = []
        self._barrier_thread: Optional[threading.Thread] = None
        self._solver_thread: Optional[threading.Thread] = None
        self._file_monitor: Optional[StepFileMonitor] = None

        # ---- 工作线程数 ----
        self._num_workers = 3  # SC/Transfer/Meshing 并发工作线程数

        # 恢复全局屏障状态（断点续传）
        if self.state.is_global_barrier_met():
            self._barrier_passed.set()

        logger.info("流水线调度器初始化完成")

    # ------------------------------------------------------------------
    # 主调度入口
    # ------------------------------------------------------------------

    def start_pipeline(self):
        """
        启动（或继续）流水线。

        执行流程：
        1. 检查 SW 宏是否已执行 → 未执行则启动 SW 宏
        2. 启动文件监控器
        3. 启动 SC/Transfer/Meshing 工作线程
        4. 启动全局屏障监控线程
        """
        logger.info("=" * 60)
        logger.info("流水线调度器启动")
        logger.info("=" * 60)

        self._stopped.clear()
        self._paused.clear()

        # ---- 步骤 1: SW 阶段 ----
        if not self.state.is_sw_macro_started():
            logger.info("SW 宏尚未启动，准备执行...")
            all_configs = self.state.get_all_configs()

            # 将所有构型的 SW 状态设为 Running
            for cn in all_configs:
                current_status = self.state.get_step_status(cn, "SW")
                if current_status == STATUS_WAITING:
                    self.state.set_step_status(cn, "SW", STATUS_RUNNING)

            # 启动 SW 宏（批量导出所有构型）
            success = self.runner.execute_sw_macro()
            if not success:
                for cn in all_configs:
                    self.state.set_step_status(cn, "SW", STATUS_ERROR, "SW 宏启动失败")
                logger.error("SW 宏启动失败，流水线中止")
                return
        else:
            logger.info("SW 宏已执行过，跳过（断点续传模式）")

        # ---- 步骤 2: 启动文件监控 ----
        self._file_monitor = StepFileMonitor(
            step_dir=None,
            on_file_ready=self._on_step_file_ready
        )
        self._file_monitor.start()

        # ---- 步骤 3: 启动工作线程池 ----
        self._start_worker_pool()

        # ---- 步骤 4: 启动全局屏障监控 ----
        self._barrier_thread = threading.Thread(
            target=self._barrier_monitor_loop,
            daemon=True,
            name="BarrierMonitor"
        )
        self._barrier_thread.start()

        # 更新引擎状态
        self.state.set_engine_status("running")

        logger.info("流水线调度器已启动，等待 STEP 文件...")

    # ------------------------------------------------------------------
    # 文件就绪回调（Producer 端）
    # ------------------------------------------------------------------

    def _on_step_file_ready(self, config_name: int, filepath: str):
        """
        当 STEP 文件监控到某个构型的文件写入完成时调用。

        此方法在文件监控线程中执行，仅做轻量操作：
        将构型推入 SC 处理队列。

        Args:
            config_name: 构型名称
            filepath: STEP 文件完整路径
        """
        # 检查该构型的 SW 状态是否为 Completed（避免 SW 还在运行就标记完成）
        # 实际上文件已稳定存在，可以标记 SW 完成
        current_sw = self.state.get_step_status(config_name, "SW")
        if current_sw != STATUS_COMPLETED:
            self.state.set_step_status(config_name, "SW", STATUS_COMPLETED)

        # 推入 SC 处理队列
        self._sc_queue.put((config_name, filepath))
        logger.info(f"构型{config_name} 已推入 SC 处理队列 (队列长度: {self._sc_queue.qsize()})")

    # ------------------------------------------------------------------
    # 工作线程池（Consumer 端）
    # ------------------------------------------------------------------

    def _start_worker_pool(self):
        """启动多个工作线程，从队列取任务执行 SC → Transfer → Meshing。"""
        for i in range(self._num_workers):
            t = threading.Thread(
                target=self._worker_loop,
                name=f"PipelineWorker-{i+1}",
                daemon=True,
            )
            t.start()
            self._worker_threads.append(t)
        logger.info(f"已启动 {self._num_workers} 个工作线程")

    def _worker_loop(self):
        """
        工作线程主循环。

        从 SC 队列获取构型，依次执行：
        SC → Transfer → Meshing

        每个步骤完成后立即更新状态，支持断点续传。
        """
        logger.info(f"[{threading.current_thread().name}] 工作线程启动")

        while not self._stopped.is_set():
            # 检查暂停
            if self._paused.is_set():
                time.sleep(1)
                continue

            try:
                # 从队列获取任务（1秒超时以便检查停止/暂停标志）
                config_name, step_file = self._sc_queue.get(timeout=1)
            except queue.Empty:
                continue

            logger.info(f"[{threading.current_thread().name}] 开始处理构型{config_name}")

            try:
                self._process_single_config(config_name)
            except (RuntimeError, ValueError, OSError, IOError) as e:
                logger.error(f"处理构型{config_name} 时发生未预期异常: {e}", exc_info=True)
                # 尝试标记当前未完成的步骤为 Error
                for step in ["SC", "Transfer", "Meshing"]:
                    s = self.state.get_step_status(config_name, step)
                    if s not in (STATUS_COMPLETED, STATUS_ERROR):
                        self.state.set_step_status(config_name, step, STATUS_ERROR, str(e))

            self._sc_queue.task_done()

        logger.info(f"[{threading.current_thread().name}] 工作线程退出")

    def _process_single_config(self, config_name: int):
        """
        处理单个构型的完整流水线（SC → Transfer → Meshing）。

        实现了断点续传：如果某步骤已经是 Completed，则跳过。

        Args:
            config_name: 构型名称
        """
        # ---- SC 阶段 ----
        sc_status = self.state.get_step_status(config_name, "SC")
        if sc_status in (STATUS_WAITING, STATUS_ERROR, STATUS_RETRYING):
            if not self._execute_with_retry(config_name, "SC",
                                             self.runner.execute_spaceclaim):
                return  # 失败则中止此构型的后续处理

        # ---- Transfer 阶段 ----
        transfer_status = self.state.get_step_status(config_name, "Transfer")
        if transfer_status in (STATUS_WAITING, STATUS_ERROR, STATUS_RETRYING):
            if not self._execute_with_retry(config_name, "Transfer",
                                             self.runner.execute_transfer):
                return

        # ---- Meshing 阶段 ----
        meshing_status = self.state.get_step_status(config_name, "Meshing")
        if meshing_status in (STATUS_WAITING, STATUS_ERROR, STATUS_RETRYING):
            if not self._execute_with_retry(config_name, "Meshing",
                                             self.runner.execute_meshing):
                return

            # 启动远程网格划分后，轮询等待完成
            logger.info(f"等待构型{config_name} 网格划分完成...")
            if self.runner.wait_meshing_completion(config_name):
                self.state.set_step_status(config_name, "Meshing", STATUS_COMPLETED)
                logger.info(f"构型{config_name} 网格划分完成 ✓")
            else:
                self.state.set_step_status(config_name, "Meshing", STATUS_ERROR, "网格划分超时")
                return

        logger.info(f"构型{config_name} SC→Transfer→Meshing 全部完成 ✓")

    def _execute_with_retry(self, config_name: int, step_name: str,
                            execute_func) -> bool:
        """
        带重试机制的任务执行包装器。

        Args:
            config_name: 构型名称
            step_name: 步骤名
            execute_func: 执行函数，签名为 func(config_name) -> bool

        Returns:
            True 表示执行成功
        """
        max_retries = ENGINE_CONFIG["max_retries"]

        for attempt in range(1, max_retries + 1):
            # 检查是否被停止
            if self._stopped.is_set():
                return False

            # 检查暂停
            while self._paused.is_set() and not self._stopped.is_set():
                time.sleep(1)
            if self._stopped.is_set():
                return False

            # 设置状态
            status = STATUS_RETRYING if attempt > 1 else STATUS_RUNNING
            self.state.set_step_status(config_name, step_name, status)

            logger.info(f"执行 [{step_name}] 构型{config_name} (尝试 {attempt}/{max_retries})")

            try:
                success = execute_func(config_name)
                if success:
                    # 对于 Meshing 和 Solver，状态由调用者设置（因为需要等待远程完成）
                    if step_name not in ("Meshing", "Solver"):
                        self.state.set_step_status(config_name, step_name, STATUS_COMPLETED)
                    return True
                else:
                    logger.warning(f"[{step_name}] 构型{config_name} 执行失败 (尝试 {attempt}/{max_retries})")
                    if attempt < max_retries:
                        retry_count = self.state.increment_retry(config_name, step_name)
                        logger.info(f"将在稍后重试 (已重试 {retry_count} 次)")
                        time.sleep(5 * attempt)  # 递增等待时间
            except (RuntimeError, ValueError, OSError, IOError) as e:
                logger.error(f"[{step_name}] 构型{config_name} 异常: {e}")
                if attempt < max_retries:
                    time.sleep(5 * attempt)

        # 所有重试均失败
        self.state.set_step_status(config_name, step_name, STATUS_ERROR,
                                    f"重试 {max_retries} 次后仍然失败")
        return False

    # ------------------------------------------------------------------
    # 全局屏障监控
    # ------------------------------------------------------------------

    def _barrier_monitor_loop(self):
        """
        全局屏障监控线程。

        持续检查所有构型的 Meshing 是否全部 Completed。
        一旦满足条件，设置屏障通过标志并启动 Solver 调度。
        """
        logger.info("[BarrierMonitor] 全局屏障监控启动")
        logger.info("[BarrierMonitor] 等待所有构型的网格划分完成...")

        while not self._stopped.is_set() and not self._barrier_passed.is_set():
            if self._paused.is_set():
                time.sleep(1)
                continue

            # 检查是否所有构型的 Meshing 都已完成
            if self.state.all_configs_completed_at_step("Meshing"):
                logger.info("=" * 60)
                logger.info(">>> 全局屏障通过！所有构型网格划分已完成 <<<")
                logger.info("=" * 60)
                self._barrier_passed.set()
                self.state.set_global_barrier_met(True)

                # 启动 Solver 调度
                self._dispatch_solver_tasks()
                break

            # 也检查是否有 Meshing 失败的（仅当状态有变化时记录）
            error_configs = self.state.get_error_configs()
            meshing_errors = [(c, s, m) for c, s, m in error_configs if s == "Meshing"]
            if meshing_errors:
                logger.warning(
                    f"[BarrierMonitor] 有 {len(meshing_errors)} 个构型的网格划分失败，"
                    f"全局屏障将无法通过。请使用 reset 命令重试失败的构型。"
                )
                # 不自动停止，等待用户干预

            # 轮询间隔：网格划分通常耗时较长，不需要高频检查
            time.sleep(5.0)

        logger.info("[BarrierMonitor] 全局屏障监控退出")

    # ------------------------------------------------------------------
    # Solver 调度（屏障通过后）
    # ------------------------------------------------------------------

    def _dispatch_solver_tasks(self):
        """
        全局屏障通过后，统一启动所有构型的仿真求解。

        所有构型的 Solver 在屏障通过后并行启动。
        """
        logger.info("=" * 60)
        logger.info("开始统一调度仿真求解任务...")
        logger.info("=" * 60)

        all_configs = self.state.get_all_configs()

        # 使用线程池并行启动所有 Solver 任务（不阻塞，求解耗时可能数小时）
        dispatched_count = 0
        for cn in all_configs:
            solver_status = self.state.get_step_status(cn, "Solver")
            if solver_status in (STATUS_WAITING, STATUS_ERROR, STATUS_RETRYING):
                t = threading.Thread(
                    target=self._execute_solver_for_config,
                    args=(cn,),
                    name=f"Solver-{cn}",
                    daemon=True,
                )
                t.start()
                dispatched_count += 1

        logger.info(f"所有 Solver 任务已分发 ({dispatched_count} 个构型)")

    def _execute_solver_for_config(self, config_name: int):
        """
        执行单个构型的仿真求解（在独立线程中运行）。

        Args:
            config_name: 构型名称
        """
        logger.info(f"[Solver] 构型{config_name} 开始求解...")

        # 启动远程求解后台任务
        if self._execute_with_retry(config_name, "Solver",
                                     self.runner.execute_solver):
            # 轮询等待求解完成
            if self.runner.wait_solver_completion(config_name):
                self.state.set_step_status(config_name, "Solver", STATUS_COMPLETED)
                logger.info(f"[Solver] 构型{config_name} 求解完成 ✓")
            else:
                self.state.set_step_status(config_name, "Solver", STATUS_ERROR, "求解超时")

    # ------------------------------------------------------------------
    # 控制接口
    # ------------------------------------------------------------------

    def pause(self):
        """暂停流水线（当前运行步骤完成后不再取新任务）。"""
        logger.info("收到暂停指令")
        self._paused.set()
        self.state.set_engine_status("paused")

    def resume(self):
        """继续流水线。"""
        logger.info("收到继续指令")
        self._paused.clear()
        self.state.set_engine_status("running")

    def stop(self):
        """停止流水线。"""
        logger.info("收到停止指令")
        self._stopped.set()
        self._paused.clear()  # 解除暂停以便线程退出

        # 等待关键线程退出
        for t in self._worker_threads:
            if t.is_alive():
                t.join(timeout=3)
        if self._barrier_thread and self._barrier_thread.is_alive():
            self._barrier_thread.join(timeout=3)

        # 停止文件监控
        if self._file_monitor:
            self._file_monitor.stop()

        # 断开 SSH
        self.runner.disconnect_ssh()

        self.state.set_engine_status("stopped")
        logger.info("流水线已停止")

    def reset_config(self, config_name: int, step_name: str = None):
        """
        重置指定构型的指定步骤（及后续步骤）。

        Args:
            config_name: 构型名称
            step_name: 步骤名，若为 None 则重置所有步骤（自 SW 起）
        """
        self.state.reset_config_steps(config_name, step_name)
        # 如果重置范围包含 Meshing，需要重新检查全局屏障
        if step_name is None or STEP_INDEX.get(step_name, 99) <= STEP_INDEX.get("Meshing", 99):
            self._barrier_passed.clear()
            self.state.set_global_barrier_met(False)
        logger.info(f"已重置构型{config_name} 从 {step_name or 'SW'} 开始")

    def reset_all(self):
        """重置所有构型的所有步骤。"""
        self.state.reset_all()
        self._barrier_passed.clear()
        logger.warning("已重置所有构型的所有步骤")
