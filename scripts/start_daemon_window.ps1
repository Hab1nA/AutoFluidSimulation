param(
    [switch]$Check
)

$ErrorActionPreference = "Stop"

$ProjectDir = Split-Path -Parent $PSScriptRoot
$PythonExe = Join-Path $ProjectDir ".venv\Scripts\python.exe"
$DaemonScript = Join-Path $ProjectDir "start_daemon.py"

if (-not (Test-Path -LiteralPath $PythonExe -PathType Leaf)) {
    throw "Project virtual environment Python was not found: $PythonExe"
}

if ($Check) {
    & $PythonExe --version
    Write-Host "Daemon launcher check passed."
    Write-Host "ProjectDir: $ProjectDir"
    Write-Host "PythonExe: $PythonExe"
    exit 0
}

$Host.UI.RawUI.WindowTitle = "AutoFluid Daemon"
Set-Location -LiteralPath $ProjectDir
$env:PYTHON = $PythonExe

& $PythonExe $DaemonScript
exit $LASTEXITCODE
