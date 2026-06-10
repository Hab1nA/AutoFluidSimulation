param(
    [switch]$Check,
    [switch]$Once
)

$ErrorActionPreference = "Stop"

$ProjectDir = Split-Path -Parent $PSScriptRoot
$PythonExe = Join-Path $ProjectDir ".venv\Scripts\python.exe"
$EnvScript = Join-Path $PSScriptRoot "autofluid_env.ps1"

. $EnvScript
Import-AutoFluidEnv -ProjectDir $ProjectDir
Assert-AutoFluidServerEndpoint

if (-not (Test-Path -LiteralPath $PythonExe -PathType Leaf)) {
    throw "Project virtual environment Python was not found: $PythonExe"
}

if ($Check) {
    & $PythonExe --version
    & $PythonExe -c "import engine.local_worker"
    Write-Host "LocalWorker launcher check passed."
    Write-Host "ProjectDir: $ProjectDir"
    Write-Host "PythonExe: $PythonExe"
    Write-AutoFluidEndpointSummary
    [void](Test-AutoFluidEndpoint)
    exit 0
}

$Host.UI.RawUI.WindowTitle = "AutoFluid LocalWorker"
Set-Location -LiteralPath $ProjectDir
$env:PYTHON = $PythonExe

Write-AutoFluidEndpointSummary
if ($Once) {
    & $PythonExe -m engine.local_worker --once
}
else {
    & $PythonExe -m engine.local_worker
}
exit $LASTEXITCODE
