# TUI 显示 Fluent Solver 剩余时间设计

> 文档版本：v2.0
> 更新日期：2026-06-14
> 目标：Fluent 开始迭代后，在 TUI 表格的 solver 步骤单元格中用剩余时间替换 `Running`，并保持绿色显示。

---

## 1. 需求与结论

当某个构型正在执行 `solver` 步骤，且 Fluent transcript 已报告剩余时间时，TUI 表格中该构型的 solver 单元格从：

```text
⏳ Running
```

替换为：

```text
⏳ 01:23:45
```

约束：

- 只替换正在执行 solver 的表格单元格，不修改顶部 `info_bar` 的引擎状态文本。
- 只有 `solver` 步骤有剩余时间数据时替换；`sw`、`sc`、`transfer`、`meshing` 的 `Running` 显示保持原样。
- 剩余时间单元格使用绿色；不全局修改 `STATUS_RUNNING` 的颜色，避免影响其他步骤。
- 如果 Fluent transcript 格式未知或暂时无进度数据，TUI 继续显示原始 `⏳ Running`，不得影响求解流程。

---

## 2. 本地现状

### 2.1 TUI 状态与显示路径

当前 TUI 的状态表格由 `autofluid-tui/src/ui/table.rs` 渲染：

```rust
let status = state.get_step_status(&cn_str, step);
let icon = status_icon(status);
let color = status_color(status);
let text = format!("{} {}", icon, status);
```

因此 `Running` 出现在表格单元格，而不是顶部 `info_bar`。顶部 `info_bar` 由 `AppState::info_bar_text()` 生成，显示的是引擎状态、构型数、屏障状态、IPC/worker/SSH 健康信息。

TUI 主循环现在通过 `get_dashboard` 拉取聚合数据：

```text
get_dashboard
  ├─ statuses
  ├─ engine
  ├─ health
  └─ logs
```

所以 solver 剩余时间应加入现有 `engine` payload；不要新增独立轮询命令。

### 2.2 Daemon 与远程 solver 路径

当前 solver 启动与等待路径：

```text
executor/remote_executor.py
  ├─ _build_solver_command()
  │    └─ 启动 executor/remote_scripts/batch_solver_gen4.py
  ├─ execute_solver()
  │    └─ 通过交互式计划任务启动远程 Fluent
  └─ wait_solver_completion()
       └─ 每 30s 检查 done/error flag 和 cas/dat 输出文件
```

`batch_solver_gen4.py` 目前直接执行：

```python
solver_session.tui.solve.iterate(args.iterate_count)
```

Fluent 迭代期间的 transcript 输出尚未被捕获；daemon 也没有 `solver_progress` 状态。

### 2.3 工作区注意事项

当前工作区已有未提交改动，且涉及本功能会触及的文件，包括：

- `autofluid-tui/src/state/app_state.rs`
- `autofluid-tui/src/main.rs`
- `autofluid-tui/src/ui/header.rs`
- `engine/daemon.py`
- `tests/test_daemon_dashboard.py`

实施时必须在现有工作区状态上增量合并，不能覆盖或回滚这些改动。

---

## 3. 架构方案

采用 Fluent transcript 回调作为主方案。

```text
远程工作站
  batch_solver_gen4.py
    ├─ launch_fluent()
    ├─ register transcript callback
    ├─ iterate()
    │    └─ Fluent 每次迭代输出 remaining time
    ├─ parse remaining time
    └─ atomic write solver_progress_<config>.json

Daemon / 本地控制端
  remote_executor.wait_solver_completion()
    ├─ 每 30s 读取 progress JSON
    ├─ state_manager.set_solver_progress()
    └─ 完成/失败/超时/停止时 clear_solver_progress()

IPC
  get_engine_status()
    └─ data.solver_progress
  get_dashboard()
    └─ engine 复用 get_engine_status payload

TUI
  apply_dashboard_response()
    └─ AppState.update_engine_info()
         └─ EngineInfo.solver_progress
  table.rs
    └─ solver Running 单元格显示 HH:MM:SS
```

选择 transcript 的理由：

- 直接使用 Fluent 自身预估的剩余时间，不在 daemon 侧重新估算。
- 回调与 `iterate()` 并行，避免阻塞主求解调用。
- 不改变 Fluent journal、report monitor 或求解配置。
- transcript 解析失败时可以自然降级为不显示进度。

---

## 4. 数据与接口设计

### 4.1 远程 progress 文件

路径：

```text
{flag_dir}/solver_progress_{config_id}.json
```

内容：

```json
{
  "config_name": 5,
  "current_iter": 350,
  "total_iter": 1000,
  "remaining_sec": 5025.0,
  "raw_line": "iter 350 ... estimated time remaining: 1:23:45",
  "updated_at": 1717584000.123
}
```

字段含义：

| 字段 | 类型 | 说明 |
|------|------|------|
| `config_name` | int | 构型编号 |
| `current_iter` | int \| null | 当前迭代步；无法解析时可为 `null` |
| `total_iter` | int | 本次 `--iterate-count` |
| `remaining_sec` | float | Fluent 报告的剩余秒数 |
| `raw_line` | str | 触发更新的原始 transcript 行 |
| `updated_at` | float | 写入时间戳，`time.time()` |

写入策略：

- 先写 `{progress_file}.tmp`，再用 `os.replace()` 原子替换。
- progress 文件为可选能力；没有传入 `--progress-file` 时脚本行为与现在一致。
- `finally` 中尽量删除 progress 文件，避免 stale 显示。

### 4.2 transcript 解析策略

先支持常见写法：

```text
estimated time remaining: 1:23:45
remaining time: 1:23:45
time remaining: 1:23:45
```

解析规则：

- 支持 `H:MM:SS` 和 `MM:SS`。
- 若同一行能解析出 iteration 编号，则写入 `current_iter`；否则 `current_iter = null`。
- 格式不匹配时忽略该行，不写 progress，不抛异常。
- 真实 Fluent 输出样例应作为实施时首个验证项；如果样例不同，只扩展解析器，不改变数据链路。

### 4.3 StateManager

复用现有 `engine_state` 表，新增 key：

```text
solver_progress
```

新增方法：

```python
def set_solver_progress(self, progress: dict[str, object]) -> None: ...
def get_solver_progress(self) -> dict[str, object] | None: ...
def clear_solver_progress(self) -> None: ...
```

行为：

- `set_solver_progress()` 存储 JSON 字符串。
- `get_solver_progress()` 在 key 不存在、为空或 JSON 损坏时返回 `None`。
- `clear_solver_progress()` 删除或清空该 key。
- `reset_all()`、`reset_config_steps(..., from_step="solver")`、solver 完成/失败/超时路径都应清理相关 progress。

### 4.4 IPC payload

`get_engine_status` 的 `data` 增加：

```json
{
  "solver_progress": {
    "config_name": 5,
    "current_iter": 350,
    "total_iter": 1000,
    "remaining_sec": 5025.0,
    "updated_at": 1717584000.123
  }
}
```

无活跃 solver progress 时：

```json
{
  "solver_progress": null
}
```

`get_dashboard` 不新增字段，只让已有 `engine` 对象自然包含 `solver_progress`。

### 4.5 Rust TUI 状态与显示

新增结构：

```rust
#[derive(Debug, Clone, Default)]
pub struct SolverProgress {
    pub config_name: u64,
    pub current_iter: Option<u64>,
    pub total_iter: u64,
    pub remaining_sec: f64,
    pub updated_at: Option<f64>,
}
```

`EngineInfo` 新增：

```rust
pub solver_progress: Option<SolverProgress>
```

显示规则：

- `AppState::update_engine_info()` 解析 `solver_progress`。
- 新增 helper，例如：

```rust
pub fn step_cell_text(&self, config: &str, step: &str) -> String
pub fn step_cell_color(&self, config: &str, step: &str) -> ratatui::style::Color
```

- 当 `step == "solver"`、状态为 `Running`、`solver_progress.config_name` 等于当前构型、`remaining_sec >= 0` 时：
  - 文本为 `⏳ HH:MM:SS`
  - 颜色为 `theme.success` 或等价绿色
- 其他情况保持当前 `status_icon(status) + status` 和 `status_color(status)`。

---

## 5. 实施计划

### Phase 1: 远程脚本 progress 生成

修改 `executor/remote_scripts/batch_solver_gen4.py`：

1. 添加 `--progress-file` 可选参数。
2. 添加纯函数：
   - `_parse_remaining_time_line(line: str, total_iter: int) -> dict[str, object] | None`
   - `_format/parse` 辅助函数用于时间转换。
3. 添加 `_write_progress_file(progress_file: str, progress: dict[str, object])`，使用 `.tmp` + `os.replace()`。
4. 在 `iterate()` 前注册 transcript callback，在 `finally` 注销 callback。
5. 在脚本结束和异常清理路径中删除 progress 文件。

### Phase 2: 远程读取与 daemon 状态

修改 `utils/ssh_client.py`：

1. 增加 `read_remote_text_file(remote_path: str, timeout: float | None = None) -> str | None`。
2. 使用 SFTP `open(..., "rb")` 读取小文本，遵循现有连接与通道 timeout 风格。

修改 `executor/remote_executor.py`：

1. 增加 `_solver_progress_file(config_name, remote_config=None)`。
2. `_build_solver_command()` 追加 `--progress-file` 参数。
3. `wait_solver_completion()` 每次 poll 时读取 progress JSON。
4. 解析成功后调用 `state.set_solver_progress()`。
5. 完成、error flag、输出文件校验失败、stop、timeout 时调用 `state.clear_solver_progress()` 并尽量删除远程 progress 文件。

修改 `engine/state_manager.py`：

1. 增加 `set_solver_progress()`、`get_solver_progress()`、`clear_solver_progress()`。
2. reset solver 或 reset all 时清理 progress。

### Phase 3: IPC 与 dashboard

修改 `engine/daemon.py`：

1. `handle_get_engine_status()` 增加 `solver_progress`。
2. 保持 `handle_get_dashboard()` 结构不变，让 `engine` payload 自动携带 progress。

### Phase 4: TUI 表格显示

修改 `autofluid-tui/src/state/app_state.rs`：

1. 增加 `SolverProgress`。
2. `EngineInfo` 增加 `solver_progress`。
3. `update_engine_info()` 解析该字段。
4. 增加 `format_hh_mm_ss()` 和 solver 单元格显示 helper。

修改 `autofluid-tui/src/ui/table.rs`：

1. 使用 AppState helper 获取单元格文本。
2. 对匹配到 solver progress 的单元格使用绿色。
3. 不修改顶部 `header.rs` / `info_bar` 展示逻辑。

---

## 6. 关键文件清单

| 文件 | 修改内容 |
|------|----------|
| `executor/remote_scripts/batch_solver_gen4.py` | transcript callback、剩余时间解析、progress JSON 写入 |
| `executor/remote_executor.py` | 构建 `--progress-file`、轮询读取 progress、清理 stale progress |
| `utils/ssh_client.py` | 新增公共远程文本读取方法 |
| `engine/state_manager.py` | 存储、读取、清理 `solver_progress` |
| `engine/daemon.py` | `get_engine_status` / dashboard engine payload 扩展 |
| `autofluid-tui/src/state/app_state.rs` | Rust progress 状态、解析、时间格式化和单元格 helper |
| `autofluid-tui/src/ui/table.rs` | solver 单元格文本与绿色显示 |
| `tests/test_batch_solver_script.py` | 远程脚本解析、callback、progress 文件测试 |
| `tests/test_remote_executor.py` / `tests/test_remote_executor_full.py` | solver command、progress 读取与清理测试 |
| `tests/test_state_manager.py` | progress 状态 CRUD 测试 |
| `tests/test_daemon_dashboard.py` | dashboard engine payload 测试 |

---

## 7. 验证方案

### 7.1 Python 单元测试

```powershell
.venv\Scripts\python.exe -m pytest tests/test_batch_solver_script.py -v
.venv\Scripts\python.exe -m pytest tests/test_remote_executor.py tests/test_remote_executor_full.py -v
.venv\Scripts\python.exe -m pytest tests/test_state_manager.py tests/test_daemon_dashboard.py -v
```

覆盖场景：

- `--progress-file` 可选，不传时行为兼容。
- transcript 样例行可解析为 `remaining_sec`。
- progress 文件原子写入且 JSON 字段完整。
- callback 注册后在 `finally` 中注销。
- solver command 包含 `--progress-file`。
- daemon 读取 progress 后写入 state。
- solver 完成、失败、超时、停止后清理 progress。
- `get_dashboard()["engine"]["solver_progress"]` 正确透传。

### 7.2 Rust 单元测试

在 `autofluid-tui/` 下运行：

```powershell
cargo test
```

覆盖场景：

- `update_engine_info()` 解析 `solver_progress`。
- `remaining_sec` 格式化为 `HH:MM:SS`。
- 只有匹配构型的 solver `Running` 单元格替换为剩余时间。
- 非 solver 步骤、非 Running 状态、构型不匹配、无 progress 时保持原显示。

### 7.3 质量门禁

Python：

```powershell
.venv\Scripts\python.exe -m ruff check .
.venv\Scripts\python.exe -m mypy .
.venv\Scripts\python.exe -m pytest tests/
```

Rust：

```powershell
cd autofluid-tui
cargo fmt --check
cargo check
cargo clippy -- -D warnings
cargo test
```

### 7.4 手动 / E2E 验证

1. 使用短迭代数 fixture，例如临时将 `solver_iteration_count` 调整为 `25`。
2. 启动一次完整 solver 流程。
3. Fluent 开始迭代后确认对应构型的 solver 单元格显示 `⏳ HH:MM:SS`。
4. 确认该单元格为绿色。
5. solver 完成后确认单元格进入 `Completed`，不残留旧剩余时间。
6. 恢复真实 `solver_iteration_count` 配置。

---

## 8. 风险与默认处理

| 风险 | 默认处理 |
|------|----------|
| Fluent transcript 实际格式与预期不同 | 先不显示剩余时间，不影响求解；用真实样例扩展解析器 |
| progress JSON 正在写入时被读取 | 远程脚本原子替换；daemon 读到损坏 JSON 时忽略本轮 |
| solver 异常退出导致 stale progress | daemon 完成/失败/超时/停止路径清理 state，并尽量删除远程 progress 文件 |
| 多工作站或多个 solver 并发 | progress 带 `config_name`；TUI 只替换匹配构型单元格 |
| 当前工作区已有未提交改动 | 实施前审阅相关 diff，增量合并，不回滚用户改动 |

---

## 9. 已确认与待实测项

已确认：

- 展示位置为 TUI 表格中的 solver 步骤单元格。
- 不修改顶部 `info_bar` 的引擎状态。
- 剩余时间显示为绿色。
- 无 progress 时保持当前 `Running` 显示。

待实测：

- 真实 Fluent transcript 中剩余时间的精确文本格式。
- PyFluent 当前版本的 `solver_session.transcript.register_callback()` 参数行为。

---

## 附录：降级备选

如果 transcript callback 在当前 PyFluent / Fluent 版本不可用，可以降级为 report 文件估算：

1. 读取 Fluent report file monitor 输出行数。
2. 根据已完成迭代数和 elapsed time 估算剩余时间。
3. 写入相同 `solver_progress_{config}.json`。
4. daemon、IPC、TUI 不需要改变。

该方案精度低于 Fluent 自身预估，仅作为 fallback。
