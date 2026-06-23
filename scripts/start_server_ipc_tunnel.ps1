param(
    [switch]$Check,
    [switch]$Monitor,
    [switch]$NoMonitor,
    [int]$RestartDelaySeconds = 5
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

function Resolve-PowerShellExe {
    $currentProcess = Get-Process -Id $PID -ErrorAction SilentlyContinue
    if ($null -ne $currentProcess -and -not [string]::IsNullOrWhiteSpace($currentProcess.Path)) {
        return $currentProcess.Path
    }
    foreach ($candidate in @("pwsh.exe", "powershell.exe")) {
        $command = Get-Command $candidate -ErrorAction SilentlyContinue
        if ($null -ne $command) {
            return $command.Source
        }
    }
    throw "PowerShell executable was not found."
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
        [int]$RemotePort,
        [switch]$SkipPidFile
    )

    $forwardSpec = "${LocalHost}:${LocalPort}:${RemoteHost}:${RemotePort}"
    $logDir = Join-Path $ProjectDir "logs/local/tunnels/server-ipc"
    try {
        New-Item -ItemType Directory -Path $logDir -Force | Out-Null
    }
    catch {
        $logDir = [System.IO.Path]::GetTempPath()
    }
    $stdoutLogPath = Join-Path $logDir "${LocalPort}.out.log"
    $stderrLogPath = Join-Path $logDir "${LocalPort}.err.log"
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

    if (-not $SkipPidFile) {
        Update-ServerTunnelPidFile -TunnelPid $process.Id
    }

    return $process
}

function Get-ServerTunnelPidFilePath {
    return Join-Path $ProjectDir "data/server_ipc_tunnel.pid"
}

function Get-ServerTunnelMonitorPid {
    $pidFile = Get-ServerTunnelPidFilePath
    if (-not (Test-Path -LiteralPath $pidFile -PathType Leaf)) {
        return $null
    }

    $rawTunnelPid = (Get-Content -LiteralPath $pidFile -Raw).Trim()
    $parsedTunnelPid = 0
    if (-not [int]::TryParse($rawTunnelPid, [ref]$parsedTunnelPid) -or $parsedTunnelPid -le 0) {
        return $null
    }

    $process = Get-CimInstance Win32_Process -Filter "ProcessId = $parsedTunnelPid" -ErrorAction SilentlyContinue
    if ($null -eq $process) {
        return $null
    }

    $scriptPath = [string]$PSCommandPath
    $commandLine = [string]$process.CommandLine
    if (-not [string]::IsNullOrWhiteSpace($scriptPath) -and
        $commandLine.Contains($scriptPath) -and
        $commandLine.Contains("-Monitor")) {
        return $parsedTunnelPid
    }
    return $null
}

function Update-ServerTunnelPidFile {
    param(
        [Parameter(Mandatory = $true)]
        [int]$TunnelPid
    )

    $pidFile = Get-ServerTunnelPidFilePath
    New-Item -ItemType Directory -Path (Split-Path -Parent $pidFile) -Force | Out-Null
    Set-Content -LiteralPath $pidFile -Value ([string]$TunnelPid) -Encoding ASCII
}

function Start-ServerTunnelMonitor {
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

    Update-ServerTunnelPidFile -TunnelPid $PID
    $process = $null
    while ($true) {
        if (Test-AutoFluidIpcProtocolEndpoint) {
            Start-Sleep -Seconds 5
            continue
        }

        Write-Host "Server IPC tunnel endpoint dropped; restarting."
        if ($null -ne $process -and -not $process.HasExited) {
            Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
        }
        $process = Start-ServerTunnel `
            -SshExe $SshExe `
            -TunnelTarget $TunnelTarget `
            -LocalHost $LocalHost `
            -LocalPort $LocalPort `
            -RemoteHost $RemoteHost `
            -RemotePort $RemotePort `
            -SkipPidFile
        Start-Sleep -Seconds $RestartDelaySeconds
    }
}

function Start-ServerTunnelMonitorProcess {
    param(
        [Parameter(Mandatory = $true)]
        [string]$PowerShellExe
    )

    $argumentList = @(
        "-NoLogo",
        "-NoProfile",
        "-ExecutionPolicy", "Bypass",
        "-File", $PSCommandPath,
        "-Monitor",
        "-NoMonitor",
        "-RestartDelaySeconds", ([string]$RestartDelaySeconds)
    )
    $process = Start-Process -FilePath $PowerShellExe `
        -ArgumentList $argumentList `
        -WindowStyle Hidden `
        -PassThru
    Update-ServerTunnelPidFile -TunnelPid $process.Id
    return $process
}

function Get-ServerTunnelListeningPid {
    param(
        [Parameter(Mandatory = $true)]
        [string]$LocalHost,
        [Parameter(Mandatory = $true)]
        [int]$LocalPort,
        [Parameter(Mandatory = $true)]
        [string]$RemoteHost,
        [Parameter(Mandatory = $true)]
        [int]$RemotePort,
        [Parameter(Mandatory = $true)]
        [string]$TunnelTarget
    )

    $expectedForward = "${LocalHost}:${LocalPort}:${RemoteHost}:${RemotePort}"
    $connections = Get-NetTCPConnection -LocalPort $LocalPort -State Listen -ErrorAction SilentlyContinue
    $matchingPids = @()
    foreach ($connection in $connections) {
        if ($connection.LocalAddress -eq $LocalHost -or $connection.LocalAddress -eq "0.0.0.0" -or $connection.LocalAddress -eq "::") {
            $matchingPids += [int]$connection.OwningProcess
        }
    }
    foreach ($candidatePid in $matchingPids) {
        $process = Get-CimInstance Win32_Process -Filter "ProcessId=$candidatePid" -ErrorAction SilentlyContinue
        if ($null -ne $process -and $process.CommandLine -like "*-L $expectedForward*" -and $process.CommandLine -like "* $TunnelTarget*") {
            return [int]$candidatePid
        }
    }
    return $null
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

if ($Monitor) {
    Start-ServerTunnelMonitor `
        -SshExe $sshExe `
        -TunnelTarget $tunnelTarget `
        -LocalHost $serverHost `
        -LocalPort $serverPort `
        -RemoteHost $remoteHost `
        -RemotePort $remotePort
    exit 0
}

if (Test-AutoFluidIpcProtocolEndpoint) {
    if ($NoMonitor) {
        $existingPid = Get-ServerTunnelListeningPid `
            -LocalHost $serverHost `
            -LocalPort $serverPort `
            -RemoteHost $remoteHost `
            -RemotePort $remotePort `
            -TunnelTarget $tunnelTarget
        if ($null -ne $existingPid) {
            Update-ServerTunnelPidFile -TunnelPid $existingPid
        }
        Write-Host "AutoFluid server IPC protocol endpoint is already reachable; reuse the existing tunnel."
        exit 0
    }

    Write-Host "AutoFluid server IPC protocol endpoint is already reachable; ensuring monitor is running."
}

if (-not $NoMonitor) {
    $existingMonitorPid = Get-ServerTunnelMonitorPid
    if ($null -ne $existingMonitorPid) {
        Write-Host "AutoFluid server IPC tunnel monitor is already running (PID $existingMonitorPid)."
        exit 0
    }

    $monitorProcess = Start-ServerTunnelMonitorProcess -PowerShellExe (Resolve-PowerShellExe)
    Write-Host "Started AutoFluid server IPC tunnel monitor (PID $($monitorProcess.Id))."
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
