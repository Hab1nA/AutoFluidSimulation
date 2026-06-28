param(
    [switch]$Install,
    [switch]$Uninstall,
    [switch]$Status,
    [switch]$Monitor,
    [string]$WorkstationId = "WS-A",
    [string]$RemoteBindHost = "127.0.0.1",
    [int]$RemoteBindPort = 2222,
    [string]$TargetHost = "127.0.0.1",
    [int]$TargetPort = 22,
    [string]$TunnelTarget = "ocar",
    [string]$InstallDir = "C:\ProgramData\AutoFluid\tunnel",
    [int]$RestartDelaySeconds = 5,
    [int]$ProbeIntervalSeconds = 5,
    [int]$LogRepeatSeconds = 60
)

$ErrorActionPreference = "Stop"

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

function Get-OwnedTunnelTaskName {
    return "AutoFluidWorkstationTunnel-$WorkstationId-$RemoteBindPort"
}

function Get-OwnedTunnelScriptPath {
    return Join-Path $InstallDir "start_workstation_owned_reverse_tunnel.ps1"
}

function Get-OwnedTunnelLogPaths {
    $logDir = Join-Path $InstallDir "logs"
    New-Item -ItemType Directory -Path $logDir -Force | Out-Null
    $prefix = "workstation-$WorkstationId-$RemoteBindPort"
    return @{
        Supervisor = Join-Path $logDir "$prefix-supervisor.log"
        Stdout = Join-Path $logDir "$prefix-ssh.stdout.log"
        Stderr = Join-Path $logDir "$prefix-ssh.stderr.log"
    }
}

function Write-OwnedTunnelLog {
    param([string]$Message)
    $logs = Get-OwnedTunnelLogPaths
    if ((Test-Path -LiteralPath $logs.Supervisor -PathType Leaf) -and
        ((Get-Item -LiteralPath $logs.Supervisor).Length -gt 5242880)) {
        Move-Item -LiteralPath $logs.Supervisor -Destination "$($logs.Supervisor).1" -Force
    }
    $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Add-Content -LiteralPath $logs.Supervisor -Value "[$timestamp] $Message" -Encoding UTF8
}

function Write-RateLimitedOwnedTunnelLog {
    param(
        [string]$Message,
        [string]$Key,
        [hashtable]$LogState,
        [switch]$Force
    )
    $now = Get-Date
    if ($Force -or $LogState.LastKey -ne $Key -or $null -eq $LogState.LastAt -or ($now - $LogState.LastAt).TotalSeconds -ge $LogRepeatSeconds) {
        Write-OwnedTunnelLog -Message $Message
        $LogState.LastKey = $Key
        $LogState.LastAt = $now
    }
}

function Register-TunnelFailure {
    param(
        [string]$Reason,
        [hashtable]$FailureState,
        [hashtable]$LogState
    )
    $FailureState.Count = [int]$FailureState.Count + 1
    $delay = [Math]::Min(60, [Math]::Max($RestartDelaySeconds, $RestartDelaySeconds * [Math]::Min($FailureState.Count, 6)))
    Write-RateLimitedOwnedTunnelLog `
        -Message "Workstation reverse tunnel is not reachable ($Reason); retrying in ${delay}s." `
        -Key $Reason `
        -LogState $LogState
    return $delay
}

function Reset-TunnelFailureBudget {
    param([hashtable]$FailureState)
    $FailureState.Count = 0
}

function Test-TcpEndpoint {
    param(
        [string]$HostName,
        [int]$Port
    )
    try {
        $client = [System.Net.Sockets.TcpClient]::new()
        $async = $client.BeginConnect($HostName, $Port, $null, $null)
        if (-not $async.AsyncWaitHandle.WaitOne([TimeSpan]::FromSeconds(2))) {
            $client.Close()
            return $false
        }
        $client.EndConnect($async)
        $client.Close()
        return $true
    }
    catch {
        return $false
    }
}

function Test-RemoteTunnelEndpoint {
    param(
        [string]$SshExe
    )
    $remoteCommand = "python3 -c `"import socket; s=socket.socket(); s.settimeout(2); s.connect(('127.0.0.1',$RemoteBindPort)); s.close()`""
    try {
        & $SshExe -o BatchMode=yes -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new $TunnelTarget $remoteCommand 2>&1 | Out-Null
        return $LASTEXITCODE -eq 0
    }
    catch {
        return $false
    }
}

function Get-OwnedTunnelProcesses {
    param([string]$Kind)
    $portPattern = [regex]::Escape("${RemoteBindHost}:${RemoteBindPort}:${TargetHost}:${TargetPort}")
    $scriptPattern = [regex]::Escape("start_workstation_owned_reverse_tunnel.ps1")
    $taskPattern = [regex]::Escape((Get-OwnedTunnelTaskName))
    Get-CimInstance -ClassName Win32_Process -ErrorAction SilentlyContinue | Where-Object {
        $commandLine = [string]$_.CommandLine
        if ($Kind -eq "Monitor") {
            return $commandLine -match $scriptPattern -and
                $commandLine -match '-Monitor' -and
                $commandLine -match [regex]::Escape([string]$RemoteBindPort)
        }
        if ($Kind -eq "Ssh") {
            return $_.Name -ieq "ssh.exe" -and
                $commandLine -match '-R' -and
                $commandLine -match $portPattern
        }
        return $commandLine -match $taskPattern
    }
}

function Stop-OwnedTunnelProcesses {
    foreach ($kind in @("Monitor", "Ssh")) {
        foreach ($process in @(Get-OwnedTunnelProcesses -Kind $kind)) {
            if ([int]$process.ProcessId -eq $PID) {
                continue
            }
            Stop-Process -Id ([int]$process.ProcessId) -Force -ErrorAction SilentlyContinue
        }
    }
}

function Get-ReverseTunnelArguments {
    $forwardSpec = "${RemoteBindHost}:${RemoteBindPort}:${TargetHost}:${TargetPort}"
    return @(
        "-o", "BatchMode=yes",
        "-o", "ExitOnForwardFailure=yes",
        "-o", "ServerAliveInterval=5",
        "-o", "ServerAliveCountMax=3",
        "-o", "TCPKeepAlive=yes",
        "-o", "StrictHostKeyChecking=accept-new",
        "-N",
        "-R", $forwardSpec,
        $TunnelTarget
    )
}

function Start-OwnedTunnelMonitor {
    $mutexName = "Global\AutoFluidWorkstationTunnel-$WorkstationId-$RemoteBindPort"
    $createdNew = $false
    $mutex = [System.Threading.Mutex]::new($true, $mutexName, [ref]$createdNew)
    if (-not $createdNew) {
        Write-OwnedTunnelLog -Message "Another monitor already owns $mutexName; exiting."
        return
    }

    $sshProcess = $null
    $failureState = @{ Count = 0 }
    $logState = @{ LastKey = ""; LastAt = $null }
    $sshExe = Resolve-SshExe
    $logs = Get-OwnedTunnelLogPaths
    Write-OwnedTunnelLog -Message "Monitor starting for ${RemoteBindHost}:${RemoteBindPort} -> ${TargetHost}:${TargetPort} via ${TunnelTarget}."
    try {
        while ($true) {
            if ($null -ne $sshProcess -and $sshProcess.HasExited) {
                $exitCode = $sshProcess.ExitCode
                $sshProcess = $null
                $delay = Register-TunnelFailure -Reason "ssh-exited-$exitCode" -FailureState $failureState -LogState $logState
                Start-Sleep -Seconds $delay
                continue
            }

            if (-not (Test-TcpEndpoint -HostName $TargetHost -Port $TargetPort)) {
                if ($null -ne $sshProcess -and -not $sshProcess.HasExited) {
                    Stop-Process -Id $sshProcess.Id -Force -ErrorAction SilentlyContinue
                    $sshProcess = $null
                }
                $delay = Register-TunnelFailure -Reason "local-target-unreachable" -FailureState $failureState -LogState $logState
                Start-Sleep -Seconds $delay
                continue
            }

            if (Test-RemoteTunnelEndpoint -SshExe $sshExe) {
                Reset-TunnelFailureBudget -FailureState $failureState
                Start-Sleep -Seconds $ProbeIntervalSeconds
                continue
            }

            if ($null -ne $sshProcess -and -not $sshProcess.HasExited) {
                Stop-Process -Id $sshProcess.Id -Force -ErrorAction SilentlyContinue
                $sshProcess = $null
                $delay = Register-TunnelFailure -Reason "remote-probe-failed" -FailureState $failureState -LogState $logState
                Start-Sleep -Seconds $delay
                continue
            }

            $argumentList = Get-ReverseTunnelArguments
            Write-RateLimitedOwnedTunnelLog `
                -Message "Starting ssh reverse tunnel: $sshExe $($argumentList -join ' ')" `
                -Key "starting" `
                -LogState $logState
            $sshProcess = Start-Process -FilePath $sshExe `
                -ArgumentList $argumentList `
                -WindowStyle Hidden `
                -RedirectStandardOutput $logs.Stdout `
                -RedirectStandardError $logs.Stderr `
                -PassThru
            Start-Sleep -Seconds $RestartDelaySeconds
        }
    }
    finally {
        if ($null -ne $sshProcess -and -not $sshProcess.HasExited) {
            Stop-Process -Id $sshProcess.Id -Force -ErrorAction SilentlyContinue
        }
        $mutex.ReleaseMutex()
        $mutex.Dispose()
    }
}

function Install-OwnedTunnelTask {
    New-Item -ItemType Directory -Path $InstallDir -Force | Out-Null
    $scriptPath = Get-OwnedTunnelScriptPath
    if ((Resolve-Path -LiteralPath $PSCommandPath).Path -ne (Resolve-Path -LiteralPath $scriptPath -ErrorAction SilentlyContinue).Path) {
        Copy-Item -LiteralPath $PSCommandPath -Destination $scriptPath -Force
    }

    $taskName = Get-OwnedTunnelTaskName
    $powerShellExe = Resolve-PowerShellExe
    Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    Stop-OwnedTunnelProcesses
    Start-Sleep -Seconds 1
    Stop-OwnedTunnelProcesses
    $args = @(
        "-NoProfile",
        "-ExecutionPolicy", "Bypass",
        "-File", $scriptPath,
        "-Monitor",
        "-WorkstationId", $WorkstationId,
        "-RemoteBindHost", $RemoteBindHost,
        "-RemoteBindPort", ([string]$RemoteBindPort),
        "-TargetHost", $TargetHost,
        "-TargetPort", ([string]$TargetPort),
        "-TunnelTarget", $TunnelTarget,
        "-InstallDir", $InstallDir,
        "-RestartDelaySeconds", ([string]$RestartDelaySeconds),
        "-ProbeIntervalSeconds", ([string]$ProbeIntervalSeconds),
        "-LogRepeatSeconds", ([string]$LogRepeatSeconds)
    )
    $argumentText = ($args | ForEach-Object {
        if ($_ -match '\s|"' ) { '"' + $_.Replace('"', '\"') + '"' } else { $_ }
    }) -join " "
    $action = New-ScheduledTaskAction -Execute $powerShellExe -Argument $argumentText
    $startupTrigger = New-ScheduledTaskTrigger -AtStartup
    $logonTrigger = New-ScheduledTaskTrigger -AtLogOn
    $retryTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
        -RepetitionInterval (New-TimeSpan -Minutes 1) `
        -RepetitionDuration (New-TimeSpan -Days 3650)
    $triggers = @($startupTrigger, $logonTrigger, $retryTrigger)
    $settings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -StartWhenAvailable `
        -MultipleInstances IgnoreNew `
        -RestartCount 10 `
        -RestartInterval (New-TimeSpan -Minutes 1)
    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $triggers -Settings $settings -RunLevel Highest -Force | Out-Null
    Start-ScheduledTask -TaskName $taskName
    Write-Output "Installed workstation-owned AutoFluid tunnel task: $taskName"
}

function Uninstall-OwnedTunnelTask {
    $taskName = Get-OwnedTunnelTaskName
    Disable-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue | Out-Null
    Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
    Stop-OwnedTunnelProcesses
    Start-Sleep -Seconds 1
    Stop-OwnedTunnelProcesses
    Write-Output "Uninstalled workstation-owned AutoFluid tunnel task: $taskName"
}

function Get-OwnedTunnelStatus {
    $taskName = Get-OwnedTunnelTaskName
    $task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    $sshExe = Resolve-SshExe
    $statusObject = [ordered]@{
        workstation_id = $WorkstationId
        task_name = $taskName
        task_exists = $null -ne $task
        task_state = if ($null -eq $task) { "missing" } else { [string]$task.State }
        monitor_processes = @((Get-OwnedTunnelProcesses -Kind "Monitor")).Count
        ssh_processes = @((Get-OwnedTunnelProcesses -Kind "Ssh")).Count
        local_target_ok = Test-TcpEndpoint -HostName $TargetHost -Port $TargetPort
        remote_tunnel_ok = Test-RemoteTunnelEndpoint -SshExe $sshExe
    }
    $statusObject | ConvertTo-Json -Compress
}

if ($Uninstall) {
    Uninstall-OwnedTunnelTask
    exit 0
}

if ($Install) {
    Install-OwnedTunnelTask
    exit 0
}

if ($Status) {
    Get-OwnedTunnelStatus
    exit 0
}

if ($Monitor) {
    Start-OwnedTunnelMonitor
    exit 0
}

Install-OwnedTunnelTask
