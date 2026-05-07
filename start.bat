@echo off
setlocal EnableDelayedExpansion
set "DIR=%~dp0"
if "%DIR:~-1%"=="\" set "DIR=%DIR:~0,-1%"

wt -w 0 new-tab --title "Daemon" -d "%DIR%" cmd /k "python start_daemon.py"

set "READY=0"
for /L %%i in (1,1,30) do (
    if !READY! equ 0 (
        python -c "import socket; s=socket.socket(); s.settimeout(1); s.connect(('127.0.0.1', 9527)); s.close()" 2>nul
        if !errorlevel! equ 0 set "READY=1"
        if !READY! equ 0 ping -n 2 127.0.0.1 >nul
    )
)

wt -w 0 new-tab --title "Client" -d "%DIR%" cmd /k "python start_client.py"
endlocal
