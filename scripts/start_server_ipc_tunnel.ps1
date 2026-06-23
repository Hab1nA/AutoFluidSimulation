param(
    [switch]$Check,
    [switch]$Monitor,
    [switch]$NoMonitor,
    [int]$OwnerPid = 0,
    [int]$RestartDelaySeconds = 5,
    [int]$MaxConsecutiveFailures = 5,
    [int]$MaxRecoverySeconds = 120,
    [int]$LogRepeatSeconds = 60
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

function Quote-ProcessArgument {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Value
    )

    return '"' + ($Value -replace '"', '\"') + '"'
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

function Test-TunnelOwnerAlive {
    if ($OwnerPid -le 0) {
        return $true
    }
    return $null -ne (Get-Process -Id $OwnerPid -ErrorAction SilentlyContinue)
}

function Get-ServerTunnelLogDir {
    $logDir = Join-Path $ProjectDir "logs/local/tunnels/server-ipc"
    try {
        New-Item -ItemType Directory -Path $logDir -Force | Out-Null
        return $logDir
    }
    catch {
        return [System.IO.Path]::GetTempPath()
    }
}

function Get-ServerTunnelSupervisorLogPath {
    return Join-Path (Get-ServerTunnelLogDir) "server-ipc.supervisor.log"
}

function Stop-ServerTunnelChild {
    param([object]$Process)
    if ($null -ne $Process -and -not $Process.HasExited) {
        Stop-Process -Id $Process.Id -Force -ErrorAction SilentlyContinue
    }
}

function Test-ServerTunnelChildHealthy {
    param([object]$Process)
    return $null -ne $Process -and -not $Process.HasExited
}

function Test-ServerTunnelListener {
    param(
        [Parameter(Mandatory = $true)]
        [string]$LocalHost,
        [Parameter(Mandatory = $true)]
        [int]$LocalPort
    )

    $connections = Get-NetTCPConnection -LocalPort $LocalPort -State Listen -ErrorAction SilentlyContinue
    foreach ($connection in $connections) {
        if ($connection.LocalAddress -eq $LocalHost -or $connection.LocalAddress -eq "0.0.0.0" -or $connection.LocalAddress -eq "::") {
            return $true
        }
    }
    return $false
}

function Write-RateLimitedTunnelLog {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Message,
        [Parameter(Mandatory = $true)]
        [string]$Key,
        [hashtable]$LogState,
        [switch]$Force
    )

    $now = Get-Date
    $lastKey = [string]$LogState.LastKey
    $lastAt = $LogState.LastAt
    if ($Force -or $lastKey -ne $Key -or $null -eq $lastAt -or ($now - $lastAt).TotalSeconds -ge $LogRepeatSeconds) {
        $SupervisorLogPath = Get-ServerTunnelSupervisorLogPath
        $line = "{0:yyyy-MM-dd HH:mm:ss.fff} {1}" -f $now, $Message
        Add-Content -LiteralPath $SupervisorLogPath -Value $line -Encoding UTF8
        $LogState.LastKey = $Key
        $LogState.LastAt = $now
    }
}

function Register-TunnelFailure {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Reason,
        [hashtable]$FailureState,
        [hashtable]$LogState
    )

    $now = Get-Date
    if ($null -eq $FailureState.FirstFailureAt) {
        $FailureState.FirstFailureAt = $now
    }
    $FailureState.ConsecutiveFailures = [int]$FailureState.ConsecutiveFailures + 1
    $elapsed = ($now - $FailureState.FirstFailureAt).TotalSeconds
    if ($FailureState.ConsecutiveFailures -ge $MaxConsecutiveFailures -or $elapsed -ge $MaxRecoverySeconds) {
        Write-RateLimitedTunnelLog `
            -Message "Server IPC tunnel recovery budget exhausted after $($FailureState.ConsecutiveFailures) failures over $([math]::Round($elapsed, 1))s: $Reason" `
            -Key "budget-exhausted" `
            -LogState $LogState `
            -Force
        return $false
    }
    Write-RateLimitedTunnelLog `
        -Message "Server IPC tunnel endpoint is not reachable ($Reason); retrying in ${RestartDelaySeconds}s." `
        -Key $Reason `
        -LogState $LogState
    return $true
}

function Reset-TunnelFailureBudget {
    param([hashtable]$FailureState)
    $FailureState.ConsecutiveFailures = 0
    $FailureState.FirstFailureAt = $null
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
    $failureState = @{ ConsecutiveFailures = 0; FirstFailureAt = $null }
    $logState = @{ LastKey = ""; LastAt = $null }
    while ($true) {
        if (-not (Test-TunnelOwnerAlive)) {
            Stop-ServerTunnelChild -Process $process
            Write-RateLimitedTunnelLog -Message "Server IPC tunnel owner is gone; monitor exiting." -Key "owner-gone" -LogState $logState -Force
            exit 0
        }

        if (Test-AutoFluidIpcProtocolEndpoint) {
            Reset-TunnelFailureBudget -FailureState $failureState
            Start-Sleep -Seconds 5
            continue
        }

        if (Test-ServerTunnelChildHealthy -Process $process) {
            if (Test-ServerTunnelListener -LocalHost $LocalHost -LocalPort $LocalPort) {
                Write-RateLimitedTunnelLog `
                    -Message "Server IPC tunnel is listening; remote daemon is not ready yet." `
                    -Key "remote-daemon-not-ready" `
                    -LogState $logState
                Start-Sleep -Seconds $RestartDelaySeconds
                continue
            }

            Stop-ServerTunnelChild -Process $process
            if (-not (Register-TunnelFailure -Reason "listener-not-ready" -FailureState $failureState -LogState $logState)) {
                exit 0
            }
            Start-Sleep -Seconds $RestartDelaySeconds
            continue
        }

        Stop-ServerTunnelChild -Process $process
        try {
            $process = Start-ServerTunnel `
                -SshExe $SshExe `
                -TunnelTarget $TunnelTarget `
                -LocalHost $LocalHost `
                -LocalPort $LocalPort `
                -RemoteHost $RemoteHost `
                -RemotePort $RemotePort `
                -SkipPidFile
        }
        catch {
            if (-not (Register-TunnelFailure -Reason "ssh-start-failed" -FailureState $failureState -LogState $logState)) {
                Stop-ServerTunnelChild -Process $process
                exit 0
            }
            Start-Sleep -Seconds $RestartDelaySeconds
            continue
        }

        Start-Sleep -Seconds $RestartDelaySeconds
        if (-not (Test-ServerTunnelListener -LocalHost $LocalHost -LocalPort $LocalPort)) {
            Stop-ServerTunnelChild -Process $process
            if (-not (Register-TunnelFailure -Reason "listener-not-ready" -FailureState $failureState -LogState $logState)) {
                exit 0
            }
        }
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
        "-File", (Quote-ProcessArgument -Value $PSCommandPath),
        "-Monitor",
        "-OwnerPid", ([string]$OwnerPid),
        "-RestartDelaySeconds", ([string]$RestartDelaySeconds),
        "-MaxConsecutiveFailures", ([string]$MaxConsecutiveFailures),
        "-MaxRecoverySeconds", ([string]$MaxRecoverySeconds),
        "-LogRepeatSeconds", ([string]$LogRepeatSeconds)
    )
    $process = Start-Process -FilePath $PowerShellExe `
        -ArgumentList $argumentList `
        -WindowStyle Hidden `
        -PassThru
    Update-ServerTunnelPidFile -TunnelPid $process.Id
    return $process
}

function Stop-ServerTunnelListeningProcess {
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

    $existingPid = Get-ServerTunnelListeningPid `
        -LocalHost $LocalHost `
        -LocalPort $LocalPort `
        -RemoteHost $RemoteHost `
        -RemotePort $RemotePort `
        -TunnelTarget $TunnelTarget
    if ($null -eq $existingPid) {
        return
    }

    Stop-Process -Id $existingPid -Force -ErrorAction SilentlyContinue
    Write-Host "Stopped existing unmanaged server IPC tunnel listener (PID $existingPid)."
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

if (-not $Check -and -not $NoMonitor -and -not $Monitor -and $OwnerPid -le 0) {
    throw "AutoFluid server IPC tunnel monitor owner is required; pass -OwnerPid for owner-bound recovery."
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

    Stop-ServerTunnelListeningProcess `
        -LocalHost $serverHost `
        -LocalPort $serverPort `
        -RemoteHost $remoteHost `
        -RemotePort $remotePort `
        -TunnelTarget $tunnelTarget
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
