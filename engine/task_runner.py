"""
===============================================================================
任务执行器 (Task Runner)
负责执行每个阶段的具体操作：

1. SW 阶段：通过 win32com 唤醒 SolidWorks，执行 Macro1.swp 宏
2. SC 阶段：通过 subprocess 无头调用 SpaceClaim
3. Transfer 阶段：通过 paramiko SSH 上传 scdoc 文件
4. Meshing 阶段：通过 SSH 远程启动网格划分后台任务
5. Solver 阶段：通过 SSH 远程启动仿真求解后台任务（全局屏障后）

每个任务执行后会更新 StateManager 中的状态。
===============================================================================
"""
import os
import subprocess
import time
import threading
from typing import Optional, Callable

from engine.config import (
    LOCAL_PATHS, REMOTE_CONFIG, ENGINE_CONFIG,
    STATUS_WAITING, STATUS_RUNNING, STATUS_COMPLETED, STATUS_ERROR, STATUS_RETRYING,
    STEP_NAMES,
)
from utils.logger import setup_logger
from utils.ssh_client import RemoteWorkstation

logger = setup_logger(__name__)


class TaskRunner:
    """
    任务执行器。

    封装了流水线中每个步骤的执行逻辑。
    每个 execute_* 方法负责执行一个步骤并返回成功/失败。
    """

    def __init__(self, state_manager):
        """
        初始化任务执行器。

        Args:
            state_manager: StateManager 实例，用于读写任务状态
        """
        self.state = state_manager
        self._ssh: Optional[RemoteWorkstation] = None
        self._ssh_lock = threading.RLock()  # 可重入锁：SSH 操作需串行化，get_ssh() 内部也需加锁

    # ------------------------------------------------------------------
    # SSH 连接管理
    # ------------------------------------------------------------------

    def get_ssh(self) -> RemoteWorkstation:
        """
        获取（或创建）SSH 客户端实例。

        线程安全：使用 _ssh_lock 防止多线程同时创建连接。
        始终返回 RemoteWorkstation 实例（即使连接失败），
        调用者需通过 is_connected() 检查连接状态。
        """
        with self._ssh_lock:
            if self._ssh is None:
                self._ssh = RemoteWorkstation(
                    host=REMOTE_CONFIG["host"],
                    port=REMOTE_CONFIG["port"],
                    username=REMOTE_CONFIG["username"],
                    password=REMOTE_CONFIG["password"],
                )
            # 如果连接断开则尝试重连
            if not self._ssh.is_connected():
                if not self._ssh.connect():
                    logger.error("SSH 重连失败")
            return self._ssh

    def disconnect_ssh(self):
        """断开 SSH 连接。"""
        if self._ssh:
            self._ssh.disconnect()
            self._ssh = None

    # ------------------------------------------------------------------
    # 阶段 2: SolidWorks 宏执行
    # ------------------------------------------------------------------

    def execute_sw_macro(self) -> bool:
        """
        执行 SolidWorks 宏 (Macro1.swp)。

        SW 宏会一次性批量导出所有构型的 STEP 文件到 step_dir。
        因此整个流水线只需要调用一次此方法。

        执行策略：
        1. 通过 win32com 获取或启动 SolidWorks 应用
        2. 打开初始模型文件
        3. 运行指定的宏
        4. 宏在后台运行，导出所有构型

        Returns:
            True 表示宏启动成功（注意：宏在 SW 内部异步运行）
        """
        logger.info("=" * 60)
        logger.info("启动 SolidWorks 宏执行")
        logger.info("=" * 60)

        sw_model = LOCAL_PATHS["sw_model"]
        sw_macro = LOCAL_PATHS["sw_macro"]

        # 检查必要文件
        if not os.path.exists(sw_model):
            logger.error(f"SW 模型文件不存在: {sw_model}")
            return False
        if not os.path.exists(sw_macro):
            logger.error(f"SW 宏文件不存在: {sw_macro}")
            return False

        try:
            # 使用 win32com 连接 SolidWorks
            import win32com.client
            import pythoncom

            # 初始化 COM
            pythoncom.CoInitialize()

            try:
                logger.info("正在连接 SolidWorks...")
                try:
                    sw_app = win32com.client.GetActiveObject("SldWorks.Application")
                    logger.info("已连接到运行中的 SolidWorks 实例")
                except (OSError, AttributeError, RuntimeError):
                    # COM 未注册或 SW 未运行，尝试启动新实例
                    logger.info("SolidWorks 未运行，正在启动...")
                    sw_app = win32com.client.Dispatch("SldWorks.Application")
                    sw_app.Visible = True  # 设为可见以便调试
                    logger.info("SolidWorks 已启动")

                # 打开模型文件
                logger.info(f"正在打开模型: {sw_model}")
                sw_app.OpenDoc2(sw_model, 1)  # 1 = swDocPART

                # 运行宏
                logger.info(f"正在执行宏: {sw_macro}")
                # RunMacro2 参数: 宏路径, 模块名, 过程名
                # Macro1.swp 是 VBA 宏，通常主过程在模块的 main() 中
                sw_app.RunMacro2(sw_macro, "Macro1", "main")

                logger.info("SW 宏已启动执行（后台批量导出中...）")
                self.state.set_sw_macro_started(True)

                # 注意：不关闭 SW，让宏在后台运行
                # COM 对象会在宏执行完毕后由用户手动或超时机制处理

                return True
            finally:
                pythoncom.CoUninitialize()

        except ImportError:
            logger.error("win32com 未安装，请执行: pip install pywin32")
            return False
        except (OSError, ValueError, RuntimeError) as e:
            logger.error(f"SW 宏执行失败: {e}", exc_info=True)
            # COM 异常类型(pywintypes.com_error)在 pythoncom 导入失败时不可用，
            # 但 pywin32 安装后 COM 错误通常是 OSError 的子类
            return False

    # ------------------------------------------------------------------
    # 阶段 3: SpaceClaim 脚本执行
    # ------------------------------------------------------------------

    def execute_spaceclaim(self, config_name: int) -> bool:
        """
        通过 subprocess 无头调用 SpaceClaim，将 STEP 转换为 SCDOC。

        调用格式（严格按需求）：
        SpaceClaim.exe /RunScript="<脚本路径>" /ScriptArgs="<构型名称>"

        Args:
            config_name: 构型名称（整数）

        Returns:
            True 表示 SC 脚本执行成功
        """
        step_file = os.path.join(
            LOCAL_PATHS["step_dir"],
            f"model_gen4_{config_name}.step"
        )
        scdoc_file = os.path.join(
            LOCAL_PATHS["scdoc_dir"],
            f"model_gen4_{config_name}.scdoc"
        )

        # 检查输入文件
        if not os.path.exists(step_file):
            logger.error(f"STEP 文件不存在: {step_file}")
            self.state.set_step_status(config_name, "SC", STATUS_ERROR, "STEP 文件不存在")
            return False

        sc_exe = LOCAL_PATHS["sc_exe"]
        sc_script = LOCAL_PATHS["sc_script"]

        if not os.path.exists(sc_exe):
            logger.error(f"SpaceClaim 可执行文件不存在: {sc_exe}")
            self.state.set_step_status(config_name, "SC", STATUS_ERROR, "SC 程序不存在")
            return False
        if not os.path.exists(sc_script):
            logger.error(f"SC 脚本文件不存在: {sc_script}")
            self.state.set_step_status(config_name, "SC", STATUS_ERROR, "SC 脚本不存在")
            return False

        # 构建命令行（严格按需求格式）
        cmd = [
            sc_exe,
            f'/RunScript="{sc_script}"',
            f'/ScriptArgs="{config_name}"',
        ]

        logger.info(f"SpaceClaim 启动: 构型{config_name}")
        logger.debug(f"命令: {' '.join(cmd)}")

        try:
            # 使用 subprocess 启动 SpaceClaim（无头模式）
            # SpaceClaim 在 /RunScript 模式下会自动在脚本执行完毕后退出
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )

            # 等待完成（带超时）
            timeout = ENGINE_CONFIG["sc_timeout"]
            try:
                stdout, stderr = process.communicate(timeout=timeout)
                if process.returncode != 0:
                    logger.error(f"SC 脚本执行失败 (exit={process.returncode}): {stderr}")
                    self.state.set_step_status(
                        config_name, "SC", STATUS_ERROR,
                        f"SC 退出码={process.returncode}: {stderr[:200]}"
                    )
                    return False
            except subprocess.TimeoutExpired:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass  # 进程已自行退出
                except OSError as e:
                    logger.error(f"无法终止 SC 进程: {e}")
                try:
                    process.communicate(timeout=5)  # 回收子进程资源，避免僵尸进程
                except subprocess.TimeoutExpired:
                    logger.warning("SC 进程在 kill 后未及时退出")
                logger.error(f"SC 脚本执行超时 ({timeout}s)")
                self.state.set_step_status(config_name, "SC", STATUS_ERROR, "SC 执行超时")
                return False

            # 验证输出文件
            if os.path.exists(scdoc_file):
                logger.info(f"SC 转换完成: model_gen4_{config_name}.scdoc")
                return True
            else:
                logger.error(f"SC 输出文件未生成: {scdoc_file}")
                self.state.set_step_status(config_name, "SC", STATUS_ERROR, "SCDOC 文件未生成")
                return False

        except (OSError, ValueError, RuntimeError) as e:
            logger.error(f"SC 执行异常: {e}")
            self.state.set_step_status(config_name, "SC", STATUS_ERROR, str(e))
            return False

    # ------------------------------------------------------------------
    # 阶段 4: 文件传输 (SCDOC -> 远程工作站)
    # ------------------------------------------------------------------

    def execute_transfer(self, config_name: int) -> bool:
        """
        通过 SFTP 将 SCDOC 文件上传到远程工作站。

        Args:
            config_name: 构型名称

        Returns:
            True 表示传输成功
        """
        local_file = os.path.join(
            LOCAL_PATHS["scdoc_dir"],
            f"model_gen4_{config_name}.scdoc"
        )
        remote_file = os.path.join(
            REMOTE_CONFIG["scdoc_dir"],
            f"model_gen4_{config_name}.scdoc"
        ).replace("\\", "/")

        if not os.path.exists(local_file):
            logger.error(f"本地 SCDOC 文件不存在: {local_file}")
            self.state.set_step_status(config_name, "Transfer", STATUS_ERROR, "本地文件不存在")
            return False

        with self._ssh_lock:
            try:
                ssh = self.get_ssh()
                success = ssh.upload_file(local_file, remote_file)
                if success:
                    logger.info(f"文件传输完成: 构型{config_name}")
                    return True
                else:
                    self.state.set_step_status(config_name, "Transfer", STATUS_ERROR, "SFTP 上传失败")
                    return False
            except (OSError, IOError, ConnectionError, EOFError) as e:
                logger.error(f"文件传输异常: {e}")
                self.state.set_step_status(config_name, "Transfer", STATUS_ERROR, str(e))
                return False

    # ------------------------------------------------------------------
    # 阶段 5: 网格划分 (远程后台任务)
    # ------------------------------------------------------------------

    def execute_meshing(self, config_name: int) -> bool:
        """
        在远程工作站启动网格划分后台任务。

        通过 SSH 触发，使用 PowerShell Start-Process 确保 SSH 断开后进程存活。
        命令格式: python batch_meshing_gen4.py XX

        Args:
            config_name: 构型名称

        Returns:
            True 表示后台任务启动成功（不等待完成）
        """
        flag_file = f"{REMOTE_CONFIG['flag_dir']}/meshing_done_{config_name}.txt".replace("\\", "/")

        # 构建远程命令：激活 conda 环境后执行网格脚本
        conda_env = REMOTE_CONFIG["conda_env"]
        meshing_script = REMOTE_CONFIG["meshing_script"]
        command = f'conda activate {conda_env} && python "{meshing_script}" {config_name}'

        logger.info(f"启动远程网格划分: 构型{config_name}")
        logger.debug(f"远程命令: {command}")

        with self._ssh_lock:
            try:
                ssh = self.get_ssh()
                success = ssh.exec_background(command, flag_file)
                if success:
                    logger.info(f"网格划分后台任务已启动: 构型{config_name}")
                    # 启动后不等待，由调度器轮询标志文件
                    return True
                else:
                    self.state.set_step_status(config_name, "Meshing", STATUS_ERROR, "远程任务启动失败")
                    return False
            except (OSError, IOError, ConnectionError, EOFError) as e:
                logger.error(f"网格划分启动异常: {e}")
                self.state.set_step_status(config_name, "Meshing", STATUS_ERROR, str(e))
                return False

    def wait_meshing_completion(self, config_name: int) -> bool:
        """
        轮询等待网格划分完成。

        Args:
            config_name: 构型名称

        Returns:
            True 表示网格划分成功完成
        """
        flag_file = f"{REMOTE_CONFIG['flag_dir']}/meshing_done_{config_name}.txt".replace("\\", "/")

        with self._ssh_lock:
            try:
                ssh = self.get_ssh()
                success = ssh.wait_for_flag(
                    flag_file,
                    timeout=ENGINE_CONFIG["meshing_timeout"],
                    poll_interval=10
                )
                return success
            except (OSError, IOError, ConnectionError, EOFError) as e:
                logger.error(f"等待网格划分异常: {e}")
                return False

    # ------------------------------------------------------------------
    # 阶段 6: 仿真求解 (全局屏障后，远程后台任务)
    # ------------------------------------------------------------------

    def execute_solver(self, config_name: int) -> bool:
        """
        在远程工作站启动仿真求解后台任务。

        仅在全局屏障通过后调用。
        命令格式: python batch_solver_gen4.py XX

        Args:
            config_name: 构型名称

        Returns:
            True 表示后台任务启动成功
        """
        flag_file = f"{REMOTE_CONFIG['flag_dir']}/solver_done_{config_name}.txt".replace("\\", "/")

        conda_env = REMOTE_CONFIG["conda_env"]
        solver_script = REMOTE_CONFIG["solver_script"]
        command = f'conda activate {conda_env} && python "{solver_script}" {config_name}'

        logger.info(f"启动远程仿真求解: 构型{config_name}")

        with self._ssh_lock:
            try:
                ssh = self.get_ssh()
                success = ssh.exec_background(command, flag_file)
                if success:
                    logger.info(f"仿真求解后台任务已启动: 构型{config_name}")
                    return True
                else:
                    self.state.set_step_status(config_name, "Solver", STATUS_ERROR, "远程求解启动失败")
                    return False
            except (OSError, IOError, ConnectionError, EOFError) as e:
                logger.error(f"仿真求解启动异常: {e}")
                self.state.set_step_status(config_name, "Solver", STATUS_ERROR, str(e))
                return False

    def wait_solver_completion(self, config_name: int) -> bool:
        """轮询等待仿真求解完成。"""
        flag_file = f"{REMOTE_CONFIG['flag_dir']}/solver_done_{config_name}.txt".replace("\\", "/")

        with self._ssh_lock:
            try:
                ssh = self.get_ssh()
                success = ssh.wait_for_flag(
                    flag_file,
                    timeout=ENGINE_CONFIG["solver_timeout"],
                    poll_interval=30  # 求解时间较长，轮询间隔加大
                )
                return success
            except (OSError, IOError, ConnectionError, EOFError) as e:
                logger.error(f"等待仿真求解异常: {e}")
                return False

    # ------------------------------------------------------------------
    # 系统自检
    # ------------------------------------------------------------------

    def run_system_check(self) -> dict:
        """
        执行系统自检：检查本地路径、SSH 连通性、远程进程状态。

        Returns:
            自检结果字典
        """
        results = {
            "local_checks": {},
            "remote_checks": {},
        }

        # ---- 本地检查 ----
        checks = {
            "SW模型": LOCAL_PATHS["sw_model"],
            "Excel参数表": LOCAL_PATHS["excel"],
            "SW宏文件": LOCAL_PATHS["sw_macro"],
            "STEP目录": LOCAL_PATHS["step_dir"],
            "SC程序": LOCAL_PATHS["sc_exe"],
            "SC脚本": LOCAL_PATHS["sc_script"],
            "SCDOC目录": LOCAL_PATHS["scdoc_dir"],
            "日志目录": LOCAL_PATHS["log_dir"],
        }
        for name, path in checks.items():
            exists = os.path.exists(path)
            results["local_checks"][name] = {
                "path": path,
                "exists": exists,
            }

        # ---- 远程检查 ----
        try:
            ssh = self.get_ssh()
            if ssh.is_connected():
                results["remote_checks"]["ssh"] = "连接成功"
                remote_info = ssh.check_system()
                results["remote_checks"].update(remote_info)
            else:
                results["remote_checks"]["ssh"] = "连接失败"
        except (OSError, IOError, ConnectionError) as e:
            results["remote_checks"]["ssh"] = f"错误: {e}"

        return results

    # ------------------------------------------------------------------
    # 文件清理
    # ------------------------------------------------------------------

    def clean_step_files(self, step_name: str, config_name: int = None):
        """
        清理指定步骤产生的文件（包括本地和远程工作站上的文件）。

        Args:
            step_name: 步骤名
            config_name: 构型名称，若为 None 则清理所有构型
        """
        # ---- 本地文件清理映射 ----
        # (目录key, 文件名模板, 额外清理的后缀对 (原后缀, 新后缀))
        local_patterns = {
            "SW":       ("step_dir",  "model_gen4_{config}.step",   None),
            "SC":       ("scdoc_dir", "model_gen4_{config}.scdoc",  None),
            "Transfer": None,  # 传输无本地文件
            "Meshing":  None,  # MSH 文件仅在远程工作站上
            "Solver":   None,  # CAS/DAT 文件仅在远程工作站上
        }

        # ---- 远程文件清理映射 ----
        # (目录key, 文件名模板, 额外清理的后缀对 (原后缀, 新后缀))
        remote_patterns = {
            "SW":       None,  # SW 无远程文件
            "SC":       None,  # SC 无远程文件（SCDOC 由 transfer 上传，但源文件在本地）
            "Transfer": None,  # 传输无产出文件
            "Meshing":  ("msh_dir",    "model_gen4_{config}.msh.h5", None),
            "Solver":   ("result_dir", "model_gen4_{config}.cas.h5", (".cas.h5", ".dat.h5")),
        }

        configs = [config_name] if config_name is not None else self.state.get_all_configs()

        # ---- 清理本地文件 ----
        local_info = local_patterns.get(step_name)
        if local_info is not None:
            dir_key, file_template, extra_suffix_pair = local_info
            target_dir = LOCAL_PATHS.get(dir_key, "")
            for cn in configs:
                filename = file_template.format(config=cn)
                filepath = os.path.join(target_dir, filename)
                if os.path.exists(filepath):
                    os.remove(filepath)
                    logger.info(f"已删除本地文件: {filepath}")
                if extra_suffix_pair:
                    old_suffix, new_suffix = extra_suffix_pair
                    extra_filename = filename.rsplit(old_suffix, 1)[0] + new_suffix
                    extra_path = os.path.join(target_dir, extra_filename)
                    if os.path.exists(extra_path):
                        os.remove(extra_path)
                        logger.info(f"已删除本地文件: {extra_path}")

        # ---- 清理远程文件 ----
        remote_info = remote_patterns.get(step_name)
        if remote_info is not None:
            dir_key, file_template, extra_suffix_pair = remote_info
            target_dir = REMOTE_CONFIG.get(dir_key, "")
            try:
                ssh = self.get_ssh()
                if ssh.is_connected():
                    for cn in configs:
                        filename = file_template.format(config=cn)
                        remote_path = f"{target_dir}\\{filename}"
                        ssh.delete_remote_file(remote_path)
                        if extra_suffix_pair:
                            old_suffix, new_suffix = extra_suffix_pair
                            extra_filename = filename.rsplit(old_suffix, 1)[0] + new_suffix
                            extra_remote_path = f"{target_dir}\\{extra_filename}"
                            ssh.delete_remote_file(extra_remote_path)
                    logger.info(f"步骤 {step_name} 远程文件清理完成 ({target_dir})")
                else:
                    logger.warning(f"SSH 未连接，跳过远程文件清理: {step_name}")
            except (OSError, IOError, ConnectionError) as e:
                logger.error(f"远程文件清理异常 ({step_name}): {e}")

        logger.info(f"步骤 {step_name} 文件清理完成")
