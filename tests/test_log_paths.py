from __future__ import annotations

import os


def test_local_session_log_dir_uses_machine_scope(tmp_path, monkeypatch):
    from engine.config import LOCAL_PATHS

    monkeypatch.setitem(LOCAL_PATHS, "log_dir", str(tmp_path / "logs"))
    monkeypatch.delenv("AUTOFLUID_SERVER_MODE", raising=False)

    from utils.log_paths import session_log_dir

    assert session_log_dir("daemon", "2026-06-18_12-30-00") == os.path.join(
        str(tmp_path / "logs"),
        "local",
        "sessions",
        "daemon",
        "2026-06-18_12-30-00",
    )


def test_server_session_log_dir_uses_server_scope(tmp_path, monkeypatch):
    from engine.config import LOCAL_PATHS

    monkeypatch.setitem(LOCAL_PATHS, "log_dir", str(tmp_path / "logs"))
    monkeypatch.setenv("AUTOFLUID_SERVER_MODE", "server")

    from utils.log_paths import session_log_dir

    assert session_log_dir("daemon", "2026-06-18_12-30-00") == os.path.join(
        str(tmp_path / "logs"),
        "server",
        "sessions",
        "daemon",
        "2026-06-18_12-30-00",
    )


def test_service_tunnel_and_export_dirs_are_scoped(tmp_path, monkeypatch):
    from engine.config import LOCAL_PATHS

    monkeypatch.setitem(LOCAL_PATHS, "log_dir", str(tmp_path / "logs"))
    monkeypatch.delenv("AUTOFLUID_SERVER_MODE", raising=False)

    from utils.log_paths import export_log_dir, service_log_file, tunnel_log_dir

    assert service_log_file("alert-watcher", "alert_watcher.log") == os.path.join(
        str(tmp_path / "logs"),
        "local",
        "services",
        "alert-watcher",
        "alert_watcher.log",
    )
    assert tunnel_log_dir("server-ipc") == os.path.join(
        str(tmp_path / "logs"),
        "local",
        "tunnels",
        "server-ipc",
    )
    assert export_log_dir() == os.path.join(
        str(tmp_path / "logs"),
        "local",
        "exports",
    )
