# Daemon 启动流程审查报告：WARNING 级别日志与 SSH 连接错误问题

> **审查日期**：2026-06-25  
> **审查范围**：`daemon start` / `worker start` 及相关启动流程中的 WARNING 及以上级别日志输出  
> **分支**：`codex/three-workstation-dynamic-settings`

---

## 问题概述

- **现象**：`daemon start`（或相关启动指令）后，后台引擎立刻在日志 / TUI 详细信息栏中输出大量 SSH 连接错误 / 配置告警类 WARNING 及以上级别报错。
- **影响范围**：TUI 客户端详细信息栏（`log_buffer`）、daemon 日志文件、用户对系统状态的信心。
- **问题域**：配置校验、SSH 连通性管理、状态上报。
- **设计预期**：如果程序各部分没有问题，用户的任何行为都不应该导致详细信息栏输出 WARNING 级别及以上报错。

---

## 调查过程

### 已检查的资料

| 文件 | 发现 |
|------|------|
| `main.py` (全篇) | `daemon start` 在 TUI 中映射为 `CommandResult::StartDaemon`，由 `daemon_mgr.rs::start_with_ipc` 处理。非 server 模式下直接 launch `start_daemon.py` 子进程。 |
| `start_daemon.py` (全篇) | 加载 `engine.daemon.main()`，不做额外网络操作。 |
| `engine/daemon.py` (全篇，~1600行) | `PipelineDaemon.start()` 调用链：`acquire_process_lock` → `reload_config_from_toml` → **`validate_config()`** → `StateManager` 初始化 → IPC 启动 → **`_start_workstation_ssh_health_monitor()`**（仅 server 模式）。`handle_worker_start` 调用 **`_refresh_workstation_ssh_checks(connect=True)`** 触发全量 SSH 连接。`handle_worker_register` 在收到构型数据后也调用同函数。 |
| `engine/config.py` `validate_config()` (L930-970) | 无条件检查 SSH 密码、SW/Excel/SC 文件存在性，所有告警以 **WARNING** 级别写入日志。 |
| `engine/task_runner.py` `get_ssh()` (L124-168) | 若未连接则调用 `ssh.connect()`，失败时以 **ERROR** 级别记录。 |
| `utils/ssh_client.py` `connect()` (L78-118) | 连接失败以 **ERROR** 记录 `[SSH] SSH 连接失败: {e}`。 |
| `engine/daemon.py` `_refresh_workstation_ssh_checks()` (L1201-1260) | 对 `WORKSTATIONS` 中每个工作站调用 `runner.get_ssh(ws_id)`（`connect=True` 时）或返回历史缓存（`connect=False` 时）。每个失败以 **WARNING** 记录。 |
| `executor/cleaner.py` `run_system_check()` (L352-440) | 对每个工作站主动调用 `ssh.is_connected()` + `ssh.check_system()`，失败以 **ERROR** 记录。由 `check` 命令触发，不是 `daemon start` 触发。 |
| `executor/cleaner.py` `_build_workstation_checks()` (L587-615) | 在 server 模式且工作站为私网 IP 且无 `reachable_host` 时，标记 `severity: "error"`。 |
| `autofluid_config.toml` (全篇) | 三个工作站 WS-A/B/C 均为 `172.17.x.x` 私网地址。WS-A 有 `reachable_host` 配置但 WS-B/WS-C 没有 `reachable_host`/`reachable_port`。WS-C 注释标注 "currently offline/unreachable"。 |
| `autofluid-tui/src/lib.rs` `poll_worker_health_watchdog()` (L364-392) | 当 `server_to_workstation_ssh` 为 `"disconnected"` 或 `"error"` 时，在 TUI 信息栏推送 SSH 隧道异常警告。 |
| `autofluid-tui/src/daemon_mgr.rs` `start_with_ipc()` (L663-700) | 已连接时直接返回，不触发任何后台操作。未连接时 launch 子进程后进入 IPC 重连等待。 |

### 关键发现

1. **`validate_config()` 在 daemon 启动时无条件以 WARNING 级别输出配置告警**：
   - `daemon.py:start()` L242-245：对每个配置告警调用 `logger.warning(f"[CONFIG] {w}")`
   - 如果未设 `AUTOFLUID_SSH_PASSWORD`，每次 daemon 启动都会输出 `[CONFIG] SSH 密码未设置！`
   - 本地模式下可能不需要远程 SSH，但这仍然是 WARNING 级别

2. **`handle_worker_start` → `_refresh_workstation_ssh_checks(connect=True)` 对全量工作站发起 SSH 连接**：
   - `daemon.py:1170`：`results.update(self._refresh_workstation_ssh_checks())`
   - `task_runner.py:get_ssh()` 中 `ssh.connect()` 失败 → `logger.error("SSH 重连失败")`
   - `ssh_client.py:connect()` 中捕获异常 → `logger.error("[SSH] SSH 连接失败")`
   - `daemon.py:_refresh_workstation_ssh_checks()` 中捕获异常 → `logger.warning("[Worker] 工作站 ... SSH 连通检查失败")`
   - **一次 `worker start` 对三个工作站分别尝试连接，产生 3×(ERROR+WARNING) 条日志**

3. **Server 模式下 `handle_worker_register` 也会自动触发 SSH 连通性检查**：
   - `daemon.py:916`：`self._refresh_workstation_ssh_checks()`（默认 `connect=True`）
   - 这在 LocalWorker 注册并提供构型数据后立即触发，用户可能完全不知情

4. **`_build_health_snapshot()` 上报的数据驱动 TUI 的 watchdog 告警**：
   - `daemon.py:1426-1480`：`_build_health_snapshot()` 中 `workstation_ssh_details` 使用 `_last_worker_ssh_checks` 缓存
   - TUI 的 `poll_worker_health_watchdog` 检测到 `server_to_workstation_ssh` 为 `disconnected`/`error` 时显示告警
   - 这意味着任何一次 SSH 连通检查失败后，TUI 的"详细信息栏"会持续显示 SSH 隧道异常

5. **未区分"预期离线"与"意外离线"工作站**：
   - `autofluid_config.toml` 中 WS-C 明确标注 `notes = "currently offline/unreachable"`
   - 但代码对所有工作站一视同仁，WS-C 的连接失败与 WS-A 的连接失败在日志严重级别上完全相同

6. **`_build_workstation_checks()` 对无 `reachable_host` 的私网工作站标记 `severity: "error"`**：
   - `cleaner.py:587-615`：当 server 模式且主机为私网 IP 且无显式可达配置时，标记为 error
   - 这在 `check` 命令输出中显示为红色错误，但问题本身是部署配置问题而非运行时故障

---

## 根因分析

### 假设 1：`validate_config()` 在 daemon 启动时无条件以 WARNING 级别输出配置检查项（置信度：**高**）

- **证据链**：`PipelineDaemon.start()` → L232 `reload_config_from_toml()` → L234 `ensure_directories()` → L235 `validate_config()` → L237-239 `for w in config_warnings: logger.warning(f"[CONFIG] {w}")`
- **代码引用**：`engine/config.py:930-970`（`validate_config`）、`engine/daemon.py:233-239`
- **影响**：本地模式下用户仅启动 daemon 查看状态，不打算使用远程功能，但仍收到 `SSH 密码未设置` 的 WARNING 告警

### 假设 2：`handle_worker_start` / `handle_worker_register` 无条件尝试所有工作站 SSH 连接（置信度：**高**）

- **证据链**：`handle_worker_start` → L1170 `_refresh_workstation_ssh_checks()`（`connect=True`）→ L1212 `for ws in WORKSTATIONS:` → L1218 `ssh = self.runner.get_ssh(ws_id)` → `task_runner.py:get_ssh()` L153 `if not ssh.connect(): logger.error("SSH 重连失败")` → `ssh_client.py:connect()` L116 `logger.error("[SSH] SSH 连接失败")` → `daemon.py:_refresh_workstation_ssh_checks` L1246 `logger.warning("[Worker] 工作站 ... SSH 连通检查失败")`
- **代码引用**：`engine/daemon.py:1149-1198`、`engine/daemon.py:1201-1260`、`engine/task_runner.py:124-168`、`utils/ssh_client.py:78-118`
- **影响**：三个工作站中只要有一个不可达，就会产生 WARNING/ERROR 日志。全部不可达时产生 3 组错误日志

### 假设 3：健康快照将一次性的 SSH 检查失败持久化为持续可见的状态（置信度：**中**）

- **证据链**：`_refresh_workstation_ssh_checks` 将结果写入 `self._last_worker_ssh_checks` → `_build_health_snapshot()` 读取该值报告 `server_to_workstation_ssh` → TUI `poll_worker_health_watchdog` 检测到后显示告警
- **代码引用**：`engine/daemon.py:1260`、`engine/daemon.py:1426-1480`、`autofluid-tui/src/lib.rs:364-392`
- **影响**：即使 SSH 检查失败是一次性的（工作站暂时不可达），TUI 会持续显示告警直到下一次成功的 `worker start` 刷新状态

### 假设 4：Server 模式 LocalWorker 注册时隐式触发 SSH 检查（置信度：**中**）

- **证据链**：`handle_worker_register` → L916 `self._refresh_workstation_ssh_checks()` → 触发全量 SSH 连接
- **代码引用**：`engine/daemon.py:908-917`
- **影响**：server 模式下，用户执行 `start` 命令 → daemon 自动唤起 LocalWorker → LocalWorker 注册 → 自动 SSH 检查，用户可见意料之外的 SSH 错误日志

---

## 修复方案

### 方案 A：将 `validate_config()` 中的非关键检查降级为 INFO ⭐⭐⭐ 推荐

- **描述**：将 `validate_config()` 中与环境相关的检查项（SSH 密码、SW/Excel/SC 文件存在性）从 `logger.warning` 降级为 `logger.info`。仅在 `config_warnings` 列表中保留事项供 `check` 命令展示。关键检查（如 server 模式缺少 `reachable_host`）保留 WARNING 级别。
- **涉及文件**：
  - `engine/config.py` — `validate_config()` 中为每个告警增加严重级别分类，local 模式的文件存在性检查降级为 INFO
  - `engine/daemon.py` — `start()` 中按严重级别分流日志输出
- **改动量**：20-50 行
- **复杂度**：简单
- **副作用风险**：
  - 低。仅改变日志级别，不影响程序行为
  - 需确保 `_config_warnings` 列表仍包含所有告警项供 `handle_get_dashboard` 上报

### 方案 B：为 `_refresh_workstation_ssh_checks` 增加"惰性首次连接"选项 ⭐⭐⭐ 推荐

- **描述**：在 `_refresh_workstation_ssh_checks(connect=True)` 中增加"首次惰性"模式：如果 `_last_worker_ssh_checks` 为空（即从未进行过检查），则不主动发起连接，而是返回 `"unchecked"` 状态。用户通过 `worker start` 显式命令时才进行首次连接。后续健康监控中的 `connect=False` 保持不变。
- **涉及文件**：
  - `engine/daemon.py` — `_refresh_workstation_ssh_checks()` 增加 `skip_first_connect` 参数
  - `engine/daemon.py` — `handle_worker_register()` 调用时传入 `connect=False`（仅 server 模式 LocalWorker 自动注册场景）
- **改动量**：< 20 行
- **复杂度**：简单
- **副作用风险**：
  - 中低。改变了 server 模式 LocalWorker 注册后的 SSH 检查时机
  - 需确保 `handle_worker_start` 仍然主动连接（用户显式意图）
  - `_last_worker_ssh_checks` 可能保持空值，TUI health panel 会显示 "unknown"，需要 UI 上做相应适配

### 方案 C：为工作站配置增加 `enabled` 字段以支持按需跳过 ⭐⭐ 可行

- **描述**：在 `autofluid_config.toml` 的 `[[workstations]]` 中增加 `enabled = true/false` 字段。`enabled = false` 的工作站在 SSH 健康检查、`worker start`、系统自检中均跳过。WS-C（标注 "currently offline"）可设为 `enabled = false`。
- **涉及文件**：
  - `autofluid_config.toml` — 为 WS-C 增加 `enabled = false`
  - `engine/config.py` — `WorkstationConfig` TypedDict 增加 `enabled` 字段，增加 `get_enabled_workstations()` 辅助函数
  - `engine/daemon.py` — `_refresh_workstation_ssh_checks()`、`_build_health_snapshot()` 中过滤 `enabled = false` 的工作站
  - `executor/cleaner.py` — `_configured_workstation_ids()`、`_build_workstation_checks()` 中过滤
- **改动量**：40-80 行
- **复杂度**：中等
- **副作用风险**：
  - 中。影响多个模块的工作站迭代逻辑，需全面回归测试
  - 配置向后兼容（默认 `enabled = true`）

### 方案 D：在 `RemoteWorkstation.connect()` 中增加快速失败和静默重试 ⭐⭐ 可行

- **描述**：在 SSH 连接失败时，将日志级别从 `ERROR` 降为 `WARNING`（因为这是预期的网络故障而非程序缺陷），并增加指数退避重试机制（而非立即报告失败）。仅在多次重试均失败后才报告。
- **涉及文件**：
  - `utils/ssh_client.py` — `connect()` 方法增加重试逻辑和日志级别调整
- **改动量**：20-30 行
- **复杂度**：简单
- **副作用风险**：
  - 中。改变了 SSH 连接的错误语义，可能影响依赖 `ERROR` 级别的监控/告警系统
  - 增加重试会延长 `worker start` 的响应时间（需设置合理的超时）

### 方案 E：将 TUI watchdog 的 SSH 告警从自动轮询改为按需展示 ⭐ 可行

- **描述**：`poll_worker_health_watchdog` 当前在每次 dashboard 轮询时检查 SSH 状态。改为仅在用户明确执行 `worker start`/`check` 后，或在 SSH 状态从 `ok` 变为 `disconnected` 时（而非初始 `unknown` 状态），才向用户报告。
- **涉及文件**：
  - `autofluid-tui/src/lib.rs` — `poll_worker_health_watchdog()` 增加状态变迁检测
  - `autofluid-tui/src/state/` — health_info 增加前次状态对比
- **改动量**：20-40 行
- **复杂度**：简单
- **副作用风险**：
  - 低。仅在 TUI 层面改变告警触发条件，不影响后端

---

## 建议下一步

1. **优先实施方案 A + B**：方案 A 消除 daemon 启动时的非关键 WARNING，方案 B 消除 `worker_start`/worker 注册时的非预期 SSH 连接。两者改动量小、风险低，能解决核心问题。

2. **考虑方案 C**：为离线工作站（WS-C）增加 `enabled = false` 标记，从根源上避免对已知不可达目标的连接尝试。

3. **后续验证步骤**：
   - 在本地模式下执行 `daemon start`，验证 daemon 日志中无 WARNING 级别配置告警
   - 在本地模式下执行 `worker start`，验证仅 WS-A（可达）触发 SSH 连接，不可达工作站静默或 INFO 级别
   - 在 server 模式（ocar）下执行 `daemon start` + `start`，验证 LocalWorker 注册不触发级联 SSH 错误
   - 验证 TUI dashboard 的 health panel 在初始状态下不显示误导性的红色/黄色告警
