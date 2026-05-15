@echo off
REM ============================================================================
REM SpaceClaimBridge 编译脚本
REM
REM 使用 .NET Framework MSBuild 编译 C# 桥接程序。
REM 需要 .NET Framework 4.8 SDK（随 Visual Studio 2019+ 安装）或
REM 至少安装了 .NET Framework 4.8 Developer Pack。
REM
REM 如果无法编译，请参见下方的"手动编译"备选方案。
REM ============================================================================
setlocal enabledelayedexpansion

echo [BUILD] SpaceClaimBridge 编译脚本
echo.

REM 仅使用 MSBuild（VS 2019+）编译
REM 找不到 MSBuild 则直接报错退出

set MSBUILD=
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
        set MSBUILD=%%~p
        goto :found_msbuild
    )
)

echo [ERROR] 未找到 MSBuild
echo.
echo 请安装 Visual Studio 2019+ 或 VS Build Tools 2022
exit /b 1

:found_msbuild
echo [INFO] 使用 MSBuild: %MSBUILD%
echo.

REM 检查 SpaceClaim API DLL 是否存在（强类型版本需要）
set SC_API_DLL=C:\Program Files\ANSYS Inc\v231\SCDM\SpaceClaim.Api.V23.dll
if not exist "%SC_API_DLL%" (
    echo [WARN] SpaceClaim API DLL 未找到: %SC_API_DLL%
    echo [WARN] 将使用纯 COM 互操作版本 (Program.NoRef.cs) 编译
    echo [WARN] 请运行 compile_noref.bat 获取详细说明
    echo.
)

REM 编译
echo [BUILD] 正在编译...
%MSBUILD% SpaceClaimBridge.csproj -restore /p:Configuration=Release /v:minimal

if errorlevel 1 (
    echo.
    echo [ERROR] 编译失败!
    echo.
    echo 可能原因:
    echo   1. SpaceClaim.Api.V23.dll 不存在（尝试 compile_noref.bat）
    echo   2. .NET Framework 4.8 targeting pack 未安装
    echo   3. 项目文件有语法错误
    exit /b 1
)

echo.
echo [SUCCESS] 编译成功!
echo [INFO] 输出: bin\Release\net48\SpaceClaimBridge.exe

REM 复制到项目 bridge 根目录方便调用
copy /Y "bin\Release\net48\SpaceClaimBridge.exe" "..\SpaceClaimBridge.exe" >nul 2>&1
if exist "..\SpaceClaimBridge.exe" (
    echo [INFO] 已复制到: ..\SpaceClaimBridge.exe
)

:done
endlocal
