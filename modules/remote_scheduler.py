# =============================================================================
# modules/remote_scheduler.py — 跨机器传输与远程异步求解（阶段4）
#
# v2.0 变更说明：
#   网格划分（batch_meshing_gen4.py）与仿真求解（batch_solver_gen4.py）
#   拆分为两个独立的远程子阶段，各自拥有独立的完成标志文件
#   （meshing_done.txt 和 solving_done.txt）。
#
#   工作流程：
#     1. SFTP 上传 .scdoc 到远程工作站
#     2. 后台启动网格脚本 → 等待 meshing_done.txt
#     3. 后台启动求解脚本 → 等待 solving_done.txt
#     4. 轮询检查完成状态
# =============================================================================
import os
import re
import time
import logging
from io import BytesIO
from typing import Optional, List, Dict, Tuple

import paramiko

from config import REMOTE_CONFIG, LOCAL_CONFIG

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 子阶段常量
# ---------------------------------------------------------------------------
SUBSTAGE_MESHING = "meshing"
SUBSTAGE_SOLVING = "solving"


class RemoteScheduler:
    """
    远程调度器 — 负责 SFTP 上传 + SSH 异步执行 + 状态轮询。

    使用方式:
        rs = RemoteScheduler()
        rs.connect()
        
        # 上传 SCDOC
        rs.upload_scdoc("R2.5_L30_A15")
        
        # 启动网格
        rs.launch_substage("R2.5_L30_A15", SUBSTAGE_MESHING)
        
        # 轮询网格完成
        if rs.poll_until_done("R2.5_L30_A15", SUBSTAGE_MESHING, timeout=1800):
            # 启动求解
            rs.launch_substage("R2.5_L30_A15", SUBSTAGE_SOLVING)
        
        rs.disconnect()
    """

    # 白名单：config_id 只能包含安全字符（字母、数字、下划线、连字符、点）
    _CONFIG_ID_PATTERN = re.compile(r'^[A-Za-z0-9_.-]+$')
    _MAX_CONFIG_ID_LEN = 128

    @staticmethod
    def _validate_config_id(config_id: str) -> None:
        """
        校验 config_id 仅包含安全字符，防止命令注入。

        Raises:
            ValueError: config_id 包含非法字符或为空
        """
        if not config_id or not isinstance(config_id, str):
            raise ValueError(f"非法的 config_id（空值或非字符串）: {config_id!r}")
        if len(config_id) > RemoteScheduler._MAX_CONFIG_ID_LEN:
            raise ValueError(
                f"config_id 长度超过最大限制 {RemoteScheduler._MAX_CONFIG_ID_LEN}: "
                f"{config_id!r}"
            )
        if not RemoteScheduler._CONFIG_ID_PATTERN.match(config_id):
            raise ValueError(
                f"config_id 包含非法字符（仅允许 A-Za-z0-9_.-）: {config_id!r}"
            )

    def __init__(self):
        self._host = REMOTE_CONFIG["host"]
        self._port = REMOTE_CONFIG["port"]
        self._username = REMOTE_CONFIG["username"]
        self._password = REMOTE_CONFIG["password"]
        self._remote_root = REMOTE_CONFIG["root_dir"]
        self._remote_scdoc_dir = REMOTE_CONFIG["scdoc_dir"]
        self._conda_env = REMOTE_CONFIG["conda_env"]
        self._meshing_script = REMOTE_CONFIG["meshing_script"]
        self._solver_script = REMOTE_CONFIG["solver_script"]
        self._meshing_done_template = REMOTE_CONFIG["meshing_done_flag_template"]
        self._solving_done_template = REMOTE_CONFIG["solving_done_flag_template"]
        self._ssh_timeout = REMOTE_CONFIG["ssh_timeout"]
        self._poll_interval = REMOTE_CONFIG["poll_interval"]

        # 连接对象
        self._ssh: Optional[paramiko.SSHClient] = None
        self._sftp: Optional[paramiko.SFTPClient] = None

    # -----------------------------------------------------------------------
    # 连接管理
    # -----------------------------------------------------------------------
    def connect(self) -> bool:
        """建立 SSH 和 SFTP 连接"""
        try:
            self._ssh = paramiko.SSHClient()
            self._ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            self._ssh.connect(
                hostname=self._host,
                port=self._port,
                username=self._username,
                password=self._password,
                timeout=self._ssh_timeout,
                look_for_keys=False,
                allow_agent=False,
            )
            self._sftp = self._ssh.open_sftp()
            logger.info("SSH 连接成功: %s@%s:%d", self._username, self._host, self._port)
            return True
        except Exception as exc:
            logger.error("SSH 连接失败: %s", exc)
            return False

    def _ensure_connected(self) -> bool:
        """确保已连接，若断开则自动重连（含活性往返验证）"""
        if (
            self._ssh is not None
            and self._ssh.get_transport() is not None
            and self._ssh.get_transport().is_active()
        ):
            # 补一次轻量往返检验，防止半开连接（is_active 可能延迟发现断开）
            try:
                _stdin, stdout, _stderr = self._ssh.exec_command("echo OK", timeout=5)
                if stdout.channel.recv_exit_status() == 0:
                    return True
                logger.warning("SSH 往返检验失败，尝试重新连接...")
            except Exception as exc:
                logger.warning("SSH 活性检查异常: %s，尝试重新连接...", exc)
        else:
            logger.warning("SSH 连接已断开，尝试重新连接...")
        return self.connect()

    def disconnect(self):
        """断开所有连接"""
        if self._sftp:
            self._sftp.close()
            self._sftp = None
        if self._ssh:
            self._ssh.close()
            self._ssh = None
        logger.info("SSH 连接已关闭")

    def is_connected(self) -> bool:
        """检查连接是否活跃"""
        return (
            self._ssh is not None
            and self._ssh.get_transport() is not None
            and self._ssh.get_transport().is_active()
        )

    # -----------------------------------------------------------------------
    # SSH 命令执行
    # -----------------------------------------------------------------------
    def _exec_ssh_command(self, command: str, timeout: int = 30) -> Tuple[int, str, str]:
        """
        执行 SSH 远程命令。

        Returns:
            (exit_code, stdout_str, stderr_str)
        """
        if not self._ensure_connected():
            return -1, "", "SSH 未连接"

        try:
            _stdin, stdout, stderr = self._ssh.exec_command(command, timeout=timeout)
            exit_code = stdout.channel.recv_exit_status()
            out_str = stdout.read().decode("utf-8", errors="replace")
            err_str = stderr.read().decode("utf-8", errors="replace")
            return exit_code, out_str, err_str
        except Exception as exc:
            logger.error("SSH 命令执行异常: %s", exc)
            return -1, "", str(exc)

    # -----------------------------------------------------------------------
    # SFTP 上传
    # -----------------------------------------------------------------------
    def upload_scdoc(self, config_id: str, local_scdoc_dir: Optional[str] = None) -> bool:
        """
        将本地 .scdoc 文件上传到远程工作站。

        Args:
            config_id: 构型 ID
            local_scdoc_dir: 本地 SCDOC 目录（默认从 LOCAL_CONFIG 读取）

        Returns:
            上传成功与否
        """
        RemoteScheduler._validate_config_id(config_id)

        if not self._ensure_connected():
            return False

        local_dir = local_scdoc_dir or LOCAL_CONFIG.get("scdoc_dir", "")
        local_file = os.path.join(local_dir, f"{config_id}.scdoc")
        remote_file = os.path.join(self._remote_scdoc_dir, f"{config_id}.scdoc").replace("\\", "/")

        if not os.path.isfile(local_file):
            logger.error("本地 SCDOC 文件不存在: %s", local_file)
            return False

        try:
            # 确保远程目录存在
            try:
                self._sftp.stat(self._remote_scdoc_dir)
            except FileNotFoundError:
                self._exec_ssh_command(f'mkdir "{self._remote_scdoc_dir}" 2>nul || echo MKDIR_DONE')

            self._sftp.put(local_file, remote_file)
            logger.info("SCDOC 上传成功: %s → %s", config_id, remote_file)
            return True
        except Exception as exc:
            logger.error("SCDOC 上传失败 [%s]: %s", config_id, exc)
            return False

    # -----------------------------------------------------------------------
    # 远程批处理脚本生成（拆分为网格和求解两个脚本）
    # -----------------------------------------------------------------------
    def _generate_bat_content(
        self, config_id: str, substage: str
    ) -> Optional[str]:
        """
        生成远程 .bat 脚本内容。

        网格阶段: conda activate → python batch_meshing_gen4.py → 生成 meshing_done.txt
        求解阶段: conda activate → python batch_solver_gen4.py → 生成 solving_done.txt

        Args:
            config_id: 构型 ID（仅允许字母、数字、下划线、连字符、点号）
            substage: "meshing" 或 "solving"

        Returns:
            .bat 脚本的字符串内容，或 None
        """
        # ---- 安全校验：config_id 仅允许安全字符，防止 .bat 命令注入 ----
        if not re.fullmatch(r'[\w\-.]+', config_id):
            logger.error(
                "config_id '%s' 包含非法字符，拒绝生成 .bat 脚本。"
                "仅允许: A-Z a-z 0-9 _ . -",
                config_id,
            )
            return None

        if substage == SUBSTAGE_MESHING:
            script_path = self._meshing_script
            done_flag = self._meshing_done_template.format(config_id=config_id)
        elif substage == SUBSTAGE_SOLVING:
            script_path = self._solver_script
            done_flag = self._solving_done_template.format(config_id=config_id)
        else:
            logger.error("未知的子阶段: %s", substage)
            return None

        done_dir = os.path.dirname(done_flag)
        done_flag = done_flag.replace("\\", "/")

        bat_content = f"""@echo off
title AutoFluid_{config_id}_{substage}
setlocal enabledelayedexpansion

echo [%date% %time%] ============================================
echo [%date% %time%] AutoFluidSimulation 远程任务启动
echo [%date% %time%] 构型: {config_id}
echo [%date% %time%] 阶段: {substage}
echo [%date% %time%] ============================================

REM --- 激活 Conda 环境 ---
call conda activate {self._conda_env}
if errorlevel 1 (
    echo [%date% %time%] Conda 环境激活失败！
    exit /b 1
)

echo [%date% %time%] Python 路径: 
where python
echo [%date% %time%] 启动脚本: {script_path}

REM --- 执行脚本 ---
python "{script_path}" --config {config_id}
set EXIT_CODE=%errorlevel%

echo [%date% %time%] 脚本执行完毕，退出码: %EXIT_CODE%

REM --- 生成完成标志 ---
if %EXIT_CODE% equ 0 (
    echo [%date% %time%] 任务成功，写入完成标志...
    mkdir "{done_dir}" 2>nul
    echo %date% %time% > "{done_flag}"
    echo [%date% %time%] 完成标志已写入: {done_flag}
) else (
    echo [%date% %time%] 任务失败，退出码=%EXIT_CODE%
    echo ERROR_EXIT_CODE=%EXIT_CODE% > "{done_flag}.error"
)

exit /b %EXIT_CODE%
"""
        return bat_content

    def _upload_bat_script(self, config_id: str, substage: str) -> Optional[str]:
        """
        生成 .bat 文件并通过 SFTP 上传到远程。

        Args:
            config_id: 构型 ID
            substage: "meshing" 或 "solving"

        Returns:
            远程 .bat 文件的绝对路径（Windows 风格），或 None
        """
        bat_content = self._generate_bat_content(config_id, substage)
        if bat_content is None:
            return None

        bat_name = f"run_{config_id}_{substage}.bat"
        remote_path = f"{self._remote_scdoc_dir}/{bat_name}"

        try:
            # 通过 SFTP 写入
            if not self._ensure_connected():
                return None

            # 确保类型窄化：_ensure_connected 成功后 _sftp 必然非 None
            sftp = self._sftp
            if sftp is None:
                logger.error("SFTP 客户端不可用（连接后仍为 None）")
                return None

            # 使用 BytesIO 编码字符串为字节流，兼容 Python 3.x paramiko
            sftp.putfo(
                BytesIO(bat_content.encode("utf-8")),
                remote_path,
            )
            logger.info(".bat 上传成功: %s → %s", bat_name, remote_path)
            return remote_path
        except Exception as exc:
            logger.error("上传 .bat 失败: %s", exc)
            return None

    # -----------------------------------------------------------------------
    # 远程后台执行（方案 A: Start-Process / 方案 B: schtasks）
    # -----------------------------------------------------------------------
    def _launch_background_powershell(self, bat_path: str, config_id: str, substage: str) -> bool:
        """
        方案 A: 通过 PowerShell Start-Process 在后台启动 .bat 脚本。

        Args:
            bat_path: 远程 .bat 完整路径
            config_id: 构型 ID
            substage: 子阶段名称

        Returns:
            是否成功启动
        """
        # PowerShell 调用 Start-Process 执行 .bat，窗口隐藏，不等待
        ps_cmd = (
            f'powershell -Command "'
            f"Start-Process -FilePath 'cmd.exe' "
            f"-ArgumentList '/c \\\"{bat_path}\\\"' "
            f"-WindowStyle Hidden -NoNewWindow"
            f'"'
        )

        exit_code, stdout, stderr = self._exec_ssh_command(ps_cmd, timeout=15)
        if exit_code == 0:
            logger.info(
                "远程后台任务已启动 (方案A/PowerShell): %s/%s", config_id, substage
            )
            return True

        logger.warning("方案A 失败 (exit=%d): %s", exit_code, stderr)
        return False

    def _launch_background_schtasks(self, bat_path: str, config_id: str, substage: str) -> bool:
        """
        方案 B: 通过 schtasks 创建并立即运行一次性的计划任务。

        Args:
            bat_path: 远程 .bat 完整路径
            config_id: 构型 ID
            substage: 子阶段名称

        Returns:
            是否成功启动
        """
        task_name = f"AutoFluid_{config_id}_{substage}"

        # 删除同名旧任务（若存在）
        self._exec_ssh_command(
            f'schtasks /Delete /TN "{task_name}" /F 2>nul', timeout=10
        )

        # 创建计划任务
        create_cmd = (
            f'schtasks /Create /SC ONCE /TN "{task_name}" '
            f'/TR "\\\"{bat_path}\\\"" /ST 00:00 /RL HIGHEST /F'
        )
        exit_code, stdout, stderr = self._exec_ssh_command(create_cmd, timeout=15)

        if exit_code != 0:
            logger.error("schtasks 创建失败: %s", stderr)
            return False

        # 立即运行
        run_cmd = f'schtasks /Run /TN "{task_name}"'
        exit_code2, stdout2, stderr2 = self._exec_ssh_command(run_cmd, timeout=15)

        if exit_code2 == 0:
            logger.info(
                "远程后台任务已启动 (方案B/schtasks): %s/%s", config_id, substage
            )
            return True

        logger.error("schtasks 运行失败: %s", stderr2)
        return False

    def launch_substage(self, config_id: str, substage: str) -> bool:
        """
        在远程后台启动指定子阶段任务（网格或求解）。
        不等待完成，立即返回。

        Args:
            config_id: 构型 ID
            substage: "meshing" 或 "solving"

        Returns:
            是否成功提交到远程执行
        """
        RemoteScheduler._validate_config_id(config_id)
        logger.info("启动远程子阶段: %s / %s", config_id, substage)

        if not self._ensure_connected():
            return False

        # Step 1: 生成并上传 .bat
        bat_path = self._upload_bat_script(config_id, substage)
        if not bat_path:
            return False

        # Step 2: 尝试方案 A
        if self._launch_background_powershell(bat_path, config_id, substage):
            return True

        # Step 3: 回退到方案 B
        logger.warning("方案A 失败，回退到方案B (schtasks)...")
        return self._launch_background_schtasks(bat_path, config_id, substage)

    # -----------------------------------------------------------------------
    # 完成状态检查
    # -----------------------------------------------------------------------
    def _get_done_flag_path(self, config_id: str, substage: str) -> str:
        """获取指定子阶段的 done.txt 远程路径"""
        if substage == SUBSTAGE_MESHING:
            return self._meshing_done_template.format(config_id=config_id)
        else:
            return self._solving_done_template.format(config_id=config_id)

    def check_substage_done(self, config_id: str, substage: str) -> bool:
        """
        检查远程子阶段任务是否完成（通过对应的 done.txt 文件）。

        Args:
            config_id: 构型 ID
            substage: "meshing" 或 "solving"

        Returns:
            是否已完成
        """
        done_flag = self._get_done_flag_path(config_id, substage)
        cmd = f'if exist "{done_flag}" (echo EXISTS) else (echo NOT_FOUND)'
        exit_code, stdout, _ = self._exec_ssh_command(cmd, timeout=10)

        if exit_code == 0 and "EXISTS" in stdout:
            logger.info("远程子阶段完成: %s / %s (检测到 done.txt)", config_id, substage)
            return True

        logger.debug("远程子阶段未完成: %s / %s", config_id, substage)
        return False

    def check_substage_error(self, config_id: str, substage: str) -> bool:
        """
        检查远程子阶段任务是否出错（通过 .error 文件检测）。

        Args:
            config_id: 构型 ID
            substage: "meshing" 或 "solving"

        Returns:
            是否检测到错误
        """
        done_flag = self._get_done_flag_path(config_id, substage)
        error_flag = f"{done_flag}.error"
        cmd = f'if exist "{error_flag}" (echo EXISTS) else (echo NOT_FOUND)'
        exit_code, stdout, _ = self._exec_ssh_command(cmd, timeout=10)

        if exit_code == 0 and "EXISTS" in stdout:
            logger.warning("检测到错误标志: %s", error_flag)
            return True
        return False

    def check_substage_running(self, config_id: str, substage: str) -> bool:
        """
        检查远程子阶段任务是否仍在运行。

        Args:
            config_id: 构型 ID
            substage: "meshing" 或 "solving"

        Returns:
            远程是否有相关进程在运行
        """
        # 检查 python.exe 进程是否包含 config_id 和 substage 相关命令行
        cmd = (
            f'powershell -Command "'
            f"(Get-WmiObject Win32_Process -Filter \\\"Name='python.exe'\\\" "
            f"| Where-Object {{ $_.CommandLine -like '*{config_id}*' }} "
            f"| Measure-Object).Count"
            f'"'
        )
        exit_code, stdout, _ = self._exec_ssh_command(cmd, timeout=15)

        if exit_code == 0:
            try:
                count = int(stdout.strip())
                if count > 0:
                    logger.debug("远程 python 进程仍在运行: %s (进程数: %d)", config_id, count)
                    return True
            except ValueError:
                pass

        # 如果 substage 是 solving，额外检查 fluent.exe
        if substage == SUBSTAGE_SOLVING:
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

        return False

    # -----------------------------------------------------------------------
    # 带超时的轮询
    # -----------------------------------------------------------------------
    def poll_until_done(
        self,
        config_id: str,
        substage: str,
        timeout: int = 3600,
    ) -> bool:
        """
        轮询等待指定子阶段完成。

        Args:
            config_id: 构型 ID
            substage: "meshing" 或 "solving"
            timeout: 超时时间（秒），默认 1 小时

        Returns:
            是否成功完成
        """
        start_time = time.time()
        logger.info("开始轮询 %s / %s (超时=%ds)", config_id, substage, timeout)

        while True:
            elapsed = time.time() - start_time

            if self.check_substage_done(config_id, substage):
                logger.info("轮询成功: %s / %s 已完成 ✓", config_id, substage)
                return True

            if self.check_substage_error(config_id, substage):
                logger.error("轮询检测到错误: %s / %s", config_id, substage)
                return False

            if elapsed > timeout:
                logger.error("轮询超时: %s / %s (%.0fs)", config_id, substage, elapsed)
                return False

            # 检查进程是否还在运行（若进程消失且无 done.txt → 异常终止）
            is_running = self.check_substage_running(config_id, substage)
            if not is_running:
                logger.warning(
                    "远程进程 %s / %s 似乎已终止但未生成 done.txt，可能异常退出",
                    config_id,
                    substage,
                )

            logger.info(
                "轮询中... %s/%s (已等待 %.0fs, 间隔 %ds)",
                config_id, substage, elapsed, self._poll_interval,
            )
            time.sleep(self._poll_interval)

    # -----------------------------------------------------------------------
    # 向后兼容方法（供旧调用方使用）
    # -----------------------------------------------------------------------
    def launch_remote_task(self, config_id: str) -> bool:
        """
        [向后兼容] 启动远程任务 — 默认只启动网格阶段。
        新代码请使用 launch_substage()。
        """
        logger.warning("使用了已弃用的 launch_remote_task()，将仅启动网格阶段")
        return self.launch_substage(config_id, SUBSTAGE_MESHING)

    def check_remote_done(self, config_id: str) -> bool:
        """
        [向后兼容] 检查远程任务完成 — 检查 solving_done.txt。
        新代码请使用 check_substage_done()。
        """
        return self.check_substage_done(config_id, SUBSTAGE_SOLVING)

    def check_remote_running(self, config_id: str) -> bool:
        """
        [向后兼容] 检查远程任务是否仍在运行。
        检查网格或求解任一阶段的进程是否存活。
        新代码请使用 check_substage_running()。
        """
        # 检查网格阶段
        if self.check_substage_running(config_id, SUBSTAGE_MESHING):
            return True
        # 检查求解阶段
        if self.check_substage_running(config_id, SUBSTAGE_SOLVING):
            return True
        return False

    def poll_remote_tasks(
        self, config_ids: List[str], timeout_per_task: int = 3600
    ) -> Dict[str, bool]:
        """
        [向后兼容] 轮询多个任务 — 检查 solving_done.txt。
        新代码请按 substage 单独轮询。
        """
        logger.info("轮询 %d 个远程求解任务...", len(config_ids))
        start_time = time.time()
        results = {cid: False for cid in config_ids}

        while True:
            all_done = True
            for cid in config_ids:
                if results[cid]:
                    continue
                if self.check_substage_done(cid, SUBSTAGE_SOLVING):
                    results[cid] = True
                    logger.info("远程求解完成: %s ✓", cid)
                else:
                    all_done = False

            if all_done:
                break

            elapsed = time.time() - start_time
            if elapsed > timeout_per_task * len(config_ids):
                logger.warning("轮询超时")
                break

            logger.info(
                "轮询中... 已完成: %d/%d",
                sum(1 for v in results.values() if v),
                len(config_ids),
            )
            time.sleep(self._poll_interval)

        return results

    # -----------------------------------------------------------------------
    # 完整上传 + 启动流程（从 pipeline_controller 调用）
    # -----------------------------------------------------------------------
    def process_config(self, config_id: str) -> bool:
        """
        执行完整的远程调度流程：
        1. SFTP 上传 SCDOC 文件
        2. 在远程后台启动网格任务（不等待完成）

        Returns:
            是否成功提交
        """
        RemoteScheduler._validate_config_id(config_id)
        if not self.upload_scdoc(config_id):
            return False

        if not self.launch_substage(config_id, SUBSTAGE_MESHING):
            return False

        logger.info("构型 %s 已提交到远程工作站 (网格阶段) ✓", config_id)
        return True

    def cleanup(self):
        """清理连接资源"""
        self.disconnect()