@echo off
setlocal EnableDelayedExpansion
set "DIR=%~dp0"
if "%DIR:~-1%"=="\" set "DIR=%DIR:~0,-1%"

wt -w 0 new-tab --title "Daemon" -d "%DIR%" cmd /k "python start_daemon.py"

rem 使用 netstat 检测端口是否已监听，避免建立真实 TCP 连接触发 IPC 日志
set "READY=0"
for /L %%i in (1,1,60) do (
    if !READY! equ 0 (
        netstat -an 2>nul | findstr ":9527" >nul
        if !errorlevel! equ 0 set "READY=1"
        if !READY! equ 0 ping -n 2 127.0.0.1 >nul
    )
)
if !READY! equ 0 (
    echo [31m错误: 后台引擎未能按时启动，请检查 start_daemon.py 的输出[0m
    exit /b 1
)

wt -w 0 new-tab --title "Client" -d "%DIR%" cmd /k "python start_client.py"
endlocal
