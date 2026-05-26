# SW→SC→Transfer 统一进程管理框架 — 重构方案

> 状态：草案 | 日期：2026-05-26 | 作者：审查驱动

---

## 目录

1. [背景与动机](#1-背景与动机)
2. [现状分析](#2-现状分析)
3. [框架设计](#3-框架设计)
4. [各步骤适配方案](#4-各步骤适配方案)
5. [实施路径](#5-实施路径)
6. [风险与缓解](#6-风险与缓解)
7. [附录](#7-附录)

---

## 1. 背景与动机

### 1.1 问题

当前 SW（SolidWorks 导出）、SC（SpaceClaim 转换）、Transfer（SFTP 传输）三个步骤各自实现了高度相似的进程管理逻辑，包括：

- 暂停/停止事件检查（`_sc_worker_loop` / `_transfer_worker_loop` / `_execute_sw_macro` 中重复 3 次）
- 断点续传输出文件检查（`_process_sc_step` / `_process_transfer_step` / `_resume_paused_steps` / `meshing_monitor` 中重复 4+ 次）
- 上下游队列衔接（每步的 `_push_downstream` 逻辑一致但写法不同）
- 错误时阻断下游步骤（每步各自实现相似的级联标记逻辑）

这些重复导致：

- **行为不一致风险**：如 SW 暂停后恢复逻辑与 SC 暂停后恢复逻辑存在细微差异（已在上一次审查中修复了部分）
- **维护成本高**：修改暂停/恢复行为需要改动 3+ 处代码
- **新步骤扩展困难**：添加新步骤（如后处理）需要复制大量样板代码

### 1.2 目标

设计一套**统一的步骤执行框架（StepExecutor）**，将"管理逻辑"与"执行逻辑"彻底分离：

- **管理逻辑**（框架统一提供）：槽位分配/释放、状态跟踪、暂停/停止集成、重试、出/入队、断点续传
- **执行逻辑**（各步骤子类实现）：COM 调用、子进程 IPC、SFTP 上传、输入验证、完成检测

---

## 2. 现状分析

### 2.1 三个步骤的核心差异矩阵

| 维度 | SW | SC | Transfer |
|------|:--:|:--:|:--------:|
| 并发上限 | 1 | 3 | 2 |
| 执行接口 | COM Dispatch（同步） | 子进程 + JSON 文件 IPC | SSH/SFTP（网络 I/O） |
| 槽位模型 | 隐式单例（缓存 `sw_app`+`doc`） | 显式槽位池 `PersistentSlot` | 隐式并发令牌（线程数上限） |
| 槽位生命周期 | 会话级：一次创建，处理全部构型 | 持久级：懒创建，跨构型复用 | 无状态：每次执行独立 |
| 输入 | `.SLDPRT` + `.xlsx` | `.step` | 本地 `.scdoc` |
| 输出 | `.step` | `.scdoc` | 远程 `.scdoc` |
| 完成检测 | 本地文件大小稳定 | 本地 mtime + 大小稳定 | 远程文件存在 + 大小校验 |
| 暂停点 | 构型间（COM 不可中断） | 命令发送前 + 轮询中（仅停止） | 上传前 + 上传中（仅停止） |
| 错误恢复 | 清 COM 缓存，重建连接 | kill 进程，重启槽位 | 删半截文件，重试上传 |
| 状态管理 | `RetryManager` | `RetryManager` | `RetryManager` |
| 入队机制 | 无输入队列（遍历构型列表） | `_sc_queue` | `_transfer_queue` |
| 出队机制 | — | → `_transfer_queue` | → `MeshingMonitor` |

### 2.2 已识别重复模式

**模式 A：暂停/停止检查**（出现 3 次）
```python
# worker_pool.py _sc_worker_loop / _transfer_worker_loop / sw_phase.py _execute_sw_macro
if self._paused.is_set():
    time.sleep(1)
    continue
if self._stopped.is_set():
    break
```

**模式 B：断点续传输出检查**（出现 4+ 次）
```python
# worker_pool._process_sc_step / worker_pool._process_transfer_step
# scheduler._resume_paused_steps / meshing_monitor._process_single_meshing
if os.path.exists(output_file) and os.path.getsize(output_file) > 0:
    self.state.set_step_status(cn, step, STATUS_COMPLETED)
    # skip execution
```

**模式 C：上下游队列衔接**（出现 2+ 次）
```python
# worker_pool._process_sc_step / worker_pool._process_transfer_step
self._downstream_queue.put(config_name)
logger.info(f"构型{config_name} {step} 完成，已推入 {next_step} 队列")
```

**模式 D：错误级联阻断**（出现 3+ 次）
```python
# worker_pool / sw_phase / barrier
for downstream_step in ["SC", "Transfer", "Meshing", "Solver"]:
    if self.state.get_step_status(cn, downstream_step) == STATUS_WAITING:
        self.state.set_step_status(cn, downstream_step, STATUS_ERROR, reason)
```

---

## 3. 框架设计

### 3.1 架构总览

```
┌──────────────────────────────────────────────────────────────┐
│                    StepExecutor[ContextT]                     │
│                     （抽象模板方法类）                          │
├──────────────────────────────────────────────────────────────┤
│                                                              │
│  ★ 模板方法（框架提供，不可覆盖）                                │
│  ┌────────────────────────────────────────────────────────┐  │
│  │ run_config(config_name) → bool                         │  │
│  │   ├─ _check_control(BEFORE_ACQUIRE)     [阶段1]        │  │
│  │   ├─ _output_already_exists(config)     [阶段2]        │  │
│  │   ├─ _validate_input(config)            [阶段3 子类]    │  │
│  │   ├─ _slot_mgr.acquire(config)          [阶段4]        │  │
│  │   ├─ _check_control(BEFORE_EXECUTE)     [阶段5]        │  │
│  │   ├─ _execute_in_slot(slot, config)     [阶段6 子类]    │  │
│  │   ├─ _detector.wait(config, ...)        [阶段7]        │  │
│  │   ├─ _check_control(BEFORE_COMMIT)      [阶段8]        │  │
│  │   └─ _slot_mgr.release(slot)            [阶段9]        │  │
│  ├────────────────────────────────────────────────────────┤  │
│  │ scan_and_resume(state) → int           [断点续传扫描]   │  │
│  │ shutdown() / reset() / get_status()                    │  │
│  │ _push_downstream(config)                               │  │
│  │ _on_skip(config) / _on_error(config) / _on_pause(...)  │  │
│  └────────────────────────────────────────────────────────┘  │
│                                                              │
│  ★ 抽象方法（子类必须实现）                                     │
│  ┌────────────────────────────────────────────────────────┐  │
│  │ @property max_slots: int              [SW=1,SC=3,TR=2] │  │
│  │ @property slot_strategy: SlotStrategy                  │  │
│  │ @property step_name: str                               │  │
│  │ @property downstream_step: str | None                  │  │
│  │ _create_slot_context(id) → ContextT                    │  │
│  │ _execute_in_slot(slot, config) → bool                  │  │
│  │ _cleanup_slot_context(ctx)                             │  │
│  │ _validate_input(config) → bool                         │  │
│  └────────────────────────────────────────────────────────┘  │
│                                                              │
│  ★ 组合组件（构造注入）                                        │
│  ┌────────────────────────────────────────────────────────┐  │
│  │ SlotManager[ContextT]        — 槽位生命周期管理          │  │
│  │ CompletionDetector           — 输出完成检测策略          │  │
│  │ ControlEvents                — 暂停/停止事件封装         │  │
│  │ RetryManager                 — 重试管理器（共享实例）     │  │
│  └────────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────┘
```

### 3.2 槽位策略 — 框架核心创新

三种步骤的本质差异在于槽位的**生命周期语义**不同。框架通过 `SlotStrategy` 枚举统一：

```python
class SlotStrategy(Enum):
    SESSION = "session"       # 会话级：一个槽位处理所有构型
    PERSISTENT = "persistent" # 持久级：槽位懒创建，跨构型复用
    STATELESS = "stateless"   # 无状态：槽位只是并发令牌
```

| 策略 | 适用步骤 | acquire() 行为 | release() 行为 |
|------|----------|---------------|---------------|
| `SESSION` | SW | 首次调用创建上下文；后续返回同一槽位 | 所有构型完成后调用一次 |
| `PERSISTENT` | SC | 找空闲槽位或创建新槽位 | 状态切回 `ready`，进程存活 |
| `STATELESS` | Transfer | 获取 `Semaphore` 令牌 | 释放 `Semaphore` 令牌 |

```python
class Slot(Generic[ContextT]):
    """泛型槽位 — 框架内部使用"""
    slot_id: int
    status: SlotStatus          # IDLE | STARTING | READY | BUSY
    current_config: int | None
    context: ContextT           # 子类定义的上下文对象
    created_at: float
    configs_processed: int
```

**SlotManager 核心方法**：

```python
class SlotManager(Generic[ContextT], ABC):
    """槽位管理器 — 框架统一管理槽位生命周期"""

    def __init__(self, max_slots: int, strategy: SlotStrategy,
                 factory: Callable[[int], ContextT],
                 cleanup: Callable[[ContextT], None]):
        ...

    def acquire(self, config_name: int) -> Slot[ContextT] | None:
        """获取可用槽位。槽位满返回 None。按 strategy 决定复用/新建逻辑。"""

    def release(self, slot: Slot[ContextT]) -> None:
        """释放槽位。按 strategy 决定归还/销毁逻辑。"""

    def cleanup_dead_slots(self) -> int:
        """检测并清理已死亡进程的槽位。返回清理数量。"""

    def shutdown_all(self) -> None:
        """强制终止所有槽位进程。"""

    def reset(self) -> None:
        """重置所有槽位状态（终止进程 + 清空槽位表）。"""

    @property
    def active_count(self) -> int:
        """当前活跃槽位数（STARTING + READY + BUSY 中进程存活者）。"""
```

### 3.3 完成检测器 — 策略接口

```python
class CompletionDetector(ABC):
    """输出完成检测器 — 三种策略一个接口"""

    @abstractmethod
    def wait(
        self,
        config_name: int,
        context: Any,              # 子类上下文（如 slot.process）
        command_sent_at: float,    # 命令发送时刻（mtime 过滤用）
        deadline: float,           # 超时截止时间
        control: "ControlEvents",  # 暂停/停止事件
    ) -> bool:
        """阻塞等待直到输出完成或超时。返回 True 表示完成。"""
        ...

    @abstractmethod
    def output_exists(self, config_name: int) -> bool:
        """检查输出文件是否已存在（用于断点续传）。"""
        ...


class LocalFileStableDetector(CompletionDetector):
    """SW 步骤：本地文件大小稳定检测（复用现有 FileStableDetector）"""


class ScdocMtimeDetector(CompletionDetector):
    """SC 步骤：本地文件 mtime ≥ 命令时刻 + 大小稳定检测"""


class RemoteFileDetector(CompletionDetector):
    """Transfer 步骤：SSH 远程文件存在 + 大小校验"""
```

### 3.4 控制事件 — 标准化检查点

```python
class Checkpoint(Enum):
    BEFORE_ACQUIRE = auto()    # ① 获取槽位前
    AFTER_ACQUIRE = auto()     # ② 获取槽位后，执行前
    DURING_POLL = auto()       # ③ 完成检测轮询中
    BEFORE_COMMIT = auto()     # ④ 状态提交前


class ControlEvents:
    """暂停/停止事件封装 — 统一检查语义"""

    def __init__(self, paused: threading.Event, stopped: threading.Event):
        self._paused = paused
        self._stopped = stopped

    def check(self, checkpoint: Checkpoint) -> bool:
        """
        检查控制事件。
        - BEFORE_ACQUIRE/AFTER_ACQUIRE/BEFORE_COMMIT: 暂停时阻塞等待
        - DURING_POLL: 暂停时仅记录日志，不中断；停止时立即中止
        Returns: True = 可继续, False = 应中止
        """

    def is_paused(self) -> bool: ...
    def is_stopped(self) -> bool: ...
    def wait_if_paused(self) -> bool: ...
```

**各步骤检查点声明**：

| Checkpoint | SW | SC | Transfer |
|------------|:--:|:--:|:--------:|
| `BEFORE_ACQUIRE` | ✅ 阻塞 | ✅ 阻塞 | ✅ 阻塞 |
| `AFTER_ACQUIRE` | ✅ 阻塞 | ✅ 阻塞 | ✅ 阻塞 |
| `DURING_POLL` | ❌ 不适用（COM 同步） | ✅ 仅停止中断 | ✅ 仅停止中断 |
| `BEFORE_COMMIT` | ✅ 阻断（不标完成） | ✅ 阻断 | ✅ 阻断 |

### 3.5 `run_config()` 模板方法 — 完整流程

```
run_config(config_name)
│
├─ [1] ControlEvents.check(BEFORE_ACQUIRE)
│      └─ 暂停 → 阻塞等待；停止 → return False
│
├─ [2] CompletionDetector.output_exists(config_name)
│      └─ 已存在 → _on_skip() → return True
│
├─ [3] self._validate_input(config_name)          ← 子类实现
│      └─ 失败 → return False
│
├─ [4] SlotManager.acquire(config_name)
│      └─ 无可用槽位 → return False
│
├─ [5] ControlEvents.check(AFTER_ACQUIRE)
│      └─ 暂停/停止 → return False（槽位在 finally 中释放）
│
├─ [6] self._execute_in_slot(slot, config_name)   ← 子类实现
│      └─ 记录 command_sent_at
│      └─ 失败 → return False
│
├─ [7] CompletionDetector.wait(config, ctx, command_sent_at, deadline, control)
│      └─ 超时/停止 → return False
│
├─ [8] ControlEvents.check(BEFORE_COMMIT)
│      └─ 暂停 → _on_pause_during_commit() → return False
│
├─ [9] SlotManager.release(slot)                  ← finally 块
│
└─ return True
```

### 3.6 `scan_and_resume()` — 统一断点续传

```python
def scan_and_resume(self, state: StateManager) -> int:
    """
    扫描 DB，将待处理构型入队。
    替代当前分散在 _resume_paused_steps / _scan_db_for_pending /
    scan_completed_downstream 中的重复逻辑。

    Returns: 入队构型数量
    """
    enqueued = 0
    for cn in state.get_all_configs():
        # ① 上游是否完成？
        if not self._upstream_completed(state, cn):
            continue

        st = state.get_step_status(cn, self.step_name)

        # ② 已完成 → 跳过
        if st == STATUS_COMPLETED:
            continue

        # ③ 孤儿 RUNNING → 输出存在则完成，否则重置
        if st == STATUS_RUNNING:
            if self._detector.output_exists(cn):
                state.set_step_status(cn, self.step_name, STATUS_COMPLETED)
                self._push_downstream(cn)
                continue
            state.set_step_status(cn, self.step_name, STATUS_WAITING)

        # ④ PAUSED / ERROR / WAITING → 输出存在则完成，否则入队
        if st in (STATUS_PAUSED, STATUS_WAITING, STATUS_ERROR, STATUS_RETRYING):
            if self._detector.output_exists(cn):
                state.set_step_status(cn, self.step_name, STATUS_COMPLETED)
                self._push_downstream(cn)
                continue
            self.input_queue.put(cn)
            enqueued += 1

    return enqueued
```

### 3.7 上下游衔接统一

```python
class StepExecutor(ABC):
    @property
    @abstractmethod
    def downstream_step(self) -> str | None:
        """声明下游步骤名。无下游返回 None。"""
        ...

    def _push_downstream(self, config_name: int) -> None:
        """完成当前步骤后，推入下游队列。框架统一实现。"""
        if self.downstream_step is None:
            return
        self._downstream_queues[self.downstream_step].put(config_name)
        logger.info(
            f"[{self.step_name}] 构型{config_name} 完成，"
            f"已推入 {self.downstream_step} 队列"
        )
```

**步骤链声明**：

```python
# SW → SC → Transfer → Meshing → Solver → None
SW.step_name      = "SW"        SW.downstream_step = "SC"
SC.step_name      = "SC"        SC.downstream_step = "Transfer"
Transfer.step_name = "Transfer"  Transfer.downstream_step = "Meshing"
# Meshing/Solver 暂不纳入本次重构范围
```

### 3.8 WorkerPoolManager 泛化

```python
class UnifiedWorkerPool:
    """通用工作线程池 — 替换当前硬编码 SC/Transfer 双循环"""

    def __init__(
        self,
        executors: dict[str, StepExecutor],
        paused: threading.Event,
        stopped: threading.Event,
    ):
        self._executors = executors
        self._paused = paused
        self._stopped = stopped
        self._threads: list[threading.Thread] = []

    def start_all(self) -> None:
        """为每个步骤启动 max_slots 个工作线程"""
        for name, executor in self._executors.items():
            for i in range(executor.max_slots):
                t = threading.Thread(
                    target=self._worker_loop,
                    args=(executor,),
                    name=f"{name}Worker-{i+1}",
                    daemon=True,
                )
                t.start()
                self._threads.append(t)

    def start_if_needed(self) -> None:
        """幂等启动：仅在无活跃线程时创建"""
        alive = [t for t in self._threads if t.is_alive()]
        self._threads = alive
        if not alive:
            self.start_all()

    def join_all(self, timeout: float = 3.0) -> None:
        """等待所有工作线程退出"""
        for t in self._threads:
            if t.is_alive():
                t.join(timeout=timeout)

    def _worker_loop(self, executor: StepExecutor) -> None:
        """通用 Worker 循环 [替换 _sc_worker_loop + _transfer_worker_loop]"""
        logger.info(f"[{executor.step_name}Worker] 启动")

        while not self._stopped.is_set():
            if self._paused.is_set():
                time.sleep(1)
                continue

            try:
                config_name = executor.input_queue.get(timeout=1)
            except queue.Empty:
                continue

            logger.info(
                f"[{executor.step_name}Worker] 开始处理构型{config_name}"
            )

            try:
                success = executor.run_config(config_name)
                if success:
                    executor._push_downstream(config_name)
            except Exception as e:
                logger.error(
                    f"[{executor.step_name}Worker] 处理构型{config_name} "
                    f"异常: {e}", exc_info=True
                )
                # 统一错误处理：标记步骤 Error + 阻断下游
                executor._on_error(config_name, str(e))
            finally:
                executor.input_queue.task_done()

        logger.info(f"[{executor.step_name}Worker] 退出")
```

---

## 4. 各步骤适配方案

### 4.1 SW → `SwStepExecutor`

```python
class SwStepExecutor(StepExecutor[ComContext]):
    """SolidWorks STEP 导出执行器"""

    max_slots = 1
    slot_strategy = SlotStrategy.SESSION
    step_name = "SW"
    downstream_step = "SC"

    # --- 上下文 ---
    # ComContext = namedtuple("ComContext", ["sw_app", "doc", "com_initialized"])

    def _create_slot_context(self, slot_id: int) -> ComContext:
        """建立 COM 连接 + 打开模型 + 导入设计表（三层降级策略）"""
        import pythoncom
        pythoncom.CoInitialize()
        sw_app = self._connect_sw()           # 复用现有 _connect_sw()
        doc = self._open_sw_model(sw_app, ...) # 复用现有 _open_sw_model()
        self._import_design_table_with_retry(doc, ...)
        return ComContext(sw_app, doc, True)

    def _execute_in_slot(self, slot: Slot[ComContext], config_name: int) -> bool:
        """切换构型 → 重建 → SaveAs STEP（复用现有 export_sw_per_config）"""
        # 直接委托给现有的 export_sw_per_config 逻辑
        return self._export_single_config(slot.context, config_name)

    def _cleanup_slot_context(self, ctx: ComContext) -> None:
        """断开 SW + 释放 COM（复用现有 _disconnect_sw）"""
        self._disconnect_sw(ctx.sw_app, ctx.doc, self._sw_model)

    def _validate_input(self, config_name: int) -> bool:
        """检查 SLDPRT + Excel + STEP 目录"""
        ...
```

**适配要点**：
- SW 的 `SESSION` 策略意味着 `acquire()` 首次创建上下文后，后续 `run_config()` 调用复用同一个 `slot`
- `_execute_in_slot()` 每次切换构型并导出，不重建连接
- `release()` 在 `_execute_sw_macro()` 全部完成后由调用方显式触发
- 暂停检查嵌入在 `export_sw_per_config()` 已有的 `_paused_event` 检查点

### 4.2 SC → `ScStepExecutor`

```python
class ScStepExecutor(StepExecutor[SubprocessContext]):
    """SpaceClaim 转换执行器"""

    max_slots = 3
    slot_strategy = SlotStrategy.PERSISTENT
    step_name = "SC"
    downstream_step = "Transfer"

    # --- 上下文 ---
    # SubprocessContext = namedtuple("SubprocessContext",
    #     ["process", "pid", "cmd_dir", "slot_id"])

    def _create_slot_context(self, slot_id: int) -> SubprocessContext:
        """启动 Bridge --persistent → 等待 ready 文件"""
        # 复用现有 _launch_persistent_process + _wait_for_slot_ready
        ...

    def _execute_in_slot(self, slot: Slot[SubprocessContext], config_name: int) -> bool:
        """写命令 JSON 文件"""
        # 复用现有命令文件写入逻辑
        ...

    def _cleanup_slot_context(self, ctx: SubprocessContext) -> None:
        """发 quit 命令 → 等退出 → 兜底 taskkill"""
        ...

    def _validate_input(self, config_name: int) -> bool:
        """检查 STEP 文件 + SC exe + 脚本 + SCDOC 目录"""
        ...
```

**适配要点**：
- `PERSISTENT` 策略使 `SlotManager` 完全替代当前 `SCProcessPool` 的槽位管理
- 首次清理（`_first_cleanup_done`）逻辑移入 `SlotManager.acquire()` 首次调用时
- 死进程检测和重启由 `SlotManager.cleanup_dead_slots()` 统一处理

### 4.3 Transfer → `TransferStepExecutor`

```python
class TransferStepExecutor(StepExecutor[None]):
    """SFTP 文件传输执行器"""

    max_slots = 2
    slot_strategy = SlotStrategy.STATELESS
    step_name = "Transfer"
    downstream_step = "Meshing"

    def _create_slot_context(self, slot_id: int) -> None:
        return None  # STATELESS 策略不需要上下文

    def _execute_in_slot(self, slot: Slot[None], config_name: int) -> bool:
        """SFTP 上传 SCDOC"""
        # 复用现有 execute_transfer 逻辑
        ...

    def _cleanup_slot_context(self, ctx: None) -> None:
        pass

    def _validate_input(self, config_name: int) -> bool:
        """检查本地 SCDOC + 可选远程预检"""
        ...
```

**适配要点**：
- `STATELESS` 策略使 `SlotManager` 退化为纯 `Semaphore(max_slots)` 并发控制
- `acquire()` = `semaphore.acquire()`；`release()` = `semaphore.release()`
- Transfer 是最轻量的适配，基本不需要改动执行逻辑

---

## 5. 实施路径

### 5.1 六阶段渐进式重构

```
Phase 1 ──▶ 提取公共基类（不改现有行为）
  │         新增文件:
  │           engine/executor/
  │             __init__.py
  │             base.py              ← StepExecutor ABC + Slot + SlotStatus
  │             slot_manager.py      ← SlotManager ABC + 三种策略实现
  │             completion.py        ← CompletionDetector ABC + 三种实现
  │             control.py           ← ControlEvents + Checkpoint
  │         预计: 2-3 天 | 风险: 低（纯新增，零回归）
  │
Phase 2 ──▶ SC 步骤率先适配
  │         新增: engine/executor/sc_executor.py ← ScStepExecutor
  │         修改: engine/sc_process_pool.py     ← 委托给 ScStepExecutor
  │         验证: 现有 SC 测试全部通过 + 新增 ScStepExecutor 单测
  │         预计: 3-4 天 | 风险: 中
  │
Phase 3 ──▶ Transfer 步骤适配
  │         新增: engine/executor/transfer_executor.py ← TransferStepExecutor
  │         修改: executor/remote_executor.py          ← execute_transfer 委托
  │         验证: STATELESS 策略 + Semaphore 并发
  │         预计: 1-2 天 | 风险: 低
  │
Phase 4 ──▶ WorkerPoolManager 泛化
  │         新增: engine/executor/worker_pool.py ← UnifiedWorkerPool
  │         修改: engine/scheduler/worker_pool.py ← 退役 _sc_worker_loop
  │                                                + _transfer_worker_loop
  │         修改: engine/scheduler/main.py       ← 注入 UnifiedWorkerPool
  │         验证: 全量测试（175 个）+ 手动集成测试
  │         预计: 2-3 天 | 风险: 中
  │
Phase 5 ──▶ SW 步骤适配
  │         新增: engine/executor/sw_executor_v2.py ← SwStepExecutor
  │         修改: executor/sw_executor.py           ← 委托 + 逐步退役旧代码
  │         修改: engine/scheduler/sw_phase.py      ← 适配 SESSION 策略
  │         验证: SESSION 策略 + COM 单线程 + 暂停/恢复全场景
  │         预计: 3-5 天 | 风险: 高（COM 特殊性）
  │
Phase 6 ──▶ 清理退役代码 + 测试加固
  │         删除: worker_pool._sc_worker_loop / _transfer_worker_loop
  │         删除: sc_process_pool 槽位管理逻辑（保留 Bridge 启动逻辑）
  │         删除: sw_phase._execute_sw_macro 中的状态管理（保留 SW 执行）
  │         预计: 1-2 天 | 风险: 低
```

### 5.2 每个 Phase 的质量门禁

| Phase | ruff | mypy | pytest | 集成测试 |
|-------|:----:|:----:|:------:|:--------:|
| 1 | ✅ | ✅ | 全量 | — |
| 2 | ✅ | ✅ | 全量 + 新增 | SC 手动 |
| 3 | ✅ | ✅ | 全量 + 新增 | Transfer 手动 |
| 4 | ✅ | ✅ | 全量 | SW+SC+Transfer 联动 |
| 5 | ✅ | ✅ | 全量 | SW 暂停/恢复/重试 |
| 6 | ✅ | ✅ | 全量 | 全场景回归 |

### 5.3 文件变更预估

| 文件 | Phase | 操作 | 行数变化 |
|------|:-----:|------|:--------:|
| `engine/executor/__init__.py` | 1 | 新增 | ~10 |
| `engine/executor/base.py` | 1 | 新增 | ~200 |
| `engine/executor/slot_manager.py` | 1 | 新增 | ~180 |
| `engine/executor/completion.py` | 1 | 新增 | ~150 |
| `engine/executor/control.py` | 1 | 新增 | ~80 |
| `engine/executor/sc_executor.py` | 2 | 新增 | ~100 |
| `engine/sc_process_pool.py` | 2 | 修改 | -200 / +30 |
| `engine/executor/transfer_executor.py` | 3 | 新增 | ~80 |
| `executor/remote_executor.py` | 3 | 修改 | -20 / +10 |
| `engine/executor/worker_pool.py` | 4 | 新增 | ~120 |
| `engine/scheduler/worker_pool.py` | 4 | 修改 | -300 / +20 |
| `engine/scheduler/main.py` | 4,5 | 修改 | -50 / +30 |
| `engine/executor/sw_executor_v2.py` | 5 | 新增 | ~150 |
| `executor/sw_executor.py` | 5 | 修改 | -50 / +20 |
| `engine/scheduler/sw_phase.py` | 5 | 修改 | -80 / +30 |
| `tests/` | 全 | 新增/修改 | ~200 |
| **合计** | | | **~+1090 / -580** |

---

## 6. 风险与缓解

| 风险 | 等级 | 缓解措施 |
|------|:--:|----------|
| **SW COM 单线程约束** | 🔴 高 | Phase 5 最后做；提前编写 COM mock 测试；保留旧代码开关 |
| **175 个测试回归** | 🟡 中 | 每 Phase 后运行全量测试；Phase 间可独立 revert |
| **SC 常驻进程稳定性** | 🟡 中 | Phase 2 保留 `SCProcessPool` 作为 fallback；灰度切换 |
| **过度抽象** | 🟡 中 | Transfer 的 STATELESS 作为"极简适配"基准；若 Phase 3 发现过度复杂即中止 |
| **调试复杂度** | 🟡 中 | 模板方法增加调用栈深度，需在每个阶段添加结构化日志 |
| **暂停行为回归** | 🟡 中 | `ControlEvents` 的单元测试覆盖所有检查点 + 暂停/停止组合 |

### 6.1 中止条件

在任一 Phase 满足以下条件之一时，暂停重构并回退：

1. 净增代码超过预估的 150% 且无明显质量收益
2. 某个步骤的测试通过率低于 95%
3. `StepExecutor` 抽象层对 Transfer 步骤增加了 >50 行额外代码

---

## 7. 附录

### 7.1 关键类型定义速查

```python
# === 槽位状态 ===
class SlotStatus(Enum):
    IDLE = "idle"
    STARTING = "starting"
    READY = "ready"
    BUSY = "busy"

# === 槽位策略 ===
class SlotStrategy(Enum):
    SESSION = "session"
    PERSISTENT = "persistent"
    STATELESS = "stateless"

# === 检查点 ===
class Checkpoint(Enum):
    BEFORE_ACQUIRE = auto()
    AFTER_ACQUIRE = auto()
    DURING_POLL = auto()
    BEFORE_COMMIT = auto()

# === 步骤链 ===
STEP_CHAIN = ["SW", "SC", "Transfer", "Meshing", "Solver"]
```

### 7.2 现有代码到新框架的映射

| 现有类/方法 | 映射到 |
|------------|--------|
| `SCProcessPool` | `ScStepExecutor` + `SlotManager(PERSISTENT, max=3)` |
| `SWExecutor.export_sw_per_config()` | `SwStepExecutor._execute_in_slot()` |
| `RemoteExecutor.execute_transfer()` | `TransferStepExecutor._execute_in_slot()` |
| `WorkerPoolManager._sc_worker_loop()` | `UnifiedWorkerPool._worker_loop(ScStepExecutor)` |
| `WorkerPoolManager._transfer_worker_loop()` | `UnifiedWorkerPool._worker_loop(TransferStepExecutor)` |
| `PipelineScheduler._resume_paused_steps()` | `StepExecutor.scan_and_resume()` × 3 |
| `SWPhaseHandler.scan_completed_downstream()` | `StepExecutor.scan_and_resume()` |
| `MeshingMonitor._scan_db_for_pending()` | `StepExecutor.scan_and_resume()` |
| `FileStableDetector` | `LocalFileStableDetector(CompletionDetector)` |
| SC mtime 轮询逻辑 | `ScdocMtimeDetector(CompletionDetector)` |
| Transfer 远程文件检查 | `RemoteFileDetector(CompletionDetector)` |

### 7.3 相关文档

- `docs/code-style-guide.md` — 项目代码风格规范
- `docs/roadmap.md` — 项目路线图
- `.github/instructions/python.instructions.md` — Python 代码规范
- `.github/instructions/implementation-planning.instructions.md` — 实施规划规范
