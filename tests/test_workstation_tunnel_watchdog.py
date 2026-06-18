from __future__ import annotations

from pathlib import Path

from utils import process_utils


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "start_workstation_reverse_tunnel.ps1"


def test_tunnel_watchdog_script_defines_watchdog_contract() -> None:
    source = SCRIPT_PATH.read_text(encoding="utf-8")

    assert "[switch]$InstallWatchdog" in source
    assert "[switch]$UninstallWatchdog" in source
    assert "[switch]$NoWatchdog" in source
    assert "function Get-TunnelWatchdogTaskName" in source
    assert "AutoFluidTunnelWatchdog-$TunnelKind-$RemotePort" in source
    assert "function Install-TunnelWatchdogTask" in source
    assert "function Uninstall-TunnelWatchdogTask" in source
    assert "Register-ScheduledTask" in source
    assert "Unregister-ScheduledTask" in source
    assert "-NoWatchdog" in source


def test_tunnel_watchdog_uses_task_scheduler_safe_repetition_duration() -> None:
    source = SCRIPT_PATH.read_text(encoding="utf-8")

    assert "[TimeSpan]::MaxValue" not in source
    assert "New-TimeSpan -Days 3650" in source


def test_tunnel_watchdog_has_default_pid_file_for_scheduled_task_runs() -> None:
    source = SCRIPT_PATH.read_text(encoding="utf-8")

    assert "function Get-TunnelPidFile" in source
    assert "tunnel_localworker.pid" in source
    assert "tunnel_workstation.pid" in source
    assert "AUTOFLUID_TUNNEL_PID_FILE" in source


def test_tunnel_watchdog_rebuilds_monitor_when_endpoint_is_reachable() -> None:
    source = SCRIPT_PATH.read_text(encoding="utf-8")

    assert "reverse SSH tunnel endpoint is reachable but no supervisor monitor was found" in source
    assert "Start-ReverseTunnelSupervisor -RemotePort $remotePort" in source


def test_tunnel_watchdog_replaces_monitor_when_endpoint_is_down() -> None:
    source = SCRIPT_PATH.read_text(encoding="utf-8")

    assert "Stop-ExistingTunnelMonitorProcess -RemotePort $remotePort" in source
    assert "Existing $tunnelLabel supervisor monitor was stopped because the endpoint is not reachable." in source


def test_tunnel_watchdog_uninstall_stops_orphan_reverse_ssh_processes() -> None:
    source = SCRIPT_PATH.read_text(encoding="utf-8")

    assert "function Stop-ReverseTunnelSshProcesses" in source
    assert "ssh.exe" in source
    assert '$remoteForwardPattern = "(^|\\s)-R\\s+\\S+:${RemotePort}:"' in source
    assert "Stopped AutoFluid $tunnelLabel reverse SSH tunnel monitor" in source


def test_cleanup_tunnel_watchdog_tasks_invokes_uninstall_for_both_tunnel_kinds(
    monkeypatch,
    tmp_path,
) -> None:
    script = tmp_path / "scripts" / "start_workstation_reverse_tunnel.ps1"
    script.parent.mkdir()
    script.write_text("", encoding="utf-8")
    calls: list[list[str]] = []

    monkeypatch.setattr(process_utils.sys, "platform", "win32")
    monkeypatch.setattr(process_utils.shutil, "which", lambda name: f"C:/Windows/System32/{name}")

    def fake_run(args, **kwargs):
        calls.append(list(args))

        class _Result:
            returncode = 0

        return _Result()

    monkeypatch.setattr(process_utils.subprocess, "run", fake_run)

    result = process_utils.cleanup_tunnel_watchdog_tasks(str(tmp_path))

    assert result == {
        "Workstation": {"status": "uninstalled"},
        "LocalWorker": {"status": "uninstalled"},
    }
    assert len(calls) == 2
    assert [call[-2:] for call in calls] == [
        ["Workstation", "-UninstallWatchdog"],
        ["LocalWorker", "-UninstallWatchdog"],
    ]
    assert all("-NoProfile" in call for call in calls)
    assert all(str(script) in call for call in calls)


def test_cleanup_tunnel_watchdog_tasks_skips_non_windows(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(process_utils.sys, "platform", "linux")

    assert process_utils.cleanup_tunnel_watchdog_tasks(str(tmp_path)) == {
        "status": "skipped",
        "reason": "non_windows",
    }
