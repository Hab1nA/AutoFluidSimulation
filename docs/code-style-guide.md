# AutoFluid 代码写作规范

> 本文档总结了本项目的代码命名和风格约定，适用于所有 Python 和 Rust 代码的编写与审查。

---

## 目录

1. [通用原则](#1-通用原则)
2. [Python 命名规范](#2-python-命名规范)
3. [Rust 命名规范](#3-rust-命名规范)
4. [配置体系规范](#4-配置体系规范)
5. [日志风格规范](#5-日志风格规范)
6. [方法结构与职责](#6-方法与结构职责)
7. [暂停/停止响应模式](#7-暂停停止响应模式)
8. [错误处理模式](#8-错误处理模式)

---

## 1. 通用原则

### 1.1 命名风格总览

| 类别 | 风格 | 示例 |
|------|------|------|
| 类名 | `PascalCase` | `TaskRunner`, `SCProcessPool`, `PipelineScheduler` |
| 函数/方法 | `snake_case` | `execute_sw_step()`, `_connect_sw()`, `run_config()` |
| 常量 | `UPPER_SNAKE_CASE` | `STATUS_COMPLETED`, `MAX_SLOTS`, `ENGINE_CONFIG` |
| 变量 | `descriptive_snake_case` | `all_configs`, `sw_status_dist`, `paused_sw` |
| 临时/循环变量 | 简短描述性 | `cn` (config_name), `s` (step_name), `fn` (filename) |
| 私有方法前缀 | `_snake_case` | `_connect_sw()`, `_rebuild_and_export_per_config()` |
| 模块级常量 | `UPPER_SNAKE_CASE` | `LOCAL_PATHS`, `OPERATION_TIMEOUTS` |

### 1.2 基本原则

- **描述性优先**：避免单字母缩写（`a`, `b`, `x`），使用完整有意义的名称
- **避免下划线前缀滥用**：仅对真正的私有方法使用 `_` 前缀
- **英语命名**：所有标识符使用英文，注释和用户界面文本使用中文
- **动词前置**：方法名以动词开头（`execute_`, `connect_`, `validate_`）

---

## 2. Python 命名规范

### 2.1 步骤方法命名模式

所有步骤执行方法遵循统一命名模式：

```
execute_{步骤名}_step()
```

| 当前方法 | 步骤 |
|----------|------|
| `execute_sw_step()` | SolidWorks STEP 导出 |
| `execute_sc_step()` | SpaceClaim 转换 |
| `execute_transfer()` | 文件传输 |
| `execute_meshing()` | 网格划分 |
| `execute_solver()` | 仿真求解 |

**注意**：使用 `_step` 后缀而非 `_macro` 或其他历史名称。

### 2.2 私有辅助方法

为每个步骤实现的方法使用 `_` 前缀 + 动词 + 步骤标识：

```python
# SW 步骤辅助方法
_connect_sw()              # 三层降级连接 SolidWorks
_open_sw_model()           # OpenDoc6 打开模型
_rebuild_and_export_per_config()  # 逐构型重建并导出 STEP
_verify_step_exports()     # 安全网校验输出文件
_disconnect_sw()           # 清理 SW 资源
_terminate_sw_processes()  # 强制终止 SW 进程
_launch_sw_process()       # 通过 subprocess 启动 SW
_validate_design_table()   # 验证 Excel 设计表格式

# SC 步骤辅助方法
_terminate_bridge_and_sc()  # 终止 Bridge 及关联 SC 进程
_build_command()            # 构建启动命令
```

### 2.3 调度器方法命名

```python
# 调度器核心
start_pipeline()              # 启动/继续流水线
pause()                       # 暂停流水线
resume()                      # 恢复流水线
stop()                        # 停止流水线
reset_config()                # 重置指定构型步骤

# 内部方法
_prepare_sw_retry()           # SW 重试准备（taskkill + 监控器重置）
_resume_paused_steps()        # 恢复暂停的步骤（SW + SC）
scan_completed_downstream()   # 同步下游步骤状态与文件系统（SWPhaseHandler）
execute_with_retry()          # 通用重试包装器（RetryManager）
monitor_loop()                # 全局屏障监控线程（BarrierCoordinator）
_dispatch_solver_tasks()      # 屏障通过后分发 Solver（BarrierCoordinator）
pause_aware_sleep()           # 可响应暂停/停止的 sleep（独立函数）
```

### 2.4 变量命名规则

**好** vs **差** 的变量名对比：

```python
# ✓ 好: 描述性名称
all_configs = self.state.get_all_configs()
sw_status_dist: dict[str, list[int]] = {}
should_run_sw = not self.state.is_sw_macro_started()
sw_completed = [cn for cn in all_configs if ...]
sw_success = self.runner.execute_sw_step()
total_found = len(already_completed) + len(found_configs)

# ✗ 差: 缩写/无意义名称
_cfgs = self.state.get_all_configs()  # 不明所以
_d = {}  # 用途不明
_flag = True  # 什么标志？
tmp1, tmp2, x, y  # 无意义
```

临时循环变量可以使用短名（`cn`, `s`, `st`），但仅在局部可见范围内：

```python
for cn in all_configs:           # cn = config_name, 合理
    st = self.state.get_step_status(cn, "sw")  # st = status, 合理
for s in ["sc", "transfer"]:     # s = step_name, 合理
```

### 2.5 布尔变量命名

使用肯定的谓语形式：

- `should_run_sw` ✓（而非 `dont_skip_sw`）
- `all_done` ✓（而非 `not_incomplete`）
- `is_dirty` ✓（而非 `not_clean`）
- `sw_success` ✓（而非 `sw_not_failed`）

---

## 3. Rust 命名规范

### 3.1 结构体与字段

遵循 Rust 标准命名约定：

```rust
// 结构体 PascalCase
pub struct SettingsConfig { ... }
pub struct EngineConfig { ... }
pub struct OperationTimeouts { ... }

// 字段 snake_case
pub struct EngineConfig {
    pub watchdog_interval: f64,
    pub sw_macro_timeout: u64,
    pub sw_close_doc_on_finish: bool,
    pub sc_timeout: u64,
    pub transfer_timeout: u64,
    pub meshing_timeout: u64,
    pub solver_timeout: u64,
    pub max_retries: u32,
    pub state_refresh_interval: f64,
}
```

### 3.2 枚举与匹配

```rust
pub enum SettingCategory {
    LocalPaths,
    RemoteConnection,
    RemoteDirs,
    StepPatterns,
    SolidWorks,
    SpaceClaim,
    Meshing,
    Solver,
    GlobalSettings,
}

impl SettingCategory {
    pub fn display_label(self, idx: usize) -> &'static str {
        match self {
            SettingCategory::SolidWorks => match idx {
                0 => "SW宏超时(秒)",
                1 => "完成后关闭文档",
                // ...
                _ => "",
            },
            // ...
        }
    }
}
```

### 3.3 配置字段名称命名

- 使用 `snake_case`，与 Python 端 `dict` 键名**完全一致**
- 单位标注在字段名或显示标签中明确标明：`sw_macro_timeout`（配置键）→ `"SW宏超时(秒)"`（显示标签）

---

## 4. 配置体系规范

### 4.1 配置分层

```
autofluid_config.toml  ← 用户可编辑的 TOML 配置文件
    ↕ TUI 可通过 Settings 页面读写
     ↕ Python reload_config_from_toml() 读取
        ↓
LOCAL_PATHS         dict  ← [local_paths] 节
REMOTE_CONFIG       dict  ← [remote_config] 节
STEP_FILE_PATTERNS  dict  ← [step_file_patterns] 节
ENGINE_CONFIG       dict  ← [solidworks] + [spaceclaim] + [meshing] + [solver] + [global_settings] 节合并
OPERATION_TIMEOUTS  dict  ← [solidworks] + [spaceclaim] + [global_settings] 节合并
```

> **向后兼容**：Python 端也支持旧格式 `[engine_config]` / `[operation_timeouts]` 顶级节，
> 但默认 TOML 文件和 Rust TUI Settings 使用细分节名。

### 4.2 数据流

```
Rust TUI Settings 页面 → 写入 autofluid_config.toml
                                              ↓
                                    Python reload_config_from_toml()
                                              ↓
                              ENGINE_CONFIG.update(toml_data["solidworks"])
                              ENGINE_CONFIG.update(toml_data["spaceclaim"])
                              ENGINE_CONFIG.update(toml_data["meshing"])
                              ENGINE_CONFIG.update(toml_data["solver"])
                              ENGINE_CONFIG.update(toml_data["global_settings"])
                              OPERATION_TIMEOUTS.update(toml_data["solidworks"])
                              ...
```

### 4.3 常量来源优先级

1. 环境变量（最高优先级，用于部署/CI）
2. `autofluid_config.toml`（中等优先级，用户通过 TUI 设置）
3. `engine/config.py` 中的硬编码默认值（最低优先级）

---

## 5. 日志风格规范

### 5.1 日志前缀格式

所有日志消息使用 `[模块/子模块]` 英文标签前缀：

| 代码位置 | 前缀 | 示例 |
|----------|------|------|
| SW 步骤通用 | `[SW]` | `logger.info("[SW] 正在连接 SolidWorks...")` |
| SW COM 操作 | `[SW-COM]` | `logger.info("[SW-COM] OpenDoc6: Errors=0")` |
| SW 设计表 | `[SW-DesignTable]` | `logger.info("[SW-DesignTable] 正在验证 Excel...")` |
| SW COM 参数 | `[SW-COM-Param]` | `logger.info("[SW-COM-Param] 匹配参数: ...")` |
| SW 清理 | `[SW-Cleanup]` | `logger.info("[SW-Cleanup] 已关闭模型文档")` |
| SC 进程池 | `[SC-Pool]` | `logger.info("[SC-Pool] 槽位1 分配给构型6")` |
| SC Bridge | `[SC-Bridge]` | 由 SC-Pool 使用 |
| 调度器通用 | `[SW]`, `[Resume]`, `[Prescan]` | `logger.info("[SW] SW 阶段完成: ...")` |

### 5.2 日志级别约定

```python
logger.error()    # 可恢复或不可恢复的错误，用户需要关注
logger.warning()  # 不影响主流程但值得注意的异常情况
logger.info()     # 正常的进度/状态信息（用户可见）
logger.debug()    # 调试细节（默认不显示）
```

### 5.3 日志消息规范

- **中文消息**：日志正文使用中文，标签使用英文
- 例：`"[SW] 正在打开模型 (OpenDoc6): model_gen4.SLDPRT"`
- 避免中英文混写标签：使用 `[SW-COM]` 而非 `[SW-COM操作]`

---

## 6. 方法与结构职责

### 6.1 方法拆分原则

- **一个方法一个职责**：每个方法只做一件事
- **入口薄包装**：入口方法只做参数校验和流程编排，具体实现委托给子方法
- **finally 职责归属**：资源清理方法（如 `_disconnect_sw()`）独立提取，并在入口方法的 `try/finally` 中调用

```python
def execute_sw_step(self) -> bool:
    """薄入口：校验参数 → 编排子步骤 → 返回结果"""
    # 1. 校验
    # 2. try: CoInitialize()
    #       try: _connect_sw() → _open_sw_model() → ... → return
    #       finally: _disconnect_sw()
    #    except: ...
    
def _connect_sw(self):
    """单一职责：三层降级连接 SW，返回 sw_app 或 None"""

def _disconnect_sw(self, sw_app, doc, sw_model):
    """单一职责：关闭文档+退出 SW+释放 COM，静默处理 None"""
```

### 6.2 方法规格

- 所有方法应有文档字符串（docstring），说明：
  - 方法职责
  - 参数说明（Args:）
  - 返回值说明（Returns:）
- 私有方法的 docstring 可以比公有方法更简洁

### 6.3 步骤命名对称性

SW 和 SC 步骤遵循对称的方法结构：

```python
# SW 步骤
execute_sw_step()           # 入口
    _connect_sw()           # 连接
    _open_sw_model()        # 打开模型
    _rebuild_and_export_per_config()  # 逐构型重建并导出
    _verify_step_exports()  # 校验
    _disconnect_sw()        # 清理

# SC 步骤
execute_sc_step()           # 入口 (委托 SCProcessPool)
    SCProcessPool.run_config()     # 进程池管理
        _execute_in_slot()         # 执行
        _terminate_bridge_and_sc() # 清理
```

---

## 7. 暂停/停止响应模式

### 7.1 可中断 vs 不可中断

| 步骤 | 可中断 | 响应机制 |
|------|--------|----------|
| SW (COM 调用) | ❌ | 仅检查标志位，COM 同步调用结束后响应 |
| SC (子进程) | ✅ | 轮询循环中检测 `paused_event`，立即 `kill()` |
| Transfer (SSH) | ✅ | 依赖 SSH 操作完成时机 |
| Meshing/Solver (远程) | ✅ | 轮询标志文件间隔中检查标志 |

### 7.2 统一暂停检查模式

```python
# 在轮询循环或长时间操作中使用
while self._paused.is_set() and not self._stopped.is_set():
    time.sleep(1)
if self._stopped.is_set():
    return  # 或 break/continue
```

### 7.3 暂停恢复策略

- **SW 步骤**：检查 STEP 文件 → 存在则 Completed，缺失则 Error + 清除 `sw_macro_started`
- **SC 步骤**：检查 SCDOC 文件 → 存在则 Completed，缺失+STEP存在则重新入队
- **其他步骤**：通过 `set_all_paused_to_running(exclude_steps=["sw", "sc"])` 统一恢复

---

## 8. 错误处理模式

### 8.1 返回值约定

```python
# 执行方法统一返回 bool
def execute_sw_step(self) -> bool: ...
def execute_sc_step(self, config_name: int) -> bool: ...

# 校验/验证方法返回详细结果
def _validate_design_table(self, excel_path: str) -> list: ...
def _rebuild_and_export_per_config(self, doc, step_dir) -> tuple: ...

# 连接/初始化方法返回对象或 None
def _connect_sw(self): ...  # → sw_app or None
def _open_sw_model(self, ...): ...  # → doc or None
```

### 8.2 状态转换概览

```
Waiting → Running → Retrying → Paused → Running → ... → Completed
                                             ↘ Error
                   ↗ Paused (暂停时直接被归档)
```

- `set_step_status(cn, step, STATUS_RUNNING)` 开始执行
- 成功 → `STATUS_COMPLETED`
- 失败且有重试次数 → `STATUS_RETRYING`
- 暂停 → `STATUS_PAUSED`
- 最终失败 → `STATUS_ERROR`

### 8.3 COM 清理安全模式

```python
def _disconnect_sw(self, sw_app, doc, sw_model: str):
    """
    设计为在 finally 块中安全调用，sw_app/doc 可能为 None。
    每个操作都有独立的 try/except 防护。
    """
    # 步骤1: 关闭文档
    if doc is not None and ENGINE_CONFIG.get("sw_close_doc_on_finish", True):
        try:
            title = doc.GetTitle()
        except Exception:
            title = os.path.basename(sw_model)
        try:
            sw_app.CloseDoc(title)
        except Exception:
            pass
    
    # 步骤2: 释放 COM；SW 进程退出由全量清理统一处理
    del doc; del sw_app; gc.collect()
    pythoncom.CoUninitialize()
```

---

## 附录 A: 速查表

### A.1 常用状态常量

```python
STATUS_WAITING    = "Waiting"     # 等待执行
STATUS_RUNNING    = "Running"     # 正在执行
STATUS_PAUSED     = "Paused"      # 已暂停
STATUS_RETRYING   = "Retrying"    # 重试中
STATUS_COMPLETED  = "Completed"   # 已完成
STATUS_ERROR      = "Error"       # 出错
```

### A.2 超时配置键

```python
# 位于 ENGINE_CONFIG dict:
"sw_macro_timeout"       # SW 总超时
"sc_timeout"             # SC 执行超时
"transfer_timeout"       # 文件传输超时
"meshing_timeout"        # 网格超时
"solver_timeout"         # 求解超时
"max_retries"            # 最大重试次数（SW/SC/Transfer/Meshing 统一）

# 位于 OPERATION_TIMEOUTS dict:
"sw_startup"             # SW 启动超时
"sw_dispatch_startup_delay"  # SW Dispatch 启动延迟
"sc_poll_interval"           # SC 轮询间隔
"ssh_connection"             # SSH 连接超时
```

### A.3 配置节与模块对应

| TOML 节 | Python dict | Rust struct | 用途 |
|---------|-------------|-------------|------|
| `[local_paths]` | `LOCAL_PATHS` | `LocalPaths` | 本地文件/程序路径 |
| `[remote_config]` | `REMOTE_CONFIG` | `RemoteConfig` | 远程工作站信息 |
| `[step_file_patterns]` | `STEP_FILE_PATTERNS` | `StepFilePatterns` | 文件名模板 |
| `[engine_config]` | `ENGINE_CONFIG` | `EngineConfig` | 引擎行为参数 |
| `[operation_timeouts]` | `OPERATION_TIMEOUTS` | `OperationTimeouts` | 操作超时/轮询参数 |
