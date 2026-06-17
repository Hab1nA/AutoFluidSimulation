# 测试失败问题分析报告

> **日期**：2026-06-17  
> **分支**：`codex/daemon-split-ocar-worker`  
> **测试文件**：`tests/test_pause_start.py`、`tests/test_spaceclaim_bridge_source.py`  
> **结果**：27 个测试中 3 个失败，24 个通过

---

## 问题概述

| # | 测试用例 | 失败断言 |
|---|---------|---------|
| 1 | `test_sw_fail_then_restart` | `AssertionError: 全 SW 失败后引擎未能进入 stopped 状态` |
| 2 | `test_start_dispatches_solver_serially_when_barrier_already_met` | `AssertionError: start 后应先启动第一个 Solver` |
| 3 | `test_bridge_normalizes_duplicate_path_environment_before_start` | `AssertionError: assert 1 >= 2`（`NormalizePathEnvironmentVariables(psi)` 调用次数） |

- **影响范围**：SW 阶段错误处理、Solver 串行分发、Bridge C# 源码静态检查
- **问题域**：运行时状态机逻辑 + 测试与源代码契约不匹配

---

## 调查过程

### 已检查的资料

| 文件 | 发现 |
|------|------|
| `tests/test_pause_start.py` | 完整测试代码（14 个用例），Mock 类定义及 `TestContext`、`run_pipeline_async` |
| `tests/test_spaceclaim_bridge_source.py` | 完整测试代码（13 个用例），纯源码字符串匹配检查 |
| `engine/scheduler/sw_phase.py` | `_execute_sw_macro` 的全部 Error 处理逻辑 |
| `engine/scheduler/main.py` | `_start_pipeline_impl`、`_resume_paused_steps`、`_finalize_barrier_after_downstream_start`、`reset_config` |
| `engine/scheduler/barrier.py` | `monitor_loop`、`dispatch_solver_if_ready`、`_dispatch_solver_tasks`、`_next_solver_config` |
| `bridge/SpaceClaimBridge/Program.cs` | `PrepareSpaceClaimEnvironment`、`NormalizePathEnvironmentVariables` 调用链 |
| `engine/scheduler/control.py` | `PipelineControl.prepare_start` |
| `engine/scheduler/utils.py` | `PauseGuard.check_should_abort` |
| Pytest 运行输出（stdout + stderr + captured log） | 完整时序日志 |

### 关键发现

- **发现 1**：`_execute_sw_macro` 在 SW 全部失败（所有构型 `sw = Error`）时，仅当 `sw_errors` 为空（即只有 `Running` 无 `Error`）才设置 `engine_status = "stopped"`。全部 Error 的路径未覆盖。
- **发现 2**：`NormalizePathEnvironmentVariables(psi)` 已被重构封装到 `PrepareSpaceClaimEnvironment(psi)` 内部，仅在 `Program.cs:409` 直接调用一次。测试仍检查原始的直接调用模式（期望 ≥2 次）。
- **发现 3**：测试预置 `_barrier_passed.set()` 导致屏障监控线程的 `while` 循环体永不执行。调度器启动后 meshing 被重置为 Waiting，之后无触发路径重新分发 Solver。

---

## 根因分析

### 失败 1：`test_sw_fail_then_restart`（置信度：高）

**根因**：`_execute_sw_macro` 未处理「全部 SW Error」的终态。

**证据链** — `engine/scheduler/sw_phase.py:230-248`：

```python
if sw_errors or sw_running:
    self._prepare_sw_retry()
    if sw_errors:
        for cn in sw_errors:
            logger.warning(f"[SW] 构型{cn} STEP 导出失败")
    if sw_running:
        for cn in sw_running:
            logger.warning(f"[SW] 构型{cn} 仍为 Running 状态 (可能导出中断)")
    if not sw_errors:                              # ← 全部 Error 时此条件为 False
        self.state.set_engine_status("stopped")     # ← 永远不执行
        logger.error("[SW] SW 步骤失败，流水线中止")
        return False
```

- `sw_errors` 非空 → `if not sw_errors:` 为 `False` → `set_engine_status("stopped")` 不执行
- 方法返回 `True` → 调度器进入 `scan_completed_downstream` → `_resume_paused_steps` 将 SW Error 重置为 Waiting → 引擎进入 `running`
- 屏障监控线程（`barrier.py:220-230`）虽有「所有 SW 终态且无 Completed」检测，但 SW 状态已被提前重置，该检测无法触发

**时序流程**（来自日志）：

```
11:38:03 SW 宏启动 → 3 个构型全部 Error
11:38:03 scan_completed_downstream → SC 步骤被标记 Completed
11:38:03 _resume_paused_steps → SW Error → Waiting（重置）
11:38:03 引擎状态 → running （而非 stopped）
```

---

### 失败 2：`test_start_dispatches_solver_serially_when_barrier_already_met`（置信度：高）

**根因**：预置 `_barrier_passed` 阻断了屏障监控循环，且启动流程中的 Solver 分发时机过早。

**证据链**：

测试代码（`test_pause_start.py:860-863`）：
```python
ctx.state.set_global_barrier_met(True)
ctx.scheduler._barrier_passed.set()      # ← 预置 barrier 已通过
```

调度器启动流程（`engine/scheduler/main.py:228-231`）：
```python
if (not self._barrier_passed.is_set()        # ← True，not True = False
    and not self._has_downstream_errors()):
    self._ensure_barrier_monitor_running()   # ← 不执行
```

后续 `_finalize_barrier_after_downstream_start`（`main.py:418-419`）：
```python
def _finalize_barrier_after_downstream_start(self) -> None:
    if not self.barrier_coordinator.dispatch_solver_if_ready():
        self._ensure_barrier_monitor_running()
```

此时 meshing 已被 `_resume_paused_steps` 重置为 Waiting → `dispatch_solver_if_ready()` 返回 `False`。

`_ensure_barrier_monitor_running` 启动的屏障线程检查（`barrier.py:196`）：
```python
while not self._stopped.is_set() and not self._barrier_passed.is_set():
    # _barrier_passed 为 True → 循环体永不执行 → 线程立即退出
```

Meshing 完成后，无任何机制重新调用 `dispatch_solver_if_ready()`。日志显示屏障监控在 11:38:26 尝试分发但 `_next_solver_config` 返回 `None`，表示此时 meshing 或 solver 状态与预期不符（推测与并发时序有关，需运行时加日志确认具体状态值）。

**时序流程**（来自日志）：

```
11:38:25 调度器启动 → SW 断点续传
11:38:25 下游扫描 → meshing Completed → 输出文件缺失 → 重置为 Waiting
11:38:25 _finalize_barrier_after_downstream_start → Solver 分发失败（meshing 未完成）
11:38:25 引擎状态 → running
11:38:26 meshing 完成（mock 即时完成）
11:38:26 屏障监控尝试分发 → "当前没有待执行的求解任务"
```

---

### 失败 3：`test_bridge_normalizes_duplicate_path_environment_before_start`（置信度：高）

**根因**：Bridge C# 源码重构后，测试断言模式未同步更新。

**证据链**：

重构前（推测）：`NormalizePathEnvironmentVariables(psi)` 在多处直接调用。  
重构后（`Program.cs:406-411`）：封装到 `PrepareSpaceClaimEnvironment` 中：

```csharp
private static void PrepareSpaceClaimEnvironment(ProcessStartInfo psi)
{
    BackfillProcessEnvironment(psi);
    NormalizePathEnvironmentVariables(psi);  // ← 唯一的直接调用点
    EnsureAnsysEnvironmentVariables(psi);
}
```

- `PrepareSpaceClaimEnvironment(psi)` 在两处被调用：一次性模式（line 309）、常驻模式（line 686）
- 但测试断言的是 `NormalizePathEnvironmentVariables(psi)` **直接调用**次数
- Grep 结果：`NormalizePathEnvironmentVariables(psi)` 仅在 line 409 匹配到 **1 次**

---

## 修复方案

### 方案 A：修复 `_execute_sw_macro` 全部 Error 终态 ⭐⭐⭐ 推荐

- **描述**：在 `sw_errors` 非空且覆盖所有构型时，设置引擎状态为 `stopped` 并返回 `False`
- **涉及文件**：`engine/scheduler/sw_phase.py` — 在 `if sw_errors or sw_running:` 块内增加全部 Error 检测
- **改动量**：< 10 行
- **复杂度**：简单（单文件局部修改）
- **副作用风险**：低（仅新增终态分支，不影响正常路径）
- **需同步修改的测试**：无

### 方案 B：修复 `_barrier_passed` 预置场景的 Solver 分发 ⭐⭐⭐ 推荐

- **描述**：在 `_resume_paused_steps` 检测到 meshing 状态被重置（已无 Completed 构型）时，同步清除 `_barrier_passed` 和 `global_barrier_met`，使屏障监控线程能正常进入循环
- **涉及文件**：`engine/scheduler/main.py` — `_resume_paused_steps` 方法中增加 barrier 清除逻辑
- **改动量**：< 15 行
- **复杂度**：中等（涉及状态同步逻辑，需确保不清除合法的断点续传 barrier）
- **副作用风险**：
  - 可能影响真实断点续传场景（Daemon 重启后 barrier 已持久化为 true）
  - 需与 `state.set_global_barrier_met(False)` 保持同步
- **需同步修改的测试**：验证失败用例通过 + 运行完整测试套件

### 方案 C：更新 Bridge 源码测试断言 ⭐⭐⭐ 推荐

- **描述**：将测试断言从 `NormalizePathEnvironmentVariables(psi)` 直接调用次数改为检查 `PrepareSpaceClaimEnvironment(psi)` 调用次数 ≥ 2，或检查 `NormalizePathEnvironmentVariables` 方法存在（不要求直接调用次数）
- **涉及文件**：`tests/test_spaceclaim_bridge_source.py` — `test_bridge_normalizes_duplicate_path_environment_before_start`
- **改动量**：< 5 行
- **复杂度**：简单
- **副作用风险**：无（仅修改测试）

**替代方案**：若 `NormalizePathEnvironmentVariables(psi)` 确实需要在 `PrepareSpaceClaimEnvironment` 之外额外调用，则在 `Program.cs` 恢复调用。需确认是有意删除还是遗漏。

---

## 建议下一步

1. **优先修复**：方案 A → 方案 C → 方案 B（前两个改动量小、风险低）
2. **验证步骤**：
   - 运行 `pytest tests/test_pause_start.py tests/test_spaceclaim_bridge_source.py -v` 确认全部 27 个测试通过
   - 对方案 B 额外运行 `pytest tests/` 确认无回归
3. **附加调查**：方案 B 中建议在 `_next_solver_config` 和 `_dispatch_solver_tasks` 入口加 DEBUG 日志打印 meshing/solver 实际状态，以精确定位 `"当前没有待执行的求解任务"` 的触发条件
