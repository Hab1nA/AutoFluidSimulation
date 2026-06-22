#Requires -Version 5.1
<#
.SYNOPSIS
    通过 SSH 检查远程工作站环境是否满足 AutoFluid 运行条件。
.DESCRIPTION
    从本地控制机通过 SSH 连接远程工作站，逐项检查：
    - 操作系统版本
    - OpenSSH Server 状态
    - Conda 安装与 pyfluent 环境
    - PyFluent (ansys-fluent-core) 包
    - ANSYS Fluent v241 安装
    - Intel MPI 路径
    - 远程目录结构
    - 磁盘空间
.PARAMETER Host
    远程主机 IP 或域名，也支持 user@host。未提供时将提示手动输入。
.PARAMETER User
    远程用户名。未提供时将提示手动输入。
.PARAMETER Port
    SSH 端口。默认 22。
.PARAMETER PyFluentVersion
    期望的 ansys-fluent-core 版本。默认 "0.37.2"。
.EXAMPLE
    .\check_remote_env.ps1
    .\check_remote_env.ps1 -RemoteHost ps@192.168.1.100
    .\check_remote_env.ps1 -RemoteHost 192.168.1.100 -User admin -Port 22
#>

[Diagnostics.CodeAnalysis.SuppressMessageAttribute('PSAvoidUsingWriteHost', '')]
[Diagnostics.CodeAnalysis.SuppressMessageAttribute('PSAvoidUsingPositionalParameters', '')]
param(
    [Alias("Host")]
    [string]$RemoteHost,
    [string]$User,
    [string]$WorkstationId,
    [int]$Port,
    [string]$PyFluentVersion = "0.37.2",
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
        [ValidateSet("Pass", "Warn", "Fail", "Info")]
        [string]$Status,
        [string]$Detail = ""
    )
    $icon = switch ($Status) {
        "Pass" { $script:PassCount++; "[OK]" }
        "Warn" { $script:WarnCount++; "[!!]" }
        "Fail" { $script:FailCount++; "[XX]" }
        "Info" { "[--]" }
    }
    $color = switch ($Status) {
        "Pass" { "Green" }
        "Warn" { "Yellow" }
        "Fail" { "Red" }
        "Info" { "DarkGray" }
    }
    $msg = "  $icon $Label"
    if ($Detail) { $msg += " — $Detail" }
    Write-Host $msg -ForegroundColor $color
}

function Exit-CheckScript {
    param([int]$ExitCode)

    if ($script:ShouldPause) {
        Read-Host "按 Enter 键退出" | Out-Null
    }
    exit $ExitCode
}

function Get-TomlString {
    param(
        [string]$Content,
        [string]$Key,
        [string]$DefaultValue
    )

    $pattern = "(?m)^\s*$Key\s*=\s*['""]([^'""]+)['""]"
    if ($Content -match $pattern) { return $Matches[1] }
    return $DefaultValue
}

function Get-TomlInt {
    param(
        [string]$Content,
        [string]$Key,
        [int]$DefaultValue
    )

    $pattern = "(?m)^\s*$Key\s*=\s*(\d+)"
    if ($Content -match $pattern) { return [int]$Matches[1] }
    return $DefaultValue
}

function Get-TomlWorkstationSection {
    param(
        [string]$Content,
        [string]$Id,
        [string]$Host
    )

    $matches = [regex]::Matches($Content, "(?ms)^\[\[workstations\]\]\s*(.*?)(?=^\[\[workstations\]\]|^\[[^\[]|\z)")
    foreach ($match in $matches) {
        $section = $match.Groups[1].Value
        $sectionId = Get-TomlString $section "id" ""
        $sectionHost = Get-TomlString $section "host" ""
        if ($Id -and $sectionId -eq $Id) { return $section }
        if (-not $Id -and $Host -and $sectionHost -eq $Host) { return $section }
    }
    return ""
}

function Apply-WorkstationTomlDefaults {
    param([string]$Section)

    if (-not $Section) { return }
    $script:SshPort = Get-TomlInt $Section "port" $script:SshPort
    $script:RemoteWorkingDir = Get-TomlString $Section "working_dir" $script:RemoteWorkingDir
    $script:RemoteScriptsDir = Get-TomlString $Section "scripts_dir" $script:RemoteScriptsDir
    $script:RemoteRefFilesDir = Get-TomlString $Section "ref_files_dir" $script:RemoteRefFilesDir
    $script:RemoteScdocDir = Get-TomlString $Section "scdoc_dir" $script:RemoteScdocDir
    $script:RemoteMshDir = Get-TomlString $Section "msh_dir" $script:RemoteMshDir
    $script:RemoteResultDir = Get-TomlString $Section "result_dir" $script:RemoteResultDir
    $script:RemoteFlagDir = Get-TomlString $Section "flag_dir" $script:RemoteFlagDir
    $script:CondaEnv = Get-TomlString $Section "conda_env" $script:CondaEnv
    $script:CondaExe = Get-TomlString $Section "conda_exe" $script:CondaExe
    $script:FluentPath = Get-TomlString $Section "fluent_path" $script:FluentPath
    $script:MpiBinDir = Get-TomlString $Section "mpi_bin_dir" $script:MpiBinDir
}

function ConvertTo-CmdExecutable {
    param([string]$Path)

    if ($Path -match '[\\:\s]') { return "`"$Path`"" }
    return $Path
}

function Resolve-ManualValue {
    param(
        [string]$Value,
        [string]$Prompt,
        [string]$Name
    )

    if ($Value) { return $Value.Trim() }
    if ([Console]::IsInputRedirected) {
        Write-Host "  [XX] $Name 未提供；请使用参数显式指定。" -ForegroundColor Red
        Exit-CheckScript 1
    }

    $inputValue = Read-Host $Prompt
    if (-not $inputValue -or -not $inputValue.Trim()) {
        Write-Host "  [XX] $Name 不能为空。" -ForegroundColor Red
        Exit-CheckScript 1
    }
    return $inputValue.Trim()
}

function Split-SshTarget {
    param([string]$Target)

    $trimmed = $Target.Trim()
    if ($trimmed -match '^([^@\\]+)@(.+)$') {
        return @{
            User = $Matches[1]
            Host = $Matches[2]
        }
    }
    return @{
        User = ""
        Host = $trimmed
    }
}

# SSH 远程执行封装
function Invoke-RemoteWithParamiko {
    param(
        [string]$Command,
        [int]$TimeoutSec = 15
    )
    if (-not $script:SshPassword -or -not $script:VenvPython -or -not (Test-Path -LiteralPath $script:VenvPython -PathType Leaf)) {
        return @{ Output = "Paramiko password fallback unavailable"; ExitCode = -1 }
    }

    $execScript = Join-Path $env:TEMP "autofluid_remote_exec.py"
    @'
import os
import socket
import sys
import paramiko

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

host = os.environ["AUTOFLUID_REMOTE_HOST"]
port = int(os.environ["AUTOFLUID_REMOTE_PORT"])
user = os.environ["AUTOFLUID_REMOTE_USER"]
password = os.environ["AUTOFLUID_REMOTE_PASSWORD"]
command = os.environ["AUTOFLUID_REMOTE_COMMAND"]
timeout = int(os.environ["AUTOFLUID_REMOTE_TIMEOUT"])

def decode_output(data):
    for encoding in ("utf-8", "gbk", "mbcs"):
        try:
            text = data.decode(encoding, errors="replace")
        except LookupError:
            continue
        if "\ufffd" not in text:
            return text
    return data.decode("utf-8", errors="replace")

client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
try:
    client.connect(
        hostname=host,
        port=port,
        username=user,
        password=password,
        timeout=min(timeout, 30),
        banner_timeout=10,
        auth_timeout=10,
    )
    _stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
    out = decode_output(stdout.read())
    err = decode_output(stderr.read())
    code = stdout.channel.recv_exit_status()
    if out:
        sys.stdout.write(out)
    if err:
        sys.stdout.write(err)
    sys.exit(code)
except socket.timeout:
    print(f"SSH_TIMEOUT: command exceeded {timeout} seconds")
    sys.exit(255)
except Exception as exc:
    print(str(exc))
    sys.exit(255)
finally:
    client.close()
'@ | Set-Content -LiteralPath $execScript -Encoding UTF8
    $env:AUTOFLUID_REMOTE_HOST = $script:SshHost
    $env:AUTOFLUID_REMOTE_PORT = $script:SshPort
    $env:AUTOFLUID_REMOTE_USER = $script:SshUser
    $env:AUTOFLUID_REMOTE_PASSWORD = $script:SshPassword
    $env:AUTOFLUID_REMOTE_COMMAND = $Command
    $env:AUTOFLUID_REMOTE_TIMEOUT = [string]$TimeoutSec
    try {
        $output = & $script:VenvPython $execScript 2>&1
        $exitCode = $LASTEXITCODE
        return @{ Output = ($output | Out-String).Trim(); ExitCode = $exitCode }
    }
    finally {
        Remove-Item $execScript -ErrorAction SilentlyContinue
        Remove-Item Env:\AUTOFLUID_REMOTE_HOST, Env:\AUTOFLUID_REMOTE_PORT, Env:\AUTOFLUID_REMOTE_USER, Env:\AUTOFLUID_REMOTE_PASSWORD, Env:\AUTOFLUID_REMOTE_COMMAND, Env:\AUTOFLUID_REMOTE_TIMEOUT -ErrorAction SilentlyContinue
    }
}

function Invoke-RemoteWithOpenSsh {
    param(
        [string]$Command,
        [int]$TimeoutSec = 15
    )
    $sshArgs = @(
        "-o", "ConnectTimeout=5",
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=no",
        "-p", $script:SshPort,
        "$script:SshUser@$script:SshHost",
        $Command
    )
    $output = ""
    $exitCode = 0
    try {
        $psi = New-Object System.Diagnostics.ProcessStartInfo
        $psi.FileName = "ssh"
        # 转义参数中的双引号，防止 SSH 参数解析错误
        $psi.Arguments = ($sshArgs | ForEach-Object {
            if ($_ -match '\s') {
                $escaped = $_ -replace '"', '\"'
                "`"$escaped`""
            } else { $_ }
        }) -join " "
        $psi.RedirectStandardOutput = $true
        $psi.RedirectStandardError = $true
        $psi.UseShellExecute = $false
        $psi.CreateNoWindow = $true

        $proc = [System.Diagnostics.Process]::Start($psi)
        $outTask = $proc.StandardOutput.ReadToEndAsync()
        $errTask = $proc.StandardError.ReadToEndAsync()
        $completed = $proc.WaitForExit([Math]::Min($TimeoutSec * 1000, 30000))
        if (-not $completed) {
            try { $proc.Kill() } catch { Write-Verbose "无法终止超时 SSH 进程: $_" }
            $proc.WaitForExit()
            $output = "SSH_TIMEOUT: command exceeded $TimeoutSec seconds"
            $exitCode = -1
            return @{ Output = $output; ExitCode = $exitCode }
        }
        $output = $outTask.Result
        $errOutput = $errTask.Result
        $exitCode = $proc.ExitCode
        if ($errOutput) {
            if ($output) { $output = "$output`n$errOutput" } else { $output = $errOutput }
        }
    }
    catch {
        $output = "SSH_ERROR: $_"
        $exitCode = -1
    }
    return @{ Output = $output.Trim(); ExitCode = $exitCode }
}

function Invoke-Remote {
    param(
        [string]$Command,
        [int]$TimeoutSec = 15
    )

    $openSshResult = Invoke-RemoteWithOpenSsh $Command $TimeoutSec
    if ($openSshResult.ExitCode -eq 0) {
        return $openSshResult
    }

    if ($script:SshPassword) {
        $paramikoResult = Invoke-RemoteWithParamiko $Command $TimeoutSec
        if ($paramikoResult.ExitCode -eq 0) {
            return $paramikoResult
        }
        return @{
            Output = "OpenSSH: $($openSshResult.Output)`nParamiko: $($paramikoResult.Output)".Trim()
            ExitCode = $paramikoResult.ExitCode
        }
    }

    return $openSshResult
}

# ---------------------------------------------------------------------------
# 从 autofluid_config.toml 读取远程运行路径配置；连接信息必须手动提供
# ---------------------------------------------------------------------------

$ProjectDir = Split-Path -Parent $PSScriptRoot
$ConfigToml = Join-Path $ProjectDir "autofluid_config.toml"
$EnvFile = Join-Path $ProjectDir ".env"
$script:VenvPython = Join-Path $ProjectDir ".venv\Scripts\python.exe"
$script:SshPassword = [Environment]::GetEnvironmentVariable("AUTOFLUID_SSH_PASSWORD")
$script:TomlContent = ""
if (Test-Path -LiteralPath $EnvFile -PathType Leaf) {
    $envContent = Get-Content $EnvFile -Raw
    if ($envContent -match "AUTOFLUID_SSH_PASSWORD=") {
        $pwdLine = ($envContent -split "`n") | Where-Object { $_ -match "AUTOFLUID_SSH_PASSWORD=" } | Select-Object -First 1
        $script:SshPassword = ($pwdLine -split "=", 2)[1].Trim().Trim('"').Trim("'")
    }
}

$defaultPort = 22
$defaultWorkingDir = "D:\xkz_1020\workingdir"
$defaultScriptsDir = "D:\xkz_1020\scripts"
$defaultRefFilesDir = "D:\xkz_1020\fluent_chemkin_files"
$defaultScdocDir = "D:\xkz_1020\scdoc"
$defaultMshDir = "D:\xkz_1020\msh"
$defaultResultDir = "D:\xkz_1020\case"
$defaultFlagDir = "D:\xkz_1020\flags"
$defaultCondaEnv = "pyfluent"
$defaultCondaExe = "C:\ProgramData\anaconda3\Scripts\conda.exe"
$defaultFluentPath = "C:\Program Files\ANSYS Inc\v241\fluent\ntbin\win64\fluent.exe"
$defaultMpiBinDir = "C:\Program Files\ANSYS Inc\v241\fluent\fluent24.1.0\multiport\mpi\win64\intel2021\bin"

if (Test-Path -LiteralPath $ConfigToml -PathType Leaf) {
    $tomlContent = Get-Content $ConfigToml -Raw
    $script:TomlContent = $tomlContent
    $remoteMatch = [regex]::Match($tomlContent, "(?ms)^\[remote_config\]\s*(.*?)(?=^\[|\z)")
    $remoteSection = if ($remoteMatch.Success) { $remoteMatch.Groups[1].Value } else { $tomlContent }
    $defaultWorkingDir = Get-TomlString $remoteSection "working_dir" $defaultWorkingDir
    $defaultScriptsDir = Get-TomlString $remoteSection "scripts_dir" $defaultScriptsDir
    $defaultRefFilesDir = Get-TomlString $remoteSection "ref_files_dir" $defaultRefFilesDir
    $defaultScdocDir = Get-TomlString $remoteSection "scdoc_dir" $defaultScdocDir
    $defaultMshDir = Get-TomlString $remoteSection "msh_dir" $defaultMshDir
    $defaultResultDir = Get-TomlString $remoteSection "result_dir" $defaultResultDir
    $defaultFlagDir = Get-TomlString $remoteSection "flag_dir" $defaultFlagDir
    $defaultCondaEnv = Get-TomlString $remoteSection "conda_env" $defaultCondaEnv
    $defaultCondaExe = Get-TomlString $remoteSection "conda_exe" $defaultCondaExe
    $defaultFluentPath = Get-TomlString $remoteSection "fluent_path" $defaultFluentPath
    $defaultMpiBinDir = Get-TomlString $remoteSection "mpi_bin_dir" $defaultMpiBinDir
}

$targetInput = Resolve-ManualValue $RemoteHost "请输入工作站 IP 或主机名（可输入 user@host）" "工作站 IP/主机名"
$targetParts = Split-SshTarget $targetInput
$script:SshHost = $targetParts.Host
if ($User) {
    $script:SshUser = $User.Trim()
}
elseif ($targetParts.User) {
    $script:SshUser = $targetParts.User
}
else {
    $script:SshUser = Resolve-ManualValue "" "请输入工作站用户名" "工作站用户名"
}
$script:SshPort = if ($Port -gt 0) { $Port } else { $defaultPort }
$script:RemoteWorkingDir = $defaultWorkingDir
$script:RemoteScriptsDir = $defaultScriptsDir
$script:RemoteRefFilesDir = $defaultRefFilesDir
$script:RemoteScdocDir = $defaultScdocDir
$script:RemoteMshDir = $defaultMshDir
$script:RemoteResultDir = $defaultResultDir
$script:RemoteFlagDir = $defaultFlagDir
$script:CondaEnv = $defaultCondaEnv
$script:CondaExe = $defaultCondaExe
$script:FluentPath = $defaultFluentPath
$script:MpiBinDir = $defaultMpiBinDir
if ($script:TomlContent) {
    $workstationSection = Get-TomlWorkstationSection $script:TomlContent $WorkstationId $script:SshHost
    Apply-WorkstationTomlDefaults $workstationSection
}

Write-Host ""
Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  AutoFluid 远程工作站环境检测" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  目标: $script:SshUser@$script:SshHost`:$script:SshPort"
Write-Host ""

# ===========================================================================
# 1. SSH 连通性
# ===========================================================================
Write-Host "[1/8] SSH 连通性" -ForegroundColor Yellow

$sshExe = Get-Command ssh -ErrorAction SilentlyContinue
if (-not $sshExe) {
    Write-Check "SSH 客户端" "Fail" "本地 ssh 命令不可用"
    Write-Host "`n  无法继续远程检查。" -ForegroundColor Red
    Exit-CheckScript 1
}

$pingResult = Invoke-Remote "echo SSH_OK"
if ($pingResult.ExitCode -eq 0 -and $pingResult.Output -match "SSH_OK") {
    Write-Check "SSH 连接" "Pass" "连接成功"
}
else {
    Write-Check "SSH 连接" "Fail" "无法连接: $($pingResult.Output)"
    Write-Host "`n  无法继续远程检查。请确认远程 SSH 服务已启动且网络连通。" -ForegroundColor Red
    Exit-CheckScript 1
}

# ===========================================================================
# 2. 操作系统
# ===========================================================================
Write-Host "`n[2/8] 操作系统" -ForegroundColor Yellow

$osResult = Invoke-Remote "wmic os get Caption /value"
if ($osResult.ExitCode -eq 0 -and $osResult.Output -match "Caption=(.+)") {
    Write-Check "操作系统" "Pass" $Matches[1].Trim()
}
else {
    $osResult2 = Invoke-Remote "ver"
    if ($osResult2.ExitCode -eq 0) {
        Write-Check "操作系统" "Pass" ($osResult2.Output -split "`n" | Select-Object -Last 1).Trim()
    }
    else {
        Write-Check "操作系统" "Warn" "无法获取版本信息"
    }
}

# ===========================================================================
# 3. OpenSSH Server
# ===========================================================================
Write-Host "`n[3/8] OpenSSH Server" -ForegroundColor Yellow

$sshSvcResult = Invoke-Remote "sc.exe query sshd"
if ($sshSvcResult.ExitCode -eq 0 -and $sshSvcResult.Output -match "RUNNING") {
    Write-Check "OpenSSH Server" "Pass" "服务运行中"
}
elseif ($sshSvcResult.ExitCode -eq 0) {
    Write-Check "OpenSSH Server" "Warn" "服务已安装但未运行"
}
else {
    Write-Check "OpenSSH Server" "Warn" "无法查询服务状态（当前可通过 SSH 连接，说明服务正在运行）"
}

# ===========================================================================
# 4. Conda 环境
# ===========================================================================
Write-Host "`n[4/8] Conda / Python 环境" -ForegroundColor Yellow

# 检查 conda.exe
$condaExePath = $script:CondaExe
$condaEnv = $script:CondaEnv
$condaCmd = ConvertTo-CmdExecutable $condaExePath
$condaVerResult = Invoke-Remote "$condaCmd --version"
if ($condaVerResult.ExitCode -eq 0 -and $condaVerResult.Output) {
    Write-Check "Conda" "Pass" $condaVerResult.Output
}
else {
    # 尝试 PATH 中的 conda
    $condaVerResult2 = Invoke-Remote "conda --version"
    if ($condaVerResult2.ExitCode -eq 0 -and $condaVerResult2.Output) {
        Write-Check "Conda" "Pass" "$($condaVerResult2.Output)（PATH 中）"
        Write-Check "" "Info" "注意: 项目配置指向 $condaExePath，但 conda 不在该路径"
        $condaExePath = "conda"
        $condaCmd = "conda"
    }
    else {
        Write-Check "Conda" "Fail" "未找到 conda ($condaExePath)"
    }
}

# 检查 pyfluent 环境
$envListResult = Invoke-Remote "$condaCmd env list"
if ($envListResult.ExitCode -eq 0) {
    if ($envListResult.Output -match [regex]::Escape($condaEnv)) {
        Write-Check "Conda 环境 '$condaEnv'" "Pass" "存在"

        # 检查 Python 版本
        $pyVerResult = Invoke-Remote "$condaCmd run --no-capture-output -n `"$condaEnv`" python --version"
        if ($pyVerResult.ExitCode -eq 0) {
            Write-Check "pyfluent Python 版本" "Pass" $pyVerResult.Output.Trim()
        }
        else {
            Write-Check "pyfluent Python 版本" "Warn" "无法获取"
        }
    }
    else {
        Write-Check "Conda 环境 '$condaEnv'" "Fail" "不存在"
    }
}
else {
    Write-Check "Conda 环境列表" "Warn" "无法获取（conda 可能未安装）"
}

# ===========================================================================
# 5. PyFluent
# ===========================================================================
Write-Host "`n[5/8] PyFluent (ansys-fluent-core)" -ForegroundColor Yellow

$pfResult = Invoke-Remote "$condaCmd run --no-capture-output -n `"$condaEnv`" python -c `"import ansys.fluent.core; print(ansys.fluent.core.__version__)`""
if ($pfResult.ExitCode -eq 0 -and $pfResult.Output -and $pfResult.Output -notmatch "ModuleNotFoundError") {
    $remotePyFluentVersion = $pfResult.Output.Trim()
    if ($remotePyFluentVersion -eq $PyFluentVersion) {
        Write-Check "ansys-fluent-core" "Pass" "版本 $remotePyFluentVersion"
    }
    else {
        Write-Check "ansys-fluent-core" "Fail" "版本 $remotePyFluentVersion（期望 $PyFluentVersion）"
        Write-Check "" "Info" "修复: conda run -n $condaEnv pip install ansys-fluent-core==$PyFluentVersion"
    }
}
else {
    Write-Check "ansys-fluent-core" "Fail" "未安装或导入失败"
    Write-Check "" "Info" "安装: conda run -n $condaEnv pip install ansys-fluent-core==$PyFluentVersion"
}

# ===========================================================================
# 6. ANSYS Fluent
# ===========================================================================
Write-Host "`n[6/8] ANSYS Fluent" -ForegroundColor Yellow

# Fluent v241 可执行文件
$fluentExe = $script:FluentPath
$fluentCheck = Invoke-Remote "if exist `"$fluentExe`" (echo EXISTS) else (echo NOT_FOUND)"
if ($fluentCheck.Output -match "EXISTS") {
    Write-Check "ANSYS Fluent 24.1" "Pass" $fluentExe
}
else {
    Write-Check "ANSYS Fluent 24.1" "Fail" "未找到: $fluentExe"
}

# Intel MPI
$mpiBinDir = $script:MpiBinDir
$mpiCheck = Invoke-Remote "if exist `"$mpiBinDir`" (echo EXISTS) else (echo NOT_FOUND)"
if ($mpiCheck.Output -match "EXISTS") {
    Write-Check "Intel MPI 2021" "Pass" $mpiBinDir
}
else {
    Write-Check "Intel MPI 2021" "Fail" "未找到: $mpiBinDir"
}

# MPI 可执行文件
$mpiexecExe = "$mpiBinDir\mpiexec.exe"
$mpiexecCheck = Invoke-Remote "if exist `"$mpiexecExe`" (echo EXISTS) else (echo NOT_FOUND)"
if ($mpiexecCheck.Output -match "EXISTS") {
    Write-Check "mpiexec.exe" "Pass" "存在"
}
else {
    Write-Check "mpiexec.exe" "Warn" "未找到（MPI 可能安装不完整）"
}

# ===========================================================================
# 7. 远程目录结构
# ===========================================================================
Write-Host "`n[7/8] 远程目录结构" -ForegroundColor Yellow

$requiredDirs = @(
    @{ Path = $script:RemoteWorkingDir;  Label = "工作目录 (working_dir)" },
    @{ Path = $script:RemoteScriptsDir;  Label = "脚本目录 (scripts_dir)" },
    @{ Path = $script:RemoteRefFilesDir; Label = "引用文件目录 (ref_files_dir)" },
    @{ Path = $script:RemoteScdocDir;    Label = "SCDOC 接收目录" },
    @{ Path = $script:RemoteMshDir;      Label = "网格输出目录 (msh_dir)" },
    @{ Path = $script:RemoteResultDir;   Label = "算例输出目录 (result_dir)" },
    @{ Path = $script:RemoteFlagDir;     Label = "标志目录 (flag_dir)" }
)

foreach ($dir in $requiredDirs) {
    $dirCheck = Invoke-Remote "if exist `"$($dir.Path)`" (echo EXISTS) else (echo NOT_FOUND)"
    if ($dirCheck.Output -match "EXISTS") {
        # 统计文件数
        $countResult = Invoke-Remote "dir /a-d /b `"$($dir.Path)`" 2>nul | find /c /v `"`""
        $fileCount = if ($countResult.ExitCode -eq 0) { $countResult.Output.Trim() } else { "?" }
        Write-Check $dir.Label "Pass" "$($dir.Path) ($fileCount 文件)"
    }
    else {
        Write-Check $dir.Label "Fail" "不存在: $($dir.Path)"
    }
}

# ===========================================================================
# 8. 磁盘空间
# ===========================================================================
Write-Host "`n[8/8] 磁盘空间" -ForegroundColor Yellow

$driveLetter = (Split-Path $script:RemoteWorkingDir -Qualifier).TrimEnd(":")
if (-not $driveLetter) { $driveLetter = "D" }
$wmicResult = Invoke-Remote "wmic logicaldisk where DeviceID='$driveLetter`:' get FreeSpace,Size /format:csv"
if ($wmicResult.ExitCode -eq 0 -and $wmicResult.Output) {
    Write-Check "$driveLetter`: 盘空间" "Pass" ($wmicResult.Output -replace "`n", " ").Trim()
}
else {
    Write-Check "$driveLetter`: 盘空间" "Warn" "无法获取"
}

# ===========================================================================
# CPU 核心数
# ===========================================================================
$cpuResult = Invoke-Remote "wmic cpu get NumberOfLogicalProcessors /value"
if ($cpuResult.ExitCode -eq 0 -and $cpuResult.Output) {
    $cpuCount = 0
    foreach ($match in [regex]::Matches($cpuResult.Output, "NumberOfLogicalProcessors=(\d+)")) {
        $cpuCount += [int]$match.Groups[1].Value
    }
    if ($cpuCount -ge 128) {
        Write-Check "CPU 逻辑核心" "Pass" "$cpuCount 核（满足 Solver 128 核需求）"
    }
    elseif ($cpuCount -ge 8) {
        Write-Check "CPU 逻辑核心" "Warn" "$cpuCount 核（不足 128 核，需调整 solver_processor_count）"
    }
    else {
        Write-Check "CPU 逻辑核心" "Fail" "$cpuCount 核（不足，Fluent 运行可能失败）"
    }
}

# ===========================================================================
# 汇总
# ===========================================================================
Write-Host ""
Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  远程检测结果汇总" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  通过: $script:PassCount" -ForegroundColor Green
Write-Host "  警告: $script:WarnCount" -ForegroundColor Yellow
Write-Host "  失败: $script:FailCount" -ForegroundColor Red
Write-Host ""

if ($script:FailCount -gt 0) {
    Write-Host "  存在 $script:FailCount 项失败。" -ForegroundColor Red
    Write-Host "  建议: 在远程工作站运行 setup_remote_workstation.ps1 进行修复。" -ForegroundColor Yellow
    Exit-CheckScript 1
}
elseif ($script:WarnCount -gt 0) {
    Write-Host "  存在 $script:WarnCount 项警告，部分功能可能受限。" -ForegroundColor Yellow
    Exit-CheckScript 0
}
else {
    Write-Host "  远程工作站环境检查全部通过！" -ForegroundColor Green
    Exit-CheckScript 0
}
