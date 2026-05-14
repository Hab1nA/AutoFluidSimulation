@echo off
chcp 65001 >nul
setlocal EnableDelayedExpansion

echo ==============================================
echo      AutoFluid TUI - 重建工作流
echo ==============================================
echo.

set "PROJECT_ROOT=%~dp0"
set "TUI_DIR=%PROJECT_ROOT%autofluid-tui"
set "TARGET_DIR=%TUI_DIR%\target"
set "RELEASE_DIR=%TARGET_DIR%\release"
set "STAGING_DIR=%TUI_DIR%\_staging"

cd /d "%TUI_DIR%"
if !errorlevel! neq 0 (
    echo [错误] 无法进入项目目录: %TUI_DIR%
    pause
    exit /b 1
)

:: =============================================================================
::  第 1 步：全项目临时文件清理
:: =============================================================================
echo [1/3] 清理全项目临时文件和缓存...

:: --- Rust 编译产物 ---
if exist "%TARGET_DIR%" (
    echo   正在删除 Rust target 目录...
    rmdir /s /q "%TARGET_DIR%" 2>nul
    if exist "%TARGET_DIR%" (
        echo   [警告] Rust target 目录部分清理失败，继续执行...
    ) else (
        echo   [完成] 已删除 Rust 编译产物
    )
)

:: --- Python __pycache__ 目录 ---
set "PYCACHE_COUNT=0"
for /d /r "%PROJECT_ROOT%" %%D in (__pycache__) do (
    if exist "%%D" (
        rmdir /s /q "%%D" 2>nul
        set /a PYCACHE_COUNT+=1
    )
)
echo   [完成] 已清理 !PYCACHE_COUNT! 个 Python __pycache__ 目录

:: --- Python 孤立 .pyc 文件 ---
set "PYC_COUNT=0"
for /r "%PROJECT_ROOT%" %%F in (*.pyc) do (
    del /f /q "%%F" 2>nul
    set /a PYC_COUNT+=1
)
if !PYC_COUNT! gtr 0 echo   [完成] 已清理 !PYC_COUNT! 个 .pyc 文件

:: --- pytest / mypy / ruff 缓存 ---
for %%C in (".pytest_cache" ".mypy_cache" ".ruff_cache" ".tox") do (
    set "CACHE_DIR=%PROJECT_ROOT%%%C"
    if exist "!CACHE_DIR!" (
        rmdir /s /q "!CACHE_DIR!" 2>nul
        echo   [完成] 已删除 %%~C
    )
)

:: --- Rust flycheck 缓存 (rust-analyzer 外部检查产物) ---
if exist "%TUI_DIR%\flycheck0" (
    rmdir /s /q "%TUI_DIR%\flycheck0" 2>nul
    echo   [完成] 已删除 Rust flycheck 缓存
)

echo [完成] 第 1 步：全项目清理完毕
echo.

:: =============================================================================
::  第 2 步：编译 Rust TUI 前端
:: =============================================================================
echo [2/3] 编译 Rust TUI 前端 ^(Release 模式^)...

cargo build --release
if !errorlevel! neq 0 (
    echo [错误] 编译失败！错误代码: !errorlevel!
    pause
    exit /b !errorlevel!
)
echo [完成] 编译成功
echo.

:: =============================================================================
::  第 3 步：再次清理，仅保留最终编译成果
:: =============================================================================
echo [3/3] 清理编译副产品，仅保留可执行文件...

:: 验证编译产物
set "BIN_FILES=autofluid-tui.exe autofluid_tui.pdb"
set "FOUND_COUNT=0"
for %%F in (%BIN_FILES%) do (
    if exist "%RELEASE_DIR%\%%F" set /a FOUND_COUNT+=1
)

if !FOUND_COUNT! equ 0 (
    echo [错误] 未找到任何编译产物！
    pause
    exit /b 1
)

:: 暂存最终产物
if exist "%STAGING_DIR%" rmdir /s /q "%STAGING_DIR%" 2>nul
mkdir "%STAGING_DIR%" 2>nul
if not exist "%STAGING_DIR%" (
    echo [错误] 无法创建暂存目录，编译产物将保留在 target\release 中
    goto :skip_staging
)

set "BACKUP_COUNT=0"
for %%F in (%BIN_FILES%) do (
    if exist "%RELEASE_DIR%\%%F" (
        copy /y "%RELEASE_DIR%\%%F" "%STAGING_DIR%\%%F" >nul 2>&1
        if exist "%STAGING_DIR%\%%F" set /a BACKUP_COUNT+=1
    )
)

:: 删除整个 target 目录（含所有中间产物）
echo   正在删除 Rust 编译中间产物...
rmdir /s /q "%TARGET_DIR%" 2>nul
if exist "%TARGET_DIR%" (
    echo   [警告] target 目录部分残留，尝试强制清理...
    timeout /t 2 /nobreak >nul
    rmdir /s /q "%TARGET_DIR%" 2>nul
)

:: --- 二次清理 Python 缓存（编译过程中可能生成） ---
for /d /r "%PROJECT_ROOT%" %%D in (__pycache__) do (
    if exist "%%D" rmdir /s /q "%%D" 2>nul
)
for /r "%PROJECT_ROOT%" %%F in (*.pyc) do del /f /q "%%F" 2>nul

:: 恢复最终产物到 release 目录
if !BACKUP_COUNT! gtr 0 (
    mkdir "%RELEASE_DIR%" 2>nul
    for %%F in ("%STAGING_DIR%\*") do (
        copy /y "%%F" "%RELEASE_DIR%\%%~nxF" >nul 2>&1
    )
    echo   [完成] 已恢复 !BACKUP_COUNT! 个编译产物到 %RELEASE_DIR%
)

rmdir /s /q "%STAGING_DIR%" 2>nul

:skip_staging
echo [完成] 第 3 步：临时文件清理完毕
echo.

:: =============================================================================
::  输出结果
:: =============================================================================
echo ==============================================
echo      重建成功！
echo.
if exist "%RELEASE_DIR%\autofluid-tui.exe" (
    echo      可执行文件:
    for %%F in ("%RELEASE_DIR%\*") do echo        %%~nxF
)
echo      输出目录: %RELEASE_DIR%
echo ==============================================
endlocal
pause
exit /b 0
