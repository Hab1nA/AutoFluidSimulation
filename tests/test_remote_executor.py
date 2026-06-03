from __future__ import annotations

import json
import threading

from engine.config import LOCAL_PATHS, REMOTE_CONFIG, ENGINE_CONFIG, STATUS_ERROR
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


def test_execute_transfer_passes_timeout_and_control_events(tmp_path, monkeypatch):
    scdoc_dir = tmp_path / "scdoc"
    scdoc_dir.mkdir()
    scdoc_file = scdoc_dir / "model_gen4_1.scdoc"
    scdoc_file.write_bytes(b"scdoc")

    monkeypatch.setitem(LOCAL_PATHS, "scdoc_dir", str(scdoc_dir))
    monkeypatch.setitem(REMOTE_CONFIG, "scdoc_dir", r"D:\remote scdoc")
    monkeypatch.setitem(ENGINE_CONFIG, "transfer_timeout", 17)
    monkeypatch.setitem(ENGINE_CONFIG, "ssh_upload_max_retries", 2)

    paused = threading.Event()
    stopped = threading.Event()
    calls: list[dict[str, object]] = []

    class _SSH:
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

    state = _StateRecorder()
    executor = RemoteExecutor(state, lambda: _SSH(), threading.RLock())

    assert executor.execute_transfer(2) is False
    assert deleted == ["D:/remote scdoc/model_gen4_2.scdoc"]
    assert state.status_updates[-1] == (
        2,
        "Transfer",
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
        "Transfer",
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


def test_wait_meshing_completion_returns_false_immediately_on_error_flag(monkeypatch):
    monkeypatch.setitem(REMOTE_CONFIG, "flag_dir", r"D:\flags")
    monkeypatch.setitem(ENGINE_CONFIG, "meshing_timeout", 60)

    error_flag = "D:/flags/meshing_done_3.txt.error"
    checked: list[str] = []
    deleted: list[str] = []

    class _SSH:
        def check_remote_file(self, remote_path: str) -> bool:
            checked.append(remote_path)
            return remote_path == error_flag

        def delete_remote_file(self, remote_path: str) -> bool:
            deleted.append(remote_path)
            return True

    executor = RemoteExecutor(_StateRecorder(), lambda: _SSH(), threading.RLock())
    executor._remote_tasks[3] = "AutoFluid_meshing"
    monkeypatch.setattr(
        "executor.remote_executor.time.sleep",
        lambda _: (_ for _ in ()).throw(
            AssertionError("error flag should stop Meshing polling immediately")
        ),
    )

    assert executor.wait_meshing_completion(3) is False
    assert checked == [error_flag]
    assert deleted == [error_flag]
    assert 3 not in executor._remote_tasks


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

    executor = RemoteExecutor(_StateRecorder(), lambda: _SSH(), threading.RLock())
    executor._remote_tasks[4] = "AutoFluid_solver"
    monkeypatch.setattr(
        "executor.remote_executor.time.sleep",
        lambda _: (_ for _ in ()).throw(
            AssertionError("error flag should stop Solver polling immediately")
        ),
    )

    assert executor.wait_solver_completion(4) is False
    assert checked == [error_flag]
    assert deleted == [error_flag]
    assert 4 not in executor._remote_tasks


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
