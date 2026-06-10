param(
    [switch]$Check,
    [switch]$LegacyLocalDaemon
)

$ErrorActionPreference = "Stop"

$ProjectDir = Split-Path -Parent $PSScriptRoot
$PythonExe = Join-Path $ProjectDir ".venv\Scripts\python.exe"
$DaemonScript = Join-Path $ProjectDir "start_daemon.py"
$EnvScript = Join-Path $PSScriptRoot "autofluid_env.ps1"

. $EnvScript
Import-AutoFluidEnv -ProjectDir $ProjectDir

if (-not (Test-Path -LiteralPath $PythonExe -PathType Leaf)) {
    throw "Project virtual environment Python was not found: $PythonExe"
}

if ($Check) {
    & $PythonExe --version
    Write-Host "Daemon launcher is disabled for local startup."
    Write-Host "Use scripts\start_local_worker_window.ps1 plus scripts\start_client_window.ps1."
    Write-Host "ProjectDir: $ProjectDir"
    Write-Host "PythonExe: $PythonExe"
    exit 0
}

if (-not $LegacyLocalDaemon) {
    Write-Error "Local daemon startup is disabled. The daemon now runs on the server. Use start_autofluid.bat to launch LocalWorker and Client, or pass -LegacyLocalDaemon explicitly for local development only."
    exit 2
}

$Host.UI.RawUI.WindowTitle = "AutoFluid Daemon"
Set-Location -LiteralPath $ProjectDir
$env:PYTHON = $PythonExe

& $PythonExe $DaemonScript
exit $LASTEXITCODE
