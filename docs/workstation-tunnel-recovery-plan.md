# 工作站反向隧道自恢复改动与测试方案

> 文档日期：2026-06-17
> 适用范围：server mode 下 S->W SSH 反向隧道 `127.0.0.1:2222 -> workstation:22`
> 目标：补齐“工作站端 supervisor 自身死亡后恢复”和“daemon 后台主动发现并重连”两类能力

## 1. 当前结论

当前代码已经具备一层自恢复能力：

- `scripts/start_workstation_reverse_tunnel.ps1` 会启动隐藏 `-Monitor` supervisor。
- supervisor 通过 `Test-RemoteTunnelEndpoint` 检查服务器侧 `127.0.0.1:2222` 是否可达。
- SSH 子进程退出后，supervisor 会重新启动 `ssh -R`。
- daemon 侧 `TaskRunner.get_ssh()` 和 `RemoteWorkstation.ensure_connected()` 会在下一次 SSH 操作时按需重连。

仍存在两个缺口：

1. 如果隐藏 PowerShell supervisor 进程自身死亡，当前没有更高层 watchdog 自动拉起它。
2. daemon 没有后台健康线程主动刷新 `server_to_workstation_ssh`，dashboard 可能短时间显示旧状态，SSH 重连也只在下一次业务操作时触发。

## 2. 推荐方案

采用组合方案：

1. 工作站侧增加 Windows Task Scheduler watchdog，定期执行现有隧道脚本。
2. daemon 侧增加 server mode 专用 SSH 健康线程，周期性调用现有 SSH 检查和重连路径。

这样可以覆盖两个独立故障面：

- SSH 子进程死亡：现有 supervisor 重启。
- supervisor 进程死亡：计划任务 watchdog 重新执行脚本，脚本发现 monitor 不存在后重建。
- daemon Paramiko 连接失效：后台健康线程主动 `is_connected()` / `connect()`，并刷新 dashboard 状态。

不建议只做 daemon 侧改动。daemon 运行在 ocar，无法重启工作站本地的 PowerShell supervisor；隧道入口仍应由工作站端负责。

不建议只做 TUI 侧轮询。TUI 退出或本地前端关闭后，server mode 仍应允许远程阶段继续运行。

## 3. 工作站端 watchdog 改动

### 3.1 修改文件

- `scripts/start_workstation_reverse_tunnel.ps1`
- `autofluid-tui/src/worker_mgr.rs`
- `utils/process_utils.py`

### 3.2 PowerShell 脚本新增参数

在 `scripts/start_workstation_reverse_tunnel.ps1` 的 `param(...)` 中新增：

```powershell
[switch]$InstallWatchdog,
[switch]$UninstallWatchdog,
[switch]$NoWatchdog
```

含义：

- 默认启动隧道时自动确保 watchdog 存在。
- `-InstallWatchdog` 只安装或刷新计划任务，不强制等待隧道可达。
- `-UninstallWatchdog` 删除对应计划任务，用于 `worker stop` / `quit full` 清理。
- `-NoWatchdog` 用于测试或手动诊断，避免脚本递归安装任务。

### 3.3 计划任务命名

按 tunnel kind 和远端端口生成稳定任务名：

```text
AutoFluidTunnelWatchdog-Workstation-2222
AutoFluidTunnelWatchdog-LocalWorker-2223
```

其中 Workstation 使用 `AUTOFLUID_SSH_REACHABLE_PORT`，默认 `2222`；LocalWorker 使用 `AUTOFLUID_WORKER_SSH_PORT`，默认 `2223`。

### 3.4 新增函数

在 `scripts/start_workstation_reverse_tunnel.ps1` 中新增：

```powershell
function Get-TunnelWatchdogTaskName {
    param([Parameter(Mandatory = $true)][int]$RemotePort)
    return "AutoFluidTunnelWatchdog-$TunnelKind-$RemotePort"
}

function Install-TunnelWatchdogTask {
    param(
        [Parameter(Mandatory = $true)][int]$RemotePort,
        [Parameter(Mandatory = $true)][string]$PowerShellExe
    )

    $taskName = Get-TunnelWatchdogTaskName -RemotePort $RemotePort
    $scriptPath = $PSCommandPath
    $arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$scriptPath`" -TunnelKind $TunnelKind -NoWatchdog"

    $existing = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    if ($null -ne $existing) {
        return $taskName
    }

    $action = New-ScheduledTaskAction -Execute $PowerShellExe -Argument $arguments
    $trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
        -RepetitionInterval (New-TimeSpan -Minutes 1) `
        -RepetitionDuration ([TimeSpan]::MaxValue)
    $settings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -MultipleInstances IgnoreNew `
        -ExecutionTimeLimit (New-TimeSpan -Minutes 2)

    Register-ScheduledTask -TaskName $taskName `
        -Action $action `
        -Trigger $trigger `
        -Settings $settings `
        -Description "AutoFluid $TunnelKind reverse tunnel watchdog for remote port $RemotePort" `
        -Force | Out-Null
    return $taskName
}

function Uninstall-TunnelWatchdogTask {
    param([Parameter(Mandatory = $true)][int]$RemotePort)
    $taskName = Get-TunnelWatchdogTaskName -RemotePort $RemotePort
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
}
```

执行顺序：

1. 解析 `$remotePort` 后，如果 `-UninstallWatchdog` 为真，删除任务并退出。
2. 非 `-Monitor`、非 `-Check`、非 `-NoWatchdog` 时，先 `Install-TunnelWatchdogTask`。
3. 继续执行现有逻辑：如果隧道可达则复用；不可达则 `Start-ReverseTunnelSupervisor`。

计划任务每分钟调用一次普通脚本入口。普通入口是幂等的：隧道可达且 monitor 存在时直接退出；monitor 不存在或隧道不可达时会重建 supervisor。

### 3.5 Rust TUI 清理路径

`autofluid-tui/src/worker_mgr.rs` 当前 `stop_workers_for_project()` 会清理 `tunnel_workstation.pid` 和 `tunnel_localworker.pid`。需要增加一个 PowerShell 调用：

```text
scripts/start_workstation_reverse_tunnel.ps1 -TunnelKind Workstation -UninstallWatchdog
scripts/start_workstation_reverse_tunnel.ps1 -TunnelKind LocalWorker -UninstallWatchdog
```

触发位置：

- `worker stop`
- `quit full`
- 已有 `stop_workers_for_project(project_dir)` 清理 helper

要求：

- 删除计划任务失败只记录 warning，不阻塞 worker stop。
- PID 文件清理仍保留，避免影响现有进程清理逻辑。

### 3.6 Python 清理路径

`utils/process_utils.py` 目前清理 PID 文件记录的 worker/tunnel 进程。建议增加：

```python
def cleanup_tunnel_watchdog_tasks(project_dir: str) -> None:
    ...
```

实现方式：

- 仅 Windows 下执行。
- 调用 `powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts/start_workstation_reverse_tunnel.ps1 -TunnelKind <kind> -UninstallWatchdog`。
- 对 Workstation 和 LocalWorker 都执行。
- 异常只写 warning。

这样 Python 侧 `worker_stop` 或测试清场也能清理计划任务，避免 TUI 与 Python 清理边界不一致。

## 4. daemon 后台主动发现并重连

### 4.1 修改文件

- `engine/daemon.py`
- `engine/config.py`
- `tests/test_daemon_dashboard.py`
- `tests/test_scheduler_modules.py`

### 4.2 配置项

新增环境变量：

```text
AUTOFLUID_WORKSTATION_SSH_HEALTH_INTERVAL=30
```

规则：

- 仅 server mode 生效。
- 默认 30 秒。
- 小于等于 0 表示禁用后台 SSH 健康线程。
- 每次循环使用现有 `_refresh_workstation_ssh_checks()`，不新增第二套 SSH 连接逻辑。

### 4.3 daemon 新增字段

`PipelineDaemon.__init__()` 新增：

```python
self._ssh_health_thread: threading.Thread | None = None
self._ssh_health_stop_event = threading.Event()
self._ssh_health_interval_seconds = get_workstation_ssh_health_interval()
```

`engine/config.py` 新增：

```python
def get_workstation_ssh_health_interval() -> float:
    raw = os.getenv("AUTOFLUID_WORKSTATION_SSH_HEALTH_INTERVAL", "30")
    try:
        value = float(raw)
    except ValueError:
        return 30.0
    return value
```

### 4.4 daemon 新增方法

```python
def _start_workstation_ssh_health_monitor(self) -> None:
    if not is_server_mode():
        return
    if self._ssh_health_interval_seconds <= 0:
        return
    if self._ssh_health_thread and self._ssh_health_thread.is_alive():
        return
    self._ssh_health_stop_event.clear()
    self._ssh_health_thread = threading.Thread(
        target=self._workstation_ssh_health_loop,
        name="AutoFluidWorkstationSshHealth",
        daemon=True,
    )
    self._ssh_health_thread.start()

def _workstation_ssh_health_loop(self) -> None:
    while not self._ssh_health_stop_event.wait(self._ssh_health_interval_seconds):
        self._run_workstation_ssh_health_check_once()

def _run_workstation_ssh_health_check_once(self) -> dict[str, Any]:
    if self.runner is None:
        return {"ssh_checks": {}, "ssh_targets": {}}
    try:
        return self._refresh_workstation_ssh_checks()
    except Exception as exc:
        logger.warning("[ServerMode] 后台工作站 SSH 健康检查失败: %s", exc)
        return {"ssh_checks": {}, "ssh_targets": {}}

def _stop_workstation_ssh_health_monitor(self) -> None:
    self._ssh_health_stop_event.set()
    thread = self._ssh_health_thread
    if thread and thread.is_alive():
        thread.join(timeout=2.0)
    self._ssh_health_thread = None
```

启动位置：

- `PipelineDaemon.start()` 创建 `TaskRunner` 后、IPC server 启动前或启动后均可。
- 推荐放在 runner 创建完成后、IPC ready 前；这样 daemon ready 时后台检查已经开始。

停止位置：

- `PipelineDaemon.shutdown()` 开头调用 `_stop_workstation_ssh_health_monitor()`。

### 4.5 状态语义

后台健康线程只做两件事：

1. 调用现有 `runner.get_ssh(ws_id)`，触发 `is_connected()` 和必要时 `connect()`。
2. 更新 `_last_worker_ssh_checks`，让 dashboard 和 `_server_mode_ssh_not_ready()` 使用较新的结果。

它不负责：

- 启动或杀死远程 meshing/solver 任务。
- 改写步骤状态。
- 清理远程 PID 文件。
- 重启工作站端 supervisor。

这样可以避免把“连通性恢复”和“业务调度恢复”混在一个线程里。

## 5. 测试方案

### 5.1 PowerShell watchdog 单元测试

新增测试文件：

```text
tests/test_workstation_tunnel_watchdog.py
```

测试方式：

- 用临时目录复制 `scripts/start_workstation_reverse_tunnel.ps1`。
- 通过 PowerShell 静态解析或命令输出验证函数存在。
- 对计划任务命令采用 `-WhatIf` 风格不合适，因为 `Register-ScheduledTask` 支持有限；测试中不真正注册系统任务。
- 推荐把 PowerShell 任务名和参数构造逻辑拆为纯函数，测试这些纯函数输出。

建议测试用例：

1. `Get-TunnelWatchdogTaskName` 对 Workstation 2222 返回 `AutoFluidTunnelWatchdog-Workstation-2222`。
2. `Get-TunnelWatchdogTaskName` 对 LocalWorker 2223 返回 `AutoFluidTunnelWatchdog-LocalWorker-2223`。
3. watchdog action 参数包含 `-NoWatchdog`，防止计划任务运行时重复安装自身。
4. 普通启动路径包含 `Install-TunnelWatchdogTask`，`-Check` 和 `-Monitor` 路径不安装 watchdog。

如果不想引入 PowerShell 函数级测试，也可以在 Rust 侧做集成验证：创建 fake PowerShell 脚本，断言 `worker stop` 会用 `-UninstallWatchdog` 调用两次。这个测试不能覆盖脚本内部任务注册，但能覆盖 TUI 清理契约。

### 5.2 Rust TUI 清理测试

修改：

```text
autofluid-tui/src/worker_mgr.rs
```

新增或扩展现有测试：

```rust
#[test]
fn stop_workers_for_project_uninstalls_tunnel_watchdogs() {
    // Arrange: fake scripts/start_workstation_reverse_tunnel.ps1 writes args to marker file.
    // Act: WorkerManager::stop_workers_for_project(project_dir)
    // Assert: marker contains both:
    // -TunnelKind Workstation -UninstallWatchdog
    // -TunnelKind LocalWorker -UninstallWatchdog
}
```

验证命令：

```powershell
cd autofluid-tui
cargo test stop_workers_for_project_uninstalls_tunnel_watchdogs
```

### 5.3 daemon 健康线程单元测试

新增测试放在：

```text
tests/test_daemon_dashboard.py
tests/test_scheduler_modules.py
```

建议测试用例：

1. `test_ssh_health_check_once_refreshes_last_worker_checks`
   - 构造 fake runner。
   - 第一次 `get_ssh().is_connected()` 返回 False。
   - 第二次返回 True。
   - 调用 `_run_workstation_ssh_health_check_once()` 后断言 `_last_worker_ssh_checks` 更新。

2. `test_ssh_health_monitor_disabled_when_interval_zero`
   - 设置 `AUTOFLUID_SERVER_MODE=server`。
   - 设置 `AUTOFLUID_WORKSTATION_SSH_HEALTH_INTERVAL=0`。
   - 调用 `_start_workstation_ssh_health_monitor()`。
   - 断言 `_ssh_health_thread is None`。

3. `test_ssh_health_monitor_stops_on_shutdown`
   - 设置较短 interval，例如 `0.01`。
   - 启动 monitor。
   - 调用 `_stop_workstation_ssh_health_monitor()`。
   - 断言线程已停止。

4. `test_dashboard_uses_latest_background_ssh_check`
   - fake runner 没有 `_ssh_pool` 活跃连接。
   - 预置 `_last_worker_ssh_checks = {"WS-A": "disconnected"}`。
   - dashboard 返回 `server_to_workstation_ssh == "disconnected"`。
   - 后台 check 更新为 `ok` 后，dashboard 返回 `ok`。

验证命令：

```powershell
.venv\Scripts\python.exe -m pytest tests/test_daemon_dashboard.py -q
.venv\Scripts\python.exe -m pytest tests/test_scheduler_modules.py -q -k "workstation_ssh or ssh_health or worker_start"
```

### 5.4 端到端手动验证

在真实 server mode 环境执行：

1. 启动 TUI，执行 `worker start`。
2. 在 Windows 上找到 `AutoFluidTunnelWatchdog-Workstation-2222` 计划任务。
3. 确认 `data/tunnel_workstation.pid` 指向当前 supervisor PID。
4. 杀死 `ssh.exe` 子进程，观察 supervisor log 出现重启记录。
5. 杀死 PowerShell supervisor 进程，等待最多 60 秒。
6. 计划任务再次执行脚本，重建 supervisor，`127.0.0.1:2222` 恢复可达。
7. 在 ocar 上断开或等待 Paramiko stale，确认 daemon 日志出现后台 SSH 健康检查恢复记录。
8. TUI dashboard 最终显示：

```text
local_worker_online=true
server_to_local_ssh=ok
server_to_workstation_ssh=ok
```

建议额外观察日志：

```text
%TEMP%\autofluid-workstation-tunnel-2222.supervisor.log
utils.ssh_client.log
daemon 日志中的 [ServerMode] 后台工作站 SSH 健康检查
```

## 6. 验收标准

代码改动完成后，必须满足：

1. 工作站端 `worker start` 后存在 `AutoFluidTunnelWatchdog-Workstation-2222` 计划任务。
2. 杀死 `ssh.exe` 子进程后，原 supervisor 能重启 SSH。
3. 杀死 supervisor PowerShell 进程后，计划任务能在 60 秒内重建 supervisor。
4. daemon 在无业务 SSH 操作时，也会定期刷新 `_last_worker_ssh_checks`。
5. dashboard 不再长期依赖 stale `connection_is_active()` 结果。
6. `worker stop` / `quit full` 后计划任务、PID 文件、隧道进程均被清理。
7. 相关 Python/Rust 测试通过。

完整验证命令：

```powershell
.venv\Scripts\python.exe -m pytest tests/test_daemon_dashboard.py -q
.venv\Scripts\python.exe -m pytest tests/test_scheduler_modules.py -q -k "workstation_ssh or ssh_health or worker_start"
cd autofluid-tui
cargo test stop_workers_for_project_uninstalls_tunnel_watchdogs
cargo test -- --test-threads=1
```

## 7. 风险与边界

- Windows Task Scheduler 可能需要当前用户有注册计划任务权限；如果权限不足，脚本应降级为 warning，并保留现有 supervisor 行为。
- 计划任务不应保存密码、token 或 SSH 密钥内容；它只运行现有脚本，由脚本按当前环境和 SSH 配置建立隧道。
- daemon 后台健康线程会增加周期性 SSH 登录尝试；默认 30 秒是保守值，不建议低于 10 秒。
- 健康线程不能直接改步骤状态，否则可能和 scheduler/retry/remote task recovery 产生竞态。
- `worker stop` 必须清理 watchdog，否则用户停止后计划任务会重新拉起隧道。

## 8. 建议实施顺序

1. 先实现 PowerShell watchdog 任务安装/卸载和脚本幂等测试。
2. 再实现 TUI/Python 清理路径，确保 `worker stop` 不留下计划任务。
3. 实现 daemon 单次健康检查方法 `_run_workstation_ssh_health_check_once()` 并写单测。
4. 最后接入 daemon 后台线程，并验证 shutdown 能干净退出。
5. 做一次真实断连演练：先杀 SSH 子进程，再杀 supervisor 进程，最后观察 daemon dashboard 恢复。
