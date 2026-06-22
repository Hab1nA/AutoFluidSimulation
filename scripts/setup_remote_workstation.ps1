#Requires -RunAsAdministrator
#Requires -Version 5.1
<#
.SYNOPSIS
    在远程工作站上一键搭建 AutoFluid 运行环境。
.DESCRIPTION
    自动完成以下步骤：
    1. 安装并配置 OpenSSH Server（SSH 服务 + 防火墙规则）
    2. 下载安装 Miniconda（如未安装）
    3. 创建 pyfluent Conda 环境 (Python 3.10) 并安装 ansys-fluent-core
    4. 创建远程工作目录结构
    5. 运行环境验证

    注意：ANSYS Fluent 2024 R1 为商业软件，需手动安装，本脚本无法自动部署。
.PARAMETER RemoteUser
    远程工作站用户名（用于 SSH 访问配置）。默认 "ps"。
.PARAMETER ConfigPath
    autofluid_config.toml 路径。脚本会读取 [remote_config] 中的远程路径、Conda 和 MPI 配置；读取失败会直接退出。
.PARAMETER PythonVersion
    Conda 环境中的 Python 版本。默认 "3.10"。
.PARAMETER PyFluentVersion
    ansys-fluent-core 版本。默认 "0.37.2"。
.PARAMETER SkipSsh
    跳过 OpenSSH Server 配置步骤。
.PARAMETER SkipConda
    跳过 Conda/PyFluent 安装步骤。
.PARAMETER SkipDirs
    跳过目录创建步骤。
.EXAMPLE
    # 在远程工作站上以管理员身份运行：
    .\setup_remote_workstation.ps1

    # 按项目配置文件中的 [remote_config] 创建/检查目录：
    .\setup_remote_workstation.ps1 -ConfigPath "D:\xkz_1020\autofluid_config.toml"
#>

[Diagnostics.CodeAnalysis.SuppressMessageAttribute('PSAvoidUsingWriteHost', '')]
param(
    [string]$RemoteUser = "ps",
    [string]$ConfigPath = "",
    [string]$WorkstationId = "",
    [string]$PythonVersion = "3.10",
    [string]$PyFluentVersion = "0.37.2",
    [switch]$SkipSsh,
    [switch]$SkipConda,
    [switch]$SkipDirs,
    [switch]$NoPause
)

$ErrorActionPreference = "Stop"
$script:ShouldPause = -not $NoPause -and -not [Console]::IsInputRedirected
$script:ConfigPathInput = $ConfigPath

# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

function Write-Step {
    param([int]$Num, [int]$Total, [string]$Message)
    Write-Host "`n[Step $Num/$Total] $Message" -ForegroundColor Yellow
    Write-Host ("-" * 60) -ForegroundColor DarkGray
}

function Write-OK {
    param([string]$Message)
    Write-Host "  [OK] $Message" -ForegroundColor Green
}

function Write-Info {
    param([string]$Message)
    Write-Host "  [--] $Message" -ForegroundColor DarkGray
}

function Write-Warn {
    param([string]$Message)
    Write-Host "  [!!] $Message" -ForegroundColor Yellow
}

function Write-Err {
    param([string]$Message)
    Write-Host "  [XX] $Message" -ForegroundColor Red
}

function Exit-SetupScript {
    param([int]$ExitCode)

    if ($script:ShouldPause) {
        Read-Host "按 Enter 键退出" | Out-Null
    }
    exit $ExitCode
}

function Test-CommandExist {
    param([string]$Command)
    $null -ne (Get-Command $Command -ErrorAction SilentlyContinue)
}

function Join-CommandArgument {
    param([string[]]$Arguments)

    (($Arguments | ForEach-Object {
        if ($_ -match '[\s"]') {
            '"' + ($_ -replace '"', '\"') + '"'
        }
        else {
            $_
        }
    }) -join " ")
}

function Invoke-ExternalCommand {
    param(
        [string]$FilePath,
        [string[]]$Arguments,
        [int]$TimeoutSec = 600
    )

    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $FilePath
    $psi.Arguments = Join-CommandArgument $Arguments
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true

    $proc = [System.Diagnostics.Process]::Start($psi)
    $outTask = $proc.StandardOutput.ReadToEndAsync()
    $errTask = $proc.StandardError.ReadToEndAsync()
    if (-not $proc.WaitForExit($TimeoutSec * 1000)) {
        try { $proc.Kill() } catch { Write-Verbose "无法终止超时进程: $_" }
        $proc.WaitForExit()
        return @{ ExitCode = -1; Output = "命令超时（超过 $TimeoutSec 秒）" }
    }

    $output = $outTask.Result
    $errOutput = $errTask.Result
    if ($errOutput) {
        if ($output) { $output = "$output`n$errOutput" } else { $output = $errOutput }
    }
    return @{ ExitCode = $proc.ExitCode; Output = $output.Trim() }
}

function Invoke-Conda {
    param(
        [string[]]$Arguments,
        [int]$TimeoutSec = 600
    )

    Invoke-ExternalCommand -FilePath $CondaExe -Arguments $Arguments -TimeoutSec $TimeoutSec
}

function Resolve-ConfigPath {
    if ($script:ConfigPathInput) {
        return $script:ConfigPathInput
    }

    $candidates = @(
        (Join-Path $PSScriptRoot "autofluid_config.toml"),
        (Join-Path $PSScriptRoot "..\autofluid_config.toml"),
        (Join-Path (Get-Location) "autofluid_config.toml")
    )
    foreach ($candidate in $candidates) {
        if (Test-Path $candidate) {
            return (Resolve-Path $candidate).Path
        }
    }
    Write-Err "未找到 autofluid_config.toml。请使用 -ConfigPath 指定配置文件。"
    Exit-SetupScript 1
}

function Read-TomlSection {
    param(
        [string]$Path,
        [string]$SectionName
    )

    $values = @{}
    if (-not $Path -or -not (Test-Path $Path)) {
        return $values
    }

    $inSection = $false
    foreach ($line in Get-Content -Path $Path) {
        $trimmed = $line.Trim()
        if (-not $trimmed -or $trimmed.StartsWith("#")) {
            continue
        }
        if ($trimmed -match '^\[(.+)\]$') {
            $inSection = ($Matches[1] -eq $SectionName)
            continue
        }
        if (-not $inSection) {
            continue
        }
        if ($trimmed -match '^([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.+)$') {
            $key = $Matches[1]
            $value = $Matches[2].Trim()
            if (($value.StartsWith("'") -and $value.EndsWith("'")) -or
                ($value.StartsWith('"') -and $value.EndsWith('"'))) {
                $value = $value.Substring(1, $value.Length - 2)
            }
            $values[$key] = $value
        }
    }
    return $values
}

function Read-RemoteConfig {
    param([string]$Path)

    Read-TomlSection -Path $Path -SectionName "remote_config"
}

function Read-TomlWorkstation {
    param(
        [string]$Path,
        [string]$Id
    )

    $values = @{}
    if (-not $Id) { return $values }

    $inWorkstation = $false
    $current = @{}
    foreach ($line in Get-Content -Path $Path) {
        $trimmed = $line.Trim()
        if ($trimmed -match '^\[\[workstations\]\]$') {
            if ($inWorkstation -and $current.ContainsKey("id") -and $current["id"] -eq $Id) { return $current }
            $inWorkstation = $true
            $current = @{}
            continue
        }
        if ($trimmed -match '^\[' -and $inWorkstation) {
            if ($current.ContainsKey("id") -and $current["id"] -eq $Id) { return $current }
            $inWorkstation = $false
            continue
        }
        if (-not $inWorkstation -or -not $trimmed -or $trimmed.StartsWith("#")) { continue }
        if ($trimmed -match '^([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.+)$') {
            $key = $Matches[1]
            $value = $Matches[2].Trim()
            if (($value.StartsWith("'") -and $value.EndsWith("'")) -or
                ($value.StartsWith('"') -and $value.EndsWith('"'))) {
                $value = $value.Substring(1, $value.Length - 2)
            }
            $current[$key] = $value
        }
    }
    if ($inWorkstation -and $current.ContainsKey("id") -and $current["id"] -eq $Id) { return $current }
    return $values
}

function Merge-ConfigValues {
    param(
        [hashtable]$Base,
        [hashtable]$Overrides
    )

    foreach ($key in $Overrides.Keys) {
        if ($Overrides[$key]) { $Base[$key] = $Overrides[$key] }
    }
    return $Base
}

function Get-RequiredPositiveInteger {
    param(
        [hashtable]$Values,
        [string]$Key,
        [string]$SectionName
    )

    if (-not $Values.ContainsKey($Key) -or -not $Values[$Key]) {
        Write-Err "配置文件 [$SectionName] 缺少必需字段: $Key"
        Exit-SetupScript 1
    }

    $parsedValue = 0
    if (-not [int]::TryParse([string]$Values[$Key], [ref]$parsedValue) -or $parsedValue -le 0) {
        Write-Err "配置文件 [$SectionName].$Key 必须是正整数，当前值: $($Values[$Key])"
        Exit-SetupScript 1
    }
    return $parsedValue
}

function Initialize-RemoteConfigValue {
    $resolvedConfigPath = Resolve-ConfigPath
    if (-not (Test-Path $resolvedConfigPath)) {
        Write-Err "配置文件不存在: $resolvedConfigPath"
        Exit-SetupScript 1
    }

    $remoteConfig = Read-RemoteConfig $resolvedConfigPath
    $workstationConfig = Read-TomlWorkstation -Path $resolvedConfigPath -Id $WorkstationId
    if ($workstationConfig.Count -gt 0) { $remoteConfig = Merge-ConfigValues -Base $remoteConfig -Overrides $workstationConfig }
    if ($remoteConfig.Count -eq 0) {
        Write-Err "未能从配置文件读取 [remote_config]: $resolvedConfigPath"
        Exit-SetupScript 1
    }

    $requiredKeys = @(
        "working_dir",
        "scripts_dir",
        "ref_files_dir",
        "scdoc_dir",
        "msh_dir",
        "result_dir",
        "flag_dir",
        "conda_env",
        "conda_exe",
        "mpi_bin_dir"
    )
    $missingKeys = @()
    foreach ($key in $requiredKeys) {
        if (-not $remoteConfig.ContainsKey($key) -or -not $remoteConfig[$key]) {
            $missingKeys += $key
        }
    }
    if ($missingKeys.Count -gt 0) {
        Write-Err "配置文件 [remote_config] 缺少必需字段: $($missingKeys -join ', ')"
        Exit-SetupScript 1
    }

    $meshingConfig = Read-TomlSection -Path $resolvedConfigPath -SectionName "meshing"
    $solverConfig = Read-TomlSection -Path $resolvedConfigPath -SectionName "solver"

    if ($WorkstationId -and $workstationConfig.Count -eq 0) {
        Write-Warn "未找到工作站配置 $WorkstationId，使用 [remote_config]"
    }
    Write-Info "已读取远程配置: $resolvedConfigPath"
    $script:CondaEnv = $remoteConfig["conda_env"]
    $script:CondaExe = $remoteConfig["conda_exe"]
    $script:WorkingDir = $remoteConfig["working_dir"]
    $script:ScriptsDir = $remoteConfig["scripts_dir"]
    $script:RefFilesDir = $remoteConfig["ref_files_dir"]
    $script:ScdocDir = $remoteConfig["scdoc_dir"]
    $script:MshDir = $remoteConfig["msh_dir"]
    $script:ResultDir = $remoteConfig["result_dir"]
    $script:FlagDir = $remoteConfig["flag_dir"]
    if ($remoteConfig.ContainsKey("fluent_path") -and $remoteConfig["fluent_path"]) {
        $script:FluentPath = $remoteConfig["fluent_path"]
    }
    else {
        $script:FluentPath = "C:\Program Files\ANSYS Inc\v241\fluent\ntbin\win64\fluent.exe"
    }
    $script:MpiBinDir = $remoteConfig["mpi_bin_dir"]
    $script:MeshingProcessorCount = Get-RequiredPositiveInteger -Values $meshingConfig -Key "meshing_processor_count" -SectionName "meshing"
    $script:SolverProcessorCount = Get-RequiredPositiveInteger -Values $solverConfig -Key "solver_processor_count" -SectionName "solver"
    $script:RequiredProcessorCount = [Math]::Max($script:MeshingProcessorCount, $script:SolverProcessorCount)
}

function Initialize-RemotePathValue {
    Initialize-RemoteConfigValue
}

function Get-CondaPackageVersion {
    param([string]$PackageName)

    $showResult = Invoke-Conda @("run", "--no-capture-output", "-n", $CondaEnv, "pip", "show", $PackageName) 120
    if ($showResult.ExitCode -ne 0) { return "" }

    $versionLine = ($showResult.Output -split "`r?`n") | Where-Object { $_ -match '^Version:\s*(.+)$' } | Select-Object -First 1
    if ($versionLine -and $versionLine -match '^Version:\s*(.+)$') {
        return $Matches[1].Trim()
    }
    return ""
}

function Sync-CondaPackageVersion {
    param(
        [string]$PackageName,
        [string]$Version,
        [string]$DisplayName = $PackageName
    )

    $currentVersion = Get-CondaPackageVersion $PackageName
    if ($currentVersion -eq $Version) {
        Write-OK "$DisplayName 已安装: $currentVersion"
        return $true
    }

    if ($currentVersion) {
        Write-Warn "$DisplayName 当前版本 $currentVersion，将卸载并安装 $Version"
        $uninstallResult = Invoke-Conda @("run", "--no-capture-output", "-n", $CondaEnv, "pip", "uninstall", "-y", $PackageName) 600
        if ($uninstallResult.ExitCode -ne 0) {
            Write-Err "卸载 $DisplayName 失败 (ExitCode: $($uninstallResult.ExitCode))"
            if ($uninstallResult.Output) { Write-Info $uninstallResult.Output }
            return $false
        }
    }
    else {
        Write-Info "$DisplayName 未安装，将安装 $Version"
    }

    $installResult = Invoke-Conda @("run", "--no-capture-output", "-n", $CondaEnv, "pip", "install", "$PackageName==$Version") 1800
    if ($installResult.ExitCode -ne 0) {
        Write-Err "安装 $DisplayName==$Version 失败 (ExitCode: $($installResult.ExitCode))"
        if ($installResult.Output) { Write-Info $installResult.Output }
        return $false
    }

    $verifiedVersion = Get-CondaPackageVersion $PackageName
    if ($verifiedVersion -eq $Version) {
        Write-OK "$DisplayName 已安装: $verifiedVersion"
        return $true
    }

    Write-Err "$DisplayName 安装后版本不匹配：当前 $verifiedVersion，期望 $Version"
    return $false
}

function Test-CondaTosRequired {
    param([string]$Output)

    $Output -match "CondaToSNonInteractiveError" -or
        $Output -match "Terms of Service have not been accepted"
}

function Confirm-CondaTosAcceptance {
    if ([Console]::IsInputRedirected) {
        Write-Warn "当前为非交互执行，无法确认 Conda Terms of Service。"
        return $false
    }

    Write-Warn "Conda 需要先接受 Anaconda channel Terms of Service 才能继续。"
    Write-Host "  将执行以下命令：" -ForegroundColor Yellow
    Write-Host "    conda tos accept --override-channels --channel https://repo.anaconda.com/pkgs/main" -ForegroundColor White
    Write-Host "    conda tos accept --override-channels --channel https://repo.anaconda.com/pkgs/r" -ForegroundColor White
    Write-Host "    conda tos accept --override-channels --channel https://repo.anaconda.com/pkgs/msys2" -ForegroundColor White
    $answer = Read-Host "是否接受并继续？输入 y 确认"
    return $answer -match '^(y|Y|yes|YES)$'
}

function Invoke-CondaTermsOfServiceAcceptance {
    $channels = @(
        "https://repo.anaconda.com/pkgs/main",
        "https://repo.anaconda.com/pkgs/r",
        "https://repo.anaconda.com/pkgs/msys2"
    )

    foreach ($channel in $channels) {
        Write-Info "接受 Conda Terms of Service: $channel"
        $tosResult = Invoke-Conda @("tos", "accept", "--override-channels", "--channel", $channel) 120
        if ($tosResult.ExitCode -ne 0) {
            Write-Err "接受 Conda Terms of Service 失败 (ExitCode: $($tosResult.ExitCode))"
            if ($tosResult.Output) { Write-Info $tosResult.Output }
            return $false
        }
    }
    Write-OK "Conda Terms of Service 已接受"
    return $true
}

$totalSteps = 4
if ($SkipSsh)   { $totalSteps-- }
if ($SkipConda) { $totalSteps-- }
if ($SkipDirs)  { $totalSteps-- }
if ($totalSteps -eq 0) { $totalSteps = 1 }

$stepNum = 0
Initialize-RemotePathValue

Write-Host ""
Write-Host "========================================================" -ForegroundColor Cyan
Write-Host "  AutoFluid 远程工作站环境搭建" -ForegroundColor Cyan
Write-Host "========================================================" -ForegroundColor Cyan
Write-Host "  远程用户:   $RemoteUser"
Write-Host "  Fluent工作: $WorkingDir"
Write-Host "  脚本目录:   $ScriptsDir"
Write-Host "  引用文件:   $RefFilesDir"
Write-Host "  Conda 环境: $CondaEnv"
Write-Host "  Python:     $PythonVersion"
Write-Host "  PyFluent:   ansys-fluent-core==$PyFluentVersion"
Write-Host "  Conda 路径: $CondaExe"
Write-Host "========================================================" -ForegroundColor Cyan

# ===========================================================================
# Step 1: OpenSSH Server
# ===========================================================================
if (-not $SkipSsh) {
    $stepNum++
    Write-Step -Num $stepNum -Total $totalSteps -Message "配置 OpenSSH Server"

    # 1a. 安装 OpenSSH Server
    $sshCap = Get-WindowsCapability -Online | Where-Object Name -like 'OpenSSH.Server*'
    if ($sshCap.State -ne 'Installed') {
        Write-Info "正在安装 OpenSSH Server..."
        try {
            Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0 | Out-Null
            Write-OK "OpenSSH Server 已安装"
        }
        catch {
            Write-Err "安装 OpenSSH Server 失败: $_"
            Write-Info "请手动运行: Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0"
        }
    }
    else {
        Write-OK "OpenSSH Server 已安装"
    }

    # 1b. 启动并设为自动运行
    try {
        $sshd = Get-Service sshd -ErrorAction SilentlyContinue
        if ($sshd) {
            if ($sshd.Status -ne 'Running') {
                Start-Service sshd
                Write-OK "SSH 服务已启动"
            }
            else {
                Write-OK "SSH 服务已在运行"
            }
            Set-Service -Name sshd -StartupType 'Automatic'
            Write-OK "SSH 服务已设为自动启动"
        }
        else {
            Write-Warn "未找到 sshd 服务（可能需要重启系统）"
        }
    }
    catch {
        Write-Warn "配置 SSH 服务时出错: $_"
    }

    # 1c. 防火墙规则
    $fwRule = Get-NetFirewallRule -Name 'OpenSSH-Server-In-TCP' -ErrorAction SilentlyContinue
    if (-not $fwRule) {
        try {
            New-NetFirewallRule -Name 'OpenSSH-Server-In-TCP' `
                -DisplayName 'OpenSSH Server (sshd)' `
                -Enabled True -Direction Inbound -Protocol TCP `
                -Action Allow -LocalPort 22 | Out-Null
            Write-OK "防火墙规则已添加 (TCP 22)"
        }
        catch {
            Write-Warn "添加防火墙规则失败: $_"
            Write-Info "请手动运行: New-NetFirewallRule -Name 'OpenSSH-Server-In-TCP' ..."
        }
    }
    else {
        Write-OK "防火墙规则已存在 (TCP 22)"
    }

    # 1d. 用户检查
    $userExists = Get-LocalUser -Name $RemoteUser -ErrorAction SilentlyContinue
    if ($userExists) {
        Write-OK "用户 '$RemoteUser' 已存在"
    }
    else {
        Write-Warn "用户 '$RemoteUser' 不存在"
        Write-Info "如需创建: net user $RemoteUser <密码> /add"
    }
}

# ===========================================================================
# Step 2: Conda + pyfluent 环境
# ===========================================================================
if (-not $SkipConda) {
    $stepNum++
    Write-Step -Num $stepNum -Total $totalSteps -Message "配置 Conda 环境 '$CondaEnv'"

    # 2a. 检查/安装 Conda
    $condaReady = $false
    if (Test-Path $CondaExe) {
        try {
            $condaVersion = Invoke-Conda @("--version") 60
            if ($condaVersion.ExitCode -eq 0) {
                Write-OK "Conda 已安装: $($condaVersion.Output)"
                $condaReady = $true
            }
            else {
                Write-Warn "Conda 文件存在但无法执行: $($condaVersion.Output)"
            }
        }
        catch {
            Write-Warn "Conda 文件存在但无法执行: $_"
        }
    }

    if (-not $condaReady) {
        Write-Info "配置中的 Conda 未找到，正在下载安装 Miniconda..."
            try {
                $minicondaUrl = "https://repo.anaconda.com/miniconda/Miniconda3-latest-Windows-x86_64.exe"
                $installerPath = Join-Path $env:TEMP "Miniconda3-latest.exe"

                # 下载
                Write-Info "正在下载 Miniconda 安装程序..."
                [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
                Invoke-WebRequest -Uri $minicondaUrl -OutFile $installerPath -UseBasicParsing

                # 安装（AllUsers 模式，安装到 C:\ProgramData\anaconda3）
                Write-Info "正在安装 Miniconda（这可能需要几分钟）..."
                $installArgs = "/InstallationType=AllUsers /AddToPath=1 /RegisterPython=1 /S /D=C:\ProgramData\anaconda3"
                $installProc = Start-Process -FilePath $installerPath `
                    -ArgumentList $installArgs -Wait -PassThru

                if ($installProc.ExitCode -eq 0) {
                    if (Test-Path $CondaExe) {
                        Write-OK "Miniconda 已安装，配置中的 Conda 可用: $CondaExe"
                        $condaReady = $true
                    }
                    else {
                        Write-Err "Miniconda 安装完成，但配置中的 conda_exe 仍不存在: $CondaExe"
                    }
                }
                else {
                    Write-Err "Miniconda 安装失败 (ExitCode: $($installProc.ExitCode))"
                }

                # 清理安装程序
                Remove-Item $installerPath -ErrorAction SilentlyContinue
            }
            catch {
                Write-Err "Miniconda 安装失败: $_"
                Write-Info "请手动安装: winget install Anaconda.Miniconda3"
                Write-Info "或下载: https://docs.conda.io/en/latest/miniconda.html"
            }
    }

    if (-not $condaReady) {
        Write-Err "Conda 不可用，跳过环境创建"
        Write-Info "请手动安装 Conda 后重新运行此脚本"
    }
    else {
        # 2b. 创建 conda 环境
        Write-Info "检查 Conda 环境 '$CondaEnv'..."
        $envList = Invoke-Conda @("env", "list") 120
        $envReady = $false
        if ($envList.ExitCode -eq 0 -and $envList.Output -match [regex]::Escape($CondaEnv)) {
            $envReady = $true
            Write-OK "Conda 环境 '$CondaEnv' 已存在"

            # 检查 Python 版本
            $currentPyVer = Invoke-Conda @("run", "--no-capture-output", "-n", $CondaEnv, "python", "--version") 120
            if ($currentPyVer.ExitCode -eq 0) {
                Write-Info "当前 Python 版本: $($currentPyVer.Output)"
            }
            else {
                Write-Warn "无法获取当前 Python 版本: $($currentPyVer.Output)"
            }
        }
        else {
            if ($envList.ExitCode -ne 0) {
                Write-Warn "获取 Conda 环境列表失败: $($envList.Output)"
            }
            Write-Info "正在创建 Conda 环境 '$CondaEnv' (Python $PythonVersion)..."
            try {
                $createEnv = Invoke-Conda @("create", "-n", $CondaEnv, "python=$PythonVersion", "-y") 1800
                if ($createEnv.ExitCode -eq 0) {
                    Write-OK "Conda 环境 '$CondaEnv' 已创建 (Python $PythonVersion)"
                    $envReady = $true
                }
                else {
                    if (Test-CondaTosRequired $createEnv.Output) {
                        Write-Warn "创建 Conda 环境需要先接受 Conda Terms of Service。"
                        if (Confirm-CondaTosAcceptance) {
                            if (Invoke-CondaTermsOfServiceAcceptance) {
                                Write-Info "正在重新创建 Conda 环境 '$CondaEnv'..."
                                $createEnv = Invoke-Conda @("create", "-n", $CondaEnv, "python=$PythonVersion", "-y") 1800
                                if ($createEnv.ExitCode -eq 0) {
                                    Write-OK "Conda 环境 '$CondaEnv' 已创建 (Python $PythonVersion)"
                                    $envReady = $true
                                }
                                else {
                                    Write-Err "重试创建 Conda 环境失败 (ExitCode: $($createEnv.ExitCode))"
                                    if ($createEnv.Output) { Write-Info $createEnv.Output }
                                }
                            }
                        }
                        else {
                            Write-Err "用户未确认 Conda Terms of Service，无法创建环境"
                        }
                    }
                    else {
                        Write-Err "创建 Conda 环境失败 (ExitCode: $($createEnv.ExitCode))"
                        if ($createEnv.Output) { Write-Info $createEnv.Output }
                    }
                }
            }
            catch {
                Write-Err "创建 Conda 环境失败: $_"
            }
        }

        if (-not $envReady) {
            Write-Err "Conda 环境 '$CondaEnv' 不可用，跳过 PyFluent 安装"
        }
        else {
            # 2c. 安装/校准项目直接依赖版本
            Write-Info "校准 pyfluent 环境依赖版本..."
            [void](Sync-CondaPackageVersion -PackageName "ansys-fluent-core" -Version $PyFluentVersion -DisplayName "ansys-fluent-core")
        }
    }
}

# ===========================================================================
# Step 3: 远程目录结构
# ===========================================================================
if (-not $SkipDirs) {
    $stepNum++
    Write-Step -Num $stepNum -Total $totalSteps -Message "创建远程工作目录"

    $directories = @(
        @{ Path = $WorkingDir;                                Label = "Fluent 工作目录" },
        @{ Path = $ScriptsDir;                                Label = "脚本部署目录" },
        @{ Path = $RefFilesDir;                               Label = "Chemkin 引用文件目录" },
        @{ Path = $ScdocDir;                                  Label = "SCDOC 接收目录" },
        @{ Path = $MshDir;                                    Label = "网格输出目录" },
        @{ Path = $ResultDir;                                 Label = "算例输出目录" },
        @{ Path = $FlagDir;                                   Label = "标志文件目录" }
    )

    foreach ($dir in $directories) {
        if (Test-Path $dir.Path) {
            Write-OK "$($dir.Label): $($dir.Path)"
        }
        else {
            try {
                New-Item -ItemType Directory -Force -Path $dir.Path | Out-Null
                Write-OK "$($dir.Label): 已创建 $($dir.Path)"
            }
            catch {
                Write-Err "$($dir.Label): 创建失败 — $_"
            }
        }
    }
}

# ===========================================================================
# Step 4: ANSYS Fluent 检查（手动安装）
# ===========================================================================
$stepNum++
Write-Step -Num $stepNum -Total $totalSteps -Message "检查 ANSYS Fluent（需手动安装）"

$fluentExePath = $script:FluentPath

if (Test-Path $fluentExePath) {
    Write-OK "ANSYS Fluent 24.1 已安装: $fluentExePath"
}
else {
    Write-Err "ANSYS Fluent 24.1 未找到"
    Write-Info ""
    Write-Info "ANSYS Fluent 为商业软件，需手动安装："
    Write-Info "  1. 从 ANSYS 官方获取安装包 (https://www.ansys.com/academic/students)"
    Write-Info "  2. 安装时勾选: ANSYS Fluent + ANSYS Fluent Meshing"
    Write-Info "  3. 配置路径: $fluentExePath"
    Write-Info "  4. Intel MPI 随 Fluent 自动安装"
    Write-Info ""
}

if (Test-Path $MpiBinDir) {
    Write-OK "Intel MPI 2021 已安装: $MpiBinDir"
}
else {
    Write-Err "Intel MPI 2021 未找到: $MpiBinDir"
}

# 检查 CPU 核心数
$cpuCount = (Get-CimInstance Win32_Processor | Measure-Object -Property NumberOfLogicalProcessors -Sum).Sum
$cpuDetail = "配置要求 Meshing $MeshingProcessorCount 核 / Solver $SolverProcessorCount 核，最低 $RequiredProcessorCount 核"
if ($cpuCount -ge $RequiredProcessorCount) {
    Write-OK "CPU: $cpuCount 逻辑核心（$cpuDetail）"
}
else {
    Write-Err "CPU: $cpuCount 逻辑核心，不满足 $cpuDetail"
}

# ===========================================================================
# 验证汇总
# ===========================================================================
Write-Host ""
Write-Host "========================================================" -ForegroundColor Cyan
Write-Host "  环境搭建完成 — 验证汇总" -ForegroundColor Cyan
Write-Host "========================================================" -ForegroundColor Cyan

$checks = @()

# SSH 服务
$sshdSvc = Get-Service sshd -ErrorAction SilentlyContinue
$checks += @{
    Name = "OpenSSH Server"
    OK = ($sshdSvc -and $sshdSvc.Status -eq 'Running')
}

# Conda
$checks += @{
    Name = "Conda ($CondaExe)"
    OK = (Test-Path $CondaExe)
}

# pyfluent 环境
if (Test-Path $CondaExe) {
    $envList = Invoke-Conda @("env", "list") 120
    $envExists = $envList.ExitCode -eq 0 -and $envList.Output -match [regex]::Escape($CondaEnv)
    $checks += @{ Name = "Conda 环境 '$CondaEnv'"; OK = $envExists }
}

# PyFluent
if (Test-Path $CondaExe) {
    $pfCheck = Invoke-Conda @("run", "--no-capture-output", "-n", $CondaEnv, "python", "-c", "import ansys.fluent.core; print(ansys.fluent.core.__version__)") 120
    $pfOK = $pfCheck.ExitCode -eq 0 -and $pfCheck.Output -eq $PyFluentVersion
    $checks += @{ Name = "ansys-fluent-core==$PyFluentVersion"; OK = $pfOK }
}

# ANSYS Fluent
$checks += @{
    Name = "ANSYS Fluent 24.1"
    OK = (Test-Path $fluentExePath)
}

# Intel MPI
$checks += @{
    Name = "Intel MPI 2021"
    OK = (Test-Path $MpiBinDir)
}

# CPU 核心
$checks += @{
    Name = "CPU 逻辑核心 >= $RequiredProcessorCount"
    OK = ($cpuCount -ge $RequiredProcessorCount)
}

# 目录结构（用 foreach 语句避免 ForEach-Object 作用域问题）
$dirPaths = @($WorkingDir, $ScriptsDir, $ScdocDir, $RefFilesDir, $MshDir, $ResultDir, $FlagDir)
$allDirsExist = $true
foreach ($d in $dirPaths) {
    if (-not (Test-Path $d)) { $allDirsExist = $false; break }
}
$checks += @{ Name = "远程目录结构"; OK = $allDirsExist }

# 输出结果
$passCount = 0
$failCount = 0
foreach ($check in $checks) {
    if ($check.OK) {
        Write-Host "  [OK] $($check.Name)" -ForegroundColor Green
        $passCount++
    }
    else {
        Write-Host "  [XX] $($check.Name)" -ForegroundColor Red
        $failCount++
    }
}

Write-Host ""
if ($failCount -eq 0) {
    Write-Host "  所有检查通过！远程工作站环境就绪。" -ForegroundColor Green
    $exitCode = 0
}
else {
    Write-Host "  $failCount 项未通过，请按上方提示修复。" -ForegroundColor Red
    $exitCode = 1
}

Write-Host ""
Write-Host "  后续步骤：" -ForegroundColor Cyan
Write-Host "    1. 如 ANSYS Fluent 未安装，请手动安装" -ForegroundColor White
Write-Host "    2. 在本地运行 scripts\check_remote_env.ps1 验证远程环境" -ForegroundColor White
Write-Host "    3. 确认 autofluid_config.toml 中 [remote_config] 路径正确" -ForegroundColor White
Write-Host ""
Exit-SetupScript $exitCode
