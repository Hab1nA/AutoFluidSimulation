from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_module():
    repo_root = Path(__file__).resolve().parents[1]
    module_path = repo_root / "scripts" / "ws_c_ssh_diagnostics.py"
    spec = importlib.util.spec_from_file_location("ws_c_ssh_diagnostics", module_path)
    if spec is None or spec.loader is None:
        raise AssertionError("failed to load diagnostic module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_should_write_snapshot_on_change_or_heartbeat() -> None:
    module = _load_module()

    state = {"last_signature": "ok|ready", "last_log_at": "1970-01-01T00:00:00Z"}

    assert module.should_write_snapshot("failed|timeout", state, now=600, heartbeat_seconds=300)
    assert module.should_write_snapshot("ok|ready", state, now=301, heartbeat_seconds=300)
    assert not module.should_write_snapshot("ok|ready", state, now=299, heartbeat_seconds=300)


def test_diagnostic_source_bounds_logs_and_subprocesses() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    source = (repo_root / "scripts" / "ws_c_ssh_diagnostics.py").read_text(encoding="utf-8")

    assert "MAX_LOG_BYTES = 1_048_576" in source
    assert "MAX_LOG_FILES = 5" in source
    assert "HEARTBEAT_SECONDS = 300" in source
    assert "rotate_log" in source
    assert "subprocess.run" in source
    assert "timeout=" in source
    assert "while True" not in source
    assert "Get-CimInstance" not in source
    assert "Win32_Process" not in source


def test_signature_ignores_healthy_monitor_loop_ready_jitter() -> None:
    module = _load_module()
    base = {
        "status": "ok",
        "local_target": {"status": "open"},
        "remote_banner": {"status": "ready"},
        "ssh_process": {"running": True},
        "monitor_status": {"state": "ready", "last_reason": ""},
    }
    jitter = {
        **base,
        "monitor_status": {"state": "loop", "last_reason": ""},
    }

    assert module.signature(base) == module.signature(jitter)
