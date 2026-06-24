from __future__ import annotations

import json
import hashlib
import threading

import pytest

from engine.config import (
    DEFAULT_WORKSTATION_ID,
    LOCAL_PATHS,
    REMOTE_CONFIG,
    ENGINE_CONFIG,
    OPERATION_TIMEOUTS,
    STATUS_ERROR,
)
import executor.remote_executor as remote_executor_module
from executor.remote_executor import RemoteExecutor


@pytest.fixture(autouse=True)
def _default_non_server_mode(monkeypatch):
    monkeypatch.delenv("AUTOFLUID_SERVER_MODE", raising=False)


class _GuardLock:
    def __init__(self) -> None:
        self.held = False
        self.entries = 0

    def __enter__(self):
        self.held = True
        self.entries += 1
        return self

    def __exit__(self, exc_type, exc, tb):
        self.held = False
        return False


class _GuardedRemoteTasks(dict):
    def __init__(self, guard: _GuardLock) -> None:
        super().__init__()
        self.guard = guard

    def _require_lock(self) -> None:
        assert self.guard.held, "remote task mapping accessed without lock"

    def __setitem__(self, key, value):
        self._require_lock()
        return super().__setitem__(key, value)

    def pop(self, key, default=None):
        self._require_lock()
        return super().pop(key, default)

    def clear(self):
        self._require_lock()
        return super().clear()

    def get(self, key, default=None):
        self._require_lock()
        return super().get(key, default)

class _StateRecorder:
    def __init__(self) -> None:
        self.status_updates: list[tuple[int, str, str, str]] = []
        self.remote_tasks: dict[tuple[int, str], dict[str, object]] = {}
        self.solver_progress_updates: list[dict[str, object]] = []
        self.solver_progress_clears = 0

    def set_step_status(
        self,
        config_name: int,
        step_name: str,
        status: str,
        error_message: str = "",
    ) -> None:
        self.status_updates.append((config_name, step_name, status, error_message))

    def save_remote_task(self, **kwargs) -> None:
        key = self._remote_task_key(
            kwargs["config_name"],
            kwargs["step_name"],
            str(kwargs.get("workstation_id", DEFAULT_WORKSTATION_ID)),
        )
        self.remote_tasks[key] = dict(kwargs)

    def get_remote_task(
        self,
        config_name: int,
        step_name: str,
        workstation_id: str = DEFAULT_WORKSTATION_ID,
    ):
        return self.remote_tasks.get(
            self._remote_task_key(config_name, step_name, workstation_id)
        )

    def get_all_remote_tasks(self):
        return list(self.remote_tasks.values())

    def delete_remote_task(
        self,
        config_name: int,
        step_name: str,
        workstation_id: str = DEFAULT_WORKSTATION_ID,
    ) -> None:
        self.remote_tasks.pop(
            self._remote_task_key(config_name, step_name, workstation_id),
            None,
        )

    def set_solver_progress(self, progress: dict[str, object]) -> None:
        self.solver_progress_updates.append(progress)

    def clear_solver_progress(self) -> None:
        self.solver_progress_clears += 1

    @staticmethod
    def _remote_task_key(
        config_name: object,
        step_name: object,
        workstation_id: str = DEFAULT_WORKSTATION_ID,
    ) -> tuple[object, ...]:
        if workstation_id == DEFAULT_WORKSTATION_ID:
            return (config_name, step_name)
        return (workstation_id, config_name, step_name)


def test_execute_transfer_passes_timeout_and_control_events(tmp_path, monkeypatch):
    scdoc_dir = tmp_path / "scdoc"
    scdoc_dir.mkdir()
    scdoc_file = scdoc_dir / "model_gen4_1.scdoc"
    scdoc_file.write_bytes(b"scdoc")

    monkeypatch.setitem(LOCAL_PATHS, "scdoc_dir", str(scdoc_dir))
    monkeypatch.setitem(REMOTE_CONFIG, "scdoc_dir", r"D:\remote scdoc")
    monkeypatch.setitem(ENGINE_CONFIG, "transfer_timeout", 17)
    monkeypatch.setitem(OPERATION_TIMEOUTS, "ssh_upload_max_retries", 2)
    monotonic_values = iter([100.0, 100.0, 100.0])
    monkeypatch.setattr(
        remote_executor_module.time,
        "monotonic",
        lambda: next(monotonic_values),
    )

    paused = threading.Event()
    stopped = threading.Event()
    calls: list[dict[str, object]] = []
    size_checks: list[dict[str, object]] = []

    class _SSH:
        def get_remote_file_size(self, remote_path: str, *, timeout: int | None = None):
            size_checks.append({"remote_path": remote_path, "timeout": timeout})
            return None

        def upload_file(
            self,
            local_path: str,
            remote_path: str,
            *,
            timeout: int,
            max_retries: int,
            paused_event: threading.Event | None,
            stopped_event: threading.Event | None,
        ) -> bool:
            calls.append(
                {
                    "local_path": local_path,
                    "remote_path": remote_path,
                    "timeout": timeout,
                    "max_retries": max_retries,
                    "paused_event": paused_event,
                    "stopped_event": stopped_event,
                }
            )
            return True

    executor = RemoteExecutor(_StateRecorder(), lambda: _SSH(), threading.RLock())
    executor.set_control_events(paused, stopped)

    assert executor.execute_transfer(1) is True
    assert size_checks == [
        {
            "remote_path": "D:/remote scdoc/model_gen4_1.scdoc",
            "timeout": 17,
        }
    ]
    assert calls == [
        {
            "local_path": str(scdoc_file),
            "remote_path": "D:/remote scdoc/model_gen4_1.scdoc",
            "timeout": 17,
            "max_retries": 2,
            "paused_event": paused,
            "stopped_event": stopped,
        }
    ]


def test_execute_transfer_reads_worker_scdoc_from_data_dir_in_server_mode(
    tmp_path,
    monkeypatch,
):
    data_scdoc_dir = tmp_path / "data" / "scdoc"
    local_scdoc_dir = tmp_path / "local_scdoc"
    data_scdoc_dir.mkdir(parents=True)
    local_scdoc_dir.mkdir()
    scdoc_file = data_scdoc_dir / "model_gen4_5.scdoc"
    scdoc_file.write_bytes(b"server-mode-scdoc")

    monkeypatch.setenv("AUTOFLUID_SERVER_MODE", "server")
    monkeypatch.setitem(LOCAL_PATHS, "data_dir", str(tmp_path / "data"))
    monkeypatch.setitem(LOCAL_PATHS, "scdoc_dir", str(local_scdoc_dir))
    monkeypatch.setitem(REMOTE_CONFIG, "scdoc_dir", r"D:\remote scdoc")

    uploaded: list[str] = []

    class _SSH:
        def get_remote_file_size(self, remote_path: str, *, timeout: float | None = None):
            return None

        def upload_file(self, local_path: str, remote_path: str, **kwargs) -> bool:
            uploaded.append(local_path)
            return True

    executor = RemoteExecutor(_StateRecorder(), lambda: _SSH(), threading.RLock())

    assert executor.execute_transfer(5) is True
    assert uploaded == [str(scdoc_file)]


def test_execute_transfer_uses_workstation_specific_remote_dir(tmp_path, monkeypatch):
    scdoc_dir = tmp_path / "scdoc"
    scdoc_dir.mkdir()
    scdoc_file = scdoc_dir / "model_gen4_1.scdoc"
    scdoc_file.write_bytes(b"scdoc")

    monkeypatch.setitem(LOCAL_PATHS, "scdoc_dir", str(scdoc_dir))
    monkeypatch.setitem(REMOTE_CONFIG, "scdoc_dir", r"D:\default scdoc")
    monkeypatch.setattr(
        remote_executor_module,
        "get_workstation_config",
        lambda workstation_id: {
            **REMOTE_CONFIG,
            "id": workstation_id,
            "scdoc_dir": r"E:\ws-a scdoc",
        },
    )

    requested_workstations: list[str] = []
    uploaded: list[str] = []

    class _SSH:
        def get_remote_file_size(self, remote_path: str, *, timeout: int | None = None):
            return None

        def upload_file(self, _local_path: str, remote_path: str, **_kwargs) -> bool:
            uploaded.append(remote_path)
            return True

    def _get_ssh(workstation_id: str):
        requested_workstations.append(workstation_id)
        return _SSH()

    executor = RemoteExecutor(_StateRecorder(), _get_ssh, threading.RLock())

    assert executor.execute_transfer(1, workstation_id="WS-A") is True
    assert requested_workstations == ["WS-A"]
    assert uploaded == ["E:/ws-a scdoc/model_gen4_1.scdoc"]


def test_execute_transfer_uses_one_timeout_budget(tmp_path, monkeypatch):
    scdoc_dir = tmp_path / "scdoc"
    scdoc_dir.mkdir()
    scdoc_file = scdoc_dir / "model_gen4_7.scdoc"
    scdoc_file.write_bytes(b"scdoc")

    monkeypatch.setitem(LOCAL_PATHS, "scdoc_dir", str(scdoc_dir))
    monkeypatch.setitem(REMOTE_CONFIG, "scdoc_dir", r"D:\remote scdoc")
    monkeypatch.setitem(ENGINE_CONFIG, "transfer_timeout", 17)

    monotonic_values = iter([100.0, 100.0, 105.0])
    monkeypatch.setattr(
        remote_executor_module.time,
        "monotonic",
        lambda: next(monotonic_values),
    )

    timeouts: list[float] = []

    class _SSH:
        def get_remote_file_size(self, remote_path: str, *, timeout: float | None = None):
            timeouts.append(float(timeout or 0))
            return None

        def upload_file(self, local_path: str, remote_path: str, **kwargs) -> bool:
            timeouts.append(float(kwargs["timeout"]))
            return True

    executor = RemoteExecutor(_StateRecorder(), lambda: _SSH(), threading.RLock())

    assert executor.execute_transfer(7) is True
    assert timeouts == [17.0, 12.0]


def test_execute_transfer_fails_when_timeout_budget_is_exhausted_before_upload(
    tmp_path,
    monkeypatch,
):
    scdoc_dir = tmp_path / "scdoc"
    scdoc_dir.mkdir()
    (scdoc_dir / "model_gen4_8.scdoc").write_bytes(b"scdoc")

    monkeypatch.setitem(LOCAL_PATHS, "scdoc_dir", str(scdoc_dir))
    monkeypatch.setitem(REMOTE_CONFIG, "scdoc_dir", r"D:\remote scdoc")
    monkeypatch.setitem(ENGINE_CONFIG, "transfer_timeout", 17)

    monotonic_values = iter([100.0, 100.0, 118.0])
    monkeypatch.setattr(
        remote_executor_module.time,
        "monotonic",
        lambda: next(monotonic_values),
    )

    class _SSH:
        def get_remote_file_size(self, remote_path: str, *, timeout: float | None = None):
            return None

        def upload_file(self, local_path: str, remote_path: str, **kwargs) -> bool:
            raise AssertionError("timeout-exhausted transfer must not upload")

    state = _StateRecorder()
    executor = RemoteExecutor(state, lambda: _SSH(), threading.RLock())

    assert executor.execute_transfer(8) is False
    assert state.status_updates[-1] == (
        8,
        "transfer",
        STATUS_ERROR,
        "文件传输超时",
    )


def test_execute_transfer_falls_back_when_upload_retries_invalid(tmp_path, monkeypatch):
    scdoc_dir = tmp_path / "scdoc"
    scdoc_dir.mkdir()
    scdoc_file = scdoc_dir / "model_gen4_6.scdoc"
    scdoc_file.write_bytes(b"scdoc")

    monkeypatch.setitem(LOCAL_PATHS, "scdoc_dir", str(scdoc_dir))
    monkeypatch.setitem(REMOTE_CONFIG, "scdoc_dir", r"D:\remote scdoc")
    monkeypatch.setitem(OPERATION_TIMEOUTS, "ssh_upload_max_retries", "bad")

    calls: list[int] = []

    class _SSH:
        def get_remote_file_size(self, remote_path: str):
            return None

        def upload_file(self, local_path: str, remote_path: str, **kwargs) -> bool:
            calls.append(kwargs["max_retries"])
            return True

    executor = RemoteExecutor(_StateRecorder(), lambda: _SSH(), threading.RLock())

    assert executor.execute_transfer(6) is True
    assert calls == [3]


def test_check_meshing_outputs_exist_holds_ssh_lock_and_passes_timeout(monkeypatch):
    monkeypatch.setitem(REMOTE_CONFIG, "flag_dir", r"D:\flags")
    monkeypatch.setitem(REMOTE_CONFIG, "msh_dir", r"D:\msh")

    inside_lock = False
    lock_entries: list[str] = []
    calls: list[tuple[str, float | None, bool]] = []

    class _Lock:
        def __enter__(self):
            nonlocal inside_lock
            inside_lock = True
            lock_entries.append("enter")

        def __exit__(self, exc_type, exc, tb):
            nonlocal inside_lock
            inside_lock = False
            lock_entries.append("exit")

    class _SSH:
        def check_remote_file(
            self,
            remote_path: str,
            *,
            timeout: float | None = None,
        ) -> bool:
            calls.append((remote_path, timeout, inside_lock))
            return remote_path.endswith(".msh.h5")

    executor = RemoteExecutor(_StateRecorder(), lambda: _SSH(), _Lock())

    assert executor.check_meshing_outputs_exist(4, timeout=13) is True
    assert lock_entries == ["enter", "exit"]
    assert calls == [
        ("D:/msh/model_gen4_4.msh.h5", 13, True),
    ]


def test_check_meshing_outputs_exist_ignores_stale_done_flag_without_mesh(monkeypatch):
    monkeypatch.setitem(REMOTE_CONFIG, "flag_dir", r"D:\flags")
    monkeypatch.setitem(REMOTE_CONFIG, "msh_dir", r"D:\msh")

    calls: list[str] = []

    class _SSH:
        def check_remote_file(self, remote_path: str) -> bool:
            calls.append(remote_path)
            return remote_path == "D:/flags/meshing_done_4.txt"

    executor = RemoteExecutor(_StateRecorder(), lambda: _SSH(), threading.RLock())

    assert executor.check_meshing_outputs_exist(4) is False
    assert calls == ["D:/msh/model_gen4_4.msh.h5"]


def test_check_meshing_outputs_exist_uses_independent_workstation_locks(monkeypatch):
    monkeypatch.setitem(REMOTE_CONFIG, "msh_dir", r"D:\msh")
    monkeypatch.setattr(
        remote_executor_module,
        "get_workstation_config",
        lambda workstation_id: {
            **REMOTE_CONFIG,
            "id": workstation_id,
            "msh_dir": rf"D:\{workstation_id}\msh",
        },
    )

    entered: list[str] = []
    release = threading.Event()

    class _SSH:
        def __init__(self, workstation_id: str) -> None:
            self.workstation_id = workstation_id

        def check_remote_file(self, remote_path: str) -> bool:
            entered.append(self.workstation_id)
            if self.workstation_id == "WS-A":
                release.wait(timeout=2)
            return remote_path.endswith(".msh.h5")

    def _get_ssh(workstation_id: str) -> _SSH:
        return _SSH(workstation_id)

    executor = RemoteExecutor(_StateRecorder(), _get_ssh, threading.RLock())
    thread_a = threading.Thread(
        target=lambda: executor.check_meshing_outputs_exist(1, workstation_id="WS-A")
    )
    thread_a.start()

    deadline = remote_executor_module.time.time() + 1.0
    while entered != ["WS-A"] and remote_executor_module.time.time() < deadline:
        remote_executor_module.time.sleep(0.01)

    try:
        started_at = remote_executor_module.time.monotonic()
        assert executor.check_meshing_outputs_exist(2, workstation_id="WS-B") is True
        elapsed = remote_executor_module.time.monotonic() - started_at
        assert "WS-B" in entered
        assert elapsed < 0.5
    finally:
        release.set()
        thread_a.join(timeout=2)


def test_check_meshing_done_uses_workstation_specific_paths_and_lock(monkeypatch):
    monkeypatch.setitem(REMOTE_CONFIG, "flag_dir", r"D:\default_flags")
    monkeypatch.setattr(
        remote_executor_module,
        "get_workstation_config",
        lambda workstation_id: {
            **REMOTE_CONFIG,
            "id": workstation_id,
            "flag_dir": rf"D:\{workstation_id}\flags",
        },
    )

    requested_workstations: list[str] = []
    checked: list[str] = []
    deleted: list[str] = []

    class _SSH:
        def check_remote_file(self, remote_path: str) -> bool:
            checked.append(remote_path)
            return True

        def delete_remote_file(self, remote_path: str) -> None:
            deleted.append(remote_path)

    def _get_ssh(workstation_id: str) -> _SSH:
        requested_workstations.append(workstation_id)
        return _SSH()

    executor = RemoteExecutor(_StateRecorder(), _get_ssh, threading.RLock())

    assert executor.check_meshing_done(3, workstation_id="WS-C") is True
    assert requested_workstations == ["WS-C"]
    assert checked == ["D:/WS-C/flags/meshing_done_3.txt"]
    assert deleted == ["D:/WS-C/flags/meshing_done_3.txt"]


def test_execute_transfer_deletes_partial_remote_file_on_upload_failure(tmp_path, monkeypatch):
    scdoc_dir = tmp_path / "scdoc"
    scdoc_dir.mkdir()
    (scdoc_dir / "model_gen4_2.scdoc").write_bytes(b"scdoc")

    monkeypatch.setitem(LOCAL_PATHS, "scdoc_dir", str(scdoc_dir))
    monkeypatch.setitem(REMOTE_CONFIG, "scdoc_dir", r"D:\remote scdoc")

    deleted: list[str] = []

    class _SSH:
        def upload_file(self, local_path: str, remote_path: str, **kwargs) -> bool:
            return False

        def delete_remote_file(self, remote_path: str) -> bool:
            deleted.append(remote_path)
            return True

        def kill_remote_task(self, task_name: str) -> bool:
            return True

    state = _StateRecorder()
    executor = RemoteExecutor(state, lambda: _SSH(), threading.RLock())

    assert executor.execute_transfer(2) is False
    assert deleted == ["D:/remote scdoc/model_gen4_2.scdoc"]
    assert state.status_updates[-1] == (
        2,
        "transfer",
        STATUS_ERROR,
        "SFTP 上传失败",
    )


def test_execute_transfer_rejects_empty_local_scdoc(tmp_path, monkeypatch):
    scdoc_dir = tmp_path / "scdoc"
    scdoc_dir.mkdir()
    (scdoc_dir / "model_gen4_4.scdoc").write_bytes(b"")

    monkeypatch.setitem(LOCAL_PATHS, "scdoc_dir", str(scdoc_dir))
    monkeypatch.setitem(REMOTE_CONFIG, "scdoc_dir", r"D:\remote scdoc")

    class _SSH:
        def upload_file(self, local_path: str, remote_path: str, **kwargs) -> bool:
            raise AssertionError("empty SCDOC must not be uploaded")

    state = _StateRecorder()
    executor = RemoteExecutor(state, lambda: _SSH(), threading.RLock())

    assert executor.execute_transfer(4) is False
    assert state.status_updates[-1] == (
        4,
        "transfer",
        STATUS_ERROR,
        "本地 SCDOC 文件为空",
    )


def test_execute_transfer_does_not_mark_error_when_pause_interrupts_upload(
    tmp_path,
    monkeypatch,
):
    scdoc_dir = tmp_path / "scdoc"
    scdoc_dir.mkdir()
    (scdoc_dir / "model_gen4_3.scdoc").write_bytes(b"scdoc")

    monkeypatch.setitem(LOCAL_PATHS, "scdoc_dir", str(scdoc_dir))
    monkeypatch.setitem(REMOTE_CONFIG, "scdoc_dir", r"D:\remote scdoc")

    paused = threading.Event()
    stopped = threading.Event()
    deleted: list[str] = []

    class _SSH:
        def upload_file(self, local_path: str, remote_path: str, **kwargs) -> bool:
            paused.set()
            return False

        def delete_remote_file(self, remote_path: str) -> bool:
            deleted.append(remote_path)
            return True

        def kill_remote_task(self, task_name: str) -> bool:
            return True

    state = _StateRecorder()
    executor = RemoteExecutor(state, lambda: _SSH(), threading.RLock())
    executor.set_control_events(paused, stopped)

    assert executor.execute_transfer(3) is False
    assert deleted == ["D:/remote scdoc/model_gen4_3.scdoc"]
    assert state.status_updates == []


def test_sync_file_group_does_not_upload_when_remote_hash_lookup_fails(tmp_path):
    (tmp_path / "alpha.txt").write_bytes(b"content")

    class _SSH:
        def get_remote_combined_file_hash(self, remote_dir: str, filenames: list[str]):
            return None

        def get_remote_file_hashes(self, remote_dir: str, filenames: list[str]):
            raise ConnectionError("hash lookup failed")

        def upload_file(self, *args, **kwargs):
            raise AssertionError("must not upload when remote hash lookup fails")

    executor = RemoteExecutor(_StateRecorder(), lambda: _SSH(), threading.RLock())

    assert executor._sync_file_group(
        str(tmp_path),
        r"D:\remote",
        ["alpha.txt"],
        "测试",
    ) is False


def test_sync_scripts_skips_remote_hash_after_successful_unchanged_sync(
    tmp_path,
    monkeypatch,
):
    scripts_dir = tmp_path / "remote_scripts"
    ref_dir = scripts_dir / "fluent_chemkin_files"
    data_dir = tmp_path / "data"
    scripts_dir.mkdir()
    ref_dir.mkdir()
    data_dir.mkdir()
    (scripts_dir / "alpha.txt").write_bytes(b"script")
    (ref_dir / "beta.txt").write_bytes(b"ref")

    monkeypatch.setitem(LOCAL_PATHS, "remote_scripts_dir", str(scripts_dir))
    monkeypatch.setitem(LOCAL_PATHS, "data_dir", str(data_dir))
    monkeypatch.setitem(REMOTE_CONFIG, "scripts_dir", r"D:\scripts")
    monkeypatch.setitem(REMOTE_CONFIG, "ref_files_dir", r"D:\refs")
    monkeypatch.setattr(remote_executor_module, "REMOTE_SCRIPT_FILES", ["alpha.txt"])
    monkeypatch.setattr(remote_executor_module, "REMOTE_REF_FILES", ["beta.txt"])

    script_hash = hashlib.md5(b"script").hexdigest()
    ref_hash = hashlib.md5(b"ref").hexdigest()
    expected_combined = {
        ("D:\\scripts", ("alpha.txt",)): RemoteExecutor._compute_combined_hash(
            {"alpha.txt": script_hash}
        ),
        ("D:\\refs", ("beta.txt",)): RemoteExecutor._compute_combined_hash(
            {"beta.txt": ref_hash}
        ),
    }
    combined_calls: list[tuple[str, tuple[str, ...]]] = []

    class _SSH:
        def get_remote_combined_file_hash(
            self,
            remote_dir: str,
            filenames: list[str],
        ) -> str:
            key = (remote_dir, tuple(filenames))
            combined_calls.append(key)
            return expected_combined[key]

        def get_remote_file_hashes(self, remote_dir: str, filenames: list[str]):
            raise AssertionError("combined hash match should skip per-file hashes")

        def upload_file(self, *args, **kwargs):
            raise AssertionError("unchanged remote files should not be uploaded")

    executor = RemoteExecutor(_StateRecorder(), lambda: _SSH(), threading.RLock())

    assert executor.sync_scripts() is True
    assert executor.sync_scripts() is True
    assert combined_calls == [
        ("D:\\scripts", ("alpha.txt",)),
        ("D:\\refs", ("beta.txt",)),
    ]


def test_wait_meshing_completion_returns_false_immediately_on_error_flag(monkeypatch):
    monkeypatch.setitem(REMOTE_CONFIG, "flag_dir", r"D:\flags")
    monkeypatch.setitem(ENGINE_CONFIG, "meshing_timeout", 60)

    error_flag = "D:/flags/meshing_done_3.txt.error"
    checked: list[str] = []
    deleted: list[str] = []
    read_paths: list[str] = []
    cleaned: list[tuple[str, str | None]] = []

    class _SSH:
        def check_remote_file(self, remote_path: str) -> bool:
            checked.append(remote_path)
            return remote_path.replace("\\", "/") == error_flag

        def read_remote_text_file(self, remote_path: str, *, timeout: float | None = None):
            read_paths.append(remote_path)
            normalized = remote_path.replace("\\", "/")
            if normalized == error_flag:
                return "REMOTE_ERROR_FLAG"
            if normalized == "D:/logs/meshing_3.log":
                return "\n".join(
                    [
                        "line before",
                        "FileNotFoundError: MPI bin 目录不存在",
                        "ERROR conda.cli.main_run",
                    ]
                )
            return ""

        def delete_remote_file(self, remote_path: str) -> bool:
            deleted.append(remote_path)
            return True

        def cleanup_remote_task_entry(self, task_name: str, pid_file: str | None = None) -> bool:
            cleaned.append((task_name, pid_file))
            return True

    state = _StateRecorder()
    state.remote_tasks[(3, "meshing")] = {
        "config_name": 3,
        "step_name": "meshing",
        "task_name": "AutoFluid_meshing",
        "flag_file": "D:/flags/meshing_done_3.txt",
        "error_flag_file": error_flag,
        "log_file": "D:/logs/meshing_3.log",
        "pid_file": "D:/flags/autofluid_bg_meshing.pid",
        "started_at": 0.0,
    }
    executor = RemoteExecutor(state, lambda: _SSH(), threading.RLock())
    executor._remote_tasks[3] = "AutoFluid_meshing"
    monkeypatch.setattr(
        "executor.remote_executor.time.sleep",
        lambda _: (_ for _ in ()).throw(
            AssertionError("error flag should stop Meshing polling immediately")
        ),
    )
    monkeypatch.setattr(remote_executor_module.time, "time", lambda: 0.0)

    assert executor.wait_meshing_completion(3) is False
    assert [path.replace("\\", "/") for path in checked] == [error_flag]
    assert [path.replace("\\", "/") for path in read_paths] == [
        error_flag,
        "D:/logs/meshing_3.log",
    ]
    assert "FileNotFoundError: MPI bin" in executor.last_meshing_error
    assert "ERROR conda.cli.main_run" in executor.last_meshing_error
    assert deleted == [error_flag]
    assert cleaned == [("AutoFluid_meshing", "D:/flags/autofluid_bg_meshing.pid")]
    assert 3 not in executor._remote_tasks
    assert (3, "meshing") not in state.remote_tasks


def test_wait_meshing_completion_requires_mesh_after_done_flag(monkeypatch):
    monkeypatch.setitem(REMOTE_CONFIG, "flag_dir", r"D:\flags")
    monkeypatch.setitem(REMOTE_CONFIG, "msh_dir", r"D:\msh")
    monkeypatch.setitem(ENGINE_CONFIG, "meshing_timeout", 120)

    flag_file = "D:/flags/meshing_done_4.txt"
    mesh_file = "D:/msh/model_gen4_4.msh.h5"
    checked: list[str] = []
    deleted: list[str] = []
    times = iter([0.0, 0.0, 61.0, 61.0])

    class _SSH:
        def check_remote_file(self, remote_path: str) -> bool:
            checked.append(remote_path)
            return remote_path == flag_file

        def delete_remote_file(self, remote_path: str) -> bool:
            deleted.append(remote_path)
            return True

        def cleanup_remote_task_entry(self, task_name: str, pid_file: str | None = None) -> bool:
            return True

    state = _StateRecorder()
    state.remote_tasks[(4, "meshing")] = {
        "config_name": 4,
        "step_name": "meshing",
        "task_name": "AutoFluid_meshing",
        "flag_file": flag_file,
        "error_flag_file": f"{flag_file}.error",
        "pid_file": "D:/flags/autofluid_bg_meshing.pid",
        "started_at": 0.0,
    }
    executor = RemoteExecutor(state, lambda: _SSH(), threading.RLock())
    executor._remote_tasks[4] = "AutoFluid_meshing"
    monkeypatch.setattr(remote_executor_module.time, "time", lambda: next(times))
    monkeypatch.setattr(remote_executor_module.time, "sleep", lambda _seconds: None)

    assert executor.wait_meshing_completion(4) is False
    assert checked == [
        f"{flag_file}.error",
        flag_file,
        mesh_file,
    ]
    assert deleted == [flag_file]
    assert (4, "meshing") not in state.remote_tasks


def test_wait_solver_completion_returns_false_immediately_on_error_flag(monkeypatch):
    monkeypatch.setitem(REMOTE_CONFIG, "flag_dir", r"D:\flags")
    monkeypatch.setitem(REMOTE_CONFIG, "result_dir", r"D:\result")
    monkeypatch.setitem(ENGINE_CONFIG, "solver_timeout", 60)

    error_flag = "D:/flags/solver_done_4.txt.error"
    checked: list[str] = []
    deleted: list[str] = []

    class _SSH:
        def check_remote_file(self, remote_path: str) -> bool:
            checked.append(remote_path)
            return remote_path == error_flag

        def delete_remote_file(self, remote_path: str) -> bool:
            deleted.append(remote_path)
            return True

        def kill_remote_task(self, task_name: str) -> bool:
            return True

    state = _StateRecorder()
    executor = RemoteExecutor(state, lambda: _SSH(), threading.RLock())
    executor._remote_tasks[4] = "AutoFluid_solver"
    monkeypatch.setattr(
        "executor.remote_executor.time.sleep",
        lambda _: (_ for _ in ()).throw(
            AssertionError("error flag should stop Solver polling immediately")
        ),
    )

    assert executor.wait_solver_completion(4) is False
    assert checked == [error_flag]
    assert deleted == [error_flag, "D:/flags/solver_progress_4.json"]
    assert 4 not in executor._remote_tasks
    assert state.solver_progress_clears == 1


def test_start_meshing_persists_remote_task_metadata(monkeypatch):
    monkeypatch.setitem(REMOTE_CONFIG, "flag_dir", r"D:\flags")
    monkeypatch.setitem(REMOTE_CONFIG, "working_dir", r"D:\work")

    class _SSH:
        def exec_background(
            self,
            command: str,
            flag_file: str,
            *,
            working_dir: str | None,
            interactive: bool,
        ):
            assert flag_file == "D:/flags/meshing_done_5.txt"
            assert working_dir == r"D:\work"
            assert interactive is True
            return True, "AutoFluid_abc123"

    state = _StateRecorder()
    executor = RemoteExecutor(state, lambda: _SSH(), threading.RLock())
    monkeypatch.setattr(executor, "sync_scripts", lambda workstation_id=DEFAULT_WORKSTATION_ID: True)

    assert executor.start_meshing(5) is True
    assert state.remote_tasks[(5, "meshing")] == {
        "workstation_id": DEFAULT_WORKSTATION_ID,
        "config_name": 5,
        "step_name": "meshing",
        "task_name": "AutoFluid_abc123",
        "flag_file": "D:/flags/meshing_done_5.txt",
        "error_flag_file": "D:/flags/meshing_done_5.txt.error",
        "log_file": "D:/flags/autofluid_bg_abc123.log",
        "pid_file": "D:/flags/autofluid_bg_abc123.pid",
        "script_file": "D:/flags/autofluid_bg_abc123.cmd",
        "started_at": state.remote_tasks[(5, "meshing")]["started_at"],
    }


def test_start_meshing_uses_workstation_specific_paths(monkeypatch):
    monkeypatch.setitem(REMOTE_CONFIG, "scripts_dir", r"D:\default scripts")
    monkeypatch.setitem(REMOTE_CONFIG, "scdoc_dir", r"D:\default scdoc")
    monkeypatch.setitem(REMOTE_CONFIG, "msh_dir", r"D:\default msh")
    monkeypatch.setitem(REMOTE_CONFIG, "working_dir", r"D:\default work")
    monkeypatch.setitem(REMOTE_CONFIG, "flag_dir", r"D:\default flags")
    monkeypatch.setattr(
        remote_executor_module,
        "get_workstation_config",
        lambda workstation_id: {
            **REMOTE_CONFIG,
            "id": workstation_id,
            "scripts_dir": r"E:\ws-a scripts",
            "scdoc_dir": r"E:\ws-a scdoc",
            "msh_dir": r"E:\ws-a msh",
            "working_dir": r"E:\ws-a work",
            "flag_dir": r"E:\ws-a flags",
        },
    )

    calls: list[dict[str, str | None]] = []

    class _SSH:
        def exec_background(
            self,
            command: str,
            flag_file: str,
            *,
            working_dir: str | None,
            interactive: bool,
        ):
            calls.append(
                {
                    "command": command,
                    "flag_file": flag_file,
                    "working_dir": working_dir,
                }
            )
            return True, "AutoFluid_ws_a"

    executor = RemoteExecutor(_StateRecorder(), lambda _ws: _SSH(), threading.RLock())
    monkeypatch.setattr(executor, "sync_scripts", lambda workstation_id=DEFAULT_WORKSTATION_ID: True)

    assert executor.start_meshing(5, workstation_id="WS-A") is True

    assert calls[0]["flag_file"] == "E:/ws-a flags/meshing_done_5.txt"
    assert calls[0]["working_dir"] == r"E:\ws-a work"
    assert "E:\\ws-a scripts/batch_meshing_gen4.py" in str(calls[0]["command"])
    assert '"E:\\ws-a scdoc"' in str(calls[0]["command"])
    assert '"E:\\ws-a msh"' in str(calls[0]["command"])


def test_persist_remote_task_keeps_workstation_dimension():
    state = _StateRecorder()
    executor = RemoteExecutor(state, lambda: None, threading.RLock())

    executor._persist_remote_task(
        config_name=5,
        step_name="meshing",
        task_name="AutoFluid_default",
        flag_file="D:/flags/default_done.txt",
    )
    executor._persist_remote_task(
        workstation_id="WS-A",
        config_name=5,
        step_name="meshing",
        task_name="AutoFluid_ws_a",
        flag_file="D:/flags/ws_a_done.txt",
    )

    assert state.remote_tasks[(5, "meshing")]["task_name"] == "AutoFluid_default"
    assert state.remote_tasks[("WS-A", 5, "meshing")]["task_name"] == "AutoFluid_ws_a"
    assert state.remote_tasks[("WS-A", 5, "meshing")]["workstation_id"] == "WS-A"


def test_remote_task_memory_mapping_is_accessed_under_lock():
    executor = RemoteExecutor(_StateRecorder(), lambda: None, threading.RLock())
    guard = _GuardLock()
    executor._remote_tasks_lock = guard
    executor._remote_tasks = _GuardedRemoteTasks(guard)

    executor._remember_remote_task(7, "solver", "AutoFluid_solver", "WS-A")
    assert guard.entries >= 1

    assert executor._pop_remote_task(7, "solver", "WS-A") == "AutoFluid_solver"


def test_restore_remote_tasks_from_db_rebuilds_memory_mapping_under_lock():
    state = _StateRecorder()
    state.remote_tasks[(9, "solver")] = {
        "config_name": 9,
        "step_name": "solver",
        "task_name": "AutoFluid_solver",
        "flag_file": "D:/flags/solver_done_9.txt",
        "error_flag_file": "D:/flags/solver_done_9.txt.error",
        "started_at": 100.0,
    }
    executor = RemoteExecutor(state, lambda: None, threading.RLock())
    guard = _GuardLock()
    executor._remote_tasks_lock = guard
    executor._remote_tasks = _GuardedRemoteTasks(guard)

    executor.restore_remote_tasks_from_db()

    assert executor._remote_tasks == {
        (DEFAULT_WORKSTATION_ID, 9, "solver"): "AutoFluid_solver",
    }
    assert guard.entries >= 1


def test_last_sync_paths_are_loaded_under_lock(tmp_path, monkeypatch):
    monkeypatch.setitem(LOCAL_PATHS, "data_dir", str(tmp_path))
    executor = RemoteExecutor(_StateRecorder(), lambda: None, threading.RLock())
    guard = _GuardLock()
    executor._last_sync_paths_lock = guard

    def _load_last_sync_paths():
        assert guard.held, "last sync state loaded without lock"
        return {}

    executor._load_last_sync_paths = _load_last_sync_paths

    assert executor._last_sync_paths_for_workstation("WS-A") == {}
    executor._save_last_sync_paths(
        {"scripts_dir": "D:/scripts", "ref_files_dir": "D:/refs"},
        "WS-A",
    )
    assert guard.entries >= 2

def test_restore_remote_tasks_from_db_rebuilds_memory_mapping():
    state = _StateRecorder()
    state.remote_tasks[(9, "solver")] = {
        "config_name": 9,
        "step_name": "solver",
        "task_name": "AutoFluid_solver",
        "flag_file": "D:/flags/solver_done_9.txt",
        "error_flag_file": "D:/flags/solver_done_9.txt.error",
        "started_at": 100.0,
    }
    executor = RemoteExecutor(state, lambda: None, threading.RLock())

    executor.restore_remote_tasks_from_db()

    assert executor._remote_tasks == {
        (DEFAULT_WORKSTATION_ID, 9, "solver"): "AutoFluid_solver",
    }


def test_restore_remote_tasks_from_db_preserves_workstation_dimension():
    state = _StateRecorder()
    state.remote_tasks[(9, "solver")] = {
        "workstation_id": DEFAULT_WORKSTATION_ID,
        "config_name": 9,
        "step_name": "solver",
        "task_name": "AutoFluid_default_solver",
        "flag_file": "D:/flags/default_solver_done_9.txt",
        "error_flag_file": "D:/flags/default_solver_done_9.txt.error",
        "started_at": 100.0,
    }
    state.remote_tasks[("WS-A", 9, "solver")] = {
        "workstation_id": "WS-A",
        "config_name": 9,
        "step_name": "solver",
        "task_name": "AutoFluid_ws_a_solver",
        "flag_file": "D:/flags/ws_a_solver_done_9.txt",
        "error_flag_file": "D:/flags/ws_a_solver_done_9.txt.error",
        "started_at": 100.0,
    }
    executor = RemoteExecutor(state, lambda: None, threading.RLock())

    executor.restore_remote_tasks_from_db()

    assert executor._remote_tasks == {
        (DEFAULT_WORKSTATION_ID, 9, "solver"): "AutoFluid_default_solver",
        ("WS-A", 9, "solver"): "AutoFluid_ws_a_solver",
    }


def test_query_remote_task_status_uses_workstation_specific_ssh():
    state = _StateRecorder()
    state.remote_tasks[("WS-A", 2, "meshing")] = {
        "workstation_id": "WS-A",
        "config_name": 2,
        "step_name": "meshing",
        "task_name": "AutoFluid_running",
        "flag_file": "D:/flags/meshing_done_2.txt",
        "error_flag_file": "D:/flags/meshing_done_2.txt.error",
        "started_at": 100.0,
    }
    requested_workstations: list[str] = []

    class _SSH:
        def check_remote_file(self, _remote_path: str) -> bool:
            return False

        def exec_command(self, _command: str, timeout: int = 30):
            return '"AutoFluid_running","Ready"\r\n', "", 0

    def _get_ssh(workstation_id: str):
        requested_workstations.append(workstation_id)
        return _SSH()

    executor = RemoteExecutor(state, _get_ssh, threading.RLock())

    assert executor.query_remote_task_status(2, "meshing", workstation_id="WS-A") == "running"
    assert requested_workstations == ["WS-A"]


def test_query_remote_task_status_returns_running_when_task_exists(monkeypatch):
    state = _StateRecorder()
    state.remote_tasks[(2, "meshing")] = {
        "config_name": 2,
        "step_name": "meshing",
        "task_name": "AutoFluid_running",
        "flag_file": "D:/flags/meshing_done_2.txt",
        "error_flag_file": "D:/flags/meshing_done_2.txt.error",
        "pid_file": "D:/flags/autofluid_bg_running.pid",
        "started_at": 100.0,
    }
    checked: list[str] = []

    class _SSH:
        def check_remote_file(self, remote_path: str) -> bool:
            checked.append(remote_path)
            return False

        def exec_command(self, command: str, timeout: int = 30):
            assert 'schtasks /Query /TN "AutoFluid_running"' in command
            return '"AutoFluid_running","Ready"\r\n', "", 0

    executor = RemoteExecutor(state, lambda: _SSH(), threading.RLock())

    assert executor.query_remote_task_status(2, "meshing") == "running"
    assert checked == [
        "D:/flags/meshing_done_2.txt",
        "D:/flags/meshing_done_2.txt.error",
    ]


def test_query_remote_task_status_uses_pid_file_when_available():
    state = _StateRecorder()
    state.remote_tasks[(2, "meshing")] = {
        "config_name": 2,
        "step_name": "meshing",
        "task_name": "AutoFluid_running",
        "flag_file": "D:/flags/meshing_done_2.txt",
        "error_flag_file": "D:/flags/meshing_done_2.txt.error",
        "pid_file": "D:/flags/autofluid_bg_running.pid",
        "started_at": 100.0,
    }
    commands: list[str] = []

    class _SSH:
        def check_remote_file(self, remote_path: str) -> bool:
            return False

        def read_remote_pid_file(self, pid_file: str) -> int:
            assert pid_file == "D:/flags/autofluid_bg_running.pid"
            return 4321

        def exec_command(self, command: str, timeout: int = 30):
            commands.append(command)
            if command.startswith("schtasks"):
                return '"AutoFluid_running","Ready"\r\n', "", 0
            if command.startswith("tasklist"):
                return '"python.exe","4321","Console","1","10,000 K"\r\n', "", 0
            raise AssertionError(command)

    executor = RemoteExecutor(state, lambda: _SSH(), threading.RLock())

    assert executor.query_remote_task_status(2, "meshing") == "running"
    assert commands == [
        'schtasks /Query /TN "AutoFluid_running" /FO CSV /NH',
        'tasklist /FI "PID eq 4321" /FO CSV /NH',
    ]


def test_query_remote_task_status_returns_lost_when_persisted_pid_is_not_running():
    state = _StateRecorder()
    state.remote_tasks[(2, "solver")] = {
        "config_name": 2,
        "step_name": "solver",
        "task_name": "AutoFluid_stale",
        "flag_file": "D:/flags/solver_done_2.txt",
        "error_flag_file": "D:/flags/solver_done_2.txt.error",
        "pid_file": "D:/flags/autofluid_bg_stale.pid",
        "started_at": 100.0,
    }

    class _SSH:
        def check_remote_file(self, remote_path: str) -> bool:
            return False

        def read_remote_pid_file(self, pid_file: str) -> int:
            return 9876

        def exec_command(self, command: str, timeout: int = 30):
            if command.startswith("schtasks"):
                return '"AutoFluid_stale","Ready"\r\n', "", 0
            if command.startswith("tasklist"):
                return "INFO: No tasks are running which match the specified criteria.\r\n", "", 0
            raise AssertionError(command)

    executor = RemoteExecutor(state, lambda: _SSH(), threading.RLock())

    assert executor.query_remote_task_status(2, "solver") == "lost"


def test_query_remote_task_status_returns_failed_for_error_flag():
    state = _StateRecorder()
    state.remote_tasks[(3, "solver")] = {
        "config_name": 3,
        "step_name": "solver",
        "task_name": "AutoFluid_failed",
        "flag_file": "D:/flags/solver_done_3.txt",
        "error_flag_file": "D:/flags/solver_done_3.txt.error",
        "started_at": 100.0,
    }

    class _SSH:
        def check_remote_file(self, remote_path: str) -> bool:
            return remote_path.endswith(".error")

        def exec_command(self, command: str, timeout: int = 30):
            raise AssertionError("error flag should decide status before schtasks")

    executor = RemoteExecutor(state, lambda: _SSH(), threading.RLock())

    assert executor.query_remote_task_status(3, "solver") == "failed"


def test_query_remote_task_status_returns_unknown_on_remote_check_failure():
    state = _StateRecorder()
    state.remote_tasks[(4, "meshing")] = {
        "config_name": 4,
        "step_name": "meshing",
        "task_name": "AutoFluid_unknown",
        "flag_file": "D:/flags/meshing_done_4.txt",
        "error_flag_file": "D:/flags/meshing_done_4.txt.error",
        "started_at": 100.0,
    }

    class _SSH:
        def check_remote_file(self, remote_path: str) -> bool:
            raise OSError("sftp unavailable")

        def exec_command(self, command: str, timeout: int = 30):
            raise AssertionError("unknown file state should not query schtasks")

    executor = RemoteExecutor(state, lambda: _SSH(), threading.RLock())

    assert executor.query_remote_task_status(4, "meshing") == "unknown"


def test_execute_solver_uses_workstation_specific_paths(monkeypatch):
    monkeypatch.setitem(REMOTE_CONFIG, "scripts_dir", r"D:\default scripts")
    monkeypatch.setitem(REMOTE_CONFIG, "msh_dir", r"D:\default msh")
    monkeypatch.setitem(REMOTE_CONFIG, "result_dir", r"D:\default result")
    monkeypatch.setitem(REMOTE_CONFIG, "working_dir", r"D:\default work")
    monkeypatch.setitem(REMOTE_CONFIG, "flag_dir", r"D:\default flags")
    monkeypatch.setattr(
        remote_executor_module,
        "get_workstation_config",
        lambda workstation_id: {
            **REMOTE_CONFIG,
            "id": workstation_id,
            "scripts_dir": r"E:\ws-a scripts",
            "msh_dir": r"E:\ws-a msh",
            "result_dir": r"E:\ws-a result",
            "animation_dir": r"E:\ws-a animation",
            "postprocess_animation_dir": r"E:\ws-a animation",
            "working_dir": r"E:\ws-a work",
            "flag_dir": r"E:\ws-a flags",
        },
    )

    calls: list[dict[str, str | None]] = []

    class _SSH:
        def exec_background(
            self,
            command: str,
            flag_file: str,
            *,
            working_dir: str | None,
            interactive: bool,
        ):
            calls.append(
                {
                    "command": command,
                    "flag_file": flag_file,
                    "working_dir": working_dir,
                }
            )
            return True, "AutoFluid_solver_ws_a"

    executor = RemoteExecutor(_StateRecorder(), lambda _ws: _SSH(), threading.RLock())
    monkeypatch.setattr(executor, "sync_scripts", lambda workstation_id=DEFAULT_WORKSTATION_ID: True)

    assert executor.execute_solver(6, workstation_id="WS-A") is True

    assert calls[0]["flag_file"] == "E:/ws-a flags/solver_done_6.txt"
    assert calls[0]["working_dir"] == r"E:\ws-a work"
    assert "E:\\ws-a scripts/batch_solver_gen4.py" in str(calls[0]["command"])
    assert '"E:\\ws-a msh"' in str(calls[0]["command"])
    assert '"E:\\ws-a result"' in str(calls[0]["command"])
    assert '--anim-dir "E:\\ws-a animation"' in str(calls[0]["command"])
    assert '"E:\\ws-a work"' in str(calls[0]["command"])
    assert '--progress-file "E:/ws-a flags/solver_progress_6.json"' in str(calls[0]["command"])


def test_execute_postprocess_reuses_workstation_specific_solver_task(monkeypatch):
    monkeypatch.setitem(REMOTE_CONFIG, "flag_dir", r"D:\default flags")
    monkeypatch.setattr(
        remote_executor_module,
        "get_workstation_config",
        lambda workstation_id: {
            **REMOTE_CONFIG,
            "id": workstation_id,
            "flag_dir": r"E:\ws-a flags",
        },
    )

    class _SSH:
        def exec_background(
            self,
            command: str,
            flag_file: str,
            *,
            working_dir: str | None,
            interactive: bool,
        ):
            raise AssertionError("postprocess must not start a second Fluent task")

    state = _StateRecorder()
    executor = RemoteExecutor(state, lambda _ws: _SSH(), threading.RLock())
    executor._remember_remote_task(6, "solver", "AutoFluid_solver_ws_a", workstation_id="WS-A")

    assert executor.execute_postprocess(6, workstation_id="WS-A") is True

    assert state.remote_tasks[("WS-A", 6, "postprocess")]["task_name"] == (
        "AutoFluid_solver_ws_a"
    )
    assert state.remote_tasks[("WS-A", 6, "postprocess")]["flag_file"] == (
        "E:/ws-a flags/postprocess_done_6.txt"
    )


def test_cleanup_solver_runtime_flag_artifacts_deletes_only_completed_wrapper_logs(monkeypatch):
    monkeypatch.setitem(REMOTE_CONFIG, "flag_dir", r"D:\flags")
    deleted: list[str] = []

    class _SSH:
        def list_remote_directory(self, remote_dir: str):
            assert remote_dir == r"D:\flags"
            return [
                "autofluid_bg_done.cmd",
                "autofluid_bg_done.log",
                "autofluid_bg_done.pid",
                "solver_done_1.txt",
                "solver_progress_1.json",
                "other.log",
            ]

        def delete_remote_file(self, remote_path: str) -> bool:
            deleted.append(remote_path)
            return True

    executor = RemoteExecutor(_StateRecorder(), lambda: _SSH(), threading.RLock())

    assert executor.cleanup_solver_runtime_flag_artifacts() == {"deleted": 2, "failed": 0}
    assert deleted == [
        "D:/flags/autofluid_bg_done.cmd",
        "D:/flags/autofluid_bg_done.log",
    ]


def test_wait_solver_completion_reads_progress_and_updates_state(monkeypatch):
    monkeypatch.setitem(REMOTE_CONFIG, "flag_dir", r"D:\flags")
    monkeypatch.setitem(REMOTE_CONFIG, "result_dir", r"D:\result")
    monkeypatch.setitem(ENGINE_CONFIG, "solver_timeout", 60)

    progress_payload = {
        "config_name": 4,
        "current_iter": 350,
        "total_iter": 1000,
        "remaining_sec": 5025.0,
        "updated_at": 1717584000.123,
    }
    checked: list[str] = []
    read_paths: list[str] = []
    times = iter([0.0, 0.0, 30.0, 30.0, 30.0])

    class _SSH:
        def check_remote_file(self, remote_path: str) -> bool:
            checked.append(remote_path)
            return remote_path in {
                "D:/flags/solver_done_4.txt",
                "D:/result/model_gen4_4.cas.h5",
                "D:/result/model_gen4_4.dat.h5",
            }

        def read_remote_text_file(self, remote_path: str, *, timeout: float | None = None):
            read_paths.append(remote_path)
            return json.dumps(progress_payload)

        def delete_remote_file(self, remote_path: str) -> bool:
            return True

        def cleanup_remote_task_entry(self, task_name: str, pid_file: str | None = None) -> bool:
            return True

    state = _StateRecorder()
    executor = RemoteExecutor(state, lambda: _SSH(), threading.RLock())
    executor._remote_tasks[4] = "AutoFluid_solver"
    monkeypatch.setattr(remote_executor_module.time, "time", lambda: next(times))

    assert executor.wait_solver_completion(4) is True
    assert read_paths == ["D:/flags/solver_progress_4.json"]
    assert state.solver_progress_updates == [progress_payload]
    assert state.solver_progress_clears == 1


def test_wait_meshing_completion_uses_persisted_started_at_for_timeout(monkeypatch):
    monkeypatch.setitem(REMOTE_CONFIG, "flag_dir", r"D:\flags")
    monkeypatch.setitem(ENGINE_CONFIG, "meshing_timeout", 60)

    state = _StateRecorder()
    state.remote_tasks[(7, "meshing")] = {
        "config_name": 7,
        "step_name": "meshing",
        "task_name": "AutoFluid_meshing",
        "flag_file": "D:/flags/meshing_done_7.txt",
        "error_flag_file": "D:/flags/meshing_done_7.txt.error",
        "started_at": 1_000.0,
    }
    killed: list[tuple[int, str]] = []

    class _SSH:
        def check_remote_file(self, remote_path: str) -> bool:
            return False

    executor = RemoteExecutor(state, lambda: _SSH(), threading.RLock())
    executor._remote_tasks[7] = "AutoFluid_meshing"
    monkeypatch.setattr(remote_executor_module.time, "time", lambda: 1_061.0)
    monkeypatch.setattr(
        executor,
        "_kill_remote_task_for_config",
        lambda config_name, step_name, workstation_id=DEFAULT_WORKSTATION_ID: killed.append(
            (config_name, step_name)
        ),
    )
    monkeypatch.setattr(
        remote_executor_module.time,
        "sleep",
        lambda _: (_ for _ in ()).throw(
            AssertionError("expired persisted timeout must not sleep")
        ),
    )

    assert executor.wait_meshing_completion(7) is False
    assert killed == [(7, "meshing")]


def test_wait_solver_completion_uses_persisted_started_at_for_timeout(monkeypatch):
    monkeypatch.setitem(REMOTE_CONFIG, "flag_dir", r"D:\flags")
    monkeypatch.setitem(REMOTE_CONFIG, "result_dir", r"D:\result")
    monkeypatch.setitem(ENGINE_CONFIG, "solver_timeout", 60)

    state = _StateRecorder()
    state.remote_tasks[(8, "solver")] = {
        "config_name": 8,
        "step_name": "solver",
        "task_name": "AutoFluid_solver",
        "flag_file": "D:/flags/solver_done_8.txt",
        "error_flag_file": "D:/flags/solver_done_8.txt.error",
        "started_at": 1_000.0,
    }
    killed: list[tuple[int, str]] = []

    class _SSH:
        def check_remote_file(self, remote_path: str) -> bool:
            return False

    executor = RemoteExecutor(state, lambda: _SSH(), threading.RLock())
    executor._remote_tasks[8] = "AutoFluid_solver"
    monkeypatch.setattr(remote_executor_module.time, "time", lambda: 1_061.0)
    monkeypatch.setattr(
        executor,
        "_kill_remote_task_for_config",
        lambda config_name, step_name, workstation_id=DEFAULT_WORKSTATION_ID: killed.append(
            (config_name, step_name)
        ),
    )
    monkeypatch.setattr(
        remote_executor_module.time,
        "sleep",
        lambda _: (_ for _ in ()).throw(
            AssertionError("expired persisted timeout must not sleep")
        ),
    )

    assert executor.wait_solver_completion(8) is False
    assert killed == [(8, "solver")]


def test_apply_placeholders_uses_forward_slashes_for_fluent_templates(monkeypatch):
    monkeypatch.setitem(REMOTE_CONFIG, "scripts_dir", r"D:\xkz_1020\scripts")
    monkeypatch.setitem(REMOTE_CONFIG, "scdoc_dir", r"D:\xkz_1020\scdoc")
    monkeypatch.setitem(REMOTE_CONFIG, "working_dir", r"D:\xkz_1020\working")
    monkeypatch.setitem(REMOTE_CONFIG, "ref_files_dir", r"D:\xkz_1020\ref_files")
    monkeypatch.setitem(REMOTE_CONFIG, "msh_dir", r"D:\xkz_1020\msh")
    monkeypatch.setitem(REMOTE_CONFIG, "result_dir", r"D:\xkz_1020\result")

    executor = RemoteExecutor(_StateRecorder(), lambda: None, threading.RLock())
    rendered = executor._apply_placeholders(
        '{"file": "{{REMOTE_SCDOC_DIR}}/{{SC_FILENAME}}", '
        '"script": "{{REMOTE_ROOT}}/meshing_gen4.wft"}'
    )

    assert "\\" not in rendered
    assert json.loads(rendered) == {
        "file": "D:/xkz_1020/scdoc/model_gen4_{config}.scdoc",
        "script": "D:/xkz_1020/scripts/meshing_gen4.wft",
    }
