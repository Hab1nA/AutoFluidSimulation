"""
===============================================================================
SC 进程池单元测试 (M6)

覆盖：
- PersistentSlot 数据类
- SCProcessPool 初始化
- 文件协议 IPC 命令/结果/就绪文件读写
- MAX_SLOTS 上限
- shutdown_all / do_first_cleanup / do_final_cleanup / reset
- run_config（Mock subprocess）
===============================================================================
"""
from __future__ import annotations

import json
import os

from engine.config import LOCAL_PATHS


# ====================================================================
# PersistentSlot 测试
# ====================================================================

class TestPersistentSlot:
    """验证 PersistentSlot 数据类。"""

    def test_default_values(self):
        from engine.sc_process_pool import PersistentSlot
        slot = PersistentSlot(slot_id=1)
        assert slot.slot_id == 1
        assert slot.process is None
        assert slot.pid is None
        assert slot.status == "idle"
        assert slot.current_config is None
        assert slot.configs_processed == 0

    def test_custom_values(self):
        from engine.sc_process_pool import PersistentSlot
        slot = PersistentSlot(slot_id=5, status="ready", configs_processed=10)
        assert slot.slot_id == 5
        assert slot.status == "ready"
        assert slot.configs_processed == 10


# ====================================================================
# SCProcessPool 初始化测试
# ====================================================================

class TestSCProcessPoolInit:
    """验证 SCProcessPool 初始化。"""

    def test_creates_directories(self, tmp_path, monkeypatch):
        """初始化时应创建 data_dir 和 sc_ipc 目录。"""
        data_dir = str(tmp_path / "data")
        monkeypatch.setitem(LOCAL_PATHS, "data_dir", data_dir)

        from engine.sc_process_pool import SCProcessPool
        SCProcessPool()

        assert os.path.isdir(data_dir)
        assert os.path.isdir(os.path.join(data_dir, "sc_ipc"))

    def test_max_slots_constant(self):
        from engine.sc_process_pool import SCProcessPool
        assert SCProcessPool.MAX_SLOTS == 3

    def test_initial_state(self, tmp_path, monkeypatch):
        data_dir = str(tmp_path / "data")
        monkeypatch.setitem(LOCAL_PATHS, "data_dir", data_dir)

        from engine.sc_process_pool import SCProcessPool
        pool = SCProcessPool()

        assert pool._first_cleanup_done is False
        assert pool._final_cleanup_done is False
        assert pool._persistent_slots == {}
        assert pool._next_slot_id == 1


# ====================================================================
# 文件协议 IPC 测试
# ====================================================================

class TestFileProtocolIPC:
    """验证文件协议 IPC 文件的读写。"""

    def test_command_file_format(self, tmp_path):
        """命令文件应为 JSON 格式。"""
        cmd = {
            "command": "process",
            "run_id": "abc123",
            "config": 3,
            "step_dir": str(tmp_path / "step"),
            "scdoc_dir": str(tmp_path / "scdoc"),
        }
        cmd_file = tmp_path / "sc_cmd_1.json"
        cmd_file.write_text(json.dumps(cmd), encoding="utf-8")

        with open(cmd_file, encoding="utf-8") as f:
            data = json.load(f)
        assert data["command"] == "process"
        assert data["run_id"] == "abc123"
        assert data["config"] == 3

    def test_result_file_format(self, tmp_path):
        """结果文件应为 JSON 格式。"""
        result = {
            "config": "3",
            "run_id": "abc123",
            "success": True,
            "message": "ok",
            "timestamp": 1234567890.0,
        }
        result_file = tmp_path / "sc_result_1_abc123.json"
        result_file.write_text(json.dumps(result), encoding="utf-8")

        with open(result_file, encoding="utf-8") as f:
            data = json.load(f)
        assert data["success"] is True
        assert data["config"] == "3"

    def test_ready_file_format(self, tmp_path):
        """就绪文件应为 JSON 格式。"""
        ready = {"ready": True, "slot_id": 1}
        ready_file = tmp_path / "sc_ready_1.json"
        ready_file.write_text(json.dumps(ready), encoding="utf-8")

        with open(ready_file, encoding="utf-8") as f:
            data = json.load(f)
        assert data["ready"] is True
        assert data["slot_id"] == 1

    def test_quit_command_file(self, tmp_path):
        """退出命令文件格式。"""
        cmd = {"command": "quit"}
        cmd_file = tmp_path / "sc_cmd_1.json"
        cmd_file.write_text(json.dumps(cmd), encoding="utf-8")

        with open(cmd_file, encoding="utf-8") as f:
            data = json.load(f)
        assert data["command"] == "quit"


# ====================================================================
# MAX_SLOTS 上限测试
# ====================================================================

class TestMaxSlots:
    """验证槽位上限限制。"""

    def test_get_slot_returns_none_when_full(self, tmp_path, monkeypatch):
        """槽位满时 _get_or_create_persistent_slot 返回 None。"""
        data_dir = str(tmp_path / "data")
        monkeypatch.setitem(LOCAL_PATHS, "data_dir", data_dir)
        # 设置 bridge_path 为空，避免实际启动进程
        monkeypatch.setitem(LOCAL_PATHS, "sc_bridge", "")
        monkeypatch.setitem(LOCAL_PATHS, "sc_script", "")

        from engine.sc_process_pool import SCProcessPool, PersistentSlot
        pool = SCProcessPool()

        # 模拟 3 个 busy 且进程存活的槽位
        for i in range(1, 4):
            slot = PersistentSlot(slot_id=i, status="busy", current_config=i)
            slot.process = type("AlivePopen", (), {"poll": lambda self: None})()
            pool._persistent_slots[i] = slot

        with pool._lock:
            result = pool._get_or_create_persistent_slot()
        assert result is None


# ====================================================================
# 清理与重置测试
# ====================================================================

class TestCleanupAndReset:
    """验证清理和重置操作。"""

    def test_shutdown_all_resets_flags(self, tmp_path, monkeypatch):
        """shutdown_all 重置清理标志。"""
        data_dir = str(tmp_path / "data")
        monkeypatch.setitem(LOCAL_PATHS, "data_dir", data_dir)

        from engine.sc_process_pool import SCProcessPool
        pool = SCProcessPool()
        pool._first_cleanup_done = True
        pool._final_cleanup_done = True

        pool.shutdown_all()

        assert pool._first_cleanup_done is False
        assert pool._final_cleanup_done is False

    def test_do_first_cleanup_idempotent(self, tmp_path, monkeypatch):
        """do_first_cleanup 多次调用只执行一次。"""
        data_dir = str(tmp_path / "data")
        monkeypatch.setitem(LOCAL_PATHS, "data_dir", data_dir)

        from engine.sc_process_pool import SCProcessPool
        pool = SCProcessPool()
        pool.do_first_cleanup()
        assert pool._first_cleanup_done is True
        # 第二次调用不执行
        pool.do_first_cleanup()
        assert pool._first_cleanup_done is True

    def test_do_final_cleanup_idempotent(self, tmp_path, monkeypatch):
        """do_final_cleanup 多次调用只执行一次。"""
        data_dir = str(tmp_path / "data")
        monkeypatch.setitem(LOCAL_PATHS, "data_dir", data_dir)

        from engine.sc_process_pool import SCProcessPool
        pool = SCProcessPool()
        pool.do_final_cleanup()
        assert pool._final_cleanup_done is True
        pool.do_final_cleanup()
        assert pool._final_cleanup_done is True

    def test_reset_clears_all_state(self, tmp_path, monkeypatch):
        """reset 清空所有槽位和标志。"""
        data_dir = str(tmp_path / "data")
        monkeypatch.setitem(LOCAL_PATHS, "data_dir", data_dir)

        from engine.sc_process_pool import SCProcessPool, PersistentSlot
        pool = SCProcessPool()
        pool._first_cleanup_done = True
        pool._final_cleanup_done = True
        pool._persistent_slots[1] = PersistentSlot(slot_id=1, status="ready")
        pool._next_slot_id = 5

        pool.reset()

        assert pool._first_cleanup_done is False
        assert pool._final_cleanup_done is False
        assert pool._persistent_slots == {}
        assert pool._next_slot_id == 1

    def test_cleanup_ipc_files(self, tmp_path, monkeypatch):
        """_cleanup_ipc_files 删除指定槽位的 IPC 文件。"""
        data_dir = str(tmp_path / "data")
        cmd_dir = os.path.join(data_dir, "sc_ipc")
        os.makedirs(cmd_dir, exist_ok=True)
        monkeypatch.setitem(LOCAL_PATHS, "data_dir", data_dir)

        # 创建 IPC 文件
        for name in ["sc_cmd_1.json", "sc_ready_1.json", "sc_result_1_abc.json"]:
            filepath = os.path.join(cmd_dir, name)
            with open(filepath, "w") as f:
                f.write("{}")
            assert os.path.exists(filepath)

        from engine.sc_process_pool import SCProcessPool
        pool = SCProcessPool()
        pool._cleanup_ipc_files(1)

        # 验证文件被删除
        assert not os.path.exists(os.path.join(cmd_dir, "sc_cmd_1.json"))
        assert not os.path.exists(os.path.join(cmd_dir, "sc_ready_1.json"))
        assert not os.path.exists(os.path.join(cmd_dir, "sc_result_1_abc.json"))
