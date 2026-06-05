# TUI 显示 Fluent 仿真剩余时间 — 设计文档

> 文档版本：v1.0  
> 创建日期：2026-06-05  
> 目标：在 TUI info_bar 显示当前构型的 Fluent 仿真剩余时间

---

## 目录

1. [需求概述](#1-需求概述)
2. [现状分析](#2-现状分析)
3. [方案选型](#3-方案选型)
4. [架构设计](#4-架构设计)
5. [数据流与协议](#5-数据流与协议)
6. [实施计划](#6-实施计划)
7. [关键文件清单](#7-关键文件清单)
8. [验证方案](#8-验证方案)
9. [待确认事项](#9-待确认事项)

---

## 1. 需求概述

在 TUI 界面顶部 info_bar 区域，"当前时间"一栏左侧显示**当前构型仿真剩余时间**。

**显示效果**：
```
  引擎: 运行中  │  构型数: 5  │  屏障: 已通过  │  ⏳ 01:23:45
```

仅当有构型正在 solver 阶段且进度数据可用时显示 `│ ⏳ HH:MM:SS`，否则保持 info_bar 原样。

---

## 2. 现状分析

### 2.1 当前数据流

```
远程工作站 (Windows)
  └─ batch_solver_gen4.py
       └─ pyfluent.launch_fluent() → solver_session
            └─ solver_session.tui.solve.iterate(1000)  ← 阻塞调用
                 └─ Fluent 控制台输出迭代信息（含剩余时间）→ 无人捕获

本地 PC
  └─ remote_executor.wait_solver_completion()
       └─ 每 30s 轮询标志文件（仅检查完成/未完成）

  └─ IPC get_engine_status → {engine_status, sw_macro_started, barrier_passed, pipeline_started}
       └─ 无进度信息

  └─ TUI info_bar → "引擎: 运行中 │ 构型数: 5 │ 屏障: 已通过"
       └─ 无剩余时间
```

### 2.2 Fluent 迭代输出

Fluent 在每次迭代时向控制台输出信息，其中包含**Fluent 自身预估的剩余仿真时间**。该输出由 PyFluent 的 gRPC transcript 通道实时传输到本地 Python 进程。

### 2.3 PyFluent Transcript 机制

`solver_session.transcript` 在会话创建时已自动启动（`start_transcript=True` 默认）。

**核心 API**（源码：`ansys/fluent/core/streaming_services/transcript_streaming.py`）：

| 方法 | 说明 |
|------|------|
| `register_callback(fn, **kwargs)` | 注册回调，每行 Fluent 输出触发一次，返回 `callback_id` |
| `unregister_callback(callback_id)` | 注销回调 |
| `start()` / `stop()` | 控制流式传输（默认已启动） |

- 回调在 gRPC 后台线程中调用，与 `iterate()` 主线程**并行执行**
- 回调接收参数为 Fluent 输出文本行（默认去尾换行符）
- `keep_new_lines=True` 时保留换行符

**继承关系**：`Transcript` → `StreamingService`（`streaming.py`）

`StreamingService.register_callback()` 实现：
```python
def register_callback(self, callback: Callable, *args, **kwargs) -> str:
    with self._lock:
        callback_id = f"{next(self._service_callback_id)}"
        self._service_callbacks[callback_id] = [callback, args, kwargs]
        return callback_id
```

---

## 3. 方案选型

### 3.1 候选方案对比

| 方案 | 数据来源 | 优点 | 缺点 |
|------|---------|------|------|
| **A. Transcript 回调** | Fluent 控制台输出 | 实时、直接获取 Fluent 预估时间、无需额外文件 | 需确认输出格式 |
| B. Report 文件行数 | `.set` 中配置的 report file monitor | 已有配置、无需改 Fluent 设置 | 需自行计算剩余时间、精度低 |
| C. PyFluent Solution Monitor | `solver_session.solution.monitor` | Pythonic API | 仅含残差数据，无剩余时间 |

### 3.2 结论

**选择方案 A（Transcript 回调）**。

理由：
1. **直接获取 Fluent 预估时间**，无需自行计算
2. **实时性好**：回调在 gRPC 线程中触发，与 iterate() 并行
3. **改动最小**：仅需注册/注销回调，不改变 Fluent 配置或迭代流程
4. **report 文件方案作为降级备选**：若 transcript 不可用，可回退到方案 B

---

## 4. 架构设计

### 4.1 整体架构

```
┌─────────────────────────────────────────────────────────────────────┐
│                        远程工作站 (Windows)                          │
│                                                                     │
│  batch_solver_gen4.py                                               │
│  ┌───────────────────────────────────────────────────────────────┐  │
│  │  solver_session = pyfluent.launch_fluent(...)                 │  │
│  │                                                               │  │
│  │  # 注册 transcript 回调                                       │  │
│  │  callback_id = solver_session.transcript                      │  │
│  │      .register_callback(_on_transcript_line)                  │  │
│  │                                                               │  │
│  │  solver_session.tui.solve.iterate(1000)  ← 阻塞              │  │
│  │       │                                                       │  │
│  │       │  gRPC transcript 流                                   │  │
│  │       ▼                                                       │  │
│  │  _on_transcript_line(text)                                    │  │
│  │       │  解析迭代行 → 提取剩余时间                             │  │
│  │       ▼                                                       │  │
│  │  写入 progress_file (JSON) ─────────────────────────────────┐ │  │
│  │                                                               │  │
│  │  solver_session.transcript                                    │  │
│  │      .unregister_callback(callback_id)                        │  │
│  └───────────────────────────────────────────────────────────────┘  │
│                                                                     │
│  flag_dir/solver_progress_{config_id}.json                          │
└─────────────────────────────────────────────────────────────────────┘
                                    │
                                    │ SSH SFTP 读取（30s 间隔）
                                    ▼
┌─────────────────────────────────────────────────────────────────────┐
│                          本地 PC                                     │
│                                                                     │
│  remote_executor.wait_solver_completion()                           │
│       │  读取 solver_progress_{config_id}.json                      │
│       ▼                                                             │
│  state_manager.set_solver_progress()                                │
│       │  存入 engine_state 表                                       │
│       ▼                                                             │
│  daemon.handle_get_engine_status()                                  │
│       │  响应中附加 solver_progress 字段                             │
│       ▼                                                             │
│  IPC get_engine_status ─────────────────────────────────────────┐   │
│                                                                 │   │
│  TUI                                                            ▼   │
│  ┌───────────────────────────────────────────────────────────────┐  │
│  │  AppState.update_engine_info()                                │  │
│  │       │  解析 solver_progress → SolverProgress                │  │
│  │       ▼                                                       │  │
│  │  info_bar_text()                                              │  │
│  │       │  追加 "│ ⏳ HH:MM:SS"                                 │  │
│  │       ▼                                                       │  │
│  │  render_info_bar() → 显示在 info_bar 区域                     │  │
│  └───────────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────────┘
```

### 4.2 组件职责

| 组件 | 职责 | 修改类型 |
|------|------|---------|
| `batch_solver_gen4.py` | 注册 transcript 回调，解析输出，写入进度文件 | **新增** |
| `_build_solver_command()` | 传递 `--progress-file` 参数 | **修改** |
| `wait_solver_completion()` | 轮询时同步读取进度文件 | **修改** |
| `StateManager` | 存储/查询 solver progress | **新增方法** |
| `handle_get_engine_status()` | 响应中附加 progress 数据 | **修改** |
| `EngineInfo` / `SolverProgress` | Rust 侧数据结构 | **新增** |
| `update_engine_info()` | 解析 progress 字段 | **修改** |
| `info_bar_text()` | 追加剩余时间显示 | **修改** |

---

## 5. 数据流与协议

### 5.1 远程进度文件格式

路径：`{flag_dir}/solver_progress_{config_id}.json`

```json
{
  "current_iter": 350,
  "total_iter": 1000,
  "remaining_sec": 5025.0,
  "raw_line": "  iter  350  ...  estimated time remaining: 1:23:45",
  "ts": 1717584000.123
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `current_iter` | int | 当前迭代数 |
| `total_iter` | int | 总迭代数 |
| `remaining_sec` | float | Fluent 预估剩余秒数 |
| `raw_line` | str | 原始 Fluent 输出行（便于调试） |
| `ts` | float | 写入时间戳（`time.time()`） |

> **注**：字段结构为初始设计，具体字段需根据 Fluent 实际输出格式调整。

### 5.2 StateManager 存储

在 `engine_state` 表中新增键：

```sql
INSERT OR REPLACE INTO engine_state (key, value) VALUES ('solver_progress', '{"config_name": 5, "current_iter": 350, "total_iter": 1000, "remaining_sec": 5025.0}')
```

### 5.3 IPC 响应扩展

`get_engine_status` 响应新增 `solver_progress` 字段：

```json
{
  "status": "ok",
  "data": {
    "engine_status": "running",
    "sw_macro_started": true,
    "barrier_passed": true,
    "pipeline_started": true,
    "solver_progress": {
      "config_name": 5,
      "current_iter": 350,
      "total_iter": 1000,
      "remaining_sec": 5025.0
    }
  }
}
```

当无活跃仿真时，`solver_progress` 为 `null`。

### 5.4 Rust 侧数据结构

```rust
#[derive(Debug, Clone, Default)]
pub struct SolverProgress {
    pub config_name: u64,
    pub current_iter: u64,
    pub total_iter: u64,
    pub remaining_sec: f64,
}

// EngineInfo 新增字段
pub struct EngineInfo {
    pub engine_status: String,
    pub sw_macro_started: bool,
    pub barrier_passed: bool,
    pub pipeline_started: bool,
    pub solver_progress: Option<SolverProgress>,  // 新增
}
```

---

## 6. 实施计划

### Phase 1: 远程端进度追踪（Python）

**文件**：`executor/remote_scripts/batch_solver_gen4.py`

1. 添加 `--progress-file` 可选参数
2. 定义 transcript 回调函数 `_on_transcript_line(text)`：
   - 记录原始行到日志（调试用）
   - 正则匹配 Fluent 迭代输出中的剩余时间（**具体正则待输出样例确认**）
   - 匹配成功后写入 progress_file（JSON，原子写入避免读取不完整）
3. 在 `iterate()` 前注册回调，`iterate()` 后注销回调
4. 迭代完成后删除进度文件

**文件**：`executor/remote_executor.py` — `_build_solver_command()`

5. 命令中追加 `--progress-file "{flag_dir}/solver_progress_{config_id}.json"`

### Phase 2: Daemon 侧进度读取（Python）

**文件**：`executor/remote_executor.py` — `wait_solver_completion()`

1. 在 30s 轮询循环中，用 SFTP 读取进度文件
2. 解析 JSON，调用 `state_manager.set_solver_progress()`
3. 每次轮询都更新（30s 刷新间隔）

**文件**：`engine/state_manager.py`

4. 新增 `set_solver_progress(progress: dict)` 方法
5. 新增 `get_solver_progress() -> dict | None` 方法
6. 在 `engine_state` 表中存储/读取 `solver_progress` 键

### Phase 3: IPC 协议扩展

**文件**：`engine/daemon.py` — `handle_get_engine_status()`

1. 从 StateManager 读取 solver_progress
2. 附加到响应数据中

**文件**：`autofluid-tui/src/state/app_state.rs`

3. 新增 `SolverProgress` 结构体
4. `EngineInfo` 新增 `solver_progress: Option<SolverProgress>` 字段
5. `update_engine_info()` 解析 `solver_progress` 字段

### Phase 4: TUI 显示

**文件**：`autofluid-tui/src/state/app_state.rs` — `info_bar_text()`

1. 当 `solver_progress` 存在时，追加 `│ ⏳ HH:MM:SS`
2. 格式化秒数为 `HH:MM:SS`

**文件**：`autofluid-tui/src/ui/header.rs` — 无需修改

---

## 7. 关键文件清单

| 文件 | 修改内容 | 语言 |
|------|---------|------|
| `executor/remote_scripts/batch_solver_gen4.py` | +transcript 回调 + `--progress-file` 参数 | Python |
| `executor/remote_executor.py` | +传递 progress-file 参数 + 轮询读取进度 | Python |
| `engine/state_manager.py` | +`set_solver_progress()` / `get_solver_progress()` | Python |
| `engine/daemon.py` | +`handle_get_engine_status()` 附加 progress | Python |
| `autofluid-tui/src/state/app_state.rs` | +`SolverProgress` 结构体 + 解析 + `info_bar_text()` 追加显示 | Rust |
| `autofluid-tui/src/ui/header.rs` | 无需修改 | Rust |

---

## 8. 验证方案

### 8.1 单元验证

1. **远程脚本**：手动运行 `batch_solver_gen4.py --progress-file test.json`
   - 确认 transcript 回调被触发
   - 确认进度文件 JSON 格式正确
   - 确认 iterate() 返回后进度文件被清理

2. **StateManager**：单元测试 `set_solver_progress()` / `get_solver_progress()`

3. **IPC 协议**：运行 `pytest tests/test_ipc_protocol.py` 确认兼容

### 8.2 集成验证

4. **Daemon**：启动 daemon，调用 `get_engine_status` 确认响应包含 `solver_progress`

5. **TUI**：启动 TUI，确认 info_bar 显示 `│ ⏳ HH:MM:SS`

### 8.3 端到端验证

6. **完整流程**：运行一个构型的 solver 阶段
   - 确认剩余时间随迭代递减
   - 确认仿真完成后剩余时间消失

7. **边界情况**：
   - 仿真刚开始（无数据）→ 不显示
   - 仿真中途暂停 → 保持最后值
   - 仿真超时 → 进度文件被清理

### 8.4 质量门禁

```bash
# Python
.venv\Scripts\python.exe -m ruff check .
.venv\Scripts\python.exe -m mypy .
.venv\Scripts\python.exe -m pytest tests/

# Rust (from autofluid-tui/)
cargo check
cargo clippy -- -D warnings
cargo fmt --check
cargo test
```

---

## 9. 待确认事项

| # | 问题 | 影响 | 状态 |
|---|------|------|------|
| 1 | Fluent 迭代输出中剩余时间的具体格式 | 正则解析逻辑 | ⏳ 待用户提供输出样例 |
| 2 | `--progress-file` 参数是否为可选（降级到不显示） | 命令行参数设计 | 建议可选 |
| 3 | 多构型同时在 solver 阶段时的显示策略 | info_bar 展示 | 建议显示最近更新的构型 |
| 4 | 进度文件原子写入策略（避免读取到不完整 JSON） | 数据完整性 | 建议先写 .tmp 再 rename |

---

## 附录：方案 B 降级备选（Report 文件监控）

若 transcript 回调方案不可行（如 PyFluent 版本不支持或输出格式无法解析），可降级为 report 文件监控方案：

1. `.set` 文件已配置 report file monitor（`report-def-t-rfile.out` 等），每迭代写一行
2. 在 `batch_solver_gen4.py` 中启动后台线程，每 5s 读取 report 文件行数
3. 计算 `remaining = (total - current) × (elapsed / current)`
4. 写入进度文件

**缺点**：需自行计算剩余时间，精度低于 Fluent 自身预估。
