@echo off
REM ============================================================================
REM 纯 COM 互操作版本编译 — 无需 SpaceClaim API 引用
REM
REM 使用 .NET Framework 自带的 csc.exe 编译器编译，
REM 不依赖 SpaceClaim.Api.V23.dll，使用动态 COM 互操作。
REM
REM 优点: 不需要安装 SpaceClaim 即可编译
REM 缺点: 编译时无法进行强类型检查，IDE 无智能提示
REM
REM 注意: 程序运行时仍需要 SpaceClaim 已安装
REM ============================================================================
setlocal

echo [BUILD] 纯 COM 互操作版本编译
echo.

REM 定位 csc.exe
set CSC=
for %%p in (
    "C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe"
) do (
    if exist %%p (
        set CSC=%%~p
        goto :found_csc
    )
)

echo [ERROR] 无法找到 csc.exe (需要 .NET Framework 4.x)
exit /b 1

:found_csc
echo [INFO] 使用编译器: %CSC%

REM 编译
echo [BUILD] 正在编译 Program.NoRef.cs ...
%CSC% /out:..\SpaceClaimBridge.exe ^
     /target:exe ^
     /platform:x64 ^
     /optimize+ ^
     /reference:System.dll ^
     Program.NoRef.cs

if errorlevel 1 (
    echo.
    echo [ERROR] 编译失败!
    echo 请检查 Program.NoRef.cs 是否存在语法错误
    exit /b 1
)

echo.
echo [SUCCESS] 编译成功: ..\SpaceClaimBridge.exe
echo.
echo 使用方法:
echo   SpaceClaimBridge.exe --script "C:\path\to\transit.py" --config 1 --stepdir "C:\step" --scdocdir "C:\scdoc"

endlocal
