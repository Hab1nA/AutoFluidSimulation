"""
===============================================================================
IPC 服务器集成测试 (M5)

覆盖：
- 服务器生命周期: start/stop/重复启动
- 完整命令→响应往返: 多条命令的请求-响应
- 未知命令处理
- 处理器抛异常的容错
- 连接数限制
- 多客户端并发连接
- 大消息处理
- 无效消息格式
===============================================================================
"""
from __future__ import annotations

import socket
import threading

import pytest

from ipc.protocol import serialize, create_request, deserialize
from ipc.server import IPCServer


# ====================================================================
# 辅助工具
# ====================================================================

def _find_free_port() -> int:
    """获取一个空闲的临时端口。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _send_command(port: int, command: str, params: dict | None = None,
                  timeout: float = 5.0) -> dict:
    """向 IPC 服务器发送一条命令并返回响应。"""
    req = create_request(command, params)
    raw = serialize(req)

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        sock.connect(("127.0.0.1", port))
        sock.sendall(raw)
        # 读取响应（以 \n 结尾）
        buf = b""
        while b"\n" not in buf:
            chunk = sock.recv(4096)
            if not chunk:
                break
            buf += chunk
    return deserialize(buf.rstrip(b"\n"))


def _send_raw(port: int, data: bytes, timeout: float = 5.0) -> bytes:
    """向 IPC 服务器发送原始字节并返回原始响应。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        sock.connect(("127.0.0.1", port))
        sock.sendall(data)
        buf = b""
        while b"\n" not in buf:
            chunk = sock.recv(4096)
            if not chunk:
                break
            buf += chunk
    return buf


# ====================================================================
# 服务器生命周期测试
# ====================================================================

class TestServerLifecycle:
    """验证服务器启动/停止。"""

    def test_start_and_stop(self):
        port = _find_free_port()
        server = IPCServer(host="127.0.0.1", port=port)
        server.start()
        assert server._running is True

        server.stop()
        assert server._running is False

    def test_double_start_no_error(self):
        """重复 start 不抛异常。"""
        port = _find_free_port()
        server = IPCServer(host="127.0.0.1", port=port)
        server.start()
        server.start()  # 不应报错
        server.stop()

    def test_stop_without_start_no_error(self):
        """未启动时 stop 不抛异常。"""
        server = IPCServer(host="127.0.0.1", port=_find_free_port())
        server.stop()

    def test_port_bind_failure_raises(self):
        """端口被占用时 start 应抛 OSError。"""
        port = _find_free_port()
        # 先占用端口
        blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        blocker.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
        blocker.bind(("127.0.0.1", port))
        blocker.listen(1)

        server = IPCServer(host="127.0.0.1", port=port)
        try:
            with pytest.raises(OSError):
                server.start()
        finally:
            blocker.close()


# ====================================================================
# 命令往返测试
# ====================================================================

class TestCommandRoundtrip:
    """验证完整的命令→响应往返。"""

    def _make_server(self, port: int) -> IPCServer:
        server = IPCServer(host="127.0.0.1", port=port)
        server.register_handler("ping", lambda params: (True, "pong", "ok"))
        server.register_handler("echo", lambda params: (True, params.get("msg", ""), "echo"))
        server.start()
        return server

    def test_ping_command(self):
        port = _find_free_port()
        server = self._make_server(port)
        try:
            resp = _send_command(port, "ping")
            assert resp["status"] == "ok"
            assert resp["data"] == "pong"
        finally:
            server.stop()

    def test_echo_command(self):
        port = _find_free_port()
        server = self._make_server(port)
        try:
            resp = _send_command(port, "echo", {"msg": "hello"})
            assert resp["status"] == "ok"
            assert resp["data"] == "hello"
        finally:
            server.stop()

    def test_request_id_preserved(self):
        """请求 ID 应在响应中保持一致。"""
        port = _find_free_port()
        server = self._make_server(port)
        try:
            req = create_request("ping")
            raw = serialize(req)

            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.settimeout(5)
                sock.connect(("127.0.0.1", port))
                sock.sendall(raw)
                buf = b""
                while b"\n" not in buf:
                    buf += sock.recv(4096)

            resp = deserialize(buf.rstrip(b"\n"))
            assert resp["request_id"] == req["request_id"]
        finally:
            server.stop()

    def test_multiple_commands_same_connection(self):
        """同一连接发送多条命令。"""
        port = _find_free_port()
        server = self._make_server(port)
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.settimeout(5)
                sock.connect(("127.0.0.1", port))

                for i in range(3):
                    req = create_request("echo", {"msg": f"msg{i}"})
                    sock.sendall(serialize(req))
                    buf = b""
                    while b"\n" not in buf:
                        buf += sock.recv(4096)
                    resp = deserialize(buf.rstrip(b"\n"))
                    assert resp["data"] == f"msg{i}"
        finally:
            server.stop()


# ====================================================================
# 错误处理测试
# ====================================================================

class TestErrorHandling:
    """验证各种错误场景的容错。"""

    def test_unknown_command(self):
        port = _find_free_port()
        server = IPCServer(host="127.0.0.1", port=port)
        server.start()
        try:
            resp = _send_command(port, "nonexistent_cmd")
            assert resp["status"] == "error"
            assert "未知命令" in resp["message"]
        finally:
            server.stop()

    def test_handler_raises_exception(self):
        """处理器抛异常时返回 error 响应，不崩溃。"""
        port = _find_free_port()
        server = IPCServer(host="127.0.0.1", port=port)

        def bad_handler(params):
            raise RuntimeError("boom")

        server.register_handler("crash", bad_handler)
        server.start()
        try:
            resp = _send_command(port, "crash")
            assert resp["status"] == "error"
            assert "boom" in resp["message"]
        finally:
            server.stop()

    def test_invalid_json_message(self):
        """无效 JSON 消息返回 error。"""
        port = _find_free_port()
        server = IPCServer(host="127.0.0.1", port=port)
        server.start()
        try:
            raw_resp = _send_raw(port, b"not json\n")
            resp = deserialize(raw_resp.rstrip(b"\n"))
            assert resp["status"] == "error"
        finally:
            server.stop()

    def test_handler_returns_false(self):
        """处理器返回 False 时响应 status 为 error。"""
        port = _find_free_port()
        server = IPCServer(host="127.0.0.1", port=port)
        server.register_handler("fail", lambda params: (False, None, "操作失败"))
        server.start()
        try:
            resp = _send_command(port, "fail")
            assert resp["status"] == "error"
            assert resp["message"] == "操作失败"
        finally:
            server.stop()


# ====================================================================
# 连接数限制测试
# ====================================================================

class TestConnectionLimit:
    """验证连接数限制。"""

    def test_rejects_when_at_limit(self):
        """超过 max_connections 时拒绝新连接。"""
        port = _find_free_port()
        server = IPCServer(host="127.0.0.1", port=port)
        server.register_handler("ping", lambda params: (True, "pong", "ok"))
        server.start()
        try:
            # 模拟连接数达到上限（直接设置计数器）
            with server._conn_lock:
                server._active_connections = server._max_connections

            # 新连接应被拒绝：服务器发送错误响应后立即关闭 socket，
            # 客户端可能收到响应，也可能收到连接异常
            try:
                resp = _send_command(port, "ping", timeout=3)
                assert resp["status"] == "error"
                assert "繁忙" in resp["message"] or "上限" in resp["message"]
            except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError, OSError):
                # 服务器立即关闭 socket 导致客户端连接异常，这也是拒绝行为
                pass
        finally:
            server.stop()


# ====================================================================
# 多客户端并发测试
# ====================================================================

class TestConcurrentClients:
    """验证多客户端并发连接。"""

    def test_concurrent_commands(self):
        """多个客户端同时发送命令不崩溃。"""
        port = _find_free_port()
        server = IPCServer(host="127.0.0.1", port=port)
        # 在启动前注册所有 handler，避免竞态
        server.register_handler("ping", lambda params: (True, "pong", "ok"))
        server.register_handler("echo", lambda params: (True, params.get("msg", ""), "ok"))
        server.start()

        results: list[dict] = []
        errors: list[Exception] = []

        def client_task(client_id: int):
            try:
                resp = _send_command(port, "echo", {"msg": f"client{client_id}"})
                results.append(resp)
            except Exception as e:
                errors.append(e)

        # 注册 echo 处理器（已在上方注册）

        threads = [threading.Thread(target=client_task, args=(i,)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        server.stop()

        assert not errors, f"并发客户端出错: {errors}"
        assert len(results) == 5


# ====================================================================
# 大消息测试
# ====================================================================

class TestLargeMessage:
    """验证多包消息处理（payload 超过单次 recv 缓冲区）。"""

    def test_multi_packet_payload(self):
        """超过 recv 缓冲区 (4096) 的 payload 应正常处理。"""
        port = _find_free_port()
        server = IPCServer(host="127.0.0.1", port=port)
        server.register_handler("big", lambda params: (True, len(params.get("data", "")), "ok"))
        server.start()
        try:
            # 50KB payload，远超 4096 字节的 recv 缓冲区
            big_data = "x" * 50000
            resp = _send_command(port, "big", {"data": big_data})
            assert resp["status"] == "ok"
            assert resp["data"] == 50000
        finally:
            server.stop()
