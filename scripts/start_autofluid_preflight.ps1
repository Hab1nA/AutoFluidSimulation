param(
    [switch]$Check
)

$ErrorActionPreference = "Stop"

$ProjectDir = Split-Path -Parent $PSScriptRoot
$PythonExe = Join-Path $ProjectDir ".venv\Scripts\python.exe"
$EnvScript = Join-Path $PSScriptRoot "autofluid_env.ps1"
$TunnelScript = Join-Path $PSScriptRoot "start_server_ipc_tunnel.ps1"

. $EnvScript
Import-AutoFluidEnv -ProjectDir $ProjectDir
Assert-AutoFluidServerEndpoint

if (-not (Test-Path -LiteralPath $PythonExe -PathType Leaf)) {
    throw "Project virtual environment Python was not found: $PythonExe"
}

if ($Check) {
    & $PythonExe --version
    & $TunnelScript -Check
}

Write-AutoFluidEndpointSummary
if (-not $Check) {
    & $TunnelScript
}
[void](Test-AutoFluidEndpoint)
Write-Host "AutoFluid startup preflight passed."
