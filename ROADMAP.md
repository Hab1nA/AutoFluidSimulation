# AutoFluid 远期改进计划：从单机架构到分布式三层架构

> 文档版本：v1.0  
> 创建日期：2026-05-09  
> 适用项目：液氧甲烷火箭发动机仿真流水线系统 (AutoFluid v2.1.0)

---

## 目录

1. [改进背景与目标](#1-改进背景与目标)
2. [当前架构全景](#2-当前架构全景)
3. [目标架构设计](#3-目标架构设计)
4. [改造项详细设计](#4-改造项详细设计)
   - 4.1 [Daemon 拆分与迁移](#41-daemon-拆分与迁移)
   - 4.2 [多工作站支持](#42-多工作站支持)
   - 4.3 [屏障机制适配](#43-屏障机制适配)
   - 4.4 [后处理阶段 (PostProcess)](#44-后处理阶段-postprocess)
   - 4.5 [结果收集阶段 (Collect)](#45-结果收集阶段-collect)
5. [全项目影响分析](#5-全项目影响分析)
6. [代码改动量与构建复杂度评估](#6-代码改动量与构建复杂度评估)
7. [潜在 Bug 与风险清单](#7-潜在-bug-与风险清单)
8. [实施路线图](#8-实施路线图)

---

## 1. 改进背景与目标

### 1.1 当前痛点

当前系统采用 Client + Daemon 单机架构，全部运行在本地 Windows PC 上。这带来三个核心问题：

1. **流水线无法脱离本地电脑**：Daemon 运行在本地 PC 上，PC 关机则流水线中断
2. **单工作站瓶颈**：所有网格划分和仿真求解串行排队在一台工作站上，无法利用多机并行
3. **结果分散**：仿真算例散落在远程工作站上，无自动回收机制

### 1.2 改进目标

| 目标 | 描述 |
|------|------|
| **Daemon 持久化** | 将 Daemon 迁移到可持久运行的 Ubuntu 服务器 A (2核4GB)，本地 PC 仅运行前端 |
| **多工作站并行** | 服务器 A 同时控制工作站 A/B/C，网格划分和仿真求解以三线程方式并行执行 |
| **结果自动回收** | 后处理完成后自动回传结果，本地 PC 上线时补收离线期间产生的数据 |

---

## 2. 当前架构全景

### 2.1 架构图

```
┌─────────────────────────────────────────────────────────────────┐
│                     本地 Windows PC                              │
│                                                                 │
│  ┌──────────────────┐      ┌────────────────────────────────┐   │
│  │   TUI Client      │      │  Daemon (PipelineDaemon)       │   │
│  │  ┌──────────────┐ │      │  ┌──────────────────────────┐ │   │
│  │  │ Rust TUI     │ │ IPC  │  │ IPCServer (TCP :9527)    │ │   │
│  │  │ (ratatui)    │◄├──────►│  ├──────────────────────────┤ │   │
│  │  └──────────────┘ │ JSON │  │ StateManager (SQLite WAL) │ │   │
│  └──────────────────┘      │  ├──────────────────────────┤ │   │
│                             │  │ PipelineScheduler         │ │   │
│                             │  ├──────────────────────────┤ │   │
│                             │  │ TaskRunner                │ │   │
│                             │  │  ├─ SW (win32com COM)    │ │   │
│                             │  │  ├─ SC (subprocess)      │ │   │
│                             │  │  ├─ Transfer (paramiko)  │ │   │
│                             │  │  ├─ Meshing (SSH)        │ │   │
│                             │  │  └─ Solver (SSH)         │ │   │
│                             │  ├──────────────────────────┤ │   │
│                             │  │ StepFileMonitor           │ │   │
│                             │  │  └─ 轮询本地 step_dir     │ │   │
│                             │  └──────────────────────────┘ │   │
│                             └──────────────┬─────────────────┘   │
└────────────────────────────────────────────┼────────────────────┘
                                             │ SSH (paramiko)
                                             ▼
                                  ┌───────────────────────┐
                                  │  工作站 (Windows 22H2) │
                                  │  ├─ batch_meshing      │
                                  │  ├─ batch_solver       │
                                  │  └─ 标志文件轮询        │
                                  └───────────────────────┘
```

### 2.2 流水线阶段

```
SW → SC → Transfer → Meshing → Solver
```

| 阶段 | 执行位置 | 技术手段 | 并发模型 |
|------|---------|---------|---------|
| SW | 本地 PC | win32com COM API | 批量串行（一次宏导出所有构型） |
| SC | 本地 PC | subprocess 无头调用 SpaceClaim | 流水线并发（3 Worker） |
| Transfer | 本地 PC → 工作站 | paramiko SFTP | 流水线并发（3 Worker） |
| Meshing | 远程工作站 | SSH + PowerShell Start-Process | 流水线并发（3 Worker） |
| Solver | 远程工作站 | SSH + PowerShell Start-Process | 全局屏障后并行启动 |

### 2.3 关键代码模块清单

| 模块 | 文件 | 行数 | 核心职责 |
|------|------|------|---------|
| PipelineDaemon | `engine/daemon.py` | ~390 | 后台守护进程，协调所有子系统 |
| PipelineScheduler | `engine/scheduler.py` | ~980 | DAG 调度、全局屏障、Worker 线程池 |
| TaskRunner | `engine/task_runner.py` | ~1680 | 各阶段具体执行逻辑 |
| StateManager | `engine/state_manager.py` | ~500+ | SQLite WAL 持久化状态 |
| StepFileMonitor | `engine/file_monitor.py` | ~365 | STEP 文件写入完成检测 |
| IPCServer | `ipc/server.py` | ~225 | TCP Socket 监听与命令分发 |
| IPCProtocol | `ipc/protocol.py` | ~120 | JSON over TCP 消息协议 |
| RemoteWorkstation | `utils/ssh_client.py` | ~415 | paramiko SSH 封装 |
| Config | `engine/config.py` | ~250 | 全局硬编码配置 |
| IpcClient (Rust) | `autofluid-tui/src/ipc/client.rs` | ~80 | Rust tokio TCP 客户端 |
| IpcProtocol (Rust) | `autofluid-tui/src/ipc/protocol.rs` | ~70 | Rust 端协议定义 |
| AppState (Rust) | `autofluid-tui/src/state/app_state.rs` | ~200+ | Rust 端状态管理 |
| Table UI (Rust) | `autofluid-tui/src/ui/table.rs` | ~60 | Rust 端表格渲染 |
| DaemonManager (Rust) | `autofluid-tui/src/daemon_mgr.rs` | ~70 | Rust 端 Daemon 进程管理 |
| Main (Rust) | `autofluid-tui/src/main.rs` | ~200 | Rust TUI 主循环 |
| Command (Rust) | `autofluid-tui/src/event_handler/command.rs` | ~200+ | Rust 端命令处理 |
| Main Entry | `main.py` | ~455 | 总控程序入口 |
| StartDaemon | `start_daemon.py` | ~35 | Daemon 启动脚本 |
| StartClient | `start_client.py` | ~67 | Client 启动脚本 |

---

## 3. 目标架构设计

### 3.1 三层分布式架构

```
┌──────────────────┐         ┌──────────────────────────────────────────────┐
│  本地 Windows PC  │         │          服务器 A (Ubuntu 22.04, 2核4GB)      │
│   (前端 + 本地任务) │         │              (后端调度核心)                    │
│                   │         │                                              │
│  ┌─────────────┐  │  IPC   │  ┌────────────────────────────────────────┐  │
│  │  TUI Client  │◄├────────►│  │  Daemon (PipelineDaemon)               │  │
│  │  (Rust/Py)   │  │ (TCP)  │  │  ├─ IPCServer (0.0.0.0:9527)          │  │
│  └─────────────┘  │         │  │  ├─ StateManager (SQLite WAL)          │  │
│                   │         │  │  ├─ PipelineScheduler                   │  │
│  ┌─────────────┐  │  RPC   │  │  │  ├─ Worker 线程池                    │  │
│  │ LocalWorker  │◄├────────►│  │  │  ├─ Per-WS BarrierMonitor ×2       │  │
│  │ ├─ SW (COM)  │  │       │  │  │  └─ Solver 线程池                    │  │
│  │ ├─ SC (sub)  │  │       │  │  ├─ TaskRunner (多WS版)                 │  │
│  │ └─ Monitor   │  │       │  │  │  ├─ SSH 连接池 (3条)                 │  │
│  └─────────────┘  │       │  │  │  ├─ PostProcess 执行                  │  │
│                   │       │  │  │  └─ ResultCollector                   │  │
│  ┌─────────────┐  │       │  │  ├─ StepFileMonitor (远程上报模式)        │  │
│  │ ResultFetcher│◄├───────►│  │  └─ StagingManager (结果暂存)            │  │
│  │ (结果拉取)    │  │       │  └────────────────────────────────────────┘  │
│  └─────────────┘  │       │  ┌────────────────────────────────────────┐  │
│                   │       │  │  暂存区 /var/lib/autofluid/staging/      │  │
│                   │       │  │  ├─ WS-A/  (工作站A的后处理结果)          │  │
│                   │       │  │  ├─ WS-B/  (工作站B的后处理结果)          │  │
│                   │       │  │  └─ WS-C/  (工作站C的后处理结果)          │  │
│                   │       │  └────────────────────────────────────────┘  │
└───────────────────┘       └──┬────────────┬────────────┬─────────────────┘
                              │SSH         │SSH         │SSH
                              ▼            ▼            ▼
                       ┌──────────┐ ┌──────────┐ ┌──────────┐
                       │ 工作站 A  │ │ 工作站 B  │ │ 工作站 C  │
                       │(Windows) │ │(Windows) │ │(Windows) │
                       │ mesh+sol │ │ mesh+sol │ │ mesh+sol │
                       │ postproc │ │ postproc │ │ postproc │
                       └──────────┘ └──────────┘ └──────────┘
```

### 3.2 扩展后的流水线

```
SW → SC → Transfer → Meshing → Solver → PostProcess → Collect
```

| 阶段 | 执行位置 | 触发条件 | 并发模型 |
|------|---------|---------|---------|
| SW | 本地 PC | 用户启动 | 批量串行 |
| SC | 本地 PC | STEP 文件就绪 | 流水线并发 |
| Transfer | 本地 PC → 服务器 A → 工作站 | SC 完成 | 流水线并发 |
| Meshing | 工作站 A/B/C | Transfer 完成 | 工作站级并行 |
| Solver | 工作站 A/B/C | **工作站级屏障**通过 | 工作站级并行 |
| PostProcess | 工作站 A/B/C | **工作站级 Solver 屏障**通过 | 每站一个任务 |
| Collect | 服务器 A → 本地 PC | 本地 PC 上线 | 异步拉取 |

### 3.3 文件流转路径

```
当前:
  本地PC: SW→STEP → SC→SCDOC ─SFTP─► 工作站: Meshing→Solver

改造后:
  本地PC: SW→STEP → SC→SCDOC ─RPC─► 服务器A ─SFTP─► 工作站A/B/C
  工作站A/B/C: Meshing → Solver → PostProcess
  工作站A/B/C ─SFTP─► 服务器A(暂存) ─推送─► 本地PC(归档)
```

---

## 4. 改造项详细设计

### 4.1 Daemon 拆分与迁移

#### 4.1.1 拆分原则

当前 `PipelineDaemon` 同时承担**调度编排**和**本地任务执行**两个职责。拆分后：

| 组件 | 迁移到服务器 A | 保留在本地 PC |
|------|:---:|:---:|
| `PipelineDaemon`（主控） | ✅ | |
| `StateManager`（SQLite） | ✅ | |
| `PipelineScheduler` | ✅ | |
| `IPCServer` | ✅ | |
| `TaskRunner`（远程部分：Transfer/Meshing/Solver） | ✅ | |
| `TaskRunner`（本地部分：SW/SC） | | ✅ |
| `StepFileMonitor` | | ✅（改造为远程上报模式） |
| `TUI Client` | | ✅ |
| **新增** `LocalWorker` | | ✅ |
| **新增** `ResultFetcher` | | ✅ |

#### 4.1.2 本地 PC 新增 LocalWorker 进程

LocalWorker 是本地 PC 上的独立进程，负责：

1. 执行 SW 阶段（win32com COM API）
2. 执行 SC 阶段（subprocess 无头调用 SpaceClaim）
3. 运行 StepFileMonitor，检测 STEP 文件写入完成
4. 上传 SCDOC 文件到服务器 A
5. 上线后拉取后处理结果

LocalWorker 与服务器 A 的 Daemon 之间通过 RPC 通信（复用现有 IPC JSON-over-TCP 协议），新增以下命令：

| 命令 | 方向 | 用途 |
|------|------|------|
| `worker_register` | LocalWorker → Daemon | Worker 上线注册，报告本地 PC 能力 |
| `worker_heartbeat` | LocalWorker → Daemon | 心跳保活（每 30 秒） |
| `worker_step_complete` | LocalWorker → Daemon | 上报步骤完成（SW/SC） |
| `worker_step_error` | LocalWorker → Daemon | 上报步骤执行失败 |
| `worker_execute` | Daemon → LocalWorker | 下发执行指令（SW/SC） |
| `worker_file_ready` | LocalWorker → Daemon | 上报 STEP 文件就绪 |
| `worker_scdoc_uploaded` | LocalWorker → Daemon | 上报 SCDOC 已上传到服务器 A |
| `collect_results` | LocalWorker → Daemon | 请求拉取暂存结果 |
| `collect_ack` | LocalWorker → Daemon | 确认结果已接收 |

#### 4.1.3 服务器 A 的 Daemon 改造

**IPCServer 监听地址变更**：

当前 `IPC_CONFIG["host"]` 为 `"127.0.0.1"`（仅本地回环），需改为 `"0.0.0.0"` 以接受远程连接。同时需增加 TLS/SSL 加密或 SSH 隧道，防止明文传输 SSH 密码等敏感信息。

**TaskRunner 本地部分替换**：

原来 `TaskRunner.execute_sw_macro()` 和 `TaskRunner.execute_spaceclaim()` 直接在本地执行，改为通过 RPC 下发给 LocalWorker。新增 `LocalWorkerAdapter` 类：

```python
class LocalWorkerAdapter:
    """替代 TaskRunner 中直接调用 SW/SC 的逻辑，改为 RPC 下发"""

    def execute_sw_macro(self) -> bool:
        # 向 LocalWorker 发送 worker_execute 命令
        # 等待 worker_step_complete 回调
        ...

    def execute_spaceclaim(self, config_name: int) -> bool:
        # 同上
        ...
```

**StepFileMonitor 远程化**：

当前 `StepFileMonitor` 轮询本地 `step_dir`，改造后由 LocalWorker 侧的 Monitor 检测到文件就绪后，通过 `worker_file_ready` 命令上报给 Daemon。Daemon 侧不再需要 `StepFileMonitor` 实例，改为接收上报事件推入 SC 队列。

#### 4.1.4 服务器 A 资源评估

| 组件 | 内存占用 | CPU 占用 |
|------|---------|---------|
| Daemon 主进程 | ~50-80 MB | 极低（仅调度） |
| SQLite WAL | ~10-20 MB | 极低 |
| 3 条 SSH 连接 (paramiko) | ~30 MB | 极低 |
| IPC Server (TCP) | ~5 MB | 极低 |
| 暂存区文件 I/O | ~10 MB | 偶发 |
| Python 运行时 | ~30 MB | - |
| **合计** | **~150-200 MB** | **< 5%** |

2 核 4 GB 完全足够。Daemon 本质上是 I/O 密集型（SSH 通信 + TCP 通信 + SQLite 读写），不涉及计算。

---

### 4.2 多工作站支持

#### 4.2.1 配置层改造

当前 `REMOTE_CONFIG` 是单字典，只有一套 SSH 连接信息。需改为工作站列表：

```python
# 当前 (engine/config.py)
REMOTE_CONFIG = {
    "host": "172.17.135.240",
    "port": 22,
    "username": "ps",
    ...
}

# 改造后
WORKSTATIONS = [
    {
        "id": "WS-A",
        "host": "172.17.135.240",
        "port": 22,
        "username": "ps",
        "password": os.environ.get("AUTOFLUID_WS_A_PASSWORD", ""),
        "scdoc_dir": r"D:\xkz_1020\scdoc",
        "msh_dir": r"D:\xkz_1020\msh",
        "result_dir": r"D:\xkz_1020\case",
        "postprocess_script": r"D:\xkz_1020\batch_postprocess_gen4.py",
        "postprocess_output_dir": r"D:\xkz_1020\results",
        ...
    },
    {
        "id": "WS-B",
        "host": "172.17.135.241",
        ...
    },
    {
        "id": "WS-C",
        "host": "172.17.135.242",
        ...
    },
]
```

为保持向后兼容，可保留 `REMOTE_CONFIG` 作为默认工作站的快捷引用。

#### 4.2.2 SSH 连接池

当前 `TaskRunner` 中 `self._ssh` 是单实例（`Optional[RemoteWorkstation]`），需改为连接池：

```python
# 当前 (engine/task_runner.py:51)
self._ssh: Optional[RemoteWorkstation] = None
self._ssh_lock = threading.RLock()

# 改造后
self._ssh_pool: dict[str, RemoteWorkstation] = {}
self._ssh_locks: dict[str, threading.RLock] = {}
```

`get_ssh()` 方法需增加 `workstation_id` 参数：

```python
def get_ssh(self, workstation_id: str) -> RemoteWorkstation:
    if workstation_id not in self._ssh_pool:
        ws_config = get_workstation_config(workstation_id)
        self._ssh_pool[workstation_id] = RemoteWorkstation(
            host=ws_config["host"],
            port=ws_config["port"],
            ...
        )
        self._ssh_locks[workstation_id] = threading.RLock()
    ...
```

#### 4.2.3 构型分配器 (ConfigAssigner)

新增模块，负责将构型分配到工作站。推荐轮询取模方案：

```python
class ConfigAssigner:
    def __init__(self, workstations: list[str], configs: list[int]):
        self._assignment: dict[str, list[int]] = {ws: [] for ws in workstations}
        for i, cn in enumerate(sorted(configs)):
            ws = workstations[i % len(workstations)]
            self._assignment[ws].append(cn)

    def get_workstation(self, config_name: int) -> str:
        for ws, configs in self._assignment.items():
            if config_name in configs:
                return ws
        raise ValueError(f"构型{config_name}未分配到任何工作站")

    def get_configs(self, workstation_id: str) -> list[int]:
        return self._assignment.get(workstation_id, [])
```

#### 4.2.4 Transfer 阶段改造

当前 `execute_transfer()` 上传到单一工作站的 `scdoc_dir`，改造后需根据构型分配结果上传到目标工作站。文件传输路径变更为：

```
当前: 本地PC ─SFTP─► 工作站
改造: 本地PC ─SFTP─► 服务器A ─SFTP─► 目标工作站
```

服务器 A 作为中转站，LocalWorker 先上传 SCDOC 到服务器 A 的暂存目录，Daemon 再异步转发到目标工作站。

#### 4.2.5 Meshing/Solver 阶段改造

`execute_meshing()` 和 `execute_solver()` 需增加 `workstation_id` 参数，使用对应的 SSH 连接执行远程命令。标志文件路径需包含工作站标识以避免冲突。

---

### 4.3 屏障机制适配

#### 4.3.1 当前屏障逻辑

当前全局屏障在 `scheduler.py` 的 `_barrier_monitor_loop()` 中实现：

```python
# scheduler.py:721
if self.state.all_configs_completed_at_step("Meshing"):
    self._barrier_passed.set()
    self._dispatch_solver_tasks()
```

语义：**所有**构型的 Meshing 完成后，才解锁所有 Solver。这是一道全局屏障。

#### 4.3.2 目标：工作站级屏障

将全局屏障降级为工作站级屏障。每台工作站独立等待自己的构型子集完成 Meshing 后立即启动该站的 Solver，不必等其他工作站。

```
工作站 A: 构型 [1,4,7,...,28]  →  Meshing屏障(10个全完成)  →  Solver(10个并行)
工作站 B: 构型 [2,5,8,...,29]  →  Meshing屏障(10个全完成)  →  Solver(10个并行)
工作站 C: 构型 [3,6,9,...,30]  →  Meshing屏障(10个全完成)  →  Solver(10个并行)
```

#### 4.3.3 代码改造要点

**`_barrier_passed` 从单个 Event 改为 dict**：

```python
# 当前
self._barrier_passed = threading.Event()

# 改造后
self._barrier_passed: dict[str, threading.Event] = {
    ws_id: threading.Event() for ws_id in workstation_ids
}
```

**`_barrier_monitor_loop` 改为每工作站一个**：

```python
def _barrier_monitor_loop(self, workstation_id: str):
    assigned = self._config_assigner.get_configs(workstation_id)
    while not self._stopped.is_set():
        if all(self.state.get_step_status(cn, "Meshing") == STATUS_COMPLETED
               for cn in assigned):
            self._barrier_passed[workstation_id].set()
            self._dispatch_solver_tasks(workstation_id)
            break
        time.sleep(5.0)
```

**`_dispatch_solver_tasks` 按工作站分发**：

```python
def _dispatch_solver_tasks(self, workstation_id: str):
    assigned = self._config_assigner.get_configs(workstation_id)
    for cn in assigned:
        if self.state.get_step_status(cn, "Solver") in (STATUS_WAITING, ...):
            t = threading.Thread(
                target=self._execute_solver_for_config,
                args=(cn, workstation_id),
                daemon=True,
            )
            t.start()
```

**StateManager 扩展**：

`all_configs_completed_at_step()` 需支持按工作站过滤：

```python
def configs_completed_at_step_for_workstation(
    self, step_name: str, config_names: list[int]
) -> bool:
    with self._get_connection() as conn:
        placeholders = ",".join("?" * len(config_names))
        row = conn.execute(
            f"SELECT COUNT(*) as cnt FROM steps "
            f"WHERE step_name = ? AND status != ? AND config_name IN ({placeholders})",
            [step_name, STATUS_COMPLETED] + config_names
        ).fetchone()
        return row["cnt"] == 0
```

状态表 `steps` 需新增 `workstation_id` 列，记录构型分配信息。

#### 4.3.4 关于"屏障阈值 = N/3"的讨论

另一种思路是保留全局屏障但将阈值从 N 降为 N/3，含义是"只要有 N/3 个构型 Meshing 完成就开始 Solver"。这在逻辑上可行（每个 Solver 只需自己的 mesh 文件），但比工作站级屏障更复杂：

- 需动态追踪哪些构型已就绪、哪些 Solver 已启动
- 需防止同一构型被重复启动 Solver
- 工作站负载不均衡时可能导致某些工作站堆积大量 Solver 任务

**建议**：先实现工作站级屏障（逻辑清晰、实现简单），如后续发现工作站间负载差异大，再考虑放宽为全局阈值屏障。

---

### 4.4 后处理阶段 (PostProcess)

#### 4.4.1 阶段定义

PostProcess 是新增的流水线阶段，在各工作站本地执行后处理脚本。与 Meshing/Solver 不同，它是**工作站级任务**（每站一个），而非构型级任务。

#### 4.4.2 触发条件

第二道工作站级屏障：每台工作站的 Solver 全部完成后，启动该站的后处理。

```
工作站 A 的完整调度链:
  Meshing屏障 → Solver(10并行) → Solver屏障 → PostProcess(1个)
```

#### 4.4.3 代码改造要点

**config.py** 扩展：

```python
STEP_NAMES = ["SW", "SC", "Transfer", "Meshing", "Solver", "PostProcess", "Collect"]

STEP_DISPLAY = {
    ...
    "PostProcess": "后处理",
    "Collect": "结果回收",
}

STEP_FILE_PATTERNS = {
    ...
    "PostProcess": "postprocess_results_{config}.csv",
    "Collect": None,
}
```

**TaskRunner** 新增方法：

```python
def execute_postprocess(self, workstation_id: str) -> bool:
    """在指定工作站执行后处理脚本（工作站级任务）"""
    ...

def wait_postprocess_completion(self, workstation_id: str) -> bool:
    """等待后处理完成"""
    ...
```

**Scheduler** 新增 Solver 屏障和 PostProcess 调度：

```python
def _solver_barrier_monitor_loop(self, workstation_id: str):
    """工作站级 Solver 屏障"""
    assigned = self._config_assigner.get_configs(workstation_id)
    while not self._stopped.is_set():
        if all(self.state.get_step_status(cn, "Solver") == STATUS_COMPLETED
               for cn in assigned):
            self._start_postprocess(workstation_id)
            break
        time.sleep(5.0)

def _start_postprocess(self, workstation_id: str):
    """启动工作站的后处理任务"""
    self.state.set_step_status(..., "PostProcess", STATUS_RUNNING)
    if self.runner.execute_postprocess(workstation_id):
        if self.runner.wait_postprocess_completion(workstation_id):
            self.state.set_step_status(..., "PostProcess", STATUS_COMPLETED)
            self._collect_results_from_workstation(workstation_id)
```

---

### 4.5 结果收集阶段 (Collect)

#### 4.5.1 核心难点

Collect 阶段与前面所有阶段有本质区别：**它不阻塞流水线**。本地 PC 什么时候上线、什么时候拉取，不影响流水线继续运行。它是异步的、最终一致的。

#### 4.5.2 服务器 A 作为结果聚合点

```
工作站 A ──┐
工作站 B ──┼──► 服务器 A (结果暂存) ──► 本地 PC (最终归档)
工作站 C ──┘         ▲                      │
                     │                      │
                     └──── 上线通知 ◄────────┘
```

选择服务器 A 中转而非工作站直传的原因：
- 本地 PC 离线时无法接收直传
- 本地 PC 需暴露端口（安全风险）
- 服务器 A 持久在线，可暂存结果等待交付

#### 4.5.3 交付确认表 (result_delivery)

在 StateManager 中新增表：

```sql
CREATE TABLE IF NOT EXISTS result_delivery (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workstation_id TEXT NOT NULL,
    config_name INTEGER NOT NULL,
    filename TEXT NOT NULL,
    staging_path TEXT NOT NULL,
    staged_at REAL NOT NULL,
    delivered INTEGER NOT NULL DEFAULT 0,
    delivered_at REAL,
    UNIQUE(workstation_id, config_name, filename)
);
```

#### 4.5.4 完整生命周期

```
1. PostProcess 完成
   → Daemon 通过 SFTP 从工作站拉取结果到服务器 A 暂存目录
   → 写入 result_delivery (delivered=0)

2. 本地 PC 上线
   → LocalWorker 发送 collect_results 命令
   → Daemon 查询 delivered=0 的记录
   → 打包推送文件

3. 本地 PC 确认接收
   → LocalWorker 逐个确认交付
   → Daemon 更新 delivered=1, delivered_at=now

4. 定期清理
   → delivered=1 且 delivered_at > 7天 的暂存文件自动删除
```

#### 4.5.5 Collect 阶段的状态语义

Collect 不纳入 `steps` 表的常规状态机，而是独立管理：

| 状态 | 含义 |
|------|------|
| Waiting | 后处理结果已入暂存区，等待本地 PC 拉取 |
| Running | 本地 PC 正在拉取中 |
| Completed | 本地 PC 已确认接收 |
| Error | 拉取失败（网络中断等） |

在 TUI 界面上，Collect 阶段显示为独立的结果回收面板，而非嵌入每个构型的步骤状态表中。

#### 4.5.6 暂存区存储评估

假设每个构型后处理输出 ~1-10 MB，30 个构型 × 3 台工作站 = 90 个结果，暂存区最大占用 ~900 MB。服务器 A 如有 20 GB+ 磁盘空间，暂存完全没问题。

---

## 5. 全项目影响分析

### 5.1 Python 后端影响矩阵

| 文件 | 影响程度 | 改动类型 | 详细说明 |
|------|:--------:|---------|---------|
| `engine/config.py` | 🔴 重度 | 结构变更 | `REMOTE_CONFIG` → `WORKSTATIONS` 列表；`STEP_NAMES` 增加 PostProcess/Collect；`IPC_CONFIG["host"]` 改为 `0.0.0.0`；新增 `STAGING_DIR`、`LOCAL_WORKER_CONFIG` 等配置；`STEP_FILE_PATTERNS` 增加 PostProcess/Collect 条目；`STEP_INDEX` 自动扩展 |
| `engine/daemon.py` | 🔴 重度 | 架构重构 | 新增 `LocalWorkerAdapter`；新增 `handle_collect_results` / `handle_worker_register` / `handle_worker_heartbeat` 等 IPC 命令处理器；`_load_excel_data()` 需触发构型分配；`handle_start()` 需区分本地 Worker 在线/离线场景 |
| `engine/scheduler.py` | 🔴 重度 | 核心逻辑重写 | `_barrier_passed` 改为 dict；`_barrier_monitor_loop` 改为每工作站一个；新增 `_solver_barrier_monitor_loop`；`_dispatch_solver_tasks` 增加工作站参数；`_worker_loop` 中 `_process_single_config` 需感知工作站分配；`_on_step_file_ready` 改为接收 RPC 上报；`reset_config` 需处理多工作站屏障重置 |
| `engine/task_runner.py` | 🔴 重度 | 接口重构 | `self._ssh` → `self._ssh_pool`；`get_ssh()` 增加 `workstation_id` 参数；`execute_transfer()` 需指定目标工作站；`execute_meshing()` / `execute_solver()` 增加 `workstation_id` 参数；新增 `execute_postprocess(ws_id)` / `collect_results_from_workstation(ws_id)`；`clean_step_files()` 需遍历所有工作站；`run_system_check()` 需检查所有工作站 |
| `engine/state_manager.py` | 🟡 中度 | 表结构扩展 | `steps` 表新增 `workstation_id` 列；新增 `result_delivery` 表；新增 `mark_result_staged()` / `get_undelivered_results()` / `mark_result_delivered()` 方法；`all_configs_completed_at_step()` 增加 `config_names` 过滤参数；`load_configs()` 需同步构型分配信息 |
| `engine/file_monitor.py` | 🟡 中度 | 运行模式变更 | 在 LocalWorker 侧保持原有逻辑不变；Daemon 侧不再需要此模块，改为接收 RPC 上报事件 |
| `ipc/protocol.py` | 🟡 中度 | 协议扩展 | 新增 9 个命令常量（`CMD_WORKER_REGISTER` 等）；新增 `CMD_COLLECT_RESULTS` / `CMD_COLLECT_ACK` |
| `ipc/server.py` | 🟡 中度 | 功能扩展 | 监听地址改为 `0.0.0.0`；注册新的 Worker 相关命令处理器；可能需要区分 TUI Client 连接和 LocalWorker 连接 |
| `utils/ssh_client.py` | 🟢 轻度 | 接口微调 | `RemoteWorkstation` 类本身无需修改，但调用方式从单实例变为池化管理 |
| `utils/logger.py` | 🟢 轻度 | 无变更 | 日志广播机制可复用 |
| `utils/excel_reader.py` | 🟢 轻度 | 无变更 | Excel 读取逻辑不变 |

### 5.2 Rust TUI 前端影响矩阵

| 文件 | 影响程度 | 改动类型 | 详细说明 |
|------|:--------:|---------|---------|
| `autofluid-tui/src/ipc/protocol.rs` | 🟡 中度 | 协议同步 | 新增命令常量；与 Python 端 `ipc/protocol.py` 保持一致 |
| `autofluid-tui/src/ipc/client.rs` | 🟡 中度 | 连接目标变更 | `DEFAULT_HOST` 从 `127.0.0.1` 改为服务器 A 地址（或从环境变量读取）；新增 `collect_results()` 方法 |
| `autofluid-tui/src/state/app_state.rs` | 🟡 中度 | 状态扩展 | `STEP_NAMES` 从 5 个扩展为 7 个；`STEP_DISPLAY` 增加 PostProcess/Collect；新增结果回收状态字段 |
| `autofluid-tui/src/ui/table.rs` | 🟡 中度 | 渲染扩展 | 表格列数从 6 列（构型+5步骤）扩展为 8 列（构型+7步骤）；列宽需调整以适应终端宽度 |
| `autofluid-tui/src/ui/header.rs` | 🟢 轻度 | 版本号更新 | 标题栏版本号更新 |
| `autofluid-tui/src/ui/layout.rs` | 🟡 中度 | 布局扩展 | 可能需要新增结果回收面板区域 |
| `autofluid-tui/src/event_handler/command.rs` | 🟡 中度 | 命令扩展 | 新增 `collect` 命令处理 |
| `autofluid-tui/src/daemon_mgr.rs` | 🔴 重度 | 架构变更 | 当前此模块负责在本地启动 Daemon 子进程；Daemon 迁移到服务器 A 后，此模块需改为连接远程 Daemon，或改为管理 LocalWorker 进程 |
| `autofluid-tui/src/main.rs` | 🟡 中度 | 启动逻辑调整 | Daemon 不再是本地子进程，启动流程需调整 |

### 5.3 启动入口影响矩阵

| 文件 | 影响程度 | 改动类型 | 详细说明 |
|------|:--------:|---------|---------|
| `main.py` | 🔴 重度 | 启动模式重构 | `--all` 模式不再同时启动 Daemon+Client（Daemon 在远程）；新增 `--worker` 模式启动 LocalWorker；`--status` 需检查远程 Daemon 连通性；`_start_daemon_subprocess()` / `_stop_daemon_subprocess()` 需改为远程管理 |
| `start_daemon.py` | 🟡 中度 | 平台适配 | 需在 Ubuntu 上运行，移除 Windows 特有逻辑；信号处理改为 Linux 风格 |
| `start_client.py` | 🟡 中度 | 连接目标变更 | IPC 连接地址从 `127.0.0.1` 改为服务器 A；新增 `--worker` 启动模式 |

### 5.4 新增模块

| 模块 | 文件 | 预估行数 | 职责 |
|------|------|---------|------|
| LocalWorker | `engine/local_worker.py` | ~400 | 本地 PC 侧 Worker 进程：执行 SW/SC、监控 STEP 文件、上传 SCDOC、拉取结果 |
| ConfigAssigner | `engine/config_assigner.py` | ~80 | 构型分配器：将构型按策略分配到工作站 |
| LocalWorkerAdapter | `engine/local_worker_adapter.py` | ~200 | Daemon 侧适配器：替代 TaskRunner 中直接调用 SW/SC 的逻辑 |
| ResultCollector | `engine/result_collector.py` | ~250 | 服务器 A 侧结果收集：从工作站拉取、暂存管理、推送本地 PC |
| StagingManager | `engine/staging_manager.py` | ~150 | 暂存区管理：文件存储、交付追踪、定期清理 |
| WorkerProtocol | `ipc/worker_protocol.py` | ~100 | Worker RPC 协议定义（复用 IPC 协议框架） |
| ResultFetcher | `client/result_fetcher.py` | ~150 | 本地 PC 侧结果拉取：连接 Daemon、接收文件、确认交付 |

---

## 6. 代码改动量与构建复杂度评估

### 6.1 改动量统计

| 类别 | 涉及文件数 | 新增行数(估) | 修改行数(估) | 删除行数(估) |
|------|:---------:|:----------:|:----------:|:----------:|
| Python 后端核心 | 7 | ~1,230 | ~800 | ~200 |
| Python 客户端 | 2 | ~150 | ~100 | ~20 |
| Rust TUI 前端 | 8 | ~300 | ~200 | ~50 |
| 启动入口 | 3 | ~100 | ~200 | ~150 |
| 新增模块 | 7 | ~1,330 | 0 | 0 |
| 测试 | 4+ | ~500 | ~100 | ~50 |
| **合计** | **~31** | **~3,610** | **~1,400** | **~470** |

### 6.2 构建复杂度评估

| 改造项 | 复杂度 | 评估理由 |
|--------|:------:|---------|
| Daemon 拆分迁移 | ⭐⭐⭐⭐⭐ | 涉及进程间通信重构、本地/远程执行路径分离、断点续传逻辑适配，是整个改造中最复杂的部分 |
| 多工作站支持 | ⭐⭐⭐⭐ | SSH 连接池化、构型分配策略、Transfer 路径变更，涉及多个模块接口变更 |
| 屏障机制适配 | ⭐⭐⭐ | 逻辑清晰但需仔细处理线程同步、状态一致性，以及 reset 操作时的屏障重置 |
| PostProcess 阶段 | ⭐⭐⭐ | 新增阶段相对独立，但需与现有调度框架集成，包括状态管理、TUI 显示 |
| Collect 阶段 | ⭐⭐⭐⭐ | 离线/上线状态管理、交付确认机制、文件暂存与清理，涉及新的数据库表和异步交互模式 |

### 6.3 依赖关系图

```
P0: 多工作站配置 ──────────────────────────────────────────┐
(WORKSTATIONS 列表 + SSH 连接池 + ConfigAssigner)           │
    │                                                      │
    ▼                                                      │
P1: 工作站级屏障 ◄─────────────────────────────────────────┤
(BarrierMonitor 多实例化 + steps 表增加 workstation_id)      │
    │                                                      │
    ├──► P2: PostProcess 阶段                              │
    │    (后处理脚本执行 + Solver 屏障)                      │
    │         │                                             │
    │         ▼                                             │
    │    P4: 结果暂存与交付确认                              │
    │    (result_delivery 表 + StagingManager)               │
    │         │                                             │
    │         ▼                                             │
    │    P5: 本地 PC 上线收集                                │
    │    (ResultFetcher + collect_results 命令)              │
    │                                                       │
    ├──► P3: Daemon 拆分迁移 ◄──────────────────────────────┘
    │    (LocalWorker + LocalWorkerAdapter + RPC 通信)
    │         │
    │         ▼
    │    P3 依赖 P0 + P1，且 P4/P5 依赖 P3
    │
    └──► P2 可在当前单工作站架构上独立验证
```

---

## 7. 潜在 Bug 与风险清单

### 7.1 Daemon 拆分相关

| # | 风险 | 严重度 | 触发场景 | 缓解措施 |
|---|------|:------:|---------|---------|
| R1 | **LocalWorker 与 Daemon 间网络断开导致状态不一致** | 🔴 高 | 网络波动、VPN 断连 | 心跳机制 + 超时检测；Daemon 侧设置 Worker 离线超时（如 90 秒无心跳），将受影响步骤标记为 Paused；LocalWorker 重连后自动同步状态 |
| R2 | **SW COM 对象跨进程不可用** | 🔴 高 | Daemon 侧代码误用 win32com | 严格拆分：Daemon 进程不导入 win32com；LocalWorker 独立进程运行 SW/SC；通过 RPC 隔离 |
| R3 | **STEP 文件就绪事件丢失** | 🟡 中 | LocalWorker 上报 `worker_file_ready` 时网络中断 | Daemon 侧定期轮询 LocalWorker 获取已完成的 STEP 列表（类似当前 `_scan_existing_files` 的安全网机制） |
| R4 | **SCDOC 文件上传到服务器 A 后转发失败** | 🟡 中 | 服务器 A → 工作站的 SFTP 传输中断 | 暂存区记录待转发文件，Daemon 定期重试；Transfer 步骤状态在确认到达工作站后才标记 Completed |
| R5 | **IPC 明文传输敏感信息** | 🔴 高 | SSH 密码等通过 IPC 传输被嗅探 | IPC 通信增加 TLS 加密层；或使用 SSH 隧道转发；敏感配置仅存于服务器 A |
| R6 | **LocalWorker 进程崩溃无人看管** | 🟡 中 | SW COM 异常导致 LocalWorker 崩溃 | LocalWorker 实现看门狗自重启；Daemon 侧检测 Worker 离线后告警 |

### 7.2 多工作站相关

| # | 风险 | 严重度 | 触发场景 | 缓解措施 |
|---|------|:------:|---------|---------|
| R7 | **SSH 连接池泄漏** | 🟡 中 | 异常路径未正确释放 SSH 连接 | 使用 context manager 管理 SSH 连接生命周期；定期检查连接健康状态；设置连接最大空闲时间 |
| R8 | **构型分配不均衡** | 🟢 低 | 某些构型 Meshing/Solver 耗时差异大 | 轮询取模已是较均衡方案；如需更优可考虑动态分配（但复杂度大增） |
| R9 | **工作站故障导致分配到该站的构型全部卡死** | 🔴 高 | 工作站 B 宕机，其上 10 个构型无法继续 | 实现工作站健康检查；故障检测后支持将构型重新分配到其他工作站（需重置 Transfer 步骤） |
| R10 | **多工作站标志文件路径冲突** | 🟢 低 | 不同工作站的标志文件使用相同命名 | 标志文件路径已包含构型号（`meshing_done_{config}.txt`），天然不冲突 |
| R11 | **`_ssh_lock` 粒度不当导致性能瓶颈** | 🟡 中 | 当前全局一把锁，3 个 Worker 线程竞争同一工作站时串行化 | 改为每工作站一把锁；不同工作站的 SSH 操作可并行 |

### 7.3 屏障机制相关

| # | 风险 | 严重度 | 触发场景 | 缓解措施 |
|---|------|:------:|---------|---------|
| R12 | **工作站级屏障与全局状态不一致** | 🔴 高 | `is_global_barrier_met()` 在多处被引用（`daemon.py:269`、`scheduler.py:82-83`），改为工作站级后语义变化 | 全面搜索 `barrier` 相关引用，逐一适配；`is_global_barrier_met()` 改为 `all_workstation_barriers_met()` 或保留全局语义（所有工作站屏障都通过） |
| R13 | **reset 操作后屏障状态未正确清理** | 🟡 中 | `reset_config()` 中 `_barrier_passed.clear()` 只清理了单个 Event，改为 dict 后需清理对应工作站的 Event | `reset_config` 遍历受影响工作站的 `_barrier_passed[ws_id]` 并 clear |
| R14 | **断点续传时工作站分配变化** | 🟡 中 | 重启后 WORKSTATIONS 列表顺序变化导致同一构型分配到不同工作站 | 构型分配结果持久化到 `steps` 表的 `workstation_id` 列；断点续传时优先使用已记录的分配 |
| R15 | **Solver 屏障与 Meshing 屏障的线程同步** | 🟡 中 | 两个屏障的 Monitor 线程同时操作状态 | 每个工作站的两道屏障串行触发（Meshing 屏障 → Solver 启动 → Solver 屏障 → PostProcess），不存在并发冲突 |

### 7.4 PostProcess 相关

| # | 风险 | 严重度 | 触发场景 | 缓解措施 |
|---|------|:------:|---------|---------|
| R16 | **后处理脚本无幂等性** | 🟡 中 | PostProcess 失败重试时重复处理已有结果 | 后处理脚本应实现幂等（检查输出是否已存在）；或每次执行前清理旧输出 |
| R17 | **PostProcess 作为工作站级任务的状态管理** | 🟡 中 | 当前 `steps` 表是每构型每步骤一条记录，PostProcess 是每工作站一个任务 | 方案一：为分配到该工作站的所有构型同时设置 PostProcess 状态；方案二：新增 `workstation_steps` 表管理工作站级任务 |
| R18 | **后处理输出文件名不确定** | 🟢 低 | 后处理脚本输出文件名可能包含时间戳等不确定因素 | 约定后处理脚本输出到固定目录，Daemon 拉取整个目录而非逐文件 |

### 7.5 Collect 相关

| # | 风险 | 严重度 | 触发场景 | 缓解措施 |
|---|------|:------:|---------|---------|
| R19 | **暂存区磁盘空间耗尽** | 🔴 高 | 大量结果未交付堆积 | 设置暂存区磁盘使用阈值告警；自动清理已交付且超期的文件；紧急时暂停流水线 |
| R20 | **交付确认丢失导致重复推送** | 🟡 中 | 本地 PC 接收完成但确认消息丢失 | 实现幂等推送：本地 PC 检查文件是否已存在，存在则跳过；Daemon 侧超时重试 |
| R21 | **大文件传输中断** | 🟡 中 | 网络不稳定导致文件传输中断 | 实现断点续传（记录已传输字节数）；或使用 rsync 等工具替代 SFTP |
| R22 | **本地 PC 长期离线导致暂存区无限增长** | 🟡 中 | 本地 PC 数周不在线 | 暂存文件设置最大保留天数（如 30 天）；超期后仅保留元数据（文件名、大小、校验和），实际文件删除 |
| R23 | **多批次结果混合** | 🟢 低 | 多次运行流水线，不同批次的结果混在一起 | 暂存目录按运行批次（session timestamp）隔离 |

### 7.6 跨模块交互风险

| # | 风险 | 严重度 | 触发场景 | 缓解措施 |
|---|------|:------:|---------|---------|
| R24 | **Python 端与 Rust 端协议不同步** | 🟡 中 | Python 端新增命令但 Rust 端未更新 | 协议版本号机制；Rust 端对未知命令优雅降级（显示原始消息） |
| R25 | **STEP_NAMES 长度变化导致 TUI 表格溢出** | 🟢 低 | 从 5 个步骤扩展到 7 个，终端宽度不足 | Rust TUI 表格列宽动态计算；窄终端时省略部分列或横向滚动 |
| R26 | **SQLite 数据库迁移** | 🟡 中 | `steps` 表新增 `workstation_id` 列，`result_delivery` 表新建 | 使用 `ALTER TABLE ADD COLUMN`（SQLite 支持）；新表使用 `CREATE TABLE IF NOT EXISTS`；编写数据库迁移脚本 |
| R27 | **环境变量命名冲突** | 🟢 低 | 新增多工作站的 `AUTOFLUID_WS_A_PASSWORD` 等环境变量 | 遵循现有 `AUTOFLUID_` 前缀命名规范；在 `.env` 文件中统一管理 |

---

## 8. 实施路线图

### 8.1 阶段规划

| 阶段 | 内容 | 预估工期 | 前置依赖 | 验证标准 |
|------|------|:-------:|---------|---------|
| **P0** | 多工作站配置 | 1-2 周 | 无 | `WORKSTATIONS` 列表可配置；SSH 连接池可建立多连接；`ConfigAssigner` 正确分配构型 |
| **P1** | 工作站级屏障 | 1-2 周 | P0 | 每工作站独立 Meshing 屏障；屏障通过后仅启动该站 Solver；reset 正确清理对应屏障 |
| **P2** | PostProcess 阶段 | 1 周 | P1 | Solver 屏障通过后自动启动后处理；后处理状态正确显示在 TUI |
| **P3** | Daemon 拆分迁移 | 3-4 周 | P0, P1 | LocalWorker 可独立运行 SW/SC；Daemon 在 Ubuntu 上稳定运行；RPC 通信可靠 |
| **P4** | 结果暂存与交付 | 2 周 | P2, P3 | PostProcess 完成后自动拉取到暂存区；`result_delivery` 表正确记录状态 |
| **P5** | 本地 PC 上线收集 | 1-2 周 | P4 | 本地 PC 上线后可拉取暂存结果；交付确认正确更新；离线期间结果不丢失 |

### 8.2 建议的验证顺序

1. **P0 + P1 在当前单工作站架构上验证**：先不迁移 Daemon，仅在本地 PC 上实现多工作站配置和工作站级屏障，用单工作站模拟多工作站行为
2. **P2 在当前架构上验证**：新增 PostProcess 阶段，验证第二道屏障和后处理脚本执行
3. **P3 独立验证**：搭建 Ubuntu 服务器 A，部署 Daemon，LocalWorker 连接测试
4. **P4 + P5 集成验证**：完整的三层架构端到端测试

### 8.3 回滚策略

每个阶段应保持可回滚：

- P0/P1：`REMOTE_CONFIG` 保留为单工作站模式的快捷入口，通过配置开关切换
- P3：LocalWorker 可降级为本地 Daemon 模式（即回退到当前架构）
- P4/P5：Collect 阶段为可选功能，不影响核心流水线运行

---

## 附录 A：关键代码引用索引

| 引用点 | 文件 | 行号 | 说明 |
|--------|------|------|------|
| SSH 单实例 | `engine/task_runner.py` | L51 | `self._ssh: Optional[RemoteWorkstation] = None` |
| SSH 全局锁 | `engine/task_runner.py` | L52 | `self._ssh_lock = threading.RLock()` |
| 单工作站配置 | `engine/config.py` | L87-110 | `REMOTE_CONFIG` 单字典 |
| IPC 本地监听 | `engine/config.py` | L156 | `IPC_CONFIG["host"] = "127.0.0.1"` |
| 步骤枚举 | `engine/config.py` | L115 | `STEP_NAMES = ["SW", "SC", "Transfer", "Meshing", "Solver"]` |
| 全局屏障检查 | `engine/scheduler.py` | L721 | `self.state.all_configs_completed_at_step("Meshing")` |
| 屏障 Event | `engine/scheduler.py` | L65 | `self._barrier_passed = threading.Event()` |
| Worker 线程数 | `engine/scheduler.py` | L79 | `self._num_workers = 3` |
| 屏障状态持久化 | `engine/scheduler.py` | L82-83 | `if self.state.is_global_barrier_met(): self._barrier_passed.set()` |
| 全局屏障查询 | `engine/daemon.py` | L269 | `stats["barrier_passed"] = self.state.is_global_barrier_met()` |
| Rust STEP_NAMES | `autofluid-tui/src/state/app_state.rs` | L12 | `pub const STEP_NAMES: [&str; 5]` |
| Rust IPC 默认地址 | `autofluid-tui/src/ipc/client.rs` | L7 | `const DEFAULT_HOST: &str = "127.0.0.1"` |
| Rust Daemon 管理 | `autofluid-tui/src/daemon_mgr.rs` | L12-36 | 本地启动 Daemon 子进程 |
| Python IPC 协议 | `ipc/protocol.py` | L36-51 | 命令常量定义 |
| Rust IPC 协议 | `autofluid-tui/src/ipc/protocol.rs` | L4-13 | 命令常量定义 |
| 数据库表结构 | `engine/state_manager.py` | L74-97 | `configs` + `steps` 表 |
| 全局屏障查询方法 | `engine/state_manager.py` | L448-460 | `all_configs_completed_at_step()` |

## 附录 B：术语表

| 术语 | 含义 |
|------|------|
| Daemon | 后台守护进程，流水线调度核心 |
| LocalWorker | 本地 PC 上的工作进程，执行 SW/SC 等本地任务 |
| 工作站级屏障 | 仅检查分配给特定工作站的构型子集是否全部完成 |
| 全局屏障 | 检查所有构型是否全部完成（当前实现） |
| 暂存区 (Staging) | 服务器 A 上临时存储后处理结果的目录 |
| 交付确认 (Delivery Ack) | 本地 PC 确认已成功接收结果的机制 |
| ConfigAssigner | 构型分配器，决定哪些构型分配到哪台工作站 |
