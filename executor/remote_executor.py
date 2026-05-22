"""
===============================================================================
远程任务执行器 (Remote Executor)

负责 SCDOC 文件传输（本地→远程）以及远程工作站上的网格划分和仿真求解。

从 engine/task_runner.py 中提取。
===============================================================================
"""

import hashlib
import os
import time
import threading
from typing import Any, Callable, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from utils.ssh_client import RemoteWorkstation

from engine.config import (
    LOCAL_PATHS, REMOTE_CONFIG, ENGINE_CONFIG,
    STATUS_ERROR, get_step_filename,
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
]


class RemoteExecutor:
    """远程任务执行器。

    负责 Transfer（SFTP 上传）、Meshing（远程网格）、Solver（远程求解）。
    共享同一个 SSH 连接。
    """

    def __init__(self, state_manager: Any, ssh_getter: Callable[[], "RemoteWorkstation"], ssh_lock: threading.RLock):
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
            logger.error("无法生成 SCDOC 文件名：STEP_FILE_PATTERNS['SC'] 未配置或格式错误")
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
            logger.error(f"本地 SCDOC 文件不存在: {local_file}")
            self.state.set_step_status(config_name, "Transfer", STATUS_ERROR, "本地文件不存在")
            return False

        with self._ssh_lock:
            try:
                ssh = self._get_ssh()
                success = ssh.upload_file(local_file, remote_file)
                if success:
                    logger.info(f"文件传输完成: 构型{config_name}")
                    return True
                else:
                    self.state.set_step_status(config_name, "Transfer", STATUS_ERROR, "SFTP 上传失败")
                    return False
            except (OSError, ConnectionError) as e:
                logger.error(f"文件传输异常: {e}")
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
        meshing_script = REMOTE_CONFIG["meshing_script"]
        remote_root = REMOTE_CONFIG["root_dir"]

        # 构建参数化命令
        command = (
            f'"{conda_exe}" run -n {conda_env} python "{meshing_script}" {config_name}'
            f' --remote-root "{remote_root}"'
            f' --workflow-path "{remote_root}/meshing_gen4.wft"'
            f' --journal-path "{remote_root}/meshing_gen4.jou"'
            f' --scdoc-dir "{remote_root}/scdoc"'
            f' --output-dir "{remote_root}/msh"'
        )
        return command, flag_file

    def execute_meshing(self, config_name: int) -> bool:
        """在远程工作站启动网格划分后台任务。

        注意：本方法仅返回成功/失败，不设置步骤状态。
        状态由调用方（RetryManager / MeshingMonitor）统一管理。
        """
        # 安全校验：config_name 必须为整数（来自 Excel 构型号），防止命令注入
        if not isinstance(config_name, int):
            logger.error(f"无效的构型名称类型: {type(config_name).__name__}")
            return False

        # 同步远程脚本（仅在文件变更时上传）
        if not self.sync_scripts():
            logger.error("远程脚本同步失败，无法启动网格划分")
            return False

        command, flag_file = self._build_meshing_command(config_name)

        logger.info(f"启动远程网格划分: 构型{config_name}")
        logger.debug(f"远程命令: {command}")

        with self._ssh_lock:
            try:
                ssh = self._get_ssh()
                success = ssh.exec_background(command, flag_file)
                if success:
                    logger.info(f"网格划分后台任务已启动: 构型{config_name}")
                    return True
                else:
                    logger.error(f"网格划分远程任务启动失败: 构型{config_name}")
                    return False
            except (OSError, ConnectionError) as e:
                logger.error(f"网格划分启动异常: {e}")
                return False

    def start_meshing(self, config_name: int) -> bool:
        """启动远程网格划分后台任务（不设置状态错误，由调用方处理）。

        用于 MeshingMonitor，启动失败时返回 False 由调用方决定重试策略。
        """
        if not isinstance(config_name, int):
            logger.error(f"无效的构型名称类型: {type(config_name).__name__}")
            return False

        # 同步远程脚本（仅在文件变更时上传）
        if not self.sync_scripts():
            logger.error("[MeshingMonitor] 远程脚本同步失败")
            return False

        command, flag_file = self._build_meshing_command(config_name)

        logger.info(f"[MeshingMonitor] 启动远程网格划分: 构型{config_name}")
        logger.debug(f"[MeshingMonitor] 远程命令: {command}")

        with self._ssh_lock:
            try:
                ssh = self._get_ssh()
                return ssh.exec_background(command, flag_file)
            except (OSError, ConnectionError) as e:
                logger.error(f"[MeshingMonitor] 网格划分启动异常: {e}")
                return False

    def check_meshing_done(self, config_name: int) -> bool:
        """检查网格划分是否已完成（标志文件是否存在）。

        若标志文件存在则清理并返回 True。使用短暂 SSH 锁。
        """
        flag_file = f"{REMOTE_CONFIG['flag_dir']}/meshing_done_{config_name}.txt".replace("\\", "/")
        try:
            with self._ssh_lock:
                ssh = self._get_ssh()
                if ssh.check_remote_file(flag_file):
                    logger.info(f"[MeshingMonitor] 构型{config_name} 网格划分完成（检测到标志文件）")
                    ssh.delete_remote_file(flag_file)
                    return True
            return False
        except (OSError, ConnectionError) as e:
            logger.error(f"[MeshingMonitor] 检查网格划分状态异常: {e}")
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

        logger.info(f"[MeshingMonitor] 开始轮询构型{config_name} 网格划分状态 (超时: {timeout}s)")

        while time.time() - start_time < timeout:
            if paused_event is not None:
                while paused_event.is_set():
                    if stopped_event is not None and stopped_event.is_set():
                        logger.info(f"[MeshingMonitor] 等待构型{config_name} 期间收到停止指令")
                        return False
                    time.sleep(1)
            if stopped_event is not None and stopped_event.is_set():
                logger.info(f"[MeshingMonitor] 等待构型{config_name} 期间收到停止指令")
                return False

            try:
                with self._ssh_lock:
                    ssh = self._get_ssh()
                    if ssh.check_remote_file(flag_file):
                        logger.info(f"[MeshingMonitor] 构型{config_name} 网格划分完成")
                        ssh.delete_remote_file(flag_file)
                        return True
            except (OSError, ConnectionError) as e:
                logger.warning(f"[MeshingMonitor] 轮询构型{config_name} 异常: {e}")

            time.sleep(poll_interval)

        logger.error(f"[MeshingMonitor] 构型{config_name} 网格划分超时 ({timeout}s)")
        return False

    # ------------------------------------------------------------------
    # 仿真求解
    # ------------------------------------------------------------------

    def execute_solver(self, config_name: int) -> bool:
        """在远程工作站启动仿真求解后台任务（全局屏障后调用）。

        注意：本方法仅返回成功/失败，不设置步骤状态。
        状态由调用方（RetryManager / BarrierCoordinator）统一管理。
        """
        # 安全校验：config_name 必须为整数（来自 Excel 构型号），防止命令注入
        if not isinstance(config_name, int):
            logger.error(f"无效的构型名称类型: {type(config_name).__name__}")
            return False

        # 同步远程脚本（仅在文件变更时上传）
        if not self.sync_scripts():
            logger.error("远程脚本同步失败，无法启动仿真求解")
            return False

        flag_file = f"{REMOTE_CONFIG['flag_dir']}/solver_done_{config_name}.txt".replace("\\", "/")

        conda_env = REMOTE_CONFIG["conda_env"]
        conda_exe = REMOTE_CONFIG["conda_exe"]
        solver_script = REMOTE_CONFIG["solver_script"]
        remote_root = REMOTE_CONFIG["root_dir"]

        # 构建参数化命令
        command = (
            f'"{conda_exe}" run -n {conda_env} python "{solver_script}" {config_name}'
            f' --remote-root "{remote_root}"'
            f' --journal-path "{remote_root}/solver_gen4.jou"'
            f' --post-journal-path "{remote_root}/solver_post_gen4.jou"'
            f' --msh-dir "{remote_root}/msh"'
            f' --output-dir "{remote_root}/case"'
            f' --anim-dir "{remote_root}/animation"'
            f' --working-dir-t "{remote_root}/workingdir/animation-t"'
            f' --working-dir-v "{remote_root}/workingdir/animation-v"'
        )

        logger.info(f"启动远程仿真求解: 构型{config_name}")

        with self._ssh_lock:
            try:
                ssh = self._get_ssh()
                success = ssh.exec_background(command, flag_file)
                if success:
                    logger.info(f"仿真求解后台任务已启动: 构型{config_name}")
                    return True
                else:
                    logger.error(f"远程求解启动失败: 构型{config_name}")
                    return False
            except (OSError, ConnectionError) as e:
                logger.error(f"仿真求解启动异常: {e}")
                return False

    def wait_solver_completion(
        self, config_name: int,
        paused_event: Optional[threading.Event] = None,
        stopped_event: Optional[threading.Event] = None,
    ) -> bool:
        """轮询等待仿真求解完成（逐次短暂持 SSH 锁）。"""
        flag_file = f"{REMOTE_CONFIG['flag_dir']}/solver_done_{config_name}.txt".replace("\\", "/")
        timeout = ENGINE_CONFIG["solver_timeout"]
        poll_interval = 30
        start_time = time.time()

        logger.info(f"开始轮询构型{config_name} 仿真求解状态 (超时: {timeout}s)")

        while time.time() - start_time < timeout:
            if paused_event is not None:
                while paused_event.is_set():
                    if stopped_event is not None and stopped_event.is_set():
                        return False
                    time.sleep(1)
            if stopped_event is not None and stopped_event.is_set():
                return False

            try:
                with self._ssh_lock:
                    ssh = self._get_ssh()
                    if ssh.check_remote_file(flag_file):
                        logger.info(f"构型{config_name} 仿真求解完成")
                        ssh.delete_remote_file(flag_file)
                        return True
            except (OSError, ConnectionError) as e:
                logger.warning(f"轮询构型{config_name} 求解状态异常: {e}")

            time.sleep(poll_interval)

        logger.error(f"构型{config_name} 仿真求解超时 ({timeout}s)")
        return False

    # ------------------------------------------------------------------
    # 脚本同步
    # ------------------------------------------------------------------

    def sync_scripts(self) -> bool:
        """同步远程脚本到工作站。

        比较本地和远程脚本的 MD5 哈希值，仅在文件变更时上传。
        对于 .jou 文件，上传时会动态替换其中的路径。

        Returns:
            同步成功返回 True，失败返回 False
        """
        local_scripts_dir = LOCAL_PATHS.get("remote_scripts_dir")
        if not local_scripts_dir or not os.path.isdir(local_scripts_dir):
            logger.error(f"本地脚本目录不存在: {local_scripts_dir}")
            return False

        remote_root = REMOTE_CONFIG["root_dir"]

        # 获取本地文件哈希
        local_hashes = self._calculate_local_hashes(local_scripts_dir)
        if local_hashes is None:
            return False

        with self._ssh_lock:
            try:
                ssh = self._get_ssh()

                # 获取远程文件哈希
                remote_hashes = ssh.get_remote_file_hashes(remote_root, REMOTE_SCRIPT_FILES)

                # 比较并上传变更的文件
                files_to_upload = []
                for filename in REMOTE_SCRIPT_FILES:
                    local_hash = local_hashes.get(filename)
                    remote_hash = remote_hashes.get(filename)

                    if local_hash is None:
                        logger.warning(f"本地文件不存在: {filename}")
                        continue

                    if local_hash != remote_hash:
                        files_to_upload.append(filename)
                        logger.info(f"文件变更: {filename} (本地: {local_hash[:8]}... 远程: {remote_hash[:8] if remote_hash else '不存在'})")

                if not files_to_upload:
                    logger.info("所有脚本文件已是最新，无需同步")
                    return True

                # 上传变更的文件
                logger.info(f"需要上传 {len(files_to_upload)} 个文件: {', '.join(files_to_upload)}")
                for filename in files_to_upload:
                    local_file = os.path.join(local_scripts_dir, filename)
                    remote_file = f"{remote_root}/{filename}"

                    # 对于 .jou 文件，需要动态替换路径
                    if filename.endswith('.jou'):
                        if not self._upload_jou_file_with_path_replacement(ssh, local_file, remote_file, remote_root):
                            return False
                    else:
                        if not ssh.upload_file(local_file, remote_file):
                            logger.error(f"上传文件失败: {filename}")
                            return False

                logger.info("脚本同步完成")
                return True

            except (OSError, ConnectionError) as e:
                logger.error(f"脚本同步异常: {e}")
                return False

    def _calculate_local_hashes(self, local_dir: str) -> Optional[dict]:
        """计算本地目录中脚本文件的 MD5 哈希值。

        Args:
            local_dir: 本地目录路径

        Returns:
            字典，键为文件名，值为 MD5 哈希值；失败返回 None
        """
        result = {}
        try:
            for filename in REMOTE_SCRIPT_FILES:
                filepath = os.path.join(local_dir, filename)
                if not os.path.exists(filepath):
                    logger.warning(f"本地文件不存在: {filepath}")
                    result[filename] = None
                    continue

                # 计算文件 MD5
                with open(filepath, 'rb') as f:
                    file_hash = hashlib.md5(f.read()).hexdigest()
                result[filename] = file_hash

            return result

        except OSError as e:
            logger.error(f"计算本地文件哈希失败: {e}")
            return None

    def _upload_jou_file_with_path_replacement(
        self,
        ssh: "RemoteWorkstation",
        local_file: str,
        remote_file: str,
        remote_root: str
    ) -> bool:
        """上传 .jou 文件，动态替换其中的路径。

        Args:
            ssh: SSH 连接实例
            local_file: 本地文件路径
            remote_file: 远程文件路径
            remote_root: 远程根目录

        Returns:
            上传成功返回 True，失败返回 False
        """
        try:
            # 读取本地 .jou 文件内容
            with open(local_file, 'r', encoding='utf-8') as f:
                content = f.read()

            # 替换路径：将 D:\xkz_1020 替换为实际的远程根目录
            # 处理不同格式的路径引用（双反斜杠、单反斜杠、正斜杠）
            # 注意：.jou 文件中使用双反斜杠格式，如 D:\\xkz_1020
            remote_root_escaped = remote_root.replace('\\', '\\\\')
            content = content.replace('D:\\\\xkz_1020', remote_root_escaped)
            content = content.replace(r'D:\xkz_1020', remote_root)
            content = content.replace(r'D:/xkz_1020', remote_root)

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
                logger.info(f"上传 .jou 文件（已替换路径）: {os.path.basename(local_file)}")
            return success

        except (OSError, UnicodeDecodeError) as e:
            logger.error(f"上传 .jou 文件失败: {e}")
            return False
