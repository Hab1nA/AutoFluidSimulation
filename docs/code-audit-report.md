# AutoFluid 全面代码审查报告

> **审查日期**：2026-05-19
> **审查范围**：Python 后端 (engine/executor/ipc/utils/)、Rust TUI (autofluid-tui/src/)、C# Bridge (bridge/SpaceClaimBridge/)
> **当前分支**：fix/improve-spaceclaim-schedule
> **总体评分**：**B+**（良好，有改进空间）

---

## 目录

1. [审查方法论](#1-审查方法论)
2. [总体评估](#2-总体评估)
3. [P0 — 高优先级问题（核心功能/稳定性）](#3-p0--高优先级问题)
4. [P1 — 中优先级问题（稳定性/可维护性）](#4-p1--中优先级问题)
5. [P2 — 低优先级问题（代码质量改进）](#5-p2--低优先级问题)
6. [实施计划](#6-实施计划)

---

## 1. 审查方法论

审查覆盖以下维度：

| 维度 | 说明 |
|------|------|
| 代码质量 | 可读性、可维护性、性能优化 |
| 编码规范性 | 是否符合 `docs/code-style-guide.md` 编码标准 |
| 代码健壮性 | 错误处理、边界条件、异常捕获 |
| 功能正确性 | 逻辑错误、竞态条件、资源泄漏 |
| 接口匹配度 | 模块间接口一致性、参数匹配 |
| 依赖关系 | 循环依赖、模块耦合度 |

---

## 2. 总体评估

### 2.1 亮点 ✅

- **架构设计优秀**：模块职责清晰（State/Task/Scheduler/Executor/IPC 分层），扩展性好
- **文档完整**：几乎所有模块都有详细 docstring 和模块级注释
- **断点续传设计完善**：SQLite WAL + 文件检测实现可靠的暂停/恢复
- **错误处理覆盖全面**：COM 清理、SSH 重连、进程锁、超时处理均有考虑
- **编码规范高度一致**：命名、日志前缀、状态枚举等与 `code-style-guide.md` 一致
- **IPC 协议设计清晰**：JSON + `\n` 分隔、request_id 关联、Rust/Python 两端一致
- **Rust 命名完全规范**：snake_case 函数/变量、PascalCase 类型、SCREAMING_SNAKE_CASE 常量

### 2.2 问题分布

| 严重度 | Python | Rust | C# | 合计 |
|--------|--------|------|-----|------|
| 🔴 高 (P0) | 4 | 2 | 3 | **9** |
| 🟡 中 (P1) | 10 | 6 | 8 | **24** |
| 🟢 低 (P2) | 8 | 5 | 2 | **15** |

---

## 3. P0 — 高优先级问题

> 影响核心功能或系统稳定性，必须立即修复。

### P0-1 `_resume_paused_steps` Transfer/Meshing/Solver 步骤未实际入队

**文件**：`engine/scheduler/main.py` — `_resume_paused_steps()` 方法

**问题描述**：
断点续传扫描中，Transfer/Meshing/Solver 的 PAUSED/WAITING 步骤被直接设为 `STATUS_RUNNING`，但**没有实际入队或提交给 MeshingMonitor**。Worker 线程只从 `_sc_queue` 取 SC 任务，这些步骤将永远停在 Running 状态。

**问题代码**：
```python
# 当前逻辑（有缺陷）：
for cn, step in priority_enqueue:
    if step == "SC":
        self._enqueue_sc(cn, step_dir)
    elif step in ("Transfer", "Meshing", "Solver"):
        self.state.set_step_status(cn, step, STATUS_RUNNING)  # 仅改状态，未入队！
```

**解决方案**：
1. SC 步骤：保持现有 `_enqueue_sc()` 逻辑
2. Transfer 步骤：将构型推入 `_sc_queue`（由 Worker 线程在 `_process_single_config` 中检测断点续传跳过已完成的 SC，直接执行 Transfer）
3. Meshing 步骤：提交给 `MeshingMonitor.submit(cn)`
4. Solver 步骤：在屏障通过后由 `_dispatch_solver_tasks()` 统一处理，此处无需额外操作

```python
# 修复后逻辑：
for cn, step in priority_enqueue + normal_enqueue:
    if step == "SC":
        self._enqueue_sc(cn, step_dir)
    elif step == "Transfer":
        # Transfer 需要 SC 先完成 → 入 SC 队列让 Worker 处理
        # Worker._process_single_config 会检测 SC 已 Completed 并跳过
        self._enqueue_sc(cn, step_dir)
    elif step == "Meshing":
        # MeshingMonitor 管理 Meshing 生命周期
        if self.meshing_monitor is not None:
            self.state.set_step_status(cn, "Meshing", STATUS_WAITING)
            self.meshing_monitor.submit(cn)
    elif step == "Solver":
        # Solver 由屏障调度器统一管理，此处仅保持状态
        pass  # 不改变状态，等屏障通过后自动处理
```

**验证方式**：暂停后 resume，观察 Transfer/Meshing/Solver 步骤是否真正开始执行。

---

### P0-2 `_execute_with_retry` 三处重复实现

**文件**：
- `engine/scheduler/worker_pool.py` — `WorkerPoolManager._execute_with_retry()`
- `engine/scheduler/barrier.py` — `BarrierCoordinator._execute_with_retry()`
- `engine/scheduler/retry.py` — `RetryManager.execute_with_retry()`

**问题描述**：
三处实现**逻辑完全相同**（约 80 行），违反 DRY 原则。`RetryManager` 已存在但 `WorkerPoolManager` 和 `BarrierCoordinator` 未使用它，而是各自重新实现。

**解决方案**：
1. 将 `WorkerPoolManager._execute_with_retry` 和 `BarrierCoordinator._execute_with_retry` 替换为调用 `RetryManager.execute_with_retry`
2. 在 `WorkerPoolManager` 和 `BarrierCoordinator` 的构造函数中注入 `RetryManager` 实例
3. 删除 `worker_pool.py` 和 `barrier.py` 中的重复方法

**接口变更**：
```python
# WorkerPoolManager.__init__ 新增参数：
self._retry_manager = retry_manager  # RetryManager 实例

# BarrierCoordinator.__init__ 新增参数：
self._retry_manager = retry_manager  # RetryManager 实例

# 调用方式变更：
# 旧：self._execute_with_retry(config_name, "SC", self.runner.execute_sc_step)
# 新：self._retry_manager.execute_with_retry(config_name, "SC", self.runner.execute_sc_step)
```

**注意**：`RetryManager` 的 `pause_aware_sleep` 已委托给 `utils.pause_aware_sleep`，无需额外适配。

**验证方式**：运行 `python -m pytest tests/ -v`，确认现有测试全部通过。

---

### P0-3 `SCProcessPool._launch_persistent_process` 锁管理风险

**文件**：`engine/sc_process_pool.py` — `_launch_persistent_process()` 方法

**问题描述**：
手动 `self._lock.release()` → 等待 ready 文件 → `self._lock.acquire()`（在 `finally` 中）。如果在 `release` 之后、`finally` 之前发生 `KeyboardInterrupt` 或其他未捕获异常，锁将处于**永久释放状态**，后续所有需要 `_lock` 的操作都可能出现竞态。

**问题代码**（约 L233-265）：
```python
self._lock.release()        # ← 释放锁
try:
    while time.time() < deadline:
        # ... 等待 ready 文件 ...
        time.sleep(2)
finally:
    self._lock.acquire()    # ← 重新获取（但中间可能被中断）
```

**解决方案**：
重写为**不释放锁**的模式——将等待逻辑提取为独立方法，或使用 `threading.Condition` 让等待在锁内进行但不阻塞其他操作：

```python
def _launch_persistent_process(self, slot: PersistentSlot) -> bool:
    """启动常驻 Bridge 进程。调用方须持有 _lock。"""
    # ... 启动进程 ...

    # 将等待逻辑移出锁作用域：
    # 1. 先在锁内记录 slot 状态为 "starting"
    slot.status = "starting"
    ready_timeout = ENGINE_CONFIG.get("sc_persistent_ready_timeout", 180)
    deadline = time.time() + ready_timeout

    # 2. 释放锁后等待（用独立方法包装，确保异常安全）
    return self._wait_for_ready(slot, deadline)

def _wait_for_ready(self, slot: PersistentSlot, deadline: float) -> bool:
    """等待槽位就绪（不持有锁）。"""
    ready_file = os.path.join(slot.cmd_dir, f"sc_ready_{slot.slot_id}.json")
    try:
        while time.time() < deadline:
            if slot.process is not None and slot.process.poll() is not None:
                logger.error(f"[SC-Pool] Bridge 槽位{slot.slot_id} 启动失败")
                with self._lock:
                    self._cleanup_persistent_slot(slot)
                return False
            if os.path.exists(ready_file):
                logger.info(f"[SC-Pool] 槽位{slot.slot_id} 就绪")
                with self._lock:
                    slot.status = "ready"
                return True
            time.sleep(2)

        logger.error(f"[SC-Pool] 槽位{slot.slot_id} 就绪超时")
        with self._lock:
            self._cleanup_persistent_slot(slot)
        return False
    except BaseException:
        with self._lock:
            self._cleanup_persistent_slot(slot)
        raise
```

**验证方式**：在 SC 启动期间按 Ctrl+C，确认进程池状态一致。

---

### P0-4 `SWExecutor._connect_sw` 第 3 层 fallback 无异常保护

**文件**：`executor/sw_executor.py` — `_connect_sw()` 方法

**问题描述**：
第 3 层 fallback（subprocess 启动后）调用 `win32com.client.GetActiveObject("SldWorks.Application")` **没有 try/except 保护**。如果 SW 启动但 COM 未就绪（进程还在初始化），此处抛出未捕获异常，导致 `execute_sw_step` 因 `_connect_sw` 返回 None 而失败，但错误信息不明确。

**问题代码**（约 L250）：
```python
# 第3层: 直接启动 exe
self._terminate_sw_processes()
if not self._launch_sw_process():
    return None
sw_app = win32com.client.GetActiveObject("SldWorks.Application")  # ← 无 try/except
```

**解决方案**：
```python
# 第3层: 直接启动 exe
self._terminate_sw_processes()
if not self._launch_sw_process():
    return None
try:
    sw_app = win32com.client.GetActiveObject("SldWorks.Application")
except Exception as e3:
    logger.error(f"[SW] 第3层 GetActiveObject 失败: {e3}")
    return None
```

**验证方式**：手动关闭 SW 后重新运行流水线，确认第 3 层启动正常。

---

### P0-5 Rust IPC `send_request` stream 被消费

**文件**：`autofluid-tui/src/ipc/client.rs` — `send_request()` 方法

**问题描述**：
`self.stream.take()` 会将 stream 移出 `Option`，导致发送失败或超时时连接被"消费"掉。读取超时分支虽然归还了 stream，但如果写入后读取失败（非超时），则连接永久丢失。

**解决方案**：
不使用 `take()`，改为借用 `as_mut()`：

```rust
// 旧：let stream = self.stream.take().ok_or(...)?;
// 新：let stream = self.stream.as_mut().ok_or(...)?;
```

同时移除超时分支中手动归还 stream 的代码。

---

### P0-6 Rust `generate_request_id()` 碰撞风险

**文件**：`autofluid-tui/src/main.rs` — `generate_request_id()`

**问题描述**：
基于 `SystemTime::now().as_nanos()` 生成 8 位 hex ID（32 bit entropy），快速连续调用时可能碰撞。

**解决方案**：
使用原子计数器：

```rust
use std::sync::atomic::{AtomicU64, Ordering};

static REQUEST_COUNTER: AtomicU64 = AtomicU64::new(0);

fn generate_request_id() -> String {
    let count = REQUEST_COUNTER.fetch_add(1, Ordering::Relaxed);
    let nanos = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default()
        .as_nanos();
    format!("{:08x}{:08x", (nanos & 0xFFFF_FFFF) as u32, count)
}
```

---

### P0-7 C# `Process.Start()` 返回值被忽略

**文件**：`bridge/SpaceClaimBridge/Program.cs` — `ExecutePersistent()` 和 `Execute()` 方法

**问题描述**：
`Process.Start(psi)` 的返回值（`Process` 对象）未被捕获和 Dispose。`Process` 实现了 `IDisposable`，持有非托管进程句柄，导致**进程句柄泄漏**。

**解决方案**：
```csharp
// 旧：Process.Start(psi);
// 新：
using (var started = Process.Start(psi))
{
    // 如果需要等待，可在此处理
}
```

对于 `ExecutePersistent` 中需要跟踪的进程，将其赋值给 `workingProcess` 字段并在退出时 Dispose。

---

### P0-8 C# `WaitForProcessAppear` 可能匹配旧进程

**文件**：`bridge/SpaceClaimBridge/Program.NoRef.cs` — `WaitForProcessAppear()`

**问题描述**：
`Process.Start(psi)` 返回值被丢弃后，`WaitForProcessAppear` 通过进程名重新搜索。如果之前有残留的 SpaceClaim 进程，`procs[0]` 可能返回**旧进程**而非刚启动的。

**解决方案**：
1. 记录启动前已有的 PID 集合
2. 搜索时排除旧 PID

```csharp
// 启动前记录已有进程
var existingPids = new HashSet<int>(
    Process.GetProcessesByName("SpaceClaim").Select(p => p.Id)
);

Process.Start(psi);

// 搜索时排除旧进程
Process[] procs = Process.GetProcessesByName("SpaceClaim");
Process target = procs.FirstOrDefault(p => !existingPids.Contains(p.Id));
```

---

### P0-9 C# 空 `catch { }` 块吞掉所有异常

**文件**：`bridge/SpaceClaimBridge/Program.cs` — `WaitForProcessAppear()` 和 `WaitForGuiReady()`

**问题描述**：
多处 `catch { }` 块完全吞掉异常信息，调试时无法知道失败原因。

**解决方案**：
至少记录日志到 stderr：
```csharp
// 旧：catch { }
// 新：catch (Exception ex) { Console.Error.WriteLine($"[Bridge] Warning: {ex.Message}"); }
```

---

## 4. P1 — 中优先级问题

> 影响稳定性或可维护性，建议尽快修复。共 24 项。

### P1-1 `ssh_client.py` 生产代码中使用 `assert`

**文件**：`utils/ssh_client.py` — L150

**问题**：`assert self._sftp is not None` 在 `-O` 优化模式下被跳过，后续 `self._sftp.put()` 抛出更难理解的 `AttributeError`。

**解决**：
```python
# 旧：assert self._sftp is not None, "SFTP 连接已断开"
# 新：
if self._sftp is None:
    raise ConnectionError("SFTP 连接已断开，请先调用 connect()")
```

---

### P1-2 `remote_executor.py` `execute_meshing` 与 `start_meshing` 重复代码

**文件**：`executor/remote_executor.py`

**问题**：两个方法代码近 90% 相同（命令构建、flag_file 生成、SSH 调用），唯一区别是错误处理方式。

**解决**：抽取共享的 `_build_meshing_command()` 和 `_exec_meshing_background()` 私有方法：

```python
def _build_meshing_command(self, config_name: int) -> tuple[str, str]:
    """构建远程网格划分命令和标志文件路径。"""
    flag_file = f"{REMOTE_CONFIG['flag_dir']}/meshing_done_{config_name}.txt".replace("\\", "/")
    command = (f'"{REMOTE_CONFIG["conda_exe"]}" run -n {REMOTE_CONFIG["conda_env"]} '
               f'python "{REMOTE_CONFIG["meshing_script"]}" {config_name}')
    return command, flag_file

def execute_meshing(self, config_name: int) -> bool:
    """在远程工作站启动网格划分后台任务（含状态管理）。"""
    if not isinstance(config_name, int):
        logger.error(f"无效的构型名称类型: {type(config_name).__name__}")
        return False
    command, flag_file = self._build_meshing_command(config_name)
    # ... SSH 调用 + 错误处理 ...

def start_meshing(self, config_name: int) -> bool:
    """启动远程网格划分（不含状态管理，由调用方处理）。"""
    if not isinstance(config_name, int):
        return False
    command, flag_file = self._build_meshing_command(config_name)
    # ... SSH 调用（无状态设置）...
```

---

### P1-3 `state_manager.py` 日志中使用 f-string

**文件**：`engine/state_manager.py` — `_get_connection()` 方法

**问题**：`logger.error(f"数据库回滚异常: {e}")` 使用 f-string，违反日志最佳实践（lazy formatting）。当日志级别高于 ERROR 时，字符串仍会被构造。

**解决**：
```python
# 旧：logger.error(f"数据库回滚异常: {e}")
# 新：logger.error("数据库回滚异常: %s", e)
```

---

### P1-4 `task_runner.py` 局部变量使用下划线前缀

**文件**：`engine/task_runner.py` — `execute_sc_step()` 方法 L93-94

**问题**：`_sw_step_name` 和 `_scdoc_name` 使用下划线前缀（暗示私有），但它们是纯局部变量，违反编码规范中"避免下划线前缀滥用"原则。

**解决**：
```python
# 旧：_sw_step_name = get_step_filename("SW", config_name)
# 新：sw_step_name = get_step_filename("SW", config_name)
# 旧：_scdoc_name = get_step_filename("SC", config_name)
# 新：scdoc_name = get_step_filename("SC", config_name)
```

---

### P1-5 `task_runner.py` 未向 `_remote_executor` 注入控制事件

**文件**：`engine/task_runner.py` — `set_control_events()` 方法 L74

**问题**：暂停/停止事件只注入了 `_sw_executor`，未注入 `_remote_executor`。虽然 `_remote_executor` 通过 `wait_*_completion` 方法接收事件参数，但不一致的设计可能导致遗漏。

**解决**：
```python
def set_control_events(self, paused_event, stopped_event):
    self._paused_event = paused_event
    self._stopped_event = stopped_event
    self._sw_executor.set_control_events(paused_event, stopped_event)
    # 如 RemoteExecutor 支持：
    # self._remote_executor.set_control_events(paused_event, stopped_event)
```

---

### P1-6 `scheduler/main.py` `_resume_paused_steps` 未区分 SW 重试逻辑

**文件**：`engine/scheduler/main.py` — `_resume_paused_steps()` 方法

**问题**：`ERROR/RETRYING` 状态的步骤只检查重试次数后重置为 WAITING，但不检查步骤类型。SW 步骤的重试逻辑与其他步骤不同（需要重启宏），这里统一处理可能不正确。

**解决**：
```python
if status in (STATUS_ERROR, STATUS_RETRYING):
    retry_count = self.state.get_step_retry_count(cn, step)
    if retry_count < max_retries:
        if step == "SW":
            self.state.set_step_status(cn, step, STATUS_WAITING)
            # SW 重试由 start_pipeline 的 SW 阶段统一处理
        else:
            self.state.set_step_status(cn, step, STATUS_WAITING)
            normal_enqueue.append((cn, step))
    break
```

---

### P1-7 `scheduler/sw_phase.py` 函数参数被变量覆盖

**文件**：`engine/scheduler/sw_phase.py` — `_execute_sw_macro()` 方法

**问题**：`all_configs = self.state.get_all_configs()` 覆盖了函数参数 `all_configs`。虽然当前调用时参数值与数据库查询结果一致，但这种覆盖模式容易引入 bug。

**解决**：
```python
# 旧：all_configs = self.state.get_all_configs()
# 新：db_configs = self.state.get_all_configs()
```

---

### P1-8 `file_monitor.py` 暂停标志与调度器不同步

**文件**：`engine/file_monitor.py`

**问题**：`StepFileMonitor._paused` 是实例级 `Event`，与调度器的 `_paused` Event 独立。`pause()`/`resume()` 方法独立控制，可能导致调度器暂停但文件监控器仍在推送事件。

**解决**：在 `StepFileMonitor.__init__` 中接收调度器的 `_paused` Event 引用，或在 `PipelineScheduler` 创建 `StepFileMonitor` 时传入共享的 `_paused` Event。

---

### P1-9 `ipc/server.py` 中 lambda 处理器调试不友好

**文件**：`ipc/server.py` — `register_default_handlers()`

**问题**：所有处理器使用 `lambda p: daemon.handle_start(p)` 包装，调试时堆栈只显示 `<lambda>`。

**解决**：使用 `functools.partial` 或直接传方法引用：
```python
# 旧：self.register_handler(CMD_START, lambda p: daemon.handle_start(p))
# 新：self.register_handler(CMD_START, daemon.handle_start)
```

注意：需确认 `handle_start` 等方法的签名兼容（当前 `params` 有默认值 `None`，IPC 调用时总是传入 `params` dict）。

---

### P1-10 Rust `handle_mouse_down` 参数过多（15 个）

**文件**：`autofluid-tui/src/event_handler/mouse.rs`

**问题**：函数签名有 15 个参数，职责过重，参数传递链过长容易出错。

**解决**：将相关参数封装为结构体：
```rust
struct MouseEventContext<'a> {
    app: &'a mut AppState,
    ipc: &'a mut IpcClient,
    // ... 其他字段
}
```

---

### P1-11 Rust `AppState` 字段过多（~45 个）

**文件**：`autofluid-tui/src/state/app_state.rs`

**问题**：所有 UI 状态扁平地放在一个结构体中，是典型的"上帝对象"。

**解决**：拆分为子结构体：
```rust
pub struct AppState {
    pub ui: UiState,           // focus, scroll, hover, click
    pub connection: ConnectionState,  // connected, engine_info
    pub command: CommandState,  // input, pending, confirm
    pub settings: SettingsState,
    pub log: LogState,
}
```

**注意**：这是较大的重构，建议在功能稳定后单独 PR 处理。

---

### P1-12 Rust 三个滚动处理函数重复代码

**文件**：`autofluid-tui/src/event_handler/key_handler.rs`

**问题**：`handle_table_scroll`、`handle_info_log_scroll`、`handle_detail_log_scroll` 有大量重复代码（Up/Down/PageUp/PageDown/Home/End 处理几乎相同）。

**解决**：提取为泛用的滚动处理器：
```rust
fn handle_scroll_action(
    action: ScrollAction,
    offset: &mut u16,
    total: u16,
    page_size: u16,
) {
    match action {
        ScrollAction::Up => *offset = offset.saturating_sub(1),
        ScrollAction::Down => *offset = offset.saturating_add(1),
        ScrollAction::PageUp => *offset = offset.saturating_sub(page_size),
        ScrollAction::PageDown => *offset = offset.saturating_add(page_size),
        ScrollAction::Home => *offset = 0,
        ScrollAction::End => *offset = total,
    }
}
```

---

### P1-13 Rust `protocol.rs` 序列化错误静默吞掉

**文件**：`autofluid-tui/src/ipc/protocol.rs` — L65

**问题**：`serde_json::to_string(self).unwrap_or_default()` 序列化失败时返回空串，发送端不会知道出错。

**解决**：
```rust
// 旧：serde_json::to_string(self).unwrap_or_default()
// 新：
fn serialize(&self) -> String {
    match serde_json::to_string(self) {
        Ok(s) => s + "\n",
        Err(e) => {
            eprintln!("[IPC] 序列化失败: {e}");
            // 返回错误响应
            serde_json::to_string(&IpcResponse {
                ok: false,
                request_id: self.request_id.clone(),
                data: Value::Null,
                message: Some(format!("内部序列化错误: {e}")),
            }).unwrap_or_else(|_| r#"{"ok":false,"request_id":"","data":null,"message":"序列化失败"}"#.to_string()) + "\n"
        }
    }
}
```

---

### P1-14 Rust 重连循环阻塞 UI 10 秒

**文件**：`autofluid-tui/src/daemon_mgr.rs` — `reconnect_ipc_after_launch()`

**问题**：使用 `std::thread::sleep(Duration::from_millis(500))` 阻塞当前线程进行重连循环，10 秒内 TUI 无法响应用户输入。

**解决**：改为在主事件循环中设置 `reconnecting_until: Option<Instant>` 状态标志，每次循环尝试一次连接。

**注意**：这需要修改 `main.rs` 的事件循环，属于中等重构。

---

### P1-15 Rust `serde_json::from_str` 静默返回 None

**文件**：`autofluid-tui/src/ipc/protocol.rs` — L73

**问题**：`serde_json::from_str(trimmed).ok()` 反序列化失败时静默返回 `None`，无法区分 JSON 语法错误还是 schema 不匹配。

**解决**：
```rust
// 旧：serde_json::from_str(trimmed).ok()
// 新：
match serde_json::from_str(trimmed) {
    Ok(msg) => Some(msg),
    Err(e) => {
        eprintln!("[IPC] 反序列化失败: {e}, 原始数据: {trimmed}");
        None
    }
}
```

---

### P1-16 C# `Process.Start()` 返回的 Process 对象未 Dispose

**文件**：`bridge/SpaceClaimBridge/Program.cs`

**问题**：`Process` 实现了 `IDisposable`，持有非托管的进程句柄。在 `Execute` 和 `ExecutePersistent` 的所有退出路径上都应确保 Dispose。

**解决**：在类中添加 `IDisposable` 实现，在 `Dispose` 中清理 `workingProcess`：
```csharp
public void Dispose()
{
    workingProcess?.Dispose();
}
```

---

### P1-17 C# 两个文件 90% 代码重复

**文件**：`bridge/SpaceClaimBridge/Program.cs` vs `Program.NoRef.cs`

**问题**：`Program.NoRef.cs` 是 C# 5 兼容版本（使用 `string.Format` 替代插值），维护两份几乎相同的代码容易导致不一致。

**解决**：
1. 如果构建环境已升级到 .NET 6+，删除 `Program.NoRef.cs`
2. 如果需要保留，使用条件编译 `#if` 或共享基类

---

### P1-18 C# `EnsureQuoted` 死代码

**文件**：`bridge/SpaceClaimBridge/Program.cs`

**问题**：`EnsureQuoted` 方法被定义但从未使用。

**解决**：删除该方法。

---

### P1-19 C# `Options` 类使用字段而非属性

**文件**：`bridge/SpaceClaimBridge/Program.NoRef.cs` — L18-25

**问题**：内部类 `Options` 使用 public 字段（如 `Script`, `Config`, `StepDir`），不符合 C# 惯例。公共成员应使用自动属性。`Program.cs` 中的 `BridgeOptions` 正确使用了属性。

**解决**：
```csharp
// 旧：public string Script;
// 新：public string Script { get; set; }
```

---

### P1-20 C# `Options` 字段命名使用 PascalCase（非属性）

**文件**：`bridge/SpaceClaimBridge/Program.NoRef.cs` — L18-25

**问题**：`Options` 的字段使用 PascalCase，但如果是字段（非属性），C# 惯例应用 `_camelCase` 或 `camelCase`。由于这些是 public 字段，最佳实践是改为 PascalCase 属性（与 P1-19 合并修复）。

**解决**：与 P1-19 合并——改为 PascalCase 自动属性后，命名自然符合规范。

---

### P1-21 C# `WaitForProcessAppear` 中 `procs[0]` fallback 风险

**文件**：`bridge/SpaceClaimBridge/Program.cs` — `WaitForProcessAppear()`

**问题**：`foreach` 中对 `Process.StartTime` 的访问异常被完全吞掉后，fallback 返回 `procs[0]` 可能返回一个已存在的旧进程。

**解决**：记录启动前已有 PID 集合（与 P0-8 合并处理）。

---

### P1-22 C# `WaitForGuiReady` 中多个 `catch { }` 块

**文件**：`bridge/SpaceClaimBridge/Program.cs` — `WaitForGuiReady()`

**问题**：Phase 1/2/3 检测各有独立的空 catch 块，异常信息完全丢失。

**解决**：每个 catch 块至少记录日志（与 P0-9 统一修复）。

---

### P1-23 C# `workingProcess` 变量未 Dispose

**文件**：`bridge/SpaceClaimBridge/Program.cs`

**问题**：`workingProcess` 在多处被 `Refresh()` 调用，但从未被 `Dispose()`。在 `Execute` 和 `ExecutePersistent` 的所有退出路径上都应确保 Dispose。

**解决**：添加 `try/finally` 确保 Dispose，或实现 `IDisposable`（与 P1-16 合并）。

---

### P1-24 C# Phase 3 固定等待 15 秒

**文件**：`bridge/SpaceClaimBridge/Program.cs` — `WaitForGuiReady()` Phase 3

**问题**：Phase 3 固定等待 15 秒——保守估计但每次启动额外等待 15 秒。

**解决**：提供可配置的等待时间（通过命令行参数或环境变量），或改进就绪检测机制。

---

## 5. P2 — 低优先级问题

> 代码质量改进，不影响功能但可提升可维护性。共 15 项。

### Python P2（8 项）

| # | 文件 | 问题 | 解决方案 |
|---|------|------|----------|
| P2-1 | `state_manager.py` | `configs` 表硬编码 `param1-param4` 四列，扩展性差 | 未来可改用 JSON 列或独立参数表 |
| P2-2 | `state_manager.py` | `set_step_status` 每次写入都新建 SQLite 连接，高频调用时可能造成连接风暴 | 批量操作时使用 `executemany` 或事务合并 |
| P2-3 | `sc_process_pool.py` | `scdoc_stable_seconds = 3.0` 硬编码在方法内部 | 提取为 `ENGINE_CONFIG` 配置项 |
| P2-4 | `sc_process_pool.py` | `slot_id = len(self._persistent_slots) + 1` 可能冲突（reset 后重建） | 使用自增计数器 |
| P2-5 | `file_monitor.py` | `history[:] = [...]` 原地修改可读性差 | 改为直接赋值 |
| P2-6 | `excel_reader.py` | `range(1, 5)` 硬编码参数列数 | 提取为配置常量 |
| P2-7 | `process_utils.py` | `check_ipc_ready` 中 socket 未使用 `with` 语句 | 改为 `with socket.socket(...) as s:` |
| P2-8 | `config.py` | `reload_config_from_toml()` 模块加载时自动执行，可能在测试中产生副作用 | 改为显式调用 |

### Rust P2（5 项）

| # | 文件 | 问题 | 解决方案 |
|---|------|------|----------|
| P2-9 | `ui/logs.rs` | `compute_info_lines_no_wrap` 每次 clone 所有消息字符串，日志量大时性能差 | 考虑使用 `Cow<str>` 或引用 |
| P2-10 | `settings/mod.rs` | `field_count()` 与 `display_label()` 的 match 分支无编译器同步保证 | 使用宏或数组索引映射 |
| P2-11 | `main.rs` | `run_app` 约 150 行，混合事件循环、命令派发、IPC 轮询 | 拆分为独立函数 |
| P2-12 | `ui/command_bar.rs` | `render_command_bar` 约 100 行，同时负责输入框和按钮栏 | 拆分为 `render_input`、`render_buttons` |
| P2-13 | `Cargo.toml` | `clipboard-win` 和 `windows-sys` 依赖声明但可能未完全使用 | 移除未使用的依赖 |

### C# P2（2 项）

| # | 文件 | 问题 | 解决方案 |
|---|------|------|----------|
| P2-14 | `Program.NoRef.cs` | `Options` 类使用字段而非属性（与 P1-19 重叠，此处标记为低优先级备选） | 改为自动属性 `{ get; set; }` |
| P2-15 | `Program.cs` | `SpaceClaimExePaths` 数组硬编码三个版本路径，用户安装其他版本不会被发现 | 考虑注册表查询或环境变量覆盖 |

---

## 6. 实施计划

### Phase 1 — Python P0 修复（核心功能保障）

| 顺序 | Issue | 文件 | 预估改动 |
|------|-------|------|----------|
| 1 | P0-1 | `engine/scheduler/main.py` | ~30 行修改 |
| 2 | P0-2 | `engine/scheduler/worker_pool.py`, `barrier.py` | ~160 行删除 + 注入 RetryManager |
| 3 | P0-3 | `engine/sc_process_pool.py` | ~40 行重构 |
| 4 | P0-4 | `executor/sw_executor.py` | ~5 行修改 |

### Phase 2 — Python P1 修复（稳定性/规范）

| 顺序 | Issue | 文件 | 预估改动 |
|------|-------|------|----------|
| 5 | P1-1 | `utils/ssh_client.py` | ~2 行 |
| 6 | P1-2 | `executor/remote_executor.py` | ~40 行重构 |
| 7 | P1-3 | `engine/state_manager.py` | ~3 处 f-string → %s |
| 8 | P1-4 | `engine/task_runner.py` | ~4 处变量重命名 |
| 9 | P1-5 | `engine/task_runner.py` | ~2 行新增 |
| 10 | P1-6 | `engine/scheduler/main.py` | ~10 行修改 |
| 11 | P1-7 | `engine/scheduler/sw_phase.py` | ~2 行重命名 |
| 12 | P1-9 | `ipc/server.py` | ~11 处 lambda → 方法引用 |

### Phase 3 — Rust 修复

| 顺序 | Issue | 文件 | 预估改动 |
|------|-------|------|----------|
| 13 | P0-5 | `ipc/client.rs` | ~10 行 |
| 14 | P0-6 | `main.rs` | ~15 行 |
| 15 | P1-13 | `ipc/protocol.rs` | ~10 行 |
| 16 | P1-15 | `ipc/protocol.rs` | ~8 行 |
| 17 | P2-13 | `Cargo.toml` | 1 行删除 |

### Phase 4 — C# 修复

| 顺序 | Issue | 文件 | 预估改动 |
|------|-------|------|----------|
| 18 | P0-7 | `Program.cs` | ~10 行 |
| 19 | P0-8 | `Program.NoRef.cs` | ~15 行 |
| 20 | P0-9 | `Program.cs` | ~6 处 catch 块 |
| 21 | P1-18 | `Program.cs` | 删除死代码 |

### Phase 5 — 测试验证

1. Python：`python -m pytest tests/ -v`
2. Rust：`cd autofluid-tui && cargo check && cargo clippy`
3. C#：`compile.bat`（如环境可用）

### Phase 6 — 二次审查

修改完成后进行二次审查，确认无新增问题。

---

## 附录：依赖关系图

```
config.py ← state_manager.py ← task_runner.py ← daemon.py
                                              ← scheduler/main.py
     ↑                                              ↑
executor/*.py ←──────────────────────────────────────┘
     ↑
utils/*.py
```

**无循环依赖**。依赖方向清晰。
