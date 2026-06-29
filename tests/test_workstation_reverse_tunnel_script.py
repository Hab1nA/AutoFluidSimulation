from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


def test_remote_tunnel_probe_returns_false_when_ssh_writes_stderr(
    tmp_path: Path,
) -> None:
    powershell = shutil.which("powershell.exe") or shutil.which("powershell")
    if powershell is None:
        msg = "Windows PowerShell is required for this regression test"
        raise AssertionError(msg)

    repo_root = Path(__file__).resolve().parents[1]
    script_path = repo_root / "scripts" / "start_workstation_reverse_tunnel.ps1"
    fake_ssh = tmp_path / "fake-ssh.cmd"
    fake_ssh.write_text(
        "@echo off\n"
        "echo simulated remote python traceback 1>&2\n"
        "exit /b 1\n",
        encoding="utf-8",
    )

    probe_script = tmp_path / "probe.ps1"
    probe_script.write_text(
        rf"""
$ErrorActionPreference = "Stop"
$source = Get-Content -LiteralPath "{script_path}" -Raw
$match = [regex]::Match(
    $source,
    '(?s)function Test-RemoteTunnelEndpoint \{{.*?\r?\n\}}\r?\n\r?\nfunction Get-TunnelLogPaths'
)
if (-not $match.Success) {{
    throw "Could not extract Test-RemoteTunnelEndpoint"
}}
$functionSource = $match.Value -replace '\r?\nfunction Get-TunnelLogPaths\z', ''
Invoke-Expression $functionSource
$result = Test-RemoteTunnelEndpoint `
    -SshExe "{fake_ssh}" `
    -TunnelTarget "ocar" `
    -RemoteHost "127.0.0.1" `
    -RemotePort 2222
if ($result -ne $false) {{
    throw "Expected probe to return false, got $result"
}}
Write-Output "probe returned false"
""",
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            powershell,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(probe_script),
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "probe returned false" in result.stdout


def test_tunnel_supervisor_uses_short_keepalive_and_probe_interval() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    script_path = repo_root / "scripts" / "start_workstation_reverse_tunnel.ps1"
    source = script_path.read_text(encoding="utf-8")

    assert '"ServerAliveInterval=5"' in source
    assert '"ServerAliveCountMax=3"' in source
    assert "Start-Sleep -Seconds 30" not in source
    assert "Start-Sleep -Seconds 5" in source
    assert "Start-Process -FilePath $SshExe" in source
    assert "while (-not $sshProcess.HasExited)" not in source
    assert 'Register-TunnelFailure -Reason "ssh-exited-$exitCode"' in source
    assert 'Register-TunnelFailure -Reason "remote-probe-failed"' in source


def test_tunnel_script_persists_supervisor_pid_for_cleanup() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    script_path = repo_root / "scripts" / "start_workstation_reverse_tunnel.ps1"
    source = script_path.read_text(encoding="utf-8")

    assert "AUTOFLUID_TUNNEL_PID_FILE" in source
    assert "Set-Content -LiteralPath $pidFile" in source
    assert '$Process.PSObject.Properties.Name -contains "ProcessId"' in source
    assert "$processId" in source


def test_tunnel_script_prefers_structured_log_dir_with_temp_fallback() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    script_path = repo_root / "scripts" / "start_workstation_reverse_tunnel.ps1"
    source = script_path.read_text(encoding="utf-8")

    assert "logs/local/tunnels/$tunnelName" in source
    assert "[System.IO.Path]::GetTempPath()" in source


def test_workstation_owned_tunnel_script_is_independent_of_local_owner() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    script_path = repo_root / "scripts" / "start_workstation_owned_reverse_tunnel.ps1"
    source = script_path.read_text(encoding="utf-8")

    assert "AutoFluidWorkstationTunnel-" in source
    assert "$TargetHost = \"127.0.0.1\"" in source
    assert "$TargetPort = 22" in source
    assert 'Test-RemoteTunnelEndpoint' in source
    assert 'Stop-OwnedTunnelProcesses' in source
    assert "System.Threading.Mutex" in source
    assert "OwnerMarkerPath" not in source
    assert "OwnerPid" not in source
    assert "5242880" in source
    assert 'Move-Item -LiteralPath $logs.Supervisor' in source


def test_workstation_owned_tunnel_script_reconnects_from_workstation_side() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    script_path = repo_root / "scripts" / "start_workstation_owned_reverse_tunnel.ps1"
    source = script_path.read_text(encoding="utf-8")

    assert '"ExitOnForwardFailure=yes"' in source
    assert '"ServerAliveInterval=5"' in source
    assert '"ServerAliveCountMax=3"' in source
    assert '"StrictHostKeyChecking=accept-new"' in source
    assert '"-i", $TunnelIdentityFile' in source
    assert '$forwardSpec = "${RemoteBindHost}:${RemoteBindPort}:${TargetHost}:${TargetPort}"' in source
    fn_start = source.index("function Get-ReverseTunnelArguments")
    fn_end = source.index("function Start-OwnedTunnelMonitor")
    body = source[fn_start:fn_end]
    assert body.index('"-i", $TunnelIdentityFile') < body.rindex("return $args")
    assert 'Register-TunnelFailure -Reason "ssh-exited-$exitCode"' in source
    assert 'Register-TunnelFailure -Reason "remote-probe-failed"' in source
    assert 'Register-TunnelFailure -Reason "local-target-unreachable"' in source
    assert "function Wait-RemoteTunnelEndpoint" in source
    assert 'Register-TunnelFailure -Reason $startupStatus' in source
    assert "startup-probe-timeout" in source


def test_workstation_owned_remote_probe_passes_single_remote_command(
    tmp_path: Path,
) -> None:
    powershell = shutil.which("powershell.exe") or shutil.which("powershell")
    if powershell is None:
        msg = "Windows PowerShell is required for this regression test"
        raise AssertionError(msg)

    repo_root = Path(__file__).resolve().parents[1]
    script_path = repo_root / "scripts" / "start_workstation_owned_reverse_tunnel.ps1"
    args_file = tmp_path / "ssh-args.txt"
    fake_ssh = tmp_path / "fake-ssh.cmd"
    fake_ssh.write_text(
        "@echo off\n"
        f"echo %* > \"{args_file}\"\n"
        "exit /b 0\n",
        encoding="utf-8",
    )

    probe_script = tmp_path / "owned-probe.ps1"
    probe_script.write_text(
        rf"""
$ErrorActionPreference = "Stop"
$source = Get-Content -LiteralPath "{script_path}" -Raw
$match = [regex]::Match(
    $source,
    '(?s)function Test-RemoteTunnelEndpoint \{{.*?\r?\n\}}\r?\n\r?\nfunction Clear-StaleRemoteForward.*?\r?\n\}}\r?\n\r?\nfunction Wait-RemoteTunnelEndpoint.*?\r?\n\}}\r?\n\r?\nfunction Get-OwnedTunnelProcesses'
)
if (-not $match.Success) {{
    throw "Could not extract Test-RemoteTunnelEndpoint"
}}
$functionSource = $match.Value -replace '\r?\nfunction Get-OwnedTunnelProcesses\z', ''
Invoke-Expression $functionSource
$script:TunnelTarget = "root@example.invalid"
$script:RemoteBindHost = "127.0.0.1"
$script:RemoteBindPort = 2222
$script:TunnelIdentityFile = "C:\Users\example\.ssh\autofluid_tunnel_ed25519"
$result = Test-RemoteTunnelEndpoint -SshExe "{fake_ssh}"
if ($result -ne $true) {{
    throw "Expected probe to return true, got $result"
}}
""",
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            powershell,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(probe_script),
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    captured = args_file.read_text(encoding="utf-8")
    assert "-i" in captured
    assert "autofluid_tunnel_ed25519" in captured
    assert "root@example.invalid" in captured
    assert "python3 -c 'import socket,sys;" in captured
    assert "s.connect((bytes([49,50,55,46,48,46,48,46,49]).decode(),2222))" in captured
    assert "s.recv(4)" in captured
    assert "bytes([83,83,72,45])" in captured


def test_workstation_owned_wait_probe_reports_ready_timeout_and_exit(
    tmp_path: Path,
) -> None:
    powershell = shutil.which("powershell.exe") or shutil.which("powershell")
    if powershell is None:
        msg = "Windows PowerShell is required for this regression test"
        raise AssertionError(msg)

    repo_root = Path(__file__).resolve().parents[1]
    script_path = repo_root / "scripts" / "start_workstation_owned_reverse_tunnel.ps1"
    success_ssh = tmp_path / "success-ssh.cmd"
    success_ssh.write_text("@echo off\nexit /b 0\n", encoding="utf-8")
    fail_ssh = tmp_path / "fail-ssh.cmd"
    fail_ssh.write_text("@echo off\nexit /b 1\n", encoding="utf-8")

    probe_script = tmp_path / "owned-wait-probe.ps1"
    probe_script.write_text(
        rf"""
$ErrorActionPreference = "Stop"
$source = Get-Content -LiteralPath "{script_path}" -Raw
$match = [regex]::Match(
    $source,
    '(?s)function Test-RemoteTunnelEndpoint \{{.*?\r?\n\}}\r?\n\r?\nfunction Clear-StaleRemoteForward.*?\r?\n\}}\r?\n\r?\nfunction Wait-RemoteTunnelEndpoint.*?\r?\n\}}\r?\n\r?\nfunction Get-OwnedTunnelProcesses'
)
if (-not $match.Success) {{
    throw "Could not extract probe functions"
}}
$functionSource = $match.Value -replace '\r?\nfunction Get-OwnedTunnelProcesses\z', ''
Invoke-Expression $functionSource
$script:TunnelTarget = "root@example.invalid"
$script:RemoteBindHost = "127.0.0.1"
$script:RemoteBindPort = 2222
$script:TunnelIdentityFile = ""
$script:ProbeIntervalSeconds = 1

$ready = Wait-RemoteTunnelEndpoint `
    -SshExe "{success_ssh}" `
    -SshProcess $null `
    -TimeoutSeconds 2
if ($ready -ne "ready") {{
    throw "Expected ready, got $ready"
}}

$timeout = Wait-RemoteTunnelEndpoint `
    -SshExe "{fail_ssh}" `
    -SshProcess $null `
    -TimeoutSeconds 1
if ($timeout -ne "startup-probe-timeout") {{
    throw "Expected startup-probe-timeout, got $timeout"
}}

$dead = Start-Process -FilePath "cmd.exe" `
    -ArgumentList "/d", "/c", "exit /b 7" `
    -WindowStyle Hidden `
    -PassThru
Wait-Process -Id $dead.Id
$exited = Wait-RemoteTunnelEndpoint `
    -SshExe "{fail_ssh}" `
    -SshProcess $dead `
    -TimeoutSeconds 2
if ($exited -ne "ssh-exited-7") {{
    throw "Expected ssh-exited-7, got $exited"
}}
Write-Output "wait probe paths passed"
""",
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            powershell,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(probe_script),
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "wait probe paths passed" in result.stdout


def test_workstation_owned_tunnel_uninstall_stops_processes_before_and_after_task_removal() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    script_path = repo_root / "scripts" / "start_workstation_owned_reverse_tunnel.ps1"
    source = script_path.read_text(encoding="utf-8")
    fn_start = source.index("function Uninstall-OwnedTunnelTask")
    fn_end = source.index("function Get-OwnedTunnelStatus")
    body = source[fn_start:fn_end]

    assert body.count("Stop-OwnedTunnelProcesses") == 2
    assert body.index("Disable-ScheduledTask") < body.index("Unregister-ScheduledTask")
    assert body.index("Unregister-ScheduledTask") < body.index("Stop-OwnedTunnelProcesses")
    assert "Start-Sleep -Seconds 1" in body


def test_workstation_owned_tunnel_task_uses_bounded_scheduler_restart_count() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    script_path = repo_root / "scripts" / "start_workstation_owned_reverse_tunnel.ps1"
    source = script_path.read_text(encoding="utf-8")

    assert "-RestartCount 10" in source
    assert "-RestartCount 999" not in source


def test_workstation_owned_tunnel_task_does_not_double_quote_file_argument() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    script_path = repo_root / "scripts" / "start_workstation_owned_reverse_tunnel.ps1"
    source = script_path.read_text(encoding="utf-8")

    assert '"-File", $scriptPath' in source
    assert '"-File", "`"$scriptPath`""' not in source


def test_workstation_owned_tunnel_install_stops_existing_instance_before_registering() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    script_path = repo_root / "scripts" / "start_workstation_owned_reverse_tunnel.ps1"
    source = script_path.read_text(encoding="utf-8")
    fn_start = source.index("function Install-OwnedTunnelTask")
    fn_end = source.index("function Uninstall-OwnedTunnelTask")
    body = source[fn_start:fn_end]

    assert body.count("Stop-OwnedTunnelProcesses") == 2
    assert body.index("Stop-ScheduledTask") < body.index("Register-ScheduledTask")
    assert body.index("Stop-OwnedTunnelProcesses") < body.index("Register-ScheduledTask")
    assert body.index("Start-Sleep -Seconds 1") < body.index("Register-ScheduledTask")


def test_workstation_owned_tunnel_install_falls_back_to_non_elevated_task_registration() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    script_path = repo_root / "scripts" / "start_workstation_owned_reverse_tunnel.ps1"
    source = script_path.read_text(encoding="utf-8")
    fn_start = source.index("function Install-OwnedTunnelTask")
    fn_end = source.index("function Uninstall-OwnedTunnelTask")
    body = source[fn_start:fn_end]

    assert "-RunLevel Highest" in body
    assert "Register with RunLevel Highest failed; retrying as current user task" in body
    assert "Register current user task failed; installing HKCU Run fallback" in body
    assert "Set-ItemProperty -Path $runKey -Name $taskName" in body
    assert "Start-Process -FilePath $powerShellExe" in body
    assert body.count("Register-ScheduledTask") == 2
    fallback_start = body.index("catch {")
    fallback_body = body[fallback_start:]
    assert "-RunLevel Highest" not in fallback_body


def test_workstation_owned_tunnel_ssh_cleanup_matches_forwarded_port_not_target_alias() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    script_path = repo_root / "scripts" / "start_workstation_owned_reverse_tunnel.ps1"
    source = script_path.read_text(encoding="utf-8")
    fn_start = source.index("function Get-OwnedTunnelProcesses")
    fn_end = source.index("function Stop-OwnedTunnelProcesses")
    body = source[fn_start:fn_end]
    ssh_branch = body[body.index('if ($Kind -eq "Ssh")') : body.index('return $commandLine -match $taskPattern')]

    assert "$commandLine -match $portPattern" in ssh_branch
    assert "[regex]::Escape($TunnelTarget)" not in ssh_branch


def test_workstation_owned_tunnel_uninstall_removes_hkcu_run_fallback() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    script_path = repo_root / "scripts" / "start_workstation_owned_reverse_tunnel.ps1"
    source = script_path.read_text(encoding="utf-8")
    fn_start = source.index("function Uninstall-OwnedTunnelTask")
    fn_end = source.index("function Get-OwnedTunnelStatus")
    body = source[fn_start:fn_end]

    assert "Remove-ItemProperty -Path (Get-OwnedTunnelRunKeyPath) -Name $taskName" in body


def test_workstation_owned_tunnel_status_reports_hkcu_run_fallback() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    script_path = repo_root / "scripts" / "start_workstation_owned_reverse_tunnel.ps1"
    source = script_path.read_text(encoding="utf-8")
    fn_start = source.index("function Get-OwnedTunnelStatus")
    fn_end = source.index("if ($Uninstall)")
    body = source[fn_start:fn_end]

    assert "registry_run_exists" in body
    assert "Get-OwnedTunnelRunKeyPath" in body
