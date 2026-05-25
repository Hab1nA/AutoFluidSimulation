@echo off
REM ============================================================================
REM SpaceClaimBridge Compile Script
REM
REM Compiles C# bridge using .NET Framework MSBuild.
REM Requires .NET Framework 4.8 SDK (VS 2019+) or .NET Framework 4.8 Developer Pack.
REM ============================================================================
setlocal enabledelayedexpansion

echo [BUILD] SpaceClaimBridge compile script
echo.

REM Search for MSBuild (VS 2019+)
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

echo [ERROR] MSBuild not found
echo.
echo Please install Visual Studio 2019+ or VS Build Tools 2022
exit /b 1

:found_msbuild
echo [INFO] MSBuild: !MSBUILD!
echo.

REM Check if SpaceClaim API DLL exists
set "SC_API_DLL=C:\Program Files\ANSYS Inc\v231\SCDM\SpaceClaim.Api.V23.dll"
if not exist "!SC_API_DLL!" echo [WARN] SpaceClaim API DLL not found: !SC_API_DLL!
if not exist "!SC_API_DLL!" echo [WARN] Use compile_noref.bat instead

REM Compile
echo [BUILD] Compiling...
"!MSBUILD!" SpaceClaimBridge.csproj -restore /p:Configuration=Release /v:minimal

if errorlevel 1 (
    echo [ERROR] Compile failed!
    echo Possible causes:
    echo   1. SpaceClaim.Api.V23.dll not found - try compile_noref.bat
    echo   2. .NET Framework 4.8 targeting pack not installed
    echo   3. Project file syntax error
    exit /b 1
)

echo.
echo [SUCCESS] Compile succeeded!
echo [INFO] Output: bin\Release\net48\SpaceClaimBridge.exe

REM Copy to bridge root for easy access
copy /Y "bin\Release\net48\SpaceClaimBridge.exe" "..\SpaceClaimBridge.exe" >nul 2>&1
if exist "..\SpaceClaimBridge.exe" echo [INFO] Copied to: ..\SpaceClaimBridge.exe

:end
endlocal