param(
    [int]$ConfigName,
    [ValidateSet("WS-A", "WS-B", "WS-C", "WS-D")]
    [string]$TargetWorkstation
)

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Split-Path -Parent $ScriptDir
$EnvScript = Join-Path $ScriptDir "autofluid_env.ps1"
$PythonExe = Join-Path $ProjectRoot ".venv\Scripts\python.exe"

if (Test-Path $EnvScript) {
    . $EnvScript
    if (Get-Command Import-AutoFluidEnv -ErrorAction SilentlyContinue) {
        Import-AutoFluidEnv -ProjectDir $ProjectRoot | Out-Null
    }
}

if (-not (Test-Path $PythonExe)) {
    throw "未找到项目 Python 虚拟环境: $PythonExe"
}

function Invoke-AutoFluidCliJson {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$CliArgs)

    $output = & $PythonExe -m tools.autofluid_cli @CliArgs 2>&1
    $exitCode = $LASTEXITCODE
    $text = ($output | Out-String).Trim()
    if (-not $text) {
        throw "autofluid_cli 未返回 JSON 输出，exit=$exitCode"
    }
    try {
        $payload = $text | ConvertFrom-Json -ErrorAction Stop
    } catch {
        throw "autofluid_cli JSON 解析失败，exit=$exitCode，输出: $text"
    }
    if ($exitCode -ne 0 -or -not $payload.ok) {
        $message = [string]$payload.message
        if (-not $message) { $message = "autofluid_cli 执行失败" }
        throw $message
    }
    return $payload
}

function Get-DashboardStatus {
    return (Invoke-AutoFluidCliJson status).data
}

function Get-ConfigStatuses {
    param($Dashboard, [int]$Name)

    $statuses = $Dashboard.statuses
    if ($null -eq $statuses) { return $null }
    $prop = [string]$Name
    return $statuses.$prop
}

function Test-AllStepsCompleted {
    param($Steps)

    $stepNames = @("sw", "sc", "transfer", "meshing", "solver", "postprocess")
    foreach ($stepName in $stepNames) {
        if ([string]$Steps.$stepName -ne "Completed") {
            return $false
        }
    }
    return $true
}

function Get-ConfigWorkstation {
    param($Dashboard, [int]$Name)

    $assignments = $Dashboard.config_workstations
    $prop = [string]$Name
    if ($null -ne $assignments -and $assignments.PSObject.Properties.Name -contains $prop) {
        return [string]$assignments.$prop
    }
    return ""
}

function Get-WorkstationHealth {
    param($Dashboard, [string]$WorkstationId)

    $details = $Dashboard.health.workstation_ssh_details
    if ($null -eq $details) { return "unknown" }
    if ($details.PSObject.Properties.Name -contains $WorkstationId) {
        return [string]$details.$WorkstationId
    }
    return "unknown"
}

function Assert-DaemonIpcReady {
    if (Get-Command Assert-AutoFluidServerEndpoint -ErrorAction SilentlyContinue) {
        Assert-AutoFluidServerEndpoint
    }
    if (Get-Command Write-AutoFluidEndpointSummary -ErrorAction SilentlyContinue) {
        Write-AutoFluidEndpointSummary
    }
    if (Get-Command Test-AutoFluidIpcProtocolEndpoint -ErrorAction SilentlyContinue) {
        if (Test-AutoFluidIpcProtocolEndpoint -TimeoutMs 3000) {
            return
        }
    } else {
        return
    }

    $serverHost = if (Get-Command Get-AutoFluidServerHost -ErrorAction SilentlyContinue) {
        Get-AutoFluidServerHost
    } else {
        if ($env:AUTOFLUID_IPC_HOST) { $env:AUTOFLUID_IPC_HOST } else { "127.0.0.1" }
    }
    $serverPort = if (Get-Command Get-AutoFluidServerPort -ErrorAction SilentlyContinue) {
        Get-AutoFluidServerPort
    } else {
        if ($env:AUTOFLUID_IPC_PORT) { [int]$env:AUTOFLUID_IPC_PORT } else { 9527 }
    }

    throw @"
Daemon IPC 不可达: ${serverHost}:${serverPort}

请先确认 daemon 正在运行且当前 PowerShell 能连接到 IPC 端点，然后重新执行迁移脚本。
常用处理:
1. 运行 scripts\start_autofluid_preflight.ps1 检查并启动服务器 daemon。
2. 如果通过本地端口转发连接，先运行 scripts\start_server_ipc_tunnel.ps1，并确认 .env 中 AUTOFLUID_IPC_HOST/AUTOFLUID_IPC_PORT 指向该端点。
3. 如果直接连接服务器，检查 .env 中 AUTOFLUID_SERVER_HOST 或 AUTOFLUID_IPC_HOST 是否正确。
"@
}

Push-Location $ProjectRoot
try {
    Assert-DaemonIpcReady
    $dashboard = Get-DashboardStatus

    if (-not $ConfigName) {
        $ConfigName = [int](Read-Host "请输入构型名称")
    }
    $steps = Get-ConfigStatuses -Dashboard $dashboard -Name $ConfigName
    if ($null -eq $steps) {
        throw "未在 daemon status 中找到构型 $ConfigName"
    }

    $sourceWorkstation = Get-ConfigWorkstation -Dashboard $dashboard -Name $ConfigName
    if (-not $sourceWorkstation) {
        throw "构型 $ConfigName 尚未绑定到具体工作站"
    }

    Write-Host "构型 $ConfigName 当前位于: $sourceWorkstation"
    Write-Host ("状态: transfer={0}, meshing={1}, solver={2}, postprocess={3}" -f `
        $steps.transfer, $steps.meshing, $steps.solver, $steps.postprocess)

    if (Test-AllStepsCompleted -Steps $steps) {
        Write-Host "构型 $ConfigName 已全部 Completed，不执行迁移。"
        exit 0
    }

    $engineStatus = [string]$dashboard.engine.engine_status
    if ($engineStatus -eq "running") {
        throw "pipeline 当前为 running；请先执行 pause 后再迁移。"
    }

    if (-not $TargetWorkstation) {
        $TargetWorkstation = (Read-Host "请输入目标工作站 (WS-A/WS-B/WS-C/WS-D)").Trim().ToUpperInvariant()
    }
    if ($TargetWorkstation -notin @("WS-A", "WS-B", "WS-C", "WS-D")) {
        throw "目标工作站无效: $TargetWorkstation"
    }
    if ($TargetWorkstation -eq $sourceWorkstation) {
        Write-Host "目标工作站与当前工作站相同，无需迁移。"
        exit 0
    }

    $sourceHealth = Get-WorkstationHealth -Dashboard $dashboard -WorkstationId $sourceWorkstation
    $targetHealth = Get-WorkstationHealth -Dashboard $dashboard -WorkstationId $TargetWorkstation
    if ($sourceHealth -ne "ok" -or $targetHealth -ne "ok") {
        Write-Host "工作站 SSH health 非 ok，尝试 worker start 刷新一次..."
        Invoke-AutoFluidCliJson worker start | Out-Null
        $dashboard = Get-DashboardStatus
        $sourceHealth = Get-WorkstationHealth -Dashboard $dashboard -WorkstationId $sourceWorkstation
        $targetHealth = Get-WorkstationHealth -Dashboard $dashboard -WorkstationId $TargetWorkstation
    }
    if ($sourceHealth -ne "ok" -or $targetHealth -ne "ok") {
        throw "SSH health 不满足迁移要求: $sourceWorkstation=$sourceHealth, $TargetWorkstation=$targetHealth"
    }

    $result = Invoke-AutoFluidCliJson migrate-load $ConfigName $TargetWorkstation
    $data = $result.data
    Write-Host "迁移完成: $($data.source_workstation_id) -> $($data.target_workstation_id), mode=$($data.mode)"
    foreach ($file in @($data.copied_files)) {
        Write-Host "已复制: $($file.source_path) -> $($file.target_path) ($($file.size) bytes)"
    }
    foreach ($file in @($data.deleted_source_files)) {
        Write-Host "已删除源文件: $($file.path)"
    }
    foreach ($warning in @($data.warnings)) {
        Write-Warning $warning
    }
} finally {
    Pop-Location
}
