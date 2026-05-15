@echo off
REM ============================================================================
REM 纯 COM 互操作版本编译 — 无需 SpaceClaim API 引用
REM
REM 使用 MSBuild 编译 SDK 风格 csproj，不依赖 SpaceClaim.Api.V23.dll。
REM csproj 中已设置 Private=false，编译器不会强求该 DLL 存在。
REM
REM 优点: 不需要安装 SpaceClaim 即可编译
REM 缺点: 编译时无法进行强类型检查，IDE 无智能提示
REM
REM 注意: 程序运行时仍需要 SpaceClaim 已安装
REM ============================================================================
setlocal enabledelayedexpansion

echo [BUILD] 纯 COM 互操作版本编译
echo.

REM 仅使用 MSBuild（VS 2019+）编译
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
echo [INFO] 使用 MSBuild: !MSBUILD!
echo.

REM 编译
echo [BUILD] 正在编译...
"!MSBUILD!" SpaceClaimBridge.csproj -restore /p:Configuration=Release /p:Platform=x64 /v:minimal

if errorlevel 1 (
    echo.
    echo [ERROR] 编译失败!
    echo.
    echo 可能原因:
    echo   1. .NET Framework 4.8 targeting pack 未安装
    echo   2. 项目文件有语法错误
    exit /b 1
)

REM 复制到项目 bridge 根目录方便调用
copy /Y "bin\Release\net48\SpaceClaimBridge.exe" "..\SpaceClaimBridge.exe" >nul 2>&1
if exist "..\SpaceClaimBridge.exe" (
    echo.
    echo [SUCCESS] 编译成功: ..\SpaceClaimBridge.exe
) else (
    echo.
    echo [SUCCESS] 编译成功: bin\Release\net48\SpaceClaimBridge.exe
)

echo.
echo 使用方法:
echo   SpaceClaimBridge.exe --script "C:\path\to\transit.py" --config 1 --stepdir "C:\step" --scdocdir "C:\scdoc"

endlocal
