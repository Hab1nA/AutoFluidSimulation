"""
===============================================================================
远程任务执行器 (Remote Executor)

负责 SCDOC 文件传输（本地→远程）以及远程工作站上的网格划分和仿真求解。

从 engine/task_runner.py 中提取。
===============================================================================
"""
from __future__ import annotations

import hashlib
import json
import os
import time
import threading
from typing import Callable, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from utils.ssh_client import RemoteWorkstation
    from engine.state_manager import StateManager

from engine.config import (
    LOCAL_PATHS, REMOTE_CONFIG, ENGINE_CONFIG,
    OPERATION_TIMEOUTS,
    STATUS_ERROR, get_step_filename, STEP_FILE_PATTERNS,
)
from engine.scheduler.utils import wait_unless_paused_or_stopped
from utils.logger import setup_logger

logger = setup_logger(__name__)

# 远程脚本文件列表（部署到 scripts_dir）
REMOTE_SCRIPT_FILES = [
    "batch_meshing_gen4.py",
    "batch_solver_gen4.py",
    "meshing_gen4.wft",
    "meshing_gen4.jou",
    "solver_gen4.jou",
    "solver_gen4.set",
    "solver_post_gen4.jou",
]

# 远程仿真引用文件列表（部署到 ref_files_dir）
REMOTE_REF_FILES = [
    "chemkin-import_chem.inp",
    "chemkin-import_therm.dat",
    "model_gen4.fla",
    "model_gen4.pdf",
]


class RemoteExecutor:
    """远程任务执行器。

    负责 Transfer（SFTP 上传）、Meshing（远程网格）、Solver（远程求解）。
    共享同一个 SSH 连接。
    """

    def __init__(self, state_manager: StateManager, ssh_getter: Callable[[], "RemoteWorkstation"], ssh_lock: threading.RLock):
        """初始化远程执行器。

        Args:
            state_manager: StateManager 实例
            ssh_getter: 可调用对象，返回 RemoteWorkstation 实例
            ssh_lock: SSH 连接的线程锁
        """
        self.state = state_manager
        self._get_ssh = ssh_getter
        self._ssh_lock = ssh_lock
        self._paused_event: threading.Event | None = None
        self._stopped_event: threading.Event | None = None
        # 跟踪远程后台任务名称（用于超时后终止）
        self._remote_tasks: dict[int, str] = {}  # config_name → task_name

    def set_control_events(
        self,
        paused_event: threading.Event,
        stopped_event: threading.Event,
    ) -> None:
        """注入调度器暂停/停止事件，使 Transfer 上传可响应控制指令。"""
        self._paused_event = paused_event
        self._stopped_event = stopped_event

    def get_ssh_connection(self) -> "RemoteWorkstation":
        """获取 SSH 连接实例（公共接口，供外部模块查询远程文件状态）。"""
        return self._get_ssh()

    # ------------------------------------------------------------------
    # 同步状态持久化
    # ------------------------------------------------------------------

    @property
    def _sync_state_path(self) -> str:
        """同步状态文件路径（记录上次同步的远程目录）。"""
        return os.path.join(str(LOCAL_PATHS["data_dir"]), "last_sync_paths.json")

    def _load_last_sync_paths(self) -> dict[str, str]:
        """加载上次同步时的远程目录路径。

        Returns:
            字典，包含 scripts_dir 和 ref_files_dir 的上次值；
            文件不存在或解析失败返回空字典
        """
        state_file = self._sync_state_path
        if not os.path.exists(state_file):
            return {}
        try:
            with open(state_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return {}
            return {
                str(key): str(value)
                for key, value in data.items()
                if isinstance(key, str) and isinstance(value, str)
            }
        except (OSError, json.JSONDecodeError) as e:
            logger.warning(f"[Sync] 加载同步状态失败: {e}")
            return {}

    def _save_last_sync_paths(self) -> None:
        """将当前远程目录配置保存为下次同步的比对基准。"""
        state = {
            "scripts_dir": str(REMOTE_CONFIG["scripts_dir"]),
            "ref_files_dir": str(REMOTE_CONFIG["ref_files_dir"]),
        }
        try:
            with open(self._sync_state_path, 'w', encoding='utf-8') as f:
                json.dump(state, f, ensure_ascii=False, indent=2)
            logger.debug(f"[Sync] 已保存同步状态: {state}")
        except OSError as e:
            logger.warning(f"[Sync] 保存同步状态失败: {e}")

    def _cleanup_remote_files(self, remote_dir: str, filenames: list[str], label: str) -> None:
        """清理旧远程目录中我们上传过的已知文件。

        逐个删除文件，忽略不存在或删除失败的情况。
        不删除目录本身，避免误伤共享目录中的其他文件。

        Args:
            remote_dir: 旧的远程目录路径
            filenames: 需要清理的文件名列表
            label: 日志标签（如 "脚本"、"引用文件"）
        """
        logger.info(f"[Sync] 检测到{label}远程目录变更，清理旧路径: {remote_dir}")
        with self._ssh_lock:
            try:
                ssh = self._get_ssh()
                for filename in filenames:
                    remote_path = f"{remote_dir}/{filename}".replace("\\", "/")
                    try:
                        ssh.delete_remote_file(remote_path)
                    except Exception as e:
                        logger.debug(f"[Sync] 清理旧文件跳过 {remote_path}: {e}")
            except (OSError, ConnectionError) as e:
                logger.warning(f"[Sync] 清理旧路径文件异常（不影响后续上传）: {e}")

    # ------------------------------------------------------------------
    # 文件传输
    # ------------------------------------------------------------------

    def execute_transfer(self, config_name: int) -> bool:
        """通过 SFTP 将 SCDOC 文件上传到远程工作站。

        内部先检查远程文件是否已存在且大小>0，若已存在则直接返回成功
        （断点续传场景），避免重复上传。
        所有 SSH/SFTP 操作均在 _ssh_lock 保护下执行，保证线程安全。
        """
        _scdoc_name = get_step_filename("sc", config_name)
        if not _scdoc_name:
            logger.error("[Transfer] 无法生成 SCDOC 文件名：STEP_FILE_PATTERNS['sc'] 未配置或格式错误")
            self.state.set_step_status(config_name, "transfer", STATUS_ERROR, "SCDOC 文件名配置错误")
            return False
        local_file = os.path.join(
            str(LOCAL_PATHS["scdoc_dir"]),
            _scdoc_name,
        )
        remote_file = os.path.join(
            str(REMOTE_CONFIG["scdoc_dir"]),
            _scdoc_name,
        ).replace("\\", "/")

        if not os.path.exists(local_file):
            logger.error(f"[Transfer] 本地 SCDOC 文件不存在: {local_file}")
            self.state.set_step_status(config_name, "transfer", STATUS_ERROR, "本地文件不存在")
            return False
        if os.path.getsize(local_file) <= 0:
            logger.error(f"[Transfer] 本地 SCDOC 文件为空: {local_file}")
            self.state.set_step_status(config_name, "transfer", STATUS_ERROR, "本地 SCDOC 文件为空")
            return False

        if self._stopped_event is not None and self._stopped_event.is_set():
            logger.info(f"[Transfer] 构型{config_name} 因停止取消（未开始上传）")
            return False
        if self._paused_event is not None and self._paused_event.is_set():
            logger.info(f"[Transfer] 构型{config_name} 因暂停暂缓（未开始上传）")
            return False

        with self._ssh_lock:
            try:
                ssh = self._get_ssh()

                # ★ 远程文件存在性检查（断点续传）：在锁内执行，保证线程安全。
                #    原检查位于 worker_pool._process_transfer_step() 中且未持有
                #    _ssh_lock，与 upload_file() 并发操作同一 SFTP 通道导致死锁。
                try:
                    remote_size = ssh.get_remote_file_size(remote_file)
                    if remote_size is not None and remote_size > 0:
                        logger.info(
                            f"[Transfer] 远程 SCDOC 已存在 ({remote_size} bytes)，"
                            f"构型{config_name} 跳过上传"
                        )
                        return True
                except Exception as e:
                    # 远程检查失败不影响后续上传流程（可能是临时网络问题）
                    logger.debug(
                        f"[Transfer] 构型{config_name} 远程文件检查异常"
                        f"（将继续上传）: {e}"
                    )

                upload_max_retries = self._upload_max_retries()
                upload_timeout = ENGINE_CONFIG["transfer_timeout"]
                success = ssh.upload_file(
                    local_file,
                    remote_file,
                    max_retries=upload_max_retries,
                    timeout=upload_timeout,
                    paused_event=self._paused_event,
                    stopped_event=self._stopped_event,
                )
                if success:
                    logger.info(f"[Transfer] 文件传输完成: 构型{config_name}")
                    return True
                else:
                    ssh.delete_remote_file(remote_file)
                    if self._stopped_event is not None and self._stopped_event.is_set():
                        logger.info(f"[Transfer] 构型{config_name} 上传因停止中断")
                        return False
                    if self._paused_event is not None and self._paused_event.is_set():
                        logger.info(f"[Transfer] 构型{config_name} 上传因暂停中断")
                        return False
                    self.state.set_step_status(config_name, "transfer", STATUS_ERROR, "SFTP 上传失败")
                    return False
            except (OSError, ConnectionError) as e:
                logger.error(f"[Transfer] 文件传输异常: {e}")
                self.state.set_step_status(config_name, "transfer", STATUS_ERROR, str(e))
                return False

    # ------------------------------------------------------------------
    # 网格划分
    # ------------------------------------------------------------------

    def _upload_max_retries(self) -> int:
        """返回 SFTP 上传重试次数，配置异常时回退到默认值。"""
        default_retries = 3
        raw_retries = OPERATION_TIMEOUTS.get("ssh_upload_max_retries", default_retries)
        try:
            retries = int(raw_retries)
        except (TypeError, ValueError):
            logger.warning(
                f"[Transfer] ssh_upload_max_retries 配置无效: {raw_retries!r}，"
                f"回退为 {default_retries}"
            )
            return default_retries

        if retries < 1:
            logger.warning(
                f"[Transfer] ssh_upload_max_retries 必须 >= 1，当前为 {retries}，"
                f"回退为 {default_retries}"
            )
            return default_retries
        return retries

    def _meshing_processor_count(self) -> int:
        """返回 Fluent Meshing 启动核心数，配置异常时回退到保守默认值。"""
        default_count = 8
        raw_count = ENGINE_CONFIG.get("meshing_processor_count", default_count)
        try:
            processor_count = int(raw_count)
        except (TypeError, ValueError):
            logger.warning(
                f"[Meshing] meshing_processor_count 配置无效: {raw_count!r}，"
                f"使用默认值 {default_count}"
            )
            return default_count
        if processor_count < 1:
            logger.warning(
                f"[Meshing] meshing_processor_count 必须大于 0，"
                f"当前为 {processor_count}，使用默认值 {default_count}"
            )
            return default_count
        return processor_count

    def _build_meshing_command(self, config_name: int) -> tuple[str, str]:
        """构建远程网格划分命令和标志文件路径。

        Args:
            config_name: 构型名称

        Returns:
            (command, flag_file) 元组
        """
        flag_file = f"{REMOTE_CONFIG['flag_dir']}/meshing_done_{config_name}.txt".replace("\\", "/")
        conda_env = REMOTE_CONFIG["conda_env"]
        conda_exe = REMOTE_CONFIG["conda_exe"]
        scripts_dir = REMOTE_CONFIG["scripts_dir"]
        processor_count = self._meshing_processor_count()

        # 构建参数化命令（所有路径均为必需参数，无默认值）
        command = (
            f'"{conda_exe}" run --no-capture-output -n {conda_env} '
            f'python -u "{scripts_dir}/batch_meshing_gen4.py" {config_name}'
            f' --mpi-bin-dir "{REMOTE_CONFIG["mpi_bin_dir"]}"'
            f' --workflow-path "{scripts_dir}/meshing_gen4.wft"'
            f' --journal-path "{scripts_dir}/meshing_gen4.jou"'
            f' --scdoc-dir "{REMOTE_CONFIG["scdoc_dir"]}"'
            f' --output-dir "{REMOTE_CONFIG["msh_dir"]}"'
            f' --working-dir "{REMOTE_CONFIG["working_dir"]}"'
            f' --processor-count {processor_count}'
        )
        return command, flag_file

    def _run_meshing_command(self, config_name: int, log_prefix: str = "[Meshing]") -> bool:
        """启动远程网格划分后台任务（内部方法）。

        统一处理参数校验、脚本同步、命令构建和 SSH 执行。
        由 execute_meshing() 和 start_meshing() 委托调用。

        Args:
            config_name: 构型名称
            log_prefix: 日志前缀（如 "[MeshingMonitor]"）

        Returns:
            True 表示后台任务启动成功
        """
        if not self.sync_scripts():
            logger.error(f"{log_prefix} 远程脚本同步失败，无法启动网格划分")
            return False

        command, flag_file = self._build_meshing_command(config_name)

        logger.info(f"{log_prefix} 启动远程网格划分: 构型{config_name}")
        logger.debug(f"{log_prefix} 远程命令: {command}")

        with self._ssh_lock:
            try:
                ssh = self._get_ssh()
                success, task_name = ssh.exec_background(
                    command, flag_file,
                    working_dir=str(REMOTE_CONFIG["working_dir"]),
                    interactive=True,
                )
                if success:
                    self._remote_tasks[config_name] = task_name
                    logger.info(f"{log_prefix} 网格划分后台任务已启动: 构型{config_name}")
                else:
                    logger.error(f"{log_prefix} 网格划分远程任务启动失败: 构型{config_name}")
                return success
            except (OSError, ConnectionError) as e:
                logger.error(f"{log_prefix} 网格划分启动异常: {e}")
                return False

    def execute_meshing(self, config_name: int) -> bool:
        """在远程工作站启动网格划分后台任务。

        注意：本方法仅返回成功/失败，不设置步骤状态。
        状态由调用方（RetryManager / MeshingMonitor）统一管理。
        """
        if not isinstance(config_name, int):
            logger.error(f"[Meshing] 无效的构型名称类型: {type(config_name).__name__}")
            return False
        return self._run_meshing_command(config_name)

    def start_meshing(self, config_name: int) -> bool:
        """启动远程网格划分后台任务（不设置状态错误，由调用方处理）。

        用于 MeshingMonitor，启动失败时返回 False 由调用方决定重试策略。
        """
        if not isinstance(config_name, int):
            logger.error(f"[Meshing] 无效的构型名称类型: {type(config_name).__name__}")
            return False
        return self._run_meshing_command(config_name)

    def check_meshing_done(self, config_name: int) -> bool:
        """检查网格划分是否已完成（标志文件是否存在）。

        若标志文件存在则清理并返回 True。使用短暂 SSH 锁。
        """
        flag_file = f"{REMOTE_CONFIG['flag_dir']}/meshing_done_{config_name}.txt".replace("\\", "/")
        try:
            with self._ssh_lock:
                ssh = self._get_ssh()
                if ssh.check_remote_file(flag_file):
                    logger.info(f"[Meshing] 构型{config_name} 网格划分完成（检测到标志文件）")
                    ssh.delete_remote_file(flag_file)
                    self._cleanup_completed_remote_task(config_name, "meshing", ssh)
                    return True
            return False
        except (OSError, ConnectionError) as e:
            logger.error(f"[Meshing] 检查网格划分状态异常: {e}")
            return False

    def wait_meshing_completion(
        self, config_name: int,
        paused_event: Optional[threading.Event] = None,
        stopped_event: Optional[threading.Event] = None,
    ) -> bool:
        """轮询等待网格划分完成（逐次短暂持 SSH 锁，不在整个等待期间持锁）。

        暂停期间冻结超时计时器，防止恢复运行后立即触发超时。
        """
        flag_file = f"{REMOTE_CONFIG['flag_dir']}/meshing_done_{config_name}.txt".replace("\\", "/")
        error_flag = f"{flag_file}.error"
        timeout = ENGINE_CONFIG["meshing_timeout"]
        poll_interval = 10
        start_time = time.time()

        logger.info(f"[Meshing] 开始轮询构型{config_name} 网格划分状态 (超时: {timeout}s)")

        while time.time() - start_time < timeout:
            # ★ 使用统一的暂停等待函数，替代手写内联循环
            if paused_event is not None:
                pause_start = time.time()
                if not wait_unless_paused_or_stopped(paused_event, stopped_event or threading.Event()):
                    logger.info(f"[Meshing] 等待构型{config_name} 期间收到停止指令")
                    return False
                # 暂停补偿：将超时计时器向后推移暂停时长
                pause_duration = time.time() - pause_start
                if pause_duration > 0:
                    start_time += pause_duration
            if stopped_event is not None and stopped_event.is_set():
                logger.info(f"[Meshing] 等待构型{config_name} 期间收到停止指令")
                return False

            try:
                with self._ssh_lock:
                    ssh = self._get_ssh()
                    if ssh.check_remote_file(error_flag):
                        logger.error(
                            f"[Meshing] 构型{config_name} 网格划分远程任务执行失败"
                        )
                        ssh.delete_remote_file(error_flag)
                        self._cleanup_completed_remote_task(config_name, "meshing", ssh)
                        return False
                    if ssh.check_remote_file(flag_file):
                        logger.info(f"[Meshing] 构型{config_name} 网格划分完成")
                        ssh.delete_remote_file(flag_file)
                        self._cleanup_completed_remote_task(config_name, "meshing", ssh)
                        return True
            except (OSError, ConnectionError) as e:
                logger.warning(f"[Meshing] 轮询构型{config_name} 异常: {e}")

            time.sleep(poll_interval)

        logger.error(f"[Meshing] 构型{config_name} 网格划分超时 ({timeout}s)")
        # 超时后终止远程进程，防止资源泄漏和重试冲突
        self._kill_remote_task_for_config(config_name, "meshing")
        return False

    # ------------------------------------------------------------------
    # 仿真求解
    # ------------------------------------------------------------------

    def _solver_processor_count(self) -> int:
        """返回 Fluent Solver 启动核心数，配置异常时回退到默认值。"""
        default_count = 128
        raw_count = ENGINE_CONFIG.get("solver_processor_count", default_count)
        try:
            processor_count = int(raw_count)
        except (TypeError, ValueError):
            logger.warning(
                f"[Solver] solver_processor_count 配置无效: {raw_count!r}，"
                f"使用默认值 {default_count}"
            )
            return default_count
        if processor_count < 1:
            logger.warning(
                f"[Solver] solver_processor_count 必须大于 0，"
                f"当前为 {processor_count}，使用默认值 {default_count}"
            )
            return default_count
        return processor_count

    def _build_solver_command(self, config_name: int) -> tuple[str, str]:
        """构建远程仿真求解命令和标志文件路径。

        Args:
            config_name: 构型名称

        Returns:
            (command, flag_file) 元组
        """
        flag_file = f"{REMOTE_CONFIG['flag_dir']}/solver_done_{config_name}.txt".replace("\\", "/")
        conda_env = REMOTE_CONFIG["conda_env"]
        conda_exe = REMOTE_CONFIG["conda_exe"]
        scripts_dir = REMOTE_CONFIG["scripts_dir"]
        processor_count = self._solver_processor_count()

        # 构建参数化命令（所有路径均为必需参数，无默认值）
        # ★ --anim-dir 使用 normpath 消除 .. 相对路径段，确保在 schtasks
        #   默认 CWD (System32) 下也能正确解析
        anim_dir = os.path.normpath(
            os.path.join(str(REMOTE_CONFIG["working_dir"]), "..", "animation")
        )
        command = (
            f'"{conda_exe}" run --no-capture-output -n {conda_env} '
            f'python -u "{scripts_dir}/batch_solver_gen4.py" {config_name}'
            f' --mpi-bin-dir "{REMOTE_CONFIG["mpi_bin_dir"]}"'
            f' --journal-path "{scripts_dir}/solver_gen4.jou"'
            f' --post-journal-path "{scripts_dir}/solver_post_gen4.jou"'
            f' --msh-dir "{REMOTE_CONFIG["msh_dir"]}"'
            f' --output-dir "{REMOTE_CONFIG["result_dir"]}"'
            f' --anim-dir "{anim_dir}"'
            f' --working-dir "{REMOTE_CONFIG["working_dir"]}"'
            f' --working-dir-t "{REMOTE_CONFIG["working_dir"]}/animation-t"'
            f' --working-dir-v "{REMOTE_CONFIG["working_dir"]}/animation-v"'
            f' --processor-count {processor_count}'
        )
        return command, flag_file

    def execute_solver(self, config_name: int) -> bool:
        """在远程工作站启动仿真求解后台任务（全局屏障后调用）。

        注意：本方法仅返回成功/失败，不设置步骤状态。
        状态由调用方（RetryManager / BarrierCoordinator）统一管理。
        """
        # 安全校验：config_name 必须为整数（来自 Excel 构型号），防止命令注入
        if not isinstance(config_name, int):
            logger.error(f"[Solver] 无效的构型名称类型: {type(config_name).__name__}")
            return False

        # 同步远程脚本（仅在文件变更时上传）
        if not self.sync_scripts():
            logger.error("[Solver] 远程脚本同步失败，无法启动仿真求解")
            return False

        command, flag_file = self._build_solver_command(config_name)

        logger.info(f"[Solver] 启动远程仿真求解: 构型{config_name}")
        logger.debug(f"[Solver] 远程命令: {command}")

        with self._ssh_lock:
            try:
                ssh = self._get_ssh()
                success, task_name = ssh.exec_background(
                    command, flag_file,
                    working_dir=str(REMOTE_CONFIG["working_dir"]),
                    interactive=True,
                )
                if success:
                    self._remote_tasks[config_name] = task_name
                    logger.info(f"[Solver] 仿真求解后台任务已启动: 构型{config_name}")
                    return True
                else:
                    logger.error(f"[Solver] 远程求解启动失败: 构型{config_name}")
                    return False
            except (OSError, ConnectionError) as e:
                logger.error(f"[Solver] 仿真求解启动异常: {e}")
                return False

    def wait_solver_completion(
        self, config_name: int,
        paused_event: Optional[threading.Event] = None,
        stopped_event: Optional[threading.Event] = None,
    ) -> bool:
        """轮询等待仿真求解完成（逐次短暂持 SSH 锁）。

        检测到标志文件后，额外验证 .cas.h5 和 .dat.h5 是否都存在。
        若仅存在一个文件，宽限 60s 等待另一个；超时则清理部分文件并返回错误。
        """
        flag_file = f"{REMOTE_CONFIG['flag_dir']}/solver_done_{config_name}.txt".replace("\\", "/")
        error_flag = f"{flag_file}.error"
        result_dir = str(REMOTE_CONFIG["result_dir"]).replace(chr(92), "/")
        cas_name = get_step_filename("solver", config_name)
        dat_name = get_step_filename("solverdata", config_name)
        cas_file = f"{result_dir}/{cas_name}" if cas_name else None
        dat_file = f"{result_dir}/{dat_name}" if dat_name else None

        timeout = ENGINE_CONFIG["solver_timeout"]
        poll_interval = 30
        start_time = time.time()
        file_grace_period = 60
        first_file_seen_time: Optional[float] = None

        logger.info(f"[Solver] 开始轮询构型{config_name} 仿真求解状态 (超时: {timeout}s)")

        while time.time() - start_time < timeout:
            # ---- 暂停/停止响应（统一使用 wait_unless_paused_or_stopped） ----
            if paused_event is not None:
                pause_start = time.time()
                if not wait_unless_paused_or_stopped(paused_event, stopped_event or threading.Event()):
                    return False
                # 暂停补偿：将超时计时器和文件宽限计时器向后推移暂停时长
                pause_duration = time.time() - pause_start
                if pause_duration > 0:
                    start_time += pause_duration
                    if first_file_seen_time is not None:
                        first_file_seen_time += pause_duration
            if stopped_event is not None and stopped_event.is_set():
                return False

            try:
                with self._ssh_lock:
                    ssh = self._get_ssh()
                    if ssh.check_remote_file(error_flag):
                        logger.error(
                            f"[Solver] 构型{config_name} 仿真求解远程任务执行失败"
                        )
                        ssh.delete_remote_file(error_flag)
                        self._cleanup_completed_remote_task(config_name, "solver", ssh)
                        return False
                    if ssh.check_remote_file(flag_file):
                        # 标志文件存在，验证输出文件
                        cas_exists = cas_file is not None and ssh.check_remote_file(cas_file)
                        dat_exists = dat_file is not None and ssh.check_remote_file(dat_file)

                        if cas_exists and dat_exists:
                            ssh.delete_remote_file(flag_file)
                            self._cleanup_completed_remote_task(config_name, "solver", ssh)
                            logger.info(f"[Solver] 构型{config_name} 仿真求解完成（cas+dat 均已保存）")
                            return True

                        # 部分文件缺失
                        if first_file_seen_time is None:
                            first_file_seen_time = time.time()
                            missing = []
                            if not cas_exists:
                                missing.append("cas.h5")
                            if not dat_exists:
                                missing.append("dat.h5")
                            logger.warning(
                                f"[Solver] 构型{config_name}: 标志文件已存在但缺少 "
                                f"{', '.join(missing)}，等待 {file_grace_period}s"
                            )

                        if (first_file_seen_time is not None
                                and time.time() - first_file_seen_time > file_grace_period):
                            missing = []
                            if not cas_exists:
                                missing.append("cas.h5")
                            if not dat_exists:
                                missing.append("dat.h5")
                            logger.error(
                                f"[Solver] 构型{config_name}: {', '.join(missing)} "
                                f"{file_grace_period}s 内未生成，判定为导出错误"
                            )
                            # 清理部分文件 + 标志文件，确保 retry 从干净状态开始
                            if cas_exists and cas_file:
                                ssh.delete_remote_file(cas_file)
                                logger.info(f"[Solver] 构型{config_name}: 已清理部分文件 {cas_file}")
                            if dat_exists and dat_file:
                                ssh.delete_remote_file(dat_file)
                                logger.info(f"[Solver] 构型{config_name}: 已清理部分文件 {dat_file}")
                            ssh.delete_remote_file(flag_file)
                            self._cleanup_completed_remote_task(config_name, "solver", ssh)
                            return False
            except (OSError, ConnectionError) as e:
                logger.warning(f"[Solver] 轮询构型{config_name} 求解状态异常: {e}")

            time.sleep(poll_interval)

        logger.error(f"[Solver] 构型{config_name} 仿真求解超时 ({timeout}s)")
        # 超时后终止远程进程，防止资源泄漏和重试冲突
        self._kill_remote_task_for_config(config_name, "solver")
        return False

    # ------------------------------------------------------------------
    # 远程进程生命周期管理
    # ------------------------------------------------------------------

    def _kill_remote_task_for_config(
        self,
        config_name: int,
        step_name: str,
    ) -> None:
        """超时后终止远程后台任务。

        从 _remote_tasks 中取出任务名称，调用 SSH kill_remote_task 终止。
        无论终止是否成功，都清理跟踪记录。

        Args:
            config_name: 构型编号
            step_name: 步骤名（用于日志）
        """
        task_name = self._remote_tasks.pop(config_name, None)
        if not task_name:
            logger.debug(f"[{step_name}] 构型{config_name} 无远程任务记录，跳过终止")
            return

        with self._ssh_lock:
            try:
                ssh = self._get_ssh()
                ssh.kill_remote_task(task_name)
                logger.info(f"[{step_name}] 构型{config_name} 已请求终止远程任务: {task_name}")
            except (OSError, ConnectionError) as e:
                logger.warning(f"[{step_name}] 构型{config_name} 终止远程任务异常: {e}")

    def _cleanup_completed_remote_task(
        self,
        config_name: int,
        step_name: str,
        ssh: "RemoteWorkstation",
    ) -> None:
        """清理已结束任务的计划任务条目和本地跟踪记录。"""
        task_name = self._remote_tasks.pop(config_name, None)
        if not task_name:
            return
        if ssh.kill_remote_task(task_name):
            logger.info(f"[{step_name}] 构型{config_name} 已清理远程任务条目: {task_name}")
        else:
            logger.warning(f"[{step_name}] 构型{config_name} 清理远程任务条目失败: {task_name}")

    # ------------------------------------------------------------------
    # 脚本同步
    # ------------------------------------------------------------------

    def sync_scripts(self) -> bool:
        """同步远程脚本和引用文件到工作站。

        比较本地和远程文件的 MD5 哈希值，仅在文件变更时上传。
        对于包含占位符的文件，上传时会动态替换路径。

        若检测到远程目录路径发生变更，先清理旧路径中的已知文件，
        再上传到新路径。

        Returns:
            同步成功返回 True，失败返回 False
        """
        local_scripts_dir = LOCAL_PATHS.get("remote_scripts_dir")
        if not local_scripts_dir or not os.path.isdir(local_scripts_dir):
            logger.error(f"[Sync] 本地脚本目录不存在: {local_scripts_dir}")
            return False

        # ---- 路径变更检测：清理旧远程文件 ----
        last_paths = self._load_last_sync_paths()
        current_scripts_dir = str(REMOTE_CONFIG["scripts_dir"])
        current_ref_dir = str(REMOTE_CONFIG["ref_files_dir"])

        last_scripts_dir = last_paths.get("scripts_dir")
        last_ref_dir = last_paths.get("ref_files_dir")

        if last_scripts_dir and last_scripts_dir != current_scripts_dir:
            self._cleanup_remote_files(last_scripts_dir, REMOTE_SCRIPT_FILES, "脚本")
        if last_ref_dir and last_ref_dir != current_ref_dir:
            self._cleanup_remote_files(last_ref_dir, REMOTE_REF_FILES, "引用文件")

        # ---- 正常同步流程 ----

        # 同步脚本文件 → scripts_dir
        if not self._sync_file_group(
            local_dir=local_scripts_dir,
            remote_dir=current_scripts_dir,
            filenames=REMOTE_SCRIPT_FILES,
            label="脚本",
        ):
            return False

        # 同步引用文件 → ref_files_dir
        ref_local_dir = os.path.join(local_scripts_dir, "fluent_chemkin_files")
        if os.path.isdir(ref_local_dir):
            if not self._sync_file_group(
                local_dir=ref_local_dir,
                remote_dir=current_ref_dir,
                filenames=REMOTE_REF_FILES,
                label="引用文件",
            ):
                return False

        # 同步成功 → 记录当前路径供下次比对
        self._save_last_sync_paths()
        return True

    @staticmethod
    def _compute_combined_hash(file_hashes: dict[str, Optional[str]]) -> str:
        """计算文件哈希字典的组合 MD5 哈希值。

        将各文件按名称排序后拼接为 "name:hash|..." 格式，
        再计算 MD5，确保本地与远程计算方式一致。

        Args:
            file_hashes: 文件名到哈希值的映射（值为 None 表示文件缺失）

        Returns:
            组合 MD5 哈希值（小写十六进制字符串）
        """
        combined = "|".join(
            f"{name}:{hash_val or 'MISSING'}"
            for name, hash_val in sorted(file_hashes.items())
        )
        return hashlib.md5(combined.encode('utf-8')).hexdigest()

    def _sync_file_group(self, local_dir: str, remote_dir: str,
                         filenames: list, label: str) -> bool:
        """同步一组文件到远程目录。

        采用两级哈希校验策略：
        1. 第一级：计算本地与远程文件的组合哈希，若一致则所有文件均无需同步
        2. 第二级：组合哈希不一致时，逐文件比对哈希值，仅上传变更的文件

        ★ 锁策略优化：不在整个同步期间持有 _ssh_lock。改为逐文件获取/释放锁，
          避免长时间阻塞 Transfer 等并发 SSH 操作。

        Args:
            local_dir: 本地文件目录
            remote_dir: 远程目标目录
            filenames: 文件名列表
            label: 日志标签（如 "脚本"、"引用文件"）

        Returns:
            同步成功返回 True，失败返回 False
        """
        # ---- 阶段 0: 计算本地文件哈希 ----
        local_hashes = self._calculate_local_hashes(local_dir, filenames)
        if local_hashes is None:
            return False

        # ---- 阶段 1: 第一级校验 —— 组合哈希快速比对（1 次 SSH 调用） ----
        local_combined = self._compute_combined_hash(local_hashes)
        with self._ssh_lock:
            try:
                ssh = self._get_ssh()
                remote_combined = ssh.get_remote_combined_file_hash(
                    remote_dir, filenames
                )
            except (OSError, ConnectionError) as e:
                logger.error(f"[Sync] 获取{label}文件远程组合哈希异常: {e}")
                return False

        if remote_combined is not None and local_combined == remote_combined:
            logger.info(f"[Sync] 所有{label}文件已是最新（组合哈希一致），无需同步")
            return True

        logger.debug(
            f"[Sync] {label}组合哈希不一致"
            f" (本地: {local_combined[:8]}... 远程: {remote_combined[:8] if remote_combined else 'None'}...)，"
            f"进入逐文件比对"
        )

        # ---- 阶段 2: 第二级校验 —— 逐文件比对（N 次 SSH 调用） ----
        with self._ssh_lock:
            try:
                ssh = self._get_ssh()
                remote_hashes = ssh.get_remote_file_hashes(remote_dir, filenames)
            except (OSError, ConnectionError) as e:
                logger.error(f"[Sync] 获取{label}文件远程哈希异常: {e}")
                return False

        # ---- 阶段 3: 比较哈希，确定需上传的文件（无锁） ----
        files_to_upload = []
        for filename in filenames:
            local_hash = local_hashes.get(filename)
            remote_hash = remote_hashes.get(filename)
            if local_hash is None:
                logger.warning(f"[Sync] 本地{label}文件不存在: {filename}")
                continue
            if local_hash != remote_hash:
                files_to_upload.append(filename)
                logger.info(f"[Sync] {label}文件变更: {filename} (本地: {local_hash[:8]}... 远程: {remote_hash[:8] if remote_hash else '不存在'})")

        if not files_to_upload:
            logger.info(f"[Sync] 所有{label}文件已是最新，无需同步")
            return True

        logger.info(f"[Sync] 需要上传 {len(files_to_upload)} 个{label}文件: {', '.join(files_to_upload)}")

        # ---- 阶段 3: 逐文件上传（每文件独立持锁，避免长时间阻塞） ----
        path_aware_exts = {'.jou', '.set', '.wft', '.pdf'}
        for filename in files_to_upload:
            # ★ 上传前检查控制事件
            if self._stopped_event is not None and self._stopped_event.is_set():
                logger.info(f"[Sync] {label}同步因停止指令取消")
                return False
            if self._paused_event is not None and self._paused_event.is_set():
                logger.info(f"[Sync] {label}同步因暂停指令暂缓")
                return False

            local_file = os.path.join(local_dir, filename)
            remote_file = f"{remote_dir}/{filename}".replace("\\", "/")

            # ★ 逐文件获取 SSH 锁，上传完成后立即释放，
            #   允许 Transfer 等操作在文件间插入执行
            with self._ssh_lock:
                try:
                    ssh = self._get_ssh()
                    if os.path.splitext(filename)[1] in path_aware_exts:
                        if not self._upload_text_file_with_path_replacement(
                            ssh, local_file, remote_file,
                            paused_event=self._paused_event,
                            stopped_event=self._stopped_event,
                        ):
                            return False
                    else:
                        if not ssh.upload_file(
                            local_file, remote_file,
                            paused_event=self._paused_event,
                            stopped_event=self._stopped_event,
                        ):
                            logger.error(f"[Sync] 上传{label}文件失败: {filename}")
                            return False
                except (OSError, ConnectionError) as e:
                    logger.error(f"[Sync] 上传{label}文件异常: {filename}: {e}")
                    return False

        logger.info(f"[Sync] {label}同步完成")
        return True

    def _calculate_local_hashes(self, local_dir: str, filenames: list) -> Optional[dict[str, Optional[str]]]:
        """计算本地目录中指定文件的 MD5 哈希值。

        对于包含占位符的文件（.jou/.set/.wft/.pdf），
        计算替换后的哈希值，以便与远程文件哈希正确比较，避免每次都重新上传。

        Args:
            local_dir: 本地目录路径
            filenames: 要计算哈希的文件名列表

        Returns:
            字典，键为文件名，值为 MD5 哈希值；失败返回 None
        """
        path_aware_exts = {'.jou', '.set', '.wft', '.pdf'}
        result: dict[str, Optional[str]] = {}
        try:
            for filename in filenames:
                filepath = os.path.join(local_dir, filename)
                if not os.path.exists(filepath):
                    logger.warning(f"[Sync] 本地文件不存在: {filepath}")
                    result[filename] = None
                    continue

                ext = os.path.splitext(filename)[1]
                if ext in path_aware_exts:
                    # 对包含占位符的文件，计算替换后内容的哈希
                    # ★ 使用 newline='' 保留原始换行符（\r\n），避免 Python
                    #    文本模式的通用换行符转换（\r\n → \n）导致本地哈希与
                    #    远程文件（通过 SFTP 二进制上传）的哈希永久不一致。
                    with open(filepath, 'r', encoding='utf-8', newline='') as f:
                        content = f.read()
                    content = self._apply_placeholders(content)
                    file_hash = hashlib.md5(content.encode('utf-8')).hexdigest()
                else:
                    # 普通文件直接计算原始内容哈希
                    with open(filepath, 'rb') as f:
                        file_hash = hashlib.md5(f.read()).hexdigest()
                result[filename] = file_hash

            return result

        except OSError as e:
            logger.error(f"[Sync] 计算本地文件哈希失败: {e}")
            return None

    def _apply_placeholders(self, content: str) -> str:
        """将模板中的所有占位符替换为实际远程目录值。"""
        def fluent_path(path: object) -> str:
            return str(path).replace("\\", "/")

        scripts_dir = fluent_path(REMOTE_CONFIG["scripts_dir"])
        content = content.replace('{{REMOTE_ROOT}}', scripts_dir)
        content = content.replace('{{REMOTE_SCDOC_DIR}}', fluent_path(REMOTE_CONFIG["scdoc_dir"]))
        content = content.replace('{{REMOTE_WORKING_DIR}}', fluent_path(REMOTE_CONFIG["working_dir"]))
        content = content.replace('{{REMOTE_REF_FILES_DIR}}', fluent_path(REMOTE_CONFIG["ref_files_dir"]))
        content = content.replace('{{REMOTE_MSH_DIR}}', fluent_path(REMOTE_CONFIG["msh_dir"]))
        content = content.replace('{{REMOTE_RESULT_DIR}}', fluent_path(REMOTE_CONFIG["result_dir"]))
        sc_pattern = STEP_FILE_PATTERNS.get("sc", "")
        if sc_pattern:
            content = content.replace('{{SC_FILENAME}}', sc_pattern)
        return content

    def _upload_text_file_with_path_replacement(
        self,
        ssh: "RemoteWorkstation",
        local_file: str,
        remote_file: str,
        paused_event: threading.Event | None = None,
        stopped_event: threading.Event | None = None,
    ) -> bool:
        """上传文本文件，将占位符替换为实际远程目录。

        适用于 .jou、.set、.wft、.pdf 等包含路径引用的配置文件。

        Args:
            ssh: SSH 连接实例
            local_file: 本地文件路径
            remote_file: 远程文件路径
            paused_event: 暂停事件（可选，用于中断上传）
            stopped_event: 停止事件（可选，用于中断上传）

        Returns:
            上传成功返回 True，失败返回 False
        """
        try:
            # ★ 使用 newline='' 保留原始换行符（\r\n），避免 Python
            #    文本模式的通用换行符转换破坏哈希一致性。
            with open(local_file, 'r', encoding='utf-8', newline='') as f:
                content = f.read()

            # 替换所有占位符
            content = self._apply_placeholders(content)

            # ★ 写入临时文件时使用二进制模式，确保内容字节与
            #    _calculate_local_hashes 中 encode('utf-8') 的字节完全一致，
            #    避免文本模式的平台换行符转换（Windows 上 \n → \r\n）导致
            #    上传后的远程文件哈希与本地计算的哈希不匹配。
            temp_file = local_file + '.tmp'
            with open(temp_file, 'wb') as f:
                f.write(content.encode('utf-8'))

            # ★ 上传临时文件（传递控制事件，允许暂停/停止中断大文件上传）
            success = ssh.upload_file(
                temp_file, remote_file,
                paused_event=paused_event,
                stopped_event=stopped_event,
            )

            # 清理临时文件
            try:
                os.remove(temp_file)
            except OSError:
                pass

            if success:
                logger.info(f"[Transfer] 上传配置文件（已替换路径）: {os.path.basename(local_file)}")
            return success

        except (OSError, UnicodeDecodeError) as e:
            logger.error(f"[Transfer] 上传配置文件失败: {e}")
            return False
