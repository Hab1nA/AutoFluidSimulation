@echo off
REM ============================================================================
REM SpaceClaimBridge no-reference compile script
REM
REM Builds SpaceClaimBridge.NoRef.csproj, which has no SpaceClaim API Reference.
REM The no-ref build reuses Program.cs and does not require SpaceClaim.Api.V23.dll.
REM
REM Runtime still requires SpaceClaim to be installed.
REM
REM ============================================================================
setlocal enabledelayedexpansion
pushd "%~dp0"

echo [BUILD] SpaceClaimBridge no-reference compile
echo.

call "%~dp0find_msbuild.bat"
if errorlevel 1 (
    echo [ERROR] ??? MSBuild
    echo.
    echo Please install Visual Studio 2019+ or VS Build Tools 2022
    popd
    exit /b 1
)
echo [INFO] MSBuild: !MSBUILD!
echo.

REM Compile
echo [BUILD] Compiling...
"!MSBUILD!" SpaceClaimBridge.NoRef.csproj -restore /p:Configuration=Release /v:minimal

if errorlevel 1 (
    echo.
    echo [ERROR] Compile failed!
    echo.
    echo Possible causes:
    echo   1. .NET Framework 4.8 targeting pack is not installed
    echo   2. Project file syntax error
    popd
    exit /b 1
)

REM Copy to project bridge root for easy access
copy /Y "bin\Release\net48\SpaceClaimBridge.exe" "..\SpaceClaimBridge.exe" >nul 2>&1
if exist "..\SpaceClaimBridge.exe" (
    echo.
    echo [SUCCESS] Compile succeeded: ..\SpaceClaimBridge.exe
) else (
    echo.
    echo [SUCCESS] Compile succeeded: bin\Release\net48\SpaceClaimBridge.exe
)

echo.
echo Usage:
echo   SpaceClaimBridge.exe --script "C:\path\to\transit.py" --config 1 --stepdir "C:\step" --scdocdir "C:\scdoc"

popd
endlocal
