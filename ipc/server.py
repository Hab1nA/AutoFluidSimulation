"""
===============================================================================
IPC 服务器模块 (IPC Server)
运行在 Daemon 进程中，监听 TCP Socket，接收 TUI 客户端命令。
每个连接使用独立线程处理，支持多客户端同时连接（但命令执行串行化）。
===============================================================================
"""
import socket
import threading
from typing import Optional, Callable

from ipc.protocol import (
    deserialize, create_response, serialize,
    CMD_START, CMD_PAUSE, CMD_STOP, CMD_CHECK,
    CMD_RESET_STEP, CMD_CLEAN_STEP,
    CMD_GET_ALL_STATUS, CMD_GET_STATISTICS, CMD_GET_ENGINE_STATUS,
    CMD_GET_LOG_ENTRIES, CMD_RELOAD_CONFIG,
)
from engine.config import IPC_CONFIG
from utils.logger import setup_logger

logger = setup_logger(__name__)


class IPCServer:
    """
    IPC 服务器。

    在 Daemon 进程中启动，监听来自 TUI 客户端的命令请求。
    收到命令后，调用注册的回调函数进行处理。
    """

    def __init__(self, host: str = None, port: int = None):
        """
        初始化 IPC 服务器。

        Args:
            host: 监听地址
            port: 监听端口
        """
        self.host = host or IPC_CONFIG["host"]
        self.port = port or IPC_CONFIG["port"]
        self._socket: Optional[socket.socket] = None
        self._running = False
        self._server_thread: Optional[threading.Thread] = None

        # 命令处理器注册表
        self._handlers: dict[str, Callable] = {}

    # ------------------------------------------------------------------
    # 命令处理器注册
    # ------------------------------------------------------------------

    def register_handler(self, command: str, handler: Callable):
        """
        注册命令处理器。

        Args:
            command: 命令名
            handler: 处理函数，签名为 handler(params: dict) -> (ok: bool, data: any, message: str)
        """
        self._handlers[command] = handler
        logger.debug(f"注册命令处理器: {command}")

    def register_default_handlers(self, daemon):
        """
        注册所有默认命令处理器，绑定到 daemon 实例。

        Args:
            daemon: PipelineDaemon 实例
        """
        # 引擎控制
        self.register_handler(CMD_START, lambda p: daemon.handle_start(p))
        self.register_handler(CMD_PAUSE, lambda p: daemon.handle_pause(p))
        self.register_handler(CMD_STOP, lambda p: daemon.handle_stop(p))
        self.register_handler(CMD_CHECK, lambda p: daemon.handle_check(p))

        # 查询
        self.register_handler(CMD_GET_ALL_STATUS, lambda p: daemon.handle_get_all_status(p))
        self.register_handler(CMD_GET_STATISTICS, lambda p: daemon.handle_get_statistics(p))
        self.register_handler(CMD_GET_ENGINE_STATUS, lambda p: daemon.handle_get_engine_status(p))
        self.register_handler(CMD_GET_LOG_ENTRIES, lambda p: daemon.handle_get_log_entries(p))

        # 重置
        self.register_handler(CMD_RESET_STEP, lambda p: daemon.handle_reset_step(p))

        # 清理
        self.register_handler(CMD_CLEAN_STEP, lambda p: daemon.handle_clean_step(p))

        # 配置重载
        self.register_handler(CMD_RELOAD_CONFIG, lambda p: daemon.handle_reload_config(p))

    # ------------------------------------------------------------------
    # 服务器生命周期
    # ------------------------------------------------------------------

    def start(self):
        """启动 IPC 服务器（在独立线程中运行）。"""
        if self._running:
            logger.warning("IPC 服务器已在运行")
            return

        self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._socket.settimeout(1.0)  # 1秒超时以便检查 _running 标志

        try:
            self._socket.bind((self.host, self.port))
            self._socket.listen(5)
            self._running = True
            self._server_thread = threading.Thread(target=self._accept_loop, daemon=True, name="IPC-Server")
            self._server_thread.start()
            logger.info(f"IPC 服务器已启动: {self.host}:{self.port}")
        except OSError as e:
            logger.error(f"IPC 服务器启动失败（端口可能被占用）: {e}")
            self._running = False
            self._socket.close()
            self._socket = None
            raise

    def stop(self):
        """停止 IPC 服务器。"""
        self._running = False
        if self._socket:
            try:
                self._socket.close()
            except OSError:
                pass
            self._socket = None
        if self._server_thread and self._server_thread.is_alive():
            self._server_thread.join(timeout=3)
        logger.info("IPC 服务器已停止")

    # ------------------------------------------------------------------
    # 连接处理
    # ------------------------------------------------------------------

    def _accept_loop(self):
        """接受客户端连接的主循环。"""
        while self._running:
            try:
                client_sock, addr = self._socket.accept()
                logger.info(f"IPC 客户端连接: {addr}")
                # 每个客户端在独立线程中处理
                client_thread = threading.Thread(
                    target=self._handle_client,
                    args=(client_sock, addr),
                    daemon=True,
                    name=f"IPC-Client-{addr[1]}"
                )
                client_thread.start()
            except socket.timeout:
                continue  # 超时后检查 _running 标志
            except OSError:
                if self._running:
                    logger.error("IPC 服务器 accept 异常")
                break

    def _handle_client(self, client_sock: socket.socket, addr: tuple):
        """
        处理单个客户端连接。

        持续读取命令直到连接断开，每条命令返回一个响应。
        记录是否有过有效消息交互——从未发送有效消息的连接视为探测连接，
        断开时不写入 INFO 日志，避免端口探测工具造成日志噪音。
        """
        client_sock.settimeout(30.0)
        buffer = b""
        has_sent_valid_message = False  # 是否曾处理过有效 IPC 消息

        try:
            while self._running:
                try:
                    data = client_sock.recv(4096)
                    if not data:
                        break  # 客户端断开
                    buffer += data

                    # 按换行符分割消息
                    while b"\n" in buffer:
                        line, buffer = buffer.split(b"\n", 1)
                        response = self._process_message(line)
                        if response:
                            has_sent_valid_message = True
                            client_sock.sendall(serialize(response))
                except socket.timeout:
                    continue
                except (ConnectionError, OSError) as e:
                    logger.error(f"处理客户端消息异常: {e}")
                    break
        finally:
            try:
                client_sock.close()
            except OSError:
                pass
            if has_sent_valid_message:
                logger.info(f"IPC 客户端断开: {addr}")
            else:
                logger.debug(f"IPC 客户端断开 (探测连接): {addr}")

    def _process_message(self, data: bytes) -> Optional[dict]:
        """
        处理单条消息。

        Args:
            data: 原始字节数据

        Returns:
            响应消息字典
        """
        msg = deserialize(data)
        if msg is None:
            return create_response("error", "unknown", message="无效的消息格式")

        command = msg.get("command", "")
        params = msg.get("params", {})
        request_id = msg.get("request_id", "")

        logger.debug(f"收到命令: {command}, params={params}")

        handler = self._handlers.get(command)
        if handler is None:
            return create_response("error", request_id, message=f"未知命令: {command}")

        try:
            ok, data, message = handler(params)
            return create_response(
                "ok" if ok else "error",
                request_id,
                data=data,
                message=message
            )
        except Exception as e:
            logger.error(f"命令处理异常 [{command}]: {e}", exc_info=True)
            return create_response("error", request_id, message=str(e))
