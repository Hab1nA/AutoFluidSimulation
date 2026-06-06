#Requires -Version 5.1
<#
.SYNOPSIS
    在远程工作站上快速验证 AutoFluid 环境是否就绪。
.DESCRIPTION
    执行一系列快速检查和轻量级冒烟测试，验证远程环境可正常运行
    Fluent Meshing 和 Solver 任务。请在远程工作站本机运行。
.PARAMETER ConfigPath
    autofluid_config.toml 路径。脚本会读取 [remote_config] 中的远程路径、Conda 和 MPI 配置；读取失败会直接退出。
.PARAMETER PyFluentVersion
    期望的 ansys-fluent-core 版本。默认 "0.37.2"。
.EXAMPLE
    # 在远程工作站上直接运行：
    .\verify_remote_setup.ps1

    # 按项目配置文件中的 [remote_config] 检查目录：
    .\verify_remote_setup.ps1 -ConfigPath "D:\xkz_1020\autofluid_config.toml"

    # 如需从本地控制机跨 SSH 检查，请运行：
    .\check_remote_env.ps1
#>

[Diagnostics.CodeAnalysis.SuppressMessageAttribute('PSAvoidUsingWriteHost', '')]
[Diagnostics.CodeAnalysis.SuppressMessageAttribute('PSAvoidUsingPositionalParameters', '')]
param(
    [string]$ConfigPath = "",
    [string]$PyFluentVersion = "0.37.2",
    [switch]$NoPause
)

$ErrorActionPreference = "Continue"
$script:ShouldPause = -not $NoPause -and -not [Console]::IsInputRedirected
$script:ConfigPathInput = $ConfigPath

$script:Pass = 0
$script:Fail = 0

function Write-Result {
    param([string]$Name, [bool]$OK, [string]$Detail = "")
    if ($OK) {
        $script:Pass++
        $icon = "[OK]"
        $color = "Green"
    }
    else {
        $script:Fail++
        $icon = "[XX]"
        $color = "Red"
    }
    $msg = "$icon $Name"
    if ($Detail) { $msg += " — $Detail" }
    Write-Host "  $msg" -ForegroundColor $color
}

function Exit-VerifyScript {
    param([int]$ExitCode)

    if ($script:ShouldPause) {
        Read-Host "按 Enter 键退出" | Out-Null
    }
    exit $ExitCode
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
        [int]$TimeoutSec = 120
    )

    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $FilePath
    $psi.Arguments = Join-CommandArgument $Arguments
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true

    try {
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
    catch {
        return @{ ExitCode = -1; Output = $_.Exception.Message }
    }
}

function Invoke-Conda {
    param(
        [string[]]$Arguments,
        [int]$TimeoutSec = 120
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
    Write-Result "读取配置文件" $false "未找到 autofluid_config.toml，请使用 -ConfigPath 指定配置文件"
    Exit-VerifyScript 1
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

function Get-RequiredPositiveInteger {
    param(
        [hashtable]$Values,
        [string]$Key,
        [string]$SectionName
    )

    if (-not $Values.ContainsKey($Key) -or -not $Values[$Key]) {
        Write-Result "读取 $SectionName" $false "缺少必需字段: $Key"
        Exit-VerifyScript 1
    }

    $parsedValue = 0
    if (-not [int]::TryParse([string]$Values[$Key], [ref]$parsedValue) -or $parsedValue -le 0) {
        Write-Result "读取 $SectionName" $false "$Key 必须是正整数，当前值: $($Values[$Key])"
        Exit-VerifyScript 1
    }
    return $parsedValue
}

function Initialize-RemoteConfigValue {
    $resolvedConfigPath = Resolve-ConfigPath
    if (-not (Test-Path $resolvedConfigPath)) {
        Write-Result "读取配置文件" $false "配置文件不存在: $resolvedConfigPath"
        Exit-VerifyScript 1
    }

    $remoteConfig = Read-RemoteConfig $resolvedConfigPath
    if ($remoteConfig.Count -eq 0) {
        Write-Result "读取 remote_config" $false "未能读取: $resolvedConfigPath"
        Exit-VerifyScript 1
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
        Write-Result "读取 remote_config" $false "缺少必需字段: $($missingKeys -join ', ')"
        Exit-VerifyScript 1
    }

    $meshingConfig = Read-TomlSection -Path $resolvedConfigPath -SectionName "meshing"
    $solverConfig = Read-TomlSection -Path $resolvedConfigPath -SectionName "solver"

    Write-Host "  [--] 已读取远程配置 — $resolvedConfigPath" -ForegroundColor Yellow
    $script:CondaEnv = $remoteConfig["conda_env"]
    $script:CondaExe = $remoteConfig["conda_exe"]
    $script:WorkingDir = $remoteConfig["working_dir"]
    $script:ScriptsDir = $remoteConfig["scripts_dir"]
    $script:RefFilesDir = $remoteConfig["ref_files_dir"]
    $script:ScdocDir = $remoteConfig["scdoc_dir"]
    $script:MshDir = $remoteConfig["msh_dir"]
    $script:ResultDir = $remoteConfig["result_dir"]
    $script:FlagDir = $remoteConfig["flag_dir"]
    $script:MpiBinDir = $remoteConfig["mpi_bin_dir"]
    $script:MeshingProcessorCount = Get-RequiredPositiveInteger -Values $meshingConfig -Key "meshing_processor_count" -SectionName "meshing"
    $script:SolverProcessorCount = Get-RequiredPositiveInteger -Values $solverConfig -Key "solver_processor_count" -SectionName "solver"
    $script:RequiredProcessorCount = [Math]::Max($script:MeshingProcessorCount, $script:SolverProcessorCount)
}

function Initialize-RemotePathValue {
    Initialize-RemoteConfigValue
}

Write-Host ""
Write-Host "AutoFluid Remote Verification" -ForegroundColor Cyan
Write-Host "==============================" -ForegroundColor Cyan
Initialize-RemotePathValue

# 1. SSH 服务
$sshd = Get-Service sshd -ErrorAction SilentlyContinue
Write-Result "OpenSSH Server" ($sshd -and $sshd.Status -eq 'Running')

# 2. Conda
$condaOK = Test-Path $CondaExe
$condaVer = ""
if ($condaOK) {
    $condaVersionResult = Invoke-Conda @("--version") 60
    if ($condaVersionResult.ExitCode -eq 0) {
        $condaVer = $condaVersionResult.Output
    }
    else {
        $condaOK = $false
        $condaVer = $condaVersionResult.Output
    }
}
Write-Result "Conda" $condaOK $condaVer

# 3. pyfluent 环境
$envOK = $false
if ($condaOK) {
    $envListResult = Invoke-Conda @("env", "list") 120
    $envOK = $envListResult.ExitCode -eq 0 -and $envListResult.Output -match [regex]::Escape($CondaEnv)
}
Write-Result "Conda 环境 '$CondaEnv'" $envOK

# 4. PyFluent 导入
$pfOK = $false
$pfVer = ""
if ($envOK) {
    $pfResult = Invoke-Conda @("run", "--no-capture-output", "-n", $CondaEnv, "python", "-c", "import ansys.fluent.core; print(ansys.fluent.core.__version__)") 120
    $pfOutStr = $pfResult.Output.Trim()
    $pfOK = ($pfResult.ExitCode -eq 0 -and $pfOutStr -eq $PyFluentVersion)
    if ($pfOK) { $pfVer = $pfOutStr }
    elseif ($pfResult.ExitCode -eq 0) { $pfVer = "当前 $pfOutStr，期望 $PyFluentVersion" }
    else { $pfVer = $pfOutStr }
}
Write-Result "ansys-fluent-core" $pfOK $pfVer

# 5. ANSYS Fluent
$fluentPath = "C:\Program Files\ANSYS Inc\v241\fluent\ntbin\win64\fluent.exe"
$fluentOK = Test-Path $fluentPath
Write-Result "ANSYS Fluent 24.1" $fluentOK

# 6. Intel MPI
$mpiPath = Join-Path $MpiBinDir "mpiexec.exe"
$mpiOK = Test-Path $mpiPath
Write-Result "Intel MPI (mpiexec.exe)" $mpiOK

# 7. 目录结构
$dirs = @(
    $WorkingDir,
    $ScriptsDir,
    $RefFilesDir,
    $ScdocDir,
    $MshDir,
    $ResultDir,
    $FlagDir
)
$allDirsOK = $true
foreach ($d in $dirs) {
    if (-not (Test-Path $d)) { $allDirsOK = $false }
}
Write-Result "远程目录结构 (7 个目录)" $allDirsOK

# 8. 磁盘空间（从工作目录推导盘符）
$driveLetter = (Split-Path $WorkingDir -Qualifier).TrimEnd(':')
if (-not $driveLetter) {
    Write-Result "工作目录盘符" $false "working_dir 不是带盘符的 Windows 路径: $WorkingDir"
}
else {
    $disk = Get-PSDrive $driveLetter -ErrorAction SilentlyContinue
    if ($disk) {
    $freeGB = [math]::Round($disk.Free / 1GB, 1)
    $diskOK = $freeGB -gt 10
    Write-Result "$driveLetter`: 盘可用空间" $diskOK "$freeGB GB"
    }
    else {
        Write-Result "$driveLetter`: 盘" $false "不存在"
    }
}

# 9. CPU 核心
$cpuCount = (Get-CimInstance Win32_Processor | Measure-Object -Property NumberOfLogicalProcessors -Sum).Sum
$cpuOK = $cpuCount -ge $RequiredProcessorCount
$cpuDetail = "$cpuCount 核；配置要求 Meshing $MeshingProcessorCount 核 / Solver $SolverProcessorCount 核，最低 $RequiredProcessorCount 核"
Write-Result "CPU 逻辑核心" $cpuOK $cpuDetail

# 10. PyFluent 冒烟测试（仅导入，不启动 Fluent）
if ($pfOK) {
    $smokeCode = "import ansys.fluent.core as pf; assert hasattr(pf, 'FluentMode'), 'FluentMode missing'; assert hasattr(pf, 'Precision'), 'Precision missing'; assert hasattr(pf, 'FluentVersion'), 'FluentVersion missing'; assert hasattr(pf, 'launch_fluent'), 'launch_fluent missing'; print('SMOKE_OK')"
    $smokeResult = Invoke-Conda @("run", "--no-capture-output", "-n", $CondaEnv, "python", "-c", $smokeCode) 120
    $smokeOK = ($smokeResult.ExitCode -eq 0 -and $smokeResult.Output -match "SMOKE_OK")
    Write-Result "PyFluent API 冒烟测试" $smokeOK
}

# 汇总
Write-Host ""
Write-Host "------------------------------" -ForegroundColor Cyan
if ($script:Fail -eq 0) {
    Write-Host "  全部通过 ($script:Pass/$($script:Pass + $script:Fail)) — 环境就绪" -ForegroundColor Green
    Exit-VerifyScript 0
}
else {
    Write-Host "  通过 $script:Pass / 失败 $script:Fail" -ForegroundColor Red
    Write-Host "  请运行 setup_remote_workstation.ps1 修复失败项" -ForegroundColor Yellow
    Exit-VerifyScript 1
}
