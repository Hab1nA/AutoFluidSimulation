---
description: "Python 代码规范与质量门禁。编辑 .py 文件时自动加载，提供命名规范、质量检查、防污染规则。"
applyTo: ["**/*.py"]
---

# Python 代码规范（AutoFluid）

> 本文件仅适用于 Python 代码。编辑 Rust 代码请参阅 `rust.instructions.md`，编辑 C# 代码请参阅 `csharp.instructions.md`。

---

## 🚫 语言隔离规则（最高优先级）

- **禁止**在 `.py` 文件中插入 Rust 代码、Rust 文档片段、`fn main()`、`impl` 块、`tokio`/`ratatui` 等 Rust 专用内容
- **禁止**在 `.py` 文件中插入 C# 代码、`using System`、`namespace` 声明等 C# 专用内容
- **禁止**将其他语言的 API 文档、示例代码、类型注解复制到 Python 文件中
- 如果发现上下文中混入了其他语言的代码或文档，**忽略它们**，仅关注 Python 相关内容

---

## 命名规范

### 步骤方法命名

所有步骤执行方法遵循统一模式：`execute_{步骤名}_step()`

| 方法 | 步骤 |
|------|------|
| `execute_sw_step()` | SolidWorks STEP 导出 |
| `execute_sc_step()` | SpaceClaim 转换 |
| `execute_transfer()` | 文件传输 |
| `execute_meshing()` | 网格划分 |
| `execute_solver()` | 仿真求解 |

### 私有辅助方法

使用 `_` 前缀 + 动词 + 步骤标识：

```python
# SW 步骤
_connect_sw()                    # 三层降级连接 SolidWorks
_open_sw_model()                 # OpenDoc6 打开模型
_rebuild_and_export_per_config() # 逐构型重建并导出 STEP
_verify_step_exports()           # 安全网校验输出文件
_disconnect_sw()                 # 清理 SW 资源
_terminate_sw_processes()        # 强制终止 SW 进程

# SC 步骤
_terminate_bridge_and_sc()       # 终止 Bridge 及关联 SC 进程
_build_command()                 # 构建启动命令
```

### 调度器方法

```python
start_pipeline()                 # 启动/继续流水线
pause() / resume() / stop()      # 暂停/恢复/停止
reset_config()                   # 重置指定构型步骤
_prepare_sw_retry()              # SW 重试准备
_resume_paused_steps()           # 恢复暂停的步骤
_scan_completed_downstream()     # 同步下游步骤状态
_execute_with_retry()            # 通用重试包装器
_barrier_monitor_loop()          # 全局屏障监控线程
_dispatch_solver_tasks()         # 屏障通过后分发 Solver
_pause_aware_sleep()             # 可响应暂停/停止的 sleep
```

### 通用命名规则

| 类别 | 风格 | 示例 |
|------|------|------|
| 类名 | `PascalCase` | `TaskRunner`, `SCProcessPool` |
| 函数/方法 | `snake_case` | `execute_sw_step()` |
| 常量 | `UPPER_SNAKE_CASE` | `STATUS_COMPLETED`, `MAX_SLOTS` |
| 变量 | `descriptive_snake_case` | `all_configs`, `sw_status_dist` |
| 临时变量 | 简短描述性 | `cn` (config_name), `s` (step_name) |
| 布尔变量 | 肯定谓语形式 | `should_run_sw`, `all_done`, `is_dirty` |

### 变量命名示例

```python
# ✓ 好: 描述性名称
all_configs = self.state.get_all_configs()
sw_status_dist: dict[str, list[int]] = {}
should_run_sw = not self.state.is_sw_macro_started()
sw_completed = [cn for cn in all_configs if ...]

# ✗ 差: 缩写/无意义名称
_cfgs = self.state.get_all_configs()
_d = {}
_flag = True
tmp1, tmp2, x, y
```

---

## 日志规范

使用 `[模块/子模块]` 英文标签前缀：

| 代码位置 | 前缀 | 示例 |
|----------|------|------|
| SW 步骤通用 | `[SW]` | `logger.info("[SW] 正在连接 SolidWorks...")` |
| SW COM 操作 | `[SW-COM]` | `logger.info("[SW-COM] OpenDoc6: Errors=0")` |
| SW 设计表 | `[SW-DesignTable]` | `logger.info("[SW-DesignTable] 正在验证 Excel...")` |
| SC 步骤 | `[SC]` | `logger.info("[SC] 启动 SpaceClaim Bridge...")` |
| Transfer | `[Transfer]` | `logger.info("[Transfer] 开始传输文件...")` |
| Meshing | `[Meshing]` | `logger.info("[Meshing] 提交网格任务...")` |
| Solver | `[Solver]` | `logger.info("[Solver] 启动求解器...")` |
| 调度器 | `[Scheduler]` | `logger.info("[Scheduler] 屏障检查通过")` |
| IPC | `[IPC]` | `logger.info("[IPC] 收到命令: pause")` |
| 状态管理 | `[State]` | `logger.info("[State] 构型 config_01 状态更新")` |

---

## 类型注解规范

- 使用 Python 3.10+ 语法（`dict[str, list[int]]` 而非 `Dict[str, List[int]]`）
- 当前项目标准解释器为仓库内 `.venv\Scripts\python.exe`（当前为 Python 3.13.9）
- 公共方法必须有完整的参数和返回值类型注解
- 私有方法建议添加返回值注解
- 使用 `from __future__ import annotations` 启用延迟注解求值

---

## 质量门禁

修改 Python 代码后，必须通过以下检查：

| 检查项 | 命令 | 标准 |
|--------|------|------|
| ruff linting | `.venv\Scripts\python.exe -m ruff check .` | 无新增 E/F/W 告警（忽略 E402/E501） |
| mypy 类型检查 | `.venv\Scripts\python.exe -m mypy .` | 无新增类型错误 |
| 单元测试 | `.venv\Scripts\python.exe -m pytest tests/` | 全部通过，无回归 |

---

## dataclasses 使用注意

- `dataclasses.asdict()` 内部使用 `copy.deepcopy`，会深拷贝所有字段值
- 包含 `subprocess.Popen` 等不可序列化对象时会报 `TypeError: cannot pickle '_thread.lock' object`
- **解决方案**：手动用字典字面量提取字段，而非使用 `asdict()`
