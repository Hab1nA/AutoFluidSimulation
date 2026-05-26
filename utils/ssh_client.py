"""
===============================================================================
SSH 客户端模块 (SSH Client)
基于 paramiko 封装远程 Windows 工作站的 SSH 操作。
关键功能：
- 通过 PowerShell -EncodedCommand + Start-Process 拉起独立后台进程（SSH 断开后进程存活）
- 通过轮询标志文件判断远程任务是否完成
- 文件上传 (SFTP)
===============================================================================
"""
from __future__ import annotations

import base64
import hashlib
import os
import socket
import time
import threading
from typing import Any

try:
    import paramiko
except ImportError:  # pragma: no cover
    paramiko = None
from utils.logger import setup_logger

logger = setup_logger(__name__)

class _UploadInterrupted(Exception):
    """Raised internally when pause/stop interrupts an active SFTP upload."""


class RemoteWorkstation:
    """
    远程 Windows 工作站 SSH 客户端。

    封装了 SSH 连接、文件传输、远程命令执行等功能。
    所有网格划分和仿真求解任务均通过 PowerShell Start-Process
    以独立后台进程方式启动，确保 SSH 断开后任务继续运行。
    """

    def __init__(self, host: str, port: int, username: str, password: str):
        """
        初始化 SSH 客户端配置。

        Args:
            host: 远程主机 IP
            port: SSH 端口
            username: 用户名
            password: 密码
        """
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self._ssh: paramiko.SSHClient | None = None
        self._sftp: paramiko.SFTPClient | None = None

    # ------------------------------------------------------------------
    # 连接管理
    # ------------------------------------------------------------------

    def connect(self) -> bool:
        """建立 SSH 连接。"""
        if paramiko is None:
            raise ModuleNotFoundError(
                "未安装依赖 paramiko。请执行: pip install -r requirements.txt"
            )
        try:
            self._ssh = paramiko.SSHClient()
            self._ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            self._ssh.connect(
                hostname=self.host,
                port=self.port,
                username=self.username,
                password=self.password,
                timeout=10,
                look_for_keys=False,
                allow_agent=False,
            )
            transport = self._ssh.get_transport()
            if transport:
                transport.set_keepalive(30)
            self._sftp = self._ssh.open_sftp()
            logger.info(f"[SSH] SSH 连接成功: {self.username}@{self.host}:{self.port}")
            return True
        except (paramiko.SSHException, OSError, EOFError) as e:
            logger.error(f"[SSH] SSH 连接失败: {e}")
            self._ssh = None
            self._sftp = None
            return False

    def disconnect(self):
        """断开 SSH 连接。"""
        if self._sftp:
            try:
                self._sftp.close()
            except (OSError, EOFError) as e:
                logger.warning(f"[SSH] SFTP 关闭异常: {e}")
            finally:
                self._sftp = None
        if self._ssh:
            try:
                self._ssh.close()
            except (OSError, EOFError) as e:
                logger.warning(f"[SSH] SSH 关闭异常: {e}")
            finally:
                self._ssh = None
        logger.info("[SSH] SSH 连接已断开")

    def is_connected(self) -> bool:
        """检查 SSH 是否已连接（含心跳验证）。"""
        if self._ssh is None:
            return False
        transport = self._ssh.get_transport()
        if transport is None or not transport.is_active():
            return False
        try:
            transport.send_ignore()
            return True
        except (OSError, EOFError):
            return False

    def ensure_connected(self) -> bool:
        """确保连接有效，若断开则自动重连。"""
        if not self.is_connected():
            logger.info("[SSH] SSH 已断开，尝试重新连接...")
            return self.connect()
        return True

    # ------------------------------------------------------------------
    # 文件传输
    # ------------------------------------------------------------------

    def upload_file(
        self,
        local_path: str,
        remote_path: str,
        max_retries: int = 3,
        *,
        timeout: float | None = None,
        paused_event: threading.Event | None = None,
        stopped_event: threading.Event | None = None,
    ) -> bool:
        """通过 SFTP 上传文件到远程工作站（带重试机制）。

        Args:
            local_path: 本地文件路径
            remote_path: 远程文件路径
            max_retries: 最大重试次数
            timeout: SFTP 通道读写超时（秒）
            paused_event: 暂停事件；上传前或上传中置位时中断本次上传
            stopped_event: 停止事件；上传前或上传中置位时中断本次上传

        Returns:
            上传成功返回 True，失败返回 False
        """
        for attempt in range(max_retries):
            try:
                if stopped_event is not None and stopped_event.is_set():
                    logger.info("[SSH] 文件上传因停止指令取消")
                    return False
                if paused_event is not None and paused_event.is_set():
                    logger.info("[SSH] 文件上传因暂停指令暂缓")
                    return False

                # 每次尝试前确保连接有效（解决竞态条件）
                if not self.ensure_connected():
                    if attempt < max_retries - 1:
                        logger.warning(f"[SSH] 连接失败，{attempt + 1}/{max_retries} 重试...")
                        time.sleep(0.5 * (2 ** attempt))  # 指数退避
                        continue
                    return False

                remote_dir = os.path.dirname(remote_path)
                self._ensure_remote_dir(remote_dir)

                logger.info(f"[SSH] 正在上传: {local_path} -> {remote_path}")
                # 再次确认 SFTP 连接有效
                if self._sftp is None:
                    raise ConnectionError("SFTP 连接已断开，请先调用 connect()")

                channel = self._sftp.get_channel()
                previous_timeout = None
                if timeout is not None:
                    try:
                        previous_timeout = channel.gettimeout()
                    except AttributeError:
                        previous_timeout = None
                    channel.settimeout(timeout)

                def _check_upload_control(transferred: int, total: int) -> None:
                    if stopped_event is not None and stopped_event.is_set():
                        raise _UploadInterrupted("收到停止指令")
                    if paused_event is not None and paused_event.is_set():
                        raise _UploadInterrupted("收到暂停指令")

                try:
                    self._sftp.put(
                        local_path,
                        remote_path,
                        callback=_check_upload_control,
                    )
                finally:
                    if timeout is not None:
                        channel.settimeout(previous_timeout)
                logger.info(f"[SSH] 上传完成: {os.path.basename(local_path)}")
                return True
            except _UploadInterrupted as e:
                logger.info(f"[SSH] 文件上传已中断: {e}")
                self.disconnect()
                return False
            except (paramiko.SSHException, OSError, EOFError, socket.timeout) as e:
                logger.error(f"[SSH] 文件上传失败 (尝试 {attempt + 1}/{max_retries}): {e}")
                self.disconnect()
                if attempt < max_retries - 1:
                    time.sleep(0.5 * (2 ** attempt))  # 指数退避
                else:
                    return False
        return False

    def get_remote_file_size(self, remote_path: str) -> int | None:
        """返回远程文件大小；文件不存在或连接异常时返回 None。"""
        if not self.ensure_connected():
            return None
        if self._sftp is None:
            return None
        try:
            return int(self._sftp.stat(remote_path).st_size)
        except FileNotFoundError:
            return None
        except (paramiko.SSHException, OSError, EOFError) as e:
            logger.warning(f"[SSH] 获取远程文件大小异常: {remote_path}: {e}")
            return None

    def _ensure_remote_dir(self, remote_dir: str, _depth: int = 0):
        """
        递归创建远程目录（类似 mkdir -p）。

        Args:
            remote_dir: 远程目录路径
            _depth: 内部递归深度计数器（外部调用方不应指定）

        Raises:
            ConnectionError: SFTP 未连接
            OSError: 远程目录创建失败（非"已存在"错误）
            RecursionError: 递归深度超过安全阈值
        """
        if _depth > 32:
            raise RecursionError(f"远程目录递归深度超过上限: {remote_dir}")
        if not self._sftp:
            raise ConnectionError("SFTP 未连接")
        # 规范化远程路径（统一使用正斜杠，SFTP 要求）
        remote_dir = remote_dir.replace("\\", "/")
        try:
            self._sftp.stat(remote_dir)
        except FileNotFoundError:
            # 递归创建父目录
            parent = "/".join(remote_dir.rstrip("/").split("/")[:-1])
            if parent and parent != remote_dir:
                self._ensure_remote_dir(parent, _depth + 1)
            try:
                self._sftp.mkdir(remote_dir)
                logger.debug(f"[SSH] 创建远程目录: {remote_dir}")
            except OSError as e:
                # 检查是否因目录已存在而失败（并发创建场景）
                try:
                    self._sftp.stat(remote_dir)
                    logger.debug(f"[SSH] 远程目录已存在（并发创建）: {remote_dir}")
                except FileNotFoundError:
                    # 目录确实不存在但创建失败 → 真实错误
                    logger.error(f"[SSH] 无法创建远程目录 {remote_dir}: {e}")
                    raise

    def check_remote_file(self, remote_path: str) -> bool:
        """检查远程文件是否存在。"""
        if not self.ensure_connected():
            return False
        try:
            if self._sftp is None:
                return False
            self._sftp.stat(remote_path)
            return True
        except FileNotFoundError:
            return False
        except (paramiko.SSHException, OSError, EOFError) as e:
            logger.warning(f"[SSH] 检查远程文件异常: {remote_path}: {e}")
            return False

    def delete_remote_file(self, remote_path: str) -> bool:
        """
        删除远程工作站上的单个文件（通过 SFTP 协议）。

        使用 SFTP remove() 而非 shell 命令，避免依赖远程默认 shell 类型
        （cmd.exe / PowerShell）导致的静默失败问题。

        Args:
            remote_path: 远程文件完整路径

        Returns:
            True 表示删除成功或文件本就不存在
        """
        if not self.ensure_connected():
            return False
        if self._sftp is None:
            logger.error(f"[SSH] SFTP 未就绪，无法删除远程文件: {remote_path}")
            return False
        # SFTP 协议要求使用正斜杠
        normalized = remote_path.replace("\\", "/")
        try:
            self._sftp.remove(normalized)
            logger.info(f"[SSH] 远程文件已删除: {remote_path}")
            return True
        except FileNotFoundError:
            # 文件本就不存在，视为成功
            logger.debug(f"[SSH] 远程文件不存在（跳过）: {remote_path}")
            return True
        except (paramiko.SSHException, OSError, EOFError) as e:
            logger.error(f"[SSH] 远程文件删除失败: {remote_path}: {e}")
            return False

    # ------------------------------------------------------------------
    # 远程命令执行
    # ------------------------------------------------------------------

    def exec_command(self, command: str, timeout: int = 30) -> tuple[str, str, int]:
        """在远程工作站执行命令（同步等待完成）。"""
        if not self.ensure_connected():
            return ("", "SSH 未连接", -1)
        try:
            logger.debug(f"[SSH] 远程执行: {command}")
            stdin, stdout, stderr = self._ssh.exec_command(command, timeout=timeout)  # type: ignore[union-attr]
            exit_code = stdout.channel.recv_exit_status()
            out_raw = stdout.read()
            err_raw = stderr.read()
            out = self._decode_remote_output(out_raw)
            err = self._decode_remote_output(err_raw)
            return (out, err, exit_code)
        except (paramiko.SSHException, paramiko.AuthenticationException) as e:
            logger.error(f"[SSH] SSH 认证或协议异常: {e}")
            self.disconnect()
            return ("", str(e), -1)
        except socket.timeout:
            logger.error("[SSH] 远程命令执行超时")
            return ("", "命令执行超时", -1)
        except OSError as e:
            logger.error(f"[SSH] 远程命令执行失败: {e}")
            self.disconnect()
            return ("", str(e), -1)
        except EOFError as e:
            logger.error(f"[SSH] SSH 连接已断开: {e}")
            self.disconnect()
            return ("", str(e), -1)
        except Exception as e:
            logger.error(f"[SSH] 远程命令执行未知异常: {e}")
            return ("", str(e), -1)

    @staticmethod
    def _decode_remote_output(raw: bytes) -> str:
        """解码远程 Windows 输出，优先 GBK（中文 Windows），回退 UTF-8。"""
        for encoding in ("gbk", "utf-8"):
            try:
                return raw.decode(encoding)
            except (UnicodeDecodeError, LookupError):
                continue
        return raw.decode("utf-8", errors="replace")

    @staticmethod
    def _parse_disk_space(wmic_output: str) -> str:
        """解析 wmic 磁盘空间输出，将字节值换算为 GB 并格式化。"""
        lines = wmic_output.strip().splitlines()
        # wmic 输出格式: 第一行是表头(FreeSpace  Size)，第二行是数值
        for line in lines:
            parts = line.split()
            if len(parts) >= 2:
                try:
                    free_bytes = int(parts[0])
                    total_bytes = int(parts[1])
                    free_gb = free_bytes / (1024 ** 3)
                    total_gb = total_bytes / (1024 ** 3)
                    return f"空闲 {free_gb:.2f} GB / 总计 {total_gb:.2f} GB"
                except ValueError:
                    continue
        # 如果解析失败，返回原始输出（已去除多余空白）
        return wmic_output.strip()

    def exec_background(self, command: str, flag_file: str) -> bool:
        """
        在远程工作站以独立后台进程方式执行命令。

        使用 Windows 计划任务启动独立进程，避免 SSH 非交互会话中的
        Start-Process 静默失败。任务完成后会创建指定的标志文件。

        Args:
            command: 要执行的命令（如 conda run ... python script.py 5）
            flag_file: 任务完成标志文件路径（远程路径）

        Returns:
            True 表示后台进程启动成功
        """
        if not self.ensure_connected():
            return False

        logger.info(f"[SSH] 启动远程后台任务: {command}")
        logger.debug(f"[SSH] 标志文件: {flag_file}")

        try:
            # 先清理旧的标志文件（使用 SFTP 删除，避免 shell 兼容性问题）
            self.delete_remote_file(flag_file)

            flag_dir = self._remote_dirname(flag_file)
            self._ensure_remote_dir(flag_dir)

            task_hash = hashlib.md5(
                f"{time.time_ns()}:{command}:{flag_file}".encode("utf-8")
            ).hexdigest()[:12]
            task_name = f"AutoFluid_{task_hash}"
            script_file = f"{flag_dir}/autofluid_bg_{task_hash}.cmd"
            log_file = f"{flag_dir}/autofluid_bg_{task_hash}.log"
            script = self._build_background_cmd_script(
                command, flag_file, log_file, task_name=task_name
            )
            self._write_remote_text_file(script_file, script)

            script_cmd_path = script_file.replace("/", "\\")
            create_cmd = (
                f'schtasks /Create /TN "{task_name}" /SC ONCE /ST 23:59 '
                f'/TR "{script_cmd_path}" /F'
            )
            _, stderr, exit_code = self.exec_command(create_cmd, timeout=30)
            if exit_code != 0:
                logger.error(f"[SSH] 创建远程计划任务失败 (exit={exit_code}): {stderr[:200]}")
                return False

            run_cmd = f'schtasks /Run /TN "{task_name}"'
            _, stderr, exit_code = self.exec_command(run_cmd, timeout=30)

            if exit_code == 0:
                log_display = log_file.replace("/", "\\")
                logger.info(
                    f"[SSH] 远程后台任务已启动: {task_name} "
                    f"(日志: {log_display})"
                )
                return True
            else:
                logger.error(f"[SSH] 远程后台任务启动失败 (exit={exit_code}): {stderr[:200]}")
                return False
        except (paramiko.SSHException, OSError, EOFError) as e:
            logger.error(f"[SSH] 启动远程后台任务异常: {e}")
            return False

    @staticmethod
    def _build_background_cmd_script(
        command: str,
        flag_file: str,
        log_file: str,
        task_name: str | None = None,
    ) -> str:
        """构造计划任务实际执行的 cmd 脚本。"""
        cmd_flag = flag_file.replace("/", "\\")
        cmd_error_flag = f"{cmd_flag}.error"
        cmd_log = log_file.replace("/", "\\")
        cleanup_line = ""
        if task_name:
            cleanup_line = f'schtasks /Delete /TN "{task_name}" /F >nul 2>&1\r\n'
        return (
            "@echo off\r\n"
            "setlocal\r\n"
            f"{command} >> \"{cmd_log}\" 2>&1\r\n"
            "set \"AF_EXIT=%ERRORLEVEL%\"\r\n"
            "if \"%AF_EXIT%\"==\"0\" (\r\n"
            f"  echo done > \"{cmd_flag}\"\r\n"
            ") else (\r\n"
            f"  echo error %AF_EXIT% > \"{cmd_error_flag}\"\r\n"
            ")\r\n"
            f"{cleanup_line}"
            "exit /b %AF_EXIT%\r\n"
        )

    @staticmethod
    def _remote_dirname(remote_path: str) -> str:
        """返回远程路径父目录，统一为 SFTP 兼容的正斜杠。"""
        normalized = remote_path.replace("\\", "/")
        return normalized.rsplit("/", 1)[0] if "/" in normalized else "."

    def _write_remote_text_file(self, remote_path: str, content: str) -> None:
        """通过 SFTP 写入远程 UTF-8 文本文件。"""
        if not self.ensure_connected():
            raise ConnectionError("SSH 未连接")
        if self._sftp is None:
            raise ConnectionError("SFTP 未连接")
        normalized = remote_path.replace("\\", "/")
        self._ensure_remote_dir(self._remote_dirname(normalized))
        with self._sftp.open(normalized, "wb") as remote_file:
            remote_file.write(content.encode("utf-8"))

    def wait_for_flag(
        self,
        flag_file: str,
        timeout: int = 3600,
        poll_interval: int = 10,
        paused_event=None,
        stopped_event=None,
    ) -> bool:
        """
        轮询等待远程标志文件出现（表示任务完成）。

        支持通过 paused_event / stopped_event 响应外部暂停/停止指令，
        在轮询间隔中检查这些事件，避免 pause 后仍持续轮询直至超时。

        Args:
            flag_file: 标志文件路径
            timeout: 最大等待时间（秒）
            poll_interval: 轮询间隔（秒）
            paused_event: 可选的 threading.Event，set 时暂停轮询
            stopped_event: 可选的 threading.Event，set 时提前退出

        Returns:
            True 表示标志文件已出现（任务完成），False 表示超时或外部停止
        """
        logger.info(f"[SSH] 等待远程任务完成，标志文件: {flag_file}")
        start_time = time.time()

        while time.time() - start_time < timeout:
            # ★ 响应暂停指令：暂停期间不消耗超时配额
            if paused_event is not None:
                while paused_event.is_set():
                    if stopped_event is not None and stopped_event.is_set():
                        logger.info("[SSH] 等待远程任务期间收到停止指令，提前退出")
                        return False
                    time.sleep(1)

            # ★ 响应停止指令
            if stopped_event is not None and stopped_event.is_set():
                logger.info("[SSH] 等待远程任务期间收到停止指令，提前退出")
                return False

            if self.check_remote_file(flag_file):
                logger.info("[SSH] 远程任务完成（检测到标志文件）")
                # 清理标志文件（使用 SFTP 协议，与 check_meshing_done 等方法保持一致）
                self.delete_remote_file(flag_file)
                return True

            time.sleep(poll_interval)

        logger.error(f"[SSH] 等待远程任务超时 ({timeout}s): {flag_file}")
        return False

    # ------------------------------------------------------------------
    # 系统自检
    # ------------------------------------------------------------------

    def check_system(self, conda_exe: str = "", conda_env: str = "",
                     remote_dirs: dict | None = None,
                     mpi_bin_dir: str = "",
                     scripts_dir: str = "",
                     script_files: list | None = None,
                     ref_files_dir: str = "",
                     ref_files: list | None = None) -> dict:
        """
        执行远程工作站系统自检。

        Args:
            conda_exe: conda 可执行文件的完整远程路径（用于 SSH 非交互会话中定位 conda）
            conda_env: conda 环境名称（用于检测 Python 版本）
            remote_dirs: 需要检查存在性的远程目录 {显示名: 路径}
            mpi_bin_dir: ANSYS Fluent MPI 安装目录
            scripts_dir: 远程脚本部署目录
            script_files: 需要检查部署的脚本文件列表
            ref_files_dir: 远程引用文件目录
            ref_files: 需要检查的引用文件列表

        Returns:
            包含自检结果的字典
        """
        results: dict[str, Any] = {
            "ssh_connected": self.is_connected(),
            "conda_available": False,
            "python_version": "",
            "disk_space": "",
            "background_processes": [],
            "remote_dirs": [],
            "remote_programs": [],
            "scripts_status": {"total": 0, "deployed": 0, "missing": []},
            "ref_files_status": {"total": 0, "deployed": 0, "missing": []},
        }

        if not self.ensure_connected():
            return results

        # ---- Conda 检查 ----
        if conda_exe:
            out, err, code = self.exec_command(f'if exist "{conda_exe}" (echo found)')
            results["conda_available"] = (code == 0 and "found" in out)
        else:
            out, err, code = self.exec_command("where conda")
            results["conda_available"] = (code == 0)

        results["remote_programs"].append({
            "label": "Conda可执行文件",
            "path": conda_exe or "(PATH)",
            "exists": results["conda_available"],
        })

        # Conda 环境
        conda_env_ok = False
        if conda_exe and conda_env and results["conda_available"]:
            out, err, code = self.exec_command(
                f'"{conda_exe}" run -n {conda_env} python --version'
            )
            if code == 0 and out.strip():
                results["python_version"] = out.strip()
                conda_env_ok = True

        results["remote_programs"].append({
            "label": "Conda环境",
            "path": conda_env or "(未设置)",
            "exists": conda_env_ok,
        })

        # Python 版本（回退系统 PATH）
        if not results["python_version"]:
            out, err, code = self.exec_command("python --version")
            if code == 0:
                results["python_version"] = out.strip()

        # ---- 远程目录 + MPI 逐个检查（独立 SSH 调用） ----
        # 注意：不能将多个 @if exist 用 & 拼接为单条命令——
        # cmd.exe 的 else 分支会吞噬行内 & 后续的所有命令，
        # 导致仅第一个检查执行，其余全部被跳过。
        all_dirs: dict[str, str] = dict(remote_dirs) if remote_dirs else {}
        if mpi_bin_dir:
            all_dirs["MPI安装目录"] = mpi_bin_dir

        for label, path in all_dirs.items():
            cmd = f'@if exist "{path}" (echo 1) else (echo 0)'
            out, err, code = self.exec_command(cmd)
            exists = code == 0 and out.strip() == "1"
            logger.debug(f"[SSH] 目录检查: {label} ({path}) → exists={exists}")
            entry = {"label": label, "path": path, "exists": exists}
            if label == "MPI安装目录":
                results["remote_programs"].append(entry)
            else:
                results["remote_dirs"].append(entry)

        # ---- 脚本部署逐个检查（独立 SSH 调用） ----
        if scripts_dir and script_files:
            results["scripts_status"]["total"] = len(script_files)
            missing = []
            for filename in script_files:
                remote_path = f"{scripts_dir}/{filename}".replace("\\", "/")
                cmd = f'@if exist "{remote_path}" (echo 1) else (echo 0)'
                out, err, code = self.exec_command(cmd)
                if code != 0 or out.strip() != "1":
                    missing.append(filename)
            results["scripts_status"]["missing"] = missing
            results["scripts_status"]["deployed"] = len(script_files) - len(missing)

        # ---- 引用文件部署逐个检查（独立 SSH 调用） ----
        if ref_files_dir and ref_files:
            results["ref_files_status"]["total"] = len(ref_files)
            missing = []
            for filename in ref_files:
                remote_path = f"{ref_files_dir}/{filename}".replace("\\", "/")
                cmd = f'@if exist "{remote_path}" (echo 1) else (echo 0)'
                out, err, code = self.exec_command(cmd)
                if code != 0 or out.strip() != "1":
                    missing.append(filename)
            results["ref_files_status"]["missing"] = missing
            results["ref_files_status"]["deployed"] = len(ref_files) - len(missing)

        # ---- 磁盘空间 ----
        out, err, code = self.exec_command("wmic logicaldisk where DeviceID='D:' get FreeSpace,Size")
        if code == 0:
            results["disk_space"] = self._parse_disk_space(out)

        # ---- 后台进程 ----
        out, err, code = self.exec_command(
            'tasklist /FI "IMAGENAME eq python.exe" /FO CSV /NH'
        )
        if code == 0 and out.strip():
            results["background_processes"] = [
                line.split(',')[0].strip('"') for line in out.strip().split('\n') if line.strip()
            ]

        return results

    # ------------------------------------------------------------------
    # 目录上传与文件哈希
    # ------------------------------------------------------------------

    def upload_directory(self, local_dir: str, remote_dir: str, max_retries: int = 3) -> bool:
        """递归上传本地目录到远程工作站。

        Args:
            local_dir: 本地目录路径
            remote_dir: 远程目录路径
            max_retries: 每个文件的最大重试次数

        Returns:
            上传成功返回 True，失败返回 False
        """
        if not os.path.isdir(local_dir):
            logger.error(f"[SSH] 本地目录不存在: {local_dir}")
            return False

        try:
            # 确保远程目录存在
            remote_dir_normalized = remote_dir.replace("\\", "/")
            self._ensure_remote_dir(remote_dir_normalized)

            # 遍历本地目录
            for root, dirs, files in os.walk(local_dir):
                # 计算相对路径
                rel_path = os.path.relpath(root, local_dir)
                if rel_path == ".":
                    remote_subdir = remote_dir_normalized
                else:
                    remote_subdir = f"{remote_dir_normalized}/{rel_path.replace(os.sep, '/')}"

                # 创建远程子目录
                if files:  # 仅在有文件时创建目录
                    self._ensure_remote_dir(remote_subdir)

                # 上传文件
                for filename in files:
                    local_file = os.path.join(root, filename)
                    remote_file = f"{remote_subdir}/{filename}"
                    if not self.upload_file(local_file, remote_file, max_retries):
                        logger.error(f"[SSH] 上传文件失败: {local_file}")
                        return False

            logger.info(f"[SSH] 目录上传完成: {local_dir} -> {remote_dir}")
            return True

        except (OSError, paramiko.SSHException, EOFError) as e:
            logger.error(f"[SSH] 目录上传异常: {e}")
            return False

    def get_remote_file_hash(self, remote_path: str) -> str | None:
        """获取远程文件的 MD5 哈希值。

        使用 PowerShell Get-FileHash 命令计算远程文件的哈希值。

        Args:
            remote_path: 远程文件路径

        Returns:
            文件的 MD5 哈希值（小写十六进制字符串），失败返回 None
        """
        if not self.ensure_connected():
            return None

        # 使用 PowerShell 计算文件哈希
        normalized_path = remote_path.replace("\\", "/")
        ps_command = f'Get-FileHash -Path "{normalized_path}" -Algorithm MD5 | Select-Object -ExpandProperty Hash'
        out, err, code = self.exec_command(f'powershell -Command "{ps_command}"', timeout=30)

        if code == 0 and out.strip():
            # 返回小写的哈希值
            return out.strip().lower()
        else:
            logger.debug(f"[SSH] 获取远程文件哈希失败 (可能文件不存在): {remote_path}")
            return None

    def get_remote_file_hashes(self, remote_dir: str, filenames: list) -> dict:
        """批量获取远程文件的 MD5 哈希值。

        Args:
            remote_dir: 远程目录路径
            filenames: 文件名列表

        Returns:
            字典，键为文件名，值为 MD5 哈希值（文件不存在则值为 None）
        """
        result = {}
        remote_dir_normalized = remote_dir.replace("\\", "/")

        for filename in filenames:
            remote_path = f"{remote_dir_normalized}/{filename}"
            result[filename] = self.get_remote_file_hash(remote_path)

        return result

    def get_remote_combined_file_hash(
        self, remote_dir: str, filenames: list
    ) -> str | None:
        """获取远程目录中所有指定文件的组合 MD5 哈希值（单次 SSH 调用）。

        在远程执行 PowerShell 脚本：逐文件计算 MD5 → 按文件名排序 →
        拼接为 "name:hash|..." 格式 → 计算组合 MD5。
        用于两级哈希校验的第一级快速比对。

        Args:
            remote_dir: 远程目录路径
            filenames: 文件名列表

        Returns:
            组合 MD5 哈希值（小写十六进制字符串），失败返回 None
        """
        if not self.ensure_connected():
            return None

        remote_dir_normalized = remote_dir.replace("\\", "/")
        # 对路径和文件名中的单引号进行 PowerShell 转义（'' → '）
        escaped_dir = remote_dir_normalized.replace("'", "''")
        escaped_files = [f.replace("'", "''") for f in filenames]

        # 构建 PowerShell 脚本：逐文件计算哈希 → 排序拼接 → 组合 MD5
        file_list_ps = ",".join(f"'{f}'" for f in escaped_files)
        ps_script = (
            f"$files = @({file_list_ps}); "
            f"$dir = '{escaped_dir}'; "
            f"$results = @(); "
            f"foreach ($f in $files) {{ "
            f"  $p = Join-Path $dir $f; "
            f"  if (Test-Path $p) {{ "
            f"    $h = (Get-FileHash -Path $p -Algorithm MD5).Hash.ToLower(); "
            f"    $results += ($f + ':' + $h) "
            f"  }} else {{ "
            f"    $results += ($f + ':MISSING') "
            f"  }} "
            f"}}; "
            f"$combined = ($results | Sort-Object) -join '|'; "
            f"$bytes = [System.Text.Encoding]::UTF8.GetBytes($combined); "
            f"$md5 = [System.Security.Cryptography.MD5]::Create().ComputeHash($bytes); "
            f"[System.BitConverter]::ToString($md5).Replace('-','').ToLower()"
        )

        # ★ 使用 -EncodedCommand + Base64 传递脚本，消除所有转义/引号问题：
        #   - $ 符号不会被外层 shell 意外展开
        #   - 无需处理嵌套双引号/单引号的转义
        #   - 命令字符串仅含 [A-Za-z0-9+/=]，通过 SSH/cmd.exe 时零歧义
        script_bytes = ps_script.encode('utf-16-le')
        encoded = base64.b64encode(script_bytes).decode('ascii')
        out, err, code = self.exec_command(
            f'powershell -EncodedCommand {encoded}', timeout=60
        )

        if code == 0 and out.strip():
            return out.strip()
        else:
            logger.debug(
                f"[SSH] 获取远程组合哈希失败: {remote_dir}, err={err}"
            )
            return None
