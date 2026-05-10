@echo off
chcp 65001 >nul
setlocal EnableDelayedExpansion

echo ==============================================
echo      AutoFluid TUI - 重建工作流
echo ==============================================
echo.

set "TUI_DIR=%~dp0autofluid-tui"
set "TARGET_DIR=%TUI_DIR%\target"
set "RELEASE_DIR=%TARGET_DIR%\release"
set "TEMP_DIR=%TUI_DIR%\_temp_release"

cd /d "%TUI_DIR%"
if !errorlevel! neq 0 (
    echo [错误] 无法进入项目目录: %TUI_DIR%
    pause
    exit /b 1
)

echo [1/3] 执行完整清理操作...
if exist "%TARGET_DIR%" (
    rmdir /s /q "%TARGET_DIR%"
    if exist "%TARGET_DIR%" (
        echo [错误] 完整清理失败，目录仍存在: %TARGET_DIR%
        pause
        exit /b 1
    )
    echo [完成] 已删除所有编译中间文件和结果文件
) else (
    echo [跳过] 未发现编译产物目录，无需清理
)

echo.
echo [2/3] 执行编译过程 ^(Release模式^)...
cargo build --release
if !errorlevel! neq 0 (
    echo [错误] 编译失败！错误代码: !errorlevel!
    pause
    exit /b !errorlevel!
)
echo [完成] 编译成功

echo.
echo [3/3] 执行选择性清理操作 ^(仅保留结果文件^)...

if not exist "%RELEASE_DIR%\autofluid-tui.exe" (
    echo [错误] 编译结果文件不存在: %RELEASE_DIR%\autofluid-tui.exe
    pause
    exit /b 1
)

if exist "%TEMP_DIR%" rmdir /s /q "%TEMP_DIR%"
mkdir "%TEMP_DIR%"
if not exist "%TEMP_DIR%" (
    echo [错误] 创建临时目录失败: %TEMP_DIR%
    pause
    exit /b 1
)

set "BACKUP_COUNT=0"
for %%F in (
    "autofluid-tui.exe"
    "autofluid_tui.pdb"
) do (
    if exist "%RELEASE_DIR%\%%~F" (
        copy "%RELEASE_DIR%\%%~F" "%TEMP_DIR%\%%~F" >nul 2>&1
        if exist "%TEMP_DIR%\%%~F" (
            echo [备份] %%~F
            set /a BACKUP_COUNT+=1
        ) else (
            echo [警告] 备份 %%~F 失败
        )
    )
)

echo [完成] 已备份 !BACKUP_COUNT! 个结果文件

rmdir /s /q "%TARGET_DIR%"
if exist "%TARGET_DIR%" (
    echo [错误] 删除编译产物目录失败，尝试恢复...
    if not exist "%RELEASE_DIR%" mkdir "%RELEASE_DIR%"
    for %%F in ("%TEMP_DIR%\*") do (
        copy "%%F" "%RELEASE_DIR%\%%~nxF" >nul 2>&1
    )
    rmdir /s /q "%TEMP_DIR%"
    pause
    exit /b 1
)
echo [完成] 已删除所有编译中间文件

mkdir "%RELEASE_DIR%"
set "RESTORE_COUNT=0"
for %%F in ("%TEMP_DIR%\*") do (
    copy "%%F" "%RELEASE_DIR%\%%~nxF" >nul 2>&1
    if exist "%RELEASE_DIR%\%%~nxF" (
        echo [恢复] %%~nxF
        set /a RESTORE_COUNT+=1
    ) else (
        echo [警告] 恢复 %%~nxF 失败
    )
)
echo [完成] 已恢复 !RESTORE_COUNT! 个结果文件

rmdir /s /q "%TEMP_DIR%"

echo.
if exist "%RELEASE_DIR%\autofluid-tui.exe" (
    echo [验证] 编译结果文件已保留: autofluid-tui.exe
) else (
    echo [警告] 编译结果文件未找到: autofluid-tui.exe
)

echo.
echo ==============================================
echo  重建工作流完成！
echo  结果目录: %RELEASE_DIR%\
echo ==============================================
pause
