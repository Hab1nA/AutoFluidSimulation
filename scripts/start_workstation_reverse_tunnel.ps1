param(
    [switch]$Check,
    [switch]$Monitor,
    [switch]$InstallWatchdog,
    [switch]$UninstallWatchdog,
    [switch]$NoWatchdog,
    [ValidateSet("Workstation", "LocalWorker")]
    [string]$TunnelKind = "Workstation",
    [int]$OwnerPid = 0,
    [string]$OwnerMarkerPath = "",
    [int]$RestartDelaySeconds = 5,
    [int]$MaxConsecutiveFailures = 5,
    [int]$MaxRecoverySeconds = 120,
    [int]$LogRepeatSeconds = 60,
    [int]$MonitorRemotePort = 0
)

$ErrorActionPreference = "Stop"

$ProjectDir = Split-Path -Parent $PSScriptRoot
$EnvScript = Join-Path $PSScriptRoot "autofluid_env.ps1"

. $EnvScript
Import-AutoFluidEnv -ProjectDir $ProjectDir
Assert-AutoFluidServerEndpoint

function Test-TunnelOwnerConfigured {
    if ($OwnerPid -gt 0) {
        return $true
    }
    if (-not [string]::IsNullOrWhiteSpace($OwnerMarkerPath)) {
        return $true
    }
    return $false
}

function Test-TunnelOwnerAlive {
    if ($OwnerPid -gt 0) {
        return $null -ne (Get-Process -Id $OwnerPid -ErrorAction SilentlyContinue)
    }
    if (-not [string]::IsNullOrWhiteSpace($OwnerMarkerPath)) {
        return Test-Path -LiteralPath $OwnerMarkerPath
    }
    return $false
}

function Stop-ReverseTunnelChild {
    param([object]$Process)
    if ($null -ne $Process -and -not $Process.HasExited) {
        Stop-Process -Id $Process.Id -Force -ErrorAction SilentlyContinue
    }
}

function Write-RateLimitedTunnelLog {
    param(
        [Parameter(Mandatory = $true)]
        [string]$LogPath,
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
        Write-TunnelSupervisorLog -LogPath $LogPath -Message $Message
        $LogState.LastKey = $Key
        $LogState.LastAt = $now
    }
}

function Register-TunnelFailure {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Reason,
        [Parameter(Mandatory = $true)]
        [string]$LogPath,
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
            -LogPath $LogPath `
            -Message "$TunnelKind reverse tunnel recovery budget exhausted after $($FailureState.ConsecutiveFailures) failures over $([math]::Round($elapsed, 1))s: $Reason" `
            -Key "budget-exhausted" `
            -LogState $LogState `
            -Force
        return $false
    }
    Write-RateLimitedTunnelLog `
        -LogPath $LogPath `
        -Message "$TunnelKind reverse tunnel is not reachable ($Reason); retrying in ${RestartDelaySeconds}s." `
        -Key $Reason `
        -LogState $LogState
    return $true
}

function Reset-TunnelFailureBudget {
    param([hashtable]$FailureState)
    $FailureState.ConsecutiveFailures = 0
    $FailureState.FirstFailureAt = $null
}
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

function Resolve-WScriptExe {
    $command = Get-Command wscript.exe -ErrorAction SilentlyContinue
    if ($null -eq $command) {
        throw "wscript.exe was not found in PATH."
    }
    return $command.Source
}

function Get-TunnelSshTarget {
    if ($TunnelKind -eq "LocalWorker" -and
        -not [string]::IsNullOrWhiteSpace($env:AUTOFLUID_LOCAL_WORKER_TUNNEL_HOST)) {
        return $env:AUTOFLUID_LOCAL_WORKER_TUNNEL_HOST
    }

    foreach ($candidate in @(
        $env:AUTOFLUID_WORKSTATION_TUNNEL_HOST,
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

function Get-EnvInt {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Name,
        [Parameter(Mandatory = $true)]
        [int]$DefaultValue
    )

    $rawValue = [Environment]::GetEnvironmentVariable($Name, "Process")
    if ([string]::IsNullOrWhiteSpace($rawValue)) {
        return $DefaultValue
    }

    $value = 0
    if (-not [int]::TryParse($rawValue, [ref]$value)) {
        throw "$Name is not a valid integer."
    }
    if ($value -le 0 -or $value -gt 65535) {
        throw "$Name is out of range: $value"
    }
    return $value
}

function Get-RemoteBindHost {
    if ($TunnelKind -eq "LocalWorker") {
        if (-not [string]::IsNullOrWhiteSpace($env:AUTOFLUID_WORKER_REACHABLE_HOST)) {
            return $env:AUTOFLUID_WORKER_REACHABLE_HOST
        }
        return "127.0.0.1"
    }

    if (-not [string]::IsNullOrWhiteSpace($env:AUTOFLUID_SSH_REACHABLE_HOST)) {
        return $env:AUTOFLUID_SSH_REACHABLE_HOST
    }
    return "127.0.0.1"
}

function Get-RemoteBindPort {
    if ($TunnelKind -eq "LocalWorker") {
        return Get-EnvInt -Name "AUTOFLUID_WORKER_SSH_PORT" -DefaultValue 2223
    }

    return Get-EnvInt -Name "AUTOFLUID_SSH_REACHABLE_PORT" -DefaultValue 2222
}

function Get-TunnelWatchdogTaskName {
    param(
        [Parameter(Mandatory = $true)]
        [int]$RemotePort
    )

    return "AutoFluidTunnelWatchdog-$TunnelKind-$RemotePort"
}

function ConvertTo-VbsStringLiteral {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Value
    )

    return '"' + $Value.Replace('"', '""') + '"'
}

function ConvertTo-WindowsCommandArgument {
    param(
        [Parameter(Mandatory = $true)]
        [AllowEmptyString()]
        [string]$Value
    )

    return '"' + $Value.Replace('"', '\"') + '"'
}

function Get-TunnelWatchdogLauncherPath {
    param(
        [Parameter(Mandatory = $true)]
        [int]$RemotePort
    )

    $launcherDir = Join-Path (Join-Path $ProjectDir "data") "watchdog"
    $launcherName = "autofluid-tunnel-watchdog-$TunnelKind-$RemotePort.vbs"
    return Join-Path $launcherDir $launcherName
}

function New-TunnelWatchdogLauncher {
    param(
        [Parameter(Mandatory = $true)]
        [int]$RemotePort,
        [Parameter(Mandatory = $true)]
        [string]$PowerShellExe
    )

    $scriptPath = $PSCommandPath
    $launcherPath = Get-TunnelWatchdogLauncherPath -RemotePort $RemotePort
    $launcherDir = Split-Path -Parent $launcherPath
    if (-not [string]::IsNullOrWhiteSpace($launcherDir)) {
        New-Item -ItemType Directory -Path $launcherDir -Force | Out-Null
    }

    $commandParts = @(
        $PowerShellExe,
        "-NoProfile",
        "-ExecutionPolicy", "Bypass",
        "-File", $scriptPath,
        "-TunnelKind", $TunnelKind,
        "-NoWatchdog",
        "-RestartDelaySeconds", ([string]$RestartDelaySeconds),
        "-OwnerPid", ([string]$OwnerPid),
        "-OwnerMarkerPath", $OwnerMarkerPath,
        "-MaxConsecutiveFailures", ([string]$MaxConsecutiveFailures),
        "-MaxRecoverySeconds", ([string]$MaxRecoverySeconds),
        "-LogRepeatSeconds", ([string]$LogRepeatSeconds)
    )
    $command = ($commandParts | ForEach-Object { ConvertTo-WindowsCommandArgument -Value $_ }) -join " "
    $launcherContent = @(
        'Set shell = CreateObject("WScript.Shell")',
        "command = $(ConvertTo-VbsStringLiteral -Value $command)",
        'shell.Run command, 0, False'
    ) -join "`r`n"
    Set-Content -LiteralPath $launcherPath -Value $launcherContent -Encoding ASCII
    return $launcherPath
}

function Remove-TunnelWatchdogLauncher {
    param(
        [Parameter(Mandatory = $true)]
        [int]$RemotePort
    )

    $launcherPath = Get-TunnelWatchdogLauncherPath -RemotePort $RemotePort
    Remove-Item -LiteralPath $launcherPath -Force -ErrorAction SilentlyContinue
    return $launcherPath
}

function Install-TunnelWatchdogTask {
    param(
        [Parameter(Mandatory = $true)]
        [int]$RemotePort,
        [Parameter(Mandatory = $true)]
        [string]$PowerShellExe
    )

    $taskName = Get-TunnelWatchdogTaskName -RemotePort $RemotePort
    $wscriptExe = Resolve-WScriptExe
    $launcherPath = New-TunnelWatchdogLauncher -RemotePort $RemotePort -PowerShellExe $PowerShellExe
    $arguments = ConvertTo-WindowsCommandArgument -Value $launcherPath

    $action = New-ScheduledTaskAction -Execute $wscriptExe -Argument $arguments
    $trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
        -RepetitionInterval (New-TimeSpan -Minutes 1) `
        -RepetitionDuration (New-TimeSpan -Days 3650)
    $settings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -MultipleInstances IgnoreNew `
        -ExecutionTimeLimit (New-TimeSpan -Minutes 2)

    Register-ScheduledTask -TaskName $taskName `
        -Action $action `
        -Trigger $trigger `
        -Settings $settings `
        -Description "AutoFluid $TunnelKind reverse tunnel watchdog for remote port $RemotePort" `
        -Force | Out-Null
    return $taskName
}

function Uninstall-TunnelWatchdogTask {
    param(
        [Parameter(Mandatory = $true)]
        [int]$RemotePort
    )

    $taskName = Get-TunnelWatchdogTaskName -RemotePort $RemotePort
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
    return $taskName
}

function Get-TargetHost {
    if ($TunnelKind -eq "LocalWorker") {
        if (-not [string]::IsNullOrWhiteSpace($env:AUTOFLUID_LOCAL_WORKER_TUNNEL_TARGET_HOST)) {
            return $env:AUTOFLUID_LOCAL_WORKER_TUNNEL_TARGET_HOST
        }
        return "127.0.0.1"
    }

    foreach ($candidate in @(
        $env:AUTOFLUID_WORKSTATION_TUNNEL_TARGET_HOST,
        $env:AUTOFLUID_SSH_HOST
    )) {
        if (-not [string]::IsNullOrWhiteSpace($candidate)) {
            return $candidate
        }
    }
    return "172.17.135.240"
}

function Get-TargetPort {
    if ($TunnelKind -eq "LocalWorker") {
        return Get-EnvInt -Name "AUTOFLUID_LOCAL_WORKER_TUNNEL_TARGET_PORT" -DefaultValue 22
    }

    if (-not [string]::IsNullOrWhiteSpace($env:AUTOFLUID_WORKSTATION_TUNNEL_TARGET_PORT)) {
        return Get-EnvInt -Name "AUTOFLUID_WORKSTATION_TUNNEL_TARGET_PORT" -DefaultValue 22
    }
    return Get-EnvInt -Name "AUTOFLUID_SSH_PORT" -DefaultValue 22
}

function Test-TcpEndpoint {
    param(
        [Parameter(Mandatory = $true)]
        [string]$HostName,
        [Parameter(Mandatory = $true)]
        [int]$Port,
        [int]$TimeoutMs = 1500
    )

    $client = [System.Net.Sockets.TcpClient]::new()
    try {
        $async = $client.BeginConnect($HostName, $Port, $null, $null)
        if (-not $async.AsyncWaitHandle.WaitOne($TimeoutMs)) {
            return $false
        }
        $client.EndConnect($async)
        return $true
    }
    catch {
        return $false
    }
    finally {
        $client.Close()
    }
}

function Test-RemoteTunnelEndpoint {
    param(
        [Parameter(Mandatory = $true)]
        [string]$SshExe,
        [Parameter(Mandatory = $true)]
        [string]$TunnelTarget,
        [Parameter(Mandatory = $true)]
        [string]$RemoteHost,
        [Parameter(Mandatory = $true)]
        [int]$RemotePort
    )

    $hostBytes = (($RemoteHost.ToCharArray() | ForEach-Object { [int][char]$_ }) -join ",")
    $remoteCommand = "python3 -c 'import socket; s=socket.socket(); s.settimeout(2); s.connect((bytes([$hostBytes]).decode(),$RemotePort)); s.close()'"
    $previousErrorActionPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        & $SshExe -o BatchMode=yes -o ConnectTimeout=10 $TunnelTarget $remoteCommand 2>&1 | Out-Null
        return $LASTEXITCODE -eq 0
    }
    finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }
}

function Get-TunnelLogPaths {
    param(
        [Parameter(Mandatory = $true)]
        [int]$RemotePort
    )

    $tunnelName = if ($TunnelKind -eq "LocalWorker") { "local-worker" } else { "workstation" }
    $logDir = Join-Path $ProjectDir "logs/local/tunnels/$tunnelName"
    try {
        New-Item -ItemType Directory -Path $logDir -Force | Out-Null
    }
    catch {
        $logDir = [System.IO.Path]::GetTempPath()
    }
    return @{
        Stdout = Join-Path $logDir "${RemotePort}.out.log"
        Stderr = Join-Path $logDir "${RemotePort}.err.log"
        Supervisor = Join-Path $logDir "${RemotePort}.supervisor.log"
    }
}

function Get-TunnelPidFile {
    if (-not [string]::IsNullOrWhiteSpace($env:AUTOFLUID_TUNNEL_PID_FILE)) {
        return $env:AUTOFLUID_TUNNEL_PID_FILE
    }
    $pidName = if ($TunnelKind -eq "LocalWorker") { "tunnel_localworker.pid" } else { "tunnel_workstation.pid" }
    return Join-Path (Join-Path $ProjectDir "data") $pidName
}

function Write-TunnelSupervisorLog {
    param(
        [Parameter(Mandatory = $true)]
        [string]$LogPath,
        [Parameter(Mandatory = $true)]
        [string]$Message
    )

    $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Add-Content -LiteralPath $LogPath -Value "[$timestamp] $Message"
}

function Get-ReverseTunnelArguments {
    param(
        [Parameter(Mandatory = $true)]
        [string]$TunnelTarget,
        [Parameter(Mandatory = $true)]
        [string]$RemoteHost,
        [Parameter(Mandatory = $true)]
        [int]$RemotePort,
        [Parameter(Mandatory = $true)]
        [string]$TargetHost,
        [Parameter(Mandatory = $true)]
        [int]$TargetPort
    )

    $forwardSpec = "${RemoteHost}:${RemotePort}:${TargetHost}:${TargetPort}"
    return @(
        "-o", "BatchMode=yes",
        "-o", "ExitOnForwardFailure=yes",
        "-o", "ServerAliveInterval=5",
        "-o", "ServerAliveCountMax=3",
        "-o", "TCPKeepAlive=yes",
        "-N",
        "-R", $forwardSpec,
        $TunnelTarget
    )
}

function Get-TunnelWin32Processes {
    try {
        return @(Get-CimInstance -ClassName Win32_Process -ErrorAction Stop)
    }
    catch {
        return @()
    }
}
function Start-ReverseTunnelMonitor {
    param(
        [Parameter(Mandatory = $true)]
        [string]$SshExe,
        [Parameter(Mandatory = $true)]
        [string]$TunnelTarget,
        [Parameter(Mandatory = $true)]
        [string]$RemoteHost,
        [Parameter(Mandatory = $true)]
        [int]$RemotePort,
        [Parameter(Mandatory = $true)]
        [string]$TargetHost,
        [Parameter(Mandatory = $true)]
        [int]$TargetPort
    )

    $logs = Get-TunnelLogPaths -RemotePort $RemotePort
    Write-TunnelSupervisorLog -LogPath $logs.Supervisor -Message "Supervisor starting for ${RemoteHost}:${RemotePort} -> ${TargetHost}:${TargetPort} via ${TunnelTarget}."
    $failureState = @{ ConsecutiveFailures = 0; FirstFailureAt = $null }
    $logState = @{ LastKey = ""; LastAt = $null }
    $sshProcess = $null

    while ($true) {
        if (-not (Test-TunnelOwnerAlive)) {
            Stop-ReverseTunnelChild -Process $sshProcess
            Write-RateLimitedTunnelLog -LogPath $logs.Supervisor -Message "$TunnelKind reverse tunnel owner is gone; monitor exiting." -Key "owner-gone" -LogState $logState -Force
            exit 0
        }

        if ($null -ne $sshProcess -and $sshProcess.HasExited) {
            $exitCode = $sshProcess.ExitCode
            $sshProcess = $null
            if (-not (Register-TunnelFailure -Reason "ssh-exited-$exitCode" -LogPath $logs.Supervisor -FailureState $failureState -LogState $logState)) {
                exit 0
            }
            Start-Sleep -Seconds $RestartDelaySeconds
            continue
        }

        if (Test-RemoteTunnelEndpoint `
                -SshExe $SshExe `
                -TunnelTarget $TunnelTarget `
                -RemoteHost $RemoteHost `
                -RemotePort $RemotePort) {
            Reset-TunnelFailureBudget -FailureState $failureState
            Start-Sleep -Seconds 5
            continue
        }

        if (-not (Test-TcpEndpoint -HostName $TargetHost -Port $TargetPort)) {
            if (-not (Register-TunnelFailure -Reason "target-unreachable" -LogPath $logs.Supervisor -FailureState $failureState -LogState $logState)) {
                Stop-ReverseTunnelChild -Process $sshProcess
                exit 0
            }
            Start-Sleep -Seconds $RestartDelaySeconds
            continue
        }

        if ($null -ne $sshProcess -and -not $sshProcess.HasExited) {
            Stop-ReverseTunnelChild -Process $sshProcess
            $sshProcess = $null
            if (-not (Register-TunnelFailure -Reason "remote-probe-failed" -LogPath $logs.Supervisor -FailureState $failureState -LogState $logState)) {
                exit 0
            }
            Start-Sleep -Seconds $RestartDelaySeconds
            continue
        }

        $argumentList = Get-ReverseTunnelArguments `
            -TunnelTarget $TunnelTarget `
            -RemoteHost $RemoteHost `
            -RemotePort $RemotePort `
            -TargetHost $TargetHost `
            -TargetPort $TargetPort

        Write-RateLimitedTunnelLog -LogPath $logs.Supervisor -Message "Starting ssh reverse tunnel: $SshExe $($argumentList -join ' ')" -Key "starting" -LogState $logState
        $sshProcess = Start-Process -FilePath $SshExe `
            -ArgumentList $argumentList `
            -WindowStyle Hidden `
            -RedirectStandardOutput $logs.Stdout `
            -RedirectStandardError $logs.Stderr `
            -PassThru
        Start-Sleep -Seconds $RestartDelaySeconds
    }
}
function Get-ExistingTunnelMonitorProcess {
    param(
        [Parameter(Mandatory = $true)]
        [int]$RemotePort
    )

    $scriptPattern = [regex]::Escape($PSCommandPath)
    $monitorPattern = '(^|\s)"?-Monitor"?(\s|$)'
    $kindPattern = '(^|\s)"?-TunnelKind"?\s+"?' + [regex]::Escape($TunnelKind) + '"?(\s|$)'
    $missingKindPattern = '(^|\s)"?-TunnelKind"?(\s|$)'
    $portPattern = '(^|\s)"?-MonitorRemotePort"?\s+"?' + $RemotePort + '"?(\s|$)'
    Get-TunnelWin32Processes |
        Where-Object {
            $_.ProcessId -ne $PID `
                -and $_.CommandLine -match $scriptPattern `
                -and $_.CommandLine -match $monitorPattern `
                -and (($_.CommandLine -match $kindPattern) -or ($TunnelKind -eq "Workstation" -and $_.CommandLine -notmatch $missingKindPattern)) `
                -and $_.CommandLine -match $portPattern
        } |
        Select-Object -First 1
}

function Stop-ExistingTunnelMonitorProcess {
    param(
        [Parameter(Mandatory = $true)]
        [int]$RemotePort
    )

    $existing = Get-ExistingTunnelMonitorProcess -RemotePort $RemotePort
    if ($null -eq $existing) {
        return $false
    }
    Stop-Process -Id $existing.ProcessId -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 1
    return $true
}

function Stop-ReverseTunnelSshProcesses {
    param(
        [Parameter(Mandatory = $true)]
        [int]$RemotePort
    )

    $remoteForwardPattern = "(^|\s)-R\s+\S+:${RemotePort}:"
    $stoppedCount = 0
    Get-TunnelWin32Processes |
        Where-Object {
            $_.Name -eq "ssh.exe" `
                -and $_.CommandLine -match $remoteForwardPattern
        } |
        ForEach-Object {
            Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
            $stoppedCount += 1
        }
    return $stoppedCount
}

function Start-ReverseTunnelSupervisor {
    param(
        [Parameter(Mandatory = $true)]
        [int]$RemotePort
    )

    $existing = Get-ExistingTunnelMonitorProcess -RemotePort $RemotePort
    if ($null -ne $existing) {
        return $existing
    }

    $powerShellExe = Resolve-PowerShellExe
    $argumentParts = @(
        "-NoProfile",
        "-ExecutionPolicy", "Bypass",
        "-File", $PSCommandPath,
        "-Monitor",
        "-TunnelKind", $TunnelKind,
        "-MonitorRemotePort", ([string]$RemotePort),
        "-RestartDelaySeconds", ([string]$RestartDelaySeconds),
        "-OwnerPid", ([string]$OwnerPid),
        "-OwnerMarkerPath", $OwnerMarkerPath,
        "-MaxConsecutiveFailures", ([string]$MaxConsecutiveFailures),
        "-MaxRecoverySeconds", ([string]$MaxRecoverySeconds),
        "-LogRepeatSeconds", ([string]$LogRepeatSeconds)
    )
    $argumentList = ($argumentParts | ForEach-Object { ConvertTo-WindowsCommandArgument -Value $_ }) -join " "

    return Start-Process -FilePath $powerShellExe `
        -ArgumentList $argumentList `
        -WindowStyle Hidden `
        -PassThru
}

function Write-TunnelSupervisorPid {
    param(
        [Parameter(Mandatory = $true)]
        [object]$Process
    )

    $pidFile = Get-TunnelPidFile
    if ([string]::IsNullOrWhiteSpace($pidFile)) {
        return
    }

    $processId = $null
    if ($Process.PSObject.Properties.Name -contains "ProcessId") {
        $processId = $Process.ProcessId
    }
    if ($null -eq $processId) {
        $processId = $Process.Id
    }
    if ($null -eq $processId) {
        return
    }

    $pidDir = Split-Path -Parent $pidFile
    if (-not [string]::IsNullOrWhiteSpace($pidDir)) {
        New-Item -ItemType Directory -Path $pidDir -Force | Out-Null
    }
    Set-Content -LiteralPath $pidFile -Value ([string]$processId) -Encoding ASCII
}

function Wait-RemoteTunnelEndpoint {
    param(
        [Parameter(Mandatory = $true)]
        [string]$SshExe,
        [Parameter(Mandatory = $true)]
        [string]$TunnelTarget,
        [Parameter(Mandatory = $true)]
        [string]$RemoteHost,
        [Parameter(Mandatory = $true)]
        [int]$RemotePort,
        [int]$TimeoutSeconds = 20
    )

    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    while ([DateTime]::UtcNow -lt $deadline) {
        if (Test-RemoteTunnelEndpoint `
                -SshExe $SshExe `
                -TunnelTarget $TunnelTarget `
                -RemoteHost $RemoteHost `
                -RemotePort $RemotePort) {
            return $true
        }
        Start-Sleep -Seconds 1
    }
    return $false
}

function Get-TunnelStartupDetail {
    param(
        [Parameter(Mandatory = $true)]
        [int]$RemotePort
    )

    $logs = Get-TunnelLogPaths -RemotePort $RemotePort
    $logLines = @()
    foreach ($logPath in @($logs.Supervisor, $logs.Stderr, $logs.Stdout)) {
        if (Test-Path -LiteralPath $logPath -PathType Leaf) {
            $logLines += "[$logPath]"
            $logLines += Get-Content -LiteralPath $logPath -Tail 40
        }
    }
    return $logLines -join [Environment]::NewLine
}

$remoteHost = Get-RemoteBindHost
$remotePort = Get-RemoteBindPort
$targetHost = Get-TargetHost
$targetPort = Get-TargetPort
$tunnelLabel = if ($TunnelKind -eq "LocalWorker") { "LocalWorker" } else { "Workstation" }

if (-not $Check -and -not $UninstallWatchdog -and -not $NoWatchdog -and -not $Monitor -and -not (Test-TunnelOwnerConfigured)) {
    throw "$tunnelLabel reverse tunnel monitor owner is required; pass -OwnerPid or -OwnerMarkerPath for owner-bound recovery."
}

if (-not $Check -and -not $UninstallWatchdog -and -not (Test-TunnelOwnerAlive)) {
    if (-not [string]::IsNullOrWhiteSpace($env:AUTOFLUID_TUNNEL_PID_FILE)) {
        throw "$tunnelLabel reverse tunnel owner is not alive; refusing to start supervisor."
    }
    exit 0
}

if ($UninstallWatchdog) {
    $taskName = Uninstall-TunnelWatchdogTask -RemotePort $remotePort
    $launcherPath = Remove-TunnelWatchdogLauncher -RemotePort $remotePort
    $monitorStopped = Stop-ExistingTunnelMonitorProcess -RemotePort $remotePort
    $sshStopped = Stop-ReverseTunnelSshProcesses -RemotePort $remotePort
    Write-Host "Uninstalled AutoFluid $tunnelLabel reverse SSH tunnel watchdog task: $taskName"
    Write-Host "Removed AutoFluid $tunnelLabel reverse SSH tunnel watchdog launcher: $launcherPath"
    Write-Host "Stopped AutoFluid $tunnelLabel reverse SSH tunnel monitor: $monitorStopped; ssh processes: $sshStopped"
    exit 0
}

if ($InstallWatchdog) {
    try {
        $taskName = Install-TunnelWatchdogTask -RemotePort $remotePort -PowerShellExe (Resolve-PowerShellExe)
        Write-Host "Installed AutoFluid $tunnelLabel reverse SSH tunnel watchdog task: $taskName"
    }
    catch {
        Write-Warning "Failed to install AutoFluid $tunnelLabel reverse SSH tunnel watchdog task: $_"
    }
    exit 0
}

if ($TunnelKind -eq "Workstation" -and -not $NoWatchdog -and -not $Check -and -not $Monitor) {
    try {
        $taskName = Install-TunnelWatchdogTask -RemotePort $remotePort -PowerShellExe (Resolve-PowerShellExe)
        Write-Host "AutoFluid $tunnelLabel reverse SSH tunnel watchdog task is ready: $taskName"
    }
    catch {
        Write-Warning "Failed to install AutoFluid $tunnelLabel reverse SSH tunnel watchdog task: $_"
    }
}

$sshExe = Resolve-SshExe
$tunnelTarget = Get-TunnelSshTarget

Write-Host "$tunnelLabel tunnel SSH target: $tunnelTarget"
Write-Host "$tunnelLabel tunnel remote endpoint: ${remoteHost}:${remotePort}"
Write-Host "$tunnelLabel tunnel target endpoint: ${targetHost}:${targetPort}"

if ($Monitor) {
    Start-ReverseTunnelMonitor `
        -SshExe $sshExe `
        -TunnelTarget $tunnelTarget `
        -RemoteHost $remoteHost `
        -RemotePort $remotePort `
        -TargetHost $targetHost `
        -TargetPort $targetPort
    exit 0
}

if (-not (Test-TcpEndpoint -HostName $targetHost -Port $targetPort)) {
    throw "$tunnelLabel SSH target is not reachable from this machine: ${targetHost}:${targetPort}"
}

if (Test-RemoteTunnelEndpoint `
    -SshExe $sshExe `
    -TunnelTarget $tunnelTarget `
    -RemoteHost $remoteHost `
    -RemotePort $remotePort) {
    $existing = Get-ExistingTunnelMonitorProcess -RemotePort $remotePort
    if ($null -ne $existing) {
        Write-Host "AutoFluid $tunnelLabel reverse SSH tunnel is already reachable; reuse the existing tunnel."
        Write-TunnelSupervisorPid -Process $existing
        exit 0
    }
    Write-Host "AutoFluid $tunnelLabel reverse SSH tunnel endpoint is reachable but no supervisor monitor was found; starting a new monitor."
    $process = Start-ReverseTunnelSupervisor -RemotePort $remotePort
    Write-TunnelSupervisorPid -Process $process
    exit 0
}

if ($Check) {
    throw "AutoFluid $tunnelLabel reverse SSH tunnel is not reachable on ${remoteHost}:${remotePort}."
}

if (Stop-ExistingTunnelMonitorProcess -RemotePort $remotePort) {
    Write-Host "Existing $tunnelLabel supervisor monitor was stopped because the endpoint is not reachable."
}

$process = Start-ReverseTunnelSupervisor -RemotePort $remotePort
Write-TunnelSupervisorPid -Process $process

if (-not (Wait-RemoteTunnelEndpoint `
    -SshExe $sshExe `
    -TunnelTarget $tunnelTarget `
    -RemoteHost $remoteHost `
    -RemotePort $remotePort)) {
    $detail = Get-TunnelStartupDetail -RemotePort $remotePort
    throw "AutoFluid $tunnelLabel reverse SSH tunnel supervisor started but did not become reachable. $detail"
}

$processId = $process.ProcessId
if ($null -eq $processId) {
    $processId = $process.Id
}
Write-Host "Started AutoFluid $tunnelLabel reverse SSH tunnel supervisor with PID $processId."
