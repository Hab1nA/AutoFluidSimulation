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
import os
import socket
import time
from typing import Optional

try:
    import paramiko
except ImportError:  # pragma: no cover
    paramiko = None
from utils.logger import setup_logger

logger = setup_logger(__name__)

# PowerShell 完整路径（SSH 非交互会话 PATH 不含此目录，需用完整路径定位）
_PS_EXE = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"


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
        self._ssh: Optional[paramiko.SSHClient] = None
        self._sftp: Optional[paramiko.SFTPClient] = None

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
            logger.info(f"SSH 连接成功: {self.username}@{self.host}:{self.port}")
            return True
        except (paramiko.SSHException, OSError, EOFError) as e:
            logger.error(f"SSH 连接失败: {e}")
            self._ssh = None
            self._sftp = None
            return False

    def disconnect(self):
        """断开 SSH 连接。"""
        if self._sftp:
            try:
                self._sftp.close()
            except (OSError, EOFError) as e:
                logger.warning(f"SFTP 关闭异常: {e}")
            finally:
                self._sftp = None
        if self._ssh:
            try:
                self._ssh.close()
            except (OSError, EOFError) as e:
                logger.warning(f"SSH 关闭异常: {e}")
            finally:
                self._ssh = None
        logger.info("SSH 连接已断开")

    def is_connected(self) -> bool:
        """检查 SSH 是否已连接。"""
        return self._ssh is not None and self._ssh.get_transport() is not None and self._ssh.get_transport().is_active()

    def ensure_connected(self) -> bool:
        """确保连接有效，若断开则自动重连。"""
        if not self.is_connected():
            logger.info("SSH 已断开，尝试重新连接...")
            return self.connect()
        return True

    # ------------------------------------------------------------------
    # 文件传输
    # ------------------------------------------------------------------

    def upload_file(self, local_path: str, remote_path: str, max_retries: int = 3) -> bool:
        """通过 SFTP 上传文件到远程工作站（带重试机制）。

        Args:
            local_path: 本地文件路径
            remote_path: 远程文件路径
            max_retries: 最大重试次数

        Returns:
            上传成功返回 True，失败返回 False
        """
        for attempt in range(max_retries):
            try:
                # 每次尝试前确保连接有效（解决竞态条件）
                if not self.ensure_connected():
                    if attempt < max_retries - 1:
                        logger.warning(f"连接失败，{attempt + 1}/{max_retries} 重试...")
                        time.sleep(0.5 * (2 ** attempt))  # 指数退避
                        continue
                    return False

                remote_dir = os.path.dirname(remote_path)
                self._ensure_remote_dir(remote_dir)

                logger.info(f"正在上传: {local_path} -> {remote_path}")
                # 再次确认 SFTP 连接有效（防止类型检查器报错）
                assert self._sftp is not None, "SFTP 连接已断开"
                self._sftp.put(local_path, remote_path)
                logger.info(f"上传完成: {os.path.basename(local_path)}")
                return True
            except (paramiko.SSHException, OSError, EOFError) as e:
                logger.error(f"文件上传失败 (尝试 {attempt + 1}/{max_retries}): {e}")
                self.disconnect()
                if attempt < max_retries - 1:
                    time.sleep(0.5 * (2 ** attempt))  # 指数退避
                else:
                    return False
        return False

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
                logger.debug(f"创建远程目录: {remote_dir}")
            except OSError as e:
                # 检查是否因目录已存在而失败（并发创建场景）
                try:
                    self._sftp.stat(remote_dir)
                    logger.debug(f"远程目录已存在（并发创建）: {remote_dir}")
                except FileNotFoundError:
                    # 目录确实不存在但创建失败 → 真实错误
                    logger.error(f"无法创建远程目录 {remote_dir}: {e}")
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
            logger.warning(f"检查远程文件异常: {remote_path}: {e}")
            return False

    def delete_remote_file(self, remote_path: str) -> bool:
        """
        删除远程工作站上的单个文件。

        Args:
            remote_path: 远程文件完整路径

        Returns:
            True 表示删除成功或文件本就不存在
        """
        if not self.ensure_connected():
            return False
        try:
            # 使用 if exist + del /f 安全删除（/f 强制只读文件删除）
            escaped = remote_path.replace('"', '\\"')
            self.exec_command(f'if exist "{escaped}" del /f "{escaped}"')
            logger.info(f"远程文件已删除: {remote_path}")
            return True
        except (paramiko.SSHException, OSError, EOFError) as e:
            logger.error(f"远程文件删除失败: {remote_path}: {e}")
            return False

    # ------------------------------------------------------------------
    # 远程命令执行
    # ------------------------------------------------------------------

    def exec_command(self, command: str, timeout: int = 30) -> tuple:
        """在远程工作站执行命令（同步等待完成）。"""
        if not self.ensure_connected():
            return ("", "SSH 未连接", -1)
        try:
            logger.debug(f"远程执行: {command}")
            stdin, stdout, stderr = self._ssh.exec_command(command, timeout=timeout)  # type: ignore[union-attr]
            exit_code = stdout.channel.recv_exit_status()
            out_raw = stdout.read()
            err_raw = stderr.read()
            out = self._decode_remote_output(out_raw)
            err = self._decode_remote_output(err_raw)
            return (out, err, exit_code)
        except (paramiko.SSHException, paramiko.AuthenticationException) as e:
            logger.error(f"SSH 认证或协议异常: {e}")
            self.disconnect()
            return ("", str(e), -1)
        except socket.timeout:
            logger.error("远程命令执行超时")
            return ("", "命令执行超时", -1)
        except OSError as e:
            logger.error(f"远程命令执行失败: {e}")
            self.disconnect()
            return ("", str(e), -1)
        except EOFError as e:
            logger.error(f"SSH 连接已断开: {e}")
            self.disconnect()
            return ("", str(e), -1)
        except Exception as e:
            logger.error(f"远程命令执行未知异常: {e}")
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

        使用 PowerShell -EncodedCommand + Start-Process 启动新的 Windows 进程，
        该进程不依附于 SSH 会话，SSH 断开后继续运行。
        任务完成后会创建指定的标志文件。

        通过 -EncodedCommand（UTF-16LE + Base64）传递 PowerShell 脚本，
        彻底避免 SSH → cmd.exe → PowerShell 之间的引号嵌套问题。

        Args:
            command: 要执行的命令（如 conda run ... python script.py 5）
            flag_file: 任务完成标志文件路径（远程路径）

        Returns:
            True 表示后台进程启动成功
        """
        if not self.ensure_connected():
            return False

        ps_script = self._build_background_ps_script(command, flag_file)

        # -EncodedCommand 接受 UTF-16LE + Base64 编码的脚本
        # 完全绕过 SSH → cmd.exe 的引号转义链
        encoded = base64.b64encode(ps_script.encode('utf-16-le')).decode('ascii')
        full_command = f'"{_PS_EXE}" -NoProfile -EncodedCommand {encoded}'

        logger.info(f"启动远程后台任务: {command}")
        logger.debug(f"标志文件: {flag_file}")

        try:
            # 先清理旧的标志文件
            self.exec_command(f'if exist "{flag_file}" del /f "{flag_file}"')

            # 通过 PowerShell 启动后台进程
            _, stderr, exit_code = self.exec_command(full_command, timeout=15)

            if exit_code == 0:
                logger.info("远程后台任务已启动")
                return True
            else:
                logger.error(f"远程后台任务启动失败 (exit={exit_code}): {stderr[:200]}")
                return False
        except (paramiko.SSHException, OSError, EOFError) as e:
            logger.error(f"启动远程后台任务异常: {e}")
            return False

    @staticmethod
    def _build_background_ps_script(command: str, flag_file: str) -> str:
        """构造用于 Start-Process 后台启动的 PowerShell 脚本。"""
        # 转义单引号（PowerShell 单引号字符串中 ' 需写成 ''）
        safe_cmd = command.replace("'", "''")
        safe_flag = flag_file.replace("'", "''")
        # 使用 -f 占位符替换可直接生成双引号字符，避免在单引号字符串中引入 \"
        # 等字面量导致 cmd.exe 引号结构损坏。
        return (
            f"$c = '{safe_cmd}'\n"
            f"$f = '{safe_flag}'\n"
            "$inner = '\"{0} && echo done > \"\"{1}\"\"\"' -f $c, $f\n"
            'Start-Process -FilePath cmd.exe -ArgumentList "/c $inner" -WindowStyle Hidden'
        )

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
        logger.info(f"等待远程任务完成，标志文件: {flag_file}")
        start_time = time.time()

        while time.time() - start_time < timeout:
            # ★ 响应暂停指令：暂停期间不消耗超时配额
            if paused_event is not None:
                while paused_event.is_set():
                    if stopped_event is not None and stopped_event.is_set():
                        logger.info("等待远程任务期间收到停止指令，提前退出")
                        return False
                    time.sleep(1)

            # ★ 响应停止指令
            if stopped_event is not None and stopped_event.is_set():
                logger.info("等待远程任务期间收到停止指令，提前退出")
                return False

            if self.check_remote_file(flag_file):
                logger.info("远程任务完成（检测到标志文件）")
                # 清理标志文件
                self.exec_command(f'if exist "{flag_file}" del /f "{flag_file}"')
                return True

            time.sleep(poll_interval)

        logger.error(f"等待远程任务超时 ({timeout}s): {flag_file}")
        return False

    # ------------------------------------------------------------------
    # 系统自检
    # ------------------------------------------------------------------

    def check_system(self, conda_exe: str = "", conda_env: str = "") -> dict:
        """
        执行远程工作站系统自检。

        Args:
            conda_exe: conda 可执行文件的完整远程路径（用于 SSH 非交互会话中定位 conda）
            conda_env: conda 环境名称（用于检测 Python 版本）

        Returns:
            包含自检结果的字典
        """
        results = {
            "ssh_connected": self.is_connected(),
            "conda_available": False,
            "python_version": "",
            "disk_space": "",
            "background_processes": [],
        }

        if not self.ensure_connected():
            return results

        # 检查 Conda 是否可用（使用完整路径，SSH 非交互会话 PATH 不含用户级 conda）
        if conda_exe:
            out, err, code = self.exec_command(f'if exist "{conda_exe}" (echo found)')
            results["conda_available"] = (code == 0)
        else:
            # 回退：尝试 where 命令
            out, err, code = self.exec_command("where conda")
            results["conda_available"] = (code == 0)

        # 检查 Python 版本（优先通过 conda 环境，回退系统 PATH）
        if conda_exe and conda_env and results["conda_available"]:
            out, err, code = self.exec_command(
                f'"{conda_exe}" run -n {conda_env} python --version'
            )
            if code == 0 and out.strip():
                results["python_version"] = out.strip()

        if not results["python_version"]:
            out, err, code = self.exec_command("python --version")
            if code == 0:
                results["python_version"] = out.strip()

        # 检查磁盘空间（转换为 GB 显示）
        out, err, code = self.exec_command("wmic logicaldisk where DeviceID='D:' get FreeSpace,Size")
        if code == 0:
            results["disk_space"] = self._parse_disk_space(out)

        # 查询后台 Python 进程
        out, err, code = self.exec_command(
            'tasklist /FI "IMAGENAME eq python.exe" /FO CSV /NH'
        )
        if code == 0 and out.strip():
            results["background_processes"] = [
                line.split(',')[0].strip('"') for line in out.strip().split('\n') if line.strip()
            ]

        return results
