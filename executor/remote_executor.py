"""
===============================================================================
远程任务执行器 (Remote Executor)

负责 SCDOC 文件传输（本地→远程）以及远程工作站上的网格划分和仿真求解。

从 engine/task_runner.py 中提取。
===============================================================================
"""
from __future__ import annotations

import hashlib
import os
import time
import threading
from typing import Callable, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from utils.ssh_client import RemoteWorkstation
    from engine.state_manager import StateManager

from engine.config import (
    LOCAL_PATHS, REMOTE_CONFIG, ENGINE_CONFIG,
    STATUS_ERROR, get_step_filename, STEP_FILE_PATTERNS,
)
from utils.logger import setup_logger

logger = setup_logger(__name__)

# 远程脚本文件列表
REMOTE_SCRIPT_FILES = [
    "batch_meshing_gen4.py",
    "batch_solver_gen4.py",
    "meshing_gen4.wft",
    "meshing_gen4.jou",
    "solver_gen4.jou",
    "solver_gen4.set",
    "solver_post_gen4.jou",
    "fluent_chemkin_files/chemkin-import_chem.inp",
    "fluent_chemkin_files/chemkin-import_therm.dat",
    "fluent_chemkin_files/model_gen4.fla",
    "fluent_chemkin_files/model_gen4.pdf",
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

    def get_ssh_connection(self) -> "RemoteWorkstation":
        """获取 SSH 连接实例（公共接口，供外部模块查询远程文件状态）。"""
        return self._get_ssh()

    # ------------------------------------------------------------------
    # 文件传输
    # ------------------------------------------------------------------

    def execute_transfer(self, config_name: int) -> bool:
        """通过 SFTP 将 SCDOC 文件上传到远程工作站。"""
        _scdoc_name = get_step_filename("SC", config_name)
        if not _scdoc_name:
            logger.error("[Transfer] 无法生成 SCDOC 文件名：STEP_FILE_PATTERNS['SC'] 未配置或格式错误")
            self.state.set_step_status(config_name, "Transfer", STATUS_ERROR, "SCDOC 文件名配置错误")
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
            self.state.set_step_status(config_name, "Transfer", STATUS_ERROR, "本地文件不存在")
            return False

        with self._ssh_lock:
            try:
                ssh = self._get_ssh()
                success = ssh.upload_file(local_file, remote_file)
                if success:
                    logger.info(f"[Transfer] 文件传输完成: 构型{config_name}")
                    return True
                else:
                    self.state.set_step_status(config_name, "Transfer", STATUS_ERROR, "SFTP 上传失败")
                    return False
            except (OSError, ConnectionError) as e:
                logger.error(f"[Transfer] 文件传输异常: {e}")
                self.state.set_step_status(config_name, "Transfer", STATUS_ERROR, str(e))
                return False

    # ------------------------------------------------------------------
    # 网格划分
    # ------------------------------------------------------------------

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

        # 构建参数化命令（所有路径均为必需参数，无默认值）
        command = (
            f'"{conda_exe}" run -n {conda_env} python "{scripts_dir}/batch_meshing_gen4.py" {config_name}'
            f' --mpi-bin-dir "{REMOTE_CONFIG["mpi_bin_dir"]}"'
            f' --workflow-path "{scripts_dir}/meshing_gen4.wft"'
            f' --journal-path "{scripts_dir}/meshing_gen4.jou"'
            f' --scdoc-dir "{REMOTE_CONFIG["scdoc_dir"]}"'
            f' --output-dir "{REMOTE_CONFIG["msh_dir"]}"'
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
                success = ssh.exec_background(command, flag_file)
                if success:
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
        """轮询等待网格划分完成（逐次短暂持 SSH 锁，不在整个等待期间持锁）。"""
        flag_file = f"{REMOTE_CONFIG['flag_dir']}/meshing_done_{config_name}.txt".replace("\\", "/")
        timeout = ENGINE_CONFIG["meshing_timeout"]
        poll_interval = 10
        start_time = time.time()

        logger.info(f"[Meshing] 开始轮询构型{config_name} 网格划分状态 (超时: {timeout}s)")

        while time.time() - start_time < timeout:
            if paused_event is not None:
                while paused_event.is_set():
                    if stopped_event is not None and stopped_event.is_set():
                        logger.info(f"[Meshing] 等待构型{config_name} 期间收到停止指令")
                        return False
                    time.sleep(1)
            if stopped_event is not None and stopped_event.is_set():
                logger.info(f"[Meshing] 等待构型{config_name} 期间收到停止指令")
                return False

            try:
                with self._ssh_lock:
                    ssh = self._get_ssh()
                    if ssh.check_remote_file(flag_file):
                        logger.info(f"[Meshing] 构型{config_name} 网格划分完成")
                        ssh.delete_remote_file(flag_file)
                        return True
            except (OSError, ConnectionError) as e:
                logger.warning(f"[Meshing] 轮询构型{config_name} 异常: {e}")

            time.sleep(poll_interval)

        logger.error(f"[Meshing] 构型{config_name} 网格划分超时 ({timeout}s)")
        return False

    # ------------------------------------------------------------------
    # 仿真求解
    # ------------------------------------------------------------------

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

        # 构建参数化命令（所有路径均为必需参数，无默认值）
        command = (
            f'"{conda_exe}" run -n {conda_env} python "{scripts_dir}/batch_solver_gen4.py" {config_name}'
            f' --mpi-bin-dir "{REMOTE_CONFIG["mpi_bin_dir"]}"'
            f' --journal-path "{scripts_dir}/solver_gen4.jou"'
            f' --post-journal-path "{scripts_dir}/solver_post_gen4.jou"'
            f' --msh-dir "{REMOTE_CONFIG["msh_dir"]}"'
            f' --output-dir "{REMOTE_CONFIG["result_dir"]}"'
            f' --anim-dir "{REMOTE_CONFIG["working_dir"]}/../animation"'
            f' --working-dir-t "{REMOTE_CONFIG["working_dir"]}/animation-t"'
            f' --working-dir-v "{REMOTE_CONFIG["working_dir"]}/animation-v"'
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
                success = ssh.exec_background(command, flag_file)
                if success:
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
        result_dir = str(REMOTE_CONFIG["result_dir"]).replace(chr(92), "/")
        cas_name = get_step_filename("Solver", config_name)
        dat_name = get_step_filename("SolverData", config_name)
        cas_file = f"{result_dir}/{cas_name}" if cas_name else None
        dat_file = f"{result_dir}/{dat_name}" if dat_name else None

        timeout = ENGINE_CONFIG["solver_timeout"]
        poll_interval = 30
        start_time = time.time()
        file_grace_period = 60
        first_file_seen_time: Optional[float] = None

        logger.info(f"[Solver] 开始轮询构型{config_name} 仿真求解状态 (超时: {timeout}s)")

        while time.time() - start_time < timeout:
            # ---- 暂停/停止响应 ----
            if paused_event is not None:
                pause_start = time.time()
                while paused_event.is_set():
                    if stopped_event is not None and stopped_event.is_set():
                        return False
                    time.sleep(1)
                # 暂停补偿：将 first_file_seen_time 向后推移暂停时长
                pause_duration = time.time() - pause_start
                if first_file_seen_time is not None and pause_duration > 0:
                    first_file_seen_time += pause_duration
            if stopped_event is not None and stopped_event.is_set():
                return False

            try:
                with self._ssh_lock:
                    ssh = self._get_ssh()
                    if ssh.check_remote_file(flag_file):
                        # 标志文件存在，验证输出文件
                        cas_exists = cas_file is not None and ssh.check_remote_file(cas_file)
                        dat_exists = dat_file is not None and ssh.check_remote_file(dat_file)

                        if cas_exists and dat_exists:
                            ssh.delete_remote_file(flag_file)
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
                            return False
            except (OSError, ConnectionError) as e:
                logger.warning(f"[Solver] 轮询构型{config_name} 求解状态异常: {e}")

            time.sleep(poll_interval)

        logger.error(f"[Solver] 构型{config_name} 仿真求解超时 ({timeout}s)")
        return False

    # ------------------------------------------------------------------
    # 脚本同步
    # ------------------------------------------------------------------

    def sync_scripts(self) -> bool:
        """同步远程脚本到工作站。

        比较本地和远程脚本的 MD5 哈希值，仅在文件变更时上传。
        对于包含占位符的文件，上传时会动态替换路径。

        Returns:
            同步成功返回 True，失败返回 False
        """
        local_scripts_dir = LOCAL_PATHS.get("remote_scripts_dir")
        if not local_scripts_dir or not os.path.isdir(local_scripts_dir):
            logger.error(f"[Sync] 本地脚本目录不存在: {local_scripts_dir}")
            return False

        scripts_dir = REMOTE_CONFIG["scripts_dir"]

        # 获取本地文件哈希
        local_hashes = self._calculate_local_hashes(local_scripts_dir)
        if local_hashes is None:
            return False

        with self._ssh_lock:
            try:
                ssh = self._get_ssh()

                # 获取远程文件哈希
                remote_hashes = ssh.get_remote_file_hashes(scripts_dir, REMOTE_SCRIPT_FILES)

                # 比较并上传变更的文件
                files_to_upload = []
                for filename in REMOTE_SCRIPT_FILES:
                    local_hash = local_hashes.get(filename)
                    remote_hash = remote_hashes.get(filename)

                    if local_hash is None:
                        logger.warning(f"[Sync] 本地文件不存在: {filename}")
                        continue

                    if local_hash != remote_hash:
                        files_to_upload.append(filename)
                        logger.info(f"[Sync] 文件变更: {filename} (本地: {local_hash[:8]}... 远程: {remote_hash[:8] if remote_hash else '不存在'})")

                if not files_to_upload:
                    logger.info("[Sync] 所有脚本文件已是最新，无需同步")
                    return True

                # 上传变更的文件（对包含硬编码路径的文本文件做动态替换）
                logger.info(f"[Sync] 需要上传 {len(files_to_upload)} 个文件: {', '.join(files_to_upload)}")
                path_aware_exts = {'.jou', '.set', '.wft', '.pdf'}
                for filename in files_to_upload:
                    local_file = os.path.join(local_scripts_dir, filename)
                    remote_file = f"{scripts_dir}/{filename}"

                    # 对包含占位符的文件，上传时动态替换路径
                    if os.path.splitext(filename)[1] in path_aware_exts:
                        if not self._upload_text_file_with_path_replacement(ssh, local_file, remote_file):
                            return False
                    else:
                        if not ssh.upload_file(local_file, remote_file):
                            logger.error(f"[Sync] 上传文件失败: {filename}")
                            return False

                logger.info("[Sync] 脚本同步完成")
                return True

            except (OSError, ConnectionError) as e:
                logger.error(f"[Sync] 脚本同步异常: {e}")
                return False

    def _calculate_local_hashes(self, local_dir: str) -> Optional[dict[str, Optional[str]]]:
        """计算本地目录中脚本文件的 MD5 哈希值。

        对于包含占位符的文件（.jou/.set/.wft/.pdf），
        计算替换后的哈希值，以便与远程文件哈希正确比较，避免每次都重新上传。

        Args:
            local_dir: 本地目录路径

        Returns:
            字典，键为文件名，值为 MD5 哈希值；失败返回 None
        """
        path_aware_exts = {'.jou', '.set', '.wft', '.pdf'}
        result: dict[str, Optional[str]] = {}
        try:
            for filename in REMOTE_SCRIPT_FILES:
                filepath = os.path.join(local_dir, filename)
                if not os.path.exists(filepath):
                    logger.warning(f"[Sync] 本地文件不存在: {filepath}")
                    result[filename] = None
                    continue

                ext = os.path.splitext(filename)[1]
                if ext in path_aware_exts:
                    # 对包含占位符的文件，计算替换后内容的哈希
                    with open(filepath, 'r', encoding='utf-8') as f:
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
        scripts_dir = str(REMOTE_CONFIG["scripts_dir"])
        content = content.replace('{{REMOTE_ROOT}}', scripts_dir)
        content = content.replace('{{REMOTE_SCDOC_DIR}}', str(REMOTE_CONFIG["scdoc_dir"]))
        content = content.replace('{{REMOTE_WORKING_DIR}}', str(REMOTE_CONFIG["working_dir"]))
        content = content.replace('{{REMOTE_REF_FILES_DIR}}', str(REMOTE_CONFIG["ref_files_dir"]))
        content = content.replace('{{REMOTE_MSH_DIR}}', str(REMOTE_CONFIG["msh_dir"]))
        content = content.replace('{{REMOTE_RESULT_DIR}}', str(REMOTE_CONFIG["result_dir"]))
        sc_pattern = STEP_FILE_PATTERNS.get("SC", "")
        if sc_pattern:
            content = content.replace('{{SC_FILENAME}}', sc_pattern)
        return content

    def _upload_text_file_with_path_replacement(
        self,
        ssh: "RemoteWorkstation",
        local_file: str,
        remote_file: str,
    ) -> bool:
        """上传文本文件，将占位符替换为实际远程目录。

        适用于 .jou、.set、.wft、.pdf 等包含路径引用的配置文件。

        Args:
            ssh: SSH 连接实例
            local_file: 本地文件路径
            remote_file: 远程文件路径

        Returns:
            上传成功返回 True，失败返回 False
        """
        try:
            with open(local_file, 'r', encoding='utf-8') as f:
                content = f.read()

            # 替换所有占位符
            content = self._apply_placeholders(content)

            # 写入临时文件
            temp_file = local_file + '.tmp'
            with open(temp_file, 'w', encoding='utf-8') as f:
                f.write(content)

            # 上传临时文件
            success = ssh.upload_file(temp_file, remote_file)

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
