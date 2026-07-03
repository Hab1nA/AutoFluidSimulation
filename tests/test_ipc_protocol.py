import ipc

from ipc import protocol


def test_create_request_and_response_roundtrip():
    req = protocol.create_request(protocol.CMD_GET_LOG_ENTRIES, {"since_id": 0, "limit": 10})
    assert "command" in req and req["command"] == protocol.CMD_GET_LOG_ENTRIES
    assert "request_id" in req and isinstance(req["request_id"], str)

    resp = protocol.create_response("ok", req["request_id"], data={"items": []}, message="ok")
    assert resp["status"] == "ok"
    assert resp["request_id"] == req["request_id"]


def test_create_request_includes_auth_token_when_provided():
    req = protocol.create_request("ping", auth_token="secret-token")

    assert req["auth_token"] == "secret-token"


def test_serialize_deserialize_basic():
    msg = {"command": "ping", "params": {"a": 1}, "request_id": "abcd"}
    b = protocol.serialize(msg)
    assert isinstance(b, bytes)
    assert b.endswith(b"\n")

    out = protocol.deserialize(b)
    assert isinstance(out, dict)
    assert out["command"] == "ping"


def test_deserialize_empty_or_whitespace():
    assert protocol.deserialize(b"\n") is None
    assert protocol.deserialize(b"   \n") is None


def test_deserialize_invalid_json_logs_and_returns_none(caplog):
    bad = b"{not json}\n"
    out = protocol.deserialize(bad)
    assert out is None
    assert any("消息反序列化失败" in r.message or "JSON" in r.message or "反序列化" in r.message for r in caplog.records)


def test_deserialize_non_dict_json_returns_none(caplog):
    arr = b"[1,2,3]\n"
    out = protocol.deserialize(arr)
    assert out is None
    assert any("不是对象" in r.message or "不是对象" in r.getMessage() for r in caplog.records)


def test_serialize_unicode_and_preserve():
    msg = {"command": "测试", "params": {"文本": "中文"}, "request_id": "r1"}
    b = protocol.serialize(msg)
    assert b.decode("utf-8").strip().find("测试") >= 0
    out = protocol.deserialize(b)
    assert out["command"] == "测试"


# ====================================================================
# IPC 命令常量一致性验证（Python ↔ Rust 同步检查）
# ====================================================================

# 预期完整的命令常量集合（与 Rust autofluid-tui/src/ipc/protocol.rs 保持一致）
_EXPECTED_COMMANDS = frozenset({
    "start",
    "pause",
    "stop",
    "stop_step",
    "check",
    "migrate_config_workstation",
    "reset_step",
    "clean_step",
    "get_all_status",
    "get_statistics",
    "get_engine_status",
    "get_log_entries",
    "get_dashboard",
    "reload_config",
    "worker_register",
    "worker_heartbeat",
    "worker_poll",
    "worker_step_complete",
    "worker_step_error",
    "worker_start",
    "worker_stop",
    "worker_restart",
})


def test_ipc_command_constants_complete():
    """验证所有 IPC 命令常量均已定义且值非空。"""
    all_cmd_attrs = [
        attr for attr in dir(protocol)
        if attr.startswith("CMD_") and not attr.startswith("__")
    ]
    defined_commands = set()
    for attr in all_cmd_attrs:
        val = getattr(protocol, attr)
        assert isinstance(val, str) and val, (
            f"命令常量 {attr} 必须是有效的非空字符串，实际值: {val!r}"
        )
        defined_commands.add(val)

    # 验证无遗漏的命令
    missing = _EXPECTED_COMMANDS - defined_commands
    assert not missing, (
        f"以下 IPC 命令常量在 ipc/protocol.py 中缺失: {sorted(missing)}。"
        f"请确保与 Rust autofluid-tui/src/ipc/protocol.rs 同步更新。"
    )

    # 验证无多余的（可能已废弃）的命令
    extra = defined_commands - _EXPECTED_COMMANDS
    assert not extra, (
        f"以下 IPC 命令常量在 ipc/protocol.py 中存在但预期集合中缺失: {sorted(extra)}。"
        f"如果这些是新命令，请同时更新 Rust 侧和此测试的 _EXPECTED_COMMANDS。"
    )


def test_package_re_exports_all_ipc_command_constants():
    missing = [
        name
        for name in dir(protocol)
        if name.startswith("CMD_") and not hasattr(ipc, name)
    ]

    assert not missing
