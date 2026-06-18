param(
    [switch]$Check
)

$ErrorActionPreference = "Stop"

$ProjectDir = Split-Path -Parent $PSScriptRoot
$PythonExe = Join-Path $ProjectDir ".venv\Scripts\python.exe"
$ClientScript = Join-Path $ProjectDir "start_client.py"
$EnvScript = Join-Path $PSScriptRoot "autofluid_env.ps1"

. $EnvScript
Import-AutoFluidEnv -ProjectDir $ProjectDir
Assert-AutoFluidServerEndpoint

if (-not (Test-Path -LiteralPath $PythonExe -PathType Leaf)) {
    throw "Project virtual environment Python was not found: $PythonExe"
}

if ($Check) {
    & $PythonExe --version
    Write-AutoFluidEndpointSummary
    [void](Test-AutoFluidEndpoint)
    Write-Host "Client launcher check passed."
    Write-Host "ProjectDir: $ProjectDir"
    Write-Host "PythonExe: $PythonExe"
    exit 0
}

$Host.UI.RawUI.WindowTitle = "AutoFluid Client"
Set-Location -LiteralPath $ProjectDir
$env:PYTHON = $PythonExe

Write-AutoFluidEndpointSummary
& $PythonExe $ClientScript
exit $LASTEXITCODE
