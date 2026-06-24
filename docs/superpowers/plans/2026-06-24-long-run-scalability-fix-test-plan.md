# Long-Run Scalability Fix Test Plan Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复并验证审查发现的百构型批处理和超长时间运行稳定性问题，避免 SC 槽位污染、TUI 卡死、状态数据库瓶颈、后台子进程静默死亡、日志/告警无界增长。

**Architecture:** 计划按风险边界拆成 5 个实施包：SC persistent 生命周期、TUI 非阻塞控制面、Python 调度/状态批量化、daemon/log/alert 长运行治理、端到端压力验证。每个包先写失败测试，再实现最小修复，最后运行对应语言质量门禁。

**Tech Stack:** Python 3.13 + pytest/ruff/mypy, Rust tokio/ratatui + cargo test/clippy/fmt, C# .NET Framework 4.8 source-level checks and compile scripts, SQLite WAL, JSON-over-TCP IPC.

---

## Scope And Fix Order

优先级：

1. `SCProcessPool` persistent timeout 后重启槽位，避免一个卡死构型污染后续 100+ 构型。
2. Rust TUI dashboard polling 和命令分发改为后台任务，避免主线程 `block_on` 卡死。
3. Python `StateManager`/`BarrierCoordinator`/`MeshingMonitor` 降低百构型轮询和 requeue 成本。
4. daemon child process、alert watcher、日志文件做长运行治理。
5. 添加集成压力测试脚本/用例，证明 100+ 构型路径不会回归。

不在本计划内：

- 不调整真实 Fluent/SpaceClaim journal 业务逻辑。
- 不改远程工作站网络拓扑。
- 不做大规模架构替换，例如用 PostgreSQL 替换 SQLite。

---

## Files To Modify

- Modify: `engine/sc_process_pool.py`
  - persistent SC 命令超时后杀掉并移除槽位；可选记录 `slot.restart_count` 或日志原因。
- Modify/Test: `tests/test_sc_ipc_runid.py`, `tests/test_sc_process_pool.py`
  - 覆盖超时后不复用旧槽位、taskkill 被调用、后续 run 会重新 launch。
- Modify: `bridge/SpaceClaimBridge/Program.cs`
  - 可选增强 quit wait 自保护超时；如果只由 Python 兜底，本项作为次级修复。
- Test: `tests/test_spaceclaim_bridge_source.py`
  - 源码级断言 Bridge persistent quit 有超时或 Python timeout path 会强制清理。
- Modify: `autofluid-tui/src/lib.rs`
  - dashboard polling 后台化；用户命令后台化；主循环只消费 channel。
- Modify: `autofluid-tui/src/ipc/client.rs`
  - 如有必要暴露 clone-safe request 参数或轻量 client 创建接口。
- Modify/Test: `autofluid-tui/src/state/log_buffer.rs`, `autofluid-tui/src/ui/logs.rs`, `autofluid-tui/src/lib.rs`
  - 日志行缓存或只计算一次后传入 render。
- Test: Rust unit tests in `autofluid-tui/src/lib.rs` or relevant modules.
- Modify: `engine/state_manager.py`
  - 添加批量状态统计/查询方法，避免 barrier N 次 `get_step_status`。
- Modify: `engine/scheduler/barrier.py`
  - 使用批量统计方法替换逐构型轮询。
- Modify: `engine/scheduler/meshing_monitor.py`
  - 工作站全忙时退避或等待 slot release signal。
- Test: `tests/test_state_manager.py`, `tests/test_scheduler_modules.py`
  - 覆盖批量统计正确性和 requeue backoff。
- Modify: `engine/daemon.py`
  - 监控并重启 alert watcher / LocalWorker 子进程，带 backoff 和最大频率。
- Modify: `tools/autofluid_cli.py`
  - 定期 prune `seen_until`。
- Modify: `utils/logger.py`
  - 使用 rotating file handler；保留 deferred buffer 行为。
- Test: `tests/test_autofluid_cli.py`, `tests/test_daemon_server_mode.py`, `tests/test_daemon_local_worker_handlers.py`, `tests/test_detail_log.py` or new focused tests.

---

### Task 1: SC Persistent Timeout Must Retire Slot

**Files:**
- Modify: `engine/sc_process_pool.py`
- Test: `tests/test_sc_ipc_runid.py`
- Test: `tests/test_sc_process_pool.py`

- [ ] **Step 1: Add failing test for timeout retiring persistent slot**

Add a focused test that constructs a `PersistentSlot`, forces `_send_persistent_command()` to hit `sc_timeout`, and asserts cleanup is invoked instead of returning the same slot to reusable `ready` state.

```python
def test_persistent_command_timeout_retires_slot(monkeypatch, tmp_path):
    from engine.config import ENGINE_CONFIG
    from engine.sc_process_pool import PersistentSlot, SCProcessPool

    monkeypatch.setitem(ENGINE_CONFIG, "sc_timeout", 0)
    pool = SCProcessPool()
    pool._persistent_cmd_dir = str(tmp_path)

    slot = PersistentSlot(slot_id=1, cmd_dir=str(tmp_path))
    slot.status = "busy"
    slot.current_config = 1
    slot.pid = 12345
    slot.spaceclaim_pid = 23456
    pool._persistent_slots[1] = slot

    cleaned: list[int] = []

    def fake_cleanup(cleaned_slot):
        cleaned.append(cleaned_slot.slot_id)
        pool._persistent_slots.pop(cleaned_slot.slot_id, None)
        cleaned_slot.status = "idle"

    monkeypatch.setattr(pool, "_cleanup_persistent_slot", fake_cleanup)

    assert pool._send_persistent_command(slot, 1, paused_event=None, stopped_event=None) is False
    assert cleaned == [1]
    assert 1 not in pool._persistent_slots
```

- [ ] **Step 2: Run the failing test**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests/test_sc_ipc_runid.py::test_persistent_command_timeout_retires_slot -q
```

Expected before fix: failure because timeout returns `_fail(reason)` without retiring/removing the slot.

- [ ] **Step 3: Implement minimal timeout cleanup**

In `engine/sc_process_pool.py`, add a helper that can be called outside the lock:

```python
def _retire_persistent_slot_after_failure(self, slot: PersistentSlot, reason: str) -> None:
    logger.warning(
        "[SC-Pool] 常驻槽位%s 因 %s 被废弃并重启",
        slot.slot_id,
        reason,
        extra=self._sc_log_extra(slot.current_config, slot),
    )
    with self._lock:
        if self._persistent_slots.get(slot.slot_id) is slot:
            self._cleanup_persistent_slot(slot)
            self._persistent_slots.pop(slot.slot_id, None)
```

Then in `_send_persistent_command()` timeout branch, call the helper before returning failure:

```python
reason = f"构型{config_name} 超时 ({timeout}s), run={run_id}"
logger.error(f"[SC-Pool] {reason}", extra=self._sc_log_extra(config_name, slot))
self._cleanup_run_files(slot.slot_id, run_id)
self._retire_persistent_slot_after_failure(slot, reason)
return self._fail(reason)
```

Guard `run_config()` `finally` so it does not mark a removed slot as ready:

```python
finally:
    with self._lock:
        if self._persistent_slots.get(slot.slot_id) is slot:
            slot.status = "ready"
            slot.current_config = None
```

- [ ] **Step 4: Add stale late-output regression**

Add a test that simulates an old command timing out, then verifies the next command must create a new slot/process before accepting success.

```python
def test_persistent_timeout_does_not_reuse_late_output_slot(monkeypatch, tmp_path):
    from engine.config import ENGINE_CONFIG
    from engine.sc_process_pool import PersistentSlot, SCProcessPool

    monkeypatch.setitem(ENGINE_CONFIG, "sc_timeout", 0)
    pool = SCProcessPool()
    pool._persistent_cmd_dir = str(tmp_path)
    slot = PersistentSlot(slot_id=1, cmd_dir=str(tmp_path))
    pool._persistent_slots[1] = slot

    retired: list[int] = []
    monkeypatch.setattr(
        pool,
        "_cleanup_persistent_slot",
        lambda s: retired.append(s.slot_id),
    )

    assert pool._send_persistent_command(slot, 1, None, None) is False
    assert retired == [1]
```

- [ ] **Step 5: Run SC-focused Python tests**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests/test_sc_ipc_runid.py tests/test_sc_process_pool.py tests/test_spaceclaim_bridge_source.py -q
```

Expected: all pass.

- [ ] **Step 6: Run Python quality gate for touched files**

Run:

```powershell
.venv\Scripts\python.exe -m ruff check engine/sc_process_pool.py tests/test_sc_ipc_runid.py tests/test_sc_process_pool.py tests/test_spaceclaim_bridge_source.py
.venv\Scripts\python.exe -m mypy engine/sc_process_pool.py
```

Expected: no new errors.

---

### Task 2: Optional Bridge Quit Deadline

**Files:**
- Modify: `bridge/SpaceClaimBridge/Program.cs`
- Test: `tests/test_spaceclaim_bridge_source.py`

- [ ] **Step 1: Add source-level failing test**

Add a test that asserts persistent mode has an explicit quit deadline once `quitRequested` is true.

```python
def test_persistent_bridge_quit_has_deadline():
    source = Path("bridge/SpaceClaimBridge/Program.cs").read_text(encoding="utf-8")
    assert "quitRequestedAt" in source
    assert "PersistentQuitTimeoutSeconds" in source
    assert "TryKillWorkingProcess(workingProcess" in source
```

- [ ] **Step 2: Run failing test**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests/test_spaceclaim_bridge_source.py::test_persistent_bridge_quit_has_deadline -q
```

Expected before fix: fail because no Bridge-side quit deadline exists.

- [ ] **Step 3: Implement Bridge quit deadline**

In `Program.cs`, define:

```csharp
private const int PersistentQuitTimeoutSeconds = 30;
```

Track quit request time:

```csharp
DateTime? quitRequestedAt = null;
```

When quit is first observed:

```csharp
quitRequested = true;
quitRequestedAt = DateTime.UtcNow;
```

In the persistent loop, after quit is requested:

```csharp
if (quitRequestedAt.HasValue
    && (DateTime.UtcNow - quitRequestedAt.Value).TotalSeconds >= PersistentQuitTimeoutSeconds)
{
    Console.Error.WriteLine("[BRIDGE_ERROR] quit 请求超时，强制终止 SpaceClaim");
    TryKillWorkingProcess(workingProcess, "persistent quit timeout");
    exitCode = (int)ExitCode.Timeout;
    break;
}
```

- [ ] **Step 4: Compile Bridge**

Run:

```powershell
cd bridge\SpaceClaimBridge
.\compile_noref.bat
.\compile.bat
cd ..\..
```

Expected: both compile scripts succeed with zero errors.

---

### Task 3: TUI Dashboard Polling Must Not Block Main Loop

**Files:**
- Modify: `autofluid-tui/src/lib.rs`
- Modify if needed: `autofluid-tui/src/ipc/client.rs`
- Test: `autofluid-tui/src/lib.rs` or a new focused module test

- [ ] **Step 1: Add non-blocking dashboard task unit test**

Introduce a small state-machine helper so it can be tested without a real terminal:

```rust
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum DashboardPollState {
    Idle,
    InFlight,
}

impl DashboardPollState {
    fn can_spawn(self) -> bool {
        matches!(self, DashboardPollState::Idle)
    }
}

#[test]
fn dashboard_poll_state_prevents_overlapping_polls() {
    let state = DashboardPollState::Idle;
    assert!(state.can_spawn());
    let state = DashboardPollState::InFlight;
    assert!(!state.can_spawn());
}
```

- [ ] **Step 2: Run failing/compile test**

Run:

```powershell
cd autofluid-tui
cargo test dashboard_poll_state_prevents_overlapping_polls
cd ..
```

Expected before helper exists: compile failure.

- [ ] **Step 3: Implement background dashboard task**

Use the existing `mpsc::channel` pattern from lifecycle/check tasks:

```rust
struct DashboardPollTask {
    rx: std::sync::mpsc::Receiver<Result<ipc::protocol::IpcResponse, String>>,
}
```

Spawn:

```rust
fn spawn_dashboard_poll_task(
    host: String,
    port: u16,
    since_log_id: u64,
    log_limit: usize,
) -> DashboardPollTask {
    let (tx, rx) = std::sync::mpsc::channel();
    std::thread::spawn(move || {
        let rt = tokio::runtime::Runtime::new().expect("dashboard poll runtime");
        let mut client = IpcClient::new(&host, port);
        let result = rt.block_on(async {
            client.connect().await?;
            client.get_dashboard(since_log_id, log_limit).await
        });
        let _ = tx.send(result);
    });
    DashboardPollTask { rx }
}
```

Main loop behavior:

- If no task is in flight and poll interval elapsed, spawn task.
- Each tick, `try_recv()` result and call `apply_dashboard_response()`.
- Do not call `rt.block_on(ipc.get_dashboard(...))` from the main loop.

- [ ] **Step 4: Add source regression**

Add Rust test or Python source test:

```python
def test_tui_dashboard_refresh_not_block_on_get_dashboard():
    source = Path("autofluid-tui/src/lib.rs").read_text(encoding="utf-8")
    assert "block_on(ipc.get_dashboard" not in source
    assert "spawn_dashboard_poll_task" in source
```

- [ ] **Step 5: Run Rust TUI gate**

Run:

```powershell
cd autofluid-tui
cargo fmt --check
cargo check
cargo clippy -- -D warnings
cargo test
cd ..
```

Expected: all pass.

---

### Task 4: TUI Commands Must Run Off Main Thread

**Files:**
- Modify: `autofluid-tui/src/lib.rs`
- Modify if needed: `autofluid-tui/src/event_handler/command.rs`
- Test: `autofluid-tui/src/lib.rs`

- [ ] **Step 1: Add command task state test**

```rust
#[derive(Debug, Clone, PartialEq, Eq)]
struct PendingCommand {
    command: String,
    source: String,
}

#[test]
fn pending_command_records_source_and_command() {
    let pending = PendingCommand {
        command: "reset all all".to_string(),
        source: "keyboard".to_string(),
    };
    assert_eq!(pending.command, "reset all all");
    assert_eq!(pending.source, "keyboard");
}
```

- [ ] **Step 2: Implement command worker**

Create command task equivalent to dashboard:

```rust
struct CommandTask {
    command: String,
    rx: std::sync::mpsc::Receiver<command::CommandResult>,
}
```

Replace direct:

```rust
rt.block_on(command::dispatch_command(cmd, ipc, state, log_buffer))
```

with:

```rust
spawn_command_task(cmd.to_string(), source.to_string(), ipc_host, ipc_port)
```

Main loop consumes command result and calls `handle_command_result()`.

- [ ] **Step 3: Preserve immediate local commands**

Keep `quit` and local text-buffer editing immediate if they do not touch IPC. Commands that call IPC (`start`, `pause`, `stop`, `reset`, `clean`, `reload_config`, `worker_start`, `worker_stop`, `worker_restart`) go through the background command task.

- [ ] **Step 4: Add source regression**

```python
def test_tui_submit_command_does_not_block_on_dispatch():
    source = Path("autofluid-tui/src/lib.rs").read_text(encoding="utf-8")
    assert "block_on(command::dispatch_command" not in source
    assert "CommandTask" in source
```

- [ ] **Step 5: Run Rust command tests**

Run:

```powershell
cd autofluid-tui
cargo test command
cargo test
cd ..
```

Expected: all pass.

---

### Task 5: TUI Log Rendering Should Compute Lines Once Per Frame Or Cache

**Files:**
- Modify: `autofluid-tui/src/ui/logs.rs`
- Modify: `autofluid-tui/src/lib.rs`
- Optional Modify: `autofluid-tui/src/state/log_buffer.rs`

- [ ] **Step 1: Add render API test or source regression**

```python
def test_log_render_api_accepts_precomputed_lines():
    source = Path("autofluid-tui/src/ui/logs.rs").read_text(encoding="utf-8")
    assert "render_info_panel_with_lines" in source
    assert "render_detail_panel_with_lines" in source
```

- [ ] **Step 2: Split render functions**

Keep existing public function if useful, but add internal functions:

```rust
pub fn render_info_panel_with_lines(
    frame: &mut Frame,
    area: Rect,
    theme: &Theme,
    lines: Vec<Line>,
    max_content_width: usize,
    scroll_offset: u16,
    hscroll_offset: u16,
    focused: bool,
) {
    // existing render body without calling compute_info_lines_no_wrap()
}
```

Do the same for detail panel.

- [ ] **Step 3: Update `do_redraw()` to pass precomputed lines**

The frame should call `compute_*_lines_no_wrap()` once, use counts for scrollbar, then pass the same lines into render.

- [ ] **Step 4: Run Rust rendering tests**

Run:

```powershell
cd autofluid-tui
cargo test logs
cargo check
cd ..
```

Expected: all pass, no clippy warnings.

---

### Task 6: StateManager Batch Queries For Barrier And Dashboard

**Files:**
- Modify: `engine/state_manager.py`
- Modify: `engine/scheduler/barrier.py`
- Test: `tests/test_state_manager.py`
- Test: `tests/test_scheduler_modules.py`

- [ ] **Step 1: Add batch status stats tests**

```python
def test_get_step_status_counts_returns_single_step_distribution(tmp_path):
    from engine.state_manager import StateManager
    from engine.config import STATUS_COMPLETED, STATUS_ERROR, STATUS_WAITING

    state = StateManager(db_path=str(tmp_path / "state.db"))
    state.load_configs([1, 2, 3])
    state.set_step_status(1, "meshing", STATUS_COMPLETED)
    state.set_step_status(2, "meshing", STATUS_ERROR)
    state.set_step_status(3, "meshing", STATUS_WAITING)

    counts = state.get_step_status_counts("meshing")

    assert counts[STATUS_COMPLETED] == 1
    assert counts[STATUS_ERROR] == 1
    assert counts[STATUS_WAITING] == 1
```

- [ ] **Step 2: Implement batch query**

Add:

```python
def get_step_status_counts(self, step_name: str) -> dict[str, int]:
    with self._get_connection(readonly=True) as conn:
        rows = conn.execute(
            """
            SELECT status, COUNT(*) AS cnt
            FROM steps
            WHERE step_name = ?
            GROUP BY status
            """,
            (step_name,),
        ).fetchall()
    return {str(row["status"]): int(row["cnt"]) for row in rows}
```

Add workstation-aware variant only if barrier needs per-workstation solver dispatch:

```python
def get_step_status_counts_by_workstation(self, step_name: str) -> dict[str, dict[str, int]]:
    ...
```

- [ ] **Step 3: Refactor BarrierCoordinator monitor loop**

Replace repeated:

```python
for cn in all_configs:
    s = self.state.get_step_status(cn, "meshing")
```

with status-count logic. Keep existing behavior:

- all terminal and all error: stop engine.
- any workstation completed: dispatch solver for ready workstation.
- partial errors: log once.

- [ ] **Step 4: Add query-count regression**

Use a spy state object in `tests/test_scheduler_modules.py`:

```python
def test_barrier_monitor_uses_batch_counts_for_large_config_set(monkeypatch):
    calls = {"get_step_status": 0, "get_step_status_counts": 0}

    class SpyState(FakeState):
        def get_step_status(self, config_name, step_name):
            calls["get_step_status"] += 1
            return super().get_step_status(config_name, step_name)

        def get_step_status_counts(self, step_name):
            calls["get_step_status_counts"] += 1
            return {"Completed": 100}

    # Start one monitor iteration and stop.
    assert calls["get_step_status_counts"] >= 1
    assert calls["get_step_status"] < 10
```

- [ ] **Step 5: Run scheduler/state tests**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests/test_state_manager.py tests/test_scheduler_modules.py -q
```

Expected: all pass.

---

### Task 7: MeshingMonitor Backoff Under Slot Contention

**Files:**
- Modify: `engine/scheduler/meshing_monitor.py`
- Test: `tests/test_scheduler_modules.py`

- [ ] **Step 1: Add backoff test**

```python
def test_meshing_monitor_uses_longer_backoff_when_slot_busy(monkeypatch):
    sleeps: list[float] = []

    def fake_pause_aware_sleep(seconds, paused, stopped):
        sleeps.append(seconds)
        stopped.set()
        return False

    monkeypatch.setattr("engine.scheduler.meshing_monitor.pause_aware_sleep", fake_pause_aware_sleep)
    # Build monitor with _try_start_worker returning False and one queued config.
    # Run _monitor_loop once.
    assert sleeps
    assert max(sleeps) >= 1.0
```

- [ ] **Step 2: Implement conservative backoff**

Replace fixed `0.2` with config-local constants:

```python
_BUSY_SLOT_REQUEUE_SLEEP_SECONDS = 1.0
_BUSY_SLOT_REQUEUE_SLEEP_MAX_SECONDS = 5.0
```

Track consecutive busy requeues:

```python
busy_requeue_count = 0
...
if not self._try_start_worker(...):
    busy_requeue_count += 1
    sleep_seconds = min(
        _BUSY_SLOT_REQUEUE_SLEEP_MAX_SECONDS,
        _BUSY_SLOT_REQUEUE_SLEEP_SECONDS * busy_requeue_count,
    )
    self._meshing_queue.requeue(config_name)
    if not pause_aware_sleep(sleep_seconds, self._paused, self._stopped):
        break
else:
    busy_requeue_count = 0
```

- [ ] **Step 3: Preserve responsiveness**

Ensure pause/stop still interrupts sleep via `pause_aware_sleep`; do not use raw `time.sleep()`.

- [ ] **Step 4: Run meshing monitor tests**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests/test_scheduler_modules.py -k MeshingMonitor -q
```

Expected: all pass.

---

### Task 8: Daemon Child Process Health Monitoring

**Files:**
- Modify: `engine/daemon.py`
- Test: `tests/test_daemon_server_mode.py`
- Test: `tests/test_daemon_local_worker_handlers.py`

- [ ] **Step 1: Add alert watcher restart test**

```python
def test_daemon_restarts_exited_alert_watcher(monkeypatch):
    from engine.daemon import PipelineDaemon

    daemon = PipelineDaemon()
    starts: list[str] = []

    class DeadProcess:
        def poll(self):
            return 1

    daemon._alert_watcher_process = DeadProcess()
    monkeypatch.setattr("engine.daemon.is_server_mode", lambda: True)
    monkeypatch.setenv("AUTOFLUID_OPENCLAW_WEBHOOK_URL", "http://example.invalid/hook")
    monkeypatch.setattr(daemon, "_start_alert_watcher", lambda: starts.append("alert"))

    daemon._check_child_process_health_once()

    assert starts == ["alert"]
```

- [ ] **Step 2: Add LocalWorker restart test**

```python
def test_daemon_restarts_exited_local_worker_when_required(monkeypatch):
    from engine.daemon import PipelineDaemon

    daemon = PipelineDaemon()
    starts: list[str] = []

    class DeadProcess:
        def poll(self):
            return 1

    daemon._local_worker_process = DeadProcess()
    monkeypatch.setattr("engine.daemon.is_server_mode", lambda: True)
    monkeypatch.setattr(daemon, "_ensure_local_worker_autostarted", lambda: starts.append("worker"))

    daemon._check_child_process_health_once()

    assert starts == ["worker"]
```

- [ ] **Step 3: Implement health check helper**

Add:

```python
def _check_child_process_health_once(self) -> None:
    alert = getattr(self, "_alert_watcher_process", None)
    if alert is not None and alert.poll() is not None:
        logger.warning("[AlertWatcher] 子进程已退出，准备按退避策略重启")
        self._alert_watcher_process = None
        self._start_alert_watcher()

    worker = getattr(self, "_local_worker_process", None)
    if worker is not None and worker.poll() is not None:
        logger.warning("[LocalWorker] 自动唤起的子进程已退出，准备按退避策略重启")
        self._local_worker_process = None
        self._ensure_local_worker_autostarted()
```

Call it inside daemon main loop no more than once every 5-15 seconds.

- [ ] **Step 4: Add backoff guard**

Use existing `_local_worker_last_start_attempt` cooldown. Add `_alert_watcher_last_start_attempt` with similar cooldown to avoid restart storms.

- [ ] **Step 5: Run daemon tests**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests/test_daemon_server_mode.py tests/test_daemon_local_worker_handlers.py -q
```

Expected: all pass.

---

### Task 9: Alert Watcher Fingerprint Pruning

**Files:**
- Modify: `tools/autofluid_cli.py`
- Test: `tests/test_autofluid_cli.py`

- [ ] **Step 1: Add prune helper test**

```python
def test_prune_expired_alert_fingerprints():
    from tools.autofluid_cli import _prune_expired_fingerprints

    seen_until = {"old": 10.0, "new": 30.0}
    _prune_expired_fingerprints(seen_until, current_time=20.0)

    assert seen_until == {"new": 30.0}
```

- [ ] **Step 2: Implement helper**

```python
def _prune_expired_fingerprints(seen_until: dict[str, float], current_time: float) -> None:
    expired = [fingerprint for fingerprint, expires_at in seen_until.items() if expires_at <= current_time]
    for fingerprint in expired:
        seen_until.pop(fingerprint, None)
```

- [ ] **Step 3: Call helper periodically**

Inside `_watch_alerts()`:

```python
if loops % 20 == 0 or len(seen_until) > 1000:
    _prune_expired_fingerprints(seen_until, now())
```

- [ ] **Step 4: Run CLI tests**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests/test_autofluid_cli.py -q
```

Expected: all pass.

---

### Task 10: Rotating Log Files

**Files:**
- Modify: `utils/logger.py`
- Test: `tests/test_detail_log.py` or new `tests/test_logger_rotation.py`

- [ ] **Step 1: Add rotating handler test**

```python
def test_setup_logger_uses_rotating_file_handler(tmp_path, monkeypatch):
    import logging.handlers
    from utils.logger import setup_logger

    log_file = tmp_path / "service.log"
    logger = setup_logger("rotation-test", log_file=str(log_file))

    file_handlers = [
        handler for handler in logger.handlers
        if isinstance(handler, logging.handlers.RotatingFileHandler)
    ]
    assert file_handlers
    assert file_handlers[0].maxBytes > 0
    assert file_handlers[0].backupCount >= 1
```

- [ ] **Step 2: Implement rotating file handler**

Replace:

```python
file_handler = logging.FileHandler(log_file, encoding="utf-8")
```

with:

```python
from logging.handlers import RotatingFileHandler

file_handler = RotatingFileHandler(
    log_file,
    maxBytes=int(os.environ.get("AUTOFLUID_LOG_MAX_BYTES", str(20 * 1024 * 1024))),
    backupCount=int(os.environ.get("AUTOFLUID_LOG_BACKUP_COUNT", "10")),
    encoding="utf-8",
)
```

Apply the same change in deferred logger flush path if it creates a file handler.

- [ ] **Step 3: Preserve broadcast/deferred behavior**

Run tests that cover log buffers and detail logs:

```powershell
.venv\Scripts\python.exe -m pytest tests/test_detail_log.py tests/test_log_paths.py -q
```

Expected: all pass.

---

### Task 11: Dashboard Payload Size Guard

**Files:**
- Modify: `engine/daemon.py`
- Test: `tests/test_daemon_dashboard.py`

- [ ] **Step 1: Add non-log payload budget test**

```python
def test_dashboard_payload_stays_under_rust_ipc_cap_for_many_configs(monkeypatch):
    from engine.daemon import _json_size_bytes, _MAX_DASHBOARD_LOG_BYTES

    data = {
        "statuses": {
            str(i): {step: "Waiting" for step in ["sw", "sc", "transfer", "meshing", "solver", "postprocess"]}
            for i in range(200)
        },
        "config_workstations": {str(i): "WS-A" for i in range(200)},
        "engine": {"status": "running"},
        "health": {},
        "logs": {"entries": [{"message": "x" * 1000} for _ in range(1000)]},
    }
    PipelineDaemon._trim_dashboard_logs_to_budget(data)
    assert _json_size_bytes(data) <= _MAX_DASHBOARD_LOG_BYTES
```

- [ ] **Step 2: Confirm trim happens before response serialization**

Keep `_trim_dashboard_logs_to_budget(data)` in `handle_get_dashboard()` before returning. If non-log data alone exceeds the cap, add a warning field:

```python
if _json_size_bytes(data) > _MAX_DASHBOARD_LOG_BYTES:
    data["logs"] = {"entries": [], "truncated": True}
    data["dashboard_warning"] = "dashboard payload exceeded log budget without logs"
```

- [ ] **Step 3: Run dashboard tests**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests/test_daemon_dashboard.py -q
```

Expected: all pass.

---

### Task 12: Integrated 100+ Config Simulation Gate

**Files:**
- Test: `tests/test_pipeline_large_batch_scalability.py`

- [ ] **Step 1: Add large batch scheduler simulation test**

Use fakes/mocks; do not launch SolidWorks, SpaceClaim, Fluent, SSH, or daemon sockets.

```python
def test_large_batch_status_and_barrier_paths_remain_bounded(tmp_path, monkeypatch):
    from engine.state_manager import StateManager
    from engine.config import STATUS_COMPLETED

    state = StateManager(db_path=str(tmp_path / "state.db"))
    state.load_configs(list(range(1, 151)))

    for config_name in range(1, 151):
        state.set_step_status(config_name, "meshing", STATUS_COMPLETED)

    counts = state.get_step_status_counts("meshing")

    assert counts[STATUS_COMPLETED] == 150
```

- [ ] **Step 2: Add command/control nonblocking source gate**

```python
def test_tui_has_no_main_loop_blocking_ipc_calls():
    source = Path("autofluid-tui/src/lib.rs").read_text(encoding="utf-8")
    forbidden = [
        "block_on(ipc.get_dashboard",
        "block_on(command::dispatch_command",
    ]
    for needle in forbidden:
        assert needle not in source
```

- [ ] **Step 3: Add SC timeout source/behavior gate**

```python
def test_sc_timeout_path_retires_persistent_slot_source_guard():
    source = Path("engine/sc_process_pool.py").read_text(encoding="utf-8")
    assert "_retire_persistent_slot_after_failure" in source
    assert "构型{config_name} 超时" in source
```

- [ ] **Step 4: Run focused integrated gate**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests/test_pipeline_large_batch_scalability.py -q
```

Expected: all pass in under 10 seconds on local dev machine.

---

## Full Verification Matrix

Run after all tasks:

```powershell
.venv\Scripts\python.exe -m ruff check .
.venv\Scripts\python.exe -m mypy .
.venv\Scripts\python.exe -m pytest tests/ -q
cd autofluid-tui
cargo fmt --check
cargo check
cargo clippy -- -D warnings
cargo test
cd ..\bridge\SpaceClaimBridge
.\compile_noref.bat
.\compile.bat
cd ..\..
git diff --check
```

Expected:

- Python lint/type/tests pass or only pre-existing unrelated failures are documented with exact test names.
- Rust fmt/check/clippy/test pass.
- C# no-ref and full compile pass.
- `git diff --check` reports no whitespace errors.

---

## Manual Runtime Smoke Test

Only run after unit gates pass.

- [ ] Start daemon and client via existing launchers.

```powershell
.\start_autofluid.bat --check
```

- [ ] Verify TUI remains responsive while daemon is unavailable or slow.

Manual check:

- Stop daemon or point IPC to a closed port.
- TUI should still accept keyboard input and redraw.
- It should show degraded IPC state instead of freezing.

- [ ] Run a fake or reduced multi-config pipeline if available.

Evidence to collect:

- `get_dashboard` returns under Rust 1 MB IPC cap.
- no continuous `reset/clean/start` command freezes in UI.
- logs rotate when threshold is lowered with:

```powershell
$env:AUTOFLUID_LOG_MAX_BYTES="4096"
$env:AUTOFLUID_LOG_BACKUP_COUNT="2"
```

- [ ] If real workstations are used, run only after explicit approval.

Evidence to collect:

- worker/daemon process IDs.
- IPC `get_dashboard` health.
- workstation output files for a small sample.
- no orphaned `SpaceClaimBridge.exe` or `SpaceClaim.exe` after forced SC timeout test.

---

## Rollback Strategy

- SC timeout fix is isolated to `engine/sc_process_pool.py`; rollback by reverting that file and SC tests.
- TUI nonblocking work should be committed separately from Python work; rollback Rust commit if UI behavior regresses.
- StateManager/barrier batch-query changes should keep old public methods intact; rollback by restoring barrier loop to previous per-config implementation.
- daemon/log/alert watcher changes are operational; keep env defaults conservative and document new env vars.

---

## Suggested Commit Sequence

Use Chinese commit messages:

```text
fix: 修复SC常驻槽位超时复用
fix: 避免TUI主循环阻塞IPC轮询
perf: 优化百构型状态统计与网格排队
fix: 增强daemon子进程与告警长期运行治理
test: 增加大批量长运行回归测试
```

Each commit must pass its focused tests before moving to the next one.

