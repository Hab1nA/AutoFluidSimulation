@echo off
chcp 65001 >nul
setlocal EnableDelayedExpansion

echo ==============================================
echo      AutoFluid 全项目重建工作流
echo ==============================================
echo.

set "PROJECT_ROOT=%~dp0"
set "TUI_DIR=%PROJECT_ROOT%autofluid-tui"
set "TARGET_DIR=%TUI_DIR%\target"
set "RELEASE_DIR=%TARGET_DIR%\release"
set "STAGING_DIR=%TUI_DIR%\_staging"
set "BRIDGE_DIR=%PROJECT_ROOT%bridge\SpaceClaimBridge"
set "BRIDGE_EXE=%PROJECT_ROOT%bridge\SpaceClaimBridge.exe"

:: =============================================================================
::  第 1 步：全项目编译产物清理（Rust + Python + C#）
:: =============================================================================
echo [1/3] 清理全项目编译产物和临时文件...
echo.

:: --- Rust 编译产物 ---
echo   [Rust]
if exist "%TARGET_DIR%" (
    echo     正在删除 target 目录...
    rmdir /s /q "%TARGET_DIR%" 2>nul
    if exist "%TARGET_DIR%" (
        echo     [警告] target 目录部分清理失败，继续执行...
    ) else (
        echo     [完成] 已删除 Rust 编译产物
    )
) else (
    echo     [跳过] target 目录不存在
)

:: --- Rust flycheck 缓存 ---
if exist "%TUI_DIR%\flycheck0" (
    rmdir /s /q "%TUI_DIR%\flycheck0" 2>nul
    echo     [完成] 已删除 flycheck 缓存
)

:: --- C# 编译产物 ---
echo   [C#]
if exist "%BRIDGE_DIR%\bin" (
    rmdir /s /q "%BRIDGE_DIR%\bin" 2>nul
    echo     [完成] 已删除 bin 目录
)
if exist "%BRIDGE_DIR%\obj" (
    rmdir /s /q "%BRIDGE_DIR%\obj" 2>nul
    echo     [完成] 已删除 obj 目录
)
if exist "%BRIDGE_EXE%" (
    del /f /q "%BRIDGE_EXE%" 2>nul
    echo     [完成] 已删除 SpaceClaimBridge.exe
)

:: --- Python __pycache__ 目录 ---
echo   [Python]
set "PYCACHE_COUNT=0"
for /d /r "%PROJECT_ROOT%" %%D in (__pycache__) do (
    if exist "%%D" (
        rmdir /s /q "%%D" 2>nul
        set /a PYCACHE_COUNT+=1
    )
)
echo     [完成] 已清理 !PYCACHE_COUNT! 个 __pycache__ 目录

:: --- Python 孤立 .pyc 文件 ---
set "PYC_COUNT=0"
for /r "%PROJECT_ROOT%" %%F in (*.pyc) do (
    del /f /q "%%F" 2>nul
    set /a PYC_COUNT+=1
)
if !PYC_COUNT! gtr 0 echo     [完成] 已清理 !PYC_COUNT! 个 .pyc 文件

:: --- pytest / mypy / ruff 缓存 ---
for %%C in (".pytest_cache" ".mypy_cache" ".ruff_cache" ".tox") do (
    set "CACHE_DIR=%PROJECT_ROOT%%%C"
    if exist "!CACHE_DIR!" (
        rmdir /s /q "!CACHE_DIR!" 2>nul
        echo     [完成] 已删除 %%~C
    )
)

echo.
echo [完成] 第 1 步：全项目编译产物清理完毕
echo.

:: =============================================================================
::  第 2 步：完整编译（Rust TUI + C# Bridge）
:: =============================================================================
echo [2/3] 完整编译所有项目组件...
echo.

:: --- 编译 Rust TUI ---
echo   [Rust TUI] 编译 Release 模式...
cd /d "%TUI_DIR%"
if !errorlevel! neq 0 (
    echo [错误] 无法进入 TUI 目录: %TUI_DIR%
    pause
    exit /b 1
)

cargo build --release
if !errorlevel! neq 0 (
    echo [错误] Rust TUI 编译失败！错误代码: !errorlevel!
    pause
    exit /b !errorlevel!
)
echo     [完成] Rust TUI 编译成功
echo.

:: --- 编译 C# Bridge ---
echo   [C# Bridge] 编译 SpaceClaimBridge...
cd /d "%BRIDGE_DIR%"
if !errorlevel! neq 0 (
    echo [错误] 无法进入 Bridge 目录: %BRIDGE_DIR%
    pause
    exit /b 1
)

:: 仅使用 MSBuild（VS 2019+）编译 C# Bridge
set "BRIDGE_BUILT=0"

set "MSBUILD="
for %%p in (
    "C:\Program Files\Microsoft Visual Studio\2022\Community\MSBuild\Current\Bin\MSBuild.exe"
    "C:\Program Files\Microsoft Visual Studio\2022\Professional\MSBuild\Current\Bin\MSBuild.exe"
    "C:\Program Files\Microsoft Visual Studio\2022\Enterprise\MSBuild\Current\Bin\MSBuild.exe"
    "C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\MSBuild\Current\Bin\MSBuild.exe"
    "C:\Program Files\Microsoft Visual Studio\2019\Community\MSBuild\Current\Bin\MSBuild.exe"
    "C:\Program Files\Microsoft Visual Studio\2019\Professional\MSBuild\Current\Bin\MSBuild.exe"
    "C:\Program Files (x86)\Microsoft Visual Studio\2019\BuildTools\MSBuild\Current\Bin\MSBuild.exe"
) do (
    if exist %%p (
        set "MSBUILD=%%~p"
        goto :found_msbuild
    )
)

echo [错误] 未找到 MSBuild
echo     请安装 Visual Studio 2019+ 或 VS Build Tools 2022
pause
exit /b 1

:found_msbuild
echo     使用 MSBuild: !MSBUILD!
"!MSBUILD!" SpaceClaimBridge.csproj -restore /p:Configuration=Release /v:minimal
if !errorlevel! neq 0 (
    echo [错误] C# Bridge 编译失败！
    pause
    exit /b !errorlevel!
)
if exist "bin\Release\net48\SpaceClaimBridge.exe" (
    copy /Y "bin\Release\net48\SpaceClaimBridge.exe" "%BRIDGE_EXE%" >nul 2>&1
)
echo     [完成] C# Bridge 编译成功（MSBuild）
set "BRIDGE_BUILT=1"

:bridge_build_done
echo.
cd /d "%PROJECT_ROOT%"
echo [完成] 第 2 步：全部编译完成
echo.

:: =============================================================================
::  第 3 步：清理编译中间产物，仅保留最终成果
:: =============================================================================
echo [3/3] 清理编译中间产物，仅保留最终成果...
echo.

:: --- Rust：暂存 release 产物，删除整个 target ---
echo   [Rust] 清理编译中间产物...

set "BIN_FILES=autofluid-tui.exe autofluid_tui.pdb"
set "FOUND_COUNT=0"
for %%F in (%BIN_FILES%) do (
    if exist "%RELEASE_DIR%\%%F" set /a FOUND_COUNT+=1
)

if !FOUND_COUNT! equ 0 (
    echo     [警告] 未找到 Rust 编译产物，跳过暂存
    goto :skip_rust_staging
)

:: 暂存最终产物
if exist "%STAGING_DIR%" rmdir /s /q "%STAGING_DIR%" 2>nul
mkdir "%STAGING_DIR%" 2>nul

set "BACKUP_COUNT=0"
for %%F in (%BIN_FILES%) do (
    if exist "%RELEASE_DIR%\%%F" (
        copy /y "%RELEASE_DIR%\%%F" "%STAGING_DIR%\%%F" >nul 2>&1
        if exist "%STAGING_DIR%\%%F" set /a BACKUP_COUNT+=1
    )
)

:: 删除整个 target 目录
rmdir /s /q "%TARGET_DIR%" 2>nul
if exist "%TARGET_DIR%" (
    echo     [警告] target 目录部分残留，尝试强制清理...
    timeout /t 2 /nobreak >nul
    rmdir /s /q "%TARGET_DIR%" 2>nul
)

:: 恢复最终产物
if !BACKUP_COUNT! gtr 0 (
    mkdir "%RELEASE_DIR%" 2>nul
    for %%F in ("%STAGING_DIR%\*") do (
        copy /y "%%F" "%RELEASE_DIR%\%%~nxF" >nul 2>&1
    )
    echo     [完成] 已保留 !BACKUP_COUNT! 个 Rust 编译产物
)
rmdir /s /q "%STAGING_DIR%" 2>nul

:skip_rust_staging

:: --- C#：清理 bin/obj 中间产物，保留最终 exe ---
echo   [C#] 清理编译中间产物...
if exist "%BRIDGE_DIR%\obj" (
    rmdir /s /q "%BRIDGE_DIR%\obj" 2>nul
    echo     [完成] 已删除 obj 目录
)
if exist "%BRIDGE_DIR%\bin" (
    rmdir /s /q "%BRIDGE_DIR%\bin" 2>nul
    echo     [完成] 已删除 bin 目录
)
if exist "%BRIDGE_EXE%" (
    echo     [完成] 已保留 SpaceClaimBridge.exe
) else (
    echo     [警告] 未找到 SpaceClaimBridge.exe
)

:: --- Python：二次清理（编译过程中可能重新生成） ---
echo   [Python] 清理运行时缓存...
for /d /r "%PROJECT_ROOT%" %%D in (__pycache__) do (
    if exist "%%D" rmdir /s /q "%%D" 2>nul
)
for /r "%PROJECT_ROOT%" %%F in (*.pyc) do del /f /q "%%F" 2>nul

echo.
echo [完成] 第 3 步：中间产物清理完毕
echo.

:: =============================================================================
::  输出结果
:: =============================================================================
echo ==============================================
echo      重建完成！
echo.
echo      编译成果：
if exist "%RELEASE_DIR%\autofluid-tui.exe" (
    echo        [Rust TUI]
    for %%F in ("%RELEASE_DIR%\*") do echo          %%~nxF
    echo          位置: %RELEASE_DIR%
)
if exist "%BRIDGE_EXE%" (
    echo        [C# Bridge]
    echo          SpaceClaimBridge.exe
    echo          位置: %BRIDGE_EXE%
)
echo.
echo ==============================================
endlocal
pause
exit /b 0
