"""
===============================================================================
远程执行器完整测试 (M7) — 扩展 test_remote_executor.py

覆盖：
- execute_transfer: stopped before start, remote file exists (断点续传)
- _build_meshing_command: 命令格式验证
- _build_solver_command: 命令格式验证
- _load_last_sync_paths / _save_last_sync_paths: 同步状态持久化
- execute_meshing: 正常流程 (mock SSH)
- execute_solver: 正常流程 (mock SSH)
===============================================================================
"""
from __future__ import annotations

import os
import threading

from engine.config import ENGINE_CONFIG, LOCAL_PATHS, REMOTE_CONFIG, STATUS_ERROR
from executor.remote_executor import RemoteExecutor


class _StateRecorder:
    def __init__(self) -> None:
        self.status_updates: list[tuple[int, str, str, str]] = []

    def set_step_status(
        self,
        config_name: int,
        step_name: str,
        status: str,
        error_message: str = "",
    ) -> None:
        self.status_updates.append((config_name, step_name, status, error_message))


# ====================================================================
# execute_transfer 边界测试
# ====================================================================

class TestTransferEdgeCases:
    """Transfer 步骤的边界场景。"""

    def test_stopped_before_upload(self, tmp_path, monkeypatch):
        """stopped 标志置位时不开始上传。"""
        scdoc_dir = tmp_path / "scdoc"
        scdoc_dir.mkdir()
        (scdoc_dir / "model_gen4_5.scdoc").write_bytes(b"data")

        monkeypatch.setitem(LOCAL_PATHS, "scdoc_dir", str(scdoc_dir))
        monkeypatch.setitem(REMOTE_CONFIG, "scdoc_dir", r"D:\remote")

        stopped = threading.Event()
        stopped.set()

        class _SSH:
            def upload_file(self, *a, **kw):
                raise AssertionError("should not upload when stopped")

        executor = RemoteExecutor(_StateRecorder(), lambda: _SSH(), threading.RLock())
        executor.set_control_events(threading.Event(), stopped)
        assert executor.execute_transfer(5) is False

    def test_paused_before_upload(self, tmp_path, monkeypatch):
        """paused 标志置位时不开始上传。"""
        scdoc_dir = tmp_path / "scdoc"
        scdoc_dir.mkdir()
        (scdoc_dir / "model_gen4_6.scdoc").write_bytes(b"data")

        monkeypatch.setitem(LOCAL_PATHS, "scdoc_dir", str(scdoc_dir))
        monkeypatch.setitem(REMOTE_CONFIG, "scdoc_dir", r"D:\remote")

        paused = threading.Event()
        paused.set()

        class _SSH:
            def upload_file(self, *a, **kw):
                raise AssertionError("should not upload when paused")

        executor = RemoteExecutor(_StateRecorder(), lambda: _SSH(), threading.RLock())
        executor.set_control_events(paused, threading.Event())
        assert executor.execute_transfer(6) is False

    def test_remote_file_exists_skips_upload(self, tmp_path, monkeypatch):
        """远程文件已存在且大小 > 0 → 跳过上传（断点续传）。"""
        scdoc_dir = tmp_path / "scdoc"
        scdoc_dir.mkdir()
        (scdoc_dir / "model_gen4_7.scdoc").write_bytes(b"data")

        monkeypatch.setitem(LOCAL_PATHS, "scdoc_dir", str(scdoc_dir))
        monkeypatch.setitem(REMOTE_CONFIG, "scdoc_dir", r"D:\remote")

        class _SSH:
            def get_remote_file_size(self, remote_path: str):
                return 1024  # 远程文件已存在

            def upload_file(self, *a, **kw):
                raise AssertionError("should skip upload when remote exists")

        executor = RemoteExecutor(_StateRecorder(), lambda: _SSH(), threading.RLock())
        assert executor.execute_transfer(7) is True

    def test_missing_local_file_sets_error(self, tmp_path, monkeypatch):
        """本地文件不存在时标记 Error。"""
        monkeypatch.setitem(LOCAL_PATHS, "scdoc_dir", str(tmp_path / "nope"))
        monkeypatch.setitem(REMOTE_CONFIG, "scdoc_dir", r"D:\remote")

        state = _StateRecorder()
        executor = RemoteExecutor(state, lambda: None, threading.RLock())
        assert executor.execute_transfer(99) is False
        assert any(s[2] == STATUS_ERROR for s in state.status_updates)


# ====================================================================
# _build_meshing_command 测试
# ====================================================================

class TestBuildMeshingCommand:
    """验证远程 Meshing 命令构建。"""

    def test_command_contains_config_name(self, monkeypatch):
        """命令中包含构型号。"""
        monkeypatch.setitem(REMOTE_CONFIG, "conda_env", "pyfluent")
        monkeypatch.setitem(REMOTE_CONFIG, "conda_exe", r"C:\conda.exe")
        monkeypatch.setitem(REMOTE_CONFIG, "scripts_dir", r"D:\scripts")
        monkeypatch.setitem(REMOTE_CONFIG, "flag_dir", r"D:\flags")
        monkeypatch.setitem(REMOTE_CONFIG, "scdoc_dir", r"D:\scdoc")
        monkeypatch.setitem(REMOTE_CONFIG, "msh_dir", r"D:\msh")
        monkeypatch.setitem(REMOTE_CONFIG, "mpi_bin_dir", r"C:\mpi")
        monkeypatch.setitem(ENGINE_CONFIG, "meshing_processor_count", 8)

        executor = RemoteExecutor(_StateRecorder(), lambda: None, threading.RLock())
        command, flag_file = executor._build_meshing_command(5)

        assert "5" in command
        assert "batch_meshing_gen4.py" in command
        assert "run --no-capture-output -n pyfluent python -u" in command
        assert "--processor-count 8" in command
        assert "meshing_done_5.txt" in flag_file

    def test_command_contains_all_paths(self, monkeypatch):
        """命令中包含所有必需路径参数。"""
        monkeypatch.setitem(REMOTE_CONFIG, "conda_env", "pyfluent")
        monkeypatch.setitem(REMOTE_CONFIG, "conda_exe", r"C:\conda.exe")
        monkeypatch.setitem(REMOTE_CONFIG, "scripts_dir", r"D:\scripts")
        monkeypatch.setitem(REMOTE_CONFIG, "flag_dir", r"D:\flags")
        monkeypatch.setitem(REMOTE_CONFIG, "scdoc_dir", r"D:\scdoc")
        monkeypatch.setitem(REMOTE_CONFIG, "msh_dir", r"D:\msh")
        monkeypatch.setitem(REMOTE_CONFIG, "working_dir", r"D:\working")
        monkeypatch.setitem(REMOTE_CONFIG, "mpi_bin_dir", r"C:\mpi")
        monkeypatch.setitem(ENGINE_CONFIG, "meshing_processor_count", 4)

        executor = RemoteExecutor(_StateRecorder(), lambda: None, threading.RLock())
        command, _ = executor._build_meshing_command(1)

        assert "--mpi-bin-dir" in command
        assert "--workflow-path" in command
        assert "--journal-path" in command
        assert "--scdoc-dir" in command
        assert "--output-dir" in command
        assert '--working-dir "D:\\working"' in command
        assert "--processor-count 4" in command

    def test_invalid_meshing_processor_count_falls_back_to_default(self, monkeypatch):
        """无效 Meshing 核心数配置回退到保守默认值。"""
        monkeypatch.setitem(REMOTE_CONFIG, "conda_env", "pyfluent")
        monkeypatch.setitem(REMOTE_CONFIG, "conda_exe", r"C:\conda.exe")
        monkeypatch.setitem(REMOTE_CONFIG, "scripts_dir", r"D:\scripts")
        monkeypatch.setitem(REMOTE_CONFIG, "flag_dir", r"D:\flags")
        monkeypatch.setitem(REMOTE_CONFIG, "scdoc_dir", r"D:\scdoc")
        monkeypatch.setitem(REMOTE_CONFIG, "msh_dir", r"D:\msh")
        monkeypatch.setitem(REMOTE_CONFIG, "mpi_bin_dir", r"C:\mpi")
        monkeypatch.setitem(ENGINE_CONFIG, "meshing_processor_count", 0)

        executor = RemoteExecutor(_StateRecorder(), lambda: None, threading.RLock())
        command, _ = executor._build_meshing_command(1)

        assert "--processor-count 8" in command

    def test_timeout_cleanup_kills_tracked_remote_task(self, monkeypatch):
        """Meshing 超时清理时终止已跟踪的远程任务。"""
        monkeypatch.setitem(REMOTE_CONFIG, "flag_dir", r"D:\flags")
        monkeypatch.setitem(ENGINE_CONFIG, "meshing_timeout", 0)

        killed: list[str] = []

        class _SSH:
            def check_remote_file(self, _remote_path: str) -> bool:
                return False

            def kill_remote_task(self, task_name: str) -> bool:
                killed.append(task_name)
                return True

        executor = RemoteExecutor(_StateRecorder(), lambda: _SSH(), threading.RLock())
        executor._remote_tasks[7] = "AutoFluid_meshing"

        assert executor.wait_meshing_completion(7) is False
        assert killed == ["AutoFluid_meshing"]


class TestBuildSolverCommand:
    """验证远程 Solver 命令构建。"""

    def test_command_streams_conda_output(self, monkeypatch):
        """Solver 命令使用实时输出参数。"""
        monkeypatch.setitem(REMOTE_CONFIG, "conda_env", "pyfluent")
        monkeypatch.setitem(REMOTE_CONFIG, "conda_exe", r"C:\conda.exe")
        monkeypatch.setitem(REMOTE_CONFIG, "scripts_dir", r"D:\scripts")
        monkeypatch.setitem(REMOTE_CONFIG, "flag_dir", r"D:\flags")
        monkeypatch.setitem(REMOTE_CONFIG, "msh_dir", r"D:\msh")
        monkeypatch.setitem(REMOTE_CONFIG, "result_dir", r"D:\result")
        monkeypatch.setitem(REMOTE_CONFIG, "mpi_bin_dir", r"C:\mpi")
        monkeypatch.setitem(ENGINE_CONFIG, "solver_processor_count", 128)

        executor = RemoteExecutor(_StateRecorder(), lambda: None, threading.RLock())
        command, flag_file = executor._build_solver_command(2)

        assert "batch_solver_gen4.py" in command
        assert "run --no-capture-output -n pyfluent python -u" in command
        assert "--processor-count 128" in command
        assert "solver_done_2.txt" in flag_file

    def test_command_uses_configured_solver_processor_count(self, monkeypatch):
        """Solver 命令使用配置的核心数。"""
        monkeypatch.setitem(REMOTE_CONFIG, "conda_env", "pyfluent")
        monkeypatch.setitem(REMOTE_CONFIG, "conda_exe", r"C:\conda.exe")
        monkeypatch.setitem(REMOTE_CONFIG, "scripts_dir", r"D:\scripts")
        monkeypatch.setitem(REMOTE_CONFIG, "flag_dir", r"D:\flags")
        monkeypatch.setitem(REMOTE_CONFIG, "msh_dir", r"D:\msh")
        monkeypatch.setitem(REMOTE_CONFIG, "result_dir", r"D:\result")
        monkeypatch.setitem(REMOTE_CONFIG, "mpi_bin_dir", r"C:\mpi")
        monkeypatch.setitem(ENGINE_CONFIG, "solver_processor_count", 64)

        executor = RemoteExecutor(_StateRecorder(), lambda: None, threading.RLock())
        command, _ = executor._build_solver_command(3)

        assert "--processor-count 64" in command

    def test_invalid_solver_processor_count_falls_back_to_default(self, monkeypatch):
        """无效 Solver 核心数配置回退到默认值。"""
        monkeypatch.setitem(REMOTE_CONFIG, "conda_env", "pyfluent")
        monkeypatch.setitem(REMOTE_CONFIG, "conda_exe", r"C:\conda.exe")
        monkeypatch.setitem(REMOTE_CONFIG, "scripts_dir", r"D:\scripts")
        monkeypatch.setitem(REMOTE_CONFIG, "flag_dir", r"D:\flags")
        monkeypatch.setitem(REMOTE_CONFIG, "msh_dir", r"D:\msh")
        monkeypatch.setitem(REMOTE_CONFIG, "result_dir", r"D:\result")
        monkeypatch.setitem(REMOTE_CONFIG, "mpi_bin_dir", r"C:\mpi")
        monkeypatch.setitem(ENGINE_CONFIG, "solver_processor_count", 0)

        executor = RemoteExecutor(_StateRecorder(), lambda: None, threading.RLock())
        command, _ = executor._build_solver_command(3)

        assert "--processor-count 128" in command


# ====================================================================
# 同步状态持久化测试
# ====================================================================

class TestSyncStatePersistence:
    """验证 _load_last_sync_paths / _save_last_sync_paths。"""

    def test_save_and_load(self, tmp_path, monkeypatch):
        """保存后加载应返回相同值。"""
        data_dir = str(tmp_path / "data")
        os.makedirs(data_dir, exist_ok=True)
        monkeypatch.setitem(LOCAL_PATHS, "data_dir", data_dir)
        monkeypatch.setitem(REMOTE_CONFIG, "scripts_dir", r"D:\scripts")
        monkeypatch.setitem(REMOTE_CONFIG, "ref_files_dir", r"D:\refs")

        executor = RemoteExecutor(_StateRecorder(), lambda: None, threading.RLock())
        executor._save_last_sync_paths()

        loaded = executor._load_last_sync_paths()
        assert loaded["scripts_dir"] == r"D:\scripts"
        assert loaded["ref_files_dir"] == r"D:\refs"

    def test_load_nonexistent_returns_empty(self, tmp_path, monkeypatch):
        """文件不存在时返回空字典。"""
        monkeypatch.setitem(LOCAL_PATHS, "data_dir", str(tmp_path / "nope"))

        executor = RemoteExecutor(_StateRecorder(), lambda: None, threading.RLock())
        assert executor._load_last_sync_paths() == {}

    def test_load_corrupted_json_returns_empty(self, tmp_path, monkeypatch):
        """JSON 解析失败时返回空字典。"""
        data_dir = str(tmp_path / "data")
        os.makedirs(data_dir, exist_ok=True)
        state_file = os.path.join(data_dir, "last_sync_paths.json")
        with open(state_file, "w") as f:
            f.write("not json {{{")

        monkeypatch.setitem(LOCAL_PATHS, "data_dir", data_dir)

        executor = RemoteExecutor(_StateRecorder(), lambda: None, threading.RLock())
        assert executor._load_last_sync_paths() == {}


# ====================================================================
# execute_meshing / execute_solver 测试
# ====================================================================

class TestExecuteMeshing:
    """Meshing 步骤测试（mock SSH）。"""

    def test_execute_meshing_delegates_to_run_command(self, monkeypatch):
        """execute_meshing 正确委托给 _run_meshing_command。"""
        executor = RemoteExecutor(_StateRecorder(), lambda: None, threading.RLock())
        monkeypatch.setattr(executor, "_run_meshing_command", lambda cn, lp="[Meshing]": True)
        assert executor.execute_meshing(1) is True

    def test_non_int_config_returns_false(self):
        """非整数构型号返回 False（防注入）。"""
        executor = RemoteExecutor(_StateRecorder(), lambda: None, threading.RLock())
        assert executor.execute_meshing("1") is False
        assert executor.execute_meshing(1.5) is False

    def test_run_meshing_uses_configured_working_dir(self, monkeypatch):
        """Meshing 后台任务使用仿真工作目录启动 Fluent。"""
        monkeypatch.setitem(REMOTE_CONFIG, "conda_env", "pyfluent")
        monkeypatch.setitem(REMOTE_CONFIG, "conda_exe", r"C:\conda.exe")
        monkeypatch.setitem(REMOTE_CONFIG, "scripts_dir", r"D:\scripts")
        monkeypatch.setitem(REMOTE_CONFIG, "working_dir", r"D:\working")
        monkeypatch.setitem(REMOTE_CONFIG, "flag_dir", r"D:\flags")
        monkeypatch.setitem(REMOTE_CONFIG, "scdoc_dir", r"D:\scdoc")
        monkeypatch.setitem(REMOTE_CONFIG, "msh_dir", r"D:\msh")
        monkeypatch.setitem(REMOTE_CONFIG, "mpi_bin_dir", r"C:\mpi")
        monkeypatch.setitem(ENGINE_CONFIG, "meshing_processor_count", 8)
        captured: dict[str, str | None] = {}

        class _SSH:
            def exec_background(
                self,
                command: str,
                flag_file: str,
                *,
                working_dir: str | None = None,
                interactive: bool = False,
            ) -> tuple[bool, str]:
                captured["command"] = command
                captured["flag_file"] = flag_file
                captured["working_dir"] = working_dir
                captured["interactive"] = str(interactive)
                return (True, "AutoFluid_meshing")

        executor = RemoteExecutor(_StateRecorder(), lambda: _SSH(), threading.RLock())
        monkeypatch.setattr(executor, "sync_scripts", lambda: True)

        assert executor._run_meshing_command(2) is True
        assert captured["working_dir"] == r"D:\working"
        assert captured["interactive"] == "True"
        assert '--working-dir "D:\\working"' in str(captured["command"])

    def test_wait_meshing_completion_cleans_remote_task(self, monkeypatch):
        """Meshing 完成后清理计划任务条目，避免远程任务列表堆积。"""
        monkeypatch.setitem(REMOTE_CONFIG, "flag_dir", r"D:\flags")
        monkeypatch.setitem(ENGINE_CONFIG, "meshing_timeout", 30)

        deleted: list[str] = []
        killed: list[str] = []

        class _SSH:
            def check_remote_file(self, path: str) -> bool:
                return path == "D:/flags/meshing_done_4.txt"

            def delete_remote_file(self, path: str) -> bool:
                deleted.append(path)
                return True

            def kill_remote_task(self, task_name: str) -> bool:
                killed.append(task_name)
                return True

        executor = RemoteExecutor(_StateRecorder(), lambda: _SSH(), threading.RLock())
        executor._remote_tasks[4] = "AutoFluid_done_task"

        assert executor.wait_meshing_completion(4) is True
        assert deleted == ["D:/flags/meshing_done_4.txt"]
        assert killed == ["AutoFluid_done_task"]
        assert 4 not in executor._remote_tasks


class TestExecuteSolver:
    """Solver 步骤测试（mock SSH）。"""

    def test_non_int_config_returns_false(self):
        """非整数构型号返回 False（防注入）。"""
        executor = RemoteExecutor(_StateRecorder(), lambda: None, threading.RLock())
        assert executor.execute_solver("1") is False
        assert executor.execute_solver(1.5) is False
