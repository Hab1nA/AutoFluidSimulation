param(
    [switch]$Check
)

$ErrorActionPreference = "Stop"

$ProjectDir = Split-Path -Parent $PSScriptRoot
$PythonExe = Join-Path $ProjectDir ".venv\Scripts\python.exe"
$EnvScript = Join-Path $PSScriptRoot "autofluid_env.ps1"
$TunnelScript = Join-Path $PSScriptRoot "start_server_ipc_tunnel.ps1"
$WorkstationTunnelScript = Join-Path $PSScriptRoot "start_workstation_reverse_tunnel.ps1"

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

function Get-AutoFluidServerDaemonStartCommand {
    if (-not [string]::IsNullOrWhiteSpace($env:AUTOFLUID_SERVER_DAEMON_START_CMD)) {
        return $env:AUTOFLUID_SERVER_DAEMON_START_CMD
    }
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
    return "cd $serverProjectDir && mkdir -p logs && env AUTOFLUID_SERVER_MODE=server setsid -f .venv/bin/python start_daemon.py > logs/autofluid-daemon.out 2>&1 < /dev/null"
}

function Start-AutoFluidServerDaemon {
    $sshExe = Resolve-AutoFluidSshExe
    $target = Get-AutoFluidServerDaemonTarget
    $remoteCommand = Get-AutoFluidServerDaemonStartCommand
    Write-Host "Starting AutoFluid server daemon on ${target}..."
    & $sshExe -o BatchMode=yes -o ConnectTimeout=10 $target $remoteCommand
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to start AutoFluid server daemon on '$target'."
    }
}

function Wait-AutoFluidIpcProtocolEndpoint {
    param(
        [int]$TimeoutSeconds = 20
    )
    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    while ([DateTime]::UtcNow -lt $deadline) {
        if (Test-AutoFluidIpcProtocolEndpoint -TimeoutMs 1500) {
            return $true
        }
        Start-Sleep -Milliseconds 500
    }
    return $false
}

if (-not (Test-Path -LiteralPath $PythonExe -PathType Leaf)) {
    throw "Project virtual environment Python was not found: $PythonExe"
}

if ($Check) {
    & $PythonExe --version
    & $TunnelScript -Check
    & $WorkstationTunnelScript -Check
}

Write-AutoFluidEndpointSummary
if (-not $Check) {
    & $TunnelScript
    & $WorkstationTunnelScript
}
$tcpReady = Test-AutoFluidEndpoint
if (-not $tcpReady) {
    Write-Warning "AutoFluid daemon TCP endpoint is not reachable yet. Attempting to start the server daemon."
    if (-not $Check) {
        Start-AutoFluidServerDaemon
        if (-not (Wait-AutoFluidIpcProtocolEndpoint)) {
            throw "AutoFluid daemon IPC protocol is still not reachable after starting the server daemon."
        }
    }
}
elseif (-not (Test-AutoFluidIpcProtocolEndpoint)) {
    Write-Warning "AutoFluid daemon IPC protocol probe failed. Attempting to start the server daemon."
    if (-not $Check) {
        Start-AutoFluidServerDaemon
        if (-not (Wait-AutoFluidIpcProtocolEndpoint)) {
            throw "AutoFluid daemon IPC protocol is still not reachable after starting the server daemon."
        }
    }
}
Write-Host "AutoFluid startup preflight passed."
