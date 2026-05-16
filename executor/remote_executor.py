"""
===============================================================================
远程任务执行器 (Remote Executor)

负责 SCDOC 文件传输（本地→远程）以及远程工作站上的网格划分和仿真求解。

从 engine/task_runner.py 中提取。
===============================================================================
"""

import os
import threading
from typing import Optional

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

    def __init__(self, state_manager, ssh_getter, ssh_lock: threading.RLock):
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
        command = f'call "{conda_exe}" activate {conda_env} && python "{meshing_script}" {config_name}'

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

    def wait_meshing_completion(
        self, config_name: int,
        paused_event: Optional[threading.Event] = None,
        stopped_event: Optional[threading.Event] = None,
    ) -> bool:
        """轮询等待网格划分完成。"""
        flag_file = f"{REMOTE_CONFIG['flag_dir']}/meshing_done_{config_name}.txt".replace("\\", "/")

        with self._ssh_lock:
            try:
                ssh = self._get_ssh()
                success = ssh.wait_for_flag(
                    flag_file,
                    timeout=ENGINE_CONFIG["meshing_timeout"],  # type: ignore[arg-type]
                    poll_interval=10,
                    paused_event=paused_event,
                    stopped_event=stopped_event,
                )
                return success
            except (OSError, ConnectionError) as e:
                logger.error(f"等待网格划分异常: {e}")
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
        command = f'call "{conda_exe}" activate {conda_env} && python "{solver_script}" {config_name}'

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
        """轮询等待仿真求解完成。"""
        flag_file = f"{REMOTE_CONFIG['flag_dir']}/solver_done_{config_name}.txt".replace("\\", "/")

        with self._ssh_lock:
            try:
                ssh = self._get_ssh()
                success = ssh.wait_for_flag(
                    flag_file,
                    timeout=ENGINE_CONFIG["solver_timeout"],  # type: ignore[arg-type]
                    poll_interval=30,
                    paused_event=paused_event,
                    stopped_event=stopped_event,
                )
                return success
            except (OSError, ConnectionError) as e:
                logger.error(f"等待仿真求解异常: {e}")
                return False
