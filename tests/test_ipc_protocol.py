from ipc import protocol


def test_create_request_and_response_roundtrip():
    req = protocol.create_request(protocol.CMD_GET_LOG_ENTRIES, {"since_id": 0, "limit": 10})
    assert "command" in req and req["command"] == protocol.CMD_GET_LOG_ENTRIES
    assert "request_id" in req and isinstance(req["request_id"], str)

    resp = protocol.create_response("ok", req["request_id"], data={"items": []}, message="ok")
    assert resp["status"] == "ok"
    assert resp["request_id"] == req["request_id"]


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
