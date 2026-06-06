#Requires -Version 5.1
<#
.SYNOPSIS
    检查本地控制机环境是否满足 AutoFluid 运行条件。
.DESCRIPTION
    逐项检查以下组件并报告状态：
    - Python 虚拟环境 (.venv)
    - pip 运行时/开发依赖
    - SolidWorks COM 注册
    - ANSYS SpaceClaim 安装
    - Rust 工具链 (cargo/rustc)
    - .NET Framework 4.8 / MSBuild
    - autofluid_config.toml 可解析
    - .env 敏感配置文件
    - SSH 连通性（到远程工作站）
.EXAMPLE
    .\check_local_env.ps1
    .\check_local_env.ps1 -Full
    .\check_local_env.ps1 -SkipSsh
#>

[Diagnostics.CodeAnalysis.SuppressMessageAttribute('PSAvoidUsingWriteHost', '')]
[Diagnostics.CodeAnalysis.SuppressMessageAttribute('PSAvoidUsingPositionalParameters', '')]
param(
    [switch]$Full,
    [switch]$SkipSsh,
    [switch]$NoPause
)

$ErrorActionPreference = "Continue"
$script:ShouldPause = -not $NoPause -and -not [Console]::IsInputRedirected

# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------
$script:PassCount = 0
$script:WarnCount = 0
$script:FailCount = 0

function Write-Check {
    param(
        [string]$Label,
        [string]$Status,
        [string]$Detail = ""
    )
    switch ($Status) {
        "Pass" { $script:PassCount++; $icon = "[OK]"; $color = "Green" }
        "Warn" { $script:WarnCount++; $icon = "[!!]"; $color = "Yellow" }
        "Fail" { $script:FailCount++; $icon = "[XX]"; $color = "Red" }
        "Info" { $icon = "[--]"; $color = "DarkGray" }
        default { $icon = "[--]"; $color = "DarkGray" }
    }
    $msg = "  $icon $Label"
    if ($Detail -ne "") { $msg = "$msg - $Detail" }
    Write-Host $msg -ForegroundColor $color
}

function Exit-CheckScript {
    param([int]$ExitCode)

    if ($script:ShouldPause) {
        Read-Host "按 Enter 键退出" | Out-Null
    }
    exit $ExitCode
}

# ---------------------------------------------------------------------------
# 路径
# ---------------------------------------------------------------------------
$ProjectDir = Split-Path -Parent $PSScriptRoot
$VenvPython = Join-Path $ProjectDir ".venv\Scripts\python.exe"
$ConfigToml = Join-Path $ProjectDir "autofluid_config.toml"
$EnvFile    = Join-Path $ProjectDir ".env"
$ReqFile    = Join-Path $ProjectDir "requirements.txt"
$ReqDevFile = Join-Path $ProjectDir "requirements-dev.txt"

Write-Host ""
Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  AutoFluid 本地环境检测" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  项目目录: $ProjectDir"
Write-Host ""

# ===========================================================================
# 1. Python 虚拟环境
# ===========================================================================
Write-Host "[1/8] Python 虚拟环境" -ForegroundColor Yellow

if (Test-Path -LiteralPath $VenvPython -PathType Leaf) {
    try {
        $pyVer = & $VenvPython --version 2>&1
        Write-Check "Python 虚拟环境" "Pass" $pyVer
    }
    catch {
        Write-Check "Python 虚拟环境" "Fail" "存在但无法执行: $_"
    }
}
else {
    Write-Check "Python 虚拟环境" "Fail" "未找到 $VenvPython"
    Write-Check "" "Info" "运行: python -m venv .venv; .venv\Scripts\pip install -r requirements.txt"
}

# ===========================================================================
# 2. pip 依赖
# ===========================================================================
Write-Host ""
Write-Host "[2/8] pip 依赖检查" -ForegroundColor Yellow

if (Test-Path -LiteralPath $VenvPython -PathType Leaf) {
    # 运行时依赖完整性
    if (Test-Path -LiteralPath $ReqFile -PathType Leaf) {
        $pipCheck = & $VenvPython -m pip check 2>&1
        if ($LASTEXITCODE -eq 0) {
            Write-Check "运行时依赖完整性" "Pass" "所有依赖已安装且无冲突"
        }
        else {
            $pipMsg = ($pipCheck | Select-Object -First 2) -join "; "
            Write-Check "运行时依赖完整性" "Fail" $pipMsg
        }

        # 逐包检查关键依赖
        $criticalPkgs = @("openpyxl", "toml", "python-dotenv", "pywin32", "paramiko")
        foreach ($pkg in $criticalPkgs) {
            $showOut = & $VenvPython -m pip show $pkg 2>&1
            $verLine = $showOut | Select-String "^Version:"
            if ($verLine) {
                $verStr = ($verLine -replace "Version:", "").Trim()
                Write-Check $pkg "Pass" $verStr
            }
            else {
                Write-Check $pkg "Fail" "未安装"
            }
        }
    }
    else {
        Write-Check "requirements.txt" "Fail" "文件不存在"
    }

    # 开发依赖
    if (Test-Path -LiteralPath $ReqDevFile -PathType Leaf) {
        $devPkgs = @("mypy", "ruff", "pytest")
        foreach ($pkg in $devPkgs) {
            $showOut = & $VenvPython -m pip show $pkg 2>&1
            $verLine = $showOut | Select-String "^Version:"
            if ($verLine) {
                $verStr = ($verLine -replace "Version:", "").Trim()
                Write-Check "$pkg (dev)" "Pass" $verStr
            }
            else {
                Write-Check "$pkg (dev)" "Warn" "未安装（开发用）"
            }
        }
    }
}
else {
    Write-Check "pip 依赖" "Fail" "Python 虚拟环境不可用，跳过"
}

# ===========================================================================
# 3. SolidWorks COM
# ===========================================================================
Write-Host ""
Write-Host "[3/8] SolidWorks" -ForegroundColor Yellow

$swRegPath = "HKLM:\SOFTWARE\SolidWorks"
if (Test-Path $swRegPath) {
    Write-Check "SolidWorks 注册表" "Pass" "已检测到注册信息"
}
else {
    Write-Check "SolidWorks 注册表" "Warn" "注册表键不存在（可能为便携安装）"
}

if ($Full -and (Test-Path -LiteralPath $VenvPython -PathType Leaf)) {
    Write-Check "" "Info" "正在测试 COM 连接（可能需要 10-30 秒）..."
    $comScript = Join-Path $env:TEMP "autofluid_com_test.py"
    @'
try:
    import win32com.client
    sw = win32com.client.Dispatch("SldWorks.Application")
    ver = sw.RevisionNumber()
    print("OK:" + str(ver))
except Exception as e:
    print("FAIL:" + str(e))
'@ | Set-Content -LiteralPath $comScript -Encoding UTF8
    $comTest = & $VenvPython $comScript 2>&1
    Remove-Item $comScript -ErrorAction SilentlyContinue
    $comStr = [string]$comTest
    if ($comStr -match "^OK:") {
        $swVer = $comStr -replace "^OK:", ""
        Write-Check "SolidWorks COM 连接" "Pass" "版本 $swVer"
    }
    else {
        $errMsg = $comStr -replace "^FAIL:", ""
        Write-Check "SolidWorks COM 连接" "Warn" "连接失败: $errMsg"
    }
}
elseif (-not $Full) {
    Write-Check "SolidWorks COM 连接" "Info" "跳过（使用 -Full 启用 COM 测试）"
}

# ===========================================================================
# 4. ANSYS SpaceClaim
# ===========================================================================
Write-Host ""
Write-Host "[4/8] ANSYS SpaceClaim" -ForegroundColor Yellow

$scVersions = @(
    @{ Ver = "v231"; Name = "2023 R1" },
    @{ Ver = "v232"; Name = "2023 R2" },
    @{ Ver = "v241"; Name = "2024 R1" }
)
$scFound = $false
foreach ($sc in $scVersions) {
    $scExe = "C:\Program Files\ANSYS Inc\" + $sc.Ver + "\SCDM\SpaceClaim.exe"
    if (Test-Path -LiteralPath $scExe -PathType Leaf) {
        Write-Check ("SpaceClaim " + $sc.Name + " (" + $sc.Ver + ")") "Pass" $scExe
        $scFound = $true
    }
}
if (-not $scFound) {
    Write-Check "SpaceClaim" "Fail" "未检测到任何版本（v231/v232/v241）"
}

# ===========================================================================
# 5. Rust 工具链
# ===========================================================================
Write-Host ""
Write-Host "[5/8] Rust 工具链" -ForegroundColor Yellow

$cargoCmd = Get-Command cargo -ErrorAction SilentlyContinue
if ($cargoCmd) {
    $cargoVer = & cargo --version 2>&1
    Write-Check "Cargo" "Pass" $cargoVer
    $rustcVer = & rustc --version 2>&1
    Write-Check "rustc" "Pass" $rustcVer

    $targets = & rustup target list --installed 2>&1
    $targetStr = [string]$targets
    if ($targetStr -match "x86_64-pc-windows-msvc") {
        Write-Check "MSVC 目标" "Pass" "x86_64-pc-windows-msvc"
    }
    else {
        Write-Check "MSVC 目标" "Warn" "未安装，运行: rustup target add x86_64-pc-windows-msvc"
    }
}
else {
    Write-Check "Rust 工具链" "Fail" "cargo 未找到"
    Write-Check "" "Info" "安装: winget install Rustlang.Rustup 或访问 https://rustup.rs"
}

# ===========================================================================
# 6. .NET Framework / MSBuild
# ===========================================================================
Write-Host ""
Write-Host "[6/8] .NET Framework / MSBuild" -ForegroundColor Yellow

$netRegPath = "HKLM:\SOFTWARE\Microsoft\NET Framework Setup\NDP\v4\Full"
if (Test-Path $netRegPath) {
    $netProps = Get-ItemProperty $netRegPath -ErrorAction SilentlyContinue
    $netRelease = $netProps.Release
    if ($netRelease -ge 528040) {
        Write-Check ".NET Framework 4.8" "Pass" "Release $netRelease"
    }
    elseif ($netRelease) {
        Write-Check ".NET Framework" "Warn" "Release $netRelease（需 4.8 即 528040+）"
    }
}
else {
    Write-Check ".NET Framework 4.8" "Fail" "未检测到"
}

$msbuildCmd = Get-Command msbuild -ErrorAction SilentlyContinue
if ($msbuildCmd) {
    Write-Check "MSBuild" "Pass" $msbuildCmd.Source
}
else {
    $vsWhere = "${env:ProgramFiles(x86)}\Microsoft Visual Studio\Installer\vswhere.exe"
    $msbuildPath = ""
    if (Test-Path -LiteralPath $vsWhere -PathType Leaf) {
        $vsPath = & $vsWhere -latest -requires Microsoft.Component.MSBuild -property installationPath 2>&1
        $vsPathStr = [string]$vsPath
        if ($vsPathStr -and $vsPathStr.Length -gt 0) {
            $msbuildPath = Join-Path $vsPathStr "MSBuild\Current\Bin\MSBuild.exe"
        }
        if ($vsPathStr -and (Test-Path $msbuildPath)) {
            Write-Check "MSBuild" "Pass" "Visual Studio: $vsPathStr"
        }
        else {
            $msbuildPath = ""
        }
    }

    if (-not $msbuildPath) {
        $msbuildCandidates = @(
            "C:\Program Files\Microsoft Visual Studio\2022\Community\MSBuild\Current\Bin\MSBuild.exe",
            "C:\Program Files\Microsoft Visual Studio\2022\Professional\MSBuild\Current\Bin\MSBuild.exe",
            "C:\Program Files\Microsoft Visual Studio\2022\Enterprise\MSBuild\Current\Bin\MSBuild.exe",
            "C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\MSBuild\Current\Bin\MSBuild.exe",
            "C:\Program Files\Microsoft Visual Studio\2019\Community\MSBuild\Current\Bin\MSBuild.exe",
            "C:\Program Files\Microsoft Visual Studio\2019\Professional\MSBuild\Current\Bin\MSBuild.exe",
            "C:\Program Files (x86)\Microsoft Visual Studio\2019\BuildTools\MSBuild\Current\Bin\MSBuild.exe"
        )
        foreach ($candidate in $msbuildCandidates) {
            if (Test-Path -LiteralPath $candidate -PathType Leaf) {
                $msbuildPath = $candidate
                break
            }
        }
    }

    if ($msbuildPath) {
        Write-Check "MSBuild" "Pass" $msbuildPath
    }
    else {
        Write-Check "MSBuild" "Warn" "未找到 MSBuild"
        Write-Check "" "Info" "安装: winget install Microsoft.VisualStudio.2022.BuildTools"
    }
}

# ===========================================================================
# 7. 项目配置文件
# ===========================================================================
Write-Host ""
Write-Host "[7/8] 项目配置" -ForegroundColor Yellow

if (Test-Path -LiteralPath $ConfigToml -PathType Leaf) {
    Write-Check "autofluid_config.toml" "Pass" "文件存在"
    if (Test-Path -LiteralPath $VenvPython -PathType Leaf) {
        $tomlScript = Join-Path $env:TEMP "autofluid_toml_check.py"
        @'
import sys, toml
_cfg = r"__CONFIG_TOML_PATH__"
try:
    cfg_data = toml.load(_cfg)
    sections = list(cfg_data.keys())
    print("OK:" + ",".join(sections))
except Exception as e:
    print("FAIL:" + str(e))
'@ -replace '__CONFIG_TOML_PATH__', $ConfigToml | Set-Content -LiteralPath $tomlScript -Encoding UTF8
        $tomlCheck = & $VenvPython $tomlScript 2>&1
        Remove-Item $tomlScript -ErrorAction SilentlyContinue
        $tomlStr = [string]$tomlCheck
        if ($tomlStr -match "^OK:") {
            $sections = $tomlStr -replace "^OK:", ""
            Write-Check "TOML 解析" "Pass" "Sections: $sections"
        }
        else {
            $errMsg = $tomlStr -replace "^FAIL:", ""
            Write-Check "TOML 解析" "Fail" $errMsg
        }
    }
}
else {
    Write-Check "autofluid_config.toml" "Fail" "文件不存在"
}

$sshPassword = [Environment]::GetEnvironmentVariable("AUTOFLUID_SSH_PASSWORD")
if (Test-Path -LiteralPath $EnvFile -PathType Leaf) {
    $envContent = Get-Content $EnvFile -Raw
    if ($envContent -match "AUTOFLUID_SSH_PASSWORD=") {
        $pwdLine = ($envContent -split "`n") | Where-Object { $_ -match "AUTOFLUID_SSH_PASSWORD=" } | Select-Object -First 1
        $pwdValue = ($pwdLine -split "=", 2)[1].Trim().Trim('"').Trim("'")
        if ($pwdValue -and $pwdValue -ne "your_password" -and $pwdValue.Length -gt 0) {
            $sshPassword = $pwdValue
            Write-Check ".env (SSH 密码)" "Pass" "已设置"
        }
        else {
            Write-Check ".env (SSH 密码)" "Warn" "值为空或为占位符"
        }
    }
    else {
        Write-Check ".env (SSH 密码)" "Warn" "文件存在但未找到 AUTOFLUID_SSH_PASSWORD"
    }
}
else {
    Write-Check ".env" "Warn" "文件不存在（SSH 密码需通过环境变量设置）"
}

# ===========================================================================
# 8. SSH 连通性
# ===========================================================================
Write-Host ""
Write-Host "[8/8] SSH 连通性" -ForegroundColor Yellow

if ($SkipSsh) {
    Write-Check "SSH 连通性" "Info" "已跳过（-SkipSsh）"
}
else {
    $sshCmd = Get-Command ssh -ErrorAction SilentlyContinue
    if (-not $sshCmd) {
        Write-Check "SSH 客户端" "Fail" "ssh 命令不可用"
    }
    else {
        Write-Check "SSH 客户端" "Pass" $sshCmd.Source

        $remoteHost = "172.17.135.240"
        $remoteUser = "ps"
        $remotePort = "22"
        if (Test-Path -LiteralPath $ConfigToml -PathType Leaf) {
            $tomlContent = Get-Content $ConfigToml -Raw
            if ($tomlContent -match 'host\s*=\s*"([^"]+)"') { $remoteHost = $Matches[1] }
            if ($tomlContent -match 'username\s*=\s*"([^"]+)"') { $remoteUser = $Matches[1] }
            if ($tomlContent -match 'port\s*=\s*(\d+)') { $remotePort = $Matches[1] }
        }

        Write-Check "" "Info" "正在测试 SSH 连接 ($remoteUser@$remoteHost`:$remotePort)..."
        if ($sshPassword -and (Test-Path -LiteralPath $VenvPython -PathType Leaf)) {
            $sshScript = Join-Path $env:TEMP "autofluid_ssh_check.py"
            @'
import os
import sys
import paramiko

host = os.environ["AUTOFLUID_CHECK_HOST"]
port = int(os.environ["AUTOFLUID_CHECK_PORT"])
user = os.environ["AUTOFLUID_CHECK_USER"]
password = os.environ["AUTOFLUID_CHECK_PASSWORD"]

client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
try:
    client.connect(
        hostname=host,
        port=port,
        username=user,
        password=password,
        timeout=5,
        banner_timeout=5,
        auth_timeout=5,
    )
    _stdin, stdout, _stderr = client.exec_command("echo SSH_OK", timeout=5)
    output = stdout.read().decode("utf-8", errors="replace")
    if "SSH_OK" not in output:
        print("missing SSH_OK")
        sys.exit(1)
    print("SSH_OK")
finally:
    client.close()
'@ | Set-Content -LiteralPath $sshScript -Encoding UTF8
            $env:AUTOFLUID_CHECK_HOST = $remoteHost
            $env:AUTOFLUID_CHECK_PORT = $remotePort
            $env:AUTOFLUID_CHECK_USER = $remoteUser
            $env:AUTOFLUID_CHECK_PASSWORD = $sshPassword
            $sshTest = & $VenvPython $sshScript 2>&1
            $sshExit = $LASTEXITCODE
            Remove-Item $sshScript -ErrorAction SilentlyContinue
            Remove-Item Env:\AUTOFLUID_CHECK_HOST, Env:\AUTOFLUID_CHECK_PORT, Env:\AUTOFLUID_CHECK_USER, Env:\AUTOFLUID_CHECK_PASSWORD -ErrorAction SilentlyContinue
            $sshStr = [string]$sshTest
        }
        else {
            $sshTest = & ssh -o ConnectTimeout=5 -o BatchMode=yes -o StrictHostKeyChecking=no -p $remotePort "$remoteUser@$remoteHost" "echo SSH_OK" 2>&1
            $sshExit = $LASTEXITCODE
            $sshStr = [string]$sshTest
        }
        if ($sshExit -eq 0 -and $sshStr -match "SSH_OK") {
            Write-Check "SSH 连接" "Pass" "$remoteUser@$remoteHost`:$remotePort"
        }
        else {
            Write-Check "SSH 连接" "Fail" "无法连接到 $remoteUser@$remoteHost`:$remotePort"
            Write-Check "" "Info" "检查: 网络连通性、远程 SSH 服务、防火墙规则"
        }
    }
}

# ===========================================================================
# 汇总
# ===========================================================================
Write-Host ""
Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  检测结果汇总" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  通过: $script:PassCount" -ForegroundColor Green
Write-Host "  警告: $script:WarnCount" -ForegroundColor Yellow
Write-Host "  失败: $script:FailCount" -ForegroundColor Red
Write-Host ""

if ($script:FailCount -gt 0) {
    Write-Host "  存在 $script:FailCount 项失败，请修复后重试。" -ForegroundColor Red
    Exit-CheckScript 1
}
elseif ($script:WarnCount -gt 0) {
    Write-Host "  存在 $script:WarnCount 项警告，部分功能可能受限。" -ForegroundColor Yellow
    Exit-CheckScript 0
}
else {
    Write-Host "  所有检查通过！" -ForegroundColor Green
    Exit-CheckScript 0
}
