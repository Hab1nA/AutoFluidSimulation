"""
===============================================================================
详细日志栏目功能验证测试脚本
===============================================================================
测试 LogBroadcastHandler、LogEntry、IPC 日志传输、
日志过滤和导出功能。

运行方式：
  cd "项目根目录"
  python tests/test_detail_log.py
===============================================================================
"""
import os
import sys
import time
import threading
import tempfile
import shutil
import logging

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_TEST_TMP_ROOT = tempfile.mkdtemp(prefix="log_test_")
_TEST_LOG_DIR = os.path.join(_TEST_TMP_ROOT, "logs")
os.makedirs(_TEST_LOG_DIR, exist_ok=True)
os.environ["AUTOFLUID_LOG_DIR"] = _TEST_LOG_DIR

from utils.logger import (
    LogEntry, LogBroadcastHandler, _classify_source,
    install_broadcast_handler, get_broadcast_handler,
)


# ============================================================================
# 测试 1: LogEntry 数据类
# ============================================================================

def test_log_entry_to_dict():
    """测试 LogEntry.to_dict() 序列化。"""
    entry = LogEntry(
        id=1,
        timestamp="2026-05-08 12:00:00",
        level="INFO",
        source="system",
        logger_name="test",
        message="[2026-05-08 12:00:00] [INFO] [test] test message",
        raw_message="[test] test message",
    )
    d = entry.to_dict()
    assert d["id"] == 1
    assert d["level"] == "INFO"
    assert d["source"] == "system"
    assert d["message"] == "[2026-05-08 12:00:00] [INFO] [test] test message"
    assert d["raw_message"] == "[test] test message"
    print("  ✅ LogEntry.to_dict() 正确")


def test_log_entry_from_dict():
    """测试 LogEntry.from_dict() 反序列化。"""
    data = {
        "id": 42,
        "timestamp": "2026-05-08 12:00:00",
        "level": "ERROR",
        "source": "remote_ps",
        "logger_name": "utils.ssh_client",
        "message": "SSH 连接失败",
    }
    entry = LogEntry.from_dict(data)
    assert entry.id == 42
    assert entry.level == "ERROR"
    assert entry.source == "remote_ps"
    assert entry.logger_name == "utils.ssh_client"
    print("  ✅ LogEntry.from_dict() 正确")


def test_log_entry_roundtrip():
    """测试 LogEntry 序列化→反序列化 往返一致性。"""
    original = LogEntry(
        id=99,
        timestamp="2026-05-08 12:00:00",
        level="WARNING",
        source="com",
        logger_name="engine.task_runner",
        message="[2026-05-08 12:00:00] [WARNING] [engine.task_runner] COM 对象验证失败",
        raw_message="[engine.task_runner] COM 对象验证失败",
    )
    restored = LogEntry.from_dict(original.to_dict())
    assert restored.id == original.id
    assert restored.level == original.level
    assert restored.source == original.source
    assert restored.message == original.message
    assert restored.raw_message == original.raw_message
    print("  ✅ LogEntry 序列化往返一致")


# ============================================================================
# 测试 2: _classify_source 日志来源分类
# ============================================================================

def test_classify_source_ssh():
    """测试 SSH 相关日志分类为 remote_ps。"""
    assert _classify_source("utils.ssh_client", "SSH 连接成功") == "remote_ps"
    assert _classify_source("utils.ssh_client", "上传文件") == "remote_ps"
    assert _classify_source("utils.ssh_client", "remote task started") == "remote_ps"
    print("  ✅ SSH 日志分类正确")


def test_classify_source_scheduler():
    """测试调度器日志分类。"""
    assert _classify_source("engine.scheduler", "启动流水线") == "scheduler"
    assert _classify_source("engine.scheduler", "barrier check") == "scheduler"
    print("  ✅ 调度器日志分类正确")


def test_classify_source_ipc():
    """测试 IPC 日志分类。"""
    assert _classify_source("ipc.server", "客户端连接") == "ipc"
    assert _classify_source("ipc.protocol", "消息解析") == "ipc"
    print("  ✅ IPC 日志分类正确")


def test_classify_source_daemon():
    """测试 Daemon 日志分类为 system。"""
    assert _classify_source("PipelineDaemon", "初始化完成") == "system"
    print("  ✅ Daemon 日志分类正确")


def test_classify_source_com():
    """测试 COM 相关日志分类。"""
    assert _classify_source("engine.task_runner", "SolidWorks COM 接口已就绪") == "com"
    assert _classify_source("engine.task_runner", "OpenDoc6 打开模型") == "com"
    assert _classify_source("engine.task_runner", "设计表导入成功") == "com"
    print("  ✅ COM 日志分类正确")


def test_classify_source_local_ps():
    """测试本地子进程日志分类。"""
    assert _classify_source("engine.task_runner", "SpaceClaim 启动") == "local_ps"
    assert _classify_source("engine.task_runner", "STEP 导出完成") == "local_ps"
    assert _classify_source("engine.task_runner", "SC 脚本执行") == "local_ps"
    print("  ✅ 本地子进程日志分类正确")


def test_classify_source_fallback():
    """测试无法分类的日志回退为 system。"""
    assert _classify_source("unknown.module", "普通消息") == "system"
    print("  ✅ 未知来源回退为 system")


# ============================================================================
# 测试 3: LogBroadcastHandler 基本功能
# ============================================================================

def test_handler_emit_and_get():
    """测试日志写入和读取。"""
    handler = LogBroadcastHandler(capacity=100)
    test_logger = logging.getLogger("test.handler.emit")
    test_logger.addHandler(handler)
    test_logger.setLevel(logging.DEBUG)

    test_logger.info("测试消息1")
    test_logger.error("错误消息2")
    test_logger.warning("警告消息3")

    result = handler.get_entries(since_id=0, limit=10)
    entries = result["entries"]
    assert len(entries) == 3, f"期望3条日志，实际{len(entries)}"
    assert entries[0]["level"] == "INFO"
    assert entries[1]["level"] == "ERROR"
    assert entries[2]["level"] == "WARNING"
    assert result["total"] == 3

    test_logger.removeHandler(handler)
    print("  ✅ Handler 写入和读取正确")


def test_handler_incremental_query():
    """测试增量查询（since_id）。"""
    handler = LogBroadcastHandler(capacity=100)
    test_logger = logging.getLogger("test.handler.incr")
    test_logger.addHandler(handler)
    test_logger.setLevel(logging.DEBUG)

    test_logger.info("消息A")
    test_logger.info("消息B")
    test_logger.info("消息C")

    result_all = handler.get_entries(since_id=0, limit=10)
    all_entries = result_all["entries"]
    assert len(all_entries) == 3

    first_id = all_entries[0]["id"]
    result_incr = handler.get_entries(since_id=first_id, limit=10)
    incr_entries = result_incr["entries"]
    assert len(incr_entries) == 2, f"期望2条增量日志，实际{len(incr_entries)}"
    assert incr_entries[0]["id"] > first_id

    test_logger.removeHandler(handler)
    print("  ✅ 增量查询正确")


def test_handler_level_filter():
    """测试按级别过滤。"""
    handler = LogBroadcastHandler(capacity=100)
    test_logger = logging.getLogger("test.handler.filter.level")
    test_logger.addHandler(handler)
    test_logger.setLevel(logging.DEBUG)

    test_logger.debug("调试")
    test_logger.info("信息")
    test_logger.warning("警告")
    test_logger.error("错误")

    result = handler.get_entries(since_id=0, limit=10, level_filter="ERROR")
    entries = result["entries"]
    assert len(entries) == 1, f"期望1条ERROR日志，实际{len(entries)}"
    assert entries[0]["level"] == "ERROR"

    test_logger.removeHandler(handler)
    print("  ✅ 级别过滤正确")


def test_handler_source_filter():
    """测试按来源过滤。"""
    handler = LogBroadcastHandler(capacity=100)
    test_logger = logging.getLogger("utils.ssh_client")
    test_logger.addHandler(handler)
    test_logger.setLevel(logging.DEBUG)

    test_logger.info("SSH 连接成功")
    test_logger.error("远程命令执行失败")

    result = handler.get_entries(since_id=0, limit=10, source_filter="remote_ps")
    entries = result["entries"]
    assert len(entries) == 2, f"期望2条remote_ps日志，实际{len(entries)}"

    test_logger.removeHandler(handler)
    print("  ✅ 来源过滤正确")


def test_handler_capacity_limit():
    """测试环形缓冲区容量限制。"""
    handler = LogBroadcastHandler(capacity=5)
    test_logger = logging.getLogger("test.handler.capacity")
    test_logger.addHandler(handler)
    test_logger.setLevel(logging.DEBUG)

    for i in range(10):
        test_logger.info(f"消息{i}")

    result = handler.get_entries(since_id=0, limit=100)
    entries = result["entries"]
    assert len(entries) == 5, f"期望5条日志（容量限制），实际{len(entries)}"
    assert result["total"] == 5
    assert "消息5" in entries[0]["message"] or "消息6" in entries[0]["message"]

    test_logger.removeHandler(handler)
    print("  ✅ 容量限制正确")


def test_handler_limit_parameter():
    """测试 limit 参数限制返回条数。"""
    handler = LogBroadcastHandler(capacity=100)
    test_logger = logging.getLogger("test.handler.limit")
    test_logger.addHandler(handler)
    test_logger.setLevel(logging.DEBUG)

    for i in range(20):
        test_logger.info(f"消息{i}")

    result = handler.get_entries(since_id=0, limit=5)
    entries = result["entries"]
    assert len(entries) == 5, f"期望5条日志（limit限制），实际{len(entries)}"
    assert result["total"] == 20

    test_logger.removeHandler(handler)
    print("  ✅ limit 参数正确")


def test_handler_stats():
    """测试统计信息。"""
    handler = LogBroadcastHandler(capacity=100)
    test_logger = logging.getLogger("test.handler.stats")
    test_logger.addHandler(handler)
    test_logger.setLevel(logging.DEBUG)

    test_logger.info("info1")
    test_logger.info("info2")
    test_logger.error("error1")

    stats = handler.get_stats()
    assert stats["total"] == 3
    assert stats["level_counts"].get("INFO", 0) == 2
    assert stats["level_counts"].get("ERROR", 0) == 1
    assert stats["latest_id"] > 0

    test_logger.removeHandler(handler)
    print("  ✅ 统计信息正确")


def test_handler_thread_safety():
    """测试多线程并发写入安全性。"""
    handler = LogBroadcastHandler(capacity=500)
    test_logger = logging.getLogger("test.handler.threads")
    test_logger.addHandler(handler)
    test_logger.setLevel(logging.DEBUG)

    errors = []

    def write_logs(thread_id):
        try:
            for i in range(50):
                test_logger.info(f"线程{thread_id}-消息{i}")
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=write_logs, args=(t,)) for t in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert len(errors) == 0, f"多线程写入出错: {errors}"
    result = handler.get_entries(since_id=0, limit=1000)
    assert result["total"] == 250, f"期望250条日志，实际{result['total']}"

    test_logger.removeHandler(handler)
    print("  ✅ 多线程并发安全")


def test_handler_empty_buffer():
    """测试空缓冲区查询。"""
    handler = LogBroadcastHandler(capacity=100)
    result = handler.get_entries(since_id=0, limit=10)
    assert result["entries"] == []
    assert result["total"] == 0
    assert result["latest_id"] == 0

    stats = handler.get_stats()
    assert stats["total"] == 0
    assert stats["latest_id"] == 0
    print("  ✅ 空缓冲区查询正确")


# ============================================================================
# 测试 4: install_broadcast_handler 全局安装
# ============================================================================

def test_install_broadcast_handler():
    """测试全局安装 broadcast handler。"""
    import utils.logger as logger_mod

    original_handler = logger_mod._broadcast_handler
    logger_mod._broadcast_handler = None

    root_logger = logging.getLogger()

    handler = install_broadcast_handler(capacity=50)
    assert handler is not None
    assert get_broadcast_handler() is handler
    assert handler in root_logger.handlers

    test_logger = logging.getLogger("test.install.broadcast")
    test_logger.setLevel(logging.DEBUG)
    test_logger.info("全局handler测试")
    result = handler.get_entries(since_id=0, limit=10)
    assert len(result["entries"]) >= 1

    root_logger.removeHandler(handler)
    logger_mod._broadcast_handler = original_handler
    print("  ✅ 全局安装 broadcast handler 正确")


def test_install_broadcast_handler_idempotent():
    """测试重复安装返回同一实例。"""
    import utils.logger as logger_mod

    original_handler = logger_mod._broadcast_handler
    logger_mod._broadcast_handler = None

    h1 = install_broadcast_handler(capacity=50)
    h2 = install_broadcast_handler(capacity=50)
    assert h1 is h2, "重复安装应返回同一实例"

    root_logger = logging.getLogger()
    root_logger.removeHandler(h1)
    logger_mod._broadcast_handler = original_handler
    print("  ✅ 重复安装幂等性正确")


# ============================================================================
# 测试 5: IPC 日志传输集成测试
# ============================================================================

def test_ipc_log_transmission():
    """测试通过 IPC 传输日志条目。"""
    from ipc.protocol import create_request, serialize, deserialize, CMD_GET_LOG_ENTRIES

    handler = LogBroadcastHandler(capacity=100)
    test_logger = logging.getLogger("test.ipc.log")
    test_logger.addHandler(handler)
    test_logger.setLevel(logging.DEBUG)

    test_logger.info("IPC测试消息1")
    test_logger.error("IPC测试消息2")

    result = handler.get_entries(since_id=0, limit=10)
    entries = result["entries"]
    assert len(entries) == 2

    request = create_request(CMD_GET_LOG_ENTRIES, {
        "since_id": 0,
        "limit": 10,
    })
    serialized = serialize(request)
    deserialized = deserialize(serialized)
    assert deserialized is not None
    assert deserialized["command"] == CMD_GET_LOG_ENTRIES
    assert deserialized["params"]["since_id"] == 0
    assert deserialized["params"]["limit"] == 10

    test_logger.removeHandler(handler)
    print("  ✅ IPC 日志传输协议正确")


def test_ipc_log_transmission_with_filter():
    """测试带过滤条件的 IPC 日志请求序列化。"""
    from ipc.protocol import create_request, serialize, deserialize, CMD_GET_LOG_ENTRIES

    request = create_request(CMD_GET_LOG_ENTRIES, {
        "since_id": 5,
        "limit": 20,
        "level_filter": "ERROR",
        "source_filter": "remote_ps",
    })
    serialized = serialize(request)
    deserialized = deserialize(serialized)
    assert deserialized["params"]["level_filter"] == "ERROR"
    assert deserialized["params"]["source_filter"] == "remote_ps"
    print("  ✅ IPC 日志过滤参数序列化正确")


# ============================================================================
# 测试 6: 日志导出功能模拟
# ============================================================================

def test_log_export():
    """测试日志导出到文件。"""
    handler = LogBroadcastHandler(capacity=100)
    test_logger = logging.getLogger("test.export")
    test_logger.addHandler(handler)
    test_logger.setLevel(logging.DEBUG)

    test_logger.info("导出测试1")
    test_logger.error("导出测试2")
    test_logger.warning("导出测试3")

    result = handler.get_entries(since_id=0, limit=10)
    entries = result["entries"]

    export_dir = os.path.join(_TEST_TMP_ROOT, "export_test")
    os.makedirs(export_dir, exist_ok=True)
    export_path = os.path.join(export_dir, "test_export.log")

    lines = []
    for entry in entries:
        lines.append(
            f"[{entry['timestamp']}] [{entry['level']}] [{entry['source']}] {entry['message']}"
        )
    with open(export_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    assert os.path.exists(export_path)
    with open(export_path, "r", encoding="utf-8") as f:
        content = f.read()
    assert "导出测试1" in content
    assert "导出测试2" in content
    assert "导出测试3" in content

    test_logger.removeHandler(handler)
    print("  ✅ 日志导出功能正确")


def test_log_export_with_filter():
    """测试带过滤的日志导出。"""
    handler = LogBroadcastHandler(capacity=100)
    test_logger = logging.getLogger("test.export.filter")
    test_logger.addHandler(handler)
    test_logger.setLevel(logging.DEBUG)

    test_logger.info("INFO消息")
    test_logger.error("ERROR消息")
    test_logger.warning("WARNING消息")

    result = handler.get_entries(since_id=0, limit=10, level_filter="ERROR")
    entries = result["entries"]
    assert len(entries) == 1
    assert "ERROR消息" in entries[0]["message"]

    test_logger.removeHandler(handler)
    print("  ✅ 带过滤的日志导出正确")


# ============================================================================
# 测试 7: 边界条件
# ============================================================================

def test_handler_since_id_exceeds_buffer():
    """测试 since_id 超过缓冲区最大 ID。"""
    handler = LogBroadcastHandler(capacity=100)
    test_logger = logging.getLogger("test.boundary.since_id")
    test_logger.addHandler(handler)
    test_logger.setLevel(logging.DEBUG)

    test_logger.info("消息1")
    result = handler.get_entries(since_id=0, limit=10)
    latest = result["latest_id"]

    result2 = handler.get_entries(since_id=latest + 100, limit=10)
    assert result2["entries"] == []

    test_logger.removeHandler(handler)
    print("  ✅ since_id 超出范围返回空结果")


def test_handler_zero_limit():
    """测试 limit=0 返回空列表。"""
    handler = LogBroadcastHandler(capacity=100)
    test_logger = logging.getLogger("test.boundary.zero_limit")
    test_logger.addHandler(handler)
    test_logger.setLevel(logging.DEBUG)

    test_logger.info("消息1")
    result = handler.get_entries(since_id=0, limit=0)
    assert result["entries"] == []
    assert result["total"] == 1

    test_logger.removeHandler(handler)
    print("  ✅ limit=0 返回空列表")


def test_handler_combined_filter():
    """测试同时使用级别和来源过滤。"""
    handler = LogBroadcastHandler(capacity=100)
    ssh_logger = logging.getLogger("utils.ssh_client")
    ssh_logger.addHandler(handler)
    ssh_logger.setLevel(logging.DEBUG)

    ssh_logger.info("SSH 连接成功")
    ssh_logger.error("远程命令失败")

    result = handler.get_entries(since_id=0, limit=10, level_filter="ERROR", source_filter="remote_ps")
    entries = result["entries"]
    assert len(entries) == 1
    assert entries[0]["level"] == "ERROR"

    result2 = handler.get_entries(since_id=0, limit=10, level_filter="ERROR", source_filter="local_ps")
    entries2 = result2["entries"]
    assert len(entries2) == 0

    ssh_logger.removeHandler(handler)
    print("  ✅ 组合过滤正确")


def test_handler_large_volume():
    """测试大量日志写入性能。"""
    handler = LogBroadcastHandler(capacity=1000)
    test_logger = logging.getLogger("test.perf.volume")
    test_logger.addHandler(handler)
    test_logger.setLevel(logging.DEBUG)

    start = time.time()
    for i in range(1000):
        test_logger.info(f"性能测试消息{i}")
    elapsed = time.time() - start

    result = handler.get_entries(since_id=0, limit=10)
    assert result["total"] == 1000
    assert elapsed < 5.0, f"1000条日志写入耗时{elapsed:.2f}s，超过5s阈值"

    test_logger.removeHandler(handler)
    print(f"  ✅ 大量日志写入性能正常 (1000条/{elapsed:.2f}s)")


# ============================================================================
# 测试 9: 轮询命令过滤
# ============================================================================

def test_polling_filter_get_all_status():
    """测试 get_all_status 轮询命令被过滤。"""
    handler = LogBroadcastHandler(capacity=100)
    ipc_logger = logging.getLogger("ipc.server")
    ipc_logger.addHandler(handler)
    ipc_logger.setLevel(logging.DEBUG)

    ipc_logger.debug("收到命令: get_all_status, params={}")
    result = handler.get_entries(since_id=0, limit=10)
    assert result["total"] == 0, f"get_all_status 应被过滤，但缓冲区有 {result['total']} 条"

    ipc_logger.removeHandler(handler)
    print("  ✅ get_all_status 轮询命令被正确过滤")


def test_polling_filter_get_log_entries():
    """测试 get_log_entries 轮询命令被过滤。"""
    handler = LogBroadcastHandler(capacity=100)
    ipc_logger = logging.getLogger("ipc.server")
    ipc_logger.addHandler(handler)
    ipc_logger.setLevel(logging.DEBUG)

    ipc_logger.debug("收到命令: get_log_entries, params={'since_id': 0}")
    result = handler.get_entries(since_id=0, limit=10)
    assert result["total"] == 0, f"get_log_entries 应被过滤，但缓冲区有 {result['total']} 条"

    ipc_logger.removeHandler(handler)
    print("  ✅ get_log_entries 轮询命令被正确过滤")


def test_polling_filter_get_engine_status():
    """测试 get_engine_status 轮询命令被过滤。"""
    handler = LogBroadcastHandler(capacity=100)
    ipc_logger = logging.getLogger("ipc.server")
    ipc_logger.addHandler(handler)
    ipc_logger.setLevel(logging.DEBUG)

    ipc_logger.debug("收到命令: get_engine_status, params={}")
    result = handler.get_entries(since_id=0, limit=10)
    assert result["total"] == 0, f"get_engine_status 应被过滤，但缓冲区有 {result['total']} 条"

    ipc_logger.removeHandler(handler)
    print("  ✅ get_engine_status 轮询命令被正确过滤")


def test_polling_filter_mixed_with_normal():
    """测试轮询命令与正常日志混合时，仅轮询命令被过滤。"""
    handler = LogBroadcastHandler(capacity=100)
    ipc_logger = logging.getLogger("ipc.server")
    ipc_logger.addHandler(handler)
    ipc_logger.setLevel(logging.DEBUG)

    ipc_logger.debug("收到命令: get_all_status, params={}")
    ipc_logger.info("IPC 客户端连接: ('127.0.0.1', 12345)")
    ipc_logger.debug("收到命令: start, params={}")
    ipc_logger.debug("收到命令: get_log_entries, params={'since_id': 5}")
    ipc_logger.error("处理客户端消息异常: timeout")

    result = handler.get_entries(since_id=0, limit=10)
    entries = result["entries"]
    assert result["total"] == 3, f"期望3条日志（2条轮询被过滤），实际{result['total']}"

    messages = [e["message"] for e in entries]
    assert any("客户端连接" in m for m in messages), "正常日志'客户端连接'应保留"
    assert any("收到命令: start" in m for m in messages), "手动命令'start'应保留"
    assert any("处理客户端消息异常" in m for m in messages), "错误日志应保留"
    assert not any("get_all_status" in m for m in messages), "轮询命令 get_all_status 不应出现"
    assert not any("get_log_entries" in m for m in messages), "轮询命令 get_log_entries 不应出现"

    ipc_logger.removeHandler(handler)
    print("  ✅ 混合日志中轮询命令被正确过滤，正常日志保留")


def test_polling_filter_non_ipc_logger_not_filtered():
    """测试非 IPC logger 中的轮询命令名称不被误过滤。"""
    handler = LogBroadcastHandler(capacity=100)
    daemon_logger = logging.getLogger("PipelineDaemon")
    daemon_logger.addHandler(handler)
    daemon_logger.setLevel(logging.DEBUG)

    daemon_logger.info("模拟调用 get_all_status 接口进行初始化检查")
    result = handler.get_entries(since_id=0, limit=10)
    assert result["total"] == 1, f"非 IPC logger 中的 get_all_status 不应被过滤，实际 {result['total']} 条"
    assert "get_all_status" in result["entries"][0]["message"]

    daemon_logger.removeHandler(handler)
    print("  ✅ 非 IPC logger 中的轮询命令名称不被误过滤")


def test_polling_filter_high_frequency():
    """测试高频轮询场景下日志不被刷屏。"""
    handler = LogBroadcastHandler(capacity=100)
    ipc_logger = logging.getLogger("ipc.server")
    ipc_logger.addHandler(handler)
    ipc_logger.setLevel(logging.DEBUG)

    for i in range(100):
        ipc_logger.debug("收到命令: get_all_status, params={}")
        ipc_logger.debug(f"收到命令: get_log_entries, params={{'since_id': {i}}}")
        if i % 5 == 0:
            ipc_logger.debug("收到命令: get_engine_status, params={}")

    ipc_logger.info("用户手动触发: start 命令")
    ipc_logger.error("SSH 连接超时")

    result = handler.get_entries(since_id=0, limit=10)
    assert result["total"] == 2, f"300+条轮询日志应全部过滤，仅保留2条正常日志，实际 {result['total']} 条"

    messages = [e["message"] for e in result["entries"]]
    assert any("start 命令" in m for m in messages)
    assert any("SSH 连接超时" in m for m in messages)

    ipc_logger.removeHandler(handler)
    print("  ✅ 高频轮询场景下日志不被刷屏（300+条轮询全部过滤）")


def test_polling_commands_constant():
    """测试 POLLING_COMMANDS 常量完整性。"""
    from utils.logger import POLLING_COMMANDS

    assert "get_all_status" in POLLING_COMMANDS
    assert "get_log_entries" in POLLING_COMMANDS
    assert "get_engine_status" in POLLING_COMMANDS
    assert "get_statistics" not in POLLING_COMMANDS, "get_statistics 是手动命令，不应在过滤列表"
    assert "start" not in POLLING_COMMANDS
    assert "pause" not in POLLING_COMMANDS
    assert "check" not in POLLING_COMMANDS
    print("  ✅ POLLING_COMMANDS 常量完整且正确")


def test_is_polling_log_static_method():
    """测试 _is_polling_log 静态方法。"""
    import logging

    record_ipc_polling = logging.LogRecord(
        name="ipc.server", level=logging.DEBUG, pathname="", lineno=0,
        msg="收到命令: get_all_status, params={}", args=None, exc_info=None,
    )
    record_ipc_normal = logging.LogRecord(
        name="ipc.server", level=logging.DEBUG, pathname="", lineno=0,
        msg="收到命令: start, params={}", args=None, exc_info=None,
    )
    record_daemon_polling_name = logging.LogRecord(
        name="PipelineDaemon", level=logging.INFO, pathname="", lineno=0,
        msg="调用 get_all_status 接口", args=None, exc_info=None,
    )

    assert LogBroadcastHandler._is_polling_log(record_ipc_polling) is True
    assert LogBroadcastHandler._is_polling_log(record_ipc_normal) is False
    assert LogBroadcastHandler._is_polling_log(record_daemon_polling_name) is False

    print("  ✅ _is_polling_log 静态方法判断正确")


def test_broadcast_false_record_is_suppressed():
    """测试带 broadcast=False 的日志不会进入详细日志广播。"""
    handler = LogBroadcastHandler(capacity=100)
    test_logger = logging.getLogger("ipc.server")
    test_logger.addHandler(handler)
    test_logger.setLevel(logging.DEBUG)

    test_logger.debug(
        "[IPC] IPC 客户端断开 (未完成握手): ('127.0.0.1', 53318)",
        extra={"broadcast": False},
    )

    result = handler.get_entries(since_id=0, limit=10)
    assert result["total"] == 0
    assert result["entries"] == []

    test_logger.removeHandler(handler)
    print("  ✅ broadcast=False 日志不会进入详细日志")


# ============================================================================
# 测试 10: 周期性健康检查日志过滤（通过 broadcast=False 控制）
# ============================================================================

def test_periodic_log_queue_health_suppressed():
    """测试 [队列健康] DEBUG 日志（broadcast=False）不会进入详细日志。"""
    handler = LogBroadcastHandler(capacity=100)
    test_logger = logging.getLogger("engine.scheduler.worker_pool")
    test_logger.addHandler(handler)
    test_logger.setLevel(logging.DEBUG)

    test_logger.debug(
        "[队列健康] SC深度=3, Transfer深度=1, "
        "活跃SC=2, 活跃Transfer=1, Barrier=已通过",
        extra={"broadcast": False},
    )

    result = handler.get_entries(since_id=0, limit=10)
    assert result["total"] == 0, f"[队列健康] broadcast=False 应被过滤，实际 {result['total']} 条"

    test_logger.removeHandler(handler)
    print("  ✅ [队列健康] broadcast=False 日志被正确过滤")


def test_periodic_log_queue_health_waiting_suppressed():
    """测试 [队列健康] 构型等待确认 DEBUG 日志（broadcast=False）不会进入详细日志。"""
    handler = LogBroadcastHandler(capacity=100)
    test_logger = logging.getLogger("engine.scheduler.worker_pool")
    test_logger.addHandler(handler)
    test_logger.setLevel(logging.DEBUG)

    test_logger.debug(
        "[队列健康] 构型5 SW 已完成，等待 SC 入队确认",
        extra={"broadcast": False},
    )

    result = handler.get_entries(since_id=0, limit=10)
    assert result["total"] == 0, f"[队列健康] 等待确认日志应被过滤，实际 {result['total']} 条"

    test_logger.removeHandler(handler)
    print("  ✅ [队列健康] 构型等待确认日志被正确过滤")


def test_periodic_log_queue_warning_passes_through():
    """测试 [队列异常] WARNING 日志（无 broadcast=False）应被放行。"""
    handler = LogBroadcastHandler(capacity=100)
    test_logger = logging.getLogger("engine.scheduler.worker_pool")
    test_logger.addHandler(handler)
    test_logger.setLevel(logging.DEBUG)

    test_logger.warning("[队列异常] 构型8 SW 已完成但未入队")

    result = handler.get_entries(since_id=0, limit=10)
    assert result["total"] == 1, f"[队列异常] WARNING 应被放行，实际 {result['total']} 条"
    assert "[队列异常]" in result["entries"][0]["message"]

    test_logger.removeHandler(handler)
    print("  ✅ [队列异常] WARNING 日志被正确放行")


def test_periodic_log_mixed_with_normal():
    """测试 broadcast=False 日志与正常日志混合时，仅 broadcast=False 被过滤。"""
    handler = LogBroadcastHandler(capacity=100)
    test_logger = logging.getLogger("engine.scheduler.worker_pool")
    test_logger.addHandler(handler)
    test_logger.setLevel(logging.DEBUG)

    test_logger.debug(
        "[队列健康] SC深度=0, Transfer深度=0, 活跃SC=2, 活跃Transfer=1, Barrier=已通过",
        extra={"broadcast": False},
    )
    test_logger.info("[WorkerPool] 检测到所有构型 SC 步骤已完成，触发 SC 进程清理")
    test_logger.debug(
        "[队列健康] SC深度=1, Transfer深度=2, 活跃SC=2, 活跃Transfer=1, Barrier=未通过",
        extra={"broadcast": False},
    )
    test_logger.warning("[WorkerPool] SC 进程清理异常: timeout")

    result = handler.get_entries(since_id=0, limit=10)
    entries = result["entries"]
    assert result["total"] == 2, f"期望 2 条正常日志（2 条 broadcast=False 被过滤），实际 {result['total']} 条"

    messages = [e["message"] for e in entries]
    assert any("SC 进程清理" in m for m in messages), "正常 INFO 日志应保留"
    assert any("清理异常" in m for m in messages), "WARNING 日志应保留"
    assert not any("队列健康" in m for m in messages), "[队列健康] 不应出现"

    test_logger.removeHandler(handler)
    print("  ✅ 混合日志中 broadcast=False 被正确过滤，正常日志保留")


# ============================================================================
# 测试 11: 逐构型日志广播过滤
# ============================================================================

def test_config_scoped_logs_are_suppressed():
    """测试详细日志不会显示逐构型日志或广播层摘要。"""
    handler = LogBroadcastHandler(capacity=100)
    test_logger = logging.getLogger("engine.state_manager")
    test_logger.addHandler(handler)
    test_logger.setLevel(logging.DEBUG)

    for config_name in range(1, 51):
        test_logger.info(f"状态更新: 构型{config_name} [sc] -> Completed")

    result = handler.get_entries(since_id=0, limit=10)
    assert result["total"] == 0
    assert result["entries"] == []

    test_logger.removeHandler(handler)
    print("  ✅ 逐构型中文日志不会进入详细日志")


def test_config_scoped_log_suppression_matches_config_equals():
    """测试 config=123 形式的单构型日志也不会进入详细日志。"""
    handler = LogBroadcastHandler(capacity=100)
    test_logger = logging.getLogger("engine.scheduler.main")
    test_logger.addHandler(handler)
    test_logger.setLevel(logging.DEBUG)

    for config_name in range(1, 4):
        test_logger.info(f"已重置 config={config_name} step=all")

    result = handler.get_entries(since_id=0, limit=10)
    assert result["total"] == 0
    assert result["entries"] == []

    test_logger.removeHandler(handler)
    print("  ✅ config=N 形式日志不会进入详细日志")


def test_config_scoped_log_suppression_matches_operation_context():
    """测试带队列、槽位和 run id 的逐构型日志不会进入详细日志。"""
    handler = LogBroadcastHandler(capacity=100)
    test_logger = logging.getLogger("engine.sc_process_pool")
    test_logger.addHandler(handler)
    test_logger.setLevel(logging.DEBUG)

    for config_name in range(1, 6):
        test_logger.info(
            f"[SC-Pool] 构型{config_name} 命令已发送 "
            f"(槽位{config_name % 3 + 1}, run={config_name})"
        )

    result = handler.get_entries(since_id=0, limit=10)
    assert result["total"] == 0
    assert result["entries"] == []

    test_logger.removeHandler(handler)
    print("  ✅ 带操作上下文的逐构型日志不会进入详细日志")


def test_single_config_scoped_log_is_suppressed():
    """测试单条逐构型日志也不会进入详细日志。"""
    handler = LogBroadcastHandler(capacity=100)
    test_logger = logging.getLogger("utils.excel_reader")
    test_logger.addHandler(handler)
    test_logger.setLevel(logging.DEBUG)

    test_logger.debug("[Excel] 读取构型1: 参数 = [7.0, 16.0, 14.0, 23.0]")

    result = handler.get_entries(since_id=0, limit=10)
    assert result["total"] == 0
    assert result["entries"] == []

    test_logger.removeHandler(handler)
    print("  ✅ 单条逐构型日志不会进入详细日志")


def test_config_scoped_excel_logs_do_not_emit_aggregate_summary():
    """测试 Excel 逐构型日志不会生成详细日志摘要。"""
    handler = LogBroadcastHandler(capacity=100)
    test_logger = logging.getLogger("utils.excel_reader")
    test_logger.addHandler(handler)
    test_logger.setLevel(logging.DEBUG)

    test_logger.debug("[Excel] 读取构型1: 参数 = [7.0, 16.0, 14.0, 23.0]")
    test_logger.debug("[Excel] 读取构型2: 参数 = [8.0, 17.0, 15.0, 24.0]")

    result = handler.get_entries(since_id=0, limit=10)
    assert result["total"] == 0
    assert result["entries"] == []

    test_logger.removeHandler(handler)
    print("  ✅ Excel 逐构型日志不生成详细日志摘要")


def test_config_scoped_log_passes_through_warning_and_above():
    """WARNING/ERROR/CRITICAL 级别的构型日志应被放行（不被过滤）。"""
    handler = LogBroadcastHandler(capacity=100)
    test_logger = logging.getLogger("engine.scheduler.main")
    test_logger.addHandler(handler)
    test_logger.setLevel(logging.DEBUG)

    # WARNING 级别含"构型N"的日志应被放行
    test_logger.warning("检测到重复入队：构型8 Transfer 已在队列中")
    # ERROR 级别含"构型N"的日志应被放行
    test_logger.error("构型3 Meshing 异常重试 3 次后放弃")
    # CRITICAL 级别含"构型N"的日志应被放行
    test_logger.critical("构型5 致命异常: RuntimeError: 连接中断")

    # INFO 级别含"构型N"的日志仍应被过滤
    test_logger.info("状态更新: 构型1 [sc] -> Completed")

    result = handler.get_entries(since_id=0, limit=10)
    # 3 条 WARNING/ERROR/CRITICAL 应被放行，1 条 INFO 应被过滤
    assert result["total"] == 3, (
        f"预期 3 条 WARNING+ 日志被放行，实际: {result['total']}"
    )

    levels = [e["level"] for e in result["entries"]]
    assert "WARNING" in levels
    assert "ERROR" in levels
    assert "CRITICAL" in levels
    assert "INFO" not in levels

    test_logger.removeHandler(handler)
    print("  ✅ WARNING+ 级别的构型日志被放行，INFO 仍被过滤")

def test_raw_message_contains_logger_name_and_msg():
    """测试 raw_message 仅包含 logger 名和消息内容，不含时间戳和级别。"""
    handler = LogBroadcastHandler(capacity=100)
    test_logger = logging.getLogger("engine.task_runner")
    test_logger.addHandler(handler)
    test_logger.setLevel(logging.DEBUG)

    test_logger.info("SpaceClaim 启动成功")

    result = handler.get_entries(since_id=0, limit=10)
    entries = result["entries"]
    assert len(entries) == 1

    entry = entries[0]
    assert "[engine.task_runner]" in entry["raw_message"]
    assert "SpaceClaim 启动成功" in entry["raw_message"]
    assert "[INFO]" not in entry["raw_message"]
    assert "2026" not in entry["raw_message"]

    test_logger.removeHandler(handler)
    print("  ✅ raw_message 不含时间戳和级别，仅含 logger 名和消息")


def test_message_still_has_full_format():
    """测试 message 字段仍包含完整格式（时间戳+级别+logger名+消息）。"""
    handler = LogBroadcastHandler(capacity=100)
    test_logger = logging.getLogger("utils.ssh_client")
    test_logger.addHandler(handler)
    test_logger.setLevel(logging.DEBUG)

    test_logger.error("SSH 连接超时")

    result = handler.get_entries(since_id=0, limit=10)
    entry = result["entries"][0]

    assert "[ERROR]" in entry["message"]
    assert "[utils.ssh_client]" in entry["message"]
    assert "SSH 连接超时" in entry["message"]

    test_logger.removeHandler(handler)
    print("  ✅ message 字段保留完整格式（含时间戳和级别）")


def test_raw_message_vs_message_difference():
    """测试 raw_message 与 message 的差异：raw_message 更短。"""
    handler = LogBroadcastHandler(capacity=100)
    test_logger = logging.getLogger("engine.scheduler")
    test_logger.addHandler(handler)
    test_logger.setLevel(logging.DEBUG)

    test_logger.warning("屏障检查失败")

    result = handler.get_entries(since_id=0, limit=10)
    entry = result["entries"][0]

    assert len(entry["raw_message"]) < len(entry["message"])
    assert entry["raw_message"] == "[engine.scheduler] 屏障检查失败"

    test_logger.removeHandler(handler)
    print("  ✅ raw_message 比 message 更短（去除时间戳和级别）")


def test_export_uses_full_message():
    """测试导出功能使用完整 message 字段（含时间戳和级别）。"""
    handler = LogBroadcastHandler(capacity=100)
    test_logger = logging.getLogger("test.export.full")
    test_logger.addHandler(handler)
    test_logger.setLevel(logging.DEBUG)

    test_logger.info("导出测试消息")

    result = handler.get_entries(since_id=0, limit=10)
    entries = result["entries"]

    export_dir = os.path.join(_TEST_TMP_ROOT, "export_full_test")
    os.makedirs(export_dir, exist_ok=True)
    export_path = os.path.join(export_dir, "test_full.log")

    lines = []
    for entry in entries:
        lines.append(entry["message"])

    with open(export_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    with open(export_path, "r", encoding="utf-8") as f:
        content = f.read()

    assert "[INFO]" in content, "导出文件应包含级别信息"
    assert "[test.export.full]" in content, "导出文件应包含 logger 名"
    assert "导出测试消息" in content

    test_logger.removeHandler(handler)
    print("  ✅ 导出文件使用完整 message 格式（含时间戳和级别）")


def test_from_dict_backward_compatible():
    """测试 from_dict 在缺少 raw_message 时回退到 message。"""
    data = {
        "id": 1,
        "timestamp": "2026-05-08 12:00:00",
        "level": "INFO",
        "source": "system",
        "logger_name": "test",
        "message": "[2026-05-08 12:00:00] [INFO] [test] hello",
    }
    entry = LogEntry.from_dict(data)
    assert entry.raw_message == entry.message, "缺少 raw_message 时应回退到 message"

    print("  ✅ from_dict 向后兼容（缺少 raw_message 时回退到 message）")


# ============================================================================
# 测试 11: Issue1 - _broadcast_handler 线程安全
# ============================================================================

def test_install_broadcast_handler_thread_safe():
    """测试多线程并发调用 install_broadcast_handler 只创建一个实例。"""
    import utils.logger as logger_mod

    original_handler = logger_mod._broadcast_handler
    logger_mod._broadcast_handler = None

    results = []
    errors = []

    def install():
        try:
            h = install_broadcast_handler(capacity=50)
            results.append(id(h))
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=install) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert len(errors) == 0, f"多线程安装出错: {errors}"
    assert len(set(results)) == 1, f"多线程安装应返回同一实例，实际返回 {len(set(results))} 个不同实例"

    root_logger = logging.getLogger()
    handler_count = sum(1 for h in root_logger.handlers if isinstance(h, LogBroadcastHandler))
    assert handler_count == 1, f"root logger 应只有1个 broadcast handler，实际 {handler_count} 个"

    root_logger.removeHandler(logger_mod._broadcast_handler)
    logger_mod._broadcast_handler = original_handler
    print("  ✅ 多线程并发安装 broadcast handler 线程安全")


def test_get_broadcast_handler_thread_safe():
    """测试多线程并发读取 get_broadcast_handler 不崩溃。"""
    import utils.logger as logger_mod

    original_handler = logger_mod._broadcast_handler
    logger_mod._broadcast_handler = None

    handler = install_broadcast_handler(capacity=50)
    results = []
    errors = []

    def read_handler():
        try:
            h = get_broadcast_handler()
            results.append(h is not None)
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=read_handler) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert len(errors) == 0, f"多线程读取出错: {errors}"
    assert all(results), "所有线程应读到非 None handler"

    logging.getLogger().removeHandler(handler)
    logger_mod._broadcast_handler = original_handler
    print("  ✅ 多线程并发读取 broadcast handler 线程安全")


# ============================================================================
# 主测试入口
# ============================================================================

def main():
    print("=" * 60)
    print("  详细日志栏目功能验证测试套件")
    print("=" * 60)

    tests = [
        ("LogEntry.to_dict()", test_log_entry_to_dict),
        ("LogEntry.from_dict()", test_log_entry_from_dict),
        ("LogEntry 序列化往返", test_log_entry_roundtrip),
        ("日志来源分类-SSH", test_classify_source_ssh),
        ("日志来源分类-调度器", test_classify_source_scheduler),
        ("日志来源分类-IPC", test_classify_source_ipc),
        ("日志来源分类-Daemon", test_classify_source_daemon),
        ("日志来源分类-COM", test_classify_source_com),
        ("日志来源分类-本地PS", test_classify_source_local_ps),
        ("日志来源分类-回退", test_classify_source_fallback),
        ("Handler 写入读取", test_handler_emit_and_get),
        ("Handler 增量查询", test_handler_incremental_query),
        ("Handler 级别过滤", test_handler_level_filter),
        ("Handler 来源过滤", test_handler_source_filter),
        ("Handler 容量限制", test_handler_capacity_limit),
        ("Handler limit参数", test_handler_limit_parameter),
        ("Handler 统计信息", test_handler_stats),
        ("Handler 多线程安全", test_handler_thread_safety),
        ("Handler 空缓冲区", test_handler_empty_buffer),
        ("全局安装handler", test_install_broadcast_handler),
        ("全局安装幂等性", test_install_broadcast_handler_idempotent),
        ("IPC日志传输", test_ipc_log_transmission),
        ("IPC日志过滤参数", test_ipc_log_transmission_with_filter),
        ("日志导出", test_log_export),
        ("带过滤日志导出", test_log_export_with_filter),
        ("边界: since_id超出", test_handler_since_id_exceeds_buffer),
        ("边界: limit=0", test_handler_zero_limit),
        ("边界: 组合过滤", test_handler_combined_filter),
        ("性能: 大量日志", test_handler_large_volume),
        ("轮询过滤: get_all_status", test_polling_filter_get_all_status),
        ("轮询过滤: get_log_entries", test_polling_filter_get_log_entries),
        ("轮询过滤: get_engine_status", test_polling_filter_get_engine_status),
        ("轮询过滤: 混合日志", test_polling_filter_mixed_with_normal),
        ("轮询过滤: 非IPC不误过滤", test_polling_filter_non_ipc_logger_not_filtered),
        ("轮询过滤: 高频场景", test_polling_filter_high_frequency),
        ("轮询过滤: 常量完整性", test_polling_commands_constant),
        ("轮询过滤: _is_polling_log", test_is_polling_log_static_method),
        ("广播过滤: broadcast=False", test_broadcast_false_record_is_suppressed),
        ("逐构型过滤: 构型N", test_config_scoped_logs_are_suppressed),
        ("逐构型过滤: config=N", test_config_scoped_log_suppression_matches_config_equals),
        ("逐构型过滤: 操作上下文", test_config_scoped_log_suppression_matches_operation_context),
        ("逐构型过滤: 单条日志", test_single_config_scoped_log_is_suppressed),
        ("逐构型过滤: Excel摘要", test_config_scoped_excel_logs_do_not_emit_aggregate_summary),
        ("逐构型过滤: WARNING+放行", test_config_scoped_log_passes_through_warning_and_above),
        ("raw_message: 不含时间戳和级别", test_raw_message_contains_logger_name_and_msg),
        ("raw_message: message保留完整格式", test_message_still_has_full_format),
        ("raw_message: 比message更短", test_raw_message_vs_message_difference),
        ("导出: 使用完整message格式", test_export_uses_full_message),
        ("向后兼容: from_dict缺raw_message", test_from_dict_backward_compatible),
        ("Issue1: 并发安装handler线程安全", test_install_broadcast_handler_thread_safe),
        ("Issue1: 并发读取handler线程安全", test_get_broadcast_handler_thread_safe),
    ]

    passed = 0
    failed = 0

    for name, test_func in tests:
        try:
            test_func()
            passed += 1
        except AssertionError as e:
            print(f"\n  ❌ 测试失败 [{name}]: {e}")
            failed += 1
        except Exception as e:
            print(f"\n  ❌ 测试异常 [{name}]: {type(e).__name__}: {e}")
            import traceback
            traceback.print_exc()
            failed += 1

    print("\n" + "=" * 60)
    print(f"  测试结果: {passed} 通过, {failed} 失败, {len(tests)} 总计")
    print("=" * 60)

    if os.path.exists(_TEST_TMP_ROOT):
        shutil.rmtree(_TEST_TMP_ROOT, ignore_errors=True)

    return failed == 0


if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
