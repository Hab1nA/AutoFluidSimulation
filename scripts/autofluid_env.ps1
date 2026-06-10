$ErrorActionPreference = "Stop"

function Import-AutoFluidEnv {
    param(
        [Parameter(Mandatory = $true)]
        [string]$ProjectDir
    )

    $envPath = Join-Path $ProjectDir ".env"
    if (Test-Path -LiteralPath $envPath -PathType Leaf) {
        foreach ($rawLine in Get-Content -LiteralPath $envPath) {
            $line = $rawLine.Trim()
            if ($line.Length -eq 0 -or $line.StartsWith("#")) {
                continue
            }
            if ($line.StartsWith("export ")) {
                $line = $line.Substring(7).TrimStart()
            }

            $separator = $line.IndexOf("=")
            if ($separator -le 0) {
                continue
            }

            $key = $line.Substring(0, $separator).Trim()
            if ($key -notmatch "^[A-Za-z_][A-Za-z0-9_]*$") {
                continue
            }

            $value = $line.Substring($separator + 1).Trim()
            if ($value.Length -ge 2) {
                $first = $value.Substring(0, 1)
                $last = $value.Substring($value.Length - 1, 1)
                if (($first -eq '"' -and $last -eq '"') -or ($first -eq "'" -and $last -eq "'")) {
                    $value = $value.Substring(1, $value.Length - 2)
                }
            }

            [Environment]::SetEnvironmentVariable($key, $value, "Process")
        }
    }

    if ([string]::IsNullOrWhiteSpace($env:AUTOFLUID_SERVER_MODE)) {
        $env:AUTOFLUID_SERVER_MODE = "server"
    }

    Sync-AutoFluidEndpointEnv
}

function Sync-AutoFluidEndpointEnv {
    if ([string]::IsNullOrWhiteSpace($env:AUTOFLUID_IPC_HOST) -and -not [string]::IsNullOrWhiteSpace($env:AUTOFLUID_SERVER_HOST)) {
        $env:AUTOFLUID_IPC_HOST = $env:AUTOFLUID_SERVER_HOST
    }
    if ([string]::IsNullOrWhiteSpace($env:AUTOFLUID_SERVER_HOST) -and -not [string]::IsNullOrWhiteSpace($env:AUTOFLUID_IPC_HOST)) {
        $env:AUTOFLUID_SERVER_HOST = $env:AUTOFLUID_IPC_HOST
    }
}

function Get-AutoFluidServerHost {
    if (-not [string]::IsNullOrWhiteSpace($env:AUTOFLUID_SERVER_HOST)) {
        return $env:AUTOFLUID_SERVER_HOST
    }
    if (-not [string]::IsNullOrWhiteSpace($env:AUTOFLUID_IPC_HOST)) {
        return $env:AUTOFLUID_IPC_HOST
    }
    return "127.0.0.1"
}

function Get-AutoFluidServerPort {
    if ([string]::IsNullOrWhiteSpace($env:AUTOFLUID_IPC_PORT)) {
        return 9527
    }

    $port = 0
    if (-not [int]::TryParse($env:AUTOFLUID_IPC_PORT, [ref]$port)) {
        throw "AUTOFLUID_IPC_PORT is not a valid integer."
    }
    if ($port -le 0 -or $port -gt 65535) {
        throw "AUTOFLUID_IPC_PORT is out of range: $port"
    }
    return $port
}

function Test-AutoFluidLocalEndpoint {
    $serverHost = Get-AutoFluidServerHost
    $localHosts = @("127.0.0.1", "localhost", "::1")
    return $serverHost -in $localHosts
}

function Get-AutoFluidLocalEndpointAllowed {
    $allowLocalEndpoint = $env:AUTOFLUID_ALLOW_LOCAL_SERVER_ENDPOINT
    return $allowLocalEndpoint -in @("1", "true", "TRUE", "yes", "YES")
}

function Write-AutoFluidEndpointSummary {
    $serverHost = Get-AutoFluidServerHost
    $serverPort = Get-AutoFluidServerPort
    Write-Host "AutoFluid mode: $env:AUTOFLUID_SERVER_MODE"
    Write-Host "Daemon IPC endpoint: ${serverHost}:${serverPort}"
}

function Assert-AutoFluidServerEndpoint {
    $serverHost = Get-AutoFluidServerHost
    [void](Get-AutoFluidServerPort)

    if ((Test-AutoFluidLocalEndpoint) -and -not (Get-AutoFluidLocalEndpointAllowed)) {
        throw @"
AutoFluid is launching in server mode, but no remote daemon IPC host is configured.
Set AUTOFLUID_SERVER_HOST or AUTOFLUID_IPC_HOST in .env to the ocar/server address.
If you intentionally connect through a local port-forward, set AUTOFLUID_ALLOW_LOCAL_SERVER_ENDPOINT=1.
"@
    }
}

function Test-AutoFluidEndpoint {
    param(
        [int]$TimeoutMs = 1500
    )

    $serverHost = Get-AutoFluidServerHost
    $serverPort = Get-AutoFluidServerPort
    $client = [System.Net.Sockets.TcpClient]::new()
    try {
        $async = $client.BeginConnect($serverHost, $serverPort, $null, $null)
        if (-not $async.AsyncWaitHandle.WaitOne($TimeoutMs)) {
            Write-Warning "Daemon IPC endpoint did not respond within ${TimeoutMs}ms: ${serverHost}:${serverPort}"
            return $false
        }
        $client.EndConnect($async)
        return $true
    }
    catch {
        Write-Warning "Daemon IPC endpoint is not reachable now: ${serverHost}:${serverPort} ($($_.Exception.Message))"
        return $false
    }
    finally {
        $client.Close()
    }
}
