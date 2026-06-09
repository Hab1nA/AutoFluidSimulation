# Daemon 系统代码拆分计划

> 文档版本：v2.0  
> 创建日期：2026-05-29  
> 最后更新：2026-06-09
> 基于：`docs/architecture-refactoring-plan.md` 远期计划
> 目标：将单机 Daemon 拆分为"本地 PC + 服务器 A"分布式架构

---

## 目录

1. [拆分概述](#1-拆分概述)
2. [架构对比](#2-架构对比)
3. [模块拆分清单](#3-模块拆分清单)
4. [核心拆分点详解](#4-核心拆分点详解)
5. [IPC 协议扩展](#5-ipc-协议扩展)
6. [配置层改造](#6-配置层改造)
7. [数据库 Schema 变更](#7-database-schema-变更)
8. [新增模块设计](#8-新增模块设计)
9. [实施路线图](#9-实施路线图)

---

## 1. 拆分概述

### 1.1 拆分原则

当前 `PipelineDaemon` 同时承担**调度编排**和**本地任务执行**两个职责。拆分后：

| 组件 | 迁移到服务器 A | 保留在本地 PC |
|------|:---:|:---:|
| `PipelineDaemon`（主控） | ✅ | |
| `StateManager`（SQLite） | ✅ | |
| `PipelineScheduler` | ✅ | |
| `PipelineControl` | ✅ | |
| `IPCServer` | ✅ | |
| `TaskRunner`（协调器，远程部分） | ✅ | |
| `TaskRunner`（本地部分：SW/SC） | | ✅ |
| `StepFileMonitor` | | ✅（改造为远程上报模式） |
| `TUI Client` | | ✅ |
| **新增** `LocalWorker` | | ✅ |
| **新增** `ResultFetcher` | | ✅ |

### 1.2 拆分依据

- **SW/SC 阶段**依赖本地 Windows 环境（win32com COM API、SpaceClaimBridge.exe），必须保留在本地 PC
- **Transfer/Meshing/Solver 阶段**通过 SSH 远程执行，可迁移到服务器 A
- **StepFileMonitor** 轮询本地文件系统，需保留在本地，改造为远程上报模式

---

## 2. 架构对比

### 2.1 当前单机架构

```
┌───────────────────────────────────────────────────────────────────────┐
│                     本地 Windows PC                                    │
│                                                                       │
│  ┌─────────────────────────────────────────────────────────────────┐  │
│  │                    PipelineDaemon (单一进程)                     │  │
│  │  ┌─────────────┐  ┌──────────────┐  ┌────────────────────────┐  │  │
│  │  │ IPCServer   │  │ StateManager │  │ PipelineScheduler      │  │  │
│  │  │ (TCP:9527)  │  │ (SQLite WAL) │  │  ├─ PipelineControl   │  │  │
│  │  │ 11 个命令    │  │ DB 分片      │  │  ├─ SWPhaseHandler    │  │  │
│  │  └─────────────┘  └──────────────┘  │  ├─ WorkerPoolManager │  │  │
│  │                                      │  │  ├─ SC 队列        │  │  │
│  │  ┌─────────────────────────────┐     │  │  └─ Transfer 队列  │  │  │
│  │  │ TaskRunner (协调器)          │     │  ├─ BarrierCoordinator│  │  │
│  │  │  ├─ SWExecutor (sw_executor)│     │  ├─ MeshingMonitor   │  │  │
│  │  │  ├─ RemoteExecutor          │     │  ├─ RetryManager     │  │  │
│  │  │  └─ FileCleaner             │     │  └─ UniqueWorkQueue  │  │  │
│  │  └─────────────────────────────┘     └────────────────────────┘  │  │
│  │  ┌────────────────────┐                                         │  │
│  │  │ StepFileMonitor    │  ← FileStableDetector 文件稳定性判定     │  │
│  │  │ (轮询本地 step_dir)│                                         │  │
│  │  └────────────────────┘                                         │  │
│  └─────────────────────────────────────────────────────────────────┘  │
└───────────────────────────────────────────────────────────────────────┘
                           │ SSH (paramiko)
                           ▼
                ┌───────────────────────┐
                │  工作站 (Windows 22H2) │
                │  ├─ batch_meshing      │
                │  ├─ batch_solver       │
                │  └─ 标志文件轮询        │
                └───────────────────────┘
```

### 2.2 目标分布式架构

```
┌──────────────────────────────────────────────────────────────────────────┐
│                         目标三层分布式架构                                 │
│                                                                          │
│  ┌──────────────────────────────┐      ┌───────────────────────────────┐ │
│  │      本地 PC (Windows)        │      │   服务器 A (Ubuntu 22.04)     │ │
│  │                              │      │                              │ │
│  │  ┌────────────────────────┐  │ RPC  │  ┌────────────────────────┐ │ │
│  │  │ LocalWorker (新增)      │◄├──────►│  │ PipelineDaemon (主控)   │ │ │
│  │  │  ├─ SWExecutor         │  │      │  │  ├─ IPCServer (0.0.0.0) │ │ │
│  │  │  ├─ SCProcessPool      │  │      │  │  ├─ StateManager        │ │ │
│  │  │  └─ StepFileMonitor    │  │      │  │  └─ PipelineScheduler   │ │ │
│  │  └────────────────────────┘  │      │  │     ├─ PipelineControl │ │ │
│  │                              │      │  │     ├─ WorkerPoolMgr   │ │ │
│  │  ┌────────────────────────┐  │      │  │     │  ├─ SC 队列      │ │ │
│  │  │ TUI Client (Rust)      │◄├──────┤  │     │  └─ Transfer 队列│ │ │
│  │  └────────────────────────┘  │      │  │     ├─ BarrierCoord   │ │ │
│  │                              │      │  │     └─ MeshingMonitor  │ │ │
│  │  ┌────────────────────────┐  │      │  ├────────────────────────┤ │ │
│  │  │ ResultFetcher (新增)    │◄├──────┤  │ TaskRunner (远程部分)   │ │ │
│  │  │ (结果拉取)              │  │      │  │  ├─ RemoteExecutor    │ │ │
│  │  └────────────────────────┘  │      │  │  └─ FileCleaner       │ │ │
│  └──────────────────────────────┘      │  ├────────────────────────┤ │ │
│                                        │  │ ResultCollector (新增)  │ │ │
│                                        │  │ StagingManager (新增)   │ │ │
│                                        │  └────────────────────────┘ │ │
│                                        └───────────────────────────────┘ │
└──────────────────────────────────────────────────────────────────────────┘
```

---

## 3. 模块拆分清单

### 3.1 保留在本地 PC 的模块

| 模块 | 文件路径 | 职责说明 | 改动类型 |
|------|---------|---------|:-------:|
| `TaskRunner` 本地部分 | `engine/task_runner.py` | 协调器，保留 `SWExecutor` 委托调用 | 拆分 |
| `SWExecutor` | `executor/sw_executor.py` | SolidWorks COM 自动化（~1734 行） | 无变更 |
| `SCProcessPool` | `engine/sc_process_pool.py` | SpaceClaim 3 槽位常驻进程池（文件协议 IPC） | 无变更 |
| `SCScript` | `executor/spaceclaim_transit.py` | SpaceClaim Python API 转换脚本（~985 行） | 无变更 |
| `StepFileMonitor` | `engine/file_monitor.py` | FileStableDetector 改造为远程上报模式 | 重构 |
| `ExcelReader` | `utils/excel_reader.py` | Excel 参数读取 | 无变更 |

### 3.2 迁移到服务器 A 的模块

| 模块 | 文件路径 | 当前行数 | 职责说明 | 改动类型 |
|------|---------|:-------:|---------|:-------:|
| `PipelineDaemon` | `engine/daemon.py` | ~551 | 主控协调器，新增 LocalWorkerAdapter | 重构 |
| `StateManager` | `engine/state_manager.py` | ~579 | SQLite WAL + DB 分片（config fingerprint），新增 workstation_id 字段 | 扩展 |
| `ConfigFingerprint` | `engine/config_fingerprint.py` | ~31 | 构型组合指纹 MD5，数据库分片依据 | 无变更 |
| `PipelineScheduler` | `engine/scheduler/main.py` | ~691 | 调度核心：SW→SC/Transfer/Meshing 流水线 + 全局屏障 + Solver 分发 | 重构 |
| `PipelineControl` | `engine/scheduler/control.py` | ~70 | 统一 pause/resume/stop 并发控制（transition_lock 防竞态） | 轻微调整 |
| `SWPhaseHandler` | `engine/scheduler/sw_phase.py` | ~459 | SW 阶段执行、断点续传递归、下游状态同步 | 无变更 |
| `WorkerPoolManager` | `engine/scheduler/worker_pool.py` | ~515 | SC/Transfer 解耦双队列工作线程池 | 轻微调整 |
| `BarrierCoordinator` | `engine/scheduler/barrier.py` | ~316 | 全局屏障监控 + Solver 线程调度（串行执行） | 重构 |
| `MeshingMonitor` | `engine/scheduler/meshing_monitor.py` | ~295 | 串行 Meshing 执行器（UniqueWorkQueue 去重） | 轻微调整 |
| `RetryManager` | `engine/scheduler/retry.py` | ~148 | 统一重试逻辑（Running→Retrying→Error 状态机） | 无变更 |
| `UniqueWorkQueue` | `engine/scheduler/work_queue.py` | ~68 | 去重工作队列（线程安全） | 无变更 |
| `IPCServer` | `ipc/server.py` | ~264 | 监听地址改为 0.0.0.0，新增 Worker 命令处理器 | 扩展 |
| `IPCProtocol` | `ipc/protocol.py` | ~129 | JSON-over-TCP 协议（当前 11 个命令），新增 Worker 相关命令 | 扩展 |
| `RemoteExecutor` | `executor/remote_executor.py` | ~1028 | SFTP 传输 + 远程 Meshing/Solver 执行 | 扩展 |
| `RemoteWorkstation` | `utils/ssh_client.py` | ~1092 | paramiko SSH/SFTP 封装，新增多工作站支持 | 轻微调整 |
| `FileCleaner` | `executor/cleaner.py` | ~174 | 系统健康检查 + 中间文件清理 | 无变更 |
| `Logger` | `utils/logger.py` | ~493 | 会话级日志 + 广播处理器（TUI 增量拉取） | 无变更 |
| `Config` | `engine/config.py` | ~516 | TOML 配置加载 + 环境变量覆盖 + TypedDict 定义 | 扩展 |

### 3.3 Rust TUI 侧需要适配的模块

| 模块 | 文件路径 | 职责说明 | 改动类型 |
|------|---------|---------|:-------:|
| `IpcProtocol` | `autofluid-tui/src/ipc/protocol.rs` | 新增 Worker 相关命令常量 | 扩展 |
| `Settings` | `autofluid-tui/src/settings/mod.rs` | 新增多工作站配置分类（7 分类 → 8+） | 扩展 |
| `SettingsIO` | `autofluid-tui/src/settings/config_io.rs` | TOML 读写支持新配置段 | 扩展 |
| `AppState` | `autofluid-tui/src/state/app_state.rs` | 新增 Worker 状态显示 | 扩展 |
| `CommandBar` | `autofluid-tui/src/ui/command_bar.rs` | 新增 Worker 管理按钮 | 扩展 |

### 3.4 新增模块

| 模块 | 文件路径 | 预估行数 | 职责说明 |
|------|---------|:-------:|---------|
| `LocalWorker` | `engine/local_worker.py` | ~400 | 本地 PC 侧 Worker 进程（asyncio 事件循环） |
| `LocalWorkerAdapter` | `engine/local_worker_adapter.py` | ~200 | Daemon 侧适配器（RPC 下发替代直接调用） |
| `ConfigAssigner` | `engine/config_assigner.py` | ~80 | 构型分配器（轮询取模策略） |
| `MeshingAssigner` | `engine/scheduler/meshing_assigner.py` | ~150 | 动态任务分配（槽位池模型） |
| `WorkstationMesher` | `engine/scheduler/workstation_mesher.py` | ~200 | 工作站 Meshing 状态管理 + 自感知屏障 |
| `ResultCollector` | `engine/result_collector.py` | ~250 | 服务器 A 侧结果收集（SFTP 拉取） |
| `StagingManager` | `engine/staging_manager.py` | ~150 | 暂存区管理（磁盘 + DB 记录） |
| `WorkerProtocol` | `ipc/worker_protocol.py` | ~100 | Worker RPC 协议定义 |
| `ResultFetcher` | `client/result_fetcher.py` | ~150 | 本地 PC 侧结果拉取 |

---

## 4. 核心拆分点详解

### 4.1 TaskRunner 协调器分离

**当前代码** (`engine/task_runner.py`) 采用**协调器 + 子执行器**模式：

```python
class TaskRunner:
    """任务执行器（协调者）。保持 SSH 连接和 SCProcessPool，将具体执行逻辑委托给子模块。"""
    def __init__(self, state_manager: StateManager):
        self._ssh: Optional[RemoteWorkstation] = None
        self._ssh_lock = threading.RLock()
        self._sc_pool = SCProcessPool()

        # ---- 子执行器 ----
        self._sw_executor = SWExecutor(self.state)
        self._remote_executor = RemoteExecutor(
            self.state,
            ssh_getter=self.get_ssh,
            ssh_lock=self._ssh_lock,
        )
        self._cleaner = FileCleaner(self.state, ssh_getter=self.get_ssh)
```

**拆分后**：

- **服务器端** `TaskRunner`：移除 `SWExecutor` 和 `SCProcessPool`，仅保留 `RemoteExecutor`（Transfer/Meshing/Solver）
- **本地端** `LocalWorker`：包含 `SWExecutor`、`SCProcessPool`、`StepFileMonitor`
- `RemoteExecutor` 中的 SSH 连接获取改为支持多工作站：`ssh_getter` 需增加 `workstation_id` 参数

### 4.2 StepFileMonitor 远程化

**当前**: `StepFileMonitor` 使用 `FileStableDetector` 轮询本地 `step_dir`，检测文件大小稳定后推入 SC 队列。由 `SWPhaseHandler` 注入并管理生命周期。

**改造后**:
1. 本地 `LocalWorker` 运行 `StepFileMonitor`（保持现有逻辑不变）
2. 检测到文件就绪后，通过 RPC 命令 `worker_file_ready` 上报给 Daemon
3. Daemon 侧 `SWPhaseHandler` 不再需要 `StepFileMonitor` 实例，改为接收上报事件推入 SC 队列

### 4.3 SSH 连接池化

**当前**: `self._ssh: Optional[RemoteWorkstation]` (单实例)，通过 `TaskRunner.get_ssh()` 懒加载。

**改造后**: `self._ssh_pool: dict[str, RemoteWorkstation]` (按工作站 ID 索引)

```python
# 当前 (engine/task_runner.py)
self._ssh: Optional[RemoteWorkstation] = None
self._ssh_lock = threading.RLock()

# 改造后
self._ssh_pool: dict[str, RemoteWorkstation] = {}
self._ssh_locks: dict[str, threading.RLock] = {}
```

### 4.4 屏障机制改造

**当前**: `BarrierCoordinator` 使用全局 `threading.Event` (`_barrier_passed`)。当 `state.all_configs_completed_at_step("meshing")` 返回 True 时，设置屏障事件并启动 Solver 线程（串行执行：同一时刻仅一个构型求解）。

```python
# 当前 (engine/scheduler/barrier.py)
if self.state.all_configs_completed_at_step("meshing"):
    self._barrier_passed.set()
    self._dispatch_solver_tasks()
```

**改造后**: 工作站级屏障，每台工作站独立判断

```python
# 每台工作站独立判断
if self.state.all_configs_completed_at_step("meshing", workstation_id=ws_id):
    self._barrier_passed[ws_id].set()
    self._dispatch_solver_tasks(workstation_id=ws_id)
```

### 4.5 PipelineControl 适配

**当前**: `PipelineControl`（`engine/scheduler/control.py`）通过 `transition_lock` 序列化 pause/resume/stop 状态变更，防止 TOCTOU 竞态。所有子模块通过 `paused_event` / `stopped_event` 响应控制信号。

**改造后**: `PipelineControl` 需扩展支持多工作站维度的状态管理，但核心 pause/resume/stop 语义不变。`LocalWorker` 侧需独立的控制事件（通过 RPC 下发 pause/stop 命令）。

### 4.6 PauseGuard / RetryManager 跨语言复用

**当前**: `PauseGuard`（`engine/scheduler/utils.py`）封装了分散在各子模块中的暂停检查逻辑。`RetryManager` 统一管理重试状态机（Running → Retrying → Error）。

**改造后**: `LocalWorker` 侧需要实现等价的暂停检查和重试逻辑。建议将重试策略通过 RPC 下发，LocalWorker 执行时遵循相同的重试语义。

---

## 5. IPC 协议扩展

### 5.1 当前协议字段名

**重要**：当前 IPC 协议使用以下字段名（非 `cmd`/`payload`）：

```json
{
    "command": "命令名",
    "params": { ... },
    "request_id": "唯一请求ID"
}
```

响应格式：
```json
{
    "status": "ok" | "error",
    "data": { ... },
    "message": "描述信息",
    "request_id": "与请求相同的ID"
}
```

### 5.2 新增命令

| 命令常量 | 值 | 方向 | 用途 |
|---------|---|------|------|
| `CMD_WORKER_REGISTER` | `"worker_register"` | LocalWorker → Daemon | Worker 上线注册 |
| `CMD_WORKER_HEARTBEAT` | `"worker_heartbeat"` | LocalWorker → Daemon | 心跳保活（每 30 秒） |
| `CMD_WORKER_STEP_COMPLETE` | `"worker_step_complete"` | LocalWorker → Daemon | 上报步骤完成 |
| `CMD_WORKER_STEP_ERROR` | `"worker_step_error"` | LocalWorker → Daemon | 上报步骤失败 |
| `CMD_WORKER_EXECUTE` | `"worker_execute"` | Daemon → LocalWorker | 下发执行指令 |
| `CMD_WORKER_FILE_READY` | `"worker_file_ready"` | LocalWorker → Daemon | 上报 STEP 文件就绪 |
| `CMD_WORKER_SCDOC_UPLOADED` | `"worker_scdoc_uploaded"` | LocalWorker → Daemon | 上报 SCDOC 已上传 |
| `CMD_COLLECT_RESULTS` | `"collect_results"` | LocalWorker → Daemon | 请求拉取暂存结果 |
| `CMD_COLLECT_ACK` | `"collect_ack"` | LocalWorker → Daemon | 确认结果已接收 |

### 5.3 协议消息格式（使用实际字段名）

```json
// Worker 注册
{
    "command": "worker_register",
    "params": {
        "worker_id": "local-pc-01",
        "capabilities": ["sw", "sc"],
        "hostname": "DESKTOP-XYZ"
    },
    "request_id": "a1b2c3d4"
}

// 执行指令下发
{
    "command": "worker_execute",
    "params": {
        "step": "sw",
        "config_names": [1, 2, 3],
        "params": {}
    },
    "request_id": "e5f6g7h8"
}

// 步骤完成上报
{
    "command": "worker_step_complete",
    "params": {
        "step": "sc",
        "config_name": 5,
        "output_files": ["D:\\scdoc\\5.scdoc"]
    },
    "request_id": "i9j0k1l2"
}
```

### 5.4 双端同步要求

新增命令常量必须同时更新：
- Python 侧：`ipc/protocol.py`（`CMD_*` 常量）
- Rust 侧：`autofluid-tui/src/ipc/protocol.rs`（`CMD_*` 常量）
- 验证测试：`tests/test_ipc_protocol.py`

---

## 6. 配置层改造

### 6.1 当前配置系统

当前配置采用**三层叠加**机制（优先级从高到低）：
1. **环境变量**（`AUTOFLUID_*`）— 最高优先级
2. **TOML 配置文件**（`autofluid_config.toml`）— 通过 `reload_config_from_toml()` 加载
3. **Python 硬编码默认值**（`engine/config.py` 中的 `LOCAL_PATHS`、`REMOTE_CONFIG`、`ENGINE_CONFIG` 等）

TOML 支持 `${VAR}` 语法引用环境变量（如 `password = "${AUTOFLUID_SSH_PASSWORD}"`）。

**关键共享字段**：`autofluid_config.toml` 同时被 Python 配置加载和 Rust TUI 设置页面读取，共享字段名必须保持一致的 `snake_case`。

### 6.2 工作站配置改造

**当前** (`autofluid_config.toml` 中的 `[remote_config]` 段):

```toml
[remote_config]
host = "WORKSTATION_A_OCAR_REACHABLE_HOST"
port = 22
username = "ps"
scdoc_dir = 'D:\xkz_1020\scdoc'
# ... 其他路径
```

**改造后** — 新增 `[[workstations]]` 数组段：

```toml
# 向后兼容：默认工作站（等价于 workstations[0]）
[remote_config]
host = "WORKSTATION_A_OCAR_REACHABLE_HOST"
port = 22
username = "ps"
password = "${AUTOFLUID_SSH_PASSWORD}"
scdoc_dir = 'D:\xkz_1020\scdoc'
msh_dir = 'D:\xkz_1020\msh'
result_dir = 'D:\xkz_1020\case'
flag_dir = 'D:\xkz_1020\flags'
conda_env = "pyfluent"
conda_exe = 'C:\ProgramData\anaconda3\Scripts\conda.exe'
mpi_bin_dir = 'C:\Program Files\ANSYS Inc\v241\fluent\fluent24.1.0\multiport\mpi\win64\intel2021\bin'

# 多工作站配置（新增）
[[workstations]]
id = "WS-A"
host = "WORKSTATION_A_ROUTABLE_IP"
port = 22
username = "ps"
password = "${AUTOFLUID_WS_A_PASSWORD}"
scdoc_dir = 'D:\xkz_1020\scdoc'
msh_dir = 'D:\xkz_1020\msh'
result_dir = 'D:\xkz_1020\case'
flag_dir = 'D:\xkz_1020\flags'
postprocess_script = 'D:\xkz_1020\batch_postprocess_gen4.py'
postprocess_output_dir = 'D:\xkz_1020\results'
notes = "现有工作站，已配置好环境"

[[workstations]]
id = "WS-B"
host = "WORKSTATION_B_ROUTABLE_IP"
port = 22
username = "ps"
password = "${AUTOFLUID_WS_B_PASSWORD}"
# ... 同上路径结构

[[workstations]]
id = "WS-C"
host = "WORKSTATION_C_ROUTABLE_IP"
port = 22
username = "ps"
password = "${AUTOFLUID_WS_C_PASSWORD}"
# ... 同上路径结构
```

> 注意：`workstations[].host` 必须填写从服务器 A（例如 ocar）所在网络位置可路由、可 SSH 访问的工作站地址，不能直接沿用仅本地 Windows PC 内网可达的 `[IP]`。部署前需在服务器 A 上逐台验证 `ssh <username>@<workstations.host>` 与端口、防火墙、NAT/公网映射可用性。

### 6.3 Python 侧 TypedDict 扩展

在 `engine/config.py` 中新增：

```python
class WorkstationConfig(TypedDict):
    id: str
    host: str
    port: int
    username: str
    password: str
    scdoc_dir: str
    msh_dir: str
    result_dir: str
    flag_dir: str
    postprocess_script: str
    postprocess_output_dir: str
    notes: str

WORKSTATIONS: list[WorkstationConfig] = []
```

`reload_config_from_toml()` 需新增对 `[[workstations]]` 数组段的解析。

### 6.4 环境变量

```bash
# 服务器 A
AUTOFLUID_WS_A_PASSWORD=xxx
AUTOFLUID_WS_B_PASSWORD=xxx  # 或留空（无需密码）
AUTOFLUID_WS_C_PASSWORD=xxx  # 或留空（无需密码）
AUTOFLUID_SERVER_HOST=OCAR_REACHABLE_HOST  # 服务器 A/ocar 地址
AUTOFLUID_SERVER_MODE=server
AUTOFLUID_IPC_HOST=0.0.0.0      # daemon 在 ocar 上监听
AUTOFLUID_IPC_PORT=9527
AUTOFLUID_IPC_AUTH_TOKEN=xxx    # server 模式必须配置；建议配合 SSH 隧道

# 本地 PC
AUTOFLUID_SERVER_HOST=OCAR_REACHABLE_HOST  # 服务器 A/ocar 地址
AUTOFLUID_SERVER_MODE=server
AUTOFLUID_IPC_HOST=OCAR_REACHABLE_HOST     # TUI 连接 ocar；使用隧道时填 127.0.0.1
AUTOFLUID_IPC_PORT=9527
AUTOFLUID_IPC_AUTH_TOKEN=xxx
```

`AUTOFLUID_SERVER_MODE=server` 时，TUI 不再尝试启动本地 `start_daemon.py`，只连接 `AUTOFLUID_IPC_HOST:AUTOFLUID_IPC_PORT`。如果 IPC 直接监听 `0.0.0.0`，必须配置 `AUTOFLUID_IPC_AUTH_TOKEN`；更推荐用 SSH 隧道或 VPN 暴露 IPC。

过渡阶段注意：server 模式下 ocar 后端可以启动并响应 `check` / `get_*` 等控制面命令，也支持 `worker_register` / `worker_heartbeat` 记录 LocalWorker 在线状态。但在 LocalWorker 执行适配器接入前会拒绝 `start`，避免 Linux 后端误调用本地 Windows-only 的 SolidWorks / SpaceClaim 执行路径。

### 6.5 Rust TUI 设置适配

当前 Rust TUI 设置系统（`autofluid-tui/src/settings/mod.rs`）有 7 分类 48 字段。新增多工作站配置后：

- 新增 `workstations` 分类（每个工作站一组字段）
- `config_io.rs` 需支持 TOML 数组段 `[[workstations]]` 的读写
- `settings_ui.rs` 需新增工作站列表页面

---

## 7. 数据库 Schema 变更

### 7.1 现有分片机制

**重要**：当前数据库已按构型组合指纹自动分片（`config_fingerprint.py`）。不同构型组合使用不同的 `.db` 文件，修改设计表后自动切换数据库，互不干扰。新增的 Schema 变更需要在每个分片数据库中生效。

### 7.2 steps 表扩展

```sql
-- 新增字段
ALTER TABLE steps ADD COLUMN workstation_id TEXT DEFAULT NULL;
ALTER TABLE steps ADD COLUMN slot_id INTEGER DEFAULT NULL;
```

### 7.3 result_delivery 表（新建）

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

---

## 8. 新增模块设计

### 8.1 LocalWorker (`engine/local_worker.py`)

本地 PC 侧独立进程，负责：

1. 执行 SW 阶段（通过 `SWExecutor`，win32com COM API）
2. 执行 SC 阶段（通过 `SCProcessPool` 管理，3 槽位常驻进程池）
3. 运行 `StepFileMonitor`（`FileStableDetector` 文件稳定性判定），检测 STEP 文件写入完成
4. 上传 SCDOC 文件到服务器 A
5. 上线后拉取后处理结果

```python
class LocalWorker:
    """本地 PC 侧 Worker 进程"""
    
    def __init__(self, server_host: str, server_port: int):
        self._server_host = server_host
        self._server_port = server_port
        self._sw_executor = SWExecutor(...)  # 复用现有 SWExecutor
        self._sc_pool = SCProcessPool()      # 复用现有进程池
        self._file_monitor = StepFileMonitor(...)
        self._running = False
    
    async def start(self):
        """启动 Worker"""
        await self._connect_to_daemon()
        await self._register()
        await self._start_heartbeat()
        await self._start_file_monitor()
        await self._main_loop()
    
    async def _register(self):
        """向 Daemon 注册"""
        await self._send({
            "command": "worker_register",
            "params": {
                "worker_id": self._worker_id,
                "capabilities": ["sw", "sc"],
            }
        })
    
    async def _on_file_ready(self, config_name: int, file_path: str):
        """文件就绪回调"""
        await self._send({
            "command": "worker_file_ready",
            "params": {
                "config_name": config_name,
                "file_path": file_path,
            }
        })
```

### 8.2 LocalWorkerAdapter (`engine/local_worker_adapter.py`)

Daemon 侧适配器，替代 TaskRunner 中直接调用 SW/SC 的逻辑：

```python
class LocalWorkerAdapter:
    """替代 TaskRunner 中直接调用 SW/SC 的逻辑，改为 RPC 下发"""
    
    def __init__(self, ipc_server: IPCServer):
        self._ipc_server = ipc_server
        self._worker_connection = None
        self._pending_tasks: dict[str, asyncio.Future] = {}
    
    async def execute_sw_step(self, configs: list[int]) -> bool:
        """向 LocalWorker 发送 worker_execute 命令"""
        future = asyncio.get_event_loop().create_future()
        task_id = f"sw-{uuid.uuid4()}"
        self._pending_tasks[task_id] = future
        
        await self._send_to_worker({
            "command": "worker_execute",
            "params": {
                "task_id": task_id,
                "step": "sw",
                "config_names": configs,
            }
        })
        
        # 等待 worker_step_complete 回调
        result = await asyncio.wait_for(future, timeout=3600)
        return result["success"]
    
    async def execute_sc_step(self, config_name: int) -> bool:
        """同上"""
        ...
```

### 8.3 ConfigAssigner (`engine/config_assigner.py`)

构型分配器，将构型按策略分配到工作站：

```python
class ConfigAssigner:
    """将构型按轮询取模策略分配到工作站"""
    
    def __init__(self, workstations: list[str], configs: list[int]):
        self._assignment: dict[str, list[int]] = {ws: [] for ws in workstations}
        for i, cn in enumerate(sorted(configs)):
            ws = workstations[i % len(workstations)]
            self._assignment[ws].append(cn)
    
    def get_workstation(self, config_name: int) -> str:
        """获取构型分配到的工作站"""
        for ws, configs in self._assignment.items():
            if config_name in configs:
                return ws
        raise ValueError(f"构型{config_name}未分配到任何工作站")
    
    def get_configs(self, workstation_id: str) -> list[int]:
        """获取工作站分配到的构型列表"""
        return self._assignment.get(workstation_id, [])
```

### 8.4 MeshingAssigner (`engine/scheduler/meshing_assigner.py`)

动态任务分配核心逻辑，采用槽位池模型（类似 SCProcessPool 的 3 槽位设计）：

```python
class MeshingAssigner:
    """动态任务分配器，将构型序列动态分配到空闲工作站槽位"""
    
    def __init__(self, configs: list[int], workstations: list[str]):
        self._configs = deque(sorted(configs))
        self._total = len(configs)
        self._assigned = 0
        self._ws_slots = {ws: [None, None, None] for ws in workstations}
        self._ws_idle_count = {ws: 3 for ws in workstations}
    
    def assign_next(self, workstation_id: str) -> Optional[int]:
        """尝试为空闲工作站分配下一个构型"""
        if not self._configs:
            return None
        if self._ws_idle_count[workstation_id] == 0:
            return None
        
        config = self._configs.popleft()
        self._assigned += 1
        slot_idx = self._ws_slots[workstation_id].index(None)
        self._ws_slots[workstation_id][slot_idx] = config
        self._ws_idle_count[workstation_id] -= 1
        return config
    
    def finalize(self):
        """所有构型分配完毕，向各工作站发送结束信号"""
        for ws in self._workstations:
            self.send(ws, "MESHING_COMPLETE")
```

### 8.5 ResultCollector (`engine/result_collector.py`)

服务器 A 侧结果收集：

```python
class ResultCollector:
    """从工作站拉取后处理结果到暂存区"""
    
    def __init__(self, ssh_pool: dict[str, RemoteWorkstation], staging_dir: str):
        self._ssh_pool = ssh_pool
        self._staging_dir = staging_dir
        self._staging_manager = StagingManager(staging_dir)
    
    async def collect_from_workstation(self, workstation_id: str, config_name: int):
        """从工作站拉取后处理结果"""
        ssh = self._ssh_pool[workstation_id]
        ws_config = get_workstation_config(workstation_id)
        
        # 拉取结果文件
        remote_path = f"{ws_config['postprocess_output_dir']}/{config_name}"
        local_path = f"{self._staging_dir}/{workstation_id}/{config_name}"
        
        await ssh.sftp_get(remote_path, local_path)
        
        # 记录到数据库
        self._staging_manager.mark_staged(workstation_id, config_name, local_path)
```

### 8.6 ResultFetcher (`client/result_fetcher.py`)

本地 PC 侧结果拉取：

```python
class ResultFetcher:
    """从服务器 A 拉取暂存结果到本地"""
    
    def __init__(self, server_host: str, server_port: int):
        self._server_host = server_host
        self._server_port = server_port
        self._ipc_client = None
    
    async def fetch_results(self, local_dir: str):
        """拉取所有未交付的结果"""
        # 请求结果列表
        response = await self._send({
            "command": "collect_results",
            "params": {}
        })
        
        for result in response["data"]["results"]:
            # 下载文件
            await self._download_file(result["staging_path"], local_dir)
            
            # 确认交付
            await self._send({
                "command": "collect_ack",
                "params": {
                    "workstation_id": result["workstation_id"],
                    "config_name": result["config_name"],
                    "filename": result["filename"],
                }
            })
```

---

## 9. 实施路线图

### 9.1 阶段规划

| 阶段 | 内容 | 预估工期 | 前置依赖 | 验证标准 |
|------|------|:-------:|---------|---------|
| **P0** | 多工作站配置 | 1-2 周 | 无 | `[[workstations]]` TOML 配置可加载；SSH 连接池可建立多连接；`ConfigAssigner` 正确分配构型 |
| **P1** | 工作站级屏障 | 1-2 周 | P0 | 每工作站独立 Meshing 屏障；屏障通过后仅启动该站 Solver；reset 正确清理对应屏障 |
| **P2** | PostProcess 阶段 | 1 周 | P1 | 完成检测逻辑基于结果文件（.cas/.dat）轮询；TUI 正确显示 PostProcess 状态 |
| **P3** | Daemon 拆分迁移 | 3-4 周 | P0, P1 | LocalWorker 可独立运行 SW/SC；Daemon 在 Ubuntu 上稳定运行；RPC 通信可靠 |
| **P4** | 结果暂存与交付 | 2 周 | P2, P3 | PostProcess 完成后自动拉取到暂存区；`result_delivery` 表正确记录状态 |
| **P5** | 本地 PC 上线收集 | 1-2 周 | P4 | 本地 PC 上线后可拉取暂存结果；交付确认正确更新；离线期间结果不丢失 |

### 9.2 依赖关系

```
P0: 多工作站配置 ──────────────────────────────────────────┐
([[workstations]] TOML + SSH 连接池 + ConfigAssigner)       │
    │                                                      │
    ▼                                                      │
P1: 工作站级屏障 ◄─────────────────────────────────────────┤
(BarrierMonitor 多实例化 + steps 表增加 workstation_id)      │
    │                                                      │
    ├──► P2: PostProcess 阶段                              │
    │    (完成判断依据：结果文件检测)                        │
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
    │
    └──► P2 可在当前单工作站架构上独立验证
```

### 9.3 验证顺序建议

1. **P0 + P1 在当前单工作站架构上验证**：先不迁移 Daemon，仅在本地 PC 上实现多工作站配置和工作站级屏障
2. **P2 在当前架构上验证**：新增 PostProcess 状态追踪
3. **P3 独立验证**：搭建 Ubuntu 服务器 A，部署 Daemon，LocalWorker 连接测试
4. **P4 + P5 集成验证**：完整的三层架构端到端测试

### 9.4 回滚策略

每个阶段应保持可回滚：

- **P0/P1**：`[remote_config]` 保留为单工作站模式的快捷入口，通过配置开关切换
- **P3**：LocalWorker 可降级为本地 Daemon 模式（即回退到当前架构）
- **P4/P5**：Collect 阶段为可选功能，不影响核心流水线运行

---

## 附录 A：与 architecture-refactoring-plan.md 的关系

本文档（daemon-split-plan.md）聚焦于**代码级拆分方案**，包含模块清单、IPC 协议扩展、配置层改造等实施细节。

`architecture-refactoring-plan.md` 包含更广泛的架构设计内容：
- 多工作站环境配置指引（WS-B/WS-C 的软件环境部署步骤）
- 屏障机制的动态分配方案详细设计（`WorkstationMesher` 自感知屏障）
- PostProcess 阶段的实际运行方式分析（内嵌在仿真脚本中自动执行）
- 文件流转路径设计（服务器 A 中转而非工作站直传）
- 资源评估（服务器 A 2 核 4GB 足够）

建议实施时两份文档配合阅读。

## 附录 B：风险清单

| # | 风险 | 严重度 | 缓解措施 |
|---|------|:------:|---------|
| R1 | LocalWorker 与 Daemon 间网络断开导致状态不一致 | 🔴 高 | 心跳机制 + 超时检测；Daemon 侧设置 Worker 离线超时（90 秒） |
| R2 | SW COM 对象跨进程不可用 | 🔴 高 | 严格拆分：Daemon 进程不导入 win32com；通过 RPC 隔离 |
| R3 | STEP 文件就绪事件丢失 | 🟡 中 | Daemon 侧定期轮询 LocalWorker 获取已完成的 STEP 列表 |
| R4 | SCDOC 文件上传到服务器 A 后转发失败 | 🟡 中 | 暂存区记录待转发文件，Daemon 定期重试 |
| R5 | IPC 明文传输敏感信息 | 🔴 高 | IPC 通信增加 TLS 加密层；或使用 SSH 隧道 |
| R6 | SSH 连接池泄漏 | 🟡 中 | 使用 context manager 管理连接生命周期 |
| R7 | 构型分配不均衡 | 🟢 低 | 轮询取模已是较均衡方案 |
| R8 | 工作站故障导致分配到该站的构型全部卡死 | 🔴 高 | 实现工作站健康检查；故障后支持构型重新分配 |
| R9 | 动态分配状态丢失 | 🔴 高 | 分配状态持久化到数据库；断点续传时重建分配队列 |
| R10 | 暂存区磁盘空间耗尽 | 🔴 高 | 设置磁盘使用阈值告警；自动清理已交付且超期的文件 |
| R11 | TOML 配置与 Rust TUI 设置页面字段名不一致 | 🟡 中 | 共享字段名必须保持 snake_case 一致；CI 测试验证 |
| R12 | DB 分片机制下 Schema 迁移遗漏 | 🟡 中 | `StateManager` 初始化时自动检查并执行 ALTER TABLE |

## 附录 C：关键代码文件行数参考

| 文件 | 当前行数 | 说明 |
|------|:-------:|------|
| `engine/daemon.py` | ~551 | PipelineDaemon 主进程 |
| `engine/scheduler/main.py` | ~691 | PipelineScheduler 调度核心 |
| `engine/scheduler/worker_pool.py` | ~515 | SC/Transfer 解耦双队列 |
| `engine/scheduler/barrier.py` | ~316 | 全局屏障 + Solver 调度 |
| `engine/scheduler/meshing_monitor.py` | ~295 | 串行 Meshing 执行器 |
| `engine/scheduler/sw_phase.py` | ~459 | SW 阶段处理 |
| `engine/scheduler/retry.py` | ~148 | 重试管理器 |
| `engine/scheduler/work_queue.py` | ~68 | 去重工作队列 |
| `engine/scheduler/control.py` | ~70 | PipelineControl 并发控制 |
| `engine/scheduler/utils.py` | ~330 | PauseGuard + 工具函数 |
| `engine/state_manager.py` | ~579 | SQLite WAL 状态管理 |
| `engine/task_runner.py` | ~269 | 任务执行协调器 |
| `engine/sc_process_pool.py` | ~636 | SC 3 槽位进程池 |
| `engine/file_monitor.py` | ~382 | FileStableDetector |
| `engine/config.py` | ~516 | TOML + 环境变量 + TypedDict |
| `engine/config_fingerprint.py` | ~31 | 配置指纹 MD5 |
| `ipc/server.py` | ~264 | TCP IPC 服务器 |
| `ipc/protocol.py` | ~129 | JSON 协议（11 命令） |
| `executor/sw_executor.py` | ~1734 | SolidWorks COM 自动化 |
| `executor/remote_executor.py` | ~1028 | 远程执行器 |
| `executor/cleaner.py` | ~174 | 文件清理器 |
| `executor/spaceclaim_transit.py` | ~985 | SC 转换脚本 |
| `utils/ssh_client.py` | ~1092 | SSH/SFTP 封装 |
| `utils/logger.py` | ~493 | 日志系统 |
