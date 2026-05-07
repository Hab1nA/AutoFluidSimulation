@echo off
cd /d "%~dp0"
powershell -NoExit -Command "Write-Host '正在启动 AutoFluidSimulation 客户端...' -ForegroundColor Cyan; python start_client.py"
