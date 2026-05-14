# 待修复问题清单

> 生成于 2026-05-15，基于全面代码审查结果
>
> 以下问题已审查确认但**本次未修复**，建议在未来迭代中按优先级处理。
> 优先级标注：🔴 高 / 🟡 中 / 🟢 低

---

## Rust TUI 前端（autofluid-tui/）

### 🟡 架构重构

#### 1. AppState 结构体字段过多

- **文件**: [state/app_state.rs](file:///c:/Users/XKZ/Documents/VSCode%20Projects/AutoFluidSimulation/autofluid-tui/src/state/app_state.rs)
- **描述**: `AppState` 拥有超过 50 个公共字段，违反单一职责原则。应将相关字段分组到子结构体中（例如 `ScrollState`、`ClickState`、`HoverState`、`MouseState`），使状态变更推理和缺陷定位更清晰。
- **建议**: 将鼠标/滚动/点击状态提取为独立结构体，AppState 保留组合引用

#### 2. `handle_mouse` 函数过长

- **文件**: [main.rs:L771-L1303](file:///c:/Users/XKZ/Documents/VSCode%20Projects/AutoFluidSimulation/autofluid-tui/src/main.rs#L771-L1303)（约 530 行）
- **描述**: 该函数处理所有鼠标交互（移动、滚动、拖拽、按下、释放）以及滚动条命中检测、按钮检测、字段检测、对话框按钮点击处理。逻辑高度集中，难以独立测试。
- **建议**: 按交互类型拆分为 `handle_mouse_click`、`handle_mouse_scroll`、`handle_mouse_drag` 等方法

#### 3. `dispatch_command` 函数过长

- **文件**: [event_handler/command.rs:L18-L311](file:///c:/Users/XKZ/Documents/VSCode%20Projects/AutoFluidSimulation/autofluid-tui/src/event_handler/command.rs#L18-L311)（约 290 行）
- **描述**: 所有命令分发逻辑集中在一个函数中，包含大量 match arm，每个 arm 中有内联逻辑。
- **建议**: 每个命令一个处理函数，dispatch 仅做路由

#### 4. `do_redraw` 函数过长

- **文件**: [main.rs:L461-L683](file:///c:/Users/XKZ/Documents/VSCode%20Projects/AutoFluidSimulation/autofluid-tui/src/main.rs#L461-L683)（约 220 行）
- **描述**: 渲染函数中包含大量滚动条信息计算逻辑，且各区域的计算模式高度重复。
- **建议**: 抽象为统一的滚动条辅助函数

### 🟡 代码重复（DRY）

#### 5. `char_to_byte_index` 重复实现

- **文件**: [settings/mod.rs:L650-L655](file:///c:/Users/XKZ/Documents/VSCode%20Projects/AutoFluidSimulation/autofluid-tui/src/settings/mod.rs#L650-L655) + [event_handler/key_handler.rs:L54-L59](file:///c:/Users/XKZ/Documents/VSCode%20Projects/AutoFluidSimulation/autofluid-tui/src/event_handler/key_handler.rs#L54-L59)
- **描述**: 完全相同的 `char_to_byte_index` 函数在两个文件中各定义一次。
- **建议**: 提取到共享工具模块

#### 6. `truncate_for_display` 重复实现

- **文件**: [ui/dialogs.rs:L338-L361](file:///c:/Users/XKZ/Documents/VSCode%20Projects/AutoFluidSimulation/autofluid-tui/src/ui/dialogs.rs#L338-L361) + [settings/settings_ui.rs:L353-L377](file:///c:/Users/XKZ/Documents/VSCode%20Projects/AutoFluidSimulation/autofluid-tui/src/settings/settings_ui.rs#L353-L377)
- **描述**: 字符串截断逻辑完全一致地重复。
- **建议**: 提取到 `ui/` 模块的共享工具函数

#### 7. `pad_label_by_display_width` 重复实现

- **文件**: [ui/dialogs.rs:L332-L336](file:///c:/Users/XKZ/Documents/VSCode%20Projects/AutoFluidSimulation/autofluid-tui/src/ui/dialogs.rs#L332-L336) + [settings/settings_ui.rs:L335-L339](file:///c:/Users/XKZ/Documents/VSCode%20Projects/AutoFluidSimulation/autofluid-tui/src/settings/settings_ui.rs#L335-L339)
- **描述**: 标签填充逻辑重复。
- **建议**: 提取到共享工具函数

### 🟡 架构耦合

#### 8. `format_local_time` 定义位置不当

- **文件**: [main.rs:L27-L36](file:///c:/Users/XKZ/Documents/VSCode%20Projects/AutoFluidSimulation/autofluid-tui/src/main.rs#L27-L36)
- **描述**: `format_local_time` 在 crate 根目录（main.rs）中定义，但被 `ui/header.rs` 和 `event_handler/command.rs` 引用，创建了从 UI 层到入口点的反向依赖。
- **建议**: 移动到独立的 `utils` 或 `time` 模块

### 🟢 可维护性

#### 9. 点击超时检查重复代码

- **文件**: [main.rs:L196-L231](file:///c:/Users/XKZ/Documents/VSCode%20Projects/AutoFluidSimulation/autofluid-tui/src/main.rs#L196-L231)
- **描述**: 五个几乎完全相同的点击超时检查块（按钮、守护进程菜单、对话框、详情行、设置字段）在主循环中重复出现。
- **建议**: 提取为 `clear_expired_clicks(&mut self)` 方法

#### 10. `End` 键滚动偏移量使用 `u16::MAX`

- **文件**: [event_handler/key_handler.rs:L158](file:///c:/Users/XKZ/Documents/VSCode%20Projects/AutoFluidSimulation/autofluid-tui/src/event_handler/key_handler.rs#L158)
- **描述**: `End` 键将 `table_scroll_offset` 设置为 `u16::MAX`，依赖 `do_redraw` 中的 `clamp_table_scroll` 进行钳位修正。不够直观。
- **建议**: 改为在 `End` 键处理方法中直接计算最大滚动量

#### 11. 魔术数字 `3`

- **文件**: [ui/command_bar.rs:L143-L153](file:///c:/Users/XKZ/Documents/VSCode%20Projects/AutoFluidSimulation/autofluid-tui/src/ui/command_bar.rs#L143-L153)（+3）、[settings/settings_ui.rs:L192](file:///c:/Users/XKZ/Documents/VSCode%20Projects/AutoFluidSimulation/autofluid-tui/src/settings/settings_ui.rs#L192)（saturating_sub(3)）
- **描述**: 多处出现含义不明确的魔术数字。
- **建议**: 提取为命名常量并添加注释

#### 12. `info_generation` 命名不一致

- **文件**: [state/log_buffer.rs:L61-L74](file:///c:/Users/XKZ/Documents/VSCode%20Projects/AutoFluidSimulation/autofluid-tui/src/state/log_buffer.rs#L61-L74)
- **描述**: `push_detail` 不会递增 `info_generation`，但 `info_generation` 在 `do_redraw` 中用于触发信息面板的自动滚动。仅向详情日志推送数据时自动滚动不会触发。
- **建议**: 重命名为 `log_generation` 或拆分两个计数器

### 🟢 微小优化

#### 13. 不必要的 `cn_str.clone()`

- **文件**: [ui/table.rs:L46](file:///c:/Users/XKZ/Documents/VSCode%20Projects/AutoFluidSimulation/autofluid-tui/src/ui/table.rs#L46)
- **描述**: `cn_str` 在创建 Cell 时多 clone 了一次。
- **建议**: 考虑直接引用

#### 14. `_state` 参数未使用

- **文件**: [ui/header.rs:L9](file:///c:/Users/XKZ/Documents/VSCode%20Projects/AutoFluidSimulation/autofluid-tui/src/ui/header.rs#L9)
- **描述**: `render_header` 接收 `_state` 但从未使用。
- **建议**: 移除参数或添加 `#[allow(dead_code)]`

---

## Python 后端

### 🟡 架构重构

#### 15. 默认配置含硬编码开发路径

- **文件**: [engine/config.py:L41-L86](file:///c:/Users/XKZ/Documents/VSCode%20Projects/AutoFluidSimulation/engine/config.py#L41-L86)
- **描述**: `LOCAL_PATHS` 和 `REMOTE_CONFIG` 中将硬编码的路径、IP 地址和用户名作为默认值（如 `C:\Users\XKZ\...`），这些路径仅对一位开发者的计算机有效。
- **建议**: 默认值改为空字符串或占位符，由 TOML 配置文件或环境变量填充，并启动时验证

#### 16. SSH 密码以明文存储

- **文件**: [utils/ssh_client.py](file:///c:/Users/XKZ/Documents/VSCode%20Projects/AutoFluidSimulation/utils/ssh_client.py)
- **描述**: SSH 密码在内存中以明文存储，并且可通过环境变量 `AUTOFLUID_SSH_PASSWORD` 暴露。
- **建议**: 考虑使用操作系统密钥链（Windows Credential Manager）或 SSH 密钥认证替代密码认证

#### 17. `StateManager._config_pragmas` 与 `_get_connection` 未在锁保护下

- **文件**: [engine/state_manager.py:L48-L58](file:///c:/Users/XKZ/Documents/VSCode%20Projects/AutoFluidSimulation/engine/state_manager.py#L48-L58)
- **描述**: `_config_pragmas` 方法在 `__init__` 中创建独立连接来设置 WAL 模式，而 `_get_connection` 每次创建新连接但不重复设置 PRAGMA。如果多个 `StateManager` 实例操作同一数据库文件，PRAGMA 可能未正确设置。
- **建议**: 将 PRAGMA 设置移到 `_get_connection` 中，或使用连接池

### 🟢 可维护性

#### 18. `_cleanup_on_exit` 中 `daemon_proc` 闭包引用

- **文件**: [main.py:L348-L376](file:///c:/Users/XKZ/Documents/VSCode%20Projects/AutoFluidSimulation/main.py#L348-L376)
- **描述**: 闭包直接捕获外部变量 `daemon_proc`，在 `_run_all_mode` 执行完毕后 `daemon_proc` 可能失效（函数作用域已结束），存在闭包陷阱风险（已替换 list 反模式，但 Python 闭包捕获的是变量名而非值，for 循环中仍然存在风险）。
- **建议**: 使用 `nonlocal` 声明或函数工厂模式

#### 19. `_stop_all_processes` 中使用 `wmic`

- **文件**: [main.py:L171-L195](file:///c:/Users/XKZ/Documents/VSCode%20Projects/AutoFluidSimulation/main.py#L171-L195)
- **描述**: `wmic` 命令在 Windows 11 24H2+ 中被弃用，未来可能被移除。
- **建议**: 替换为 `Get-CimInstance` PowerShell 命令或 `tasklist.exe` + `taskkill.exe` 的组合

#### 20. `openpyxl.load_workbook` 未使用上下文管理器

- **文件**: [engine/task_runner.py](file:///c:/Users/XKZ/Documents/VSCode%20Projects/AutoFluidSimulation/engine/task_runner.py) 中多处
- **描述**: 多处使用 `try/finally` 手动关闭 workbook 而非 `with` 上下文管理器。虽功能正确但不够 Pythonic。
- **建议**: 统一使用 `with openpyxl.load_workbook(...) as wb:`

---

## Python 测试

### 🟢 测试质量

#### 21. `test_process_message_unknown_command` 中 `import pytest` 位置不当

- **文件**: [tests/test_ipc_server.py:L60](file:///c:/Users/XKZ/Documents/VSCode%20Projects/AutoFluidSimulation/tests/test_ipc_server.py#L60)
- **描述**: `import pytest` 出现在函数定义之间，不符合 PEP8 导入规范。
- **状态**: ⚠️ 本次已部分修复（添加了 `import time` 在文件顶部），但 `import pytest` 仍在函数中间

#### 22. `TestContext` 被 pytest 误收集

- **文件**: [tests/test_pause_start.py:L182](file:///c:/Users/XKZ/Documents/VSCode%20Projects/AutoFluidSimulation/tests/test_pause_start.py#L182)
- **描述**: `TestContext` 类有 `__init__` 构造函数，pytest 会输出 `PytestCollectionWarning`。
- **建议**: 改名 `_TestContext` 或以 `contextlib.contextmanager` 改写

---

## 跨模块协调

### 🟡 接口兼容性

#### 23. Rust `ipc/client.rs` 连接静默断开

- **文件**: [ipc/client.rs:L48-L82](file:///c:/Users/XKZ/Documents/VSCode%20Projects/AutoFluidSimulation/autofluid-tui/src/ipc/client.rs#L48-L82)
- **描述**: `send_request` 中 `self.stream.take()` 后若读取失败，连接会静默丢失。虽然 `is_connected()` 会返回 `false`，但调用方可能继续尝试发送。
- **建议**: 添加显式的重连机制，或在关键命令前检查连接状态

#### 24. IPC 协议缺乏消息分帧

- **文件**: [ipc/client.rs:L48-L82](file:///c:/Users/XKZ/Documents/VSCode%20Projects/AutoFluidSimulation/autofluid-tui/src/ipc/client.rs#L48-L82)
- **描述**: 协议依赖以 `\n` 结尾的 JSON 行，但 TCP 是流式协议。单次 `read_until(b'\n')` 在残留数据或响应中包含嵌入换行符时可能读取不完整。
- **建议**: 使用长度前缀（length-prefixed framing）替代行分隔符

#### 25. `settings/mod.rs` 中 `dirty` 标志无条件置 true

- **文件**: [settings/mod.rs:L478](file:///c:/Users/XKZ/Documents/VSCode%20Projects/AutoFluidSimulation/autofluid-tui/src/settings/mod.rs#L478)
- **描述**: `set_field_value` 无论值是否实际变化，都会将 `self.dirty = true`。编辑后恢复原值会不必要地触发保存。
- **建议**: 仅在新值与旧值不同时设置 `dirty = true`

---
