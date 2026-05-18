"""
===============================================================================
远程任务执行器 (Remote Executor)

负责 SCDOC 文件传输（本地→远程）以及远程工作站上的网格划分和仿真求解。

从 engine/task_runner.py 中提取。
===============================================================================
"""

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

    def execute_meshing(self, config_name: int) -> bool:
        """在远程工作站启动网格划分后台任务。"""
        # 安全校验：config_name 必须为整数（来自 Excel 构型号），防止命令注入
        if not isinstance(config_name, int):
            logger.error(f"无效的构型名称类型: {type(config_name).__name__}")
            return False
        flag_file = f"{REMOTE_CONFIG['flag_dir']}/meshing_done_{config_name}.txt".replace("\\", "/")

        conda_env = REMOTE_CONFIG["conda_env"]
        conda_exe = REMOTE_CONFIG["conda_exe"]
        meshing_script = REMOTE_CONFIG["meshing_script"]
        command = f'"{conda_exe}" run -n {conda_env} python "{meshing_script}" {config_name}'

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
                    self.state.set_step_status(config_name, "Meshing", STATUS_ERROR, "远程任务启动失败")
                    return False
            except (OSError, ConnectionError) as e:
                logger.error(f"网格划分启动异常: {e}")
                self.state.set_step_status(config_name, "Meshing", STATUS_ERROR, str(e))
                return False

    def start_meshing(self, config_name: int) -> bool:
        """启动远程网格划分后台任务（不设置状态错误，由调用方处理）。

        用于 MeshingMonitor，启动失败时返回 False 由调用方决定重试策略。
        """
        if not isinstance(config_name, int):
            logger.error(f"无效的构型名称类型: {type(config_name).__name__}")
            return False
        flag_file = f"{REMOTE_CONFIG['flag_dir']}/meshing_done_{config_name}.txt".replace("\\", "/")

        conda_env = REMOTE_CONFIG["conda_env"]
        conda_exe = REMOTE_CONFIG["conda_exe"]
        meshing_script = REMOTE_CONFIG["meshing_script"]
        command = f'"{conda_exe}" run -n {conda_env} python "{meshing_script}" {config_name}'

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
        """在远程工作站启动仿真求解后台任务（全局屏障后调用）。"""
        # 安全校验：config_name 必须为整数（来自 Excel 构型号），防止命令注入
        if not isinstance(config_name, int):
            logger.error(f"无效的构型名称类型: {type(config_name).__name__}")
            return False
        flag_file = f"{REMOTE_CONFIG['flag_dir']}/solver_done_{config_name}.txt".replace("\\", "/")

        conda_env = REMOTE_CONFIG["conda_env"]
        conda_exe = REMOTE_CONFIG["conda_exe"]
        solver_script = REMOTE_CONFIG["solver_script"]
        command = f'"{conda_exe}" run -n {conda_env} python "{solver_script}" {config_name}'

        logger.info(f"启动远程仿真求解: 构型{config_name}")

        with self._ssh_lock:
            try:
                ssh = self._get_ssh()
                success = ssh.exec_background(command, flag_file)
                if success:
                    logger.info(f"仿真求解后台任务已启动: 构型{config_name}")
                    return True
                else:
                    self.state.set_step_status(config_name, "Solver", STATUS_ERROR, "远程求解启动失败")
                    return False
            except (OSError, ConnectionError) as e:
                logger.error(f"仿真求解启动异常: {e}")
                self.state.set_step_status(config_name, "Solver", STATUS_ERROR, str(e))
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
