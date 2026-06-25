from __future__ import annotations

from pathlib import Path

from utils import process_utils


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "start_workstation_reverse_tunnel.ps1"
SERVER_IPC_SCRIPT_PATH = (
    Path(__file__).resolve().parents[1] / "scripts" / "start_server_ipc_tunnel.ps1"
)


def test_tunnel_watchdog_script_defines_watchdog_contract() -> None:
    source = SCRIPT_PATH.read_text(encoding="utf-8")

    assert "[string]$RemoteBindHost = \"\"" in source
    assert "[int]$RemoteBindPort = 0" in source
    assert "[string]$TargetHost = \"\"" in source
    assert "[int]$TargetPort = 0" in source
    assert "[string]$TunnelTarget = \"\"" in source
    assert "[int]$OwnerPid = 0" in source
    assert "[string]$OwnerMarkerPath = \"\"" in source
    assert "[int]$MaxConsecutiveFailures = 5" in source
    assert "[int]$MaxRecoverySeconds = 120" in source
    assert "[int]$LogRepeatSeconds = 60" in source
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
    assert "New-ScheduledTaskAction -Execute $wscriptExe -Argument" in source



def test_reverse_tunnel_monitor_binds_recovery_to_owner_budget_and_log_limit() -> None:
    source = SCRIPT_PATH.read_text(encoding="utf-8")
    monitor_start = source.index("function Start-ReverseTunnelMonitor")
    monitor_end = source.index("function Get-ExistingTunnelMonitorProcess")
    monitor_source = source[monitor_start:monitor_end]

    assert "function Test-TunnelOwnerAlive" in source
    assert "function Register-TunnelFailure" in source
    assert "function Write-RateLimitedTunnelLog" in source
    assert "function Stop-ReverseTunnelChild" in source
    assert "Test-TunnelOwnerAlive" in monitor_source
    assert "Register-TunnelFailure" in monitor_source
    assert "Write-RateLimitedTunnelLog" in monitor_source
    assert "Stop-ReverseTunnelChild" in monitor_source
    assert "Target endpoint is unreachable" not in monitor_source
    assert 'Register-TunnelFailure -Reason "remote-probe-failed"' in monitor_source


def test_reverse_tunnel_startup_noops_when_owner_is_gone() -> None:
    source = SCRIPT_PATH.read_text(encoding="utf-8")
    owner_guard = (
        'if (-not $Check -and -not $UninstallWatchdog -and '
        '-not (Test-TunnelOwnerAlive))'
    )

    assert owner_guard in source
    assert source.index(owner_guard) < source.index('if ($InstallWatchdog)')
    assert source.index(owner_guard) < source.index("$sshExe = Resolve-SshExe")


def test_reverse_tunnel_default_supervisor_requires_owner_identity() -> None:
    source = SCRIPT_PATH.read_text(encoding="utf-8")
    owner_required_guard = (
        'if (-not $Check -and -not $UninstallWatchdog -and '
        '-not $NoWatchdog -and -not $Monitor -and -not (Test-TunnelOwnerConfigured))'
    )

    assert "function Test-TunnelOwnerConfigured" in source
    assert owner_required_guard in source
    assert source.index(owner_required_guard) < source.index('if ($InstallWatchdog)')
    assert "reverse tunnel monitor owner is required" in source
    assert "return $true" not in source[
        source.index("function Test-TunnelOwnerAlive"):
        source.index("function Stop-ReverseTunnelChild")
    ]


def test_reverse_tunnel_watchdog_launcher_carries_owner_and_budget_arguments() -> None:
    source = SCRIPT_PATH.read_text(encoding="utf-8")
    launcher_start = source.index("function New-TunnelWatchdogLauncher")
    launcher_end = source.index("function Remove-TunnelWatchdogLauncher")
    launcher_source = source[launcher_start:launcher_end]

    assert "-RemoteBindHost" in launcher_source
    assert "-RemoteBindPort" in launcher_source
    assert "-TargetHost" in launcher_source
    assert "-TargetPort" in launcher_source
    assert "-TunnelTarget" in launcher_source
    assert "-OwnerPid" in launcher_source
    assert "-OwnerMarkerPath" in launcher_source
    assert "-MaxConsecutiveFailures" in launcher_source
    assert "-MaxRecoverySeconds" in launcher_source
    assert "-LogRepeatSeconds" in launcher_source


def test_tunnel_owner_marker_takes_precedence_over_stale_pid() -> None:
    source = SCRIPT_PATH.read_text(encoding="utf-8")
    owner_fn = source[
        source.index("function Test-TunnelOwnerAlive"):
        source.index("function Stop-ReverseTunnelChild")
    ]

    marker_check = 'if (-not [string]::IsNullOrWhiteSpace($OwnerMarkerPath))'
    pid_check = "if ($OwnerPid -gt 0)"

    assert owner_fn.index(marker_check) < owner_fn.index(pid_check)
    assert "return Test-Path -LiteralPath $OwnerMarkerPath" in owner_fn


def test_tunnel_watchdog_task_restarts_at_startup_and_logon() -> None:
    source = SCRIPT_PATH.read_text(encoding="utf-8")
    install_start = source.index("function Install-TunnelWatchdogTask")
    install_end = source.index("function Uninstall-TunnelWatchdogTask")
    install_source = source[install_start:install_end]

    assert "New-ScheduledTaskTrigger -AtStartup" in install_source
    assert "New-ScheduledTaskTrigger -AtLogOn" in install_source
    assert "-StartWhenAvailable" in install_source
    assert "-Trigger $triggers" in install_source


def test_server_ipc_tunnel_monitor_binds_recovery_to_owner_budget_and_log_limit() -> None:
    source = SERVER_IPC_SCRIPT_PATH.read_text(encoding="utf-8")
    monitor_start = source.index("function Start-ServerTunnelMonitor")
    monitor_end = source.index("function Start-ServerTunnelMonitorProcess")
    monitor_source = source[monitor_start:monitor_end]

    assert "[int]$OwnerPid = 0" in source
    assert "[int]$MaxConsecutiveFailures = 5" in source
    assert "[int]$MaxRecoverySeconds = 120" in source
    assert "[int]$LogRepeatSeconds = 60" in source
    assert "function Test-TunnelOwnerAlive" in source
    assert "function Register-TunnelFailure" in source
    assert "function Write-RateLimitedTunnelLog" in source
    assert "function Stop-ServerTunnelChild" in source
    assert "Test-TunnelOwnerAlive" in monitor_source
    assert "Register-TunnelFailure" in monitor_source
    assert "Write-RateLimitedTunnelLog" in monitor_source
    assert "Stop-ServerTunnelChild" in monitor_source
    assert "Write-Host \"Server IPC tunnel endpoint dropped; restarting.\"" not in monitor_source


def test_server_ipc_tunnel_default_monitor_requires_owner_pid() -> None:
    source = SERVER_IPC_SCRIPT_PATH.read_text(encoding="utf-8")
    owner_guard_start = source.index("if (-not $Check -and -not $NoMonitor")
    owner_guard_end = source.index("if ($Monitor)", owner_guard_start)
    owner_guard = source[owner_guard_start:owner_guard_end]
    monitor_launcher = source[
        source.index("function Start-ServerTunnelMonitorProcess"):
        source.index("function Stop-ServerTunnelListeningProcess")
    ]

    assert "$OwnerPid -le 0" in owner_guard
    assert "server IPC tunnel monitor owner is required" in owner_guard
    assert '"-Monitor"' in monitor_launcher
    assert '"-NoMonitor"' not in monitor_launcher


def test_server_ipc_tunnel_monitor_writes_supervisor_log_file() -> None:
    source = SERVER_IPC_SCRIPT_PATH.read_text(encoding="utf-8")
    log_fn = source[
        source.index("function Write-RateLimitedTunnelLog"):
        source.index("function Register-TunnelFailure")
    ]
    monitor_source = source[
        source.index("function Start-ServerTunnelMonitor"):
        source.index("function Start-ServerTunnelMonitorProcess")
    ]

    assert "server-ipc.supervisor.log" in source
    assert "Add-Content -LiteralPath $SupervisorLogPath" in log_fn
    assert "Write-Host $Message" not in log_fn
    assert "Write-RateLimitedTunnelLog" in monitor_source


def test_server_ipc_tunnel_monitor_does_not_spend_budget_on_remote_daemon_not_ready() -> None:
    source = SERVER_IPC_SCRIPT_PATH.read_text(encoding="utf-8")
    monitor_start = source.index("function Start-ServerTunnelMonitor")
    monitor_end = source.index("function Start-ServerTunnelMonitorProcess")
    monitor_source = source[monitor_start:monitor_end]

    assert "function Test-ServerTunnelChildHealthy" in source
    assert "function Test-ServerTunnelListener" in source
    assert "remote-daemon-not-ready" in monitor_source
    assert "endpoint-unreachable" not in monitor_source
    assert "Register-TunnelFailure -Reason \"remote-daemon-not-ready\"" not in monitor_source
    assert "Register-TunnelFailure -Reason \"listener-not-ready\"" in monitor_source

def test_tunnel_watchdog_is_installed_by_default_startup() -> None:
    source = SCRIPT_PATH.read_text(encoding="utf-8")

    assert 'if ($TunnelKind -eq "Workstation" -and -not $NoWatchdog -and -not $Check -and -not $Monitor)' in source
    assert "Install-TunnelWatchdogTask -RemotePort $remotePort" in source
    assert "Install-TunnelWatchdogTask" in source
    assert "AutoFluid $tunnelLabel reverse SSH tunnel watchdog task is ready" in source



def test_local_worker_tunnel_does_not_install_watchdog_by_default() -> None:
    source = SCRIPT_PATH.read_text(encoding="utf-8")

    assert 'if ($TunnelKind -eq "Workstation" -and -not $NoWatchdog -and -not $Check -and -not $Monitor)' in source
    assert 'if (-not $NoWatchdog -and -not $Check -and -not $Monitor)' not in source


def test_tunnel_watchdog_uses_task_scheduler_safe_repetition_duration() -> None:
    source = SCRIPT_PATH.read_text(encoding="utf-8")

    assert "[TimeSpan]::MaxValue" not in source
    assert "New-TimeSpan -Days 3650" in source


def test_tunnel_watchdog_task_uses_hidden_wscript_launcher() -> None:
    source = SCRIPT_PATH.read_text(encoding="utf-8")

    assert "function Resolve-WScriptExe" in source
    assert "function New-TunnelWatchdogLauncher" in source
    assert "New-ScheduledTaskAction -Execute $wscriptExe -Argument" in source
    assert "WScript.Shell" in source
    assert "Run command, 0, False" in source
    assert 'Set-Content -LiteralPath $launcherPath' in source


def test_tunnel_watchdog_uninstall_removes_hidden_launcher() -> None:
    source = SCRIPT_PATH.read_text(encoding="utf-8")

    assert "function Remove-TunnelWatchdogLauncher" in source
    assert "Remove-Item -LiteralPath $launcherPath -Force -ErrorAction SilentlyContinue" in source
    assert "Remove-TunnelWatchdogLauncher -RemotePort $remotePort" in source
    assert "Removed AutoFluid $tunnelLabel reverse SSH tunnel watchdog launcher" in source


def test_tunnel_watchdog_has_default_pid_file_for_scheduled_task_runs() -> None:
    source = SCRIPT_PATH.read_text(encoding="utf-8")

    assert "function Get-TunnelPidFile" in source
    assert "tunnel_localworker.pid" in source
    assert "tunnel_workstation.pid" in source
    assert "AUTOFLUID_TUNNEL_PID_FILE" in source


def test_reverse_tunnel_supervisor_quotes_monitor_arguments_with_spaces() -> None:
    source = SCRIPT_PATH.read_text(encoding="utf-8")
    supervisor_start = source.index("function Start-ReverseTunnelSupervisor")
    supervisor_end = source.index("function Write-TunnelSupervisorPid")
    supervisor_source = source[supervisor_start:supervisor_end]

    assert "function ConvertTo-WindowsCommandArgument" in source
    assert '"-File"' in supervisor_source
    assert '"-Monitor"' in supervisor_source
    assert '"-OwnerMarkerPath"' in supervisor_source
    assert "ConvertTo-WindowsCommandArgument -Value $_" in supervisor_source
    assert "-EncodedCommand" not in supervisor_source

def test_tunnel_watchdog_rebuilds_monitor_when_endpoint_is_reachable() -> None:
    source = SCRIPT_PATH.read_text(encoding="utf-8")

    assert "reverse SSH tunnel endpoint is reachable but no supervisor monitor was found" in source
    assert "Start-ReverseTunnelSupervisor -RemotePort $remotePort" in source


def test_reverse_tunnel_monitor_lookup_matches_quoted_file_arguments() -> None:
    source = SCRIPT_PATH.read_text(encoding="utf-8")
    lookup_start = source.index("function Get-ExistingTunnelMonitorProcess")
    lookup_end = source.index("function Stop-ExistingTunnelMonitorProcess")
    lookup_source = source[lookup_start:lookup_end]

    assert '"? -Monitor' not in lookup_source
    assert '"?-Monitor"?' in lookup_source
    assert '"?-TunnelKind"?' in lookup_source
    assert '"?-MonitorRemotePort"?' in lookup_source


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


def test_tunnel_watchdog_process_enumeration_tolerates_cim_access_denied() -> None:
    source = SCRIPT_PATH.read_text(encoding="utf-8")

    helper_start = source.index("function Get-TunnelWin32Processes")
    helper_end = source.index("function Start-ReverseTunnelMonitor")
    helper_source = source[helper_start:helper_end]

    assert "-ErrorAction Stop" in helper_source
    assert "catch" in helper_source
    assert "Get-CimInstance Win32_Process |" not in source
    assert "Get-TunnelWin32Processes |" in source

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
