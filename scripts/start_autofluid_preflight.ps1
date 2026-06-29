param(
    [switch]$Check,
    [switch]$StartDaemon
)

$ErrorActionPreference = "Stop"

$ProjectDir = Split-Path -Parent $PSScriptRoot
$PythonExe = Join-Path $ProjectDir ".venv\Scripts\python.exe"
$EnvScript = Join-Path $PSScriptRoot "autofluid_env.ps1"
$TunnelScript = Join-Path $PSScriptRoot "start_server_ipc_tunnel.ps1"

. $EnvScript
Import-AutoFluidEnv -ProjectDir $ProjectDir
Assert-AutoFluidServerEndpoint

function Resolve-AutoFluidSshExe {
    if (-not [string]::IsNullOrWhiteSpace($env:AUTOFLUID_SSH_EXE)) {
        return $env:AUTOFLUID_SSH_EXE
    }
    $command = Get-Command ssh.exe -ErrorAction SilentlyContinue
    if ($null -eq $command) {
        throw "ssh.exe was not found in PATH."
    }
    return $command.Source
}

function Get-AutoFluidServerDaemonTarget {
    foreach ($candidate in @(
        $env:AUTOFLUID_SERVER_DAEMON_SSH_TARGET,
        $env:AUTOFLUID_SERVER_TUNNEL_HOST,
        $env:AUTOFLUID_SERVER_HOST,
        $env:AUTOFLUID_IPC_HOST
    )) {
        if (-not [string]::IsNullOrWhiteSpace($candidate)) {
            return $candidate
        }
    }
    return "ocar"
}

function Quote-RemoteShellArg {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Value
    )
    return "'" + $Value.Replace("'", "'\''") + "'"
}

function Get-AutoFluidServerDaemonIpcPort {
    foreach ($candidate in @(
        $env:AUTOFLUID_SERVER_DAEMON_IPC_PORT,
        $env:AUTOFLUID_SERVER_TUNNEL_REMOTE_PORT
    )) {
        if ([string]::IsNullOrWhiteSpace($candidate)) {
            continue
        }
        $port = 0
        if (-not [int]::TryParse($candidate, [ref]$port)) {
            throw "AutoFluid server daemon IPC port is not a valid integer: $candidate"
        }
        if ($port -le 0 -or $port -gt 65535) {
            throw "AutoFluid server daemon IPC port is out of range: $port"
        }
        return $port
    }
    return 9527
}

function Get-AutoFluidServerDaemonStartCommand {
    if (-not [string]::IsNullOrWhiteSpace($env:AUTOFLUID_SERVER_DAEMON_START_CMD)) {
        return $env:AUTOFLUID_SERVER_DAEMON_START_CMD
    }
    $serverPort = Get-AutoFluidServerDaemonIpcPort
    $probeCode = "import json,socket; s=socket.create_connection((`"127.0.0.1`", $serverPort), 1); s.settimeout(2); s.sendall((json.dumps(dict(command=`"get_engine_status`", params=dict(), request_id=`"daemon-start-probe`"))+chr(10)).encode()); data=s.recv(4096); s.close(); resp=json.loads(data.decode().strip()); raise SystemExit(0 if resp.get(`"status`") == `"ok`" else 1)"
    $probeArg = Quote-RemoteShellArg -Value $probeCode
    $serverProjectDir = $env:AUTOFLUID_SERVER_DAEMON_PROJECT_DIR
    if ([string]::IsNullOrWhiteSpace($serverProjectDir)) {
        $serverProjectDir = $env:AUTOFLUID_SERVER_PROJECT_DIR
    }
    if ([string]::IsNullOrWhiteSpace($serverProjectDir)) {
        $serverProjectDir = '$HOME/AutoFluidSimulation'
    }
    else {
        $serverProjectDir = Quote-RemoteShellArg -Value $serverProjectDir
    }
    return "cd $serverProjectDir && mkdir -p logs/server/services/daemon-bootstrap && started_pid=; if ! .venv/bin/python -c $probeArg >/dev/null 2>&1; then env AUTOFLUID_SERVER_MODE=server nohup .venv/bin/python start_daemon.py > logs/server/services/daemon-bootstrap/autofluid-daemon.out 2>&1 < /dev/null & started_pid=`$!; fi; ready_count=0; for i in `$(seq 1 60); do daemon_pid=`$(cat data/daemon.pid 2>/dev/null || true); if .venv/bin/python -c $probeArg >/dev/null 2>&1; then ready_count=`$((ready_count + 1)); if [ `"`$ready_count`" -ge 3 ]; then if [ -z `"`$daemon_pid`" ]; then daemon_pid=`"`$started_pid`"; fi; if [ -z `"`$daemon_pid`" ]; then daemon_pid=unknown; fi; echo `"AutoFluid daemon IPC ready pid=`$daemon_pid`"; exit 0; fi; else ready_count=0; if [ -n `"`$daemon_pid`" ] && kill -0 `"`$daemon_pid`" 2>/dev/null; then sleep 1; continue; fi; if [ -n `"`$started_pid`" ] && kill -0 `"`$started_pid`" 2>/dev/null; then sleep 1; continue; fi; if [ -n `"`$started_pid`" ] || [ -n `"`$daemon_pid`" ]; then echo 'AutoFluid daemon exited before IPC became ready' >&2; tail -n 80 logs/server/services/daemon-bootstrap/autofluid-daemon.out >&2 2>/dev/null || true; exit 1; fi; fi; sleep 1; done; echo 'AutoFluid daemon IPC readiness timeout' >&2; tail -n 80 logs/server/services/daemon-bootstrap/autofluid-daemon.out >&2 2>/dev/null || true; exit 1"
}

function Start-AutoFluidServerDaemon {
    $sshExe = Resolve-AutoFluidSshExe
    $target = Get-AutoFluidServerDaemonTarget
    $remoteCommand = Get-AutoFluidServerDaemonStartCommand
    Write-Host "Starting AutoFluid server daemon on ${target}..."
    $remoteCommand | & $sshExe -o BatchMode=yes -o ConnectTimeout=10 $target bash -s
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to start AutoFluid server daemon on '$target'."
    }
}

if (-not (Test-Path -LiteralPath $PythonExe -PathType Leaf)) {
    throw "Project virtual environment Python was not found: $PythonExe"
}

& $PythonExe --version
& $TunnelScript -Check
Write-Host "Worker-owned reverse tunnels are checked during worker start."
Write-AutoFluidEndpointSummary

if ($Check) {
    Write-Host "AutoFluid startup preflight check passed."
    exit 0
}

if ($StartDaemon) {
    Start-AutoFluidServerDaemon
    Write-Host "AutoFluid startup preflight passed."
    exit 0
}

Write-Host "AutoFluid startup preflight passed."
