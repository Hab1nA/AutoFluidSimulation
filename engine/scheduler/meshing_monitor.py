"""
网格划分监控模块 (Meshing Monitor)

负责串行执行网格划分任务：从队列中取出构型，启动远程 Meshing，
等待完成后标记 Completed，然后处理下一个。

设计约束：
- 同一时刻只能有 1 个构型在执行 Meshing（串行处理）
- 启动远程任务和轮询标志文件均使用短暂 SSH 锁（不在整个等待期间持锁）
- 支持暂停/停止事件响应
- Daemon 重启时扫描 DB 补充队列（断点续传）
"""

import queue
import threading
import time
from typing import Optional

from engine.config import (
    STATUS_WAITING, STATUS_RUNNING, STATUS_PAUSED, STATUS_COMPLETED,
    STATUS_ERROR, STATUS_RETRYING, ENGINE_CONFIG, REMOTE_CONFIG,
)
from engine.state_manager import StateManager
from executor.remote_executor import RemoteExecutor
from utils.logger import setup_logger
from engine.scheduler.utils import (
    pause_aware_sleep, wait_unless_paused_or_stopped,
    check_step_output_exists,
)
from engine.scheduler.work_queue import UniqueWorkQueue

logger = setup_logger(__name__)


class MeshingMonitor:
    """串行 Meshing 执行器 — 同时只运行 1 个 Meshing 任务。

    从内部队列中逐个取出构型，启动远程 Meshing 并等待完成。
    Worker 线程在完成 SC + Transfer 后将构型提交到本模块的队列。
    """

    def __init__(
        self,
        state_manager: StateManager,
        remote_executor: RemoteExecutor,
        paused_event: threading.Event,
        stopped_event: threading.Event,
    ):
        self.state = state_manager
        self._remote_executor = remote_executor
        self._paused = paused_event
        self._stopped = stopped_event

        self._meshing_queue = UniqueWorkQueue[int]()
        self._monitor_thread: Optional[threading.Thread] = None
        self._in_flight_config: Optional[int] = None  # 当前正在执行 Meshing 的构型

        logger.info("[MeshingMonitor] 初始化完成")

    # ------------------------------------------------------------------
    # 队列操作
    # ------------------------------------------------------------------

    def submit(self, config_name: int) -> bool:
        """将构型提交到 Meshing 队列。由 Worker 线程调用。"""
        if not self._meshing_queue.submit(config_name):
            logger.debug(f"[MeshingMonitor] 构型{config_name} 已存在，跳过重复提交")
            return False
        logger.info(
            f"[MeshingMonitor] 构型{config_name} 已入队 "
            f"(队列深度: {self._meshing_queue.qsize()})"
        )
        return True

    def qsize(self) -> int:
        """返回当前队列深度。"""
        return self._meshing_queue.qsize()

    # ------------------------------------------------------------------
    # 线程生命周期
    # ------------------------------------------------------------------

    def start_if_needed(self) -> None:
        """仅在监控线程未启动或已死亡时创建新线程。"""
        if self._monitor_thread is not None and self._monitor_thread.is_alive():
            return
        self._monitor_thread = threading.Thread(
            target=self._monitor_loop,
            name="MeshingMonitor",
            daemon=True,
        )
        self._monitor_thread.start()
        logger.info("[MeshingMonitor] 监控线程已启动")

    def _monitor_loop(self) -> None:
        """监控线程主循环。串行处理 Meshing 队列。"""
        logger.info("[MeshingMonitor] 监控循环开始")

        # ---- 断点续传：扫描 DB 补充队列 ----
        try:
            self._scan_db_for_pending()
        except Exception as e:
            logger.error(
                f"[MeshingMonitor] 断点续传扫描异常（已跳过）: {e}",
                exc_info=True,
            )

        while not self._stopped.is_set():
            # 检查暂停
            if self._paused.is_set():
                time.sleep(1)
                continue

            try:
                config_name = self._meshing_queue.get(timeout=2)
            except queue.Empty:
                continue

            logger.info(
                f"[MeshingMonitor] 从队列取出构型{config_name} "
                f"(剩余队列深度: {self._meshing_queue.qsize()})"
            )

            self._in_flight_config = config_name
            should_requeue = False
            try:
                should_requeue = self._process_single_meshing(config_name)
            except (RuntimeError, ValueError, OSError, ConnectionError) as e:
                logger.error(
                    f"[MeshingMonitor] 处理构型{config_name} 异常: {e}",
                    exc_info=True,
                )
                self.state.set_step_status(config_name, "meshing", STATUS_ERROR, str(e))
            except Exception as e:
                logger.critical(
                    f"[MeshingMonitor] 处理构型{config_name} 致命异常: "
                    f"{type(e).__name__}: {e}",
                    exc_info=True,
                )
                self.state.set_step_status(
                    config_name, "meshing", STATUS_ERROR,
                    f"致命异常: {type(e).__name__}: {e}",
                )
            finally:
                self._in_flight_config = None
                if should_requeue:
                    self._meshing_queue.requeue(config_name)
                else:
                    self._meshing_queue.complete(config_name)

        logger.info("[MeshingMonitor] 监控循环退出")

    # ------------------------------------------------------------------
    # 单构型处理
    # ------------------------------------------------------------------

    def _process_single_meshing(self, config_name: int) -> bool:
        """处理单个构型的 Meshing 阶段。"""
        # ---- 暂停/停止检查 ----
        if not wait_unless_paused_or_stopped(self._paused, self._stopped):
            return False

        # ---- 断点续传：检查远程输出是否已存在（复用共享工具函数） ----
        if self._check_remote_outputs_exist(config_name):
            self.state.set_step_status(config_name, "meshing", STATUS_COMPLETED)
            logger.info(f"[MeshingMonitor] 构型{config_name} Meshing: 远程输出已存在，标记完成")
            return False

        # ---- 检查是否为重启后仍在运行的 Meshing ----
        meshing_status = self.state.get_step_status(config_name, "meshing")
        if meshing_status == STATUS_RUNNING:
            # Daemon 重启后发现 Meshing=Running，无法确定远程是否仍在运行
            # 先检查标志文件，若不存在则重置为 Waiting 后重新启动
            if self._remote_executor.check_meshing_done(config_name):
                self.state.set_step_status(config_name, "meshing", STATUS_COMPLETED)
                logger.info(f"[MeshingMonitor] 构型{config_name} Meshing: 重启后检测到完成标志")
                return False
            # ★ 重置为 Waiting：清除孤儿 Running 状态，避免原子防护误判
            self.state.set_step_status(config_name, "meshing", STATUS_WAITING)
            logger.warning(
                f"[MeshingMonitor] 构型{config_name} Meshing 重启后状态为 Running，"
                f"无法确定远程状态，重置为 Waiting 后重新启动"
            )

        # ---- 启动远程 Meshing（带重试） ----
        max_retries = int(ENGINE_CONFIG["max_retries"])
        for attempt in range(1, max_retries + 1):
            if self._stopped.is_set():
                return False
            if not wait_unless_paused_or_stopped(self._paused, self._stopped):
                return False

            # ★ 原子防护：确保同一时刻只有一个构型处于 Meshing Running
            if not self.state.set_meshing_running_if_idle(config_name):
                logger.warning(
                    f"[MeshingMonitor] 构型{config_name} 被跳过："
                    f"另一个构型正在执行网格划分，重新入队"
                )
                return True

            logger.info(
                f"[MeshingMonitor] 启动构型{config_name} Meshing "
                f"(尝试 {attempt}/{max_retries})"
            )

            if self._remote_executor.start_meshing(config_name):
                break  # 启动成功，进入等待

            # 启动失败
            if attempt < max_retries:
                retry_count = self.state.increment_retry(config_name, "meshing")
                self.state.set_step_status(
                    config_name, "meshing", STATUS_RETRYING,
                    f"启动重试 {attempt + 1}/{max_retries}（已重试 {retry_count} 次）",
                )
                logger.warning(
                    f"[MeshingMonitor] 构型{config_name} Meshing 启动失败，"
                    f"{5 * attempt}s 后重试"
                )
                if not pause_aware_sleep(5 * attempt, self._paused, self._stopped):
                    return False
            else:
                self.state.set_step_status(
                    config_name, "meshing", STATUS_ERROR,
                    f"Meshing 启动重试 {max_retries} 次后仍然失败",
                )
                logger.error(f"[MeshingMonitor] 构型{config_name} Meshing 启动最终失败")
                return False

        # ---- 轮询等待完成 ----
        logger.info(f"[MeshingMonitor] 等待构型{config_name} 网格划分完成...")
        if self._remote_executor.wait_meshing_completion(
            config_name,
            paused_event=self._paused,
            stopped_event=self._stopped,
        ):
            self.state.set_step_status(config_name, "meshing", STATUS_COMPLETED)
            logger.info(f"[MeshingMonitor] 构型{config_name} 网格划分完成 ✓")
        else:
            if self._paused.is_set():
                self.state.set_step_status(
                    config_name, "meshing", STATUS_PAUSED,
                    "等待网格划分期间暂停",
                )
            elif self._stopped.is_set():
                self.state.set_step_status(
                    config_name, "meshing", STATUS_PAUSED,
                    "引擎已停止",
                )
            else:
                self.state.set_step_status(
                    config_name, "meshing", STATUS_ERROR, "网格划分超时",
                )
        return False

    # ------------------------------------------------------------------
    # 断点续传
    # ------------------------------------------------------------------

    def _scan_db_for_pending(self) -> None:
        """扫描 DB，找出 Transfer 已完成但 Meshing 未完成的构型，补充入队。

        Daemon 重启后内存队列丢失，通过 DB 状态恢复。
        """
        pending = []
        for cn in self.state.get_all_configs():
            transfer_st = self.state.get_step_status(cn, "transfer")
            meshing_st = self.state.get_step_status(cn, "meshing")
            if transfer_st == STATUS_COMPLETED and meshing_st in (
                STATUS_WAITING, STATUS_ERROR, STATUS_RETRYING,
            ):
                pending.append(cn)
            elif transfer_st == STATUS_COMPLETED and meshing_st == STATUS_RUNNING:
                # Daemon 重启时 Meshing 仍在 Running，需要检查远程状态
                pending.append(cn)
            elif transfer_st == STATUS_COMPLETED and meshing_st == STATUS_PAUSED:
                # 暂停状态：恢复后会由 resume() 设置为 Running，此处也入队
                # MeshingMonitor 会在 _process_single_meshing 中检查暂停标志
                pending.append(cn)

        if pending:
            for cn in sorted(pending):
                self.submit(cn)
        else:
            logger.info("[MeshingMonitor] 断点续传: 无需补充的构型")

    def _check_remote_outputs_exist(self, config_name: int) -> bool:
        """检查远程标志文件或网格文件是否已存在（复用共享工具函数）。"""
        try:
            ssh = self._remote_executor.get_ssh_connection()
            return check_step_output_exists(
                config_name, "meshing", "", "", REMOTE_CONFIG, ssh,
            )
        except Exception:
            return False
