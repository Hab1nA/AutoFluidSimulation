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
import subprocess

from engine.config import ENGINE_CONFIG, LOCAL_PATHS


# ====================================================================
# Bridge 退出码诊断测试
# ====================================================================

def test_format_bridge_exit_adds_known_reason():
    """Bridge 退出码应补充 C# 端语义，便于诊断。"""
    from engine.sc_process_pool import _format_bridge_exit

    assert _format_bridge_exit(5) == "exit=5 (Timeout)"


def test_format_bridge_exit_handles_unknown_and_missing_code():
    """未知或缺失退出码应保留可读诊断信息。"""
    from engine.sc_process_pool import _format_bridge_exit

    assert _format_bridge_exit(99) == "exit=99 (Unknown)"
    assert _format_bridge_exit(None) == "exit=unknown"


def test_sc_log_extra_marks_step_context(tmp_path, monkeypatch):
    """SC 构型日志应带结构化步骤上下文，供详细日志栏过滤/展示。"""
    monkeypatch.setitem(LOCAL_PATHS, "data_dir", str(tmp_path / "data"))
    monkeypatch.setitem(LOCAL_PATHS, "sc_bridge", "")
    monkeypatch.setitem(LOCAL_PATHS, "sc_script", "")

    from engine.sc_process_pool import PersistentSlot, SCProcessPool

    pool = SCProcessPool()
    assert pool._sc_log_extra(7) == {
        "log_category": "step",
        "config_name": "7",
        "step_name": "sc",
    }
    assert pool._sc_log_extra(7, PersistentSlot(slot_id=3)) == {
        "log_category": "step",
        "config_name": "7",
        "step_name": "sc",
        "worker_id": "sc-slot-3",
    }


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
        assert slot.spaceclaim_pid is None
        assert slot.bridge_log_path is None
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

    def test_project_config_keeps_persistent_mode_enabled(self):
        """项目默认配置应使用常驻 SC，避免每个构型反复冷启动 SpaceClaim。"""
        assert ENGINE_CONFIG["sc_persistent_enabled"] is True

    def test_creates_directories(self, tmp_path, monkeypatch):
        """初始化时应创建 data_dir 和 sc_ipc 目录。"""
        data_dir = str(tmp_path / "data")
        monkeypatch.setitem(LOCAL_PATHS, "data_dir", data_dir)

        from engine.sc_process_pool import SCProcessPool
        SCProcessPool()

        assert os.path.isdir(data_dir)
        assert os.path.isdir(os.path.join(data_dir, "sc_ipc"))

    def test_default_max_slots_is_one(self):
        from engine.sc_process_pool import SCProcessPool
        pool = SCProcessPool()
        assert pool._max_slots == 1

    def test_max_slots_can_be_configured(self, monkeypatch):
        from engine.config import ENGINE_CONFIG
        from engine.sc_process_pool import SCProcessPool

        monkeypatch.setitem(ENGINE_CONFIG, "sc_max_slots", 2)

        pool = SCProcessPool()

        assert pool._max_slots == 2

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

        # 默认单槽：模拟唯一槽位 busy 且进程存活。
        slot = PersistentSlot(slot_id=1, status="busy", current_config=1)
        slot.process = type("AlivePopen", (), {"poll": lambda self: None})()
        pool._persistent_slots[1] = slot

        with pool._lock:
            result = pool._get_or_create_persistent_slot()
        assert result is None

    def test_launch_persistent_process_passes_bridge_log_dir_to_transit(
        self, tmp_path, monkeypatch
    ):
        """启动 Bridge 时应把会话 bridge 日志目录传给 transit 脚本。"""
        data_dir = str(tmp_path / "data")
        bridge_exe = tmp_path / "SpaceClaimBridge.exe"
        bridge_exe.write_text("fake bridge", encoding="utf-8")

        monkeypatch.setitem(LOCAL_PATHS, "data_dir", data_dir)
        monkeypatch.setitem(LOCAL_PATHS, "sc_bridge", str(bridge_exe))
        monkeypatch.setitem(LOCAL_PATHS, "sc_script", str(tmp_path / "spaceclaim_transit.py"))
        monkeypatch.setattr("engine.sc_process_pool.get_session_log_dir", lambda: str(tmp_path / "session_logs"))

        captured = {}

        class _FakePopen:
            def __init__(self, cmd, stdout=None, stderr=None, creationflags=0, env=None):
                captured["cmd"] = cmd
                captured["env"] = env or {}
                self.pid = 4242

            def poll(self):
                return None

        monkeypatch.setattr("engine.sc_process_pool.subprocess.Popen", _FakePopen)

        from engine.sc_process_pool import SCProcessPool, PersistentSlot

        pool = SCProcessPool()
        slot = PersistentSlot(slot_id=2, cmd_dir=pool._persistent_cmd_dir)

        assert pool._launch_persistent_process(slot)
        expected_log_dir = os.path.join(str(tmp_path / "session_logs"), "bridge")
        assert captured["env"]["AUTOFLUID_SC_LOG_DIR"] == expected_log_dir
        assert slot.bridge_log_path is not None
        assert os.path.dirname(slot.bridge_log_path) == expected_log_dir

    def test_bridge_log_dir_falls_back_to_spaceclaim_service_dir(
        self, tmp_path, monkeypatch
    ):
        """没有会话目录时，Bridge/transit 日志应进入 SpaceClaim 服务目录。"""
        data_dir = str(tmp_path / "data")
        log_dir = str(tmp_path / "logs")
        monkeypatch.setitem(LOCAL_PATHS, "data_dir", data_dir)
        monkeypatch.setitem(LOCAL_PATHS, "log_dir", log_dir)
        monkeypatch.setattr("engine.sc_process_pool.get_session_log_dir", lambda: None)
        monkeypatch.delenv("AUTOFLUID_SERVER_MODE", raising=False)

        from engine.sc_process_pool import SCProcessPool

        pool = SCProcessPool()

        assert pool._build_bridge_log_dir() == os.path.join(
            log_dir,
            "local",
            "services",
            "spaceclaim",
        )

    def test_get_slot_balances_ready_slots_and_cleans_dead_idle_slot(
        self, tmp_path, monkeypatch
    ):
        """选择 ready 槽位前应扫描所有槽位，清理死亡空闲进程并均衡复用。"""
        data_dir = str(tmp_path / "data")
        monkeypatch.setitem(LOCAL_PATHS, "data_dir", data_dir)
        monkeypatch.setitem(LOCAL_PATHS, "sc_bridge", "")
        monkeypatch.setitem(LOCAL_PATHS, "sc_script", "")

        from engine.sc_process_pool import SCProcessPool, PersistentSlot

        class _AlivePopen:
            def poll(self):
                return None

        class _DeadPopen:
            def poll(self):
                return 1

            def kill(self):
                return None

            def communicate(self, timeout=None):
                return None

        pool = SCProcessPool()
        slot1 = PersistentSlot(slot_id=1, status="ready", configs_processed=3)
        slot1.process = _AlivePopen()
        slot2 = PersistentSlot(slot_id=2, status="ready", configs_processed=0)
        slot2.process = _AlivePopen()
        slot3 = PersistentSlot(slot_id=3, status="ready", configs_processed=0)
        slot3.process = _DeadPopen()
        pool._persistent_slots = {1: slot1, 2: slot2, 3: slot3}

        with pool._lock:
            result = pool._get_or_create_persistent_slot()

        assert result is slot2
        assert 3 not in pool._persistent_slots


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
        for name in [
            "sc_cmd_1.json",
            "sc_ready_1.json",
            "sc_bridge_1.json",
            "sc_result_1_abc.json",
        ]:
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
        assert not os.path.exists(os.path.join(cmd_dir, "sc_bridge_1.json"))
        assert not os.path.exists(os.path.join(cmd_dir, "sc_result_1_abc.json"))

    def test_load_bridge_monitor_info_records_spaceclaim_pid(self, tmp_path, monkeypatch):
        """读取 Bridge 监控文件后应记录实际 SpaceClaim PID。"""
        data_dir = str(tmp_path / "data")
        cmd_dir = os.path.join(data_dir, "sc_ipc")
        os.makedirs(cmd_dir, exist_ok=True)
        monkeypatch.setitem(LOCAL_PATHS, "data_dir", data_dir)

        from engine.sc_process_pool import SCProcessPool, PersistentSlot

        monitor_file = os.path.join(cmd_dir, "sc_bridge_2.json")
        with open(monitor_file, "w", encoding="utf-8") as f:
            json.dump({
                "slot_id": 2,
                "bridge_pid": 1234,
                "spaceclaim_pid": 5678,
                "status": "launched",
            }, f)

        pool = SCProcessPool()
        slot = PersistentSlot(slot_id=2, cmd_dir=cmd_dir)

        pool._load_bridge_monitor_info(slot)

        assert slot.spaceclaim_pid == 5678

    def test_cleanup_removes_stale_ansys_runningcad_markers(
        self, tmp_path, monkeypatch
    ):
        """SC 清场应删除指向已退出 PID 的 Ansys RunningCADs 标记。"""
        data_dir = str(tmp_path / "data")
        temp_dir = tmp_path / "temp"
        marker_dir = temp_dir / "Ansys" / "RunningCADs" / "SPICA" / "v231"
        marker_dir.mkdir(parents=True)
        stale_marker = marker_dir / "999999.direct"
        stale_marker.write_text(
            '<?xml version="1.0"?><plugin name="SpaceClaimModeling" '
            'pid="999999" port="49014"/>',
            encoding="utf-8",
        )
        monkeypatch.setitem(LOCAL_PATHS, "data_dir", data_dir)
        monkeypatch.setenv("TEMP", str(temp_dir))
        monkeypatch.setattr("engine.sc_process_pool.is_process_alive", lambda _pid: False)
        monkeypatch.setattr(
            "engine.sc_process_pool.subprocess.run",
            lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0),
        )
        monkeypatch.setattr("engine.sc_process_pool.time.sleep", lambda _seconds: None)

        from engine.sc_process_pool import SCProcessPool

        pool = SCProcessPool()
        pool._kill_all_sc_processes()

        assert not stale_marker.exists()

    def test_run_config_falls_back_to_oneshot_when_persistent_ready_fails(
        self, tmp_path, monkeypatch
    ):
        """常驻槽位启动失败时应退回一次性 Bridge。"""
        data_dir = str(tmp_path / "data")
        monkeypatch.setitem(LOCAL_PATHS, "data_dir", data_dir)
        monkeypatch.setitem(ENGINE_CONFIG, "sc_persistent_enabled", True)
        monkeypatch.setitem(ENGINE_CONFIG, "sc_oneshot_fallback_enabled", True)

        called = []

        from engine.sc_process_pool import SCProcessPool, PersistentSlot

        pool = SCProcessPool()
        slot = PersistentSlot(slot_id=5, status="starting")
        pool._persistent_slots[slot.slot_id] = slot

        monkeypatch.setattr(pool, "_get_or_create_persistent_slot", lambda: slot)
        monkeypatch.setattr(pool, "_wait_for_slot_ready", lambda _slot: False)
        monkeypatch.setattr(
            pool,
            "_run_oneshot_bridge",
            lambda config_name: called.append(config_name) or True,
        )

        assert pool.run_config(3) is True
        assert called == [3]

    def test_run_config_uses_oneshot_when_persistent_disabled(
        self, tmp_path, monkeypatch
    ):
        """配置关闭常驻模式时应直接使用一次性 Bridge。"""
        data_dir = str(tmp_path / "data")
        monkeypatch.setitem(LOCAL_PATHS, "data_dir", data_dir)
        monkeypatch.setitem(ENGINE_CONFIG, "sc_persistent_enabled", False)

        called = []

        from engine.sc_process_pool import SCProcessPool

        pool = SCProcessPool()
        monkeypatch.setattr(
            pool,
            "_run_oneshot_bridge",
            lambda config_name: called.append(config_name) or True,
        )
        monkeypatch.setattr(
            pool,
            "_get_or_create_persistent_slot",
            lambda: (_ for _ in ()).throw(AssertionError("persistent path used")),
        )

        assert pool.run_config(2) is True
        assert called == [2]

    def test_run_config_respects_disabled_oneshot_fallback(self, tmp_path, monkeypatch):
        """配置关闭 fallback 时，常驻启动失败仍返回错误。"""
        data_dir = str(tmp_path / "data")
        monkeypatch.setitem(LOCAL_PATHS, "data_dir", data_dir)
        monkeypatch.setitem(ENGINE_CONFIG, "sc_persistent_enabled", True)
        monkeypatch.setitem(ENGINE_CONFIG, "sc_oneshot_fallback_enabled", False)

        from engine.sc_process_pool import SCProcessPool, PersistentSlot

        pool = SCProcessPool()
        slot = PersistentSlot(slot_id=6, status="starting")
        pool._persistent_slots[slot.slot_id] = slot

        monkeypatch.setattr(pool, "_get_or_create_persistent_slot", lambda: slot)
        monkeypatch.setattr(pool, "_wait_for_slot_ready", lambda _slot: False)

        assert pool.run_config(4) is False
        assert "常驻槽位6 就绪失败" in pool.last_error

    def test_shutdown_persistent_slot_tree_kills_bridge_and_spaceclaim(
        self, tmp_path, monkeypatch
    ):
        """quit 超时后应按进程树清理 Bridge 与 SpaceClaim。"""
        data_dir = str(tmp_path / "data")
        monkeypatch.setitem(LOCAL_PATHS, "data_dir", data_dir)

        killed = []

        class _TimedOutPopen:
            def wait(self, timeout=None):
                raise subprocess.TimeoutExpired("bridge", timeout)

            def communicate(self, timeout=None):
                return None

        monkeypatch.setattr(
            "engine.sc_process_pool.run_taskkill",
            lambda pid: killed.append(pid) or True,
        )

        from engine.sc_process_pool import SCProcessPool, PersistentSlot

        pool = SCProcessPool()
        slot = PersistentSlot(slot_id=4, cmd_dir=pool._persistent_cmd_dir)
        slot.process = _TimedOutPopen()
        slot.pid = 111
        slot.spaceclaim_pid = 222

        pool._shutdown_persistent_slot(slot)

        assert killed == [222, 111]
        assert slot.process is None
        assert slot.pid is None
        assert slot.spaceclaim_pid is None
        assert not os.path.exists(
            os.path.join(pool._persistent_cmd_dir, "sc_cmd_4.json")
        )
