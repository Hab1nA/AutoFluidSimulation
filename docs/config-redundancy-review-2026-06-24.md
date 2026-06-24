# 配置冗余与默认值漂移审查报告

> 审查日期：2026-06-24  
> 审查范围：`autofluid_config.toml` ↔ `engine/config.py` ↔ `autofluid-tui/src/settings/mod.rs` 及全部相关源码  
> 审查目标：检查 TOML 已作为可编辑配置存在、但代码中仍写了默认/回退路径的冗余配置

---

## 问题概述

- **现象**：项目 `autofluid_config.toml` 已作为可编辑配置存在，但代码中广泛存在硬编码默认/回退路径，且部分默认值与 TOML 不一致，存在配置漂移风险。
- **影响范围**：Python Daemon（`engine/config.py`）、Rust TUI（`autofluid-tui/src/settings/`）、远程执行脚本（`executor/remote_scripts/`）——配置加载的整个生命周期。
- **问题域**：配置管理 / 运行时逻辑。

---

## 调查过程

### 已检查的资料

| 文件 | 发现 |
|------|------|
| `autofluid_config.toml` | 定义了 `[local_paths]`、`[remote_config]`、`[[workstations]]`、`[solidworks]`、`[spaceclaim]`、`[meshing]`、`[solver]`、`[postprocess]`、`[global_settings]`、`[step_file_patterns]` 等节 |
| `engine/config.py` | 使用 `_toml_or_default()` 从 TOML 读取，每项均有硬编码 fallback。部分默认值与 TOML **不一致** |
| `engine/sc_process_pool.py` | `MAX_SLOTS=1` 类常量，有两项 `ENGINE_CONFIG` 值（`sc_persistent_ready_timeout`、`sc_scdoc_stable_seconds`）未入 TOML |
| `executor/cleaner.py` | `_DEFAULT_POSTPROCESS_ANIMATION_DIR = r"D:\xkz_1020\animation"` 硬编码，用于回退判断逻辑 |
| `executor/remote_executor.py` | 同上常量重复定义；多层回退链重复出现 |
| `executor/remote_scripts/*.py` | argparse `default=` 包含硬编码数值（如 `--exit-to-throat-area-ratio=7.427276607`） |
| `utils/ssh_client.py` | `connect()` 中 `timeout=10` 硬编码，未读取 `OPERATION_TIMEOUTS["ssh_connection"]` |
| `utils/logger.py` | `_DEFAULT_LOG_MAX_BYTES`（20MB）/ `_DEFAULT_LOG_BACKUP_COUNT`（10）只支持环境变量覆盖，不支持 TOML |
| `autofluid-tui/src/settings/mod.rs` | Rust 端独立维护了一套 `Default` impl，部分值与 Python 端、TOML 均不一致 |
| `autofluid-tui/src/daemon_mgr.rs` | `9527` 端口硬编码、`$HOME/AutoFluidSimulation` 项目目录硬编码、多个超时常量不可通过 TOML 配置 |
| `engine/config_fingerprint.py` | 数据库分片路径依赖 `LOCAL_PATHS["data_dir"]`，无独立硬编码 |
| `engine/local_worker.py` | Worker 配置来自环境变量，`register_*` 间隔有合理默认值 |
| `tests/test_config.py` | 测试中使用的硬编码路径值与 `config.py` fallback 一致（未暴露漂移问题） |

### 关键发现

1. **路径默认值漂移**：`LOCAL_PATHS` 中 4 个路径的 fallback 指向旧目录 `000ansys_data`，而 TOML 已迁移到项目内 `data/` 目录
2. **超时默认值三方不一致**：TOML / Python / Rust 在 `meshing_timeout`、`solver_timeout`、`sc_gui_stable_delay` 上各持不同值
3. **硬编码常量重复定义**：`_DEFAULT_POSTPROCESS_ANIMATION_DIR` 在 `cleaner.py` 和 `remote_executor.py` 中各自定义
4. **SSH 连接超时忽略 TOML**：`connect()` 使用裸 `timeout=10`，不读取配置
5. **TUI 项目目录不可配置**：`SERVER_DAEMON_DEFAULT_PROJECT_DIR` 硬编码为 `$HOME/AutoFluidSimulation`

---

## 根因分析

### 问题 1: TOML 与 Python 默认值漂移（置信度：高 🔴）

#### 1.1 本地路径

| 配置项 | TOML 值 | `config.py` fallback | 差异 |
|--------|---------|---------------------|------|
| `sw_model` | `data/model.SLDPRT` | `000ansys_data/Graduation_Project(RE0.)/solidworks_models/model_gen4.SLDPRT` | **完全不同目录** |
| `excel` | `data/model.xlsx` | `000ansys_data/Graduation_Project(RE0.)/solidworks_models/model_gen4.xlsx` | **完全不同目录** |
| `step_dir` | `data/step` | `000ansys_data/Graduation_Project(RE0.)/solidworks_models/step` | **完全不同目录** |
| `scdoc_dir` | `data/scdoc` | `000ansys_data/Graduation_Project(RE0.)/solidworks_models/scdoc` | **完全不同目录** |

> **代码引用**：`engine/config.py:192-230`

#### 1.2 超时值

| 配置项 | TOML | `config.py` fallback | 差异 |
|--------|------|---------------------|------|
| `meshing_timeout` | `1800` | `600` | 3x |
| `solver_timeout` | `14400` | `7200` | 2x |
| `sc_gui_stable_delay` | `5` | `15` | 3x |

> **代码引用**：`engine/config.py:509-519`、`:465-470`

#### 1.3 远程路径

| 配置项 | TOML | `config.py` fallback |
|--------|------|---------------------|
| `scripts_dir` | `D:\xkz_1020\scripts` | `D:\xkz_1020`（缺少 `\scripts` 子目录） |

> **代码引用**：`engine/config.py:258`

**触发条件**：当 `autofluid_config.toml` 缺失或某字段未定义时，系统回退到与 TOML 预期不一致的值，导致路径错误、超时过短/过长等问题。

---

### 问题 2: `_DEFAULT_POSTPROCESS_ANIMATION_DIR` 硬编码常量漂移（置信度：高 🔴）

**证据链**：

- `executor/cleaner.py:34`：`_DEFAULT_POSTPROCESS_ANIMATION_DIR = r"D:\xkz_1020\animation"`
- `executor/remote_executor.py:36`：完全相同的常量重复定义
- 两处使用相同逻辑：比较 `ENGINE_CONFIG["postprocess_animation_dir"]` 是否等于该常量

```python
# executor/cleaner.py:77 和 executor/remote_executor.py:1216
elif engine_animation_dir == _DEFAULT_POSTPROCESS_ANIMATION_DIR and config.get("animation_dir", ""):
    animation_source = config.get("animation_dir", "")
```

**问题分析**：

此常量是 `ENGINE_CONFIG["postprocess_animation_dir"]` 默认值 `REMOTE_CONFIG["animation_dir"]`（即 `D:\xkz_1020\animation`）的**第三次拷贝**。它出现在：

1. TOML `[remote_config].animation_dir` → 配置文件
2. `ENGINE_CONFIG["postprocess_animation_dir"]` 的 fallback 链 → `config.py`
3. `_DEFAULT_POSTPROCESS_ANIMATION_DIR` → `cleaner.py` / `remote_executor.py`（硬编码）

如果用户修改了动画输出路径，这个三重冗余会导致回退判断逻辑不一致。

---

### 问题 3: Rust TUI 默认值与 Python/TOML 三向不一致（置信度：高 🔴）

| 配置项 | TOML | Python `config.py` | Rust `settings/mod.rs` | 状态 |
|--------|------|-------------------|----------------------|------|
| `meshing_timeout` | **1800** | 600 | 600 | Python/Rust 一致但与 TOML 不同 |
| `solver_timeout` | **14400** | 7200 | 7200 | Python/Rust 一致但与 TOML 不同 |
| `sc_gui_stable_delay` | **5** | 15 | **5** | Rust/TOML 一致但与 Python 不同 |

> **代码引用**：Rust — `autofluid-tui/src/settings/mod.rs:262-272`（`MeshingConfig::default()`）、`:276-285`（`SolverConfig::default()`）、`:237-246`（`SpaceClaimConfig::default()`）；Python — `engine/config.py:509-519`、`:465-470`

**风险**：Rust TUI 保存配置时使用自己的默认值，Python daemon 读取 TOML 时使用自己的 fallback。三方不同步时，TUI 编辑后保存的值可能被 Python 的 fallback 覆盖，或者相反。

---

### 问题 4: `sc_persistent_ready_timeout` 与 `sc_scdoc_stable_seconds` 不可通过 TOML 编辑（置信度：中 🟡）

**证据链**：

- `engine/config.py:551-553`：
  ```python
  "sc_persistent_ready_timeout": 180,      # 硬编码
  "sc_scdoc_stable_seconds": 3.0,          # 硬编码
  ```
- `engine/sc_process_pool.py:501,552,677`：使用 `ENGINE_CONFIG.get("sc_persistent_ready_timeout", 180)` 等访问
- TOML 的 `[spaceclaim]` 节中**没有**这两个字段
- Rust `SpaceClaimConfig` 中也没有这两个字段

**推理**：这两个超时值是 SpaceClaim 常驻模式的关键调优参数（等待 Bridge 就绪 / 等待 SCDOC 文件稳定），但无法通过配置文件调整。

---

### 问题 5: SSH `connect()` 忽略 TOML 配置的连接超时（置信度：高 🔴）

**证据链**：

- `autofluid_config.toml` → `[global_settings].ssh_connection = 10`
- `engine/config.py:475` → `OPERATION_TIMEOUTS["ssh_connection"] = _toml_or_default(...)`（正确读取 TOML）
- `utils/ssh_client.py:103`：
  ```python
  self._ssh.connect(
      hostname=self.host, port=self.port, username=self.username,
      password=password, key_filename=key_filename,
      timeout=10,  # ← 硬编码！未读取 OPERATION_TIMEOUTS
      ...
  )
  ```

> **代码引用**：`utils/ssh_client.py:103`

**推理**：TOML 中修改 `ssh_connection` 对 SSH `connect()` 的超时不起作用。

---

### 问题 6: 远程脚本 argparse 默认值与配置体系脱钩（置信度：中 🟡）

**证据链**：

`executor/remote_scripts/postprocess_metrics_gen4.py:25-26`：
```python
DEFAULT_EXIT_TO_THROAT_AREA_RATIO = 7.427276607
DEFAULT_CSTAR_REFERENCE = 1830.4
```

`executor/remote_scripts/batch_solver_gen4.py:73-76`：
```python
parser.add_argument('--processor-count', type=int, default=128)
parser.add_argument('--iterate-count', type=int, default=1000)
```

这些值与 TOML 中 `[postprocess]` 和 `[solver]` 的值虽然当前一致，但**独立维护**。Python daemon 构造远程命令时显式传入参数，但如果某次调用遗漏了参数，脚本会静默使用自己的硬编码默认值，不会报错。

---

### 问题 7: Rust TUI 端口和项目目录硬编码（置信度：中 🟡）

**证据链**：

- `autofluid-tui/src/daemon_mgr.rs:22`：
  ```rust
  const SERVER_DAEMON_DEFAULT_PROJECT_DIR: &str = "$HOME/AutoFluidSimulation";
  ```
- `autofluid-tui/src/daemon_mgr.rs:1219`：
  ```rust
  .unwrap_or_else(|| "9527".to_string())  // IPC 端口硬编码
  ```

Python 端 IPC 端口可通过 TOML `[ipc_config].port` + 环境变量 `AUTOFLUID_IPC_PORT` 配置，但 Rust 端不读取 TOML 中的 `ipc_config`，而是使用自己的环境变量 `AUTOFLUID_SERVER_TUNNEL_REMOTE_PORT`。

---

### 问题 8: Rust TUI 常量不可通过 TOML 配置（置信度：低 🟢）

以下常量在 `daemon_mgr.rs` 中硬编码，不可通过 TOML 调整：

| 常量 | 值 | 用途 |
|------|-----|------|
| `DAEMON_SHUTDOWN_TIMEOUT_SECS` | 60 | daemon 退出等待超时 |
| `IPC_RECONNECT_TIMEOUT_SECS` | 60 | IPC 重连超时 |
| `IPC_RECONNECT_INTERVAL` | 500ms | IPC 重连间隔 |
| `SERVER_DAEMON_START_TIMEOUT` | 150s | 服务器 daemon 启动超时 |
| `SERVER_DAEMON_STOP_TIMEOUT` | 10s | 服务器 daemon 停止超时 |

---

### 问题 9: 日志轮转参数不在 TOML 中（置信度：低 🟢）

`utils/logger.py:33-34`：
```python
_DEFAULT_LOG_MAX_BYTES = 20 * 1024 * 1024   # 20MB
_DEFAULT_LOG_BACKUP_COUNT = 10
```

只能通过环境变量 `AUTOFLUID_LOG_MAX_BYTES` / `AUTOFLUID_LOG_BACKUP_COUNT` 覆盖，TOML 中没有对应条目。

---

### 问题 10: 后处理路径多层回退链重复且不一致（置信度：低 🟢）

`cleaner.py` 和 `remote_executor.py` 中的多个方法包含复杂的多层回退链：

```python
# cleaner.py:86-92
metrics_dir = str(
    config.get("postprocess_metrics_dir")
    or ENGINE_CONFIG.get("postprocess_metrics_dir")
    or config.get("postprocess_output_dir")
    or ENGINE_CONFIG.get("postprocess_output_dir")
    or config.get("result_dir", "")
)
```

此逻辑在 `_postprocess_cleanup_paths`、`_postprocess_metrics_config_dir`、`_postprocess_output_config_dir`、`_postprocess_path_config` 等方法中重复出现，回退优先级可能在不同方法间存在细微差异。

---

## 修复方案

### 方案 A: 统一默认值源——以 TOML 为唯一真相来源 ⭐⭐⭐ 推荐

**描述**：将 `config.py` 中所有 `_toml_or_default()` 的 fallback 值改为与 `autofluid_config.toml` 完全一致。同时将 `_DEFAULT_POSTPROCESS_ANIMATION_DIR` 常量移除，改为从 `ENGINE_CONFIG` 读取。

**涉及文件**：

| 文件 | 改动说明 |
|------|---------|
| `engine/config.py` | 修改 `LOCAL_PATHS`（sw_model, excel, step_dir, scdoc_dir）、`REMOTE_CONFIG`（scripts_dir）、`ENGINE_CONFIG`（meshing_timeout, solver_timeout）、`OPERATION_TIMEOUTS`（sc_gui_stable_delay）的 fallback 值 |
| `executor/cleaner.py` | 移除 `_DEFAULT_POSTPROCESS_ANIMATION_DIR`，改为从 `ENGINE_CONFIG` 读取 |
| `executor/remote_executor.py` | 同上 |
| `autofluid-tui/src/settings/mod.rs` | 同步 Rust `Default` impl 与 TOML |

**改动量**：20-100 行  
**复杂度**：简单  
**副作用风险**：
- 如果存在依赖旧默认值的测试，需同步更新
- 不影响 TOML 已存在时的行为（TOML 优先级最高）

### 方案 B: 修复 SSH `connect()` 不读取配置的问题 ⭐⭐⭐ 推荐

**描述**：`RemoteWorkstation.connect()` 应接受显式 timeout 参数，或从 `OPERATION_TIMEOUTS["ssh_connection"]` 读取。

**涉及文件**：

| 文件 | 改动说明 |
|------|---------|
| `utils/ssh_client.py` | `connect()` 方法添加 timeout 参数或读取 OPERATION_TIMEOUTS |
| `engine/task_runner.py` | 调用方传入配置值 |

**改动量**：< 20 行  
**复杂度**：简单  
**副作用风险**：低，向后兼容

### 方案 C: 将缺失的配置项加入 TOML ⭐⭐ 可行

**描述**：将 `sc_persistent_ready_timeout`、`sc_scdoc_stable_seconds`、日志轮转参数加入 TOML，并同步 `reload_config_from_toml()` 的合并逻辑。

**涉及文件**：

| 文件 | 改动说明 |
|------|---------|
| `autofluid_config.toml` | `[spaceclaim]` 节新增 `sc_persistent_ready_timeout`、`sc_scdoc_stable_seconds`；`[global_settings]` 节新增日志轮转参数 |
| `engine/config.py` | `ENGINE_CONFIG` 中两处改为 `_toml_or_default()`；`reload_config_from_toml()` 合并新增字段 |
| `autofluid-tui/src/settings/mod.rs` | `SpaceClaimConfig` 添加对应字段 |

**改动量**：< 20 行 Python + < 10 行 Rust  
**复杂度**：简单  
**副作用风险**：低，纯增量变更

### 方案 D: Rust TUI 端口和项目目录支持 TOML 配置 ⭐⭐ 可行

**描述**：Rust 端 `default_server_start_command()` 中的端口回退值应从 TOML 的 `ipc_config.port` 读取，项目目录应支持从 TOML `global_settings` 读取。

**涉及文件**：

| 文件 | 改动说明 |
|------|---------|
| `autofluid-tui/src/daemon_mgr.rs` | `default_server_start_command()`、`default_server_project_dir()` |
| `autofluid-tui/src/settings/mod.rs` | 添加 `IpcConfig` 结构体及相关 `Default` impl |
| `autofluid_config.toml` | 新增 `[ipc_config]` 节（已有 `port` 字段的读取逻辑但 TOML 中未定义该节） |

**改动量**：20-100 行  
**复杂度**：中等（跨语言 IPC 端口契约需同步验证）  
**副作用风险**：需要确保 Python 端 `IPC_CONFIG["port"]` 变更时 Rust 端同步

### 方案 E: 远程脚本默认值外部化 ⭐ 可行（低优先级）

**描述**：将远程脚本 argparse 的硬编码默认值改为 `required=True`（无默认值），确保调用方必须显式传入。

**涉及文件**：

| 文件 | 改动说明 |
|------|---------|
| `executor/remote_scripts/postprocess_metrics_gen4.py` | `--exit-to-throat-area-ratio`、`--cstar-reference` 改为 required |
| `executor/remote_scripts/batch_solver_gen4.py` | `--processor-count`、`--iterate-count` 改为 required |

**改动量**：< 10 行  
**复杂度**：简单  
**副作用风险**：需确保所有调用方（`remote_executor.py` 等）已传入这些参数

---

## 影响级别汇总

| 优先级 | 问题 | 影响 |
|--------|------|------|
| 🔴 P0 | TOML vs config.py 路径默认值漂移（4 个路径） | TOML 丢失时系统使用完全错误的路径 |
| 🔴 P0 | TOML vs config.py 超时默认值漂移（3 个超时） | 可能使用错误的超时值 |
| 🔴 P0 | SSH connect() 忽略 TOML ssh_connection | 连接超时不可配置 |
| 🔴 P0 | `_DEFAULT_POSTPROCESS_ANIMATION_DIR` 硬编码重复 | 动画路径回退逻辑脆弱 |
| 🟡 P1 | Rust TUI 默认值与 Python/TOML 三方不一致 | TUI 保存的配置可能与 daemon 预期不同 |
| 🟡 P1 | `sc_persistent_ready_timeout` 等未入 TOML | SC 常驻调优参数无法配置 |
| 🟡 P1 | 远程脚本 argparse 默认值脱钩 | 遗漏参数时静默使用错误值 |
| 🟡 P1 | Rust TUI 端口硬编码 9527 | IPC 端口变更需同时改两处 |
| 🟢 P2 | Rust TUI 超时常量不可配置 | 调优需改源码 |
| 🟢 P2 | 日志轮转参数不在 TOML | 只能通过环境变量调整 |
| 🟢 P2 | 后处理路径回退链重复 | 维护负担，逻辑不一致风险 |

---

## 建议执行顺序

1. **立即执行方案 A**：统一 `config.py` fallback 与 TOML，消除路径和超时漂移——影响面最大、修复成本最低
2. **立即执行方案 B**：修复 SSH `connect()` 忽略 TOML 配置的问题
3. **短期执行方案 C**：将 `sc_persistent_ready_timeout` 等调优参数加入 TOML
4. **中期执行方案 D**：统一 Rust/Python 的 IPC 端口配置来源
5. **按需执行方案 E**：远程脚本默认值改为 required

### 验证步骤

- 运行 `pytest tests/test_config.py` 确保配置加载测试通过
- 验证 TOML 缺失时系统仍使用正确的 fallback 值
- 在 Rust 端运行 `cargo test` 确保 TUI 配置解析不变
- 手动测试：移除 TOML 文件 → 启动 daemon → 验证各路径指向正确位置
