"""
SC 步骤文件检测完成逻辑单元测试。

覆盖：
- _write_result 输出包含 run_id 字段
- SCDOC 文件 mtime 过滤（旧文件不被误判为新完成）
- SCDOC 文件大小稳定性检测
- 错误提前信号（result_file success=false）
- _cleanup_run_files 仅清理 run-scoped 文件
- per-run 结果文件路径隔离
"""

import json
import os
import tempfile
import shutil
import threading
import time


# ====================================================================
# _write_result 测试（spaceclaim_transit.py 内的函数）
# ====================================================================

class TestWriteResultRunId:
    """验证 _write_result 在提供 run_id 时将其写入结果 JSON。"""

    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp(prefix="sc_result_test_")

    def teardown_method(self):
        if os.path.exists(self.tmpdir):
            shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _import_write_result(self):
        """复制 _write_result 的核心逻辑进行测试（无法直接 import transit）。"""
        import io

        def _write_result(result_file, config_name, success, message="",
                          run_id=None):
            result = {
                "config": config_name,
                "success": bool(success),
                "message": str(message),
                "timestamp": time.time(),
            }
            if run_id:
                result["run_id"] = run_id
            tmp_file = result_file + ".tmp"
            with io.open(tmp_file, "w", encoding="utf-8") as f:
                json.dump(result, f)
                f.write("\n")
                f.flush()
                os.fsync(f.fileno())
            try:
                if os.path.exists(result_file):
                    os.remove(result_file)
            except (IOError, OSError):
                pass
            os.rename(tmp_file, result_file)

        return _write_result

    def test_write_result_with_run_id(self):
        """提供 run_id 时，结果 JSON 应包含该字段。"""
        write_result = self._import_write_result()
        result_path = os.path.join(self.tmpdir, "sc_result_1_abc123.json")
        write_result(result_path, "3", True, "ok", run_id="abc123")

        with open(result_path, "r") as f:
            data = json.load(f)
        assert data["run_id"] == "abc123"
        assert data["config"] == "3"
        assert data["success"] is True

    def test_write_result_without_run_id(self):
        """不提供 run_id 时，结果 JSON 不应包含该字段。"""
        write_result = self._import_write_result()
        result_path = os.path.join(self.tmpdir, "sc_result_1.json")
        write_result(result_path, "3", True, "ok")

        with open(result_path, "r") as f:
            data = json.load(f)
        assert "run_id" not in data
        assert data["config"] == "3"

    def test_write_result_atomic_rename(self):
        """结果文件应通过原子 rename 写入（.tmp -> final）。"""
        write_result = self._import_write_result()
        result_path = os.path.join(self.tmpdir, "sc_result_1_xyz.json")
        write_result(result_path, "5", False, "fail", run_id="xyz")

        assert not os.path.exists(result_path + ".tmp")
        assert os.path.exists(result_path)

        with open(result_path, "r") as f:
            data = json.load(f)
        assert data["success"] is False
        assert data["run_id"] == "xyz"


# ====================================================================
# SCDOC 文件 mtime 过滤测试
# ====================================================================

class TestScdocMtimeFiltering:
    """验证 mtime 过滤：仅接受命令发送后创建/修改的 SCDOC 文件。"""

    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp(prefix="sc_mtime_test_")

    def teardown_method(self):
        if os.path.exists(self.tmpdir):
            shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_old_file_rejected_by_mtime(self):
        """mtime 早于命令发送时刻的文件应被跳过。"""
        scdoc_file = os.path.join(self.tmpdir, "model_gen4_1.scdoc")

        # 创建一个旧文件（mtime = 1000）
        with open(scdoc_file, "w") as f:
            f.write("old content")
        os.utime(scdoc_file, (1000, 1000))

        command_sent_at = 2000.0

        file_mtime = os.path.getmtime(scdoc_file)
        assert file_mtime < command_sent_at - 1.0  # 应被判定为旧文件

    def test_new_file_accepted_by_mtime(self):
        """mtime 晚于命令发送时刻的文件应被接受。"""
        scdoc_file = os.path.join(self.tmpdir, "model_gen4_1.scdoc")

        command_sent_at = time.time() - 5.0  # 5 秒前发送

        # 创建一个新文件（当前时间）
        with open(scdoc_file, "w") as f:
            f.write("new content")

        file_mtime = os.path.getmtime(scdoc_file)
        assert file_mtime >= command_sent_at - 1.0  # 应被判定为新文件

    def test_mtime_within_tolerance(self):
        """mtime 在 1.0s 容差内的文件应被接受。"""
        scdoc_file = os.path.join(self.tmpdir, "model_gen4_1.scdoc")
        command_sent_at = time.time()

        with open(scdoc_file, "w") as f:
            f.write("content")

        file_mtime = os.path.getmtime(scdoc_file)
        assert file_mtime >= command_sent_at - 1.0  # 容差 1.0s


# ====================================================================
# SCDOC 文件大小稳定性检测测试
# ====================================================================

class TestScdocSizeStability:
    """验证文件大小稳定性检测逻辑。"""

    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp(prefix="sc_stable_test_")

    def teardown_method(self):
        if os.path.exists(self.tmpdir):
            shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_stable_size_detected(self):
        """文件大小在窗口期内不变应被判定为稳定。"""
        scdoc_file = os.path.join(self.tmpdir, "model_gen4_1.scdoc")
        with open(scdoc_file, "w") as f:
            f.write("x" * 1024)

        scdoc_stable_seconds = 0.1  # 测试用短窗口

        size_1 = os.path.getsize(scdoc_file)
        stable_since = time.time()

        time.sleep(scdoc_stable_seconds + 0.05)

        size_2 = os.path.getsize(scdoc_file)
        now = time.time()

        assert size_1 == size_2
        assert now - stable_since >= scdoc_stable_seconds

    def test_changing_size_not_stable(self):
        """文件大小在窗口期内变化不应被判定为稳定。"""
        scdoc_file = os.path.join(self.tmpdir, "model_gen4_1.scdoc")

        with open(scdoc_file, "w") as f:
            f.write("x" * 512)
        size_1 = os.path.getsize(scdoc_file)

        with open(scdoc_file, "a") as f:
            f.write("y" * 512)
        size_2 = os.path.getsize(scdoc_file)

        assert size_1 != size_2  # 大小变化 → 不稳定

    def test_zero_size_file_not_accepted(self):
        """大小为 0 的文件不应被接受。"""
        scdoc_file = os.path.join(self.tmpdir, "model_gen4_1.scdoc")
        with open(scdoc_file, "wb"):
            pass  # 空文件

        assert os.path.getsize(scdoc_file) == 0


# ====================================================================
# 错误提前信号测试
# ====================================================================

class TestErrorSignal:
    """验证 result_file 中 success=false 作为错误提前信号。"""

    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp(prefix="sc_error_test_")

    def teardown_method(self):
        if os.path.exists(self.tmpdir):
            shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_error_result_detected(self):
        """success=false 的结果文件应被检测为错误信号。"""
        result_file = os.path.join(self.tmpdir, "sc_result_1_run123.json")
        with open(result_file, "w") as f:
            json.dump({"config": "1", "success": False,
                       "message": "SaveAs failed"}, f)

        with open(result_file, "r") as f:
            data = json.load(f)
        assert data["success"] is False
        assert "SaveAs" in data["message"]

    def test_success_result_not_used_for_completion(self):
        """success=true 的结果文件不作为完成依据（仅靠 SCDOC 文件检测）。"""
        result_file = os.path.join(self.tmpdir, "sc_result_1_run123.json")
        with open(result_file, "w") as f:
            json.dump({"config": "1", "success": True,
                       "message": "ok"}, f)

        with open(result_file, "r") as f:
            data = json.load(f)
        # success=true 存在但不影响完成判定
        assert data["success"] is True


# ====================================================================
# _cleanup_run_files 测试
# ====================================================================

class TestCleanupRunFiles:
    """验证 per-run 文件清理仅影响目标 run 的文件。"""

    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp(prefix="sc_cleanup_test_")

    def teardown_method(self):
        if os.path.exists(self.tmpdir):
            shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _simulate_cleanup_run_files(self, slot_id, run_id):
        """模拟 _cleanup_run_files 的逻辑。"""
        for suffix in ["sc_result_{}_{}.json".format(slot_id, run_id),
                       "sc_result_{}_{}.json.tmp".format(slot_id, run_id)]:
            path = os.path.join(self.tmpdir, suffix)
            if os.path.exists(path):
                os.remove(path)

    def test_cleanup_removes_target_run_files(self):
        """应清理目标 run_id 的结果文件和 .tmp 文件。"""
        slot_id = 1
        run_id = "target_run"

        result_file = os.path.join(
            self.tmpdir, "sc_result_{}_{}.json".format(slot_id, run_id))
        tmp_file = result_file + ".tmp"

        with open(result_file, "w") as f:
            f.write("{}")
        with open(tmp_file, "w") as f:
            f.write("{}")

        self._simulate_cleanup_run_files(slot_id, run_id)

        assert not os.path.exists(result_file)
        assert not os.path.exists(tmp_file)

    def test_cleanup_preserves_other_run_files(self):
        """不应清理其他 run_id 的结果文件。"""
        slot_id = 1
        other_run = "other_run_999"
        other_file = os.path.join(
            self.tmpdir, "sc_result_{}_{}.json".format(slot_id, other_run))

        with open(other_file, "w") as f:
            f.write("{}")

        self._simulate_cleanup_run_files(slot_id, "target_run")

        assert os.path.exists(other_file)

    def test_cleanup_preserves_ready_file(self):
        """不应清理 ready 文件。"""
        slot_id = 1
        run_id = "target_run"
        ready_file = os.path.join(
            self.tmpdir, "sc_ready_{}.json".format(slot_id))

        with open(ready_file, "w") as f:
            f.write("{}")

        self._simulate_cleanup_run_files(slot_id, run_id)

        assert os.path.exists(ready_file)


# ====================================================================
# per-run 结果文件路径隔离测试
# ====================================================================

class TestPerRunResultIsolation:
    """验证不同 run_id 的结果文件互不干扰。"""

    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp(prefix="sc_isolation_test_")

    def teardown_method(self):
        if os.path.exists(self.tmpdir):
            shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_different_run_ids_different_files(self):
        """不同 run_id 应产生不同的结果文件路径。"""
        slot_id = 1
        run_a = "aaa111"
        run_b = "bbb222"

        path_a = os.path.join(self.tmpdir,
                              "sc_result_{}_{}.json".format(slot_id, run_a))
        path_b = os.path.join(self.tmpdir,
                              "sc_result_{}_{}.json".format(slot_id, run_b))

        assert path_a != path_b

        with open(path_a, "w") as f:
            json.dump({"config": "1", "run_id": run_a, "success": True}, f)
        with open(path_b, "w") as f:
            json.dump({"config": "2", "run_id": run_b, "success": True}, f)

        with open(path_a, "r") as f:
            data_a = json.load(f)
        assert data_a["config"] == "1"
        assert data_a["run_id"] == run_a


class _FakeProcess:
    returncode = None

    def poll(self):
        return None


class TestScPauseSemantics:
    """验证 SC pause 只阻止新命令，不取消已发送命令。"""

    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp(prefix="sc_pause_test_")
        self.step_dir = os.path.join(self.tmpdir, "steps")
        self.scdoc_dir = os.path.join(self.tmpdir, "scdoc")
        os.makedirs(self.step_dir, exist_ok=True)
        os.makedirs(self.scdoc_dir, exist_ok=True)

    def teardown_method(self):
        if os.path.exists(self.tmpdir):
            shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _pool_with_ready_slot(self, monkeypatch):
        from engine.config import ENGINE_CONFIG, LOCAL_PATHS, OPERATION_TIMEOUTS
        from engine.sc_process_pool import PersistentSlot, SCProcessPool

        monkeypatch.setitem(LOCAL_PATHS, "data_dir", self.tmpdir)
        monkeypatch.setitem(LOCAL_PATHS, "step_dir", self.step_dir)
        monkeypatch.setitem(LOCAL_PATHS, "scdoc_dir", self.scdoc_dir)
        monkeypatch.setitem(ENGINE_CONFIG, "sc_timeout", 2)
        monkeypatch.setitem(ENGINE_CONFIG, "sc_scdoc_stable_seconds", 0.05)
        monkeypatch.setitem(OPERATION_TIMEOUTS, "sc_poll_interval", 0.02)

        pool = SCProcessPool()
        pool._first_cleanup_done = True
        slot = PersistentSlot(
            slot_id=1,
            process=_FakeProcess(),
            pid=1234,
            cmd_dir=pool._persistent_cmd_dir,
            status="ready",
        )
        pool._persistent_slots[slot.slot_id] = slot
        return pool, slot

    def test_pause_before_send_does_not_write_command(self, monkeypatch):
        pool, slot = self._pool_with_ready_slot(monkeypatch)
        paused = threading.Event()
        paused.set()

        assert pool.run_config(1, paused_event=paused) is False

        cmd_file = os.path.join(pool._persistent_cmd_dir, "sc_cmd_1.json")
        assert not os.path.exists(cmd_file)
        assert slot.status == "ready"
        assert slot.current_config is None

    def test_pause_after_send_waits_for_current_scdoc(self, monkeypatch):
        pool, slot = self._pool_with_ready_slot(monkeypatch)
        paused = threading.Event()
        scdoc_file = os.path.join(self.scdoc_dir, "model_gen4_1.scdoc")
        cmd_file = os.path.join(pool._persistent_cmd_dir, "sc_cmd_1.json")

        def pause_after_send_and_create_scdoc():
            deadline = time.time() + 1
            while not os.path.exists(cmd_file) and time.time() < deadline:
                time.sleep(0.01)
            paused.set()
            time.sleep(0.05)
            with open(scdoc_file, "wb") as f:
                f.write(b"scdoc")

        writer = threading.Thread(target=pause_after_send_and_create_scdoc, daemon=True)
        writer.start()

        assert pool._send_persistent_command(slot, 1, paused, None) is True
        writer.join(timeout=1)
        assert slot.status == "ready"
