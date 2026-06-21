import logging

from ipc.protocol import (
    CMD_GET_DASHBOARD,
    CMD_WORKER_HEARTBEAT,
    CMD_WORKER_POLL,
    CMD_WORKER_REGISTER,
    CMD_WORKER_STEP_COMPLETE,
    CMD_WORKER_STEP_ERROR,
    create_request,
    serialize,
)
from ipc.server import IPCServer


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
    srv = IPCServer(host="127.0.0.1", port=0, auth_token="")
    # do not start socket; test _process_message directly
    req = create_request("no_such_cmd", {})
    raw = serialize(req)
    resp = srv._process_message(raw)
    assert resp["status"] == "error"
    assert "未知命令" in resp["message"]


def test_process_message_handler_returns_false():
    srv = IPCServer(host="127.0.0.1", port=0, auth_token="")
    srv.register_handler("test_cmd", DummyHandler(raise_exc=False, ok=False))
    req = create_request("test_cmd", {"x": 1})
    raw = serialize(req)
    resp = srv._process_message(raw)
    assert resp["status"] == "error"
    assert resp["request_id"] == req["request_id"]
    assert "failed" in resp["message"]


def test_process_message_debug_log_summarizes_large_params(caplog):
    srv = IPCServer(host="127.0.0.1", port=0, auth_token="")
    srv.register_handler("test_cmd", DummyHandler())
    req = create_request(
        "test_cmd",
        {
            "result": {
                "scdoc_file": {
                    "filename": "model.scdoc",
                    "content_b64": "A" * 2048,
                }
            }
        },
    )

    with caplog.at_level(logging.DEBUG, logger="ipc.server"):
        resp = srv._process_message(serialize(req))

    assert resp["status"] == "ok"
    log_text = "\n".join(record.getMessage() for record in caplog.records)
    assert "content_b64" in log_text
    assert "<str len=2048" in log_text
    assert "A" * 1024 not in log_text


def test_process_message_handler_raises_exception():
    srv = IPCServer(host="127.0.0.1", port=0, auth_token="")
    srv.register_handler("test_cmd", DummyHandler(raise_exc=True))
    req = create_request("test_cmd", {})
    raw = serialize(req)
    resp = srv._process_message(raw)
    assert resp["status"] == "error"
    assert resp["request_id"] == req["request_id"]
    assert "handler-failed" in resp["message"]


def test_process_message_bad_payload():
    srv = IPCServer(host="127.0.0.1", port=0, auth_token="")
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

    srv = IPCServer(host="127.0.0.1", port=0, auth_token="")

    srv.register_default_handlers(_Daemon())

    for command in {
        CMD_WORKER_REGISTER,
        CMD_WORKER_HEARTBEAT,
        CMD_WORKER_POLL,
        CMD_WORKER_STEP_COMPLETE,
        CMD_WORKER_STEP_ERROR,
    }:
        assert command in srv._handlers


def test_register_default_handlers_includes_dashboard_query():
    class _Daemon:
        def handle_get_dashboard(self, params):
            return True, params, ""

        def __getattr__(self, _name):
            return lambda params=None: (True, params, "")

    srv = IPCServer(host="127.0.0.1", port=0, auth_token="")

    srv.register_default_handlers(_Daemon())

    assert CMD_GET_DASHBOARD in srv._handlers



