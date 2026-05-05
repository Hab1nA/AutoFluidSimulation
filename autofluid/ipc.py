"""本地 IPC 通信（Socket）。"""

from __future__ import annotations

import json
import logging
import socket
import socketserver
from typing import Any, Callable, Dict, Optional


CommandHandler = Callable[[Dict[str, Any]], Dict[str, Any]]
LOGGER = logging.getLogger("autofluid")


class IPCRequestHandler(socketserver.StreamRequestHandler):
    """逐行 JSON 的 IPC 请求处理。"""

    def handle(self) -> None:
        for raw in self.rfile:
            try:
                payload = json.loads(raw.decode("utf-8"))
                response = self.server.command_handler(payload)
            except json.JSONDecodeError:
                response = {"ok": False, "message": "无效的 JSON 请求"}
            except Exception as exc:  # pragma: no cover - 保证 IPC 稳定
                LOGGER.exception("IPC 处理失败")
                response = {"ok": False, "message": f"处理失败: {exc}"}
            self.wfile.write((json.dumps(response, ensure_ascii=False) + "\n").encode("utf-8"))


class IPCServer(socketserver.ThreadingTCPServer):
    """IPC 服务端。"""

    allow_reuse_address = True

    def __init__(self, host: str, port: int, command_handler: CommandHandler) -> None:
        super().__init__((host, port), IPCRequestHandler)
        self.command_handler = command_handler


class IPCClient:
    """IPC 客户端。"""

    def __init__(self, host: str, port: int, timeout: float = 5.0) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout

    def send_command(self, command: str, args: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        payload = {"command": command, "args": args or {}}
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8") + b"\n"
        with socket.create_connection((self.host, self.port), timeout=self.timeout) as sock:
            sock.sendall(data)
            sock.settimeout(self.timeout)
            with sock.makefile("r", encoding="utf-8") as reader:
                response_line = reader.readline()
        return json.loads(response_line)
