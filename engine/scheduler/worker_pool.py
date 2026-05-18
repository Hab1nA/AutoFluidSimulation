"""
工作线程池管理模块。

负责管理 SC/Transfer/Meshing 工作线程的创建、启动和任务分发。
"""

import threading
import queue
import os
import time

from engine.config import (
    STATUS_WAITING, STATUS_RUNNING, STATUS_PAUSED, STATUS_COMPLETED, STATUS_ERROR, STATUS_RETRYING,
    ENGINE_CONFIG, LOCAL_PATHS, REMOTE_CONFIG, get_step_filename,
)
from engine.state_manager import StateManager
from engine.task_runner import TaskRunner
from utils.logger import setup_logger

logger = setup_logger(__name__)


class WorkerPoolManager:
    """
    工作线程池管理器。

    管理多个工作线程，从 SC 队列获取构型，依次执行 SC → Transfer → Meshing。
    """

    def __init__(
        self,
        state_manager: StateManager,
        task_runner: TaskRunner,
        sc_queue: queue.Queue,
        paused_event: threading.Event,
        stopped_event: threading.Event,
        barrier_passed_event: threading.Event,
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
        """
        self.state = state_manager
        self.runner = task_runner
        self._sc_queue = sc_queue
        self._paused = paused_event
        self._stopped = stopped_event
        self._barrier_passed = barrier_passed_event

        # ---- MeshingMonitor（由 PipelineScheduler 注入） ----
        self._meshing_monitor = None

        # ---- 工作线程 ----
        self._worker_threads: list[threading.Thread] = []

        # ---- 工作线程数 ----
        self._num_workers = 3  # SC/Transfer 并发工作线程数

        logger.info("工作线程池管理器初始化完成")

    def set_meshing_monitor(self, meshing_monitor) -> None:
        """注入 MeshingMonitor 实例。由 PipelineScheduler 在创建后调用。"""
        self._meshing_monitor = meshing_monitor

    # ------------------------------------------------------------------
    # 工作线程池管理
    # ------------------------------------------------------------------

    def start_if_needed(self):
        """仅在工作线程未启动或全部死亡时创建新的工作线程池。"""
        alive_workers = [t for t in self._worker_threads if t.is_alive()]
        self._worker_threads = alive_workers
        if not alive_workers:
            self._start_worker_pool()
        else:
            logger.debug(f"工作线程池已存在 ({len(alive_workers)} 个活跃线程)，跳过创建")

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

        _last_queue_report = time.time()
        _queue_report_interval = 30.0  # 每 30 秒输出一次队列健康状态

        while not self._stopped.is_set():
            # 检查暂停
            if self._paused.is_set():
                time.sleep(1)
                continue

            try:
                # 从队列获取任务（1秒超时以便检查停止/暂停标志）
                config_name, _step_file = self._sc_queue.get(timeout=1)
            except queue.Empty:
                # ---- 队列空闲时输出健康状态 ----
                now = time.time()
                if now - _last_queue_report >= _queue_report_interval:
                    qsize = self._sc_queue.qsize()
                    active_workers = sum(1 for t in self._worker_threads if t.is_alive())
                    logger.debug(
                        f"[队列健康] 深度={qsize}, 活跃Worker={active_workers}, "
                        f"Barrier={'已通过' if self._barrier_passed.is_set() else '未通过'}"
                    )
                    _last_queue_report = now
                    # 队列长时间为空且无活跃任务时记录 INFO
                    if qsize == 0:
                        waiting_configs = [
                            cn for cn in self.state.get_all_configs()
                            if self.state.get_step_status(cn, "SC") == STATUS_WAITING
                            and self.state.get_step_status(cn, "SW") == STATUS_COMPLETED
                        ]
                        if waiting_configs:
                            logger.warning(
                                f"[队列异常] {len(waiting_configs)} 个构型 SW 已完成但未入队: "
                                f"{waiting_configs[:5]}{'...' if len(waiting_configs)>5 else ''}"
                            )
                continue

            logger.info(f"[{threading.current_thread().name}] 开始处理构型{config_name}")

            try:
                self._process_single_config(config_name)
            except (RuntimeError, ValueError, OSError, ConnectionError) as e:
                logger.error(f"处理构型{config_name} 时发生未预期异常: {e}", exc_info=True)
                # 只标记 SC+Transfer 步骤为 Error（Meshing 由 MeshingMonitor 管理）
                for step in ["SC", "Transfer"]:
                    try:
                        s = self.state.get_step_status(config_name, step)
                        if s not in (STATUS_COMPLETED, STATUS_ERROR, STATUS_PAUSED):
                            self.state.set_step_status(config_name, step, STATUS_ERROR, str(e))
                    except Exception as mark_err:
                        logger.debug(f"标记构型{config_name}步骤{step}为Error时异常: {mark_err}")
                # Transfer 失败时，Meshing 也无法执行，标记为 Error
                self._mark_meshing_error_if_transfer_failed(config_name, str(e))
            except Exception as e:
                # 最后的兜底：捕获所有其他异常类型，防止工作线程意外崩溃
                logger.critical(
                    f"处理构型{config_name} 时发生致命异常: {type(e).__name__}: {e}",
                    exc_info=True
                )
                for step in ["SC", "Transfer"]:
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
        # 注：STATUS_RUNNING 也包含在内，以处理以下场景：
        # pause→kill SC进程→标记PAUSED→resume→set_all_paused_to_running()→RUNNING，
        # 此时需重新执行 SC（进程已被终止，不可恢复）。
        sc_status = self.state.get_step_status(config_name, "SC")
        if sc_status in (STATUS_WAITING, STATUS_RUNNING, STATUS_PAUSED, STATUS_ERROR, STATUS_RETRYING):
            # ★ 执行前检查暂停标志
            while self._paused.is_set() and not self._stopped.is_set():
                time.sleep(1)
            if self._stopped.is_set():
                return
            # ★ 执行前检查输出文件：若 SCDOC 已存在则直接标记完成，避免重复启动 SC
            scdoc_name = get_step_filename("SC", config_name)
            if scdoc_name:
                scdoc_path = os.path.join(LOCAL_PATHS["scdoc_dir"], scdoc_name)
                if os.path.exists(scdoc_path) and os.path.getsize(scdoc_path) > 0:
                    logger.info(
                        f"构型{config_name} SC: SCDOC 文件已存在，跳过执行"
                    )
                    self.state.set_step_status(config_name, "SC", STATUS_COMPLETED)
                elif not self._execute_with_retry(config_name, "SC",
                                                   self.runner.execute_sc_step):
                    return
            elif not self._execute_with_retry(config_name, "SC",
                                               self.runner.execute_sc_step):
                return

        # ★ SC 完成后检查暂停标志
        while self._paused.is_set() and not self._stopped.is_set():
            time.sleep(1)
        if self._stopped.is_set():
            return

        # ---- Transfer 阶段 ----
        transfer_status = self.state.get_step_status(config_name, "Transfer")
        if transfer_status in (STATUS_WAITING, STATUS_RUNNING, STATUS_PAUSED, STATUS_ERROR, STATUS_RETRYING):
            # ★ 执行前检查远程输出文件：若远程 SCDOC 已存在则跳过上传
            transfer_skip = False
            scdoc_name = get_step_filename("SC", config_name)
            if scdoc_name:
                try:
                    ssh = self.runner.get_ssh()
                    if ssh.is_connected():
                        remote_scdoc = (
                            f"{REMOTE_CONFIG['scdoc_dir'].replace(chr(92), '/')}"
                            f"/{scdoc_name}"
                        )
                        if ssh.check_remote_file(remote_scdoc):
                            logger.info(
                                f"构型{config_name} Transfer: "
                                f"远程 SCDOC 已存在，跳过执行"
                            )
                            self.state.set_step_status(
                                config_name, "Transfer", STATUS_COMPLETED
                            )
                            transfer_skip = True
                except Exception:
                    pass
            if not transfer_skip:
                if not self._execute_with_retry(config_name, "Transfer",
                                                 self.runner.execute_transfer):
                    return

        # ★ Transfer 完成后检查暂停标志
        while self._paused.is_set() and not self._stopped.is_set():
            time.sleep(1)
        if self._stopped.is_set():
            return

        # ---- 提交 Meshing 到 MeshingMonitor ----
        if self._meshing_monitor is not None:
            self._meshing_monitor.submit(config_name)
            logger.info(f"构型{config_name} SC→Transfer 完成，已提交 MeshingMonitor")
        else:
            logger.warning(
                f"构型{config_name} Transfer 完成但 MeshingMonitor 未就绪，"
                f"Meshing 将在下次重启时由断点续传处理"
            )

    def _execute_with_retry(self, config_name: int, step_name: str,
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

        for attempt in range(1, int(max_retries) + 1):
            # 检查是否被停止
            if self._stopped.is_set():
                return False

            # 检查暂停（在设置状态前检查，避免竞态）
            while self._paused.is_set() and not self._stopped.is_set():
                time.sleep(1)
            if self._stopped.is_set():
                return False

            # 设置 Running 状态
            self.state.set_step_status(config_name, step_name, STATUS_RUNNING)

            # 设置状态后立即再次检查暂停标志：
            # 防止 pause() 在 set_step_status 之后被调用导致的竞态窗口
            if self._paused.is_set():
                self.state.set_step_status(config_name, step_name, STATUS_PAUSED)
                while self._paused.is_set() and not self._stopped.is_set():
                    time.sleep(1)
                if self._stopped.is_set():
                    return False
                # 恢复后重新设置运行状态
                self.state.set_step_status(config_name, step_name, STATUS_RUNNING)

            logger.info(f"执行 [{step_name}] 构型{config_name} (尝试 {attempt}/{max_retries})")

            try:
                success = execute_func(config_name)
                if success:
                    # 对于 Meshing 和 Solver，状态由调用者设置（因为需要等待远程完成）
                    if step_name not in ("Meshing", "Solver"):
                        self.state.set_step_status(config_name, step_name, STATUS_COMPLETED)
                    return True
                else:
                    # ★ 检查是否因暂停/停止导致执行失败
                    # （例如 execute_sc_step 在轮询中检测到暂停标志，终止了 SC 进程）
                    if self._paused.is_set():
                        self.state.set_step_status(
                            config_name, step_name, STATUS_PAUSED,
                            "暂停——任务已中断，恢复后将重新执行"
                        )
                        logger.info(
                            f"[{step_name}] 构型{config_name} 因暂停中断，"
                            f"已标记为 Paused"
                        )
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
                        if not self._pause_aware_sleep(5 * attempt):
                            return False  # stopped
            except (RuntimeError, ValueError, OSError) as e:
                logger.error(f"[{step_name}] 构型{config_name} 异常: {e}")
                if attempt < max_retries:
                    self.state.set_step_status(
                        config_name, step_name, STATUS_RETRYING,
                        f"异常重试 {attempt + 1}/{max_retries}: {e}"
                    )
                    if not self._pause_aware_sleep(5 * attempt):
                        return False  # stopped

        # 所有重试均失败
        self.state.set_step_status(config_name, step_name, STATUS_ERROR,
                                    f"重试 {max_retries} 次后仍然失败")
        return False

    def _mark_meshing_error_if_transfer_failed(self, config_name: int, reason: str) -> None:
        """Transfer 失败时，将 Meshing 标记为 Error（若尚未完成）。"""
        meshing_st = self.state.get_step_status(config_name, "Meshing")
        if meshing_st not in (STATUS_COMPLETED, STATUS_ERROR):
            self.state.set_step_status(
                config_name, "Meshing", STATUS_ERROR,
                f"上游 Transfer 失败: {reason}",
            )
            logger.info(
                f"构型{config_name} Meshing 因 Transfer 失败标记为 Error"
            )

    def _pause_aware_sleep(self, duration: float, check_interval: float = 1.0) -> bool:
        """
        可响应暂停/停止的 sleep 替代方法。

        将 sleep 切分为 check_interval 粒度的小段，每段检查
        _paused 和 _stopped 标志。若检测到 stopped 则立即返回。

        Args:
            duration: 总等待时长（秒）
            check_interval: 每次检查的间隔（秒）

        Returns:
            True 表示 sleep 完整结束，False 表示因 stopped 提前退出
        """
        deadline = time.time() + duration
        while time.time() < deadline:
            if self._stopped.is_set():
                return False
            while self._paused.is_set() and not self._stopped.is_set():
                time.sleep(1)
            if self._stopped.is_set():
                return False
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            time.sleep(min(check_interval, remaining))
        return True
