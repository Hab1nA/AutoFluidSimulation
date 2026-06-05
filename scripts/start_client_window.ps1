param(
    [switch]$Check
)

$ErrorActionPreference = "Stop"

$ProjectDir = Split-Path -Parent $PSScriptRoot
$PythonExe = Join-Path $ProjectDir ".venv\Scripts\python.exe"
$ClientScript = Join-Path $ProjectDir "start_client.py"

if (-not (Test-Path -LiteralPath $PythonExe -PathType Leaf)) {
    throw "Project virtual environment Python was not found: $PythonExe"
}

if ($Check) {
    & $PythonExe --version
    Write-Host "Client launcher check passed."
    Write-Host "ProjectDir: $ProjectDir"
    Write-Host "PythonExe: $PythonExe"
    exit 0
}

$Host.UI.RawUI.WindowTitle = "AutoFluid Client"
Set-Location -LiteralPath $ProjectDir
$env:PYTHON = $PythonExe

& $PythonExe $ClientScript
exit $LASTEXITCODE
