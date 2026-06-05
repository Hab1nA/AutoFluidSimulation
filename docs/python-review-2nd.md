# Python 代码审查报告（第 2 轮）

**审查日期**: 2026-06-05  
**审查范围**: 所有 Python 文件（`engine/`、`executor/`、`ipc/`、`utils/`、`tests/`、入口脚本）  
**审查重点**: 功能重复实现、不规范代码、边界条件/竞态条件等逻辑缺陷  
**审查方式**: 逐文件全量阅读 + grep 交叉验证  
**质量门禁**: `ruff check .` 全部通过

---

## 发现统计

| 严重程度 | 数量 |
|----------|------|
| High | 5 |
| Medium | 8 |
| Low | 4 |

---

## High 级别问题

### H-1: `UniqueWorkQueue.requeue()` 存在 `task_done()` 时序窗口（`engine/scheduler/work_queue.py` 第 58-61 行）

```python
def requeue(self, item: T) -> None:
    """保留 claim 并将当前任务放回队列尾部。"""
    self._queue.put(item)      # ① item 重新入队
    self._queue.task_done()    # ② 标记原始 get() 完成
```

**问题**：`put()` 和 `task_done()` 之间没有原子性保证。如果线程 A 调用 `requeue(item)` 在 ① 之后、② 之前，线程 B 执行 `get()` 取出了该 item 并随后调用 `complete()` → `task_done()`，那么同一个原始 `get()` 会被 `task_done()` 两次（线程 A 的 ② + 线程 B 的 complete），而线程 B 的 `get()` 没有对应的 `task_done()`。`Queue.join()` 的内部 `_unfinished_tasks` 计数器将变为 -1，导致 `join()` 永远不会阻塞（语义错误）。

当前代码中 `_monitor_loop` 的 `finally` 块保证同一 item 不会被同时 `requeue` 和 `complete`，因此**实际运行中不会触发**。但这是一个脆弱的隐式约束。

**修复建议**: 将 `put()` 移到 `task_done()` 之后，或使用锁保护 `requeue` 的原子性：

```python
def requeue(self, item: T) -> None:
    self._queue.task_done()  # 先标记原始 get() 完成
    self._queue.put(item)    # 再重新入队
```

---

### H-2: `handle_start` 中调度器线程异常后 `engine_status` 残留为 `running`（`engine/daemon.py` 第 345-360 行）

```python
# 全新启动
self.state.set_engine_status("running")   # ← 先设置状态
...
scheduler_thread = threading.Thread(
    target=self.scheduler.start_pipeline,  # ← 若此方法抛异常
    daemon=True,
    name="SchedulerMain"
)
scheduler_thread.start()                   # ← 线程静默退出
```

**问题**：`start_pipeline` 在独立 daemon 线程中运行。如果内部抛出未捕获异常（如 SQLite 错误、Excel 读取失败），线程静默退出，`engine_status` 仍为 `running`，但调度器线程已死亡。TUI 显示"运行中"但流水线实际已停止。

虽然 `start()` 方法在第 198-203 行有残留状态检测，但那仅在 Daemon **进程启动时**执行一次，运行期间的线程异常不会被检测到。

**修复建议**: 在 `start_pipeline` 顶层添加 `try/finally`：

```python
def start_pipeline(self, _recursion_depth: int = 0):
    try:
        # ... 原有逻辑 ...
    except Exception as e:
        logger.error(f"调度器线程异常退出: {e}", exc_info=True)
    finally:
        if self.state.get_engine_status() != "stopped":
            self.state.set_engine_status("stopped")
```

---

### H-3: `sw_phase.py` 直接访问 `TaskRunner._sw_executor` 私有属性（`engine/scheduler/sw_phase.py` 第 182 行、第 233 行）

```python
self.runner._sw_executor.disconnect_sw_cached()     # 第 182 行
total_found = self.runner._sw_executor._verify_step_exports(step_dir)  # 第 233 行
```

**问题**：`TaskRunner` 已为其他 `_sw_executor` 方法提供了公共代理（`shutdown_sw_processes()`、`do_sw_first_cleanup()`、`do_sw_final_cleanup()` 等），但遗漏了 `disconnect_sw_cached()` 和 `_verify_step_exports()`。直接访问 `_` 前缀属性违反封装，且 `_sw_executor` 重命名时这些调用不会被 IDE 的重命名工具捕获。

**修复建议**: 在 `TaskRunner` 中添加公共代理：

```python
def disconnect_sw_cached(self) -> None:
    self._sw_executor.disconnect_sw_cached()

def verify_step_exports(self, step_dir: str) -> int:
    return self._sw_executor._verify_step_exports(step_dir)
```

---

### H-4: `execute_meshing` 和 `start_meshing` 是完全相同的重复方法（`executor/remote_executor.py` 第 278-293 行）

```python
def execute_meshing(self, config_name: int) -> bool:
    if not isinstance(config_name, int):
        logger.error(...)
        return False
    return self._run_meshing_command(config_name)

def start_meshing(self, config_name: int) -> bool:
    if not isinstance(config_name, int):
        logger.error(...)
        return False
    return self._run_meshing_command(config_name)
```

**问题**：两个方法实现完全相同，仅 docstring 描述了不同调用场景。增加了维护成本——修改一个必须同步修改另一个。

**修复建议**: 合并为一个方法，或让 `execute_meshing` 委托给 `start_meshing`：

```python
def execute_meshing(self, config_name: int) -> bool:
    """别名，保持向后兼容。"""
    return self.start_meshing(config_name)
```

---

### H-5: 标志文件路径在 `remote_executor.py` 中重复构建 5 次

| 行号 | 方法 | 路径表达式 |
|------|------|-----------|
| 317 | `_build_meshing_command` | `f"{REMOTE_CONFIG['flag_dir']}/meshing_done_{config_name}.txt"` |
| 421 | `check_meshing_done` | 同上 |
| 444 | `wait_meshing_completion` | 同上 |
| 525 | `_build_solver_command` | `f"{REMOTE_CONFIG['flag_dir']}/solver_done_{config_name}.txt"` |
| 627 | `wait_solver_completion` | 同上 |

**问题**：相同的路径构建逻辑散布在 5 个方法中。如果标志目录或命名规则变更，需要同步修改 5 处，遗漏任何一处都会导致标志文件检测失败（流水线卡死）。

**修复建议**: 提取为两个私有方法：

```python
def _meshing_flag_file(self, config_name: int) -> str:
    return f"{REMOTE_CONFIG['flag_dir']}/meshing_done_{config_name}.txt".replace("\\", "/")

def _solver_flag_file(self, config_name: int) -> str:
    return f"{REMOTE_CONFIG['flag_dir']}/solver_done_{config_name}.txt".replace("\\", "/")
```

---

## Medium 级别问题

### M-1: `execute_transfer` 设置步骤状态，但 `execute_meshing`/`execute_solver` 不设置（策略不一致）

**文件**: `executor/remote_executor.py`

`execute_transfer()` 在 5 处直接调用 `self.state.set_step_status(config_name, "transfer", STATUS_ERROR, ...)`。但 `execute_meshing()` 和 `execute_solver()` 的 docstring 明确说明"不设置步骤状态，由调用方统一管理"。

**后果**: `RetryManager.execute_with_retry()` 在 `execute_func` 返回 False 后会设置 `STATUS_ERROR`。对于 Transfer 步骤，`execute_transfer` 已经设置了一次 ERROR，`RetryManager` 又设置一次（可能带不同错误消息），后者覆盖前者。虽然功能上不影响，但语义不一致增加了理解成本。

**修复建议**: 统一策略——`execute_transfer` 也不设置步骤状态，由 `RetryManager` 统一管理。

---

### M-2: `wait_meshing_completion` 和 `wait_solver_completion` 中暂停补偿逻辑重复且未使用 `PauseGuard`

**文件**: `executor/remote_executor.py` 第 444-460 行、第 627-645 行

两处暂停补偿代码几乎完全相同：

```python
if paused_event is not None:
    pause_start = time.time()
    if not wait_unless_paused_or_stopped(paused_event, stopped_event or threading.Event()):
        return False
    pause_duration = time.time() - pause_start
    if pause_duration > 0:
        start_time += pause_duration
```

`engine/scheduler/utils.py` 中的 `PauseGuard.poll_with_pause_compensation()` 已封装了相同逻辑，但 `RemoteExecutor` 未使用。

**修复建议**: 重构为使用 `PauseGuard`，或至少提取为 `RemoteExecutor` 内部的 `_wait_with_pause_compensation()` 方法。

---

### M-3: `MeshingMonitor._monitor_loop` 外层 `except Exception` 静默重试编程错误（`engine/scheduler/meshing_monitor.py` 第 158-175 行）

```python
except (RuntimeError, ValueError, OSError, ConnectionError) as e:
    # 合理的业务异常 → 重试
    ...
except Exception as e:
    # ⚠️ 捕获所有异常，包括 AttributeError、TypeError 等编程错误
    logger.critical(...)
    retry_count = self.state.increment_retry(config_name, "meshing")
    if retry_count < max_retries:
        should_requeue = True  # 编程错误也会被重试！
```

**问题**：`except Exception` 会捕获 `AttributeError`（拼写错误）、`TypeError`（参数类型错误）、`KeyError`（字典键错误）等编程错误，然后静默重试。这些错误重试多少次都不会成功，只会浪费时间并掩盖真正的 bug。

**修复建议**: 移除外层 `except Exception`，让编程错误导致线程崩溃（daemon 日志会记录 `MeshingMonitor` 线程退出），便于发现问题。或者仅对特定的 `Exception` 子类重试。

---

### M-4: `SCProcessPool._send_persistent_command` 中 SCDOC 稳定性检测与 `FileStableDetector` 逻辑重复（`engine/sc_process_pool.py` 第 390+ 行）

`_send_persistent_command` 内部实现了 SCDOC 文件的大小稳定性检测（轮询文件大小，稳定 N 秒后判定完成）。`engine/file_monitor.py` 中的 `FileStableDetector` 类已封装了完全相同的逻辑（`stable_time` + 采样 + 大小不变判定）。

**修复建议**: 在 `SCProcessPool` 中复用 `FileStableDetector` 实例，消除重复实现。

---

### M-5: `_cleanup_remote_files` 持有 SSH 锁期间逐个删除文件（`executor/remote_executor.py` 第 150-162 行）

```python
def _cleanup_remote_files(self, remote_dir, filenames, label):
    with self._ssh_lock:  # ← 整个删除期间持锁
        ssh = self._get_ssh()
        for filename in filenames:
            ssh.delete_remote_file(...)  # 逐个删除
```

**问题**：持锁期间逐个执行 SFTP `remove()` 操作。如果远程文件较多或网络延迟较高，会长时间阻塞 `Transfer` 等并发 SSH 操作。而 `sync_scripts` 的上传逻辑已经改为逐文件持锁/释放锁的策略。

**修复建议**: 与 `sync_scripts` 保持一致——逐文件获取/释放 SSH 锁。

---

### M-6: `IPCServer._handle_client` 中 `buffer` 无大小限制（`ipc/server.py` 第 194 行）

```python
buffer += data  # 无上限
```

**问题**：恶意客户端或异常客户端可以持续发送数据而不包含换行符 `\n`。虽然 `client_sock.settimeout(30.0)` 会在 30 秒后超时，但本地 TCP 带宽下 30 秒可传输数 GB 数据，导致 Daemon 进程 OOM。

**修复建议**: 添加 buffer 大小上限检查：

```python
buffer += data
if len(buffer) > 1_000_000:  # 1MB
    logger.warning(f"[IPC] 客户端 {addr} 消息过长，断开连接")
    break
```

---

### M-7: `handle_start` 中 `paused` 恢复路径未设置 `engine_status("running")`（`engine/daemon.py` 第 332-340 行）

```python
if engine_status == "paused":
    if not self.scheduler.pipeline_alive and not self.scheduler.is_paused:
        self.state.set_engine_status("running")  # ← 此路径设置了
        ...
        return
    # 正常暂停恢复
    self.scheduler.resume()  # ← 此路径未设置 engine_status
    return True, None, "流水线已恢复运行"
```

**问题**：正常暂停恢复路径（`pipeline_alive=True` 或 `is_paused=True`）调用 `self.scheduler.resume()` 但未显式设置 `engine_status("running")`。依赖 `resume()` → `_resume_paused_steps()` 内部的 `self.state.set_engine_status("running")` 来间接设置。如果 `_resume_paused_steps` 因异常提前退出，`engine_status` 将停留在 `paused`。

**修复建议**: 在 `self.scheduler.resume()` 之后显式设置：

```python
self.scheduler.resume()
self.state.set_engine_status("running")  # 确保状态一致
return True, None, "流水线已恢复运行"
```

---

### M-8: `_build_meshing_command` 和 `_build_solver_command` 未校验 `config_name` 类型（`executor/remote_executor.py`）

`execute_meshing()` 和 `execute_solver()` 都有 `isinstance(config_name, int)` 校验，但内部调用的 `_build_meshing_command()` 和 `_build_solver_command()` 本身不做校验。`str(config_name)` 会直接拼入远程命令字符串。

**修复建议**: 在 `_build_*_command` 内部添加校验，或提取为 `_validate_config_name(config_name)` 公共方法。

---

## Low 级别问题

### L-1: `_resume_paused_steps` 中 RUNNING 步骤的孤立检测在 `resume()` 路径中已覆盖

原审查 H-1 提出 `pause()` 不调用 `set_all_running_to_paused()` 可能导致孤儿 Running 步骤。经验证，`resume()` → `_resume_paused_steps()` 第 440-455 行已处理此情况：检查输出文件是否存在，存在则标记完成，否则重置为 Waiting 重新入队。

**结论**: 当前逻辑正确，无需修改。但建议在 `pause()` 方法的 docstring 中明确说明"Running 步骤由 `resume()` 的断点续传扫描处理"。

---

### L-2: `engine/scheduler/utils.py` 使用旧式 `Optional` 导入（第 10 行）

```python
from typing import Optional, Any, Callable
```

项目规范要求 Python 3.10+ 语法。`Optional` 应使用 `X | None`（配合 `from __future__ import annotations`）。

**修复建议**: 移除 `Optional` 导入，使用 `X | None`。

---

### L-3: `barrier.py` 的 `monitor_loop` 暂停期间 `time.sleep(1)` 不响应 `stopped_event`（`engine/scheduler/barrier.py` 第 90 行）

```python
if self._paused.is_set():
    time.sleep(1)  # ← 不检查 stopped_event
    continue
```

`stop()` 在 sleep 期间被调用时，最多延迟 1 秒才能退出循环。影响较小但不规范。

**修复建议**: 改为 `self._stopped.wait(timeout=1.0)` 或使用 `pause_aware_sleep`。

---

### L-4: 13 个文件缺少 `from __future__ import annotations`

按 `python.instructions.md` 规范，新模块应使用延迟注解求值。以下文件缺失：

- `engine/file_monitor.py`
- `engine/scheduler/utils.py`、`retry.py`、`barrier.py`、`sw_phase.py`、`meshing_monitor.py`、`worker_pool.py`、`main.py`
- `utils/logger.py`、`process_utils.py`
- `main.py`、`start_client.py`、`start_daemon.py`

---

## 优先级排序

| # | 编号 | 严重程度 | 修复内容 | 改动量 | 风险 |
|---|------|----------|----------|--------|------|
| 1 | H-2 | High | `start_pipeline` 顶层添加 try/finally | 小 | 低 |
| 2 | H-3 | High | `TaskRunner` 添加公共代理方法 | 小 | 低 |
| 3 | H-5 | High | 提取标志文件路径方法 | 小 | 低 |
| 4 | H-1 | High | `requeue()` 调整 `put`/`task_done` 顺序 | 小 | 低 |
| 5 | M-6 | Medium | IPC buffer 添加大小限制 | 小 | 低 |
| 6 | M-7 | Medium | `handle_start` paused 路径显式设置 status | 小 | 低 |
| 7 | H-4 | Medium | 合并 `execute_meshing`/`start_meshing` | 小 | 低 |
| 8 | M-3 | Medium | 移除 MeshingMonitor 外层 `except Exception` | 小 | 中 |
| 9 | M-1 | Medium | 统一 `execute_transfer` 状态设置策略 | 中 | 中 |
| 10 | M-2 | Medium | 使用 `PauseGuard` 消除轮询重复代码 | 中 | 低 |
| 11 | M-4 | Medium | SCDOC 稳定性检测复用 `FileStableDetector` | 中 | 中 |
| 12 | M-5 | Medium | `_cleanup_remote_files` 改为逐文件持锁 | 小 | 低 |
| 13 | L-2 | Low | 替换旧式 `Optional` 导入 | 小 | 低 |
| 14 | L-3 | Low | `barrier.py` sleep 改为响应 stopped | 小 | 低 |
| 15 | L-4 | Low | 批量添加 `from __future__ import annotations` | 小 | 低 |

---

## 跨语言接口问题（仅记录，不修改非 Python 文件）

### X-1: IPC 命令常量需与 Rust 端保持同步

`ipc/protocol.py` 中的命令常量必须与 `autofluid-tui/src/ipc/protocol.rs` 完全一致。当前已有注释说明此约束，但无自动化验证机制。

**修复建议**: 添加测试用例验证 Python 和 Rust 端的命令常量一致性。
