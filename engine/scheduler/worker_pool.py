"""
工作线程池管理模块。

负责管理 SC 和 Transfer 的独立工作线程池，通过队列解耦：
  SC Worker:   SC 队列取任务 → 执行 SC → 推入 Transfer 队列
  Transfer Worker: Transfer 队列取任务 → 执行 Transfer → 提交 MeshingMonitor

SC 与 Transfer 解耦后，SC 常驻进程槽位在完成当前构型后可立即接收新任务，
不再被 Transfer 的网络 I/O 阻塞。
"""

import threading
import queue
import os
import time

from engine.config import (
    STATUS_WAITING, STATUS_RUNNING, STATUS_PAUSED, STATUS_COMPLETED, STATUS_ERROR, STATUS_RETRYING,
    LOCAL_PATHS, get_step_filename,
)
from engine.state_manager import StateManager
from engine.task_runner import TaskRunner
from engine.scheduler.retry import RetryManager
from engine.scheduler.utils import wait_unless_paused_or_stopped
from engine.scheduler.work_queue import UniqueWorkQueue
from utils.logger import setup_logger

logger = setup_logger(__name__)


class WorkerPoolManager:
    """
    工作线程池管理器（SC/Transfer 解耦架构）。

    SC 工作线程：从 SC 队列取构型 → 执行 SpaceClaim 转换 → 推入 Transfer 队列。
    Transfer 工作线程：从 Transfer 队列取构型 → 执行文件传输 → 提交 MeshingMonitor。

    SC 常驻槽位完成后可立即接收新构型，不再被 Transfer 网络 I/O 阻塞。
    """

    def __init__(
        self,
        state_manager: StateManager,
        task_runner: TaskRunner,
        sc_queue: UniqueWorkQueue[tuple[int, str]],
        paused_event: threading.Event,
        stopped_event: threading.Event,
        barrier_passed_event: threading.Event,
        retry_manager: RetryManager,
    ):
        """
        初始化工作线程池管理器。

        Args:
            state_manager: 共享状态管理器
            task_runner: 任务执行器
            sc_queue: SC 处理队列
            paused_event: 暂停事件
            stopped_event: 停止事件
            barrier_passed_event: 全局屏障通过事件
            retry_manager: 重试管理器
        """
        self.state = state_manager
        self.runner = task_runner
        self._sc_queue = sc_queue
        self._paused = paused_event
        self._stopped = stopped_event
        self._barrier_passed = barrier_passed_event
        self._retry_manager = retry_manager

        # ---- MeshingMonitor（由 PipelineScheduler 注入） ----
        self._meshing_monitor = None

        # ---- Transfer 解耦队列 ----
        # SC Worker 完成 SC 后将构型名称推入此队列，
        # Transfer Worker 从此队列取任务执行传输。
        self._transfer_queue = UniqueWorkQueue[int]()

        # ---- 工作线程（SC 与 Transfer 分离） ----
        self._sc_worker_threads: list[threading.Thread] = []
        self._transfer_worker_threads: list[threading.Thread] = []

        # ---- 工作线程数 ----
        self._num_sc_workers = 3       # SC 工作线程数（与 MAX_SLOTS 匹配）
        self._num_transfer_workers = 1  # Transfer 工作线程数（SFTP 单线程保证安全）

        # ---- SC 全部完成检测 ----
        # 当 SC 队列为空且所有构型的 SC 步骤均已终结时，触发一次 SC 进程清理。
        # 使用标志位防止多个 SC Worker 线程重复触发。
        self._sc_cleanup_triggered = False
        self._sc_cleanup_lock = threading.Lock()
        self._queue_report_lock = threading.Lock()
        self._last_queue_report = time.time()
        self._queue_report_interval = 30.0
        self._waiting_sc_seen_at: dict[int, float] = {}

        logger.info("工作线程池管理器初始化完成（SC/Transfer 解耦架构）")

    def set_meshing_monitor(self, meshing_monitor) -> None:
        """注入 MeshingMonitor 实例。由 PipelineScheduler 在创建后调用。"""
        self._meshing_monitor = meshing_monitor

    def submit_transfer(self, config_name: int) -> bool:
        """提交 Transfer 任务；排队或执行中的构型不会重复提交。"""
        if not self._transfer_queue.submit(config_name):
            logger.debug(f"构型{config_name} 已在 Transfer 队列或执行中，跳过重复提交")
            return False
        logger.info(
            f"构型{config_name} 已推入 Transfer 队列 "
            f"(队列深度: {self._transfer_queue.qsize()})"
        )
        return True

    def is_transfer_in_flight(self, config_name: int) -> bool:
        """检查指定构型是否在 Transfer 队列中（排队或执行中）。"""
        return self._transfer_queue.has_claim(config_name)

    def is_sc_in_flight(self, config_name: int) -> bool:
        """检查指定构型是否在 SC 队列中（排队或执行中）。"""
        return self._sc_queue.has_claim(config_name)

    def join_worker_threads(self, timeout: float = 3.0) -> None:
        """等待所有 SC 和 Transfer 工作线程退出。

        供外部模块（如 PipelineScheduler.stop()）调用，避免直接访问私有成员。

        Args:
            timeout: 每个线程的最大等待秒数
        """
        all_threads = self._sc_worker_threads + self._transfer_worker_threads
        for t in all_threads:
            if t.is_alive():
                t.join(timeout=timeout)

    # ------------------------------------------------------------------
    # 工作线程池管理
    # ------------------------------------------------------------------

    def start_if_needed(self):
        """仅在 SC 和 Transfer 工作线程均未启动或全部死亡时创建新的线程池。

        每次调用均重置 SC 清理标志，确保新的流水线运行中能正确触发清理。
        """
        # ★ 每次流水线启动/恢复时重置 SC 清理标志
        self._sc_cleanup_triggered = False
        with self._queue_report_lock:
            self._last_queue_report = time.time()

        alive_sc = [t for t in self._sc_worker_threads if t.is_alive()]
        alive_tf = [t for t in self._transfer_worker_threads if t.is_alive()]
        self._sc_worker_threads = alive_sc
        self._transfer_worker_threads = alive_tf
        if not alive_sc and not alive_tf:
            self._start_worker_pool()
        else:
            logger.debug(
                f"工作线程池已存在 "
                f"({len(alive_sc)} SC + {len(alive_tf)} Transfer 活跃线程)，跳过创建"
            )

    def _start_worker_pool(self):
        """启动 SC 和 Transfer 分离的独立工作线程池。

        SC 工作线程：从 SC 队列取任务 → 执行 SpaceClaim 转换 → 推入 Transfer 队列。
        Transfer 工作线程：从 Transfer 队列取任务 → 执行文件传输 → 提交 MeshingMonitor。
        """
        # SC 工作线程
        for i in range(self._num_sc_workers):
            t = threading.Thread(
                target=self._sc_worker_loop,
                name=f"SCWorker-{i+1}",
                daemon=True,
            )
            t.start()
            self._sc_worker_threads.append(t)

        # Transfer 工作线程
        for i in range(self._num_transfer_workers):
            t = threading.Thread(
                target=self._transfer_worker_loop,
                name=f"TransferWorker-{i+1}",
                daemon=True,
            )
            t.start()
            self._transfer_worker_threads.append(t)

        logger.info(
            f"已启动 {self._num_sc_workers} 个 SC 工作线程 + "
            f"{self._num_transfer_workers} 个 Transfer 工作线程"
        )

    # ==================================================================
    # SC 工作线程
    # ==================================================================

    def _check_sc_all_done(self) -> bool:
        """检查是否所有构型的 SC 步骤均已终结（Completed 或 Error）。

        同时验证 SW 步骤已全部终结（确保不会再有新 STEP 文件产生
        → 新 SC 任务入队）。

        注意：不检查 Transfer 队列状态。SC 进程仅负责 STEP→SCDOC 转换，
        SCDOC 文件写入磁盘后 SC 进程即不再被需要；Transfer 是纯文件传输，
        不依赖 SC 进程。

        Returns:
            True 表示 SC 阶段已彻底完成，可安全清理 SC 进程。
        """
        all_configs = self.state.get_all_configs()
        if not all_configs:
            return False

        terminal_states = {STATUS_COMPLETED, STATUS_ERROR}

        for cn in all_configs:
            sw_status = self.state.get_step_status(cn, "sw")
            sc_status = self.state.get_step_status(cn, "sc")
            # SW 必须已终结（确保不会再有新 STEP 文件产生）
            if sw_status not in terminal_states:
                return False
            # SC 必须已终结
            if sc_status not in terminal_states:
                return False

        return True

    def _report_queue_health_if_due(self, now: float) -> None:
        """由任一 SC Worker 触发队列健康检查，但每个周期只输出一次。"""
        with self._queue_report_lock:
            if now - self._last_queue_report < self._queue_report_interval:
                return
            self._last_queue_report = now

        qsize = self._sc_queue.qsize()
        tf_qsize = self._transfer_queue.qsize()
        active_sc = sum(1 for t in self._sc_worker_threads if t.is_alive())
        active_tf = sum(1 for t in self._transfer_worker_threads if t.is_alive())
        logger.debug(
            f"[队列健康] SC深度={qsize}, Transfer深度={tf_qsize}, "
            f"活跃SC={active_sc}, 活跃Transfer={active_tf}, "
            f"Barrier={'已通过' if self._barrier_passed.is_set() else '未通过'}"
        )
        if qsize == 0:
            waiting_configs = [
                cn for cn in self.state.get_all_configs()
                if self.state.get_step_status(cn, "sc") == STATUS_WAITING
                and self.state.get_step_status(cn, "sw") == STATUS_COMPLETED
            ]
            waiting_set = set(waiting_configs)
            for cn in list(self._waiting_sc_seen_at):
                if cn not in waiting_set:
                    self._waiting_sc_seen_at.pop(cn, None)

            if waiting_configs:
                for cn in waiting_configs:
                    first_seen = self._waiting_sc_seen_at.setdefault(cn, now)
                    if now - first_seen >= self._queue_report_interval:
                        logger.warning(f"[队列异常] 构型{cn} SW 已完成但未入队")
                    else:
                        logger.debug(
                            f"[队列健康] 构型{cn} SW 已完成，等待 SC 入队确认"
                        )
        else:
            self._waiting_sc_seen_at.clear()

        # SC 全部完成检测：当 SC 队列为空且所有构型 SC/SW 均已终结时，
        # 触发 SC 进程清理（不再等待 Meshing 全局屏障）。
        if qsize == 0 and not self._sc_cleanup_triggered:
            if self._check_sc_all_done():
                with self._sc_cleanup_lock:
                    if not self._sc_cleanup_triggered:
                        self._sc_cleanup_triggered = True
                        logger.info(
                            "[WorkerPool] 检测到所有构型 SC 步骤已完成，"
                            "触发 SC 进程清理"
                        )
                        try:
                            self.runner.do_sc_final_cleanup()
                        except Exception as e:
                            logger.warning(f"[WorkerPool] SC 进程清理异常: {e}")

    def _sc_worker_loop(self):
        """SC 工作线程主循环。

        从 SC 队列获取构型 → 执行 SC → 推入 Transfer 队列。
        SC 完成后立即释放常驻槽位，不等待 Transfer 完成。
        """
        logger.info(f"[{threading.current_thread().name}] SC 工作线程启动")

        _consecutive_fatal_count = 0

        while not self._stopped.is_set():
            if self._paused.is_set():
                time.sleep(1)
                continue

            try:
                config_name, _step_file = self._sc_queue.get(timeout=1)
            except queue.Empty:
                self._report_queue_health_if_due(time.time())
                continue

            logger.info(f"[{threading.current_thread().name}] 开始处理构型{config_name} SC 步骤")

            try:
                self._process_sc_step(config_name)
                _consecutive_fatal_count = 0
            except (RuntimeError, ValueError, OSError, ConnectionError) as e:
                logger.error(f"SC 步骤处理构型{config_name} 异常: {e}", exc_info=True)
                for step in ["sc", "transfer"]:
                    try:
                        s = self.state.get_step_status(config_name, step)
                        if s not in (STATUS_COMPLETED, STATUS_ERROR, STATUS_PAUSED):
                            self.state.set_step_status(config_name, step, STATUS_ERROR, str(e))
                    except Exception as mark_err:
                        logger.debug(f"标记构型{config_name}步骤{step}为Error时异常: {mark_err}")
                self._mark_meshing_error_if_transfer_failed(config_name, str(e))
            except Exception as e:
                _consecutive_fatal_count += 1
                if _consecutive_fatal_count >= 3:
                    logger.critical(
                        f"SC 步骤处理构型{config_name} 致命异常 "
                        f"(连续第{_consecutive_fatal_count}次): "
                        f"{type(e).__name__}: {e}",
                        exc_info=True
                    )
                else:
                    logger.critical(
                        f"SC 步骤处理构型{config_name} 致命异常: {type(e).__name__}: {e}",
                        exc_info=True
                    )
                for step in ["sc", "transfer"]:
                    try:
                        s = self.state.get_step_status(config_name, step)
                        if s not in (STATUS_COMPLETED, STATUS_ERROR, STATUS_PAUSED):
                            self.state.set_step_status(config_name, step, STATUS_ERROR,
                                                        f"致命异常: {type(e).__name__}: {e}")
                    except Exception:
                        pass
                self._mark_meshing_error_if_transfer_failed(
                    config_name, f"致命异常: {type(e).__name__}: {e}"
                )
            finally:
                self._sc_queue.complete((config_name, _step_file))

        logger.info(f"[{threading.current_thread().name}] SC 工作线程退出")

    def _process_sc_step(self, config_name: int):
        """执行单个构型的 SC 步骤，成功后推入 Transfer 队列。

        SC 失败时标记 Transfer/Meshing 为 Error 且不推入 Transfer 队列。

        Args:
            config_name: 构型名称
        """
        sc_status = self.state.get_step_status(config_name, "sc")

        # ★ 重复入队检测：若 SC 已处于 Running 或 Completed，说明该构型
        #   被重复推入了队列（如 resume + 文件监控器同时触发），直接跳过。
        if sc_status in (STATUS_RUNNING, STATUS_COMPLETED):
            logger.warning(
                f"[WorkerPool] 检测到重复入队：构型{config_name} SC 已处于 {sc_status} 状态，"
                f"跳过本次处理（可能是 resume 与文件监控器同时入队导致）"
            )
            return

        # 排除 RUNNING：防止 _resume_paused_steps() 重复入队导致两个 Worker
        # 同时执行同一构型的 SC 步骤（RetryManager 已将状态设为 RUNNING）
        if sc_status in (STATUS_WAITING, STATUS_PAUSED, STATUS_ERROR, STATUS_RETRYING):
            if not wait_unless_paused_or_stopped(self._paused, self._stopped):
                return

            scdoc_name = get_step_filename("sc", config_name)
            if scdoc_name:
                scdoc_path = os.path.join(LOCAL_PATHS["scdoc_dir"], scdoc_name)
                if os.path.exists(scdoc_path) and os.path.getsize(scdoc_path) > 0:
                    logger.info(
                        f"构型{config_name} SC: SCDOC 文件已存在，跳过执行"
                    )
                    self.state.set_step_status(config_name, "sc", STATUS_COMPLETED)
                elif not self._retry_manager.execute_with_retry(config_name, "sc",
                                                   self.runner.execute_sc_step):
                    self._mark_meshing_error_if_transfer_failed(
                        config_name, "SC 步骤失败"
                    )
                    return
            elif not self._retry_manager.execute_with_retry(config_name, "sc",
                                               self.runner.execute_sc_step):
                self._mark_meshing_error_if_transfer_failed(
                    config_name, "SC 步骤失败"
                )
                return

            # ★ 执行完成后再次确认状态，仅在 SC 确实 Completed 时推入 Transfer
            sc_status = self.state.get_step_status(config_name, "sc")

        # ★ SC 成功（或已 Completed），推入 Transfer 队列
        if sc_status == STATUS_COMPLETED:
            self.submit_transfer(config_name)
        else:
            logger.warning(
                f"[WorkerPool] 构型{config_name} SC 执行后状态为 {sc_status}，"
                f"未推入 Transfer 队列"
            )

    # ==================================================================
    # Transfer 工作线程
    # ==================================================================

    def _transfer_worker_loop(self):
        """Transfer 工作线程主循环。

        从 Transfer 队列获取构型 → 执行 Transfer → 提交 MeshingMonitor。
        独立于 SC 工作线程，不阻塞 SC 常驻槽位的周转。
        """
        logger.info(f"[{threading.current_thread().name}] Transfer 工作线程启动")

        _consecutive_fatal_count = 0

        while not self._stopped.is_set():
            if self._paused.is_set():
                time.sleep(1)
                continue

            try:
                config_name = self._transfer_queue.get(timeout=1)
            except queue.Empty:
                continue

            logger.info(f"[{threading.current_thread().name}] 开始处理构型{config_name} Transfer 步骤")

            try:
                self._process_transfer_step(config_name)
                _consecutive_fatal_count = 0
            except (RuntimeError, ValueError, OSError, ConnectionError) as e:
                logger.error(f"Transfer 步骤处理构型{config_name} 异常: {e}", exc_info=True)
                try:
                    s = self.state.get_step_status(config_name, "transfer")
                    if s not in (STATUS_COMPLETED, STATUS_ERROR, STATUS_PAUSED):
                        self.state.set_step_status(config_name, "transfer", STATUS_ERROR, str(e))
                except Exception as mark_err:
                    logger.debug(f"标记构型{config_name} Transfer 为 Error 时异常: {mark_err}")
                self._mark_meshing_error_if_transfer_failed(config_name, str(e))
            except Exception as e:
                _consecutive_fatal_count += 1
                if _consecutive_fatal_count >= 3:
                    logger.critical(
                        f"Transfer 步骤处理构型{config_name} 致命异常 "
                        f"(连续第{_consecutive_fatal_count}次): "
                        f"{type(e).__name__}: {e}",
                        exc_info=True
                    )
                else:
                    logger.critical(
                        f"Transfer 步骤处理构型{config_name} 致命异常: {type(e).__name__}: {e}",
                        exc_info=True
                    )
                try:
                    s = self.state.get_step_status(config_name, "transfer")
                    if s not in (STATUS_COMPLETED, STATUS_ERROR, STATUS_PAUSED):
                        self.state.set_step_status(config_name, "transfer", STATUS_ERROR,
                                                    f"致命异常: {type(e).__name__}: {e}")
                except Exception:
                    pass
                self._mark_meshing_error_if_transfer_failed(
                    config_name, f"致命异常: {type(e).__name__}: {e}"
                )
            finally:
                self._transfer_queue.complete(config_name)

        logger.info(f"[{threading.current_thread().name}] Transfer 工作线程退出")

    def _process_transfer_step(self, config_name: int):
        """执行单个构型的 Transfer 步骤，成功后提交 MeshingMonitor。

        包含远程文件存在性检查，若远程 SCDOC 已存在则跳过传输。
        Transfer 失败时标记 Meshing 为 Error。

        Args:
            config_name: 构型名称
        """
        if not wait_unless_paused_or_stopped(self._paused, self._stopped):
            return

        transfer_status = self.state.get_step_status(config_name, "transfer")

        # ★ 重复入队检测：若 Transfer 已处于 Running 或 Completed，说明该构型
        #   被重复推入了队列，直接跳过。
        if transfer_status in (STATUS_RUNNING, STATUS_COMPLETED):
            logger.warning(
                f"[WorkerPool] 检测到重复入队：构型{config_name} Transfer 已处于 {transfer_status} 状态，"
                f"跳过本次处理"
            )
            return

        # 排除 RUNNING：防止重复入队导致两个 Worker 同时执行同一构型的 Transfer
        if transfer_status in (STATUS_WAITING, STATUS_PAUSED, STATUS_ERROR, STATUS_RETRYING):
            # ★ 远程文件存在性检查已移入 remote_executor.execute_transfer() 内部，
            #    在 _ssh_lock 保护下执行，避免与 upload_file() 并发操作 SFTP 通道
            #    导致死锁。此处不再单独检查，直接委托 execute_transfer 处理。
            if not self._retry_manager.execute_with_retry(config_name, "transfer",
                                             self.runner.execute_transfer):
                self._mark_meshing_error_if_transfer_failed(
                    config_name, "Transfer 步骤失败"
                )
                return

            # ★ 执行完成后再次确认状态
            transfer_status = self.state.get_step_status(config_name, "transfer")

        # ★ Transfer 成功（或已 Completed），提交 MeshingMonitor
        if transfer_status == STATUS_COMPLETED:
            if self._meshing_monitor is not None:
                self._meshing_monitor.submit(config_name)
                logger.info(f"构型{config_name} Transfer 完成，已提交 MeshingMonitor")
            else:
                logger.warning(
                    f"构型{config_name} Transfer 完成但 MeshingMonitor 未就绪，"
                    f"Meshing 将在下次重启时由断点续传处理"
                )
        else:
            logger.warning(
                f"[WorkerPool] 构型{config_name} Transfer 执行后状态为 {transfer_status}，"
                f"未提交 MeshingMonitor"
            )

    def _mark_meshing_error_if_transfer_failed(self, config_name: int, reason: str) -> None:
        """上游步骤（SC 或 Transfer）失败时，将 Meshing 标记为 Error（若尚未完成）。"""
        meshing_st = self.state.get_step_status(config_name, "meshing")
        if meshing_st not in (STATUS_COMPLETED, STATUS_ERROR):
            self.state.set_step_status(
                config_name, "meshing", STATUS_ERROR,
                f"上游步骤失败: {reason}",
            )
            logger.info(
                f"构型{config_name} Meshing 因上游步骤失败标记为 Error"
            )
