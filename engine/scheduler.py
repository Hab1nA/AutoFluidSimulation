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
import subprocess
import time
import os
from typing import Optional

from engine.config import (
    STEP_INDEX,
    STATUS_WAITING, STATUS_RUNNING, STATUS_PAUSED, STATUS_COMPLETED, STATUS_ERROR, STATUS_RETRYING,
    ENGINE_CONFIG, LOCAL_PATHS, REMOTE_CONFIG, get_step_filename,
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

        # 将控制事件注入 TaskRunner，使长时间阻塞操作（如 SC 的 process.communicate）
        # 能够响应暂停/停止指令
        self.runner.set_control_events(self._paused, self._stopped)

        # ---- 工作队列 ----
        # SC 处理队列：(config_name, step_file_path)
        self._sc_queue: queue.Queue = queue.Queue()

        # ---- 工作线程 ----
        self._worker_threads: list[threading.Thread] = []
        self._barrier_thread: Optional[threading.Thread] = None
        self._solver_threads: list[threading.Thread] = []   # 求解线程（屏障通过后启动）
        self._file_monitor: Optional[StepFileMonitor] = None
        self._pipeline_thread: Optional[threading.Thread] = None  # 主调度线程引用

        # ---- 工作线程数 ----
        self._num_workers = 3  # SC/Transfer/Meshing 并发工作线程数

        # 恢复全局屏障状态（断点续传）
        if self.state.is_global_barrier_met():
            self._barrier_passed.set()

        logger.info("流水线调度器初始化完成")

    # ------------------------------------------------------------------
    # 暂停感知的 sleep 辅助方法
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # 输出文件预扫描（断点续传核心）
    # ------------------------------------------------------------------

    def _prescan_downstream_outputs(self):
        """
        在启动下游工作线程之前，扫描所有构型各步骤的输出文件。

        若输出文件已存在于磁盘但数据库中该步骤仍为未完成状态，
        则直接标记为 Completed，避免重复启动程序执行该步骤。

        覆盖范围：
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
                "start_pipeline 递归深度超过上限 (%d)，可能存在无法自动恢复的错误，"
                "流水线中止。", _recursion_depth
            )

            # 1) 停止所有并行的调度线程（worker / barrier / monitor）
            self._stopped.set()

            # 2) 将所有仍在非终态的构型 SW 步骤标记为 Error，附带详细诊断信息
            all_configs = self.state.get_all_configs()
            terminal_states = {STATUS_COMPLETED, STATUS_ERROR, STATUS_PAUSED}
            error_detail = (
                f"SW 宏自动重试递归深度超过上限 ({_recursion_depth})，"
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
                "已标记 %d/%d 个构型的 SW 步骤为 Error，"
                "%d 个构型的下游步骤亦已阻断",
                error_count, len(all_configs),
                sum(1 for cn in all_configs
                    if self.state.get_step_status(cn, "SC") == STATUS_ERROR)
            )

            # 3) 清除 sw_macro_started 标志 → 允许用户直接 start 重试
            self.state.set_sw_macro_started(False)

            # 4) 引擎状态切为 stopped，TUI 可显示 "已停止"
            self.state.set_engine_status("stopped")

            # 5) 停止文件监控器（已在 _stopped 置位时由主循环感知）
            #    但显式调用 stop() 避免监控线程无限等待
            if self._file_monitor is not None:
                self._file_monitor.stop()

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
        if not self.state.is_sw_macro_started():
            logger.info("SW 宏尚未启动，准备执行...")
            all_configs = self.state.get_all_configs()

            # 将所有构型的 SW 状态设为 Running（仅限 Waiting/Paused/Error/Retrying 状态）
            for cn in all_configs:
                current_status = self.state.get_step_status(cn, "SW")
                if current_status in (STATUS_WAITING, STATUS_PAUSED, STATUS_ERROR, STATUS_RETRYING):
                    self.state.set_step_status(cn, "SW", STATUS_RUNNING)

            # ---- SW 宏前暂停检查 ----
            if self._paused.is_set():
                logger.info("SW 宏启动前检测到暂停标志，等待继续指令...")
                self.state.set_engine_status("paused")
                while self._paused.is_set() and not self._stopped.is_set():
                    time.sleep(1)
                if self._stopped.is_set():
                    return
                # 恢复后重新标记 SW 为 Running
                for cn in all_configs:
                    if self.state.get_step_status(cn, "SW") == STATUS_PAUSED:
                        self.state.set_step_status(cn, "SW", STATUS_RUNNING)

            # ★ 提前启动文件监控和工作线程池（在 SW 宏执行前启动，
            #    以便在宏逐文件导出 STEP 时实时检测文件写入完成，
            #    实现边导出边处理的并行流水线）
            if self._file_monitor is None or not self._file_monitor._running:
                self._file_monitor = StepFileMonitor(
                    step_dir=None,
                    on_file_ready=self._on_step_file_ready
                )
                self._file_monitor.start()
                logger.info("文件监控已提前启动（在 SW 宏执行前）")
            self._start_worker_pool_if_needed()

            # 启动 SW 宏（批量导出所有构型）—— RunMacro2 是同步阻塞 COM 调用，不可中断
            # 增加重试机制：SW 启动/COM 调用可能因瞬时问题失败
            sw_max_retries = ENGINE_CONFIG.get("sw_max_retries", 1)
            success = False
            for sw_attempt in range(1, int(sw_max_retries) + 1):
                # ★ 检查停止标志
                if self._stopped.is_set():
                    return

                if sw_attempt > 1:
                    # ★ 重试前先等待暂停恢复（若有），然后立即设置重试状态
                    #    注意：不在此处长时间等待暂停，而是在 _pause_aware_sleep 中
                    #    合并等待，以避免重复消耗时间
                    if self._paused.is_set():
                        logger.info("SW 宏重试前检测到暂停标志，将在重试延迟中等待继续...")
                    # 将所有 SW 步骤标记为 Retrying，TUI 可显示 🔄 状态
                    for cn in all_configs:
                        sw_st = self.state.get_step_status(cn, "SW")
                        if sw_st not in (STATUS_COMPLETED, STATUS_PAUSED):
                            self.state.set_step_status(
                                cn, "SW", STATUS_RETRYING,
                                f"SW 宏重试 {sw_attempt}/{sw_max_retries}"
                            )
                    logger.info(
                        f"SW 宏重试 {sw_attempt}/{sw_max_retries}，"
                        f"等待 10 秒并清理残留进程..."
                    )
                    # ★ 使用暂停感知 sleep 替代原始 time.sleep：
                    #    若已暂停，则在此 sleep 期间等待恢复；
                    #    若在 sleep 期间被暂停，状态会被 set_all_running_to_paused() 改为 Paused，
                    #    恢复后 sleep 继续计时
                    if not self._pause_aware_sleep(10):
                        return
                    # ★ kill SW 进程（暂停期间跳过，恢复后继续）
                    try:
                        subprocess.run(
                            ["taskkill", "/f", "/im", "SLDWORKS.exe"],
                            capture_output=True, timeout=30,
                        )
                    except (OSError, subprocess.SubprocessError, subprocess.TimeoutExpired):
                        pass
                    # 等待 SW 进程完全退出后再重试（防止 COM 注册残留）
                    logger.info("等待 SolidWorks 进程完全退出...")
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
                                logger.info("✓ SolidWorks 进程已退出")
                                break
                        except (OSError, subprocess.SubprocessError, subprocess.TimeoutExpired):
                            break
                    # 额外冷却确保 COM 子系统完全释放
                    if not self._pause_aware_sleep(5):
                        return
                    # ★ 重置文件监控器状态，避免上次尝试的已处理文件集合
                    #    导致重试时同名 STEP 文件被跳过（_processed_files 命中）
                    if self._file_monitor is not None:
                        self._file_monitor._processed_files.clear()
                        self._file_monitor._known_files.clear()
                        self._file_monitor._detector._history.clear()
                        self._file_monitor._detector._first_seen.clear()
                        logger.info("文件监控器状态已重置（准备 SW 宏重试）")
                    # 恢复为 Running 后执行宏
                    # ★ 仅将 RETRYING 状态的步骤恢复为 Running（Paused 保持不变）
                    for cn in all_configs:
                        current_sw = self.state.get_step_status(cn, "SW")
                        if current_sw == STATUS_RETRYING:
                            self.state.set_step_status(cn, "SW", STATUS_RUNNING)

                logger.info(
                    f"SW 宏执行 (尝试 {sw_attempt}/{sw_max_retries})..."
                )
                success = self.runner.execute_sw_macro()
                if success:
                    break

            # 若最终仍失败，将仍为 Running/Retrying 的构型标记为 Error
            if not success and not self._paused.is_set():
                for cn in all_configs:
                    sw_st = self.state.get_step_status(cn, "SW")
                    if sw_st not in (STATUS_COMPLETED, STATUS_ERROR, STATUS_PAUSED):
                        self.state.set_step_status(
                            cn, "SW", STATUS_ERROR,
                            f"SW 宏失败（重试 {sw_max_retries} 次后）"
                        )
                self.state.set_engine_status("stopped")
                logger.error("SW 宏启动失败，流水线中止")
                return

            if not success:
                # SW 宏失败且处于暂停状态
                if self._paused.is_set():
                    logger.warning("SW 宏在暂停期间失败，保留 Paused 状态以供恢复后重试")
                    for cn in all_configs:
                        if self.state.get_step_status(cn, "SW") == STATUS_RUNNING:
                            self.state.set_step_status(cn, "SW", STATUS_PAUSED)
                    self.state.set_engine_status("paused")
                return
            else:
                # SW 宏成功执行，但 execute_sw_macro 内部可能已标记部分构型为 Error
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
                            f"暂停标志已置位，已将 {paused_count} 个 SW Error 构型回退为 Paused"
                        )

            # ★ SW 阶段导出汇总（部分成功场景的诊断日志）
            if not self._paused.is_set():
                sw_completed = [cn for cn in all_configs
                                if self.state.get_step_status(cn, "SW") == STATUS_COMPLETED]
                sw_errors = [cn for cn in all_configs
                             if self.state.get_step_status(cn, "SW") == STATUS_ERROR]
                sw_running = [cn for cn in all_configs
                              if self.state.get_step_status(cn, "SW") == STATUS_RUNNING]
                if sw_completed:
                    logger.info(
                        f"SW 阶段完成: {len(sw_completed)}/{len(all_configs)} 个构型 STEP 就绪"
                    )
                if sw_errors:
                    logger.warning(
                        f"SW 阶段部分失败: 构型 {sorted(sw_errors)} STEP 导出失败，"
                        f"将跳过其下游步骤"
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
                        f"SW 阶段: {len(sw_running)} 个构型仍为 Running 状态 "
                        f"(可能导出中断): {sorted(sw_running)}"
                    )
        else:
            logger.info("SW 宏已执行过，跳过（断点续传模式）")
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
                logger.info("检测到 SW Paused 构型，重新校验 STEP 文件...")
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
                            logger.info(f"  构型{cn} ✓ STEP 文件已存在，标记为完成")
                        else:
                            # STEP 仍缺失，保持 Error（需要重跑 SW 宏）
                            self.state.set_step_status(cn, "SW", STATUS_ERROR,
                                                       "暂停恢复后 STEP 文件仍缺失")
                            has_error_sw = True
                            logger.warning(f"  构型{cn} ✗ STEP 文件缺失")

            if has_error_sw:
                # 有 SW Error 构型，清除 sw_macro_started 标志以允许重新运行
                logger.warning(
                    "检测到 SW Error 构型，将清除 sw_macro_started 标志以允许重新执行 SW 宏"
                )
                self.state.set_sw_macro_started(False)
                # 递归调用自身以重新进入 SW 阶段
                self.start_pipeline(_recursion_depth + 1)
                return

        # ---- 步骤 1.5: 预扫描下游输出文件（断点续传） ----
        # 在启动工作线程之前扫描各步骤输出目录，若文件已存在则直接标记 Completed，
        # 避免重复启动 SpaceClaim/传输/网格划分/求解程序。
        self._prescan_downstream_outputs()

        # ---- 步骤 2: 启动文件监控 ----
        if self._file_monitor is None or not self._file_monitor._running:
            self._file_monitor = StepFileMonitor(
                step_dir=None,
                on_file_ready=self._on_step_file_ready
            )
            self._file_monitor.start()

        # ---- 步骤 3: 启动工作线程池（仅在未启动时创建） ----
        self._start_worker_pool_if_needed()

        # ---- 步骤 4: 启动全局屏障监控 ----
        if self._barrier_thread is None or not self._barrier_thread.is_alive():
            self._barrier_thread = threading.Thread(
                target=self._barrier_monitor_loop,
                daemon=True,
                name="BarrierMonitor"
            )
            self._barrier_thread.start()

        # 更新引擎状态：仅在未被暂停时设为 running（pause() 已将其设为 paused）
        if not self._paused.is_set():
            self.state.set_engine_status("running")
        else:
            logger.info("流水线组件已就绪，但暂停标志仍置位，等待继续指令...")

        logger.info("流水线调度器已启动，等待 STEP 文件...")

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

    def _start_worker_pool_if_needed(self):
        """仅在工作线程未启动或全部死亡时创建新的工作线程池。"""
        alive_workers = [t for t in self._worker_threads if t.is_alive()]
        self._worker_threads = alive_workers
        if not alive_workers:
            self._start_worker_pool()
        else:
            logger.debug(f"工作线程池已存在 ({len(alive_workers)} 个活跃线程)，跳过创建")

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
                config_name, step_file = self._sc_queue.get(timeout=1)
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
                # 尝试标记当前未完成的步骤为 Error
                for step in ["SC", "Transfer", "Meshing"]:
                    try:
                        s = self.state.get_step_status(config_name, step)
                        if s not in (STATUS_COMPLETED, STATUS_ERROR, STATUS_PAUSED):
                            self.state.set_step_status(config_name, step, STATUS_ERROR, str(e))
                    except Exception as mark_err:
                        logger.debug(f"标记构型{config_name}步骤{step}为Error时异常: {mark_err}")
            except Exception as e:
                # 最后的兜底：捕获所有其他异常类型，防止工作线程意外崩溃
                logger.critical(
                    f"处理构型{config_name} 时发生致命异常: {type(e).__name__}: {e}",
                    exc_info=True
                )
                for step in ["SC", "Transfer", "Meshing"]:
                    try:
                        s = self.state.get_step_status(config_name, step)
                        if s not in (STATUS_COMPLETED, STATUS_ERROR, STATUS_PAUSED):
                            self.state.set_step_status(config_name, step, STATUS_ERROR,
                                                        f"致命异常: {type(e).__name__}: {e}")
                    except Exception:
                        pass
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
                                                   self.runner.execute_spaceclaim):
                    return
            elif not self._execute_with_retry(config_name, "SC",
                                               self.runner.execute_spaceclaim):
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

        # ---- Meshing 阶段 ----
        meshing_status = self.state.get_step_status(config_name, "Meshing")
        if meshing_status in (STATUS_WAITING, STATUS_RUNNING, STATUS_PAUSED, STATUS_ERROR, STATUS_RETRYING):
            # ★ 执行前检查远程输出：若标志文件或网格文件已存在则跳过
            meshing_skip = False
            try:
                ssh = self.runner.get_ssh()
                if ssh.is_connected():
                    flag_file = (
                        f"{REMOTE_CONFIG['flag_dir'].replace(chr(92), '/')}"
                        f"/meshing_done_{config_name}.txt"
                    )
                    mesh_name = get_step_filename("Meshing", config_name)
                    if ssh.check_remote_file(flag_file):
                        logger.info(
                            f"构型{config_name} Meshing: "
                            f"标志文件已存在，跳过执行"
                        )
                        self.state.set_step_status(
                            config_name, "Meshing", STATUS_COMPLETED
                        )
                        meshing_skip = True
                    elif mesh_name:
                        mesh_file = (
                            f"{REMOTE_CONFIG['msh_dir'].replace(chr(92), '/')}"
                            f"/{mesh_name}"
                        )
                        if ssh.check_remote_file(mesh_file):
                            logger.info(
                                f"构型{config_name} Meshing: "
                                f"网格文件已存在，跳过执行"
                            )
                            self.state.set_step_status(
                                config_name, "Meshing", STATUS_COMPLETED
                            )
                            meshing_skip = True
            except Exception:
                pass

            if not meshing_skip:
                if not self._execute_with_retry(config_name, "Meshing",
                                                 self.runner.execute_meshing):
                    return

                # ★ 启动远程网格划分后检查暂停标志
                while self._paused.is_set() and not self._stopped.is_set():
                    time.sleep(1)
                if self._stopped.is_set():
                    return

                # 启动远程网格划分后，轮询等待完成
                logger.info(f"等待构型{config_name} 网格划分完成...")
                if self.runner.wait_meshing_completion(
                    config_name,
                    paused_event=self._paused,
                    stopped_event=self._stopped,
                ):
                    self.state.set_step_status(config_name, "Meshing", STATUS_COMPLETED)
                    logger.info(f"构型{config_name} 网格划分完成 ✓")
                else:
                    # ★ 区分暂停和真正的超时
                    if self._paused.is_set():
                        self.state.set_step_status(
                            config_name, "Meshing", STATUS_PAUSED,
                            "等待网格划分期间暂停"
                        )
                    elif self._stopped.is_set():
                        self.state.set_step_status(
                            config_name, "Meshing", STATUS_PAUSED,
                            "引擎已停止"
                        )
                    else:
                        self.state.set_step_status(
                            config_name, "Meshing", STATUS_ERROR, "网格划分超时"
                        )
                    return

        logger.info(f"构型{config_name} SC→Transfer→Meshing 全部完成 ✓")

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
                    # （例如 execute_spaceclaim 在轮询中检测到暂停标志，终止了 SC 进程）
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

    # ------------------------------------------------------------------
    # 全局屏障监控
    # ------------------------------------------------------------------

    def _barrier_monitor_loop(self):
        """
        全局屏障监控线程。

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
                time.sleep(1)
                continue

            all_configs = self.state.get_all_configs()

            # ---- 前置检查：SW 阶段是否已全部终结且有错误 ----
            # 若 SW 宏执行完毕但所有构型的 STEP 均缺失，后续流程无法推进。
            sw_all_terminal = True
            sw_has_completed = False
            for cn in all_configs:
                s = self.state.get_step_status(cn, "SW")
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
                    for step in ["SC", "Transfer", "Meshing", "Solver"]:
                        if self.state.get_step_status(cn, step) == STATUS_WAITING:
                            self.state.set_step_status(cn, step, STATUS_ERROR,
                                                       "SW 步骤失败，后续步骤无法执行")
                self._stopped.set()
                self.state.set_engine_status("stopped")
                break

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

            # 检查是否所有 Meshing 均已终结（Completed 或 Error）
            all_configs = self.state.get_all_configs()
            all_terminal = True
            has_error = False
            for cn in all_configs:
                s = self.state.get_step_status(cn, "Meshing")
                if s not in (STATUS_COMPLETED, STATUS_ERROR):
                    all_terminal = False
                    break
                if s == STATUS_ERROR:
                    has_error = True

            if all_terminal and has_error:
                logger.error("=" * 60)
                logger.error(">>> 全局屏障失败！所有构型网格划分均已终结但存在错误 <<<")
                logger.error("=" * 60)
                # 将所有 Meshing=Error 的构型的 Solver 也标记为 Error（屏障未通过）
                for cn in all_configs:
                    if self.state.get_step_status(cn, "Meshing") == STATUS_ERROR:
                        self.state.set_step_status(cn, "Solver", STATUS_ERROR, "网格划分失败，屏障未通过")
                self._stopped.set()
                self.state.set_engine_status("stopped")
                break

            # 检查是否有 Meshing 失败的（仅报告新增的失败，按构型去重）
            error_configs = self.state.get_error_configs()
            meshing_error_configs = {c for c, s, _ in error_configs if s == "Meshing"}
            new_errors = meshing_error_configs - _last_error_report
            if new_errors:
                logger.warning(
                    f"[BarrierMonitor] 检测到 {len(new_errors)} 个新的网格划分失败: "
                    f"{sorted(new_errors)}"
                )
                _last_error_report = meshing_error_configs

            # 轮询间隔：网格划分通常耗时较长，不需要高频检查
            # ★ 使用暂停感知 sleep，避免暂停期间无谓轮询消耗 CPU
            if not self._pause_aware_sleep(5.0):
                break

        logger.info("[BarrierMonitor] 全局屏障监控退出")

    # ------------------------------------------------------------------
    # Solver 调度（屏障通过后）
    # ------------------------------------------------------------------

    def _dispatch_solver_tasks(self):
        """
        全局屏障通过后，统一启动所有构型的仿真求解。

        所有构型的 Solver 在屏障通过后并行启动。
        若暂停标志已置位，则等待恢复后再分发。
        """
        # ★ 分发前检查暂停标志
        while self._paused.is_set() and not self._stopped.is_set():
            time.sleep(1)
        if self._stopped.is_set():
            logger.warning("Solver 分发前检测到停止标志，取消分发")
            return

        logger.info("=" * 60)
        logger.info("开始统一调度仿真求解任务...")
        logger.info("=" * 60)

        all_configs = self.state.get_all_configs()

        # 使用线程池并行启动所有 Solver 任务（不阻塞，求解耗时可能数小时）
        dispatched_count = 0
        for cn in all_configs:
            solver_status = self.state.get_step_status(cn, "Solver")
            if solver_status in (STATUS_WAITING, STATUS_PAUSED, STATUS_ERROR, STATUS_RETRYING):
                t = threading.Thread(
                    target=self._execute_solver_for_config,
                    args=(cn,),
                    name=f"Solver-{cn}",
                    daemon=True,
                )
                t.start()
                self._solver_threads.append(t)
                dispatched_count += 1

        logger.info(f"所有 Solver 任务已分发 ({dispatched_count} 个构型)")

    def _execute_solver_for_config(self, config_name: int):
        """
        执行单个构型的仿真求解（在独立线程中运行）。

        Args:
            config_name: 构型名称
        """
        # ★ 执行前检查暂停标志
        while self._paused.is_set() and not self._stopped.is_set():
            time.sleep(1)
        if self._stopped.is_set():
            return

        logger.info(f"[Solver] 构型{config_name} 开始求解...")

        # 启动远程求解后台任务
        if self._execute_with_retry(config_name, "Solver",
                                     self.runner.execute_solver):
            # ★ 启动远程求解后检查暂停标志
            while self._paused.is_set() and not self._stopped.is_set():
                time.sleep(1)
            if self._stopped.is_set():
                return

            # 轮询等待求解完成
            if self.runner.wait_solver_completion(
                config_name,
                paused_event=self._paused,
                stopped_event=self._stopped,
            ):
                self.state.set_step_status(config_name, "Solver", STATUS_COMPLETED)
                logger.info(f"[Solver] 构型{config_name} 求解完成 ✓")
            else:
                # ★ 区分暂停和真正的超时
                if self._paused.is_set():
                    self.state.set_step_status(
                        config_name, "Solver", STATUS_PAUSED,
                        "等待求解期间暂停"
                    )
                elif self._stopped.is_set():
                    self.state.set_step_status(
                        config_name, "Solver", STATUS_PAUSED,
                        "引擎已停止"
                    )
                else:
                    self.state.set_step_status(
                        config_name, "Solver", STATUS_ERROR, "求解超时"
                    )

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

    def resume(self):
        logger.info("收到继续指令")
        self.state.set_all_paused_to_running()
        self._paused.clear()
        self.state.set_engine_status("running")

        pipeline_needs_init = False
        if self._file_monitor is None or not self._file_monitor._running:
            pipeline_needs_init = True
        else:
            alive_workers = [t for t in self._worker_threads if t.is_alive()]
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
        """初始化 SW 之后的下游流水线组件（文件监控、工作线程、屏障监控）。

        在 resume() 恢复暂停时调用，处理 SW 已完成但 workers 尚未启动的场景。
        所有组件启动前均检查 _stopped 标志，避免在引擎停止时创建新线程。
        """
        if self._stopped.is_set():
            logger.info("引擎已停止，跳过下游组件初始化")
            return

        if self._file_monitor is None or not self._file_monitor._running:
            self._file_monitor = StepFileMonitor(
                step_dir=None,
                on_file_ready=self._on_step_file_ready
            )
            self._file_monitor.start()
            logger.info("文件监控已启动（延迟初始化）")

        if self._stopped.is_set():
            return

        self._start_worker_pool_if_needed()

        if self._stopped.is_set():
            return

        if self._barrier_thread is None or not self._barrier_thread.is_alive():
            self._barrier_thread = threading.Thread(
                target=self._barrier_monitor_loop,
                daemon=True,
                name="BarrierMonitor"
            )
            self._barrier_thread.start()
            logger.info("屏障监控已启动（延迟初始化）")

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
        for t in self._solver_threads:
            if t.is_alive():
                t.join(timeout=3)
        self._solver_threads.clear()

        # 停止文件监控
        if self._file_monitor:
            self._file_monitor.stop()

        # 断开 SSH
        self.runner.disconnect_ssh()

        self.state.set_engine_status("stopped")
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
        elif config_name == "all":
            for cn in self.state.get_all_configs():
                self.state.reset_config_steps(cn, step_name)
            if need_barrier_clear:
                self._barrier_passed.clear()
                self.state.set_global_barrier_met(False)
        else:
            self.state.reset_config_steps(config_name, step_name)
            if need_barrier_clear:
                self._barrier_passed.clear()
                self.state.set_global_barrier_met(False)

        logger.info(f"已重置 config={config_name} step={step_name or 'all'}")

    def reset_all(self):
        """重置所有构型的所有步骤（委托给 reset_config 处理全量逻辑）。"""
        self.reset_config("all", None)
