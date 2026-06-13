@echo off
setlocal

set "PROJECT_DIR=%~dp0"
if "%PROJECT_DIR:~-1%"=="\" set "PROJECT_DIR=%PROJECT_DIR:~0,-1%"

set "PYTHON_EXE=%PROJECT_DIR%\.venv\Scripts\python.exe"
set "WORKER_PS1=%PROJECT_DIR%\scripts\start_local_worker_window.ps1"
set "CLIENT_PS1=%PROJECT_DIR%\scripts\start_client_window.ps1"

if not exist "%PYTHON_EXE%" (
    echo [ERROR] Project virtual environment was not found:
    echo         %PYTHON_EXE%
    echo.
    echo Please create or restore .venv before launching AutoFluid.
    pause
    exit /b 1
)

"%PYTHON_EXE%" --version >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Project virtual environment exists but cannot run:
    echo         %PYTHON_EXE%
    echo.
    echo Recreate or repair .venv, then run this launcher again.
    pause
    exit /b 1
)

set "PS_EXE=pwsh.exe"
where "%PS_EXE%" >nul 2>nul
if errorlevel 1 set "PS_EXE=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"

where wt.exe >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Windows Terminal wt.exe was not found.
    echo.
    echo Please install Windows Terminal or ensure wt.exe is available in PATH.
    pause
    exit /b 1
)

set "PYTHON=%PYTHON_EXE%"
set "START_CLIENT=1"
set "START_WORKER=0"
set "RUN_CHECK=0"

:parse_args
if "%~1"=="" goto args_done

if /I "%~1"=="--client-only" (
    set "START_WORKER=0"
) else if /I "%~1"=="--worker-only" (
    set "START_CLIENT=0"
    set "START_WORKER=1"
) else if /I "%~1"=="--check" (
    set "RUN_CHECK=1"
) else if /I "%~1"=="--help" (
    echo Usage:
    echo   start_autofluid.bat
    echo   start_autofluid.bat --client-only
    echo   start_autofluid.bat --worker-only
    echo   start_autofluid.bat --check
    echo   start_autofluid.bat --client-only --check
    echo   start_autofluid.bat --worker-only --check
    echo.
    echo Notes:
    echo   - The daemon runs on the server in current mode.
    echo   - Default launch starts only the local TUI client.
    echo   - Use daemon start and worker start inside the TUI to establish tunnels.
    exit /b 0
) else (
    echo [ERROR] Unknown argument: %~1
    echo Run start_autofluid.bat --help for usage.
    exit /b 1
)
shift
goto parse_args

:args_done
if "%START_CLIENT%"=="0" if "%START_WORKER%"=="0" (
    echo [ERROR] Nothing to start. Choose at least one of client or worker.
    exit /b 1
)

if "%RUN_CHECK%"=="1" (
    echo PROJECT_DIR=%PROJECT_DIR%
    echo PYTHON_EXE=%PYTHON_EXE%
    echo PS_EXE=%PS_EXE%
    echo wt.exe is available.
    if "%START_WORKER%"=="1" (
        "%PS_EXE%" -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%WORKER_PS1%" -Check
        if errorlevel 1 exit /b 1
    )
    if "%START_CLIENT%"=="1" (
        "%PS_EXE%" -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%CLIENT_PS1%" -Check
        if errorlevel 1 exit /b 1
    )
    exit /b 0
)

echo [INFO] Starting AutoFluid local client. Use daemon start in the TUI to start the server daemon.

if "%START_WORKER%"=="1" (
    start "AutoFluid LocalWorker" /D "%PROJECT_DIR%" "%PS_EXE%" -NoLogo -NoProfile -NoExit -ExecutionPolicy Bypass -File "%WORKER_PS1%"
    if errorlevel 1 (
        echo [ERROR] Failed to start the AutoFluid LocalWorker console window.
        pause
        exit /b 1
    )
)

if "%START_CLIENT%"=="1" (
    if "%START_WORKER%"=="1" timeout /t 2 /nobreak >nul
    wt.exe -w -1 nt --title "AutoFluid Client" --startingDirectory "%PROJECT_DIR%" "%PS_EXE%" -NoLogo -NoProfile -NoExit -ExecutionPolicy Bypass -File "%CLIENT_PS1%"
    if errorlevel 1 (
        echo [ERROR] Failed to start the AutoFluid Client Windows Terminal window.
        pause
        exit /b 1
    )
)

endlocal
