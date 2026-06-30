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
    [string]$TunnelIdentityFile = "",
    [string]$InstallDir = "C:\ProgramData\AutoFluid\tunnel",
    [int]$RestartDelaySeconds = 5,
    [int]$ProbeIntervalSeconds = 5,
    [int]$RemoteProbeFailureThreshold = 3,
    [int]$LogRepeatSeconds = 60,
    [int]$SshCommandTimeoutSeconds = 15
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

function Get-OwnedTunnelRunKeyPath {
    return "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
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
    $hostBytes = (($RemoteBindHost.ToCharArray() | ForEach-Object { [int][char]$_ }) -join ",")
    $remoteCommand = "python3 -c 'import socket,sys; s=socket.socket(); s.settimeout(3); s.connect((bytes([$hostBytes]).decode(),$RemoteBindPort)); data=s.recv(4); s.close(); sys.exit(0 if data==bytes([83,83,72,45]) else 1)'"
    $identityArgs = @()
    if (-not [string]::IsNullOrWhiteSpace($TunnelIdentityFile)) {
        $identityArgs = @("-i", $TunnelIdentityFile)
    }
    try {
        & $SshExe -o BatchMode=yes -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new @identityArgs $TunnelTarget $remoteCommand 1>$null 2>$null
        return $LASTEXITCODE -eq 0
    }
    catch {
        return $false
    }
}

function Invoke-OwnedTunnelSshCommand {
    param(
        [string]$SshExe,
        [string[]]$Arguments,
        [int]$TimeoutSeconds = $SshCommandTimeoutSeconds
    )
    $stdoutPath = [System.IO.Path]::GetTempFileName()
    $stderrPath = [System.IO.Path]::GetTempFileName()
    $process = $null
    try {
        $process = Start-Process -FilePath $SshExe `
            -ArgumentList $Arguments `
            -WindowStyle Hidden `
            -RedirectStandardOutput $stdoutPath `
            -RedirectStandardError $stderrPath `
            -PassThru
        if (-not $process.WaitForExit([Math]::Max(1, $TimeoutSeconds) * 1000)) {
            Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
            return @{
                ExitCode = 124
                TimedOut = $true
                Stdout = (Get-Content -LiteralPath $stdoutPath -Raw -ErrorAction SilentlyContinue)
                Stderr = (Get-Content -LiteralPath $stderrPath -Raw -ErrorAction SilentlyContinue)
            }
        }
        return @{
            ExitCode = $process.ExitCode
            TimedOut = $false
            Stdout = (Get-Content -LiteralPath $stdoutPath -Raw -ErrorAction SilentlyContinue)
            Stderr = (Get-Content -LiteralPath $stderrPath -Raw -ErrorAction SilentlyContinue)
        }
    }
    finally {
        Remove-Item -LiteralPath $stdoutPath, $stderrPath -Force -ErrorAction SilentlyContinue
    }
}

function Clear-StaleRemoteForward {
    param(
        [string]$SshExe
    )
    $remoteCommand = "bash -lc 'fuser -k ${RemoteBindPort}/tcp >/dev/null 2>&1 || true'"
    $identityArgs = @()
    if (-not [string]::IsNullOrWhiteSpace($TunnelIdentityFile)) {
        $identityArgs = @("-i", $TunnelIdentityFile)
    }
    try {
        $arguments = @(
            "-o", "BatchMode=yes",
            "-o", "ConnectTimeout=10",
            "-o", "StrictHostKeyChecking=accept-new"
        ) + $identityArgs + @($TunnelTarget, $remoteCommand)
        $result = Invoke-OwnedTunnelSshCommand `
            -SshExe $SshExe `
            -Arguments $arguments `
            -TimeoutSeconds $SshCommandTimeoutSeconds
        if ($result.TimedOut) {
            Write-OwnedTunnelLog -Message "Timed out clearing stale remote forward on ${RemoteBindPort}; continuing with tunnel restart."
            return $false
        }
        return $result.ExitCode -eq 0
    }
    catch {
        Write-OwnedTunnelLog -Message "Failed to clear stale remote forward on ${RemoteBindPort}: $($_.Exception.Message)"
        return $false
    }
}

function Wait-RemoteTunnelEndpoint {
    param(
        [string]$SshExe,
        [System.Diagnostics.Process]$SshProcess,
        [int]$TimeoutSeconds
    )
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        if ($null -ne $SshProcess -and $SshProcess.HasExited) {
            return "ssh-exited-$($SshProcess.ExitCode)"
        }
        if (Test-RemoteTunnelEndpoint -SshExe $SshExe) {
            return "ready"
        }
        Start-Sleep -Seconds $ProbeIntervalSeconds
    }
    return "startup-probe-timeout"
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
    $args = @(
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
    if (-not [string]::IsNullOrWhiteSpace($TunnelIdentityFile)) {
        $args = @("-i", $TunnelIdentityFile) + $args
    }
    return $args
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
                foreach ($process in @(Get-OwnedTunnelProcesses -Kind "Ssh")) {
                    Stop-Process -Id ([int]$process.ProcessId) -Force -ErrorAction SilentlyContinue
                }
                $delay = Register-TunnelFailure -Reason "local-target-unreachable" -FailureState $failureState -LogState $logState
                Start-Sleep -Seconds $delay
                continue
            }

            $ownedSshProcesses = @(Get-OwnedTunnelProcesses -Kind "Ssh")
            if ($ownedSshProcesses.Count -gt 0 -and (Test-RemoteTunnelEndpoint -SshExe $sshExe)) {
                Reset-TunnelFailureBudget -FailureState $failureState
                Start-Sleep -Seconds $ProbeIntervalSeconds
                continue
            }

            if ($null -ne $sshProcess -and -not $sshProcess.HasExited) {
                $delay = Register-TunnelFailure -Reason "remote-probe-failed" -FailureState $failureState -LogState $logState
                if ([int]$failureState.Count -lt $RemoteProbeFailureThreshold) {
                    Start-Sleep -Seconds $ProbeIntervalSeconds
                    continue
                }
                Stop-Process -Id $sshProcess.Id -Force -ErrorAction SilentlyContinue
                $sshProcess = $null
                Start-Sleep -Seconds $delay
                continue
            }
            if ($ownedSshProcesses.Count -gt 0) {
                $delay = Register-TunnelFailure -Reason "remote-probe-failed" -FailureState $failureState -LogState $logState
                if ([int]$failureState.Count -lt $RemoteProbeFailureThreshold) {
                    Start-Sleep -Seconds $ProbeIntervalSeconds
                    continue
                }
                foreach ($process in $ownedSshProcesses) {
                    Stop-Process -Id ([int]$process.ProcessId) -Force -ErrorAction SilentlyContinue
                }
                Clear-StaleRemoteForward -SshExe $sshExe | Out-Null
                Start-Sleep -Seconds $delay
                continue
            }

            Clear-StaleRemoteForward -SshExe $sshExe | Out-Null
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
            $startupStatus = Wait-RemoteTunnelEndpoint `
                -SshExe $sshExe `
                -SshProcess $sshProcess `
                -TimeoutSeconds ([Math]::Max(30, $RestartDelaySeconds * 6))
            if ($startupStatus -eq "ready") {
                Reset-TunnelFailureBudget -FailureState $failureState
                continue
            }
            if ($null -ne $sshProcess -and -not $sshProcess.HasExited) {
                Stop-Process -Id $sshProcess.Id -Force -ErrorAction SilentlyContinue
            }
            $sshProcess = $null
            $delay = Register-TunnelFailure -Reason $startupStatus -FailureState $failureState -LogState $logState
            Start-Sleep -Seconds $delay
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
        "-TunnelIdentityFile", $TunnelIdentityFile,
        "-InstallDir", $InstallDir,
        "-RestartDelaySeconds", ([string]$RestartDelaySeconds),
        "-ProbeIntervalSeconds", ([string]$ProbeIntervalSeconds),
        "-RemoteProbeFailureThreshold", ([string]$RemoteProbeFailureThreshold),
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
    try {
        Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $triggers -Settings $settings -RunLevel Highest -Force -ErrorAction Stop | Out-Null
    }
    catch {
        Write-OwnedTunnelLog -Message "Register with RunLevel Highest failed; retrying as current user task. $($_.Exception.Message)"
        try {
            Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $triggers -Settings $settings -Force -ErrorAction Stop | Out-Null
        }
        catch {
            Write-OwnedTunnelLog -Message "Register current user task failed; installing HKCU Run fallback. $($_.Exception.Message)"
            $runKey = Get-OwnedTunnelRunKeyPath
            New-Item -Path $runKey -Force | Out-Null
            Set-ItemProperty -Path $runKey -Name $taskName -Value "$powerShellExe $argumentText"
            Start-Process -FilePath $powerShellExe -ArgumentList $argumentText -WindowStyle Hidden
            Write-Output "Installed workstation-owned AutoFluid tunnel run key: $taskName"
            return
        }
    }
    Start-ScheduledTask -TaskName $taskName
    Write-Output "Installed workstation-owned AutoFluid tunnel task: $taskName"
}

function Uninstall-OwnedTunnelTask {
    $taskName = Get-OwnedTunnelTaskName
    Disable-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue | Out-Null
    Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
    Remove-ItemProperty -Path (Get-OwnedTunnelRunKeyPath) -Name $taskName -ErrorAction SilentlyContinue
    Stop-OwnedTunnelProcesses
    Start-Sleep -Seconds 1
    Stop-OwnedTunnelProcesses
    Write-Output "Uninstalled workstation-owned AutoFluid tunnel task: $taskName"
}

function Get-OwnedTunnelStatus {
    $taskName = Get-OwnedTunnelTaskName
    $task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    $runValue = (Get-ItemProperty -Path (Get-OwnedTunnelRunKeyPath) -Name $taskName -ErrorAction SilentlyContinue).$taskName
    $sshExe = Resolve-SshExe
    $statusObject = [ordered]@{
        workstation_id = $WorkstationId
        task_name = $taskName
        task_exists = $null -ne $task
        task_state = if ($null -eq $task) { "missing" } else { [string]$task.State }
        registry_run_exists = -not [string]::IsNullOrWhiteSpace([string]$runValue)
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
