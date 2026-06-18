param(
    [switch]$Check,
    [switch]$Monitor,
    [switch]$InstallWatchdog,
    [switch]$UninstallWatchdog,
    [switch]$NoWatchdog,
    [ValidateSet("Workstation", "LocalWorker")]
    [string]$TunnelKind = "Workstation",
    [int]$RestartDelaySeconds = 5,
    [int]$MonitorRemotePort = 0
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

function Install-TunnelWatchdogTask {
    param(
        [Parameter(Mandatory = $true)]
        [int]$RemotePort,
        [Parameter(Mandatory = $true)]
        [string]$PowerShellExe
    )

    $taskName = Get-TunnelWatchdogTaskName -RemotePort $RemotePort
    $scriptPath = $PSCommandPath
    $arguments = @(
        "-NoProfile",
        "-ExecutionPolicy", "Bypass",
        "-File", "`"$scriptPath`"",
        "-TunnelKind", $TunnelKind,
        "-NoWatchdog",
        "-RestartDelaySeconds", $RestartDelaySeconds
    ) -join " "

    $existing = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    if ($null -ne $existing) {
        return $taskName
    }

    $action = New-ScheduledTaskAction -Execute $PowerShellExe -Argument $arguments
    $trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
        -RepetitionInterval (New-TimeSpan -Minutes 1) `
        -RepetitionDuration ([TimeSpan]::MaxValue)
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
    return @{
        Stdout = Join-Path ([System.IO.Path]::GetTempPath()) "autofluid-${tunnelName}-tunnel-${RemotePort}.out.log"
        Stderr = Join-Path ([System.IO.Path]::GetTempPath()) "autofluid-${tunnelName}-tunnel-${RemotePort}.err.log"
        Supervisor = Join-Path ([System.IO.Path]::GetTempPath()) "autofluid-${tunnelName}-tunnel-${RemotePort}.supervisor.log"
    }
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

    while ($true) {
        if (Test-RemoteTunnelEndpoint `
                -SshExe $SshExe `
                -TunnelTarget $TunnelTarget `
                -RemoteHost $RemoteHost `
                -RemotePort $RemotePort) {
            Start-Sleep -Seconds 5
            continue
        }

        if (-not (Test-TcpEndpoint -HostName $TargetHost -Port $TargetPort)) {
            Write-TunnelSupervisorLog -LogPath $logs.Supervisor -Message "Target endpoint is unreachable: ${TargetHost}:${TargetPort}; retrying in ${RestartDelaySeconds}s."
            Start-Sleep -Seconds $RestartDelaySeconds
            continue
        }

        $argumentList = Get-ReverseTunnelArguments `
            -TunnelTarget $TunnelTarget `
            -RemoteHost $RemoteHost `
            -RemotePort $RemotePort `
            -TargetHost $TargetHost `
            -TargetPort $TargetPort

        Write-TunnelSupervisorLog -LogPath $logs.Supervisor -Message "Starting ssh reverse tunnel: $SshExe $($argumentList -join ' ')"
        $sshProcess = Start-Process -FilePath $SshExe `
            -ArgumentList $argumentList `
            -WindowStyle Hidden `
            -RedirectStandardOutput $logs.Stdout `
            -RedirectStandardError $logs.Stderr `
            -PassThru

        while (-not $sshProcess.HasExited) {
            Start-Sleep -Seconds 5
        }

        $exitCode = $sshProcess.ExitCode
        Write-TunnelSupervisorLog -LogPath $logs.Supervisor -Message "ssh reverse tunnel exited with code ${exitCode}; restarting in ${RestartDelaySeconds}s."
        Start-Sleep -Seconds $RestartDelaySeconds
    }
}

function Get-ExistingTunnelMonitorProcess {
    param(
        [Parameter(Mandatory = $true)]
        [int]$RemotePort
    )

    $scriptPattern = [regex]::Escape($PSCommandPath)
    Get-CimInstance Win32_Process |
        Where-Object {
            $_.ProcessId -ne $PID `
                -and $_.CommandLine -match $scriptPattern `
                -and $_.CommandLine -match '(^|\s)-Monitor(\s|$)' `
                -and (($_.CommandLine -match "(^|\s)-TunnelKind\s+$TunnelKind(\s|$)") -or ($TunnelKind -eq "Workstation" -and $_.CommandLine -notmatch '(^|\s)-TunnelKind(\s|$)')) `
                -and $_.CommandLine -match "(^|\s)-MonitorRemotePort\s+$RemotePort(\s|$)"
        } |
        Select-Object -First 1
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
    $scriptPath = '"' + $PSCommandPath.Replace('"', '\"') + '"'
    $argumentList = @(
        "-NoProfile",
        "-ExecutionPolicy", "Bypass",
        "-File", $scriptPath,
        "-Monitor",
        "-TunnelKind", $TunnelKind,
        "-MonitorRemotePort", $RemotePort,
        "-RestartDelaySeconds", $RestartDelaySeconds
    )

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

    $pidFile = $env:AUTOFLUID_TUNNEL_PID_FILE
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

if ($UninstallWatchdog) {
    $taskName = Uninstall-TunnelWatchdogTask -RemotePort $remotePort
    Write-Host "Uninstalled AutoFluid $tunnelLabel reverse SSH tunnel watchdog task: $taskName"
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

if (-not $Check -and -not $NoWatchdog) {
    try {
        $taskName = Install-TunnelWatchdogTask -RemotePort $remotePort -PowerShellExe (Resolve-PowerShellExe)
        Write-Host "AutoFluid $tunnelLabel reverse SSH tunnel watchdog task is ready: $taskName"
    }
    catch {
        Write-Warning "Failed to install AutoFluid $tunnelLabel reverse SSH tunnel watchdog task: $_"
    }
}

if (-not (Test-TcpEndpoint -HostName $targetHost -Port $targetPort)) {
    throw "$tunnelLabel SSH target is not reachable from this machine: ${targetHost}:${targetPort}"
}

if (Test-RemoteTunnelEndpoint `
    -SshExe $sshExe `
    -TunnelTarget $tunnelTarget `
    -RemoteHost $remoteHost `
    -RemotePort $remotePort) {
    Write-Host "AutoFluid $tunnelLabel reverse SSH tunnel is already reachable; reuse the existing tunnel."
    $existing = Get-ExistingTunnelMonitorProcess -RemotePort $remotePort
    if ($null -ne $existing) {
        Write-TunnelSupervisorPid -Process $existing
    }
    exit 0
}

if ($Check) {
    throw "AutoFluid $tunnelLabel reverse SSH tunnel is not reachable on ${remoteHost}:${remotePort}."
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
