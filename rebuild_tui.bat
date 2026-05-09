@echo off
chcp 65001 >nul
echo ==============================================
echo      AutoFluid TUI - 清理并重新编译脚本
echo ==============================================
echo.

set "TUI_DIR=%~dp0autofluid-tui"

echo [1/2] 清理已编译产物...
cd /d "%TUI_DIR%"
cargo clean
if %errorlevel% neq 0 (
    echo 清理失败！
    pause
    exit /b %errorlevel%
)
echo 清理完成

echo.
echo [2/2] 重新编译 (Release模式)...
cargo build --release
if %errorlevel% neq 0 (
    echo 编译失败！
    pause
    exit /b %errorlevel%
)

echo.
echo ==============================================
echo 编译成功！
echo ==============================================
pause