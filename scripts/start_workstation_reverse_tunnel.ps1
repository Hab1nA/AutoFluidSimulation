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
    if (-not [string]::IsNullOrWhiteSpace($env:AUTOFLUID_SSH_REACHABLE_HOST)) {
        return $env:AUTOFLUID_SSH_REACHABLE_HOST
    }
    return "127.0.0.1"
}

function Get-RemoteBindPort {
    return Get-EnvInt -Name "AUTOFLUID_SSH_REACHABLE_PORT" -DefaultValue 2222
}

function Get-TargetHost {
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
    & $SshExe -o BatchMode=yes -o ConnectTimeout=10 $TunnelTarget $remoteCommand | Out-Null
    return $LASTEXITCODE -eq 0
}

function Start-ReverseTunnel {
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

    $forwardSpec = "${RemoteHost}:${RemotePort}:${TargetHost}:${TargetPort}"
    $stdoutLogPath = Join-Path ([System.IO.Path]::GetTempPath()) "autofluid-workstation-tunnel-${RemotePort}.out.log"
    $stderrLogPath = Join-Path ([System.IO.Path]::GetTempPath()) "autofluid-workstation-tunnel-${RemotePort}.err.log"
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
        "-R", $forwardSpec,
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
        throw "Failed to start AutoFluid workstation reverse SSH tunnel. $detail"
    }

    return $process
}

$sshExe = Resolve-SshExe
$tunnelTarget = Get-TunnelSshTarget
$remoteHost = Get-RemoteBindHost
$remotePort = Get-RemoteBindPort
$targetHost = Get-TargetHost
$targetPort = Get-TargetPort

Write-Host "Workstation tunnel SSH target: $tunnelTarget"
Write-Host "Workstation tunnel remote endpoint: ${remoteHost}:${remotePort}"
Write-Host "Workstation tunnel target endpoint: ${targetHost}:${targetPort}"

if (-not (Test-TcpEndpoint -HostName $targetHost -Port $targetPort)) {
    throw "Workstation SSH target is not reachable from this machine: ${targetHost}:${targetPort}"
}

if (Test-RemoteTunnelEndpoint `
    -SshExe $sshExe `
    -TunnelTarget $tunnelTarget `
    -RemoteHost $remoteHost `
    -RemotePort $remotePort) {
    Write-Host "AutoFluid workstation reverse SSH tunnel is already reachable; reuse the existing tunnel."
    exit 0
}

if ($Check) {
    throw "AutoFluid workstation reverse SSH tunnel is not reachable on ${remoteHost}:${remotePort}."
}

$process = Start-ReverseTunnel `
    -SshExe $sshExe `
    -TunnelTarget $tunnelTarget `
    -RemoteHost $remoteHost `
    -RemotePort $remotePort `
    -TargetHost $targetHost `
    -TargetPort $targetPort

if (-not (Test-RemoteTunnelEndpoint `
    -SshExe $sshExe `
    -TunnelTarget $tunnelTarget `
    -RemoteHost $remoteHost `
    -RemotePort $remotePort)) {
    throw "AutoFluid workstation reverse SSH tunnel started but did not become reachable."
}

Write-Host "Started AutoFluid workstation reverse SSH tunnel with $sshExe (PID $($process.Id))."
