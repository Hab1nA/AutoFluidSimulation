# Daemon 系统代码拆分计划

> 文档版本：v1.0  
> 创建日期：2026-05-29  
> 基于：`docs/roadmap.md` 远期计划  
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
| `IPCServer` | ✅ | |
| `TaskRunner`（远程部分：Transfer/Meshing/Solver） | ✅ | |
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
┌───────────────────────────────────────────────────────────────────┐
│                     本地 Windows PC                                │
│                                                                   │
│  ┌─────────────────────────────────────────────────────────────┐  │
│  │                    PipelineDaemon (单一进程)                 │  │
│  │  ┌─────────────┐  ┌──────────────┐  ┌───────────────────┐  │  │
│  │  │ IPCServer   │  │ StateManager │  │ PipelineScheduler │  │  │
│  │  │ (TCP:9527)  │  │ (SQLite WAL) │  │  ├─ WorkerPool   │  │  │
│  │  └─────────────┘  └──────────────┘  │  ├─ BarrierCoord │  │  │
│  │                                      │  └─ MeshingMon   │  │  │
│  │                                      └───────────────────┘  │  │
│  │  ┌─────────────────────────────────────────────────────┐    │  │
│  │  │ TaskRunner (统一执行器)                              │    │  │
│  │  │  ├─ execute_sw_step()      ← 本地 (win32com)        │    │  │
│  │  │  ├─ execute_sc_step()      ← 本地 (SCProcessPool)   │    │  │
│  │  │  ├─ execute_transfer()     ← 远程 (paramiko SFTP)   │    │  │
│  │  │  ├─ execute_meshing()      ← 远程 (SSH)             │    │  │
│  │  │  └─ execute_solver()       ← 远程 (SSH)             │    │  │
│  │  └─────────────────────────────────────────────────────┘    │  │
│  │  ┌────────────────────┐                                     │  │
│  │  │ StepFileMonitor    │                                     │  │
│  │  │ (轮询本地 step_dir)│                                     │  │
│  │  └────────────────────┘                                     │  │
│  └─────────────────────────────────────────────────────────────┘  │
└───────────────────────────────────────────────────────────────────┘
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
│  │  │  ├─ execute_sw_step()  │  │      │  │  ├─ IPCServer (0.0.0.0) │ │ │
│  │  │  ├─ execute_sc_step()  │  │      │  │  ├─ StateManager        │ │ │
│  │  │  └─ StepFileMonitor    │  │      │  │  └─ PipelineScheduler   │ │ │
│  │  └────────────────────────┘  │      │  │     ├─ WorkerPool       │ │ │
│  │                              │      │  │     ├─ BarrierCoord    │ │ │
│  │  ┌────────────────────────┐  │      │  │     └─ MeshingMonitor  │ │ │
│  │  │ TUI Client (Rust)      │◄├──────┤  ├────────────────────────┤ │ │
│  │  └────────────────────────┘  │      │  │ TaskRunner (远程部分)   │ │ │
│  │                              │      │  │  ├─ execute_transfer()  │ │ │
│  │  ┌────────────────────────┐  │      │  │  ├─ execute_meshing()   │ │ │
│  │  │ ResultFetcher (新增)    │◄├──────┤  │  └─ execute_solver()    │ │ │
│  │  │ (结果拉取)              │  │      │  ├────────────────────────┤ │ │
│  │  └────────────────────────┘  │      │  │ ResultCollector (新增)  │ │ │
│  └──────────────────────────────┘      │  │ StagingManager (新增)   │ │ │
│                                        │  └────────────────────────┘ │ │
│                                        └───────────────────────────────┘ │
└──────────────────────────────────────────────────────────────────────────┘
```

---

## 3. 模块拆分清单

### 3.1 保留在本地 PC 的模块

| 模块 | 文件路径 | 职责说明 | 改动类型 |
|------|---------|---------|:-------:|
| `TaskRunner` 本地部分 | `engine/task_runner.py` | 保留 `execute_sw_step()` 和 `execute_sc_step()` | 拆分 |
| `SWExecutor` | `executor/sw_executor.py` | SolidWorks COM 自动化 | 无变更 |
| `SCProcessPool` | `engine/sc_process_pool.py` | SpaceClaim 进程池 | 无变更 |
| `SCScript` | `executor/spaceclaim_transit.py` | SpaceClaim 脚本 | 无变更 |
| `StepFileMonitor` | `engine/file_monitor.py` | 改造为远程上报模式 | 重构 |
| `ExcelReader` | `utils/excel_reader.py` | Excel 参数读取 | 无变更 |

### 3.2 迁移到服务器 A 的模块

| 模块 | 文件路径 | 职责说明 | 改动类型 |
|------|---------|---------|:-------:|
| `PipelineDaemon` | `engine/daemon.py` | 主控协调器，新增 LocalWorkerAdapter | 重构 |
| `StateManager` | `engine/state_manager.py` | SQLite 状态持久化，新增 workstation_id 字段 | 扩展 |
| `PipelineScheduler` | `engine/scheduler/main.py` | 调度核心，适配多工作站 | 重构 |
| `WorkerPool` | `engine/scheduler/worker_pool.py` | 工作线程池 | 轻微调整 |
| `BarrierCoordinator` | `engine/scheduler/barrier.py` | 改为工作站级屏障 | 重构 |
| `MeshingMonitor` | `engine/scheduler/meshing_monitor.py` | 网格监控 | 轻微调整 |
| `IPCServer` | `ipc/server.py` | 监听地址改为 0.0.0.0，新增 Worker 命令 | 扩展 |
| `IPCProtocol` | `ipc/protocol.py` | 新增 Worker 相关命令 | 扩展 |
| `RemoteExecutor` | `executor/remote_executor.py` | 远程执行 | 无变更 |
| `RemoteWorkstation` | `utils/ssh_client.py` | SSH 客户端 | 无变更 |
| `Logger` | `utils/logger.py` | 日志系统 | 无变更 |

### 3.3 新增模块

| 模块 | 文件路径 | 预估行数 | 职责说明 |
|------|---------|:-------:|---------|
| `LocalWorker` | `engine/local_worker.py` | ~400 | 本地 PC 侧 Worker 进程 |
| `LocalWorkerAdapter` | `engine/local_worker_adapter.py` | ~200 | Daemon 侧适配器 |
| `ConfigAssigner` | `engine/config_assigner.py` | ~80 | 构型分配器 |
| `MeshingAssigner` | `engine/scheduler/meshing_assigner.py` | ~150 | 动态任务分配 |
| `WorkstationMesher` | `engine/scheduler/workstation_mesher.py` | ~200 | 工作站状态管理 |
| `ResultCollector` | `engine/result_collector.py` | ~250 | 服务器 A 侧结果收集 |
| `StagingManager` | `engine/staging_manager.py` | ~150 | 暂存区管理 |
| `WorkerProtocol` | `ipc/worker_protocol.py` | ~100 | Worker RPC 协议 |
| `ResultFetcher` | `client/result_fetcher.py` | ~150 | 本地 PC 侧结果拉取 |

---

## 4. 核心拆分点详解

### 4.1 TaskRunner 分离

**当前代码** (`engine/task_runner.py`):

```python
class TaskRunner:
    def __init__(self, ...):
        self._ssh: Optional[RemoteWorkstation] = None  # 单实例 SSH
        self._ssh_lock = threading.RLock()
    
    def execute_sw_step(self) -> bool: ...      # 本地执行
    def execute_sc_step(self, ...) -> bool: ...  # 本地执行
    def execute_transfer(self) -> bool: ...      # 远程执行
    def execute_meshing(self) -> bool: ...       # 远程执行
    def execute_solver(self) -> bool: ...        # 远程执行
```

**拆分后**:

- **服务器端** `TaskRunner`: 只保留 `execute_transfer()`, `execute_meshing()`, `execute_solver()`
- **本地端** `LocalWorker`: 包含 `execute_sw_step()`, `execute_sc_step()`

### 4.2 StepFileMonitor 远程化

**当前**: Daemon 侧轮询本地 `step_dir`

**改造后**:
1. 本地 `LocalWorker` 运行 `StepFileMonitor`
2. 检测到文件就绪后，通过 RPC 命令 `worker_file_ready` 上报给 Daemon
3. Daemon 侧不再需要 `StepFileMonitor` 实例

### 4.3 SSH 连接池化

**当前**: `self._ssh: Optional[RemoteWorkstation]` (单实例)

**改造后**: `self._ssh_pool: dict[str, RemoteWorkstation]` (按工作站 ID 索引)

```python
# 当前
self._ssh: Optional[RemoteWorkstation] = None
self._ssh_lock = threading.RLock()

# 改造后
self._ssh_pool: dict[str, RemoteWorkstation] = {}
self._ssh_locks: dict[str, threading.RLock] = {}
```

### 4.4 屏障机制改造

**当前**: 全局屏障

```python
if self.state.all_configs_completed_at_step("Meshing"):
    self._barrier_passed.set()
    self._dispatch_solver_tasks()
```

**改造后**: 工作站级屏障

```python
# 每台工作站独立判断
if self.state.all_configs_completed_at_step("Meshing", workstation_id=ws_id):
    self._barrier_passed[ws_id].set()
    self._dispatch_solver_tasks(workstation_id=ws_id)
```

---

## 5. IPC 协议扩展

### 5.1 新增命令

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

### 5.2 协议消息格式

```json
// Worker 注册
{
    "cmd": "worker_register",
    "payload": {
        "worker_id": "local-pc-01",
        "capabilities": ["SW", "SC"],
        "hostname": "DESKTOP-XYZ"
    }
}

// 执行指令下发
{
    "cmd": "worker_execute",
    "payload": {
        "step": "SW",
        "config_names": [1, 2, 3],
        "params": {...}
    }
}

// 步骤完成上报
{
    "cmd": "worker_step_complete",
    "payload": {
        "step": "SC",
        "config_name": 5,
        "output_files": ["D:\\scdoc\\5.scdoc"]
    }
}
```

---

## 6. 配置层改造

### 6.1 工作站配置

**当前** (`engine/config.py`):

```python
REMOTE_CONFIG = {
    "host": "172.17.135.240",
    "port": 22,
    "username": "ps",
    "password": os.environ.get("AUTOFLUID_SSH_PASSWORD", ""),
    "scdoc_dir": r"D:\xkz_1020\scdoc",
    ...
}
```

**改造后**:

```python
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
        "notes": "现有工作站，已配置好环境",
    },
    {
        "id": "WS-B",
        "host": "172.17.135.89",
        "port": 22,
        "username": "ps",
        "password": None,  # 无需密码
        ...
    },
    {
        "id": "WS-C",
        "host": "172.17.135.254",
        "port": 22,
        "username": "ps",
        "password": None,  # 无需密码
        ...
    },
]

# 向后兼容：默认工作站
REMOTE_CONFIG = WORKSTATIONS[0]

# IPC 配置
IPC_CONFIG = {
    "host": "0.0.0.0",  # 从 127.0.0.1 改为 0.0.0.0
    "port": 9527,
}

# 步骤定义扩展
STEP_NAMES = ["SW", "SC", "Transfer", "Meshing", "Solver", "PostProcess", "Collect"]
```

### 6.2 环境变量

```bash
# 服务器 A
AUTOFLUID_WS_A_PASSWORD=xxx
AUTOFLUID_WS_B_PASSWORD=xxx  # 或留空（无需密码）
AUTOFLUID_WS_C_PASSWORD=xxx  # 或留空（无需密码）
AUTOFLUID_SERVER_HOST=192.168.1.100  # 服务器 A 地址

# 本地 PC
AUTOFLUID_SERVER_HOST=192.168.1.100  # 服务器 A 地址
```

---

## 7. 数据库 Schema 变更

### 7.1 steps 表扩展

```sql
-- 新增字段
ALTER TABLE steps ADD COLUMN workstation_id TEXT DEFAULT NULL;
ALTER TABLE steps ADD COLUMN slot_id INTEGER DEFAULT NULL;
```

### 7.2 result_delivery 表（新建）

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

1. 执行 SW 阶段（win32com COM API）
2. 执行 SC 阶段（SCProcessPool 管理）
3. 运行 StepFileMonitor，检测 STEP 文件写入完成
4. 上传 SCDOC 文件到服务器 A
5. 上线后拉取后处理结果

```python
class LocalWorker:
    """本地 PC 侧 Worker 进程"""
    
    def __init__(self, server_host: str, server_port: int):
        self._server_host = server_host
        self._server_port = server_port
        self._task_runner = TaskRunner()  # 仅本地部分
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
            "cmd": "worker_register",
            "payload": {
                "worker_id": self._worker_id,
                "capabilities": ["SW", "SC"],
            }
        })
    
    async def _on_file_ready(self, config_name: int, file_path: str):
        """文件就绪回调"""
        await self._send({
            "cmd": "worker_file_ready",
            "payload": {
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
            "cmd": "worker_execute",
            "payload": {
                "task_id": task_id,
                "step": "SW",
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

动态任务分配核心逻辑：

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
            "cmd": "collect_results",
            "payload": {}
        })
        
        for result in response["results"]:
            # 下载文件
            await self._download_file(result["staging_path"], local_dir)
            
            # 确认交付
            await self._send({
                "cmd": "collect_ack",
                "payload": {
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
| **P0** | 多工作站配置 | 1-2 周 | 无 | `WORKSTATIONS` 列表可配置；SSH 连接池可建立多连接；`ConfigAssigner` 正确分配构型 |
| **P1** | 工作站级屏障 | 1-2 周 | P0 | 每工作站独立 Meshing 屏障；屏障通过后仅启动该站 Solver；reset 正确清理对应屏障 |
| **P2** | PostProcess 阶段 | 1 周 | P1 | 完成检测逻辑基于结果文件（.cas/.dat）轮询；TUI 正确显示 PostProcess 状态 |
| **P3** | Daemon 拆分迁移 | 3-4 周 | P0, P1 | LocalWorker 可独立运行 SW/SC；Daemon 在 Ubuntu 上稳定运行；RPC 通信可靠 |
| **P4** | 结果暂存与交付 | 2 周 | P2, P3 | PostProcess 完成后自动拉取到暂存区；`result_delivery` 表正确记录状态 |
| **P5** | 本地 PC 上线收集 | 1-2 周 | P4 | 本地 PC 上线后可拉取暂存结果；交付确认正确更新；离线期间结果不丢失 |

### 9.2 依赖关系

```
P0: 多工作站配置 ──────────────────────────────────────────┐
(WORKSTATIONS 列表 + SSH 连接池 + ConfigAssigner)           │
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

- **P0/P1**：`REMOTE_CONFIG` 保留为单工作站模式的快捷入口，通过配置开关切换
- **P3**：LocalWorker 可降级为本地 Daemon 模式（即回退到当前架构）
- **P4/P5**：Collect 阶段为可选功能，不影响核心流水线运行

---

## 附录：风险清单

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
