from ipc.server import IPCServer
from ipc.protocol import serialize, create_request


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



