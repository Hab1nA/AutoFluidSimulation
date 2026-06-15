param(
    [switch]$Check
)

$ErrorActionPreference = "Stop"

$ProjectDir = Split-Path -Parent $PSScriptRoot
$EnvScript = Join-Path $PSScriptRoot "autofluid_env.ps1"

. $EnvScript
Import-AutoFluidEnv -ProjectDir $ProjectDir
Assert-AutoFluidServerEndpoint

function Resolve-SshExe {
    if (-not [string]::IsNullOrWhiteSpace($env:AUTOFLUID_SSH_EXE)) {
        return $env:AUTOFLUID_SSH_EXE
    }
    $command = Get-Command ssh.exe -ErrorAction SilentlyContinue
    if ($null -eq $command) {
        throw "ssh.exe was not found in PATH."
    }
    return $command.Source
}

function Get-TunnelSshTarget {
    foreach ($candidate in @(
        $env:AUTOFLUID_SERVER_TUNNEL_HOST,
        $env:AUTOFLUID_SERVER_DAEMON_SSH_TARGET,
        $env:AUTOFLUID_SERVER_HOST,
        $env:AUTOFLUID_IPC_HOST
    )) {
        if (-not [string]::IsNullOrWhiteSpace($candidate)) {
            return $candidate
        }
    }
    return "ocar"
}

function Get-TunnelRemoteHost {
    if (-not [string]::IsNullOrWhiteSpace($env:AUTOFLUID_SERVER_TUNNEL_REMOTE_HOST)) {
        return $env:AUTOFLUID_SERVER_TUNNEL_REMOTE_HOST
    }
    return "127.0.0.1"
}

function Get-TunnelRemotePort {
    if ([string]::IsNullOrWhiteSpace($env:AUTOFLUID_SERVER_TUNNEL_REMOTE_PORT)) {
        return 9527
    }

    $port = 0
    if (-not [int]::TryParse($env:AUTOFLUID_SERVER_TUNNEL_REMOTE_PORT, [ref]$port)) {
        throw "AUTOFLUID_SERVER_TUNNEL_REMOTE_PORT is not a valid integer."
    }
    if ($port -le 0 -or $port -gt 65535) {
        throw "AUTOFLUID_SERVER_TUNNEL_REMOTE_PORT is out of range: $port"
    }
    return $port
}

function Test-SshBatchMode {
    param(
        [Parameter(Mandatory = $true)]
        [string]$SshExe,
        [Parameter(Mandatory = $true)]
        [string]$TunnelTarget
    )

    & $SshExe -o BatchMode=yes $TunnelTarget "echo ok" | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "SSH batch-mode connection to '$TunnelTarget' failed. Configure your SSH key/agent or host alias before launching AutoFluid."
    }
}

function Start-ServerTunnel {
    param(
        [Parameter(Mandatory = $true)]
        [string]$SshExe,
        [Parameter(Mandatory = $true)]
        [string]$TunnelTarget,
        [Parameter(Mandatory = $true)]
        [string]$LocalHost,
        [Parameter(Mandatory = $true)]
        [int]$LocalPort,
        [Parameter(Mandatory = $true)]
        [string]$RemoteHost,
        [Parameter(Mandatory = $true)]
        [int]$RemotePort
    )

    $forwardSpec = "${LocalHost}:${LocalPort}:${RemoteHost}:${RemotePort}"
    $stdoutLogPath = Join-Path ([System.IO.Path]::GetTempPath()) "autofluid-server-ipc-tunnel-${LocalPort}.out.log"
    $stderrLogPath = Join-Path ([System.IO.Path]::GetTempPath()) "autofluid-server-ipc-tunnel-${LocalPort}.err.log"
    foreach ($logPath in @($stdoutLogPath, $stderrLogPath)) {
        if (Test-Path -LiteralPath $logPath -PathType Leaf) {
            Remove-Item -LiteralPath $logPath -Force -ErrorAction SilentlyContinue
        }
    }

    $argumentList = @(
        "-o", "BatchMode=yes",
        "-o", "ExitOnForwardFailure=yes",
        "-o", "ServerAliveInterval=30",
        "-o", "ServerAliveCountMax=3",
        "-N",
        "-L", $forwardSpec,
        $TunnelTarget
    )

    $process = Start-Process -FilePath $SshExe `
        -ArgumentList $argumentList `
        -WindowStyle Hidden `
        -PassThru `
        -RedirectStandardOutput $stdoutLogPath `
        -RedirectStandardError $stderrLogPath

    Start-Sleep -Seconds 2

    if ($process.HasExited) {
        $detail = ""
        $logLines = @()
        foreach ($logPath in @($stdoutLogPath, $stderrLogPath)) {
            if (Test-Path -LiteralPath $logPath -PathType Leaf) {
                $logLines += Get-Content -LiteralPath $logPath -TotalCount 40
            }
        }
        if ($logLines.Count -gt 0) {
            $detail = $logLines -join [Environment]::NewLine
        }
        throw "Failed to start AutoFluid server IPC tunnel. $detail"
    }

    $pidFile = Join-Path $ProjectDir "data/server_ipc_tunnel.pid"
    New-Item -ItemType Directory -Path (Split-Path -Parent $pidFile) -Force | Out-Null
    Set-Content -LiteralPath $pidFile -Value ([string]$process.Id) -Encoding ASCII

    return $process
}

$serverHost = Get-AutoFluidServerHost
$serverPort = Get-AutoFluidServerPort
$needsTunnel = Test-AutoFluidLocalEndpoint
$tunnelTarget = Get-TunnelSshTarget
$remoteHost = Get-TunnelRemoteHost
$remotePort = Get-TunnelRemotePort

Write-AutoFluidEndpointSummary

if (-not $needsTunnel) {
    Write-Host "Remote daemon endpoint is configured directly; no local SSH tunnel is required."
    exit 0
}

Write-Host "Tunnel SSH target: $tunnelTarget"
Write-Host "Tunnel remote IPC endpoint: ${remoteHost}:${remotePort}"

$sshExe = Resolve-SshExe
Test-SshBatchMode -SshExe $sshExe -TunnelTarget $tunnelTarget

if ($Check) {
    [void](Test-AutoFluidIpcProtocolEndpoint)
    Write-Host "Server IPC tunnel check passed."
    exit 0
}

if (Test-AutoFluidIpcProtocolEndpoint) {
    Write-Host "AutoFluid server IPC protocol endpoint is already reachable; reuse the existing tunnel."
    exit 0
}

$process = Start-ServerTunnel `
    -SshExe $sshExe `
    -TunnelTarget $tunnelTarget `
    -LocalHost $serverHost `
    -LocalPort $serverPort `
    -RemoteHost $remoteHost `
    -RemotePort $remotePort

Write-Host "Started AutoFluid server IPC tunnel with $sshExe (PID $($process.Id))."
