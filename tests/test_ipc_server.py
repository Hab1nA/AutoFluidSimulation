from ipc.server import IPCServer
from ipc.protocol import (
    CMD_WORKER_HEARTBEAT,
    CMD_WORKER_POLL,
    CMD_WORKER_REGISTER,
    CMD_WORKER_STEP_COMPLETE,
    CMD_WORKER_STEP_ERROR,
    create_request,
    serialize,
)


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


def test_process_message_rejects_missing_auth_token_when_required():
    srv = IPCServer(host="127.0.0.1", port=0, auth_token="secret-token")
    srv.register_handler("test_cmd", DummyHandler())
    req = create_request("test_cmd", {})

    resp = srv._process_message(serialize(req))

    assert resp["status"] == "error"
    assert resp["request_id"] == req["request_id"]
    assert "认证失败" in resp["message"]


def test_process_message_rejects_wrong_auth_token_when_required():
    srv = IPCServer(host="127.0.0.1", port=0, auth_token="secret-token")
    srv.register_handler("test_cmd", DummyHandler())
    req = create_request("test_cmd", {}, auth_token="wrong-token")

    resp = srv._process_message(serialize(req))

    assert resp["status"] == "error"
    assert resp["request_id"] == req["request_id"]
    assert "认证失败" in resp["message"]


def test_process_message_accepts_matching_auth_token():
    srv = IPCServer(host="127.0.0.1", port=0, auth_token="secret-token")
    srv.register_handler("test_cmd", DummyHandler())
    req = create_request("test_cmd", {}, auth_token="secret-token")

    resp = srv._process_message(serialize(req))

    assert resp["status"] == "ok"
    assert resp["data"] == {"ok": True}


def test_register_default_handlers_includes_local_worker_commands():
    class _Daemon:
        def handle_worker_register(self, params):
            return True, params, ""

        def handle_worker_heartbeat(self, params):
            return True, params, ""

        def handle_worker_poll(self, params):
            return True, params, ""

        def handle_worker_step_complete(self, params):
            return True, params, ""

        def handle_worker_step_error(self, params):
            return True, params, ""

        def __getattr__(self, _name):
            return lambda params=None: (True, params, "")

    srv = IPCServer(host="127.0.0.1", port=0)

    srv.register_default_handlers(_Daemon())

    for command in {
        CMD_WORKER_REGISTER,
        CMD_WORKER_HEARTBEAT,
        CMD_WORKER_POLL,
        CMD_WORKER_STEP_COMPLETE,
        CMD_WORKER_STEP_ERROR,
    }:
        assert command in srv._handlers



