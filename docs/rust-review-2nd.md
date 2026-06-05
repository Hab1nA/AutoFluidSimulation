# AutoFluid TUI Rust 代码审查（第二轮）

> 审查日期：2026-06-05  
> 审查范围：`autofluid-tui/` 下所有 `.rs` 和 `Cargo.toml`  
> 审查重点：功能重复实现、不规范代码、边界条件/竞态/逻辑缺陷

---

## 🔴 严重（可导致 Bug 或行为错误）

### S1. StopDaemon 对话框确认：鼠标点击与键盘确认行为不一致

- **文件**：`mouse.rs` L1076-1085 vs `main.rs` L412-414
- **问题**：通过键盘（Y 键）确认 StopDaemon 时，走 `AppAction::Confirm` → `daemon.stop_with_ipc()`，该方法会：发送 IPC `full_quit` → 断开 IPC → 调用 `daemon.stop()` 等待进程退出 → 设置 `state.connected = false`。  
  但通过鼠标点击"确认"按钮时，走 `handle_dialog_button_click` → **内联实现**，仅发送 IPC `full_quit` + 断开 IPC + 设置 `state.connected = false`，**未调用 `daemon.stop()`**。  
  **后果**：鼠标确认后 TUI 以为 daemon 已停止，但 daemon 进程可能仍在运行（TUI 不等待其退出）。
- **严重程度**：🔴 功能行为差异，可能导致用户误以为 daemon 已停止
- **修复建议**：鼠标路径应调用 `daemon.stop_with_ipc()`，与键盘路径保持一致。

### S2. 设置页保存逻辑：两处 ~40 行近乎相同的代码

- **文件**：`main.rs` L436-477（`SaveSettings` action）vs `mouse.rs` L1119-1170（Settings 对话框 btn_idx==0）
- **问题**：两处均执行：commit_edit → clear_errors → save() → reload_config IPC → 日志输出，**代码完全相同**。未来任何一处修改都可能遗漏另一处，导致键盘保存（Ctrl+S）与鼠标保存行为分歧。
- **严重程度**：🔴 维护风险，当前无 bug 但极易引入
- **修复建议**：提取 `fn save_settings(state, ipc, rt, log_buffer)` 公共函数。

### S3. `generate_request_id()` 仅 32 bit，存在碰撞风险

- **文件**：`main.rs` L107-126
- **问题**：`request_id` 格式为 8 位 hex（32 bit），由 `time_part.wrapping_add(count)` 生成。`time_part` 是时间戳 XOR 后取低 32 位，count 是原子递增计数器。在高频轮询（1s 间隔 × 3 个并发轮询命令）下，32 bit 空间虽不易碰撞，但 `Relaxed` 原子序 + 低位时间戳精度（Windows ~15.6ms）意味着同一 tick 内仅靠计数器区分。更关键的是：**Python 端如果用 `request_id` 做请求-响应匹配或幂等判断**，碰撞会导致静默错误。
- **严重程度**：🔴 潜在数据正确性风险
- **修复建议**：改为 16 位 hex（64 bit），使用纯单调递增计数器 + 启动时间戳前缀。

### S4. `auto_scroll` 在用户手动滚动到底部时被强制重激活

- **文件**：`main.rs` L527-539（`apply_auto_scroll` 函数）
- **问题**：`apply_auto_scroll` 的第二个条件块：
  ```rust
  if visual_count > content_height {
      let max_scroll = (visual_count - content_height) as u16;
      if *scroll >= max_scroll {
          *auto_scroll = true;
      }
  }
  ```
  每帧无条件检查，**不论是否有新日志到达**。当用户手动滚动到底部查看内容时，`auto_scroll` 被强制设为 `true`，下一条新日志到达后滚动位置会被拉到最底部——**覆盖用户意图**。
  
  注：`do_redraw` L544-548 的 `log_generation` 检查仅对 **info 面板** 无条件重置 `auto_scroll = true`，而 `apply_auto_scroll` 对两个面板都执行底部检测。
- **严重程度**：🔴 用户体验 bug
- **修复建议**：`apply_auto_scroll` 中底部检测应仅在新日志到达时（传入 `new_logs_arrived: bool` 参数）才重置 `auto_scroll`。

---

## 🟡 中等（重复实现 / 设计缺陷）

### S5. 滚动逻辑分散在三处，步长和守卫条件不统一

- **文件**：
  - `key_handler.rs` L155-180（`ScrollArea` trait + `handle_scroll_keys`）：步长 1/10，有 `> 0` 守卫
  - `mouse.rs` L210-280（`handle_scroll_up`/`handle_scroll_down`）：步长 3，`up` 有 `> 0` 守卫，`down` 无守卫直接 `saturating_add`
  - `key_handler.rs` L310-340（`handle_dialog_scroll_keys`）：步长 1/10，有守卫
- **问题**：
  - 鼠标滚动步长（3）与键盘步长（1）散落为魔法数字，无统一常量
  - `handle_scroll_up` 对 table/info/detail 有 `> 0` 前置检查避免无效更新，`handle_scroll_down` 无此检查（靠后续 clamp），逻辑不对称
  - InfoLog/DetailLog 面板鼠标滚动时不设置 `info_log_auto_scroll = false`/`detail_log_auto_scroll = false`（键盘滚动和滚轮滚动会设置），仅在 `ScrollUp` 时设置
- **严重程度**：🟡 行为不一致，用户可感知
- **修复建议**：
  - 定义 `SCROLL_LINE=1, SCROLL_WHEEL=3, SCROLL_PAGE=10` 常量
  - 统一 `scroll_up`/`scroll_down` 的守卫模式
  - 鼠标滚轮滚动 info/detail 面板时也应禁用 auto_scroll

### S6. 对话框确认结果处理：键盘 vs 鼠标路径不统一

- **文件**：
  - 键盘：`main.rs` L403-425（`AppAction::Confirm`）
  - 鼠标：`mouse.rs` L1061-1095（`handle_dialog_button_click`）
- **问题**：两处都处理 `CommandResult::FullQuit` 和 `CommandResult::StopDaemon`，但实现不同：
  - 键盘 FullQuit：设置 `**full_quit = true` → IPC `full_quit()` → disconnect → `should_quit`
  - 鼠标 FullQuit：设置 `*full_quit = true` → `should_quit`（**未发送 IPC full_quit，也未 disconnect**）
  - 键盘 StopDaemon：调用 `daemon.stop_with_ipc()`
  - 鼠标 StopDaemon：内联实现（缺少 `daemon.stop()` 调用，见 S1）
- **严重程度**：🟡 可能导致 FullQuit 时 daemon 未收到停止命令
- **修复建议**：统一为同一个 `fn handle_confirm_result(result, ctx)` 处理函数。

### S7. 按钮点击检测重复计算布局

- **文件**：`mouse.rs` L885-915（`detect_button`）vs `command_bar.rs` L55-75（`button_bounds`）
- **问题**：`detect_button` 手动累加每个按钮宽度来确定点击位置，`button_bounds` 也做同样的累加计算。两处逻辑必须同步维护。
- **严重程度**：🟡 维护风险
- **修复建议**：`detect_button` 应改为遍历 `BUTTON_DEFS`，对每个按钮调用 `button_bounds(i)` 得到 `Rect`，再用 `point_in_rect` 判断。

### S8. `ScrollbarRenderedInfo` 使用匿名元组存储滚动条信息

- **文件**：`state/app_state.rs` L151-158
- **问题**：`Option<(Rect, usize, usize, usize)>` 的语义为 `(area, total, visible, scroll)`，只能通过上下文理解。所有使用处（`mouse.rs` 的 6 处解构、`main.rs` 的 6 处赋值）都需要记住参数顺序。
- **严重程度**：🟡 可读性、易出错
- **修复建议**：定义 `struct ScrollbarInfo { area: Rect, total: usize, visible: usize, scroll: usize }`，替换所有匿名元组。

### S9. Settings 字段映射的 6 个 match 表需同步维护

- **文件**：`settings/mod.rs` L260-900+
- **问题**：新增一个配置字段需要在以下 6+ 处同步修改：`field_count()`、`field_name()`、`display_label()`、`get_field_value()`、`set_field_value()`、`is_bool_field()`（可选）、`is_password_field()`（可选）。当前无编译时保障确保不遗漏。
- **严重程度**：🟡 维护负担高，遗漏会导致运行时静默失败
- **修复建议**：在每个 category 的 match 块顶部加 `// ⚠️ NEW FIELD CHECKLIST` 注释提醒，或考虑使用宏/derive 生成映射。

---

## 🟢 轻微（规范 / 可改进）

### S10. `env_logger::init()` 未使用 `try_init()`

- **文件**：`main.rs` L62（`init_stderr_logger`）
- **问题**：`init()` 在 logger 已初始化时会 panic。虽然当前调用路径保证只调用一次，但作为防御性编程应使用 `try_init()`。
- **修复建议**：改为 `.try_init().ok();`

### S11. `config_io::save_config` 非原子写入

- **文件**：`settings/config_io.rs` L13-19
- **问题**：直接 `File::create` + `write_all`。写入过程中如果进程被 kill 或断电，配置文件可能损坏（部分写入）。
- **修复建议**：先写 `autofluid_config.toml.tmp`，再 `fs::rename` 覆盖原文件（原子替换）。

### S12. `log_info_message` 基于 emoji 和中文关键词推断日志级别

- **文件**：`state/log_buffer.rs` L120-135
- **问题**：通过 `contains("失败")`、`contains("错误")` 等判断是否为 error。如果 info 消息中包含"失败"但语义不是错误（如"避免了失败"），会被误标为 error 级别。
- **修复建议**：在 `push_info` 中增加可选 `level` 参数，让调用方显式指定。

### S13. `StepPatterns.transfer` 字段在 UI 中不可编辑

- **文件**：`settings/mod.rs` L285（`StepPatterns::field_count` = 5，不含 transfer）
- **问题**：`StepFilePatterns` 结构体有 `transfer: Option<String>` 字段（L110），但 UI 的 5 个字段映射跳过了它。TOML 序列化时 `transfer` 会被保留（如果有值），但用户无法在设置页查看或编辑。
- **修复建议**：要么在 UI 中添加 transfer 字段，要么在保存时显式注释说明此字段不参与 UI 编辑。

### S14. `ScrollbarRenderedInfo` 每帧 clone

- **文件**：`mouse.rs` L339（`let sb_info = state.scrollbar_info.clone();`）
- **问题**：每次鼠标事件处理时 clone 整个 `ScrollbarRenderedInfo`（包含 6 个 `Option<(Rect, usize, usize, usize)>`）。虽然数据量小，但完全可以在 `handle_mouse_down` 中直接借用 `&state.scrollbar_info`。
- **修复建议**：改为不可变借用。

### S15. `filter::parse_filter_arg` 不支持前缀匹配

- **文件**：`state/filter.rs` L22-45
- **问题**：用户输入 `warn` 不会匹配 `warning`，`err` 不会匹配 `error`。帮助文本提示的完整关键词列表较长，用户可能尝试缩写。
- **修复建议**：添加前缀匹配或 `starts_with` 匹配。

### S16. `build_edit_spans` 无 cursor 越界防御

- **文件**：`settings/settings_ui.rs` L275-310
- **问题**：当 `cursor >= chars.len()` 时追加 `▎` 光标指示符，逻辑正确。但无 `debug_assert!(cursor <= chars.len())` 防御。
- **修复建议**：加 `debug_assert!` 确保 cursor 不越界。

### S17. `ScrollUp` 信息面板滚动未禁用 auto_scroll 的对称性问题

- **文件**：`mouse.rs` L247-254
- **问题**：`handle_scroll_up` 中 InfoLog 面板设置了 `info_log_auto_scroll = false`，但 `handle_scroll_down`（L278-281）中 InfoLog 和 DetailLog **均未设置** auto_scroll = false。这意味着鼠标向下滚轮不会禁用自动滚动，但向上滚轮会——逻辑不对称。
- **修复建议**：`handle_scroll_down` 中 info/detail 分支也应设置 `*_auto_scroll = false`。

---

## 跨语言接口问题（仅记录，不修改 Rust 代码）

### X1. IPC 命令名称同步

- `ipc/protocol.rs` 定义了 11 个命令常量（`CMD_START`..`CMD_RELOAD_CONFIG`）
- 必须与 Python 端 `ipc/protocol.py` 中的命令名完全一致
- **验证方式**：`python -m pytest tests/test_ipc_protocol.py -v -k "command"`

### X2. 配置字段名一致性

- `settings/mod.rs` 中 `field_name()` 返回的 snake_case 名必须与 `autofluid_config.toml` 和 Python 端一致
- `StepPatterns` 的 `solverdata` 字段名需确认与 Python 端一致（Python 端可能用 `solver_data`）

---

## 统计摘要

| 类别 | 编号 | 数量 |
|------|------|------|
| 🔴 严重 | S1-S4 | 4 |
| 🟡 中等 | S5-S9 | 5 |
| 🟢 轻微 | S10-S17 | 8 |
| 📋 跨语言 | X1-X2 | 2 |

---

## 已排除的初次误判

| 原编号 | 原始描述 | 排除原因 |
|--------|----------|----------|
| 原#2 | IPC 重连窗口内旧 stream 可能未关闭 | **误判**：超时路径中 `reader` 被 `tokio::time::timeout` 的 drop 机制自动清理；读取错误路径中 `reader` 在函数返回时 RAII 清理。代码注释中已正确说明此行为。 |
| 原#6 | 详细日志双击复制 visual_line 索引可能错误 | **误判**：双击事件和重绘在同一帧顺序执行，`visual_line` 在点击时刻有效。日志到达只在 IPC 轮询（主循环特定阶段）发生，不会在事件处理期间改变缓冲区。 |
| 原#22 | `config_io::save_config` 非原子写入 | **保留但降级**：从"严重"降为 S11（轻微），因为配置写入频率极低（仅用户手动保存），且有 TOML 格式自描述性提供一定恢复能力。 |
| 原#14 | `log_info_message` 基于 emoji 判断级别 | **保留**，已在 S12 中记录。 |

---

## 相关文件索引

| 文件 | 涉及问题 |
|------|----------|
| `autofluid-tui/src/main.rs` | S2, S3, S4, S6, S10 |
| `autofluid-tui/src/event_handler/mouse.rs` | S1, S2, S5, S6, S7, S14, S17 |
| `autofluid-tui/src/event_handler/key_handler.rs` | S5, S6 |
| `autofluid-tui/src/event_handler/command.rs` | — |
| `autofluid-tui/src/ipc/client.rs` | S3 |
| `autofluid-tui/src/ipc/protocol.rs` | X1 |
| `autofluid-tui/src/state/app_state.rs` | S8 |
| `autofluid-tui/src/state/log_buffer.rs` | S12 |
| `autofluid-tui/src/state/filter.rs` | S15 |
| `autofluid-tui/src/settings/mod.rs` | S9, S13, X2 |
| `autofluid-tui/src/settings/config_io.rs` | S11 |
| `autofluid-tui/src/settings/settings_ui.rs` | S16 |
| `autofluid-tui/src/ui/command_bar.rs` | S7 |
| `autofluid-tui/src/ui/scrollbar.rs` | S8 |
