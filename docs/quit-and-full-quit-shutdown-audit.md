# quit / quit full 退出路径快速核查

日期：2026-06-05

## 修正后的语义

本次核查按以下产品语义判断：

- `quit`：只退出 TUI 前端，daemon 后端和流水线继续运行；前后端应安全断开，下次启动 TUI 后应能重新连接 daemon、读取后端状态并继续控制。
- `quit full`：停止 daemon 后端和流水线，并退出 TUI。

因此，普通 `quit` 不应暂停流水线，不应把运行中构型落库为 `Paused`，也不应清理 SW / SC / SSH 资源。它的正确性重点是 detach / reconnect。

## 涉及路径

- TUI 命令：`autofluid-tui/src/event_handler/command.rs`
- TUI 主循环：`autofluid-tui/src/main.rs`
- TUI IPC 客户端：`autofluid-tui/src/ipc/client.rs`
- TUI 状态映射：`autofluid-tui/src/state/app_state.rs`
- Python IPC 服务端：`ipc/server.py`
- Python daemon 状态查询：`engine/daemon.py`
- SQLite 状态来源：`engine/state_manager.py`
- 客户端入口：`start_client.py`

## `quit` 当前实际行为

普通 `quit` 的行为符合“只退出前端”的设计：

1. `cmd_quit()` 在非 `quit full` 分支只返回 `CommandResult::Quit`，并提示“后台引擎仍在运行”。
2. `handle_command_result_refs()` 对 `CommandResult::Quit` 只设置 `state.should_quit = true`。
3. TUI 主循环退出后统一调用 `ipc.disconnect()`。
4. 因 `full_quit` 标志为 false，TUI 不会调用 `daemon.stop()`，也不会发送后端 stop 命令。

因此，`quit` 不会影响 daemon、scheduler、SW、SC、SSH 或状态数据库。流水线应继续按当前后端状态运行。

## 前后端断开安全性

当前 IPC 客户端 `disconnect()` 会对 TCP stream 执行 shutdown 并释放本地连接。服务端每个客户端连接由独立线程处理，当前端断开后：

- `_handle_client()` 在 `recv()` 读到 EOF 后退出循环；
- finally 中关闭 client socket；
- `_active_connections` 递减；
- IPC server 的 accept loop 保持运行，daemon 主循环不受影响。

这条路径符合安全 detach 的目标。普通 `quit` 不会把后端带停，也不会消耗一个永久连接名额。

## 再次启动前端后的状态恢复

`start_client.py` 重新启动 Rust TUI 后，TUI 会在启动时尝试连接 daemon IPC：

- 连接成功后设置 `state.connected = true`；
- 主循环每 1 秒调用 `get_all_status()`；
- 每 5 轮调用 `get_engine_status()`；
- `get_all_status()` 来自 daemon 的 `StateManager.get_all_statuses()`，查询 SQLite `steps` 表；
- `get_engine_status()` 查询 SQLite `engine_state`，并返回 `engine_status`、`sw_macro_started`、`barrier_passed`、`pipeline_started`。

所以，只要 daemon 仍在运行且 IPC 端口可用，重新启动 TUI 后可以重新读取后端持久状态，并继续通过 IPC 发出 `pause` / `start` / `reset` / `quit full` 等控制命令。

## 发现的问题与修改建议

### 1. 重连后首屏状态不是立即刷新

TUI 启动连接成功后，只显示“已连接到后台引擎”，真正的构型状态要等主循环轮询触发。当前 `last_ipc_poll` 初始化为 `Instant::now()`，因此首次 `get_all_status()` 通常要等约 1 秒，`get_engine_status()` 要等约 5 秒。

这不是流水线控制 bug，但会让用户在重新连接后的短时间内看到默认/空状态。

建议：

- IPC 连接成功后立即执行一次 `get_all_status()` 和 `get_engine_status()`；
- 或将 `last_ipc_poll` 初始化为已过期状态，使主循环第一轮立即刷新；
- 首次刷新失败时保留明确的“已连接但状态尚未刷新”提示。

### 2. 轮询失败后 `state.connected` 可能不及时反映真实连接

主循环只有在 `ipc.is_connected()` 为 true 时才轮询状态。若某次请求失败并触发 `auto_reconnect()`，IPC 客户端内部的 stream 可能已经被清空或替换，但主循环没有同步更新 `state.connected`。

这对普通 `quit` 的正常 detach / reconnect 路径影响不大；但在 daemon 临时断线或重启时，UI 可能短时间显示“已连接”，实际 IPC 已不可用。

建议：

- 每轮轮询后用 `ctx.state.connected = ctx.ipc.is_connected()` 同步连接状态；
- 当 `get_all_status()` / `get_engine_status()` 返回错误时，在信息栏或日志区显示连接异常；
- 自动重连成功后立即触发一次状态刷新。

### 3. `start_daemon.py` 顶部说明与当前 TUI 行为不一致

`start_daemon.py` 注释写着“TUI 客户端中也可通过 Ctrl+C 退出（发送 IPC full_quit 命令关闭后台引擎）”。但当前 Rust TUI 中 Ctrl+C 只是设置 `state.should_quit = true`，等价于普通前端退出，不会发送 `full_quit`。

建议：

- 将该说明改为：TUI 中 `quit` / Ctrl+C 只退出前端，后台继续运行；
- 明确只有 `quit full` 或 daemon stop 才会停止后台引擎；
- README / help 文案也应保持这个语义一致。

### 4. 缺少 detach / reconnect 的 focused tests

建议增加小范围回归测试或集成测试：

- Rust：`quit` 只产生 `CommandResult::Quit`，不调用 `full_quit`；
- Rust：主循环退出时普通 `quit` 只调用 `ipc.disconnect()`，不调用 `daemon.stop()`；
- Python IPC：客户端断开后 `_active_connections` 递减，server 仍能接受新连接；
- Rust/Python 集成：重新启动 TUI 后能通过 `get_all_status()` / `get_engine_status()` 读取 daemon 当前状态。

## `quit full` 对照结论

`quit full` 仍是完整停止路径。后端当前清理顺序总体合理：daemon 收到 stop 后执行 shutdown，scheduler.stop() 会先将 `Running` / `Retrying` 步骤落库为 `Paused`，再设置 `engine_status = stopped`，随后清理 SW、SC、SSH。

但 `quit full` 仍有此前发现的可靠性改进点：

- Rust 确认路径存在重复发送 `full_quit` 的情况；
- 多处 `let _ = ipc.full_quit()` 忽略返回值；
- `DaemonManager.stop()` 等待 daemon 退出超时后仍无条件删除 PID 文件。

这些属于 `quit full` 的完全退出可靠性问题，不属于普通 `quit` 的 detach 语义问题。

## 当前结论

普通 `quit` 的核心 detach 行为是合理的：它只关闭 TUI IPC 连接，不停止 daemon，不暂停流水线，也不修改构型状态。下次运行 `start_client.py` 时，只要 daemon IPC 仍可用，TUI 会重新连接并从后端 SQLite 状态读取当前构型和引擎状态。

建议优先做三项小修：启动连接后立即刷新状态、轮询失败时同步 `state.connected`、修正 `start_daemon.py` / README 中关于 Ctrl+C 与 `quit` 的文案。
