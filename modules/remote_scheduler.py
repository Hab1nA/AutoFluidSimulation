# =============================================================================
# remote_scheduler.py — 跨机器传输与 Windows 远程异步求解（阶段4）
#
# 功能：
#   1. 通过 SFTP 上传 .scdoc 文件到远程工作站
#   2. 创建并上传单构型远程执行脚本（.bat）
#   3. 通过 SSH + PowerShell Start-Process 实现 Windows 后台独立进程拉起
#   4. 异步轮询远程 done.txt 完成标志
#
# 核心难点：确保本地 SSH 断开后，远程 Fluent 计算进程继续独立运行。
# 解决方案：
#   方案 A（首选）：PowerShell Start-Process 后台启动 .bat
#   方案 B（备选）：Windows 计划任务 (schtasks) 触发
# =============================================================================
import os
import time
import logging
import traceback
from pathlib import Path
from typing import Optional, List, Dict
from datetime import datetime

import paramiko
from config import LOCAL_CONFIG, REMOTE_CONFIG, GLOBAL_CONFIG

logger = logging.getLogger(__name__)


class RemoteScheduler:
    """
    远程 Windows 工作站调度器。

    负责：
    - SFTP 文件传输
    - SSH 命令执行
    - 远程后台进程管理（Windows 特有）
    - 计算完成状态轮询
    """

    def __init__(self):
        """初始化连接参数和路径配置"""
        self.host = REMOTE_CONFIG["host"]
        self.port = REMOTE_CONFIG["port"]
        self.username = REMOTE_CONFIG["username"]
        self.password = REMOTE_CONFIG["password"]
        self.remote_scdoc_dir = REMOTE_CONFIG["scdoc_dir"]
        self.remote_root_dir = REMOTE_CONFIG["root_dir"]
        self.conda_env = REMOTE_CONFIG["conda_env"]
        self.meshing_script = REMOTE_CONFIG["meshing_script"]
        self.solver_script = REMOTE_CONFIG["solver_script"]
        self.done_flag_template = REMOTE_CONFIG["done_flag_template"]
        self.ssh_timeout = REMOTE_CONFIG["ssh_timeout"]
        self.poll_interval = REMOTE_CONFIG["poll_interval"]

        self.local_scdoc_dir = LOCAL_CONFIG["scdoc_dir"]

        self._ssh_client: Optional[paramiko.SSHClient] = None
        self._sftp_client: Optional[paramiko.SFTPClient] = None

    # -----------------------------------------------------------------------
    # SSH/SFTP 连接管理
    # -----------------------------------------------------------------------
    def _connect_ssh(self) -> bool:
        """
        建立 SSH 连接（带自动重试）。

        Returns:
            是否连接成功
        """
        if self._ssh_client is not None:
            try:
                self._ssh_client.exec_command("echo ok", timeout=5)
                return True
            except Exception:
                self._disconnect()

        for attempt in range(3):
            try:
                logger.info("正在连接远程工作站 %s:%d (尝试 %d/3)...",
                            self.host, self.port, attempt + 1)
                self._ssh_client = paramiko.SSHClient()
                self._ssh_client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                self._ssh_client.connect(
                    hostname=self.host,
                    port=self.port,
                    username=self.username,
                    password=self.password,
                    timeout=self.ssh_timeout,
                    banner_timeout=self.ssh_timeout,
                    auth_timeout=self.ssh_timeout,
                )
                logger.info("SSH 连接成功")
                return True
            except Exception as e:
                logger.warning("SSH 连接失败 (尝试 %d/3): %s", attempt + 1, e)
                time.sleep(3)

        logger.error("SSH 连接最终失败")
        return False

    def _connect_sftp(self) -> bool:
        """
        建立 SFTP 会话。

        Returns:
            是否建立成功
        """
        if not self._connect_ssh():
            return False
        try:
            self._sftp_client = self._ssh_client.open_sftp()
            logger.info("SFTP 会话已建立")
            return True
        except Exception as e:
            logger.error("SFTP 会话建立失败: %s", e)
            return False

    def _disconnect(self):
        """断开所有远程连接"""
        try:
            if self._sftp_client:
                self._sftp_client.close()
                self._sftp_client = None
        except Exception:
            pass
        try:
            if self._ssh_client:
                self._ssh_client.close()
                self._ssh_client = None
        except Exception:
            pass
        logger.info("远程连接已断开")

    # -----------------------------------------------------------------------
    # SSH 命令执行
    # -----------------------------------------------------------------------
    def _exec_ssh_command(self, command: str, timeout: int = 30) -> tuple:
        """
        通过 SSH 执行单条命令。

        Args:
            command: 要执行的 shell 命令
            timeout: 超时时间（秒）

        Returns:
            (exit_code, stdout_str, stderr_str)
        """
        if not self._connect_ssh():
            return (-1, "", "SSH 未连接")

        try:
            logger.debug("SSH 执行: %s", command[:200])
            stdin, stdout, stderr = self._ssh_client.exec_command(
                command, timeout=timeout
            )
            exit_code = stdout.channel.recv_exit_status()
            out_str = stdout.read().decode("utf-8", errors="replace")
            err_str = stderr.read().decode("utf-8", errors="replace")
            return (exit_code, out_str, err_str)
        except Exception as e:
            logger.error("SSH 命令执行失败: %s", e)
            return (-1, "", str(e))

    # -----------------------------------------------------------------------
    # 文件传输
    # -----------------------------------------------------------------------
    def _get_latest_scdoc_file(self) -> Optional[Path]:
        """
        获取本地 SCDOC 目录中最新的 .scdoc 文件。

        Returns:
            最新 SCDOC 文件的 Path 对象
        """
        scdoc_files = list(Path(self.local_scdoc_dir).glob("*.scdoc"))
        if not scdoc_files:
            logger.warning("本地 SCDOC 目录中没有找到文件: %s", self.local_scdoc_dir)
            return None
        return max(scdoc_files, key=lambda f: f.stat().st_mtime)

    def upload_scdoc(self, config_id: str, local_path: Optional[str] = None) -> bool:
        """
        上传 SCDOC 文件到远程工作站。

        Args:
            config_id: 构型 ID（用于命名远程目标文件）
            local_path: 本地 SCDOC 文件路径，None 则自动查找最新文件

        Returns:
            是否上传成功
        """
        if local_path is None:
            latest = self._get_latest_scdoc_file()
            if latest is None:
                logger.error("没有可用的 SCDOC 文件供上传")
                return False
            local_path = str(latest)

        if not os.path.isfile(local_path):
            logger.error("本地 SCDOC 文件不存在: %s", local_path)
            return False

        if not self._connect_sftp():
            return False

        try:
            # 确保远程目录存在
            try:
                self._sftp_client.stat(self.remote_scdoc_dir)
            except FileNotFoundError:
                # 递归创建远程目录
                self._exec_ssh_command(f'mkdir "{self.remote_scdoc_dir}"')
                logger.info("远程目录已创建: %s", self.remote_scdoc_dir)

            # 使用 config_id 命名远程文件（符合远程脚本命名约定 model_gen4_{i}.scdoc）
            remote_filename = f"{config_id}.scdoc"
            remote_path = os.path.join(self.remote_scdoc_dir, remote_filename).replace("\\", "/")

            logger.info("正在上传 SCDOC: %s → %s@%s:%s",
                        os.path.basename(local_path), self.username, self.host, remote_path)

            self._sftp_client.put(local_path, remote_path)
            logger.info("SCDOC 上传完成: %s", remote_filename)
            return True

        except Exception as e:
            logger.error("SFTP 上传失败: %s", traceback.format_exc())
            return False

    # -----------------------------------------------------------------------
    # 远程后台执行脚本生成（核心）
    # -----------------------------------------------------------------------
    def _generate_remote_bat_content(self, config_id: str) -> str:
        """
        生成远程 .bat 批处理脚本内容。

        该脚本在远程工作站上依次执行：
        1. 激活 conda 环境
        2. 运行网格生成脚本
        3. 运行求解脚本
        4. 生成完成标志 done.txt

        Args:
            config_id: 构型 ID

        Returns:
            .bat 脚本的完整内容
        """
        done_flag = self.done_flag_template.format(config_id=config_id)

        # ── 方案：使用 Python -c 内联执行，实现单构型处理 ──
        # 远程脚本 batch_meshing_gen4.py 和 batch_solver_gen4.py 是硬编码的批量循环。
        # 为了实现单构型逐个调度，我们在 .bat 中内联 Python 代码，
        # 该代码直接调用 pyfluent 处理单个 SCDOC 文件。
        # 这种方式避免了修改原始远程脚本。

        bat_content = f"""@echo off
REM =============================================================================
REM 自动生成的远程任务执行脚本
REM 构型: {config_id}
REM 生成时间: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
REM =============================================================================
setlocal enabledelayedexpansion

echo [%date% %time%] 远程任务开始: {config_id}

REM --- 激活 Conda 环境 ---
call conda activate {self.conda_env}
if errorlevel 1 (
    echo [ERROR] Conda 环境激活失败: {self.conda_env}
    exit /b 1
)
echo [%date% %time%] Conda 环境已激活: {self.conda_env}

REM --- 切换到工程根目录 ---
cd /d "{self.remote_root_dir}"
echo [%date% %time%] 工作目录: %CD%

REM --- 执行网格生成 (针对单构型) ---
echo [%date% %time%] 开始网格生成: {config_id}
python "{self.meshing_script}" --config-id "{config_id}"
if errorlevel 1 (
    echo [ERROR] 网格生成失败: {config_id}
    exit /b 2
)
echo [%date% %time%] 网格生成完成: {config_id}

REM --- 执行 Fluent 求解 (针对单构型) ---
echo [%date% %time%] 开始求解: {config_id}
python "{self.solver_script}" --config-id "{config_id}"
if errorlevel 1 (
    echo [ERROR] 求解失败: {config_id}
    exit /b 3
)
echo [%date% %time%] 求解完成: {config_id}

REM --- 生成完成标志 ---
echo %date% %time% > "{done_flag}"
echo [%date% %time%] 完成标志已生成: {done_flag}

echo [%date% %time%] ========== 任务全部完成: {config_id} ==========
exit /b 0
"""
        return bat_content

    def _upload_bat_script(self, config_id: str) -> Optional[str]:
        """
        生成并上传远程批处理脚本。

        Args:
            config_id: 构型 ID

        Returns:
            远程 .bat 文件完整路径，失败返回 None
        """
        bat_content = self._generate_remote_bat_content(config_id)
        bat_filename = f"run_{config_id}.bat"
        local_bat_path = os.path.join(LOCAL_CONFIG["log_dir"], bat_filename)

        # 本地生成临时 .bat 文件
        try:
            with open(local_bat_path, "w", encoding="utf-8", newline="\r\n") as f:
                f.write(bat_content)
            logger.debug("本地生成 .bat: %s", local_bat_path)
        except Exception as e:
            logger.error("本地生成 .bat 失败: %s", e)
            return None

        # 上传到远程
        if not self._connect_sftp():
            return None

        try:
            remote_bat_path = os.path.join(
                self.remote_root_dir, bat_filename
            ).replace("\\", "/")

            self._sftp_client.put(local_bat_path, remote_bat_path)
            logger.info("远程 .bat 脚本已上传: %s", remote_bat_path)

            # 清理本地临时文件
            try:
                os.remove(local_bat_path)
            except Exception:
                pass

            return remote_bat_path

        except Exception as e:
            logger.error("上传 .bat 脚本失败: %s", traceback.format_exc())
            return None

    # -----------------------------------------------------------------------
    # Windows 后台进程拉起（核心难点）
    # -----------------------------------------------------------------------
    def _launch_background_primary(self, bat_path: str, config_id: str) -> bool:
        """
        方案 A（首选）：使用 PowerShell Start-Process 在后台拉起 .bat。
        
        Start-Process 会创建一个独立进程，脱离 SSH 会话生命周期。
        -WindowStyle Hidden 使窗口不可见（Fluent 有自己的 GUI）。
        使用 PowerShell 单引号字面量避免路径中含空格时的转义陷阱。

        Args:
            bat_path: 远程 .bat 文件完整路径
            config_id: 构型 ID

        Returns:
            是否启动成功
        """
        # 构造 PowerShell 命令
        # 使用单引号字面量 '...' 包裹路径参数，避免反斜杠转义陷阱
        # 格式：cmd.exe /c ""bat_path"" > "log_path" 2>&1
        log_path = f"{self.remote_root_dir}/log_{config_id}.txt".replace("\\", "/")
        argument_list = f"/c '{bat_path}' > '{log_path}' 2>&1"

        ps_command = (
            f"powershell -Command \""
            f"Start-Process -FilePath 'cmd.exe' "
            f"-ArgumentList '{argument_list}' "
            f"-WindowStyle Hidden -NoNewWindow "
            f"-WorkingDirectory '{self.remote_root_dir}'"
            f"\""
        )

        logger.info("正在后台启动远程任务 (方案A: Start-Process)...")
        exit_code, stdout, stderr = self._exec_ssh_command(ps_command, timeout=15)

        if exit_code == 0:
            logger.info("远程后台任务已启动: %s", config_id)
            # 额外确认：检查 cmd.exe 进程是否在运行
            time.sleep(3)
            check_code, check_out, _ = self._exec_ssh_command(
                f'powershell -Command "Get-Process cmd -ErrorAction SilentlyContinue | Measure-Object | Select-Object -ExpandProperty Count"',
                timeout=10,
            )
            logger.info("远程 cmd 进程数: %s (用于确认后台任务拉起)", check_out.strip())
            return True
        else:
            logger.warning("方案 A 返回非零退出码: %d\nstdout: %s\nstderr: %s",
                           exit_code, stdout[:500], stderr[:500])
            return False

    def _launch_background_fallback(self, bat_path: str, config_id: str) -> bool:
        """
        方案 B（备选）：使用 Windows 计划任务 (schtasks) 拉起后台进程。

        优点：完全脱离 SSH 会话，由系统服务管理。
        缺点：权限要求较高，调度延迟约 5-10 秒。

        Args:
            bat_path: 远程 .bat 文件完整路径
            config_id: 构型 ID

        Returns:
            是否启动成功
        """
        task_name = f"FluentTask_{config_id}"
        # 创建计划任务（运行一次，2 秒后触发）
        create_cmd = (
            f'schtasks /Create /SC ONCE /TN "{task_name}" '
            f'/TR "cmd.exe /c \\"{bat_path}\\" > \\"{self.remote_root_dir}/log_{config_id}.txt\\" 2>&1" '
            f'/ST 00:00 /F'
        )
        run_cmd = f'schtasks /Run /TN "{task_name}"'

        logger.info("正在后台启动远程任务 (方案B: schtasks)...")

        # 创建任务
        exit_code, stdout, stderr = self._exec_ssh_command(create_cmd, timeout=20)
        if exit_code != 0:
            logger.error("schtasks 创建失败: %s\nstderr: %s", stdout[:500], stderr[:500])
            return False

        # 立即触发
        exit_code, stdout, stderr = self._exec_ssh_command(run_cmd, timeout=20)
        if exit_code == 0:
            logger.info("远程后台任务已启动 (schtasks): %s", config_id)
            return True
        else:
            logger.error("schtasks 运行失败: %s", stderr[:500])
            return False

    def launch_remote_task(self, config_id: str) -> bool:
        """
        启动远程计算任务。

        流程：
        1. 生成并上传 .bat 批处理脚本
        2. 尝试方案 A (Start-Process) 启动
        3. 失败则回退到方案 B (schtasks)

        Args:
            config_id: 构型 ID

        Returns:
            是否成功启动
        """
        logger.info("=" * 60)
        logger.info("阶段4 (远程调度) — 启动远程任务: %s", config_id)
        logger.info("=" * 60)

        # Step 1: 生成并上传 .bat
        bat_path = self._upload_bat_script(config_id)
        if bat_path is None:
            logger.error("无法上传远程执行脚本")
            return False

        # Step 2: 尝试方案 A
        if self._launch_background_primary(bat_path, config_id):
            return True

        # Step 3: 回退到方案 B
        logger.warning("方案 A 失败，回退到方案 B (schtasks)...")
        if self._launch_background_fallback(bat_path, config_id):
            return True

        logger.error("无法在远程启动后台计算任务")
        return False

    # -----------------------------------------------------------------------
    # 远程状态检查与轮询
    # -----------------------------------------------------------------------
    def check_remote_done(self, config_id: str) -> bool:
        """
        检查远程计算是否完成（通过 done.txt 完成标志）。

        Args:
            config_id: 构型 ID

        Returns:
            是否已完成
        """
        done_flag = self.done_flag_template.format(config_id=config_id)
        # 使用 if exist 检查文件
        cmd = f'if exist "{done_flag}" (echo EXISTS) else (echo NOT_FOUND)'
        exit_code, stdout, stderr = self._exec_ssh_command(cmd, timeout=10)

        if exit_code == 0 and "EXISTS" in stdout:
            logger.info("远程任务已完成: %s (检测到 done.txt)", config_id)
            return True

        logger.debug("远程任务尚未完成: %s", config_id)
        return False

    def check_remote_running(self, config_id: str) -> bool:
        """
        检查远程计算是否仍在运行。

        通过检查是否有 fluent.exe 或 python.exe 进程在执行相关脚本。

        Args:
            config_id: 构型 ID

        Returns:
            远程是否有相关进程在运行
        """
        # 检查 python.exe 是否包含 config_id 的命令行参数
        cmd = (
            f'powershell -Command "'
            f"(Get-WmiObject Win32_Process -Filter \\\"Name='python.exe'\\\" "
            f"| Where-Object {{ $_.CommandLine -like '*{config_id}*' }} "
            f"| Measure-Object).Count"
            f'"'
        )
        exit_code, stdout, stderr = self._exec_ssh_command(cmd, timeout=15)

        if exit_code == 0:
            try:
                count = int(stdout.strip())
                if count > 0:
                    logger.debug("远程计算进程仍在运行: %s (python 进程数: %d)", config_id, count)
                    return True
            except ValueError:
                pass

        # 额外检查 fluent.exe 进程
        cmd2 = (
            f'powershell -Command "'
            f"(Get-Process fluent -ErrorAction SilentlyContinue "
            f"| Measure-Object).Count"
            f'"'
        )
        exit_code2, stdout2, _ = self._exec_ssh_command(cmd2, timeout=10)
        if exit_code2 == 0:
            try:
                count2 = int(stdout2.strip())
                if count2 > 0:
                    logger.debug("远程 Fluent 进程仍在运行 (进程数: %d)", count2)
                    return True
            except ValueError:
                pass

        return False  # 没有任何相关进程在运行

    def poll_remote_tasks(self, config_ids: List[str], timeout_per_task: int = 3600) -> Dict[str, bool]:
        """
        轮询多个远程计算任务的状态。

        Args:
            config_ids: 需要检查的构型 ID 列表
            timeout_per_task: 每个任务的超时时间（秒），默认1小时。暂作为总超时预算。

        Returns:
            {config_id: is_complete} 字典
        """
        logger.info("开始轮询 %d 个远程任务...", len(config_ids))
        start_time = time.time()
        results = {cid: False for cid in config_ids}

        while True:
            all_done = True
            for cid in config_ids:
                if results[cid]:
                    continue  # 已确认完成的跳过

                done = self.check_remote_done(cid)
                if done:
                    results[cid] = True
                    logger.info("远程任务完成确认: %s ✓", cid)
                else:
                    # 未完成但检查进程是否还活着
                    running = self.check_remote_running(cid)
                    if not running:
                        logger.warning(
                            "远程任务 %s 的进程似乎已终止但未生成 done.txt，可能异常退出",
                            cid,
                        )
                        # 不自动标记完成，让后续逻辑决定是否重试
                    all_done = False

            if all_done:
                logger.info("所有远程任务已完成 ✓")
                break

            # 超时检查
            elapsed = time.time() - start_time
            if elapsed > timeout_per_task * len(config_ids):
                logger.warning("轮询超时（%.0f 秒），剩余未完成任务: %d 个",
                               elapsed, sum(1 for v in results.values() if not v))
                break

            logger.info("远程任务轮询中... 已完成: %d/%d (下次检查 %d 秒后)",
                        sum(1 for v in results.values() if v),
                        len(config_ids),
                        self.poll_interval)
            time.sleep(self.poll_interval)

        return results

    # -----------------------------------------------------------------------
    # 完整上传+启动流程
    # -----------------------------------------------------------------------
    def process_config(self, config_id: str) -> bool:
        """
        执行完整的远程调度流程：
        1. SFTP 上传 SCDOC 文件
        2. 在远程后台启动计算任务（不等待完成）

        Args:
            config_id: 构型 ID

        Returns:
            是否成功提交远程任务
        """
        # --- Step 1: 上传 SCDOC ---
        if not self.upload_scdoc(config_id):
            return False

        # --- Step 2: 启动远程后台任务 ---
        if not self.launch_remote_task(config_id):
            return False

        logger.info("构型 %s 已提交到远程工作站 ✓", config_id)
        return True

    def cleanup(self):
        """清理连接资源"""
        self._disconnect()