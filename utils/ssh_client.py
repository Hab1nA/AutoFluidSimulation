"""
===============================================================================
SSH 客户端模块 (SSH Client)
基于 paramiko 封装远程 Windows 工作站的 SSH 操作。
关键功能：
- 通过 Windows 计划任务拉起独立后台进程（SSH 断开后进程存活）
- 通过轮询标志文件判断远程任务是否完成
- 文件上传 (SFTP)
===============================================================================
"""
from __future__ import annotations

import base64
import hashlib
import os
import re
import socket
import stat
import time
import threading
import uuid
from typing import Any

try:
    import paramiko
except ImportError:  # pragma: no cover
    paramiko = None
from engine.config import OPERATION_TIMEOUTS
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

    def __init__(
        self,
        host: str,
        port: int,
        username: str,
        password: str,
        key_filename: str | None = None,
        auth_method: str = "password",
    ):
        """
        初始化 SSH 客户端配置。

        Args:
            host: 远程主机 IP
            port: SSH 端口
            username: 用户名
            password: 密码
            key_filename: 私钥文件路径；为空时使用 Paramiko 默认 key/agent
            auth_method: 认证方式，支持 password/key/none
        """
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.key_filename = key_filename or None
        self.auth_method = (auth_method or "password").lower()
        self._ssh: paramiko.SSHClient | None = None
        self._sftp: paramiko.SFTPClient | None = None
        self._task_pid_files: dict[str, str] = {}

    # ------------------------------------------------------------------
    # 连接管理
    # ------------------------------------------------------------------

    def connect(self) -> bool:
        """建立 SSH 连接。"""
        if paramiko is None:
            raise ModuleNotFoundError(
                "未安装依赖 paramiko。请执行: pip install -r requirements.txt"
            )
        transport = None
        try:
            self._ssh = paramiko.SSHClient()
            self._ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            # "none" represents passwordless workstation accounts in config.
            # Windows OpenSSH commonly implements this as password auth with an
            # empty password, not as the SSH protocol "none" auth method.
            password = "" if self.auth_method == "none" else self.password or None
            use_key_auth = self.auth_method == "key" or (
                password is None and self.auth_method != "none"
            )
            key_filename = self.key_filename if use_key_auth else None
            self._ssh.connect(
                hostname=self.host,
                port=self.port,
                username=self.username,
                password=password,
                key_filename=key_filename,
                timeout=OPERATION_TIMEOUTS.get("ssh_connection", 10),
                look_for_keys=use_key_auth,
                allow_agent=use_key_auth,
            )
            transport = self._ssh.get_transport()
            if transport:
                transport.set_keepalive(30)
            self._sftp = self._ssh.open_sftp()
            logger.info(f"[SSH] SSH 连接成功: {self.username}@{self.host}:{self.port}")
            return True
        except (paramiko.SSHException, OSError, EOFError, socket.timeout) as e:
            logger.error(f"[SSH] SSH 连接失败: {e}")
            if transport is not None:
                try:
                    transport.close()
                except (OSError, EOFError, socket.timeout) as close_error:
                    logger.warning(f"[SSH] Transport 关闭异常: {close_error}")
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
            self.disconnect()
            return False
        try:
            transport.send_ignore()
            return True
        except (OSError, EOFError, socket.timeout, Exception):
            self.disconnect()
            return False

    def connection_is_active(self) -> bool:
        """Return cached transport state without proving network reachability."""
        if self._ssh is None:
            return False
        transport = self._ssh.get_transport()
        return bool(transport is not None and transport.is_active())

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
            timeout: SFTP 上传总超时（秒）；包含内部重试和重试等待
            paused_event: 暂停事件；上传前或上传中置位时中断本次上传
            stopped_event: 停止事件；上传前或上传中置位时中断本次上传

        Returns:
            上传成功返回 True，失败返回 False
        """
        deadline = time.monotonic() + timeout if timeout is not None else None

        def _remaining_timeout() -> float | None:
            if deadline is None:
                return None
            return max(0.0, deadline - time.monotonic())

        for attempt in range(max_retries):
            try:
                if stopped_event is not None and stopped_event.is_set():
                    logger.info("[SSH] 文件上传因停止指令取消")
                    return False
                if paused_event is not None and paused_event.is_set():
                    logger.info("[SSH] 文件上传因暂停指令暂缓")
                    return False
                remaining_timeout = _remaining_timeout()
                if remaining_timeout is not None and remaining_timeout <= 0:
                    logger.error(f"[SSH] 文件上传超时: {os.path.basename(local_path)}")
                    return False

                # 每次尝试前确保连接有效（解决竞态条件）
                if not self.ensure_connected():
                    if attempt < max_retries - 1:
                        logger.warning(f"[SSH] 连接失败，{attempt + 1}/{max_retries} 重试...")
                        sleep_seconds = 0.5 * (2 ** attempt)  # 指数退避
                        remaining_timeout = _remaining_timeout()
                        if remaining_timeout is not None:
                            if remaining_timeout <= 0:
                                return False
                            sleep_seconds = min(sleep_seconds, remaining_timeout)
                        time.sleep(sleep_seconds)
                        continue
                    return False

                if self._sftp is None:
                    raise ConnectionError("SFTP 连接已断开，请先调用 connect()")
                channel = self._sftp.get_channel()
                previous_timeout = None
                if remaining_timeout is not None:
                    try:
                        previous_timeout = channel.gettimeout()
                    except AttributeError:
                        previous_timeout = None
                    channel.settimeout(remaining_timeout)

                try:
                    remote_dir = os.path.dirname(remote_path)
                    self._ensure_remote_dir(remote_dir)

                    logger.info(f"[SSH] 正在上传: {local_path} -> {remote_path}")

                    def _check_upload_control(transferred: int, total: int) -> None:
                        if stopped_event is not None and stopped_event.is_set():
                            raise _UploadInterrupted("收到停止指令")
                        if paused_event is not None and paused_event.is_set():
                            raise _UploadInterrupted("收到暂停指令")

                    self._sftp.put(
                        local_path,
                        remote_path,
                        callback=_check_upload_control,
                    )
                finally:
                    if remaining_timeout is not None:
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
                    sleep_seconds = 0.5 * (2 ** attempt)  # 指数退避
                    remaining_timeout = _remaining_timeout()
                    if remaining_timeout is not None:
                        if remaining_timeout <= 0:
                            return False
                        sleep_seconds = min(sleep_seconds, remaining_timeout)
                    time.sleep(sleep_seconds)
                else:
                    return False
        return False

    def _sftp_stat(self, remote_path: str, *, timeout: float | None = None):
        """执行 SFTP stat，可选设置通道超时并在结束后恢复。"""
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
        try:
            return self._sftp.stat(remote_path)
        finally:
            if timeout is not None:
                channel.settimeout(previous_timeout)

    def get_remote_file_size(
        self,
        remote_path: str,
        *,
        timeout: float | None = None,
    ) -> int | None:
        """返回远程文件大小；连接/探测异常时返回 None，文件不存在时抛 FileNotFoundError。"""
        if not self.ensure_connected():
            return None
        if self._sftp is None:
            return None
        try:
            return int(self._sftp_stat(remote_path, timeout=timeout).st_size)
        except FileNotFoundError:
            raise
        except (paramiko.SSHException, OSError, EOFError, socket.timeout) as e:
            logger.warning(f"[SSH] 获取远程文件大小异常: {remote_path}: {e}")
            return None

    def read_remote_text_file(
        self,
        remote_path: str,
        *,
        timeout: float | None = None,
    ) -> str | None:
        """读取远程 UTF-8 小文本文件；不存在或连接异常时返回 None。"""
        if not self.ensure_connected():
            return None
        if self._sftp is None:
            return None
        channel = self._sftp.get_channel()
        previous_timeout = None
        if timeout is not None:
            try:
                previous_timeout = channel.gettimeout()
            except AttributeError:
                previous_timeout = None
            channel.settimeout(timeout)
        try:
            with self._sftp.open(remote_path.replace("\\", "/"), "rb") as remote_file:
                raw: bytes = remote_file.read()
            return raw.decode("utf-8", errors="replace")
        except FileNotFoundError:
            return None
        except (paramiko.SSHException, OSError, EOFError, socket.timeout) as e:
            logger.debug(f"[SSH] 读取远程文本文件失败: {remote_path}: {e}")
            return None
        finally:
            if timeout is not None:
                channel.settimeout(previous_timeout)

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

    def check_remote_file(
        self,
        remote_path: str,
        *,
        timeout: float | None = None,
    ) -> bool:
        """检查远程文件是否存在。"""
        if not self.ensure_connected():
            return False
        try:
            if self._sftp is None:
                return False
            self._sftp_stat(remote_path, timeout=timeout)
            return True
        except FileNotFoundError:
            return False
        except (paramiko.SSHException, OSError, EOFError, socket.timeout) as e:
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
            logger.info(
                f"[SSH] 远程文件已删除: {remote_path}",
                extra={"broadcast": False},
            )
            return True
        except FileNotFoundError:
            # 文件本就不存在，视为成功
            logger.debug(
                f"[SSH] 远程文件不存在（跳过）: {remote_path}",
                extra={"broadcast": False},
            )
            return True
        except (paramiko.SSHException, OSError, EOFError) as e:
            logger.error(f"[SSH] 远程文件删除失败: {remote_path}: {e}")
            return False

    def clear_remote_directory(self, remote_dir: str) -> tuple[int, int]:
        """
        清空远程目录内容但保留目录本身。

        Args:
            remote_dir: 远程目录路径

        Returns:
            (deleted_count, failed_count)
        """
        if not self.ensure_connected():
            return (0, 1)
        if self._sftp is None:
            logger.error(f"[SSH] SFTP 未就绪，无法清空远程目录: {remote_dir}")
            return (0, 1)

        normalized = remote_dir.replace("\\", "/").rstrip("/")
        try:
            deleted_count, failed_count = self._clear_remote_directory_contents(normalized)
        except FileNotFoundError:
            logger.info(f"[SSH] 远程目录不存在（跳过）: {remote_dir}")
            return (0, 0)
        except (paramiko.SSHException, OSError, EOFError) as e:
            logger.error(f"[SSH] 清空远程目录失败: {remote_dir}: {e}")
            return (0, 1)

        logger.info(
            f"[SSH] 远程目录清空完成: {remote_dir}，"
            f"已删除 {deleted_count} 项，失败 {failed_count} 项"
        )
        return (deleted_count, failed_count)

    def _clear_remote_directory_contents(self, remote_dir: str) -> tuple[int, int]:
        """递归删除远程目录内容。调用方必须确保 SFTP 已连接。"""
        if self._sftp is None:
            return (0, 1)

        deleted_count = 0
        failed_count = 0
        for entry in self._sftp.listdir_attr(remote_dir):
            if entry.filename in {".", ".."}:
                continue
            child_path = f"{remote_dir}/{entry.filename}"
            if entry.st_mode is not None and stat.S_ISDIR(entry.st_mode):
                child_deleted, child_failed = self._clear_remote_directory_contents(child_path)
                deleted_count += child_deleted
                failed_count += child_failed
                try:
                    self._sftp.rmdir(child_path)
                    deleted_count += 1
                except (paramiko.SSHException, OSError, EOFError) as e:
                    if self._remove_remote_directory_via_shell(child_path):
                        deleted_count += 1
                    elif child_failed == 0:
                        logger.warning(
                            f"[SSH] 远程子目录已清空但目录本身仍被 Windows 占用，保留: "
                            f"{child_path}: {e}"
                        )
                    else:
                        logger.warning(f"[SSH] 删除远程子目录失败: {child_path}: {e}")
                        failed_count += 1
            else:
                try:
                    self._sftp.remove(child_path)
                    deleted_count += 1
                except (paramiko.SSHException, OSError, EOFError) as e:
                    logger.warning(f"[SSH] 删除远程文件失败: {child_path}: {e}")
                    failed_count += 1
        return (deleted_count, failed_count)

    def _remove_remote_directory_via_shell(self, remote_dir: str) -> bool:
        """Fallback for Windows OpenSSH SFTP rmdir permission quirks."""
        windows_path = remote_dir.replace("/", "\\")
        quoted = windows_path.replace('"', r'\"')
        command = f'cmd /c rmdir "{quoted}"'
        out, err, code = self.exec_command(command, timeout=30)
        if code == 0:
            logger.info(
                f"[SSH] 远程子目录已通过 shell 删除: {remote_dir}",
                extra={"broadcast": False},
            )
            return True
        logger.debug(
            f"[SSH] shell 删除远程子目录失败: {remote_dir}: code={code}, "
            f"stdout={out.strip()}, stderr={err.strip()}",
            extra={"broadcast": False},
        )
        return False

    def list_remote_directory(self, remote_dir: str) -> list[str]:
        """返回远程目录下的直接子项名称；目录不存在时返回空列表。"""
        if not self.ensure_connected():
            return []
        if self._sftp is None:
            logger.error(f"[SSH] SFTP 未就绪，无法列出远程目录: {remote_dir}")
            return []

        normalized = remote_dir.replace("\\", "/").rstrip("/")
        try:
            return [
                entry.filename
                for entry in self._sftp.listdir_attr(normalized)
                if entry.filename not in {".", ".."}
            ]
        except FileNotFoundError:
            logger.info(f"[SSH] 远程目录不存在（跳过列举）: {remote_dir}")
            return []
        except (paramiko.SSHException, OSError, EOFError) as e:
            logger.warning(f"[SSH] 列出远程目录失败: {remote_dir}: {e}")
            return []

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
            self.disconnect()
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
            self.disconnect()
            return ("", str(e), -1)

    @staticmethod
    def _decode_remote_output(raw: bytes) -> str:
        """解码远程 Windows 输出，支持 UTF-16LE（PowerShell SSH 默认）、GBK、UTF-8。

        PowerShell 通过 SSH 输出时默认使用 UTF-16LE 编码（每个 ASCII 字符
        后跟 NUL 字节）。若用 GBK 解码会产生含 NUL 的乱码字符串。
        通过检测 NUL 字节密度识别 UTF-16LE 并优先解码。
        """
        if not raw:
            return ""

        # UTF-16LE BOM 检测 (FF FE)
        if raw[:2] == b"\xff\xfe":
            return raw[2:].decode("utf-16-le", errors="replace")

        # 无 BOM 的 UTF-16LE 启发式检测：
        # ASCII 字符在 UTF-16LE 中表现为 'char\x00' 交替模式，
        # 约 50% 的字节是 NUL。正常 GBK/UTF-8 文本几乎不含 NUL。
        if len(raw) >= 4:
            sample = raw[: min(len(raw), 200)]
            null_ratio = sample.count(0) / len(sample)
            if null_ratio > 0.3:
                try:
                    decoded = raw.decode("utf-16-le")
                    # 去除可能的 BOM 字符
                    if decoded and decoded[0] == "\ufeff":
                        decoded = decoded[1:]
                    return decoded
                except UnicodeDecodeError:
                    pass

        # 原有逻辑：GBK 优先（中文 Windows），回退 UTF-8
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

    def exec_background(
        self,
        command: str,
        flag_file: str,
        *,
        working_dir: str | None = None,
        interactive: bool = False,
    ) -> tuple[bool, str]:
        """
        在远程工作站以独立后台进程方式执行命令。

        使用 Windows 计划任务启动独立进程，避免 SSH 非交互会话中的
        Start-Process 静默失败。任务完成后会创建指定的标志文件。

        Args:
            command: 要执行的命令（如 conda run ... python script.py 5）
            flag_file: 任务完成标志文件路径（远程路径）
            working_dir: 远程工作目录（cmd 脚本 cd /d 到此目录后再执行命令，
                         确保子进程的 CWD 正确；None 则不设置）
            interactive: 是否在当前登录用户的交互式桌面运行。Fluent Meshing
                         需要 GUI 执行 journal 中的 GUI 操作，因此必须启用。

        Returns:
            (success, task_name) 元组：success 表示后台进程是否启动成功，
            task_name 为计划任务名称（可用于 kill_remote_task 终止）
        """
        if not self.ensure_connected():
            return (False, "")

        logger.info(f"[SSH] 启动远程后台任务: {command}")
        logger.debug(f"[SSH] 标志文件: {flag_file}")

        try:
            # 先清理旧的标志文件（使用 SFTP 删除，避免 shell 兼容性问题）
            self.delete_remote_file(flag_file)
            self.delete_remote_file(f"{flag_file}.error")

            flag_dir = self._remote_dirname(flag_file)
            self._ensure_remote_dir(flag_dir)

            task_hash = hashlib.md5(
                f"{time.time_ns()}:{command}:{flag_file}".encode("utf-8")
            ).hexdigest()[:12]
            task_name = f"AutoFluid_{task_hash}"
            script_file = f"{flag_dir}/autofluid_bg_{task_hash}.cmd"
            log_file = f"{flag_dir}/autofluid_bg_{task_hash}.log"
            pid_file = f"{flag_dir}/autofluid_bg_{task_hash}.pid"
            script = self._build_background_cmd_script(
                command, flag_file, log_file, pid_file=pid_file, task_name=task_name,
                working_dir=working_dir, interactive=interactive,
            )
            self._write_remote_text_file(script_file, script)
            self._task_pid_files[task_name] = pid_file

            script_cmd_path = script_file.replace("/", "\\")
            create_cmd = (
                f'schtasks /Create /TN "{task_name}" /SC ONCE /ST 23:59 '
                f'/TR "{script_cmd_path}" /F'
            )
            if interactive:
                create_cmd += " /IT"
            _, stderr, exit_code = self.exec_command(create_cmd, timeout=30)
            if exit_code != 0:
                logger.error(f"[SSH] 创建远程计划任务失败 (exit={exit_code}): {stderr[:200]}")
                return (False, task_name)

            run_cmd = f'schtasks /Run /TN "{task_name}"'
            _, stderr, exit_code = self.exec_command(run_cmd, timeout=30)

            if exit_code == 0:
                disable_cmd = f'schtasks /Change /TN "{task_name}" /DISABLE'
                _, disable_stderr, disable_code = self.exec_command(disable_cmd, timeout=30)
                if disable_code != 0:
                    logger.warning(
                        f"[SSH] 禁用远程计划任务失败，可能在计划时间被二次触发 "
                        f"(exit={disable_code}): {disable_stderr[:200]}"
                    )
                log_display = log_file.replace("/", "\\")
                logger.info(
                    f"[SSH] 远程后台任务已启动: {task_name} "
                    f"(日志: {log_display})"
                )
                return (True, task_name)
            else:
                logger.error(f"[SSH] 远程后台任务启动失败 (exit={exit_code}): {stderr[:200]}")
                self.exec_command(f'schtasks /Delete /TN "{task_name}" /F', timeout=15)
                self._task_pid_files.pop(task_name, None)
                return (False, task_name)
        except (paramiko.SSHException, OSError, EOFError) as e:
            logger.error(f"[SSH] 启动远程后台任务异常: {e}")
            return (False, "")

    def kill_remote_task(self, task_name: str) -> bool:
        """终止远程计划任务及其子进程。

        先按 wrapper 脚本记录的子进程 PID 终止进程树，再结束并删除计划任务。
        用于超时后清理仍在运行的远程进程，避免按镜像名误杀其他 Fluent 任务。

        Args:
            task_name: 计划任务名称（由 exec_background 返回）

        Returns:
            True 表示任务已被终止或本就不存在
        """
        if not task_name:
            return True
        if not self.ensure_connected():
            return False

        pid_file = self._task_pid_files.pop(task_name, None)
        try:
            if pid_file:
                pid = self._read_remote_pid_file(pid_file)
                if pid is not None:
                    kill_cmd = f"taskkill /PID {pid} /T /F"
                    out, err, kill_code = self.exec_command(kill_cmd, timeout=60)
                    no_match = "没有运行的任务匹配指定标准" in out
                    if kill_code != 0 and not no_match:
                        logger.warning(
                            f"[SSH] taskkill PID {pid} 返回非零码 {kill_code}: {err or out}"
                        )
                    elif kill_code == 0:
                        logger.info(f"[SSH] 已按 PID 终止远程任务进程树: {pid}")

            # 先尝试优雅终止（向任务进程发送终止信号）
            end_cmd = f'schtasks /End /TN "{task_name}"'
            _, _, end_code = self.exec_command(end_cmd, timeout=15)

            # 删除计划任务（无论 End 是否成功）
            del_cmd = f'schtasks /Delete /TN "{task_name}" /F'
            _, _, del_code = self.exec_command(del_cmd, timeout=15)

            if pid_file:
                self.delete_remote_file(pid_file)

            if end_code == 0 or del_code == 0:
                logger.info(f"[SSH] 远程任务已终止: {task_name}")
            else:
                logger.debug(f"[SSH] 远程任务已不存在（可能已完成自清理）: {task_name}")
            return True
        except (paramiko.SSHException, OSError, EOFError) as e:
            logger.warning(f"[SSH] 终止远程任务异常 {task_name}: {e}")
            return False

    def cleanup_fluent_processes_for_task(self, task: dict[str, object]) -> bool:
        """Use task-specific command-line evidence to terminate detached Fluent children."""
        if not self.ensure_connected():
            return False
        task_name = str(task.get("task_name") or "").strip()
        config_name = str(task.get("config_name") or "").strip()
        evidence = [
            task_name,
            os.path.basename(str(task.get("script_file") or "")),
            os.path.basename(str(task.get("pid_file") or "")),
            os.path.basename(str(task.get("log_file") or "")),
        ]
        if config_name:
            evidence.extend([
                f"model_gen4_{config_name}",
                f"solver_progress_{config_name}",
                f"solver_done_{config_name}",
                f"postprocess_done_{config_name}",
            ])
        evidence = [item for item in evidence if item]
        if not evidence:
            return True

        def ps_quote(value: str) -> str:
            return "'" + value.replace("'", "''") + "'"

        evidence_array = "@(" + ",".join(ps_quote(item.lower()) for item in evidence) + ")"
        process_names = "@('fluent','cx2410','mpiexec','hydra_pmi_proxy','fl_mpi2410','ansyscl')"
        script = (
            "$ErrorActionPreference='SilentlyContinue';"
            f"$evidence={evidence_array};"
            f"$names={process_names};"
            "$matches=Get-CimInstance Win32_Process | Where-Object {"
            "  $name=([string]$_.Name).ToLower();"
            "  $cmd=([string]$_.CommandLine).ToLower();"
            "  ($names -contains [IO.Path]::GetFileNameWithoutExtension($name)) -and "
            "  ($evidence | Where-Object { $_ -and $cmd.Contains($_) })"
            "};"
            "$matches | ForEach-Object {"
            "  try { Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop; "
            "        Write-Output ('killed=' + $_.ProcessId + ':' + $_.Name) }"
            "  catch { Write-Output ('failed=' + $_.ProcessId + ':' + $_.Name + ':' + $_.Exception.Message) }"
            "}"
        )
        encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
        try:
            out, err, code = self.exec_command(
                f"powershell -NoProfile -ExecutionPolicy Bypass -EncodedCommand {encoded}",
                timeout=60,
            )
            if code != 0:
                logger.warning(
                    "[SSH] Fluent 任务证据清理返回非零码 %s: %s",
                    code,
                    err or out,
                )
                return False
            if out.strip():
                logger.info("[SSH] Fluent 任务证据清理结果: %s", out.strip())
            return True
        except (paramiko.SSHException, OSError, EOFError) as e:
            logger.warning("[SSH] Fluent 任务证据清理异常: %s", e)
            return False

    def cleanup_remote_task_entry(
        self,
        task_name: str,
        pid_file: str | None = None,
    ) -> bool:
        """清理已结束远程任务的计划任务条目和残留 PID 文件。

        完成态任务的 wrapper 会在退出前删除 PID 文件，因此这里不能按 PID
        终止进程树；超时或主动停止仍应使用 kill_remote_task()。
        """
        if not task_name:
            return True
        if not self.ensure_connected():
            return False

        try:
            del_cmd = f'schtasks /Delete /TN "{task_name}" /F'
            _, err, del_code = self.exec_command(del_cmd, timeout=15)
            if pid_file:
                self.delete_remote_file(pid_file)
            if del_code == 0:
                logger.info(f"[SSH] 已清理远程计划任务条目: {task_name}")
            else:
                logger.debug(
                    f"[SSH] 远程计划任务条目已不存在或自清理完成: {task_name} "
                    f"(exit={del_code}): {err[:200]}"
                )
            return True
        except (paramiko.SSHException, OSError, EOFError) as e:
            logger.warning(f"[SSH] 清理远程任务条目异常 {task_name}: {e}")
            return False

    def read_remote_pid_file(self, pid_file: str) -> int | None:
        """读取远程 wrapper 记录的子进程 PID（公共接口）。

        供 RemoteExecutor 等外部模块在需要验证远程进程存活时调用，
        避免通过 getattr 访问私有方法。

        Args:
            pid_file: 远程 PID 文件的路径（支持 / 和 \\ 分隔符）

        Returns:
            解析到的 PID 整数；无法读取或格式无效时返回 None
        """
        return self._read_remote_pid_file(pid_file)

    def _read_remote_pid_file(self, pid_file: str) -> int | None:
        """读取远程 wrapper 记录的子进程 PID。"""
        cmd_pid_file = pid_file.replace("/", "\\")
        out, err, code = self.exec_command(f'cmd /c type "{cmd_pid_file}"', timeout=15)
        if code != 0:
            logger.warning(f"[SSH] 读取远程任务 PID 失败: {pid_file}: {err or out}")
            return None
        cleaned = out.replace("\x00", "").replace("\ufeff", "").strip()
        match = re.search(r"\b\d+\b", cleaned)
        if match is None:
            logger.warning(f"[SSH] 远程任务 PID 文件内容无效: {pid_file}: {cleaned!r}")
            return None
        return int(match.group(0))

    @staticmethod
    def _build_background_cmd_script(
        command: str,
        flag_file: str,
        log_file: str,
        pid_file: str | None = None,
        task_name: str | None = None,
        working_dir: str | None = None,
        interactive: bool = False,
    ) -> str:
        """构造计划任务实际执行的 cmd 脚本。"""
        cmd_flag = flag_file.replace("/", "\\")
        cmd_error_flag = f"{cmd_flag}.error"
        cmd_log = log_file.replace("/", "\\")
        cmd_pid = (pid_file or f"{flag_file}.pid").replace("/", "\\")
        cleanup_line = ""
        if task_name:
            cleanup_line = f'schtasks /Delete /TN "{task_name}" /F >nul 2>&1\r\n'
        cd_line = ""
        if working_dir:
            cmd_working = working_dir.replace("/", "\\")
            cd_line = f'cd /d "{cmd_working}"\r\n'
        if interactive:
            pid_capture_line = (
                "set \"AF_WRAPPER_PID=\"\r\n"
                "for /f \"tokens=2 delims==\" %%P in ('wmic process where "
                "\"Name='cmd.exe' and CommandLine like '%%%~nx0%%' "
                "and not CommandLine like '%%wmic process%%'\" "
                "get ProcessId /value 2^>nul ^| find \"=\"') do "
                "if not defined AF_WRAPPER_PID set \"AF_WRAPPER_PID=%%P\"\r\n"
                "if defined AF_WRAPPER_PID > \"%AF_PID_FILE%\" echo %AF_WRAPPER_PID%\r\n"
            )
            command_runner = (
                f"{pid_capture_line}"
                f"call {command} >> \"{cmd_log}\" 2>&1\r\n"
            )
        else:
            ps_command_arg = command.replace("'", "''")
            ps_script = (
                "$p = Start-Process -FilePath 'cmd.exe' "
                f"-ArgumentList '/d','/s','/c','{ps_command_arg}' "
                "-PassThru -WindowStyle Hidden; "
                "Set-Content -LiteralPath $env:AF_PID_FILE -Value $p.Id -Encoding ascii; "
                "$p.WaitForExit(); exit $p.ExitCode"
            )
            encoded_script = base64.b64encode(ps_script.encode("utf-16le")).decode("ascii")
            command_runner = (
                "powershell -NoProfile -ExecutionPolicy Bypass "
                f"-EncodedCommand {encoded_script} "
                f">> \"{cmd_log}\" 2>&1\r\n"
            )
        return (
            "@echo off\r\n"
            "setlocal\r\n"
            "set PYTHONUTF8=1\r\n"
            "set PYTHONIOENCODING=utf-8\r\n"
            f"set \"AF_PID_FILE={cmd_pid}\"\r\n"
            f"{cd_line}"
            f"{command_runner}"
            "set \"AF_EXIT=%ERRORLEVEL%\"\r\n"
            "if \"%AF_EXIT%\"==\"0\" (\r\n"
            f"  echo done > \"{cmd_flag}\"\r\n"
            ") else (\r\n"
            f"  echo error %AF_EXIT% > \"{cmd_error_flag}\"\r\n"
            ")\r\n"
            "del /f /q \"%AF_PID_FILE%\" >nul 2>&1\r\n"
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
        paused_event: threading.Event | None = None,
        stopped_event: threading.Event | None = None,
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
        error_flag = f"{flag_file}.error"
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

            if self.check_remote_file(error_flag):
                logger.error(f"[SSH] 远程任务执行失败（检测到错误标志文件）: {error_flag}")
                self.delete_remote_file(error_flag)
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
                     remote_dirs: dict[str, str] | None = None,
                     fluent_path: str = "",
                     mpi_bin_dir: str = "",
                     scripts_dir: str = "",
                     script_files: list[str] | None = None,
                     ref_files_dir: str = "",
                     ref_files: list[str] | None = None) -> dict[str, object]:
        """
        执行远程工作站系统自检。

        优化：将大量独立 SSH 调用合并为少量 PowerShell 批量命令，
        避免 23+ 次串行 exec_command 导致 TUI 超时。

        Args:
            conda_exe: conda 可执行文件的完整远程路径
            conda_env: conda 环境名称
            remote_dirs: 需要检查存在性的远程目录 {显示名: 路径}
            fluent_path: Fluent 可执行文件完整路径
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
            results["ssh_connected"] = False
            results["remote_dirs"].extend(
                {"label": label, "path": path, "exists": None}
                for label, path in (remote_dirs or {}).items()
            )
            results["remote_programs"].extend([
                {
                    "label": "Conda可执行文件",
                    "path": conda_exe or "(PATH)",
                    "exists": None,
                },
                {
                    "label": "Conda环境",
                    "path": conda_env or "(未设置)",
                    "exists": None,
                },
            ])
            if fluent_path:
                results["remote_programs"].append({
                    "label": "Fluent可执行文件",
                    "path": fluent_path,
                    "exists": None,
                })
            if mpi_bin_dir:
                results["remote_programs"].append({
                    "label": "MPI安装目录",
                    "path": mpi_bin_dir,
                    "exists": None,
                })
            results["scripts_status"] = {"status": "skipped", "message": "SSH 未连接，未检查"}
            results["ref_files_status"] = {"status": "skipped", "message": "SSH 未连接，未检查"}
            return results
        # ---- Conda 检查（1 次 SSH） ----
        if conda_exe:
            out, _, code = self.exec_command(f'if exist "{conda_exe}" (echo found)')
            results["conda_available"] = (code == 0 and "found" in out)
        else:
            out, _, code = self.exec_command("where conda")
            results["conda_available"] = (code == 0)

        results["remote_programs"].append({
            "label": "Conda可执行文件",
            "path": conda_exe or "(PATH)",
            "exists": results["conda_available"],
        })

        # ---- Conda 环境 + Python 版本（1 次 SSH） ----
        conda_env_ok = False
        if conda_exe and conda_env and results["conda_available"]:
            out, _, code = self.exec_command(
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
            out, _, code = self.exec_command("python --version")
            if code == 0:
                results["python_version"] = out.strip()

        # ---- 批量路径检查（单次 PowerShell 调用） ----
        # 将目录、脚本、引用文件三类路径合并为一次 PowerShell Test-Path 调用，
        # 避免 23+ 次独立 SSH exec_command 导致总耗时超过 TUI 超时。
        all_paths: list[tuple[str, str, str]] = []  # (kind, label, path)
        # kind: "dir" | "program" | "mpi" | "script" | "ref"

        all_dirs: dict[str, str] = dict(remote_dirs) if remote_dirs else {}
        if fluent_path:
            all_paths.append(("program", "Fluent可执行文件", fluent_path))
        if mpi_bin_dir:
            all_dirs["MPI安装目录"] = mpi_bin_dir
        for label, path in all_dirs.items():
            kind = "mpi" if label == "MPI安装目录" else "dir"
            all_paths.append((kind, label, path))

        if scripts_dir and script_files:
            results["scripts_status"]["total"] = len(script_files)
            for filename in script_files:
                remote_path = f"{scripts_dir}/{filename}".replace("\\", "/")
                all_paths.append(("script", filename, remote_path))

        if ref_files_dir and ref_files:
            results["ref_files_status"]["total"] = len(ref_files)
            for filename in ref_files:
                remote_path = f"{ref_files_dir}/{filename}".replace("\\", "/")
                all_paths.append(("ref", filename, remote_path))

        if all_paths:
            # 使用 cmd.exe 批处理文件 + SFTP 写入，彻底绕过命令行长度和编码问题。
            # PowerShell 可能因组策略拒绝执行 .ps1（"拒绝访问"），cmd.exe 无此限制。
            # 每行一个 @if exist 检查（原项目已验证可用），输出 1 或 0。
            bat_lines = []
            for _, _, p in all_paths:
                bat_lines.append(
                    f'@if exist "{p}" (echo 1) else (echo 0)'
                )
            bat_script = "\r\n".join(bat_lines) + "\r\n"

            script_sftp = "C:/Windows/Temp/_af_check_paths.bat"
            # SFTP 路径 → Windows 路径（与 exec_background 中 script_cmd_path 一致）
            script_win = script_sftp.replace("/", "\\")
            out, code = "", -1
            try:
                self._write_remote_text_file(script_sftp, bat_script)
                out, _, code = self.exec_command(
                    f'cmd /c "{script_win}"', timeout=30
                )
            finally:
                try:
                    self.delete_remote_file(script_sftp)
                except (OSError, EOFError):
                    pass

            # 解析结果：每行一个 1 或 0
            flags: list[bool] = []
            if code == 0:
                for line in out.strip().splitlines():
                    cleaned = line.replace("\x00", "").replace("\ufeff", "").strip()
                    if cleaned in ("1", "0"):
                        flags.append(cleaned == "1")

            if len(flags) != len(all_paths):
                logger.error(
                    f"[SSH] 批量路径检查失败 (exit={code}, "
                    f"期望 {len(all_paths)} 行, 实际 {len(flags)} 行, "
                    f"stdout前100字符: {out[:100]!r})"
                )
                # 检查失败时假设所有路径不存在，避免误报"全部通过"
                flags = [False] * len(all_paths)

            # 将结果分发到各个字段
            script_missing: list[str] = []
            ref_missing: list[str] = []
            for (kind, label, path), exists in zip(all_paths, flags):
                if kind in {"mpi", "program"}:
                    results["remote_programs"].append({
                        "label": label, "path": path, "exists": exists,
                    })
                elif kind == "dir":
                    logger.debug(f"[SSH] 目录检查: {label} ({path}) → exists={exists}")
                    results["remote_dirs"].append({
                        "label": label, "path": path, "exists": exists,
                    })
                elif kind == "script":
                    if not exists:
                        script_missing.append(label)
                elif kind == "ref":
                    if not exists:
                        ref_missing.append(label)

            results["scripts_status"]["missing"] = script_missing
            results["scripts_status"]["deployed"] = (
                results["scripts_status"]["total"] - len(script_missing)
            )
            results["ref_files_status"]["missing"] = ref_missing
            results["ref_files_status"]["deployed"] = (
                results["ref_files_status"]["total"] - len(ref_missing)
            )

        # ---- 磁盘空间 + 后台进程（2 次 SSH，已足够快） ----
        # wmic 和 tasklist 各 1 次，无需批量化。
        out, err, code = self.exec_command(
            "wmic logicaldisk where DeviceID='D:' get FreeSpace,Size"
        )
        if code == 0:
            results["disk_space"] = self._parse_disk_space(out)

        out, err, code = self.exec_command(
            'tasklist /FI "IMAGENAME eq python.exe" /FO CSV /NH'
        )
        if code == 0 and out.strip():
            results["background_processes"] = [
                line.split(',')[0].strip('"')
                for line in out.strip().split('\n') if line.strip()
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

        使用 cmd.exe + certutil 计算远程文件哈希，避免依赖 PowerShell。

        Args:
            remote_path: 远程文件路径

        Returns:
            文件的 MD5 哈希值（小写十六进制字符串），失败返回 None
        """
        normalized_path = remote_path.replace("\\", "/")
        remote_dir = self._remote_dirname(normalized_path)
        filename = normalized_path.rsplit("/", 1)[-1]
        hashes = self._get_remote_file_hashes_via_cmd(remote_dir, [filename])
        if hashes is None:
            logger.debug(f"[SSH] 获取远程文件哈希失败: {remote_path}")
            return None
        return hashes[filename]

    def get_remote_file_hashes(
        self, remote_dir: str, filenames: list[str]
    ) -> dict[str, str | None]:
        """批量获取远程文件的 MD5 哈希值。

        Args:
            remote_dir: 远程目录路径
            filenames: 文件名列表

        Returns:
            字典，键为文件名，值为 MD5 哈希值（文件不存在则值为 None）

        Raises:
            ConnectionError: 远程批处理执行失败或输出不完整
        """
        hashes = self._get_remote_file_hashes_via_cmd(remote_dir, filenames)
        if hashes is None:
            raise ConnectionError("批量获取远程文件哈希失败")
        return hashes

    def get_remote_combined_file_hash(
        self, remote_dir: str, filenames: list[str]
    ) -> str | None:
        """获取远程目录中所有指定文件的组合 MD5 哈希值（单次 SSH 调用）。

        在远程通过 cmd.exe + certutil 批量计算各文件 MD5，再在本地按文件名排序，
        拼接为 "name:hash|..." 格式并计算组合 MD5。
        用于两级哈希校验的第一级快速比对。

        Args:
            remote_dir: 远程目录路径
            filenames: 文件名列表

        Returns:
            组合 MD5 哈希值（小写十六进制字符串），失败返回 None
        """
        hashes = self._get_remote_file_hashes_via_cmd(remote_dir, filenames)
        if hashes is None:
            logger.debug(f"[SSH] 获取远程组合哈希失败: {remote_dir}")
            return None
        combined = "|".join(
            f"{name}:{hash_value or 'MISSING'}"
            for name, hash_value in sorted(hashes.items())
        )
        return hashlib.md5(combined.encode("utf-8")).hexdigest()

    def _get_remote_file_hashes_via_cmd(
        self, remote_dir: str, filenames: list[str]
    ) -> dict[str, str | None] | None:
        """通过临时 cmd 批处理和 certutil 一次性计算多个远程文件哈希。"""
        if not self.ensure_connected():
            return None

        remote_dir_normalized = remote_dir.replace("\\", "/")
        bat_lines = ["@echo off"]
        for index, filename in enumerate(filenames):
            remote_path = f"{remote_dir_normalized}/{filename}"
            if '"' in remote_path:
                logger.error(f"[SSH] 远程文件路径包含非法双引号: {remote_path}")
                return None
            escaped_path = remote_path.replace("%", "%%")
            bat_lines.extend([
                f'@if exist "{escaped_path}" (',
                f"  @echo __AF_HASH_BEGIN__{index}",
                f'  @certutil -hashfile "{escaped_path}" MD5',
                f"  @echo __AF_HASH_END__{index}",
                ") else (",
                f"  @echo __AF_HASH_MISSING__{index}",
                ")",
            ])

        script_sftp = f"C:/Windows/Temp/_af_hash_files_{uuid.uuid4().hex}.bat"
        script_win = script_sftp.replace("/", "\\")
        try:
            self._write_remote_text_file(script_sftp, "\r\n".join(bat_lines) + "\r\n")
            out, err, code = self.exec_command(f'cmd /c "{script_win}"', timeout=60)
        finally:
            try:
                self.delete_remote_file(script_sftp)
            except (OSError, EOFError):
                pass

        if code != 0:
            logger.error(f"[SSH] 批量获取远程文件哈希失败 (exit={code}): {err}")
            return None

        return self._parse_remote_file_hashes(out, filenames)

    @staticmethod
    def _parse_remote_file_hashes(
        output: str, filenames: list[str]
    ) -> dict[str, str | None] | None:
        """解析 certutil 批处理输出；缺失文件与异常输出使用不同语义。"""
        cleaned_output = output.replace("\x00", "").replace("\ufeff", "")
        result: dict[str, str | None] = {}
        for index, filename in enumerate(filenames):
            if f"__AF_HASH_MISSING__{index}" in cleaned_output:
                result[filename] = None
                continue

            begin_marker = f"__AF_HASH_BEGIN__{index}"
            end_marker = f"__AF_HASH_END__{index}"
            begin = cleaned_output.find(begin_marker)
            end = cleaned_output.find(end_marker, begin + len(begin_marker))
            if begin == -1 or end == -1:
                logger.error(f"[SSH] 远程文件哈希输出缺少标记: {filename}")
                return None

            section = cleaned_output[begin + len(begin_marker):end]
            match = re.search(r"(?im)^[0-9a-f]{32}\s*$", section)
            if match is None:
                logger.error(f"[SSH] 远程文件哈希输出无法解析: {filename}")
                return None
            result[filename] = match.group(0).strip().lower()
        return result
