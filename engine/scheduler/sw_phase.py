"""
SW 阶段处理模块。

负责 SolidWorks 阶段的执行、下游步骤状态同步和重试准备。
"""

import threading
import subprocess
import queue
import os
from typing import Optional

from engine.config import (
    STATUS_WAITING, STATUS_RUNNING, STATUS_PAUSED, STATUS_COMPLETED, STATUS_ERROR, STATUS_RETRYING,
    LOCAL_PATHS, REMOTE_CONFIG, STEP_NAMES, get_step_filename,
)
from engine.state_manager import StateManager
from engine.task_runner import TaskRunner
from engine.file_monitor import StepFileMonitor
from engine.scheduler.utils import pause_aware_sleep, wait_unless_paused_or_stopped, check_step_output_exists
from utils.logger import setup_logger

logger = setup_logger(__name__)


class SWPhaseHandler:
    """
    SW 阶段处理器。

    负责 SolidWorks 阶段的执行、下游步骤状态同步和重试准备。
    """

    def __init__(
        self,
        state_manager: StateManager,
        task_runner: TaskRunner,
        sc_queue: queue.Queue[tuple[int, str]],
        paused_event: threading.Event,
        stopped_event: threading.Event,
        retry_manager,
        worker_pool_manager=None,
        meshing_monitor=None,
    ):
        """
        初始化 SW 阶段处理器。

        Args:
            state_manager: 共享状态管理器
            task_runner: 任务执行器
            sc_queue: SC 处理队列
            paused_event: 暂停事件
            stopped_event: 停止事件
            retry_manager: 重试管理器（统一 SW/SC/Transfer/Meshing/Solver 重试逻辑）
            worker_pool_manager: 工作线程池管理器（用于启动工作线程）
            meshing_monitor: 网格划分监控器（用于提前启动消费线程）
        """
        self.state = state_manager
        self.runner = task_runner
        self._sc_queue = sc_queue
        self._paused = paused_event
        self._stopped = stopped_event
        self._retry_manager = retry_manager
        self.worker_pool_manager = worker_pool_manager
        self.meshing_monitor = meshing_monitor

        # 文件监控器引用（在 start_pipeline 中设置）
        self._file_monitor: Optional[StepFileMonitor] = None

        # 是否需要递归调用 start_pipeline（断点续传检测到 SW Error）
        self.needs_recurse: bool = False

        logger.info("SW 阶段处理器初始化完成")

    def set_file_monitor(self, file_monitor: StepFileMonitor):
        """设置文件监控器引用。"""
        self._file_monitor = file_monitor

    def execute_sw_phase(self, recursion_depth: int = 0) -> bool:
        """
        执行 SW 阶段。

        Args:
            recursion_depth: 递归深度计数器

        Returns:
            True 表示 SW 阶段成功完成，False 表示失败或需要中止。
            注意：当返回 False 且 sw_macro_started 为 False 时，
            调用方应递归调用 start_pipeline 重新进入 SW 阶段。
        """
        all_configs = self.state.get_all_configs()
        sw_status_dist: dict[str, list[int]] = {}
        for cn in all_configs:
            st = self.state.get_step_status(cn, "SW")
            sw_status_dist.setdefault(st, []).append(cn)
        logger.info(
            f"[SW] 步骤状态分布: { {k: len(v) for k, v in sw_status_dist.items()} }；"
            f"sw_macro_started={self.state.is_sw_macro_started()}"
        )

        # 综合 sw_macro_started 标志和实际步骤状态判断是否执行 SW
        should_run_sw = not self.state.is_sw_macro_started()
        if should_run_sw:
            sw_all_completed = (
                len(sw_status_dist.get(STATUS_COMPLETED, [])) == len(all_configs)
                and len(all_configs) > 0
            )
            if sw_all_completed:
                logger.warning(
                    f"[SW] 检测到 sw_macro_started=false 但所有 {len(all_configs)} 个构型的 "
                    f"SW 步骤均为 Completed。自愈：设置 sw_macro_started=true"
                )
                self.state.set_sw_macro_started(True)
                should_run_sw = False

        if should_run_sw:
            return self._execute_sw_macro(all_configs)
        else:
            return self._handle_sw_breakpoint_resume(all_configs, recursion_depth)

    def _execute_sw_macro(self, all_configs: list[int]) -> bool:
        """
        执行 SW 宏。

        使用 RetryManager 逐构型导出 STEP 文件，与 SC/Transfer/Meshing/Solver
        步骤共享统一的重试逻辑（状态转换、暂停感知、退避等待）。

        Args:
            all_configs: 所有构型列表

        Returns:
            True 表示 SW 宏执行成功，False 表示失败
        """
        logger.info("[SW] SW 步骤尚未启动，准备执行...")

        # 将所有构型的 SW 状态设为 Running（仅限 Waiting/Paused/Error/Retrying 状态）
        for cn in all_configs:
            current_status = self.state.get_step_status(cn, "SW")
            if current_status in (STATUS_WAITING, STATUS_PAUSED, STATUS_ERROR, STATUS_RETRYING):
                self.state.set_step_status(cn, "SW", STATUS_RUNNING)

        # ---- SW 步骤前暂停检查 ----
        if self._paused.is_set():
            logger.info("[SW] SW 步骤启动前检测到暂停标志，等待继续指令...")
            self.state.set_engine_status("paused")
            if not wait_unless_paused_or_stopped(self._paused, self._stopped):
                return False
            # 恢复后重新标记 SW 为 Running
            for cn in all_configs:
                if self.state.get_step_status(cn, "SW") == STATUS_PAUSED:
                    self.state.set_step_status(cn, "SW", STATUS_RUNNING)

        # ★ 提前启动文件监控和工作线程池（在 SW 宏执行前启动，
        #    以便在宏逐文件导出 STEP 时实时检测文件写入完成，
        #    实现边导出边处理的并行流水线）
        #    同时启动 MeshingMonitor 消费线程，确保 Worker 完成 Transfer
        #    后提交的构型能被及时消费，而非堆积到 SW 阶段结束后。
        self._ensure_file_monitor_running()
        if self.worker_pool_manager:
            self.worker_pool_manager.start_if_needed()
        if self.meshing_monitor:
            self.meshing_monitor.start_if_needed()

        # ---- 通过 RetryManager 逐构型导出 STEP ----
        # 每个构型独立重试，失败时 RetryManager 自动管理
        # Running → Retrying → Running 状态转换和退避等待。
        # SWExecutor 管理 COM 连接生命周期，首次调用时建立连接，
        # 后续构型复用同一连接；重试时由 _prepare_sw_retry 清理进程。
        for cn in all_configs:
            if self._stopped.is_set():
                return False

            # 跳过已完成的构型（断点续传 / 之前批次已成功）
            if self.state.get_step_status(cn, "SW") == STATUS_COMPLETED:
                continue

            ok = self._retry_manager.execute_with_retry(
                cn, "SW", self.runner.execute_sw_per_config,
            )
            if not ok and not self._paused.is_set() and not self._stopped.is_set():
                # 构型所有重试均失败（非暂停/停止导致）→ 断开缓存连接，
                # 使下一个构型重新建立连接（若 SW 进程已崩溃可快速失败）
                self.runner._sw_executor.disconnect_sw_cached()

            # ★ 暂停中断：立即退出循环，交给尾部暂停善后逻辑统一处理。
            #   不中断会导致循环继续到下一构型并阻塞在
            #   wait_unless_paused_or_stopped，resume 后该构型成功但
            #   当前构型仍停留在 Paused 状态，成为漏网之鱼。
            if self._paused.is_set():
                break

        # ---- 清理缓存的 SW 连接（无论成功与否） ----
        self.runner._sw_executor.disconnect_sw_cached()

        # ---- 汇总与善后 ----
        if self._stopped.is_set():
            return False

        if self._paused.is_set():
            logger.warning("[SW] SW 步骤在暂停期间中断，保留 Paused 状态以供恢复后重试")
            for cn in all_configs:
                if self.state.get_step_status(cn, "SW") == STATUS_RUNNING:
                    self.state.set_step_status(cn, "SW", STATUS_PAUSED)
            self.state.set_engine_status("paused")
            return False

        # ★ 通过最终状态判断成功与否（而非中间标志）
        #   RetryManager 已将成功构型设为 COMPLETED、失败构型设为 ERROR
        sw_errors = [cn for cn in all_configs
                     if self.state.get_step_status(cn, "SW") == STATUS_ERROR]
        sw_running = [cn for cn in all_configs
                      if self.state.get_step_status(cn, "SW") == STATUS_RUNNING]

        if sw_errors or sw_running:
            # 有构型失败 → 清理 SW 进程并阻断下游
            self._prepare_sw_retry()

            if sw_errors:
                logger.warning(
                    f"[SW] SW 阶段部分失败: 构型 {sorted(sw_errors)} STEP 导出失败"
                )
                for cn in sw_errors:
                    for s in ["SC", "Transfer", "Meshing", "Solver"]:
                        if self.state.get_step_status(cn, s) == STATUS_WAITING:
                            self.state.set_step_status(
                                cn, s, STATUS_ERROR,
                                f"上游 SW 导出失败，{s} 已阻断"
                            )
            if sw_running:
                logger.warning(
                    f"[SW] {len(sw_running)} 个构型仍为 Running 状态 "
                    f"(可能导出中断): {sorted(sw_running)}"
                )

            if not sw_errors:
                # 无 ERROR 但有 RUNNING → 全部构型均未完成，引擎停止
                self.state.set_engine_status("stopped")
                logger.error("[SW] SW 步骤失败，流水线中止")
                return False

        # 安全网校验 + 设置 sw_macro_started
        step_dir = LOCAL_PATHS.get("step_dir", "")
        total_found = self.runner._sw_executor._verify_step_exports(step_dir)
        if total_found > 0 and not sw_errors:
            self.state.set_sw_macro_started(True)
            logger.info(
                f"[SW] sw_macro_started=True "
                f"（{total_found}/{len(all_configs)} 构型 STEP 就绪）"
            )

        sw_completed = [cn for cn in all_configs
                        if self.state.get_step_status(cn, "SW") == STATUS_COMPLETED]
        if sw_completed:
            logger.info(
                f"[SW] SW 阶段完成: {len(sw_completed)}/{len(all_configs)} 个构型 STEP 就绪"
            )
        return True

    def _handle_sw_breakpoint_resume(self, all_configs: list[int], recursion_depth: int) -> bool:
        """
        处理 SW 断点续传。

        Args:
            all_configs: 所有构型列表
            recursion_depth: 递归深度计数器

        Returns:
            True 表示处理成功，False 表示需要递归调用
        """
        logger.info("[SW] SW 步骤已执行过，跳过（断点续传模式）")
        # 断点续传时，检查是否有 SW 步骤处于 Paused 或 Error 状态
        # Paused：恢复后需重新校验 STEP 文件
        # Error：清除 sw_macro_started 标志以允许重试 SW 宏
        has_paused_sw = False
        has_error_sw = False
        for cn in all_configs:
            sw_status = self.state.get_step_status(cn, "SW")
            if sw_status == STATUS_PAUSED:
                has_paused_sw = True
            elif sw_status == STATUS_ERROR:
                has_error_sw = True

        if has_paused_sw:
            # 暂停恢复：重新校验 STEP 文件
            logger.info("[SW] 检测到 SW Paused 构型，重新校验 STEP 文件...")
            step_dir = LOCAL_PATHS.get("step_dir", "")
            for cn in all_configs:
                if self.state.get_step_status(cn, "SW") == STATUS_PAUSED:
                    filename = get_step_filename("SW", cn)
                    if not filename:
                        self.state.set_step_status(cn, "SW", STATUS_ERROR,
                                                   "无法生成 STEP 文件名")
                        has_error_sw = True
                        continue
                    expected_file = os.path.join(step_dir, filename)
                    if os.path.exists(expected_file):
                        self.state.set_step_status(cn, "SW", STATUS_COMPLETED)
                        logger.info(f"[SW]   构型{cn} ✓ STEP 文件已存在，标记为完成")
                    else:
                        self.state.set_step_status(cn, "SW", STATUS_ERROR,
                                                   "暂停恢复后 STEP 文件仍缺失")
                        has_error_sw = True
                        logger.warning(f"[SW]   构型{cn} ✗ STEP 文件缺失")

        if has_error_sw:
            logger.warning(
                "[SW] 检测到 SW Error 构型，将清除 sw_macro_started 标志以允许重新执行 SW 步骤"
            )
            self.state.set_sw_macro_started(False)
            self.needs_recurse = True
            return False  # 需要外部递归调用 start_pipeline

        return True

    def _ensure_file_monitor_running(self):
        """确保文件监控器正在运行。

        优先使用由 PipelineScheduler 注入的共享实例（通过 set_file_monitor()），
        仅在未注入时回退为自行创建（独立测试场景）。
        """
        if self._file_monitor is None:
            # 防御性回退：未注入时自行创建（独立测试场景）
            # 注意：回退创建的监控器使用简化的回退回调，生产环境应始终由
            # PipelineScheduler 注入带有完整 _on_step_file_ready 逻辑的实例
            def _fallback_on_file_ready(config_name: int, filepath: str) -> None:
                if self._paused.is_set():
                    return
                if self.state.get_step_status(config_name, "SW") != STATUS_COMPLETED:
                    self.state.set_step_status(config_name, "SW", STATUS_COMPLETED)
                self._sc_queue.put((config_name, filepath))
                logger.info(f"[SW] 构型{config_name} 已推入 SC 处理队列 (回退回调)")

            self._file_monitor = StepFileMonitor(
                step_dir=None,
                on_file_ready=_fallback_on_file_ready,
                shared_paused_event=self._paused,
            )
            logger.info("[SW] 文件监控器未注入，已自行创建（独立模式）")
        if not self._file_monitor.is_running:
            self._file_monitor.start()
            logger.info("[SW] 已启动 STEP 文件监控（提前于 SW 宏，实现边导出边处理）")

    # ------------------------------------------------------------------
    # 同步下游步骤状态与文件系统（断点续传）
    # ------------------------------------------------------------------

    def scan_completed_downstream(self):
        """
        启动下游组件前，同步 DB 状态与文件系统。

        扫描所有构型各步骤的输出文件，若文件已存在但步骤仍为未完成状态，
        则直接标记为 Completed，避免下游组件重复执行。
        """
        all_configs = self.state.get_all_configs()
        if not all_configs:
            return

        skipped_count = 0
        ssh = None

        # 尝试获取 SSH 连接用于远程文件检查（失败不阻塞）
        try:
            ssh = self.runner.get_ssh()
            if not ssh.is_connected():
                ssh = None
        except Exception:
            ssh = None

        step_dir = LOCAL_PATHS["step_dir"]
        scdoc_dir = LOCAL_PATHS["scdoc_dir"]

        for cn in all_configs:
            for step in STEP_NAMES:
                # 跳过已完成和错误状态（ERROR 为重试终态，留给 retry 机制处理）
                status = self.state.get_step_status(cn, step)
                if status in (STATUS_COMPLETED, STATUS_ERROR):
                    continue

                # ---- 依赖链检查：仅在上游步骤已完成时才检查下游 ----
                if step == "Transfer" and self.state.get_step_status(cn, "SC") != STATUS_COMPLETED:
                    continue
                if step == "Meshing" and self.state.get_step_status(cn, "Transfer") != STATUS_COMPLETED:
                    continue
                if step == "Solver" and self.state.get_step_status(cn, "Meshing") != STATUS_COMPLETED:
                    continue

                # ---- 远程步骤需要 SSH ----
                if step in ("Transfer", "Meshing", "Solver") and ssh is None:
                    continue

                if check_step_output_exists(cn, step, step_dir, scdoc_dir, REMOTE_CONFIG, ssh):
                    self.state.set_step_status(cn, step, STATUS_COMPLETED)
                    skipped_count += 1

        if skipped_count > 0:
            logger.info(
                f"[下游扫描] 共跳过 {skipped_count} 个步骤（输出文件已存在）"
            )
        else:
            logger.info("[下游扫描] 所有待执行步骤均无现成输出文件")

    # ------------------------------------------------------------------
    # SW 重试准备
    # ------------------------------------------------------------------

    def _prepare_sw_retry(self):
        """
        为 SW 步骤重试做准备：清理残留 SW 进程、重置文件监控器状态。

        状态管理（Running → Retrying → Running）已由 RetryManager 在
        逐构型重试时自动处理，本方法仅负责 SW 进程级清理。
        """
        # ★ 暂停时跳过进程清理（杀进程、冷却等待），但始终重置文件监控器状态。
        #   文件监控器的 _processed_files 记录了旧 STEP 文件，若不清理，
        #   恢复后重新导出的同名文件不会被检测到，导致 SC 队列缺少任务。
        if self._paused.is_set():
            logger.info("[SW] 重试准备中检测到暂停标志，跳过进程清理")
            if self._file_monitor is not None:
                self._file_monitor._processed_files.clear()
                self._file_monitor._known_files.clear()
                self._file_monitor._detector._history.clear()
                self._file_monitor._detector._first_seen.clear()
                logger.info("[SW] 文件监控器状态已重置（暂停期间仍清理，确保恢复后可检测新文件）")
            return

        logger.info("[SW] SW 步骤重试准备：等待 10 秒并清理残留进程...")
        if not pause_aware_sleep(10, self._paused, self._stopped):
            return

        # 终止残留 SW 进程
        try:
            subprocess.run(
                ["taskkill", "/f", "/im", "SLDWORKS.exe"],
                capture_output=True, timeout=30,
            )
        except (OSError, subprocess.SubprocessError, subprocess.TimeoutExpired):
            pass

        # 等待 SW 进程完全退出
        logger.info("[SW-Cleanup] 等待 SolidWorks 进程完全退出...")
        for _ in range(10):
            if not pause_aware_sleep(1, self._paused, self._stopped):
                return
            try:
                check = subprocess.run(
                    ["tasklist", "/fi", "IMAGENAME eq SLDWORKS.exe",
                     "/fo", "csv", "/nh"],
                    capture_output=True, text=True, timeout=5,
                )
                if "SLDWORKS.exe" not in check.stdout:
                    logger.info("[SW-Cleanup] ✓ SolidWorks 进程已退出")
                    break
            except (OSError, subprocess.SubprocessError, subprocess.TimeoutExpired):
                break

        # 额外冷却确保 COM 子系统完全释放
        if not pause_aware_sleep(5, self._paused, self._stopped):
            return

        # 重置文件监控器状态，避免重试时同名文件被跳过
        if self._file_monitor is not None:
            self._file_monitor._processed_files.clear()
            self._file_monitor._known_files.clear()
            self._file_monitor._detector._history.clear()
            self._file_monitor._detector._first_seen.clear()
            logger.info("[SW] 文件监控器状态已重置（准备 SW 步骤重试）")
