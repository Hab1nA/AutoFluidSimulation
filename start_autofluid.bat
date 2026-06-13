@echo off
setlocal enabledelayedexpansion

:: AutoFluid Launcher - Windows Batch Script
:: Launches Client and optionally LocalWorker in separate windows

set "PROJECT_DIR=%~dp0"
set "PS_EXE=powershell.exe"
set "CLIENT_PS1=%PROJECT_DIR%scripts\start_client_window.ps1"
set "WORKER_PS1=%PROJECT_DIR%scripts\start_local_worker_window.ps1"

:: Check for --worker-only argument
set "START_CLIENT=1"
set "START_WORKER=1"

if /I "%~1"=="--worker-only" (
    set "START_CLIENT=0"
    set "START_WORKER=1"
)

:: Check if Windows Terminal is available
where wt.exe >nul 2>nul
if %errorlevel% equ 0 (
    set "USE_WT=1"
) else (
    set "USE_WT=0"
)

:: Launch Client if enabled
if "%START_CLIENT%"=="1" (
    if "%USE_WT%"=="1" (
        :: Use Windows Terminal for Client
        wt.exe -w -1 nt --title "AutoFluid Client" --startingDirectory "%PROJECT_DIR%" "%PS_EXE%" -NoLogo -NoProfile -NoExit -ExecutionPolicy Bypass -File "%CLIENT_PS1%"
    ) else (
        :: Fallback to cmd start for Client
        start "AutoFluid Client" /D "%PROJECT_DIR%" "%PS_EXE%" -NoLogo -NoProfile -NoExit -ExecutionPolicy Bypass -File "%CLIENT_PS1%"
    )
)

:: Launch LocalWorker if enabled
if "%START_WORKER%"=="1" (
    :: Always use cmd start for LocalWorker (separate console)
    start "AutoFluid LocalWorker" /D "%PROJECT_DIR%" "%PS_EXE%" -NoLogo -NoProfile -NoExit -ExecutionPolicy Bypass -File "%WORKER_PS1%"
)

endlocal