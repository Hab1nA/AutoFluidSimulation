"""
SW 阶段处理模块。

负责 SolidWorks 阶段的执行、预扫描和重试准备。
"""

import threading
import subprocess
import queue
import os
import time
from typing import Optional

from engine.config import (
    STATUS_WAITING, STATUS_RUNNING, STATUS_PAUSED, STATUS_COMPLETED, STATUS_ERROR, STATUS_RETRYING,
    ENGINE_CONFIG, LOCAL_PATHS, REMOTE_CONFIG, get_step_filename,
)
from engine.state_manager import StateManager
from engine.task_runner import TaskRunner
from engine.file_monitor import StepFileMonitor
from utils.logger import setup_logger

logger = setup_logger(__name__)


class SWPhaseHandler:
    """
    SW 阶段处理器。

    负责 SolidWorks 阶段的执行、预扫描和重试准备。
    """

    def __init__(
        self,
        state_manager: StateManager,
        task_runner: TaskRunner,
        sc_queue: queue.Queue,
        paused_event: threading.Event,
        stopped_event: threading.Event,
        worker_pool_manager=None,
    ):
        """
        初始化 SW 阶段处理器。

        Args:
            state_manager: 共享状态管理器
            task_runner: 任务执行器
            sc_queue: SC 处理队列
            paused_event: 暂停事件
            stopped_event: 停止事件
            worker_pool_manager: 工作线程池管理器（用于启动工作线程）
        """
        self.state = state_manager
        self.runner = task_runner
        self._sc_queue = sc_queue
        self._paused = paused_event
        self._stopped = stopped_event
        self.worker_pool_manager = worker_pool_manager

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

        Args:
            all_configs: 所有构型列表

        Returns:
            True 表示 SW 宏执行成功，False 表示失败
        """
        logger.info("[SW] SW 步骤尚未启动，准备执行...")
        all_configs = self.state.get_all_configs()

        # 将所有构型的 SW 状态设为 Running（仅限 Waiting/Paused/Error/Retrying 状态）
        for cn in all_configs:
            current_status = self.state.get_step_status(cn, "SW")
            if current_status in (STATUS_WAITING, STATUS_PAUSED, STATUS_ERROR, STATUS_RETRYING):
                self.state.set_step_status(cn, "SW", STATUS_RUNNING)

        # ---- SW 步骤前暂停检查 ----
        if self._paused.is_set():
            logger.info("[SW] SW 步骤启动前检测到暂停标志，等待继续指令...")
            self.state.set_engine_status("paused")
            while self._paused.is_set() and not self._stopped.is_set():
                time.sleep(1)
            if self._stopped.is_set():
                return False
            # 恢复后重新标记 SW 为 Running
            for cn in all_configs:
                if self.state.get_step_status(cn, "SW") == STATUS_PAUSED:
                    self.state.set_step_status(cn, "SW", STATUS_RUNNING)

        # ★ 提前启动文件监控和工作线程池（在 SW 宏执行前启动，
        #    以便在宏逐文件导出 STEP 时实时检测文件写入完成，
        #    实现边导出边处理的并行流水线）
        self._ensure_file_monitor_running()
        if self.worker_pool_manager:
            self.worker_pool_manager.start_if_needed()

        # 执行 SW 步骤（含重试机制）
        sw_max_retries = ENGINE_CONFIG.get("sw_max_retries", 1)
        sw_success = False
        for sw_attempt in range(1, int(sw_max_retries) + 1):
            if self._stopped.is_set():
                return False

            if sw_attempt > 1:
                self._prepare_sw_retry(all_configs, sw_attempt, sw_max_retries)
                if self._stopped.is_set():
                    return False

            logger.info(
                f"[SW] 执行 SW 步骤 (尝试 {sw_attempt}/{sw_max_retries})..."
            )
            sw_success = self.runner.execute_sw_step()
            if sw_success:
                break

        # 若最终仍失败，将仍为 Running/Retrying 的构型标记为 Error
        if not sw_success and not self._paused.is_set():
            for cn in all_configs:
                sw_st = self.state.get_step_status(cn, "SW")
                if sw_st not in (STATUS_COMPLETED, STATUS_ERROR, STATUS_PAUSED):
                    self.state.set_step_status(
                        cn, "SW", STATUS_ERROR,
                        f"SW 步骤失败（重试 {sw_max_retries} 次后）"
                    )
            self.state.set_engine_status("stopped")
            logger.error("[SW] SW 步骤失败，流水线中止")
            return False

        if not sw_success:
            # SW 步骤失败且处于暂停状态
            if self._paused.is_set():
                logger.warning("[SW] SW 步骤在暂停期间失败，保留 Paused 状态以供恢复后重试")
                for cn in all_configs:
                    if self.state.get_step_status(cn, "SW") == STATUS_RUNNING:
                        self.state.set_step_status(cn, "SW", STATUS_PAUSED)
                self.state.set_engine_status("paused")
            return False
        else:
            # SW 步骤成功执行，但 execute_sw_step 内部可能已标记部分构型为 Error
            # （例如某些构型的 STEP 文件缺失）
            # 若此时暂停标志已置位，将这些 Error 步骤回退为 Paused
            if self._paused.is_set():
                paused_count = 0
                for cn in all_configs:
                    sw_status = self.state.get_step_status(cn, "SW")
                    if sw_status == STATUS_ERROR:
                        self.state.set_step_status(cn, "SW", STATUS_PAUSED,
                                                   "暂停中——恢复后将重新校验 STEP")
                        paused_count += 1
                if paused_count > 0:
                    logger.info(
                        f"[SW] 暂停标志已置位，已将 {paused_count} 个 SW Error 构型回退为 Paused"
                    )

        # SW 阶段导出汇总
        if not self._paused.is_set():
            sw_completed = [cn for cn in all_configs
                            if self.state.get_step_status(cn, "SW") == STATUS_COMPLETED]
            sw_errors = [cn for cn in all_configs
                         if self.state.get_step_status(cn, "SW") == STATUS_ERROR]
            sw_running = [cn for cn in all_configs
                          if self.state.get_step_status(cn, "SW") == STATUS_RUNNING]
            if sw_completed:
                logger.info(
                    f"[SW] SW 阶段完成: {len(sw_completed)}/{len(all_configs)} 个构型 STEP 就绪"
                )
            if sw_errors:
                logger.warning(
                    f"[SW] SW 阶段部分失败: 构型 {sorted(sw_errors)} STEP 导出失败"
                )
                # 阻断失败构型的下游步骤（避免 worker 线程误处理）
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
        all_configs = self.state.get_all_configs()
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
        """确保文件监控器正在运行。"""
        if self._file_monitor is None or not self._file_monitor.is_running:
            self._file_monitor = StepFileMonitor(
                step_dir=None,
                on_file_ready=self._on_step_file_ready
            )
            self._file_monitor.start()
            logger.info("[SW] 文件监控已提前启动（在 SW 步骤执行前）")

    def _on_step_file_ready(self, config_name: int, filepath: str):
        """文件就绪回调。"""
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
    # 输出文件预扫描（断点续传核心）
    # ------------------------------------------------------------------

    def prescan_downstream_outputs(self):
        """
        在启动下游工作线程之前，扫描所有构型各步骤的输出文件。

        若输出文件已存在于磁盘但数据库中该步骤仍为未完成状态，
        则直接标记为 Completed，避免重复启动程序执行该步骤。

        覆盖范围：
        - SW: 本地 STEP 文件
        - SC: 本地 SCDOC 文件
        - Transfer: 远程 SCDOC 文件（需 SSH）
        - Meshing: 远程标志文件 + 网格输出文件（需 SSH）
        - Solver: 远程标志文件 + 求解输出文件（需 SSH）
        """
        all_configs = self.state.get_all_configs()
        if not all_configs:
            return

        prescan_count = 0
        ssh_available = False
        ssh = None

        # 尝试获取 SSH 连接用于远程文件检查（失败不阻塞）
        try:
            ssh = self.runner.get_ssh()
            ssh_available = ssh.is_connected()
        except Exception:
            pass

        for cn in all_configs:
            # ---- SW: 检查本地 STEP 文件 ----
            sw_status = self.state.get_step_status(cn, "SW")
            if sw_status not in (STATUS_COMPLETED,):
                sw_filename = get_step_filename("SW", cn)
                if sw_filename:
                    sw_filepath = os.path.join(LOCAL_PATHS["step_dir"], sw_filename)
                    if os.path.exists(sw_filepath) and os.path.getsize(sw_filepath) > 0:
                        self.state.set_step_status(cn, "SW", STATUS_COMPLETED)
                        logger.info(
                            f"[Prescan] 构型{cn} SW: STEP 文件已存在，"
                            f"标记为 Completed"
                        )
                        prescan_count += 1

            # ---- SC: 检查本地 SCDOC 文件 ----
            sc_status = self.state.get_step_status(cn, "SC")
            if sc_status not in (STATUS_COMPLETED,):
                scdoc_name = get_step_filename("SC", cn)
                if scdoc_name:
                    scdoc_path = os.path.join(LOCAL_PATHS["scdoc_dir"], scdoc_name)
                    if os.path.exists(scdoc_path) and os.path.getsize(scdoc_path) > 0:
                        self.state.set_step_status(cn, "SC", STATUS_COMPLETED)
                        logger.info(
                            f"[预扫描] 构型{cn} SC: SCDOC 文件已存在，"
                            f"标记为 Completed"
                        )
                        prescan_count += 1

            # ---- Transfer: 检查远程 SCDOC 文件 ----
            transfer_status = self.state.get_step_status(cn, "Transfer")
            if transfer_status not in (STATUS_COMPLETED,) and ssh_available:
                # Transfer 依赖 SC 输出，SC Completed 后才检查远程文件
                if self.state.get_step_status(cn, "SC") == STATUS_COMPLETED:
                    scdoc_name = get_step_filename("SC", cn)
                    if scdoc_name:
                        remote_scdoc = (
                            f"{REMOTE_CONFIG['scdoc_dir'].replace(chr(92), '/')}"
                            f"/{scdoc_name}"
                        )
                        try:
                            if ssh.check_remote_file(remote_scdoc):
                                self.state.set_step_status(
                                    cn, "Transfer", STATUS_COMPLETED
                                )
                                logger.info(
                                    f"[预扫描] 构型{cn} Transfer: "
                                    f"远程 SCDOC 已存在，标记为 Completed"
                                )
                                prescan_count += 1
                        except Exception:
                            pass

            # ---- Meshing: 检查远程标志文件 + 网格输出文件 ----
            meshing_status = self.state.get_step_status(cn, "Meshing")
            if meshing_status not in (STATUS_COMPLETED,) and ssh_available:
                if self.state.get_step_status(cn, "Transfer") == STATUS_COMPLETED:
                    flag_file = (
                        f"{REMOTE_CONFIG['flag_dir'].replace(chr(92), '/')}"
                        f"/meshing_done_{cn}.txt"
                    )
                    mesh_name = get_step_filename("Meshing", cn)
                    mesh_file = None
                    if mesh_name:
                        mesh_file = (
                            f"{REMOTE_CONFIG['msh_dir'].replace(chr(92), '/')}"
                            f"/{mesh_name}"
                        )
                    try:
                        # 标志文件存在 → 网格划分刚完成但状态未更新
                        # 网格文件存在 → 上一次运行已完成
                        if ssh.check_remote_file(flag_file) or (
                            mesh_file and ssh.check_remote_file(mesh_file)
                        ):
                            self.state.set_step_status(
                                cn, "Meshing", STATUS_COMPLETED
                            )
                            logger.info(
                                f"[预扫描] 构型{cn} Meshing: "
                                f"远程输出已存在，标记为 Completed"
                            )
                            prescan_count += 1
                    except Exception:
                        pass

            # ---- Solver: 检查远程标志文件 + 求解输出文件 ----
            solver_status = self.state.get_step_status(cn, "Solver")
            if solver_status not in (STATUS_COMPLETED,) and ssh_available:
                if self.state.get_step_status(cn, "Meshing") == STATUS_COMPLETED:
                    flag_file = (
                        f"{REMOTE_CONFIG['flag_dir'].replace(chr(92), '/')}"
                        f"/solver_done_{cn}.txt"
                    )
                    result_name = get_step_filename("Solver", cn)
                    result_file = None
                    if result_name:
                        result_file = (
                            f"{REMOTE_CONFIG['result_dir'].replace(chr(92), '/')}"
                            f"/{result_name}"
                        )
                    try:
                        if ssh.check_remote_file(flag_file) or (
                            result_file and ssh.check_remote_file(result_file)
                        ):
                            self.state.set_step_status(
                                cn, "Solver", STATUS_COMPLETED
                            )
                            logger.info(
                                f"[预扫描] 构型{cn} Solver: "
                                f"远程输出已存在，标记为 Completed"
                            )
                            prescan_count += 1
                    except Exception:
                        pass

        if prescan_count > 0:
            logger.info(
                f"[预扫描] 共标记 {prescan_count} 个步骤为 Completed"
                f"（输出文件已存在）"
            )
        else:
            logger.info("[预扫描] 未发现可跳过的步骤")

    # ------------------------------------------------------------------
    # SW 重试准备
    # ------------------------------------------------------------------

    def _prepare_sw_retry(self, all_configs: list[int], attempt: int, max_retries: int):
        """
        为 SW 步骤重试做准备：清理残留进程、重置监控器状态、设置 Retrying 状态。

        Args:
            all_configs: 所有构型列表
            attempt: 当前重试次数 (1-based)
            max_retries: 最大重试次数
        """
        if self._paused.is_set():
            logger.info("[SW] SW 步骤重试前检测到暂停标志，将在重试延迟中等待继续...")

        # 将所有 SW 步骤标记为 Retrying
        for cn in all_configs:
            sw_status = self.state.get_step_status(cn, "SW")
            if sw_status not in (STATUS_COMPLETED, STATUS_PAUSED):
                self.state.set_step_status(
                    cn, "SW", STATUS_RETRYING,
                    f"SW 步骤重试 {attempt}/{max_retries}"
                )

        logger.info(
            f"[SW] SW 步骤重试 {attempt}/{max_retries}，等待 10 秒并清理残留进程..."
        )
        if not self._pause_aware_sleep(10):
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
            if not self._pause_aware_sleep(1):
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
        if not self._pause_aware_sleep(5):
            return

        # 重置文件监控器状态，避免重试时同名文件被跳过
        if self._file_monitor is not None:
            self._file_monitor._processed_files.clear()
            self._file_monitor._known_files.clear()
            self._file_monitor._detector._history.clear()
            self._file_monitor._detector._first_seen.clear()
            logger.info("[SW] 文件监控器状态已重置（准备 SW 步骤重试）")

        # 仅将 RETRYING 状态恢复为 Running（Paused 保持不变）
        for cn in all_configs:
            current_status = self.state.get_step_status(cn, "SW")
            if current_status == STATUS_RETRYING:
                self.state.set_step_status(cn, "SW", STATUS_RUNNING)

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
