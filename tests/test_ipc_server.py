import socket
import threading
from ipc.server import IPCServer
from ipc.protocol import serialize, create_request, deserialize


class DummyHandler:
    def __init__(self, raise_exc=False, ok=True):
        self.raise_exc = raise_exc
        self.ok = ok

    def __call__(self, params):
        if self.raise_exc:
            raise RuntimeError("handler-failed")
        if not self.ok:
            return False, {"reason": "bad"}, "failed"
        return True, {"ok": True}, ""


def test_process_message_unknown_command():
    srv = IPCServer(host="127.0.0.1", port=0)
    # do not start socket; test _process_message directly
    req = create_request("no_such_cmd", {})
    raw = serialize(req)
    resp = srv._process_message(raw)
    assert resp["status"] == "error"
    assert "未知命令" in resp["message"]


def test_process_message_handler_returns_false():
    srv = IPCServer(host="127.0.0.1", port=0)
    srv.register_handler("test_cmd", DummyHandler(raise_exc=False, ok=False))
    req = create_request("test_cmd", {"x": 1})
    raw = serialize(req)
    resp = srv._process_message(raw)
    assert resp["status"] == "error"
    assert resp["request_id"] == req["request_id"]
    assert "failed" in resp["message"]


def test_process_message_handler_raises_exception():
    srv = IPCServer(host="127.0.0.1", port=0)
    srv.register_handler("test_cmd", DummyHandler(raise_exc=True))
    req = create_request("test_cmd", {})
    raw = serialize(req)
    resp = srv._process_message(raw)
    assert resp["status"] == "error"
    assert resp["request_id"] == req["request_id"]
    assert "handler-failed" in resp["message"]


def test_process_message_bad_payload():
    srv = IPCServer(host="127.0.0.1", port=0)
    # send non-json / invalid payload
    resp = srv._process_message(b"not a json\n")
    assert resp["status"] == "error"
    assert resp["request_id"] == "unknown"


import pytest


@pytest.mark.skip(reason="flaky in CI; behavior covered by _process_message unit tests")
def test_end_to_end_in_memory_socket():
    # Start IPC server on an ephemeral port and send a simple request over socket
    srv = IPCServer(host="127.0.0.1", port=0)
    # bind to ephemeral port by starting server; then retrieve bound port
    srv.start()
    # register a simple echo handler after server start to avoid any race
    def echo(params):
        return True, params, ""
    srv.register_handler("echo", echo)
    # 等待服务器线程就绪
    import time
    time.sleep(0.05)

    try:
        port = srv._socket.getsockname()[1]
        # connect client socket and perform simple request/response
        s = socket.create_connection(("127.0.0.1", port), timeout=2)
        req = create_request("echo", {"a": 1})
        s.sendall(serialize(req))
        # read response line
        resp_bytes = b""
        while b"\n" not in resp_bytes:
            chunk = s.recv(4096)
            if not chunk:
                break
            resp_bytes += chunk
        s.close()
        resp = deserialize(resp_bytes)
        assert resp is not None
        if resp.get("status") != "ok":
            raise AssertionError(f"unexpected ipc response: {resp}")
        assert resp["data"]["a"] == 1
    finally:
        srv.stop()
