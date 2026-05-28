@echo off
setlocal

set "PROJECT_DIR=%~dp0"
if "%PROJECT_DIR:~-1%"=="\" set "PROJECT_DIR=%PROJECT_DIR:~0,-1%"

set "PYTHON_EXE=%PROJECT_DIR%\.venv\Scripts\python.exe"
set "DAEMON_PS1=%PROJECT_DIR%\scripts\start_daemon_window.ps1"
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

where wt.exe >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Windows Terminal wt.exe was not found.
    echo.
    echo Please install Windows Terminal or ensure wt.exe is available in PATH.
    pause
    exit /b 1
)

set "PS_EXE=pwsh.exe"
where "%PS_EXE%" >nul 2>nul
if errorlevel 1 set "PS_EXE=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"

set "PYTHON=%PYTHON_EXE%"

if /I "%~1"=="--check" (
    echo PROJECT_DIR=%PROJECT_DIR%
    echo PYTHON_EXE=%PYTHON_EXE%
    echo PS_EXE=%PS_EXE%
    echo wt.exe is available.
    "%PS_EXE%" -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%DAEMON_PS1%" -Check
    if errorlevel 1 exit /b 1
    "%PS_EXE%" -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%CLIENT_PS1%" -Check
    if errorlevel 1 exit /b 1
    exit /b 0
)

wt.exe -w new new-tab --title "AutoFluid Daemon" --startingDirectory "%PROJECT_DIR%" "%PS_EXE%" -NoLogo -NoProfile -NoExit -ExecutionPolicy Bypass -File "%DAEMON_PS1%"
if errorlevel 1 (
    echo [ERROR] Failed to start the AutoFluid Daemon Windows Terminal window.
    pause
    exit /b 1
)

timeout /t 2 /nobreak >nul
wt.exe -w new new-tab --title "AutoFluid Client" --startingDirectory "%PROJECT_DIR%" "%PS_EXE%" -NoLogo -NoProfile -NoExit -ExecutionPolicy Bypass -File "%CLIENT_PS1%"
if errorlevel 1 (
    echo [ERROR] Failed to start the AutoFluid Client Windows Terminal window.
    pause
    exit /b 1
)

endlocal
