# 修复方案：调度竞态、SC 编码错误与日志过滤

> 日期：2026-06-04
> 触发事件：2026-06-04 22:31 运行暴露的偶发故障

## 1. 问题概述

2026-06-04 运行暴露 5 个问题：

| # | 问题 | 严重性 | 根因 |
|---|------|--------|------|
| 1 | `_resume_paused_steps()` 误将活跃 Running 重置为 Waiting | 🔴 主因 | 断点续传扫描不区分"孤儿 Running"与"活跃 Running" |
| | 构型 0 meshing=Running → 被重置为 Waiting | | SW 阶段提前启动了 WorkerPool/MeshingMonitor |
| | 构型 9 sc=Running → 被重置为 Waiting | | 但 `start_pipeline()` 步骤 2 仍执行扫描 |
| 2 | Transfer 重复入队（构型 8） | 🟡 | 问题 1 的连锁反应：状态被重置后重新入队 |
| 3 | `spaceclaim_transit.py` 每次成功后 UnicodeEncodeError | 🟡 | `_write_result` 写入结果后，外层 BaseException handler 覆盖成功结果 |
| 4 | WARNING 日志被 `_is_config_scoped_log()` 无条件过滤 | 🟡 | 过滤逻辑不区分日志级别，所有含"构型N"的日志均被丢弃 |
| 5 | MeshingMonitor 异常时构型永久丢失 | 🟡 | `_monitor_loop` except 块未设置 `should_requeue` |

### 1.1 关键证据

- SC-Pool 报错后自愈：slot 2 失败（exit=2）、slot 3 死亡，但构型 0-9 均有 `OK ... SCDOC`
- Meshing 已启动：构型 0 在 22:35:14 入队，22:35:15 启动轮询
- 22:43:34 断点续传扫描误介入：将构型 0 `meshing=Running` 重置为 Waiting，构型 9 `sc=Running` 重置为 Waiting
- 重复入队来自扫描与活跃 worker 并发：构型 8 Transfer 刚完成又被推入
- WARNING 日志未出现在 TUI 详细日志：`_is_config_scoped_log()` 过滤了所有含"构型N"的消息

## 2. 修复方案

### Phase 1: 调度竞态修复（主因）

#### 2.1.1 `UniqueWorkQueue` 新增 `has_claim()` 查询

**文件**: `engine/scheduler/work_queue.py`（`complete()` 方法后）

**改动**: 新增公共方法：

```python
def has_claim(self, key: Hashable) -> bool:
    """检查指定 key 是否有活跃 claim（排队中或执行中）。"""
    with self._lock:
        return key in self._claims
```

**作用**: 外部模块可在不触发入队的情况下检查队列 claim 状态。

**安全性**: 仅读取 `_claims` set，`_lock` 保证线程安全，不影响现有 `submit`/`complete`/`requeue` 逻辑。

#### 2.1.2 WorkerPoolManager 新增 in-flight 查询方法

**文件**: `engine/scheduler/worker_pool.py`（`submit_transfer()` 方法后）

**改动**: 新增两个公共方法：

```python
def is_transfer_in_flight(self, config_name: int) -> bool:
    """检查指定构型是否在 Transfer 队列中（排队或执行中）。"""
    return self._transfer_queue.has_claim(config_name)

def is_sc_in_flight(self, config_name: int) -> bool:
    """检查指定构型是否在 SC 队列中（排队或执行中）。"""
    return self._sc_queue.has_claim(config_name)
```

**注意**: SC 队列的 key 函数是 `lambda item: item[0]`（`main.py` L87），仅提取 `config_name`，所以 claim key 是 `int` 而非元组。

**安全性**: 仅委托 `has_claim()`，无副作用。

#### 2.1.3 MeshingMonitor 新增 public in-flight getter

**文件**: `engine/scheduler/meshing_monitor.py`（`qsize()` 方法后）

**改动**: 暴露已有的私有属性：

```python
def get_in_flight_config(self) -> int | None:
    """返回当前正在执行 Meshing 的构型编号，无则返回 None。"""
    return self._in_flight_config
```

**安全性**: 只读属性，`_in_flight_config` 在 `_monitor_loop` 的 `try` 块开头设置、`finally` 块中清除，生命周期正确。

#### 2.1.4 `_resume_paused_steps()` 增加全局 in-flight 保护

**文件**: `engine/scheduler/main.py` — `_resume_paused_steps()` 方法

**核心改动**: 在 `for step in STEP_NAMES:` 循环中，获取 `status` 后、进入各分支前，新增 in-flight 检查。

**新增私有辅助方法**（在 `_check_step_output_exists` 方法后）：

```python
def _is_step_in_flight(self, cn: int, step: str) -> bool:
    """检查指定构型的指定步骤是否正在被活跃处理（排队或执行中）。

    用于 _resume_paused_steps() 区分"孤儿 Running"与"活跃 Running"，
    避免将正在执行的步骤误重置为 Waiting。
    """
    if step == "sc":
        return self.worker_pool.is_sc_in_flight(cn)
    elif step == "transfer":
        return self.worker_pool.is_transfer_in_flight(cn)
    elif step == "meshing":
        return (
            self.meshing_monitor is not None
            and self.meshing_monitor.get_in_flight_config() == cn
        )
    return False  # sw/solver 由 start_pipeline/屏障统一管理
```

**循环体修改**（原 `if status == STATUS_RUNNING:` 分支前插入）：

```python
for step in STEP_NAMES:
    status = self.state.get_step_status(cn, step)

    if status == STATUS_COMPLETED:
        continue

    # ★ 全局 in-flight 保护：无论步骤处于何种非 COMPLETED 状态，
    #   只要队列中有该构型的 claim 或 MeshingMonitor 正在处理，
    #   就跳过该构型（不重置状态、不重复入队）。
    if self._is_step_in_flight(cn, step):
        logger.debug(
            f"{log_prefix} 构型{cn} [{step}] 状态={status} "
            f"但正在活跃处理中，跳过"
        )
        break  # 该构型有活跃步骤，不重置，不检查后续步骤

    # ---- 原有 RUNNING/PAUSED/WAITING/ERROR 分支逻辑 ----
    if status == STATUS_RUNNING:
        ...
```

**覆盖范围分析**:

| 状态 | in-flight 时行为 | 非 in-flight 时行为（不变） |
|------|-----------------|---------------------------|
| RUNNING | 跳过（保护活跃任务） | 检查输出文件 → 标记完成或重置 |
| PAUSED | 跳过（worker 可能仍在清理） | 检查输出文件 → 标记完成或优先入队 |
| WAITING | 跳过（已在队列中） | 普通入队 |
| ERROR/RETRYING | 跳过（等 claim 释放后自然恢复） | 检查重试次数 → 重置或保留 |

**边界场景验证**:

| 场景 | in-flight 结果 | 最终行为 | 安全性 |
|------|---------------|---------|--------|
| pause → resume | worker 已退出，claim 已释放 | False → 走原有 PAUSED 分支 | ✅ |
| stop → start | `join_worker_threads` 释放 claim | False → 断点恢复 | ✅ |
| daemon 重启 | 线程死亡，claim 清空 | False → 孤儿 Running 被检测 | ✅ |
| worker 挂起 | claim 未释放 | True → 跳过，不误重置 | ✅ |
| step 恰好完成 | claim 释放 | False → `submit()` 被拒或 output check 发现完成 | ✅ |

### Phase 2: SC Transit 编码错误修复

#### 2.2.1 `_persistent_loop()` 异常路径精确隔离

**文件**: `executor/spaceclaim_transit.py` — `_persistent_loop()` 方法

**问题分析**:

现有代码结构（L783-795）：

```python
            # L783: _write_result 调用（无独立 try-except）
            _write_result(result_file, config_name, success,
                          "转换成功" if success else "转换失败",
                          run_id=run_id)

            # L788: logger 调用（已有独立 try-except）
            try:
                if success:
                    logger.info("常驻模式: 构型 {} 转换成功".format(config_name))
                else:
                    logger.error("常驻模式: 构型 {} 转换失败".format(config_name))
            except Exception:
                pass

        # L795: 外层 handler — 会写入 success=false 覆盖
        except BaseException as e:
            ...
            _write_result(result_file, "", False, "主循环异常: ...")
```

日志显示 `UnicodeEncodeError` 到达了 L795 外层 handler。L788-794 的 logger wrapper 已正确捕获 `Exception`（含 `UnicodeEncodeError`）。因此 **异常来源是 `_write_result`（L783），而非 logger 调用**。

`_write_result` 内部虽有 4 层降级保护，但 IronPython 的 `.NET StreamWriter` 编码行为不可预测，某些极端编码异常可能逃逸。

**修复**: 在 `_write_result` 调用外增加独立 try-except：

```python
            # ★ _write_result 独立包裹：编码异常不逃逸到外层 BaseException handler
            #   _write_result 内部已有 4 层降级保护，此处仅兜底极端编码异常
            try:
                _write_result(result_file, config_name, success,
                              "转换成功" if success else "转换失败",
                              run_id=run_id)
            except (UnicodeEncodeError, UnicodeDecodeError) as write_err:
                # 编码异常：_write_result 的降级路径可能已写入部分结果，
                # 但不应让外层 handler 再写入 success=false 覆盖
                try:
                    logger.error("常驻模式: 结果写入编码异常（已忽略）: {}".format(write_err))
                except Exception:
                    pass

            # ★ 日志调用单独包裹（已有，保持不变）
            try:
                if success:
                    logger.info("常驻模式: 构型 {} 转换成功".format(config_name))
                else:
                    logger.error("常驻模式: 构型 {} 转换失败".format(config_name))
            except Exception:
                pass
```

**异常路径分析（修复后）**:

| 场景 | _write_result | logger | 外层 handler | 结果文件状态 |
|------|--------------|--------|-------------|------------|
| 正常成功 | ✅ 成功写入 | ✅ 日志正常 | 不触发 | success=true ✅ |
| 成功 + logger 编码异常 | ✅ 成功写入 | ❌ 被 wrapper 捕获 | 不触发 | success=true ✅ |
| _write_result 编码异常 | ❌ 被新 wrapper 捕获 | ✅/❌ | 不触发 | 降级路径可能已写入 ✅ |
| 其他未预期异常 | - | - | 触发 | success=false（正确兜底） |

#### 2.2.2 `result_file` 空路径防护

**文件**: `executor/spaceclaim_transit.py` — `_persistent_loop()` 方法（L693）

**当前代码**:
```python
    result_file = ""
```

**修复**:
```python
    # ★ 基于 slot_id 的默认路径，确保外层 handler 在任何异常下都能写入有效路径
    result_file = os.path.join(cmd_dir, "sc_result_{}".format(slot_id))
```

**安全性**: 该路径仅在 run_id 提取失败时使用（外层 handler 兜底），正常流程中会在 L712-717 按 run_id 覆盖为精确路径。

### Phase 3: 日志过滤修复

#### 2.3.1 `_is_config_scoped_log()` 放行 WARNING/ERROR/CRITICAL

**文件**: `utils/logger.py` — `_is_config_scoped_log()` 方法（L387-390）

**当前代码**:
```python
    @staticmethod
    def _is_config_scoped_log(record: logging.LogRecord) -> bool:
        """Return True for log messages tied to a single configuration."""
        message = record.getMessage()
        return any(pattern.search(message) for pattern in _CONFIG_SCOPED_LOG_PATTERNS)
```

**修复**:
```python
    @staticmethod
    def _is_config_scoped_log(record: logging.LogRecord) -> bool:
        """Return True for log messages tied to a single configuration.

        仅过滤 INFO/DEBUG 级别的构型详情日志，WARNING/ERROR/CRITICAL
        始终放行，确保 TUI 详细日志面板不遗漏关键告警。
        """
        if record.levelno >= logging.WARNING:
            return False
        message = record.getMessage()
        return any(pattern.search(message) for pattern in _CONFIG_SCOPED_LOG_PATTERNS)
```

**使用 `record.levelno`（int）的原因**: 过滤发生在 `emit()` 阶段，`record.getMessage()` 返回原始消息文本，不含 `[WARNING]` 前缀。`record.levelno` 是 `logging.WARNING`（30）等整数常量，比较可靠。

**影响分析**:

| 日志级别 | 修复前 | 修复后 |
|---------|--------|--------|
| DEBUG（含"构型N"） | 过滤 | 过滤（无变化） |
| INFO（含"构型N"） | 过滤 | 过滤（无变化） |
| WARNING（含"构型N"） | 过滤 ✗ | **放行** ✓ |
| ERROR（含"构型N"） | 过滤 ✗ | **放行** ✓ |
| CRITICAL（含"构型N"） | 过滤 ✗ | **放行** ✓ |

### Phase 4: MeshingMonitor 异常 requeue 保护

#### 2.4.1 `_monitor_loop()` 异常时 requeue（复用现有 DB 重试计数）

**文件**: `engine/scheduler/meshing_monitor.py` — `_monitor_loop()` 方法

**问题分析**:

当前代码（L112-135）：

```python
            should_requeue = False
            try:
                should_requeue = self._process_single_meshing(config_name)
            except (RuntimeError, ValueError, OSError, ConnectionError) as e:
                logger.error(...)
                self.state.set_step_status(config_name, "meshing", STATUS_ERROR, str(e))
                # ↑ 标记 ERROR 但 should_requeue 仍为 False
            except Exception as e:
                logger.critical(...)
                self.state.set_step_status(config_name, "meshing", STATUS_ERROR, ...)
                # ↑ 同上
            finally:
                ...
                if should_requeue:
                    self._meshing_queue.requeue(config_name)
                else:
                    self._meshing_queue.complete(config_name)
                    # ↑ claim 被释放，构型永久丢失
```

**关键设计决策**: 异常时 **不标记 ERROR**，仅设置 `should_requeue = True`。

原因：若标记 ERROR + requeue，`_scan_db_for_pending()` 会把 `meshing=ERROR` + `transfer=Completed` 的构型再次入队 → 同一构型在队列中出现两次（requeue 一次 + DB 扫描一次）。

**复用决策**: 使用现有的 `state.increment_retry()` + `state.get_step_retry_count()` + `ENGINE_CONFIG["max_retries"]` 跟踪 requeue 次数，**不新增** `_requeue_counts` 内存 dict。

理由：
- `increment_retry()` 已在 MeshingMonitor 启动重试（L211）和 `retry.py` 中使用，是项目统一的重试计数机制
- DB 持久化：daemon 重启后计数不丢失（内存 dict 会丢失）
- TUI 可见：`retry_count` 字段在 TUI 中显示，用户可观察重试进度
- 统一重试预算：requeue 次数与启动重试共享 `max_retries` 上限，避免无限重试

**修复**:

修改 except 块和 finally 块：

```python
            self._in_flight_config = config_name
            should_requeue = False
            try:
                should_requeue = self._process_single_meshing(config_name)
            except (RuntimeError, ValueError, OSError, ConnectionError) as e:
                logger.error(
                    f"[MeshingMonitor] 处理构型{config_name} 异常: {e}",
                    exc_info=True,
                )
                # ★ 不标记 ERROR：避免 _scan_db_for_pending() 重复入队
                #   检查重试次数，未达上限则 requeue
                retry_count = self.state.increment_retry(config_name, "meshing")
                max_retries = int(ENGINE_CONFIG["max_retries"])
                if retry_count < max_retries:
                    self.state.set_step_status(
                        config_name, "meshing", STATUS_RETRYING,
                        f"异常重试 {retry_count}/{max_retries}",
                    )
                    should_requeue = True
                else:
                    self.state.set_step_status(
                        config_name, "meshing", STATUS_ERROR,
                        f"Meshing 异常重试 {retry_count} 次后放弃: {e}",
                    )
                    # should_requeue 保持 False → complete() 释放 claim
            except Exception as e:
                logger.critical(
                    f"[MeshingMonitor] 处理构型{config_name} 致命异常: "
                    f"{type(e).__name__}: {e}",
                    exc_info=True,
                )
                retry_count = self.state.increment_retry(config_name, "meshing")
                max_retries = int(ENGINE_CONFIG["max_retries"])
                if retry_count < max_retries:
                    should_requeue = True
                else:
                    self.state.set_step_status(
                        config_name, "meshing", STATUS_ERROR,
                        f"致命异常重试 {retry_count} 次后放弃: {type(e).__name__}: {e}",
                    )
            finally:
                self._in_flight_config = None
                if should_requeue:
                    logger.warning(
                        f"[MeshingMonitor] 构型{config_name} 异常，重新入队"
                    )
                    self._meshing_queue.requeue(config_name)
                else:
                    self._meshing_queue.complete(config_name)
```

**与现有重试机制的关系**:

| 重试场景 | 跟踪机制 | 位置 |
|---------|---------|------|
| Meshing 启动失败重试 | `state.increment_retry()` | `_process_single_meshing` L211 |
| Meshing 异常 requeue 重试 | `state.increment_retry()` | `_monitor_loop` except 块（本次新增） |
| SC/Transfer/Solver 重试 | `state.increment_retry()` | `retry.py` `execute_with_retry` |

三者共享同一 DB `retry_count` 字段和 `ENGINE_CONFIG["max_retries"]` 上限，架构统一。

**`_scan_db_for_pending()` 兼容性**: 该方法扫描 `meshing in (WAITING, ERROR, RETRYING, RUNNING, PAUSED)`。requeue 时状态为 RETRYING（在扫描集合内），不会导致重复入队。超过重试上限后标记 ERROR，也在扫描集合内，可由用户 `reset` 后手动恢复。

### Phase 5: 运行恢复（操作指导）

- **不执行** `reset all all`，直接 `start`
- 断点续传会检查远程构型 0 的网格文件（`model_gen4_0.msh.h5` + `meshing_done_0.txt`），标记 meshing=Completed
- 然后依次处理构型 1-9 的 Meshing（串行，Fluent 单进程限制）

## 3. 修改文件清单

| 文件 | 修改位置 | 改动描述 | 改动量 |
|------|---------|---------|-------|
| `engine/scheduler/work_queue.py` | `complete()` 后 | 新增 `has_claim(key) -> bool` | +5 行 |
| `engine/scheduler/worker_pool.py` | `submit_transfer()` 后 | 新增 `is_transfer_in_flight()`, `is_sc_in_flight()` | +10 行 |
| `engine/scheduler/meshing_monitor.py` | `qsize()` 后；`_monitor_loop` | 新增 `get_in_flight_config()`；异常 requeue + 复用 `increment_retry()` | +25 行 |
| `engine/scheduler/main.py` | `_check_step_output_exists` 后；`_resume_paused_steps` 循环 | 新增 `_is_step_in_flight()`；循环中 in-flight 检查 | +20 行 |
| `executor/spaceclaim_transit.py` | L693；L783-786 | result_file 默认路径；`_write_result` 编码异常隔离 | +10 行 |
| `utils/logger.py` | L387-390 | `_is_config_scoped_log` 放行 WARNING+ | +3 行 |

**总计**: 约 73 行新增/修改代码，6 个文件。

## 4. 验证方案

### 4.1 自动化检查

```bash
.venv\Scripts\python.exe -m ruff check .         # 无新增 E/F/W 告警
.venv\Scripts\python.exe -m mypy .               # 无新增类型错误
.venv\Scripts\python.exe -m pytest tests/        # 全部通过
```

### 4.2 手动验证场景

| # | 场景 | 验证方法 | 预期结果 |
|---|------|---------|---------|
| a | 活跃流水线中 resume 不误重置 | 构造 transfer=Running + transfer_queue 有 claim，调用 `_resume_paused_steps` | 日志显示"正在活跃处理中，跳过"，状态不变 |
| b | Transfer 重复入队被拦截 | 同一构型连续调用两次 `submit_transfer` | 第二次返回 False，无 WARNING 日志 |
| c | WARNING 日志 TUI 可见 | `logger.warning("检测到重复入队：构型8 Transfer...")` | 出现在 TUI 详细日志面板 |
| d | MeshingMonitor 异常 requeue | mock `start_meshing` 抛异常 | 构型被 requeue，状态变为 RETRYING，retry_count 递增；超过 max_retries 后标记 ERROR |
| e | SC transit 编码隔离 | mock `_write_result` 抛 UnicodeEncodeError | 外层 handler 写入失败结果（正确兜底） |
| f | SC transit 成功不被覆盖 | mock logger 抛 UnicodeEncodeError | 结果文件保持 success=true |

### 4.3 回归测试

- 完整流水线运行：SW → SC → Transfer → Meshing → Solver
- pause/resume 循环：暂停后恢复，确认所有步骤正确继续
- stop/start 断点续传：停止后重启，确认断点恢复正确
- daemon 重启恢复：kill daemon 后重启，确认孤儿 Running 被正确检测

## 5. 设计决策

| 决策 | 理由 |
|------|------|
| `_resume_paused_steps` 不拆分函数 | 通过全局 in-flight 检查保护所有活跃状态，改动最小 |
| in-flight 检查覆盖所有非 COMPLETED 状态 | WAITING 也可能在队列中，ERROR 可能 claim 未释放 |
| MeshingMonitor 串行不改 | Fluent 单进程限制，串行是设计约束 |
| requeue 时复用 `state.increment_retry()` | 统一重试计数机制，DB 持久化，TUI 可见，不新增内存 dict |
| requeue 时不标记 ERROR（标记 RETRYING） | 避免 `_scan_db_for_pending()` DB 扫描重复入队 |
| `_write_result` 编码异常隔离 | IronPython `.NET StreamWriter` 行为不可预测，需兜底 |
| `result_file` 改为 slot_id 默认路径 | 消除空路径风险，外层 handler 兜底路径有效 |
| 日志过滤放行 WARNING+ | 确保 TUI 详细日志不遗漏关键告警 |
| `has_claim()` 新增而非复用 `submit()` 返回值 | `submit()` 有副作用（入队），需要只读查询 |
| `get_in_flight_config()` 新增而非复用 `set_meshing_running_if_idle()` | 后者是原子 check-and-set（有副作用），需要只读查询 |

## 6. 风险评估

| 风险 | 概率 | 影响 | 缓解措施 |
|------|------|------|---------|
| in-flight 检查导致孤儿 Running 不被清理 | 低 | 中 | 仅在 claim 存在时跳过；daemon 重启后 claim 清空，孤儿仍被检测 |
| MeshingMonitor requeue 无限循环 | 低 | 低 | 复用 `max_retries` 上限，超过后标记 ERROR |
| requeue 与启动重试共享 retry_count 导致提前耗尽 | 低 | 低 | 两者均为同一构型的 meshing 步骤，共享预算合理 |
| `_write_result` 编码异常后结果文件不完整 | 极低 | 低 | `_write_result` 已有 4 层降级保护，极端情况外层 handler 兜底 |
| 日志过滤放行导致 TUI 日志过多 | 低 | 低 | 仅放行 WARNING+，INFO/DEBUG 噪声仍被过滤 |
