"""
===============================================================================
IPC 客户端 (IPC Client)
TUI 客户端使用此模块与后台 Daemon 通信。

提供同步请求-响应模式，自动处理连接、超时和重连。
===============================================================================
"""
import socket
import threading
import time
from typing import Any, Dict, Optional, Tuple

from ipc.protocol import (
    create_request, serialize, deserialize, create_response,
    CMD_START, CMD_PAUSE, CMD_STOP, CMD_CHECK,
    CMD_RESET_STEP, CMD_CLEAN_STEP,
    CMD_GET_ALL_STATUS, CMD_GET_STATISTICS, CMD_GET_ENGINE_STATUS,
)
from engine.config import IPC_CONFIG
from utils.logger import setup_logger

logger = setup_logger(__name__)


class IPCClient:
    """
    IPC 客户端。

    连接到 Daemon 的 IPC 服务器，发送命令并接收响应。
    支持自动重连。线程安全：所有 socket 操作受 _send_lock 保护。
    """

    def __init__(self, host: str = None, port: int = None):
        """
        初始化 IPC 客户端。

        Args:
            host: Daemon 的 IPC 地址
            port: Daemon 的 IPC 端口
        """
        self.host = host or IPC_CONFIG["host"]
        self.port = port or IPC_CONFIG["port"]
        self._socket: Optional[socket.socket] = None
        self._timeout = IPC_CONFIG["timeout"]
        self._send_lock = threading.RLock()  # 可重入锁：串行化所有 socket 操作，防止多线程竞争

    # ------------------------------------------------------------------
    # 连接管理
    # ------------------------------------------------------------------

    def connect(self) -> bool:
        """连接到 Daemon IPC 服务器。"""
        with self._send_lock:
            if self._socket is not None:
                return True  # 已连接
            try:
                self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                self._socket.settimeout(self._timeout)
                self._socket.connect((self.host, self.port))
                logger.info(f"已连接到 Daemon: {self.host}:{self.port}")
                return True
            except (ConnectionRefusedError, socket.timeout, OSError) as e:
                logger.error(f"无法连接到 Daemon: {e}")
                self._socket = None
                return False

    def disconnect(self):
        """断开与 Daemon 的连接。"""
        with self._send_lock:
            if self._socket:
                try:
                    self._socket.close()
                except OSError as e:
                    logger.debug(f"关闭 socket 时出现异常: {e}")
                self._socket = None

    def is_connected(self) -> bool:
        """检查是否已连接到 Daemon。"""
        return self._socket is not None

    # ------------------------------------------------------------------
    # 请求发送
    # ------------------------------------------------------------------

    def send_request(self, command: str, params: Dict[str, Any] = None) -> Tuple[bool, Any, str]:
        """
        发送命令并等待响应。

        Args:
            command: 命令名
            params: 参数字典

        Returns:
            (success, data, message) 元组
        """
        with self._send_lock:
            if not self._socket:
                if not self.connect():
                    return False, None, "未连接到后台引擎"

            request = create_request(command, params)

            try:
                # 发送请求
                self._socket.sendall(serialize(request))

                # 接收响应（带超时保护）
                buffer = b""
                deadline = time.monotonic() + self._timeout
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        # 超时：断开并重建 socket（防止残留数据污染后续请求）
                        self.disconnect()
                        return False, None, "请求超时"
                    try:
                        self._socket.settimeout(remaining)
                        chunk = self._socket.recv(4096)
                        if not chunk:
                            raise ConnectionError("连接已断开")
                        buffer += chunk
                        if b"\n" in buffer:
                            break
                    except socket.timeout:
                        # 累计超时：断开并重建 socket
                        self.disconnect()
                        return False, None, "请求超时"

                # 解析响应
                response = deserialize(buffer)
                if response is None:
                    return False, None, "无效的响应格式"

                ok = response.get("status") == "ok"
                data = response.get("data")
                message = response.get("message", "")
                return ok, data, message

            except (ConnectionError, OSError) as e:
                logger.error(f"IPC 通信异常: {e}")
                self.disconnect()  # 确保关闭 socket，避免资源泄漏
                return False, None, f"通信异常: {e}"

    # ------------------------------------------------------------------
    # 便捷方法
    # ------------------------------------------------------------------

    def start_pipeline(self) -> Tuple[bool, str]:
        """发送 start 命令。"""
        ok, data, msg = self.send_request(CMD_START)
        return ok, msg

    def pause_pipeline(self) -> Tuple[bool, str]:
        """发送 pause 命令。"""
        ok, data, msg = self.send_request(CMD_PAUSE)
        return ok, msg

    def full_quit(self) -> Tuple[bool, str]:
        """发送 stop (full_quit) 命令。"""
        ok, data, msg = self.send_request(CMD_STOP)
        return ok, msg

    def check_system(self) -> Tuple[bool, Any, str]:
        """发送 check 命令。"""
        return self.send_request(CMD_CHECK)

    def get_all_status(self) -> Tuple[bool, Any, str]:
        """获取所有状态。"""
        return self.send_request(CMD_GET_ALL_STATUS)

    def get_statistics(self) -> Tuple[bool, Any, str]:
        """获取统计信息。"""
        return self.send_request(CMD_GET_STATISTICS)

    def get_engine_status(self) -> Tuple[bool, Any, str]:
        """获取引擎状态。"""
        return self.send_request(CMD_GET_ENGINE_STATUS)

    def reset_step(self, config_name, step_name: str = None) -> Tuple[bool, str]:
        """
        重置步骤。

        Args:
            config_name: 构型名称 (int) 或 "all" 表示全部构型
            step_name: 步骤名，None 或 "all" 表示全部步骤
        """
        ok, data, msg = self.send_request(CMD_RESET_STEP, {
            "config_name": config_name,
            "step_name": step_name,
        })
        return ok, msg

    def clean_step(self, step_name, config_name=None) -> Tuple[bool, str]:
        """
        清理步骤文件。

        Args:
            step_name: 步骤名，或 "all" 表示全部步骤
            config_name: 构型名称 (int)，None 或 "all" 表示全部构型
        """
        ok, data, msg = self.send_request(CMD_CLEAN_STEP, {
            "step_name": step_name,
            "config_name": config_name,
        })
        return ok, msg
