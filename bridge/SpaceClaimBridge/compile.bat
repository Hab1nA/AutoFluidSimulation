@echo off
REM ============================================================================
REM SpaceClaimBridge compile script
REM
REM Builds the main project with the SpaceClaim API reference.
REM Requires .NET Framework 4.8 SDK or Developer Pack.
REM ============================================================================
setlocal enabledelayedexpansion
pushd "%~dp0"

echo [BUILD] SpaceClaimBridge compile
echo.

call "%~dp0find_msbuild.bat"
if errorlevel 1 (
    echo [ERROR] MSBuild not found
    echo.
    echo Please install Visual Studio 2019+ or VS Build Tools 2022
    popd
    exit /b 1
)
echo [INFO] MSBuild: !MSBUILD!
echo.

REM Check whether the SpaceClaim API DLL exists
set "SC_API_DLL=C:\Program Files\ANSYS Inc\v231\SCDM\SpaceClaim.Api.V23.dll"
if not exist "!SC_API_DLL!" echo [WARN] SpaceClaim API DLL not found: !SC_API_DLL!
if not exist "!SC_API_DLL!" echo [WARN] Use compile_noref.bat for no-reference verification

REM Compile
echo [BUILD] Compiling...
"!MSBUILD!" SpaceClaimBridge.csproj -restore /p:Configuration=Release /v:minimal

if errorlevel 1 (
    echo [ERROR] Compile failed!
    echo Possible causes:
    echo   1. SpaceClaim.Api.V23.dll not found, try compile_noref.bat
    echo   2. .NET Framework 4.8 targeting pack is not installed
    echo   3. Project file syntax error
    popd
    exit /b 1
)

echo.
echo [SUCCESS] Compile succeeded!
echo [INFO] Output: bin\Release\net48\SpaceClaimBridge.exe

REM Copy to bridge root for easy access
copy /Y "bin\Release\net48\SpaceClaimBridge.exe" "..\SpaceClaimBridge.exe" >nul 2>&1
if exist "..\SpaceClaimBridge.exe" echo [INFO] Copied to: ..\SpaceClaimBridge.exe

popd
endlocal
