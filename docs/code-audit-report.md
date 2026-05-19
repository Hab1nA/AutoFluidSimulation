# AutoFluid 代码审查 — 遗留问题清单

> **审查日期**：2026-05-19
> **当前分支**：fix/improve-spaceclaim-schedule
> **说明**：P0（9 项）和大部分 P1/P2 问题已修复并通过测试验证。本文档仅列出尚待处理的遗留问题，共 9 项。

---

## 目录

1. [P1 — 需重构项（3 项）](#1-p1--需重构项)
2. [P2 — 代码质量改进（6 项）](#2-p2--代码质量改进)

---

## 1. P1 — 需重构项

> 中等规模重构，建议在功能稳定后单独 PR 处理。

### P1-10 Rust `handle_mouse_down` 参数过多（15 个）

**文件**：`autofluid-tui/src/event_handler/mouse.rs`

**问题详述**：
`handle_mouse_down` 函数签名为：

```rust
fn handle_mouse_down(
    state: &mut AppState,
    col: u16, row: u16,
    layout: &AppLayout,
    ipc: &mut IpcClient,
    rt: &tokio::runtime::Runtime,
    full_quit: &mut bool,
    log_buffer: &mut LogBuffer,
    in_table: bool, in_info: bool, in_detail: bool,
    in_buttons: bool, in_cmd_input: bool,
    // ... 其他参数
)
```

共 15 个参数（含 `#[allow(clippy::too_many_arguments)]`）。这带来三个问题：
1. **调用链过长**：每个调用点都需要传递 15 个参数，容易遗漏或传错顺序
2. **职责过重**：一个函数同时处理表格点击、按钮点击、命令输入点击、Daemon 菜单点击等多种逻辑
3. **难以测试**：参数过多导致单元测试构造困难

**建议方案**：
将相关参数封装为上下文结构体，减少参数数量：

```rust
struct MouseContext<'a> {
    state: &'a mut AppState,
    log_buffer: &'a mut LogBuffer,
    ipc: &'a mut IpcClient,
    rt: &'a tokio::runtime::Runtime,
    full_quit: &'a mut bool,
    layout: AppLayout,
    col: u16,
    row: u16,
}
```

然后 `handle_mouse_down(ctx: &mut MouseContext, in_table: bool, ...)` 只需传递区域命中标志。

**预估改动**：~80 行（封装结构体 + 重构调用链）

---

### P1-11 Rust `AppState` 字段过多（~45 个）

**文件**：`autofluid-tui/src/state/app_state.rs`

**问题详述**：
`AppState` 结构体包含约 45 个字段，涵盖 UI 状态、连接状态、命令状态、设置状态、日志状态等完全不同的关注点：

```rust
pub struct AppState {
    // UI 焦点与模式
    pub ui_mode: UiMode,
    pub focus_zone: FocusZone,
    pub should_quit: bool,
    // 表格滚动
    pub table_scroll_offset: u16,
    // 日志滚动
    pub info_log_scroll: u16,
    pub info_log_auto_scroll: bool,
    pub detail_log_scroll: u16,
    pub detail_log_auto_scroll: bool,
    // 命令输入
    pub command_input: String,
    pub command_cursor: usize,
    // 连接状态
    pub connected: bool,
    pub engine_status: String,
    // 鼠标/hover 状态
    pub hover_zone: Option<FocusZone>,
    pub clicked_button: Option<String>,
    pub click_time: Option<std::time::Instant>,
    // 对话框
    pub confirm_message: Option<String>,
    pub dialog_scroll: u16,
    // 设置
    pub settings: SettingsState,
    // ... 还有更多字段
}
```

这带来以下问题：
1. **"上帝对象"反模式**：所有状态扁平堆砌，任何模块都可以修改任何字段，导致状态变更难以追踪
2. **初始化冗长**：`AppState::new()` 需要初始化 45+ 个字段
3. **认知负担高**：新开发者难以理解哪些字段属于同一逻辑组

**建议方案**：
按职责拆分为子结构体：

```rust
pub struct AppState {
    pub ui: UiState,              // focus, mode, should_quit, terminal_size
    pub scroll: ScrollState,      // table/info/detail scroll offsets, auto_scroll
    pub command: CommandState,    // command_input, cursor, history
    pub connection: ConnectionState, // connected, engine_status, sw_macro_started
    pub mouse: MouseState,        // hover_zone, clicked_button, click_time, drag state
    pub dialog: DialogState,      // confirm_message, dialog_scroll, check_result
    pub settings: SettingsState,  // 已有独立结构体
}
```

**预估改动**：~300 行（结构体拆分 + 全局引用调整），影响面广，建议单独 PR。

---

### P1-14 Rust 重连循环阻塞 UI 10 秒

**文件**：`autofluid-tui/src/daemon_mgr.rs` — `reconnect_ipc_after_launch()`

**问题详述**：
当 Daemon 进程由 TUI 自动启动后，`reconnect_ipc_after_launch` 方法在当前线程中循环尝试重连 IPC，每次间隔 500ms，最多等待 10 秒：

```rust
pub fn reconnect_ipc_after_launch(&mut self, state: &mut AppState, ...) {
    let timeout = Duration::from_secs(10);
    let deadline = std::time::Instant::now() + timeout;
    while std::time::Instant::now() < deadline {
        if ipc.is_connected() { state.connected = true; return; }
        match rt.block_on(ipc.connect()) {
            Ok(()) => { state.connected = true; return; }
            Err(_) => { std::thread::sleep(Duration::from_millis(500)); }
        }
    }
}
```

问题在于 `std::thread::sleep` 会阻塞整个 tokio 单线程运行时，导致：
1. **UI 完全冻结**：10 秒内用户无法滚动日志、切换焦点、点击按钮
2. **无法取消**：用户无法中途放弃重连（必须等 10 秒超时）
3. **信号丢失**：重连期间如果 Daemon 已就绪，仍需等到下一个 sleep 结束才能检测到

**建议方案**：
将重连逻辑改为非阻塞模式——在主事件循环中设置状态标志，每次循环迭代尝试一次连接：

```rust
// 在 AppState 中添加：
pub reconnect_deadline: Option<std::time::Instant>,

// 在主循环中：
if let Some(deadline) = state.reconnect_deadline {
    if std::time::Instant::now() < deadline {
        if let Ok(()) = rt.block_on(ipc.connect()) {
            state.connected = true;
            state.reconnect_deadline = None;
        }
    } else {
        state.reconnect_deadline = None; // 超时，停止重连
    }
}
```

**预估改动**：~40 行（状态标志 + 主循环逻辑调整）

---

## 2. P2 — 代码质量改进

> 低优先级，不影响功能但可提升可维护性。

### P2-1 `configs` 表硬编码 `param1-param4` 四列

**文件**：`engine/state_manager.py` — `_init_database()` 方法

**问题详述**：
数据库 `configs` 表硬编码了 4 个参数列：

```python
conn.execute("""
    CREATE TABLE IF NOT EXISTS configs (
        config_name INTEGER PRIMARY KEY,
        param1 REAL NOT NULL,
        param2 REAL NOT NULL,
        param3 REAL NOT NULL,
        param4 REAL NOT NULL
    )
""")
```

如果未来参数数量变化（例如增加第 5 个参数或减少为 3 个），需要同时修改：
- 数据库 schema（ALTER TABLE）
- `load_configs` 的 INSERT 语句
- `get_config_params` 的 SELECT 语句
- `excel_reader.py` 的列范围常量

**跳过原因**：schema 变更需要数据库迁移逻辑（处理旧数据库文件），风险较高。当前 4 参数设计与 Excel 设计表一致，短期内不会变化。

---

### P2-2 `set_step_status` 每次新建 SQLite 连接

**文件**：`engine/state_manager.py` — `set_step_status()` 方法

**问题详述**：
每次调用 `set_step_status` 都通过 `_get_connection()` 上下文管理器创建一个新的 SQLite 连接，执行 PRAGMA 设置，写入一条 UPDATE，然后关闭连接：

```python
def set_step_status(self, config_name, step_name, status, error_message=""):
    with self._lock:
        with self._get_connection() as conn:  # 每次新建连接
            conn.execute("UPDATE steps SET status = ? ...", ...)
```

在高频调用场景下（如 SW 宏批量导出时逐构型更新状态、Worker 线程并发更新），可能造成连接风暴。SQLite 的 WAL 模式虽然支持并发读取，但频繁的连接创建/销毁仍有开销。

**跳过原因**：需要引入连接池或批量写入机制，影响面较广。当前 `_get_connection()` 的设计（每次独立连接 + 自动提交）保证了线程安全和事务隔离，功能上没有问题。性能影响在实际使用中可能不显著（SQLite 本地文件连接开销很小）。

---

### P2-10 `SettingCategory::field_count()` 与 `display_label()` 无编译器同步保证

**文件**：`autofluid-tui/src/settings/mod.rs`

**问题详述**：
`SettingCategory` 枚举的每个变体都有 `field_count()` 和 `display_label(idx)` 方法，两者使用独立的 `match` 分支：

```rust
impl SettingCategory {
    pub fn field_count(self) -> usize {
        match self {
            SettingCategory::LocalPaths => 10,
            SettingCategory::RemoteConnection => 6,
            SettingCategory::EngineConfig => 9,
            // ...
        }
    }
    pub fn display_label(self, idx: usize) -> &'static str {
        match self {
            SettingCategory::LocalPaths => match idx {
                0 => "SW可执行文件",
                1 => "SW模型文件",
                // ... 共 10 个
                _ => "",
            },
            // ...
        }
    }
}
```

如果有人在 `field_count` 中修改了字段数量但忘记同步更新 `display_label`，或反之，不会产生编译错误，只会导致运行时显示异常（多余的字段无标签，或标签索引越界显示空字符串）。

**跳过原因**：修复需要引入 Rust 宏（如 `define_settings!`）或数据驱动的数组索引映射，属于中等规模重构。当前代码通过人工审查保持一致性，实际出错概率低。

---

### P2-11 `run_app` 函数约 150 行职责混合

**文件**：`autofluid-tui/src/main.rs` — `run_app()` 函数

**问题详述**：
`run_app` 函数是 TUI 的主事件循环，约 150 行代码混合了以下职责：
1. **初始化**：创建 IPC 客户端、AppState、LogBuffer、DaemonManager
2. **事件循环**：`loop { ... }` 主循环
3. **事件分发**：键盘事件 → `handle_key`、鼠标事件 → `handle_mouse`
4. **IPC 轮询**：定时拉取引擎状态和日志
5. **时钟刷新**：定时更新运行时间显示
6. **渲染调度**：`do_redraw` 调用
7. **Daemon 管理**：自动启动 Daemon、重连逻辑

虽然每个子职责的代码量不大（10-20 行），但全部堆在一个函数中降低了可读性。

**跳过原因**：拆分需要将各职责提取为独立函数或方法，涉及状态借用传递的重构（Rust 的 borrow checker 对此较敏感）。当前代码逻辑清晰、注释充分，功能上没有问题。

---

### P2-12 `render_command_bar` 函数约 100 行

**文件**：`autofluid-tui/src/ui/command_bar.rs`

**问题详述**：
`render_command_bar` 函数同时负责：
1. **命令输入框渲染**：文本、光标位置、焦点高亮
2. **快捷按钮栏渲染**：8 个按钮的布局、悬停/点击状态
3. **Daemon 菜单渲染**：下拉菜单的布局和状态
4. **按钮点击区域计算**：为鼠标事件提供命中检测

这些职责虽然相关但可以拆分为更小的渲染函数。

**跳过原因**：纯 UI 渲染代码，拆分不影响功能正确性。当前代码结构清晰，每个区块有注释分隔。
