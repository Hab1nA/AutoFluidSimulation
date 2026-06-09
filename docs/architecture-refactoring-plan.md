# AutoFluid 远期改进计划：从单机架构到分布式三层架构

> 文档版本：v1.2  
> 创建日期：2026-05-09  
> 最后更新：2026-05-20  
> 适用项目：液氧甲烷火箭发动机仿真流水线系统 (AutoFluid v2.7.0)

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
| SC | 本地 PC | C# SpaceClaimBridge.exe 进程检测模式（SCProcessPool 3 槽位池） | 流水线并发（3 Worker + 等待队列） |
| Transfer | 本地 PC → 工作站 | paramiko SFTP | 流水线并发（3 Worker） |
| Meshing | 远程工作站 | SSH + PowerShell Start-Process | 流水线并发（3 Worker）+ MeshingMonitor 串行管理 |
| Solver | 远程工作站 | SSH + PowerShell Start-Process | 全局屏障后并行启动 |

### 2.3 关键代码模块清单

#### Python 后端

| 模块 | 文件 | 行数 | 核心职责 |
|------|------|------|---------|
| PipelineDaemon | `engine/daemon.py` | ~500 | 后台守护进程，IPC 命令处理器，协调所有子系统 |
| PipelineScheduler | `engine/scheduler/main.py` | ~571 | DAG 调度主逻辑、全局屏障、Solver 分发 |
| Scheduler 子包 | `engine/scheduler/` | ~2100+ | `barrier.py` 屏障协调、`sw_phase.py` SW 阶段、`worker_pool.py` 3 工作线程池、`meshing_monitor.py` 网格监控、`retry.py` 重试管理、`utils.py` 辅助函数 |
| TaskRunner | `engine/task_runner.py` | ~217 | 各阶段执行逻辑编排（委托 executor 模块） |
| StateManager | `engine/state_manager.py` | ~579 | SQLite WAL 持久化状态（configs/steps/engine_state 表） |
| SCProcessPool | `engine/sc_process_pool.py` | ~567 | 3 槽位常驻进程池（文件协议 IPC，消除 SC 启动开销） |
| StepFileMonitor | `engine/file_monitor.py` | ~382 | FileStableDetector 文件写入完成检测（多采样稳定性判定） |
| Config | `engine/config.py` | ~516 | TOML 配置加载 + 环境变量覆盖 + TypedDict 定义 |
| ConfigFingerprint | `engine/config_fingerprint.py` | ~31 | 配置指纹 MD5 计算（数据库分片，不同构型组合自动切换 DB） |
| IPCServer | `ipc/server.py` | ~264 | TCP Socket 监听与命令分发 |
| IPCProtocol | `ipc/protocol.py` | ~129 | JSON over TCP 消息协议（11 个命令） |
| RemoteWorkstation | `utils/ssh_client.py` | ~504 | paramiko SSH/SFTP 封装 |
| Logger | `utils/logger.py` | ~493 | 会话级日志 + 广播处理器（TUI 增量拉取） |
| ExcelReader | `utils/excel_reader.py` | ~83 | Excel 参数表读取 |
| Cleaner | `executor/cleaner.py` | ~174 | 系统健康检查 + 中间文件清理 |
| RemoteExecutor | `executor/remote_executor.py` | ~282 | SFTP 传输 + 远程 Meshing/Solver 执行 |
| SWExecutor | `executor/sw_executor.py` | ~1395 | SolidWorks COM 自动化（直接 API 导出 STEP） |
| SCScript | `executor/spaceclaim_transit.py` | ~789 | SpaceClaim Python API 转换脚本 |

#### Rust TUI 前端

| 模块 | 文件 | 行数 | 核心职责 |
|------|------|------|---------|
| Main | `autofluid-tui/src/main.rs` | ~837 | 异步主循环、事件分发、UI 渲染调度 |
| DaemonManager | `autofluid-tui/src/daemon_mgr.rs` | ~107 | Daemon 子进程生命周期管理 |
| Theme | `autofluid-tui/src/theme.rs` | ~104 | ThemePalette 10 字段语义色板 + AppTheme 扩展 |
| IpcClient | `autofluid-tui/src/ipc/client.rs` | ~196 | tokio TCP 客户端、命令发送/响应接收 |
| IpcProtocol | `autofluid-tui/src/ipc/protocol.rs` | ~98 | Rust 端协议定义（与 Python 端同步） |
| AppState | `autofluid-tui/src/state/app_state.rs` | ~394 | 应用状态管理（表格数据、过滤、日志缓冲） |
| Table UI | `autofluid-tui/src/ui/table.rs` | ~90 | 状态表格渲染 |
| Header UI | `autofluid-tui/src/ui/header.rs` | ~45 | 标题栏渲染 |
| Layout | `autofluid-tui/src/ui/layout.rs` | ~73 | 整体布局分割 |
| CommandBar | `autofluid-tui/src/ui/command_bar.rs` | ~285 | 8 按钮快捷栏 + Daemon 子菜单 |
| Dialogs | `autofluid-tui/src/ui/dialogs.rs` | ~499 | 确认对话框、消息框渲染 |
| Scrollbar | `autofluid-tui/src/ui/scrollbar.rs` | ~118 | 垂直/水平滚动条（支持拖拽） |
| Logs UI | `autofluid-tui/src/ui/logs.rs` | ~317 | 双栏日志面板（信息提示 + 详细日志） |
| Command Handler | `autofluid-tui/src/event_handler/command.rs` | ~451 | 命令解析与执行 |
| KeyHandler | `autofluid-tui/src/event_handler/key_handler.rs` | ~544 | 键盘快捷键处理 |
| MouseHandler | `autofluid-tui/src/event_handler/mouse.rs` | ~1018 | 鼠标交互（悬停、点击、拖拽、滚轮） |
| Settings | `autofluid-tui/src/settings/mod.rs` | ~758 | 7 分类 48 字段设置管理 |
| SettingsUI | `autofluid-tui/src/settings/settings_ui.rs` | ~434 | 设置页面渲染 |
| SettingsIO | `autofluid-tui/src/settings/config_io.rs` | ~83 | TOML 配置读写 |
| SettingsValidation | `autofluid-tui/src/settings/validation.rs` | ~343 | 字段校验 |

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
│  │ LocalWorker  │◄├────────►│  │  │  ├─ Per-WS BarrierMonitor        │  │
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
| PostProcess | 工作站 A/B/C | Solver 完成（仿真脚本自动触发） | 构型级（随 Solver 自动进行） |
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
2. 执行 SC 阶段（C# SpaceClaimBridge.exe 进程检测模式，SCProcessPool 管理）
3. 运行 StepFileMonitor，检测 STEP 文件写入完成
4. 上传 SCDOC 文件到服务器 A
5. 上线后拉取后处理结果

LocalWorker 与服务器 A 的 Daemon 之间通过 RPC 通信（复用现有 IPC JSON-over-TCP 协议），新增以下命令：

| 命令 | 方向 | 用途 |
|------|------|------|
| `worker_register` | LocalWorker → Daemon | Worker 上线注册，报告本地 PC 能力 |
| `worker_heartbeat` | LocalWorker → Daemon | 心跳保活（每 30 秒） |
| `worker_poll` | LocalWorker → Daemon | 主动轮询领取待执行 SW/SC 任务 |
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

当前过渡实现采用环境变量控制远程控制面：

```text
# ocar daemon
AUTOFLUID_SERVER_MODE=server
AUTOFLUID_IPC_HOST=0.0.0.0
AUTOFLUID_IPC_PORT=9527
AUTOFLUID_IPC_AUTH_TOKEN=<shared-secret>

# 本地 TUI
AUTOFLUID_SERVER_MODE=server
AUTOFLUID_IPC_HOST=<OCAR_REACHABLE_HOST>
AUTOFLUID_IPC_PORT=9527
AUTOFLUID_IPC_AUTH_TOKEN=<shared-secret>
```

`server` 模式下 TUI 不启动本地 `start_daemon.py`，只连接远程 IPC。`auth_token` 是过渡期的最低限度保护；实际部署仍建议使用 SSH 隧道、VPN 或 TLS，避免裸露控制端口。

当前过渡实现已提供最小 `LocalWorker` 注册/心跳客户端。LocalWorker 会上报 `public_ip`、`candidate_hosts`、`reachable_host`、`connectivity_mode`、`ssh_port` 等网络诊断信息；这些信息用于帮助确认 ocar 视角的可达地址，但不会自动证明 SSH 可用。真正用于服务器端 SSH 的地址必须在 `[[workstations]]` 中配置，并由 ocar 侧探测验证。

**TaskRunner 本地部分替换**：

原来 `TaskRunner.execute_sw_step()` 和 `TaskRunner.execute_sc_step()` 直接在本地执行，改为通过 RPC 下发给 LocalWorker。新增 `LocalWorkerAdapter` 类：

```python
class LocalWorkerAdapter:
    """替代 TaskRunner 中直接调用 SW/SC 的逻辑，改为 RPC 下发"""

    def execute_sw_step(self) -> bool:
        # 向 LocalWorker 发送 worker_execute 命令
        # 等待 worker_step_complete 回调
        ...

    def execute_sc_step(self, config_name: int) -> bool:
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
    "host": "WORKSTATION_A_OCAR_REACHABLE_HOST",
    "port": 22,
    "username": "ps",
    ...
}

# 改造后 — 三台工作站真实信息
WORKSTATIONS = [
    {
        "id": "WS-A",
        "host": "WORKSTATION_A_OCAR_REACHABLE_HOST",
        "port": 22,
        "username": "ps",
        "password": os.environ.get("AUTOFLUID_WS_A_PASSWORD", ""),
        "scdoc_dir": r"D:\xkz_1020\scdoc",
        "msh_dir": r"D:\xkz_1020\msh",
        "result_dir": r"D:\xkz_1020\case",
        "postprocess_script": r"D:\xkz_1020\batch_postprocess_gen4.py",
        "postprocess_output_dir": r"D:\xkz_1020\results",
        "notes": "现有工作站，已配置好环境",
        ...
    },
    {
        "id": "WS-B",
        "host": "WORKSTATION_B_OCAR_REACHABLE_HOST",
        "port": 22,
        "username": "ps",
        "password": None,            # 可通过 ocar 到该地址的 SSH 隧道/公网映射连接
        "scdoc_dir": r"D:\xkz_1020\scdoc",
        "msh_dir": r"D:\xkz_1020\msh",
        "result_dir": r"D:\xkz_1020\case",
        "postprocess_script": r"D:\xkz_1020\batch_postprocess_gen4.py",
        "postprocess_output_dir": r"D:\xkz_1020\results",
        "notes": "待引入；无需密码登录",
        ...
    },
    {
        "id": "WS-C",
        "host": "WORKSTATION_C_OCAR_REACHABLE_HOST",
        "port": 22,
        "username": "ps",
        "password": None,            # 可通过 ocar 到该地址的 SSH 隧道/公网映射连接
        "scdoc_dir": r"D:\xkz_1020\scdoc",
        "msh_dir": r"D:\xkz_1020\msh",
        "result_dir": r"D:\xkz_1020\case",
        "postprocess_script": r"D:\xkz_1020\batch_postprocess_gen4.py",
        "postprocess_output_dir": r"D:\xkz_1020\results",
        "notes": "待引入；无需密码登录",
        ...
    },
]
```

当前实现采用轮询式下发：Daemon 侧 `LocalWorkerAdapter` 将任务放入内存队列，本地 PC 的 `LocalWorker` 通过 `worker_poll` 主动领取任务，再通过 `worker_step_complete` / `worker_step_error` 上报结果。这样 ocar 不需要主动连回本地 PC，适合本地 PC 位于 NAT/内网后的部署。

本地 PC 侧运行方式：`python main.py --worker-once` 仅做一次注册/心跳连通性检查；`python main.py --worker` 启动常驻 LocalWorker，按较短轮询周期领取 SW/SC 任务，并按心跳周期保活。

> 部署到 ocar 后，`WORKSTATIONS[*].host` 必须是从 ocar 所在网络位置可路由、可 SSH 握手的地址。不要把本地 Windows PC 才能访问的内网 `[IP]` 直接写入服务器端配置；如果工作站不在 ocar 可达网络内，应先配置公网端口映射、VPN、Tailscale、反向 SSH 隧道或等价链路。过渡期也可以保留原始 `host` 作为诊断信息，并额外配置 `reachable_host`；`AUTOFLUID_SERVER_MODE=server` 时 Python 后端会优先使用 `reachable_host` 作为实际 SSH 目标。

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

#### 4.2.6 工作站环境配置指引

在引入 WS-B 和 WS-C 之前，需要确保其软件环境与现有工作站 WS-A 保持一致。以下命令中的 `<WS_A_HOST>` / `<WS_B_HOST>` / `<WS_C_HOST>` 均指从 ocar 可达的工作站地址，而不是本地 PC 专属内网地址。

##### 4.2.6.1 现有工作站环境基线（WS-A）

首先应在 WS-A 上收集当前环境信息作为基线：

```powershell
# 1. 检查 Python 版本
ssh ps@<WS_A_HOST> "python --version"
ssh ps@<WS_A_HOST> "where python"

# 2. 检查 Conda 环境（如果使用）
ssh ps@<WS_A_HOST> "conda --version"
ssh ps@<WS_A_HOST> "conda env list"
ssh ps@<WS_A_HOST> "conda list -n <fluent_env_name>"

# 3. 检查 PyFluent 版本
ssh ps@<WS_A_HOST> "python -c 'import ansys.fluent.core; print(ansys.fluent.core.__version__)'"

# 4. 检查 Fluent 安装路径和版本
ssh ps@<WS_A_HOST> "dir 'C:\Program Files\ANSYS Inc'"
ssh ps@<WS_A_HOST> 'reg query "HKLM\SOFTWARE\ANSYS, Inc.\Fluent" /s 2>nul'

# 5. 导出当前环境为 requirements.txt（用于复现）
ssh ps@<WS_A_HOST> "pip freeze > D:\xkz_1020\ws_env_requirements.txt"

# 6. 检查 ANSYS 许可证配置
ssh ps@<WS_A_HOST> 'echo %ANSYSLMD_LICENSE_FILE%'
ssh ps@<WS_A_HOST> 'echo %ANSYS_VER%'

# 7. 检查关键目录结构
ssh ps@<WS_A_HOST> "dir D:\xkz_1020"
```

##### 4.2.6.2 新工作站环境配置步骤

对 WS-B 和 WS-C 分别按以下步骤配置：

**Step 1：基础环境**
```powershell
# WS-B：无需密码
ssh ps@<WS_B_HOST>

# WS-C：无需密码
ssh ps@<WS_C_HOST>
```

```powershell
# 安装 Miniconda/Anaconda（如果未安装）
# 下载地址：https://docs.conda.io/en/latest/miniconda.html
# 安装后创建与 WS-A 相同的 conda 环境
conda create -n fluento -c pyfluent pyfluent       # 示例，以 WS-A 实际环境为准
conda activate fluento
```

**Step 2：Python 及关键包**
```powershell
# 方式一：使用 WS-A 导出的 requirements.txt 复现
pip install -r D:\xkz_1020\ws_env_requirements.txt

# 方式二：手动安装关键包（版本号以 WS-A 实际版本为准）
pip install ansys-fluent-core==<version>
pip install paramiko
pip install openpyxl
pip install python-dotenv
```

**Step 3：ANSYS/Fluent 安装**
- 确保安装与 WS-A 相同版本的 ANSYS Fluent
- 配置许可证服务器环境变量 `ANSYSLMD_LICENSE_FILE`
- 验证：`fluent -version` 或通过 PyFluent 测试启动

**Step 4：目录结构**
```powershell
# 在工作站上创建必要的目录结构（与 WS-A 保持一致）
mkdir D:\xkz_1020
mkdir D:\xkz_1020\scdoc
mkdir D:\xkz_1020\msh
mkdir D:\xkz_1020\case
mkdir D:\xkz_1020\results
mkdir D:\xkz_1020\scripts
```

**Step 5：仿真/后处理脚本部署**
```powershell
# 将 WS-A 上的 Fluent journal 文件和后处理脚本复制到新工作站
# 从 WS-A 拉取脚本列表:
ssh ps@<WS_A_HOST> "dir D:\xkz_1020\*.py"
ssh ps@<WS_A_HOST> "dir D:\xkz_1020\scripts\*"

# 然后逐个 scp/sftp 到新工作站对应目录
```

**Step 6：连通性验证**
```powershell
# 从服务器 A 测试 SSH 连通性
ssh ps@<WS_B_HOST> "echo 'WS-B OK'"
ssh ps@<WS_C_HOST> "echo 'WS-C OK'"

# 测试 Python 环境
ssh ps@<WS_B_HOST> "python -c 'import ansys.fluent.core; print(\"PyFluent OK\")'"

# 测试 Fluent 可用性
ssh ps@<WS_B_HOST> "python -c 'import ansys.fluent.core as pyfluent; print(\"Fluent launch test OK\")'"
```

##### 4.2.6.3 环境一致性检查清单

| 检查项 | WS-A (`<WS_A_HOST>`) | WS-B (`<WS_B_HOST>`) | WS-C (`<WS_C_HOST>`) |
|--------|:---:|:---:|:---:|
| Windows 版本 | ✅ 已确认 | ⬜ 待检查 | ⬜ 待检查 |
| Python 版本 | ✅ 已确认 | ⬜ 待安装 | ⬜ 待安装 |
| Conda 环境 | ✅ 已确认 | ⬜ 待安装 | ⬜ 待安装 |
| PyFluent 版本 | ✅ 已确认 | ⬜ 待安装 | ⬜ 待安装 |
| ANSYS Fluent 版本 | ✅ 已确认 | ⬜ 待安装 | ⬜ 待安装 |
| 许可证配置 | ✅ 已确认 | ⬜ 待配置 | ⬜ 待配置 |
| 目录结构 (D:\xkz_1020\) | ✅ 已确认 | ⬜ 待创建 | ⬜ 待创建 |
| 仿真脚本部署 | ✅ 已确认 | ⬜ 待部署 | ⬜ 待部署 |
| SSH 免密/密码连接 | ✅ 已确认 | ⬜ 从 ocar 验证 | ⬜ 从 ocar 验证 |
| 22 端口可达 | ✅ 已确认 | ⬜ 待验证 | ⬜ 待验证 |

##### 4.2.6.4 注意事项

1. **PyFluent 版本敏感**：不同版本的 PyFluent 与 ANSYS Fluent 之间有版本对应关系，必须与 WS-A 保持一致，否则仿真结果可能不可复现
2. **许可证**：新工作站必须能够访问相同的 ANSYS 许可证服务器
3. **Windows 版本**：建议与 WS-A 同为 Windows 22H2 或更新版本，避免兼容性问题
4. **网络策略**：确保服务器 A 能够通过 SSH（端口 22）访问新工作站，注意防火墙规则
5. **磁盘空间**：仿真中产生的 cas/dat 文件可能较大，确保 `D:\xkz_1020\` 有足够空间

---

### 4.3 屏障机制适配

#### 4.3.1 当前屏障逻辑

当前全局屏障在 `engine/scheduler/barrier.py` 的 `_barrier_monitor_loop()` 中实现：

```python
# engine/scheduler/main.py
if self.state.all_configs_completed_at_step("meshing"):
    self._barrier_passed.set()
    self._dispatch_solver_tasks()
```

语义：**所有**构型的 Meshing 完成后，才解锁所有 Solver。这是一道全局屏障。

#### 4.3.2 目标：动态分配 + 自感知屏障

采用**动态任务分配 + 工作站自感知屏障**机制，替代静态分配方案：

**核心设计思想**：
- 将三个工作站视为类似于 SpaceClaim 的三个槽位池
- 待处理的构型排成一个序列，哪个槽位完成就将序列首项推入该工作站
- 当工作站发现不再接收到新的 Meshing 请求时，自动判定屏障通过

**工作流程**：

```
┌─────────────────────────────────────────────────────────────┐
│              分配模块 (MeshingAssigner)                      │
│  ┌──────────────────────────────────────────────────────┐   │
│  │ 构型序列: [1, 2, 3, 4, 5, 6, ..., N] (已知总数N)    │   │
│  └────────────────────────┬─────────────────────────────┘   │
│                           │                                 │
│           ┌───────────────┼───────────────┐                │
│           ▼               ▼               ▼                │
│    ┌───────────┐   ┌───────────┐   ┌───────────┐          │
│    │ 工作站 A  │   │ 工作站 B  │   │ 工作站 C  │          │
│    │  [Slot1-3]│   │  [Slot1-3]│   │  [Slot1-3]│          │
│    └─────┬─────┘   └─────┬─────┘   └─────┬─────┘          │
│          │               │               │                  │
│          ▼               ▼               ▼                  │
│    [1]处理中        [2]处理中        [3]处理中              │
│          │               │               │                  │
│          ▼               ▼               ▼                  │
│    完成→请求下一个   完成→请求下一个   完成→请求下一个        │
│          │               │               │                  │
│          ▼               ▼               ▼                  │
│    [4]处理中        [5]处理中        [6]处理中              │
│          │               │               │                  │
│          ...             ...             ...                │
│                                                            │
│  所有构型分配完毕后：                                        │
│  ┌──────────────────────────────────────────────────────┐   │
│  │ 分配模块向各工作站发送 MESHING_COMPLETE 信号         │   │
│  └────────────────────────┬─────────────────────────────┘   │
│                           │                                 │
│           ┌───────────────┼───────────────┐                │
│           ▼               ▼               ▼                │
│    收到信号+槽位全空   收到信号+槽位全空   收到信号+槽位全空  │
│    → 触发屏障          → 触发屏障          → 触发屏障       │
│    → 启动Solver       → 启动Solver       → 启动Solver      │
└─────────────────────────────────────────────────────────────┘
```

**时序示例**（假设有9个构型）：

| 时间点 | 工作站 A | 工作站 B | 工作站 C | 序列状态 |
|--------|---------|---------|---------|---------|
| t0 | [1] | [2] | [3] | [4,5,6,7,8,9] |
| t1 | [1]→完成→[4] | [2] | [3] | [5,6,7,8,9] |
| t2 | [4] | [2]→完成→[5] | [3] | [6,7,8,9] |
| t3 | [4] | [5] | [3]→完成→[6] | [7,8,9] |
| t4 | [4]→完成→[7] | [5]→完成→[8] | [6]→完成→[9] | [] |
| t5 | [7] | [8] | [9] | [] (分配完成) |
| t6 | 发送 MESHING_COMPLETE | 发送 MESHING_COMPLETE | 发送 MESHING_COMPLETE | - |
| t7 | [7]→完成→空→屏障通过 | [8]→完成→空→屏障通过 | [9]→完成→空→屏障通过 | - |
| t8 | 启动Solver(1,4,7) | 启动Solver(2,5,8) | 启动Solver(3,6,9) | - |

#### 4.3.3 关键设计要点

**1. 分配模块核心逻辑**：

```python
class MeshingAssigner:
    def __init__(self, configs: list[int], workstations: list[str]):
        self._configs = deque(sorted(configs))  # 有序构型队列
        self._total = len(configs)
        self._assigned = 0
        self._ws_slots = {ws: [None, None, None] for ws in workstations}
        self._ws_idle_count = {ws: 3 for ws in workstations}
    
    def assign_next(self, workstation_id: str) -> Optional[int]:
        """尝试为空闲工作站分配下一个构型"""
        if not self._configs:
            return None  # 无更多构型
        if self._ws_idle_count[workstation_id] == 0:
            return None  # 该工作站无空闲槽位
        
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

**2. 工作站自感知屏障逻辑**：

```python
class WorkstationMesher:
    def __init__(self, workstation_id: str):
        self._ws_id = workstation_id
        self._slots = [None, None, None]
        self._finalized = False  # 是否已收到结束信号
    
    def on_assign(self, config: int):
        """收到分配任务"""
        slot_idx = self._slots.index(None)
        self._slots[slot_idx] = config
        self._execute_meshing(config, slot_idx)
    
    def on_slot_completed(self, slot_idx: int, config: int):
        """槽位处理完成"""
        self._slots[slot_idx] = None
        
        # 尝试获取下一个任务
        next_config = self._assigner.assign_next(self._ws_id)
        if next_config:
            self.on_assign(next_config)
        elif self._finalized:
            # 无任务且收到结束信号 → 检查屏障条件
            self._check_barrier()
    
    def on_finalize(self):
        """收到结束信号"""
        self._finalized = True
        self._check_barrier()
    
    def _check_barrier(self):
        """检查屏障条件：收到结束信号 + 所有槽位空闲"""
        if self._finalized and all(slot is None for slot in self._slots):
            self._trigger_barrier()
    
    def _trigger_barrier(self):
        """启动本工作站的Solver阶段"""
        assigned_configs = self._get_processed_configs()
        for config in assigned_configs:
            self._start_solver(config)
```

**3. 容错机制**：

| 风险场景 | 缓解措施 |
|---------|---------|
| 工作站崩溃 | 文件检测完成后探测工作站存活；心跳超时触发告警 |
| 网络中断 | 使用 `FileStableDetector` 确保文件写入完成后再触发完成事件 |
| 重复分配 | 每次只分配一个构型；工作站空闲状态双重确认 |

**4. 状态持久化**：

`steps` 表需新增字段：

| 字段 | 类型 | 说明 |
|------|------|------|
| `workstation_id` | TEXT | 构型分配到的工作站ID |
| `slot_id` | INTEGER | 槽位编号（0-2） |
| `processed` | INTEGER | 是否已在此工作站处理（用于恢复） |

#### 4.3.4 与现有架构的集成

**新增模块**：

| 模块 | 文件路径 | 职责 |
|------|---------|------|
| `MeshingAssigner` | `engine/scheduler/meshing_assigner.py` | 动态任务分配核心逻辑 |
| `WorkstationMesher` | `engine/scheduler/workstation_mesher.py` | 工作站Meshing状态管理 |

**修改模块**：

| 模块 | 修改内容 |
|------|---------|
| `engine/scheduler/main.py` | 将 `_barrier_monitor_loop` 改为调用 `MeshingAssigner` |
| `engine/scheduler/barrier.py` | `_barrier_passed` 改为 dict，支持每工作站独立屏障 |
| `engine/state_manager.py` | `steps` 表新增 `workstation_id`、`slot_id` 字段 |
| `engine/task_runner.py` | `execute_meshing()` 增加 `workstation_id` 参数 |

#### 4.3.5 方案优势

| 维度 | 优势 |
|------|------|
| **负载均衡** | 动态适应各构型耗时差异，避免静态分配的"短板效应" |
| **资源利用率** | 始终保持三个槽位满载，最大化并行度 |
| **容错性** | 文件检测+心跳双重保障，工作站故障可及时发现 |
| **扩展性** | 工作站数量变化时自动适配，无需重新计算分配策略 |
| **屏障判定** | 分布式自感知，无单点瓶颈 |

#### 4.3.6 潜在风险与缓解

| 风险 | 严重度 | 缓解措施 |
|------|:------:|---------|
| 重置场景状态恢复 | 🔴 高 | 分配状态持久化到数据库；重置时重建分配队列 |
| 时序竞态 | 🟡 中 | 结束信号仅在所有构型分配完毕后发送；文件检测使用稳定性判定 |
| 工作站故障 | 🔴 高 | 心跳机制+超时检测；故障后可将构型重新分配到其他工作站 |
| 大文件传输中断 | 🟡 中 | 使用断点续传；记录已传输字节数 |

---

### 4.4 后处理阶段 (PostProcess)

#### 4.4.1 阶段定义与实际运行方式

PostProcess 是流水线中紧接在 Solver 之后的阶段。**关键理解**：后处理步骤并不是独立的流水线阶段，而是内嵌在仿真运行阶段所调用的 Python 脚本中的——仿真求解完成后，该 Python 程序会即刻调用另一个后处理脚本自动完成当前构型的后处理。因此：

- **完成一个仿真就会自动触发其对应的后处理**，不需要额外的调度触发
- **后处理是构型级任务**（每构型一个），而非工作站级任务
- **不需要 Solver 屏障来同步所有构型**：各构型的后处理独立进行，互不依赖

#### 4.4.2 完成判断依据（重要）

由于仿真和后处理由同一个 Python 程序执行，**不能以 Python 程序退出作为"仿真完成"的信号**——因为此时后处理正在运行，程序尚未退出。会导致 TUI 上仿真完成状态的显示不准确。

**正确做法**：以结果文件的产生作为各阶段的完成判断依据：

| 阶段 | 判断依据（结果文件） | 说明 |
|------|---------------------|------|
| Solver（仿真求解） | `.cas` + `.dat` 文件 | Fluent 求解完成后生成的 case/data 文件对 |
| PostProcess（后处理） | `待定` | 后处理脚本输出的结果文件格式待定，后续补充 |

具体实现方式：在远程工作站上，Fluent 求解完成后会生成 `.cas` / `.dat` 文件，Daemon 通过 SSH 轮询检测这些文件是否产生来判断 Solver 是否完成。后处理同理，检测后处理脚本预计输出的结果文件。

#### 4.4.3 对架构设计的影响

由于后处理内嵌在仿真脚本中自动执行，早期架构设想中的以下内容需要调整：

1. **不需要 Solver 屏障**：各构型 Solver 完成后自动进入 PostProcess，无需等待同工作站其他构型
2. **不需要独立的 PostProcess 调度逻辑**：`_solver_barrier_monitor_loop` / `_start_postprocess` 等方法不再需要
3. **Solver 完成状态标记时机变更**：从 "Python 程序退出时标记" 改为 "检测到 .cas/.dat 文件时标记"
4. **PostProcess 完成状态标记**：从 "Python 程序退出时标记" 改为 "检测到后处理输出文件时标记"

#### 4.4.4 代码改造要点

**config.py** 扩展：

```python
STEP_NAMES = ["sw", "sc", "transfer", "meshing", "solver", "postprocess", "collect"]

STEP_DISPLAY = {
    ...
    "postprocess": "后处理",
    "collect": "结果回收",
}

# 各阶段完成的文件判断依据
STEP_COMPLETION_FILES = {
    "meshing": {"pattern": "meshing_done_{config}.txt"},   # 标志文件
    "solver": {"pattern": "{config}.cas"},                  # cas+dat 文件对
    "postprocess": {"pattern": "待定"},                     # 后处理输出文件格式待定
}
```

**TaskRunner** 改造要点：

```python
def wait_solver_completion(self, config_name: int, workstation_id: str) -> bool:
    """等待仿真完成——轮询检测 .cas / .dat 文件而非 Python 进程结束"""
    ...

def wait_postprocess_completion(self, config_name: int, workstation_id: str) -> bool:
    """等待后处理完成——轮询检测后处理输出文件"""
    ...
```

**Scheduler** 改造要点：

```python
def _execute_solver_for_config(self, config_name: int, workstation_id: str):
    """执行单个构型的仿真求解"""
    # Solver 在远程启动后，不再等待 Python 进程结束
    # 改为等待 .cas/.dat 文件出现
    if self.runner.execute_solver(config_name, workstation_id):
        self.state.set_step_status(config_name, "solver", STATUS_RUNNING)
        if self.runner.wait_solver_completion(config_name, workstation_id):
            self.state.set_step_status(config_name, "solver", STATUS_COMPLETED)
            # Solver 完成后 PostProcess 由仿真脚本自动触发
            # 转入等待后处理输出文件
            self.state.set_step_status(config_name, "postprocess", STATUS_RUNNING)
            if self.runner.wait_postprocess_completion(config_name, workstation_id):
                self.state.set_step_status(config_name, "postprocess", STATUS_COMPLETED)
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
| `engine/scheduler/` | 🔴 重度 | 核心逻辑重写 | `_barrier_passed` 改为 dict；`_barrier_monitor_loop` 改为每工作站一个；`_dispatch_solver_tasks` 增加工作站参数；Solver/PostProcess 完成判断改为基于结果文件（.cas/.dat/后处理输出）轮询而非进程退出；`_worker_loop` 中 `_process_single_config` 需感知工作站分配；`_on_step_file_ready` 改为接收 RPC 上报；`reset_config` 需处理多工作站屏障重置 |
| `engine/task_runner.py` | 🔴 重度 | 接口重构 | `self._ssh` → `self._ssh_pool`；`get_ssh()` 增加 `workstation_id` 参数；`execute_transfer()` 需指定目标工作站；`execute_meshing()` / `execute_solver()` 增加 `workstation_id` 参数；`wait_solver_completion()` / `wait_postprocess_completion()` 改为基于结果文件轮询；`collect_results_from_workstation(ws_id)` 新增；`clean_step_files()` 需遍历所有工作站；`run_system_check()` 需检查所有工作站 |
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
| PostProcess 阶段 | ⭐⭐ | 后处理内嵌在仿真脚本中，无需独立调度；复杂度主要在完成检测逻辑（基于结果文件轮询而非进程退出） |
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
| R12 | **动态分配状态丢失** | 🔴 高 | Daemon 重启后，动态分配的构型→工作站映射关系丢失 | 分配状态持久化到 `steps` 表（`workstation_id`、`slot_id`、`processed` 字段）；断点续传时重建分配队列 |
| R13 | **reset 操作后屏障状态未正确清理** | 🟡 中 | `reset_config()` 需清理动态分配状态和屏障状态 | `reset_config` 遍历所有工作站的 `_barrier_passed[ws_id]` 并 clear；重置 `MeshingAssigner` 的分配队列 |
| R14 | **时序竞态** | 🟡 中 | 结束信号与最后一个任务的时序问题 | 结束信号仅在所有构型分配完毕后发送；文件检测使用 `FileStableDetector` 稳定性判定 |
| R15 | **工作站故障导致死锁** | 🔴 高 | 工作站崩溃但分配模块继续等待其完成信号 | 心跳机制+超时检测；故障后将未完成构型重新分配到其他工作站 |
| R16 | **Solver 与 PostProcess 的完成判断时机** | 🟡 中 | 由于仿真脚本内嵌后处理，如果不以文件产出为判断依据，会导致 Solver/PostProcess 完成状态提前标记 | Meshing 屏障通过后启动 Solver；Solver 和 PostProcess 均以检测结果文件（.cas/.dat 及后处理输出文件）为完成信号，而非 Python 程序退出信号 |

### 7.4 PostProcess 相关

| # | 风险 | 严重度 | 触发场景 | 缓解措施 |
|---|------|:------:|---------|---------|
| R17 | **后处理脚本无幂等性** | 🟡 中 | PostProcess 失败重试时重复处理已有结果 | 后处理脚本应实现幂等（检查输出是否已存在）；或每次执行前清理旧输出 |
| R18 | **PostProcess 结果文件格式待定** | 🟡 中 | 后处理由仿真脚本内嵌执行，但输出文件的格式/命名尚未确定，导致完成检测逻辑无法落地 | 待后续确认后处理脚本的实际输出文件格式后，补充到 `STEP_COMPLETION_FILES["PostProcess"]` 配置中 |
| R19 | **后处理输出文件名不确定** | 🟢 低 | 后处理脚本输出文件名可能包含时间戳等不确定因素 | 约定后处理脚本输出到固定目录，Daemon 拉取整个目录而非逐文件 |

### 7.5 Collect 相关

| # | 风险 | 严重度 | 触发场景 | 缓解措施 |
|---|------|:------:|---------|---------|
| R20 | **暂存区磁盘空间耗尽** | 🔴 高 | 大量结果未交付堆积 | 设置暂存区磁盘使用阈值告警；自动清理已交付且超期的文件；紧急时暂停流水线 |
| R21 | **交付确认丢失导致重复推送** | 🟡 中 | 本地 PC 接收完成但确认消息丢失 | 实现幂等推送：本地 PC 检查文件是否已存在，存在则跳过；Daemon 侧超时重试 |
| R22 | **大文件传输中断** | 🟡 中 | 网络不稳定导致文件传输中断 | 实现断点续传（记录已传输字节数）；或使用 rsync 等工具替代 SFTP |
| R23 | **本地 PC 长期离线导致暂存区无限增长** | 🟡 中 | 本地 PC 数周不在线 | 暂存文件设置最大保留天数（如 30 天）；超期后仅保留元数据（文件名、大小、校验和），实际文件删除 |
| R24 | **多批次结果混合** | 🟢 低 | 多次运行流水线，不同批次的结果混在一起 | 暂存目录按运行批次（session timestamp）隔离 |

### 7.6 跨模块交互风险

| # | 风险 | 严重度 | 触发场景 | 缓解措施 |
|---|------|:------:|---------|---------|
| R25 | **Python 端与 Rust 端协议不同步** | 🟡 中 | Python 端新增命令但 Rust 端未更新 | 协议版本号机制；Rust 端对未知命令优雅降级（显示原始消息） |
| R26 | **STEP_NAMES 长度变化导致 TUI 表格溢出** | 🟢 低 | 从 5 个步骤扩展到 7 个，终端宽度不足 | Rust TUI 表格列宽动态计算；窄终端时省略部分列或横向滚动 |
| R27 | **SQLite 数据库迁移** | 🟡 中 | `steps` 表新增 `workstation_id` 列，`result_delivery` 表新建 | 使用 `ALTER TABLE ADD COLUMN`（SQLite 支持）；新表使用 `CREATE TABLE IF NOT EXISTS`；编写数据库迁移脚本 |
| R28 | **环境变量命名冲突** | 🟢 低 | 新增多工作站的 `AUTOFLUID_WS_A_PASSWORD` 等环境变量 | 遵循现有 `AUTOFLUID_` 前缀命名规范；在 `.env` 文件中统一管理 |

---

## 8. 实施路线图

### 8.1 阶段规划

| 阶段 | 内容 | 预估工期 | 前置依赖 | 验证标准 |
|------|------|:-------:|---------|---------|
| **P0** | 多工作站配置 | 1-2 周 | 无 | `WORKSTATIONS` 列表可配置；SSH 连接池可建立多连接；`ConfigAssigner` 正确分配构型 |
| **P1** | 工作站级屏障 | 1-2 周 | P0 | 每工作站独立 Meshing 屏障；屏障通过后仅启动该站 Solver；reset 正确清理对应屏障 |
| **P2** | PostProcess 阶段 | 1 周 | P1 | 完成检测逻辑：Solver 完成后轮询检测 .cas/.dat 文件出现即为 Solver 完成；后处理输出文件检测逻辑待文件格式确认后补充；TUI 正确显示 PostProcess 状态 |
| **P3** | Daemon 拆分迁移 | 3-4 周 | P0, P1 | LocalWorker 可独立运行 SW/SC；Daemon 在 Ubuntu 上稳定运行；RPC 通信可靠 |
| **P4** | 结果暂存与交付 | 2 周 | P2, P3 | PostProcess 完成后自动拉取到暂存区；`result_delivery` 表正确记录状态 |
| **P5** | 本地 PC 上线收集 | 1-2 周 | P4 | 本地 PC 上线后可拉取暂存结果；交付确认正确更新；离线期间结果不丢失 |

### 8.2 建议的验证顺序

1. **P0 + P1 在当前单工作站架构上验证**：先不迁移 Daemon，仅在本地 PC 上实现多工作站配置和工作站级屏障，用单工作站模拟多工作站行为
2. **P2 在当前架构上验证**：新增 PostProcess 状态追踪，验证基于结果文件（.cas/.dat）的完成检测逻辑；后处理输出文件检测待格式确认后补充
3. **P3 独立验证**：搭建 Ubuntu 服务器 A，部署 Daemon，LocalWorker 连接测试
4. **P4 + P5 集成验证**：完整的三层架构端到端测试

### 8.3 回滚策略

每个阶段应保持可回滚：

- P0/P1：`REMOTE_CONFIG` 保留为单工作站模式的快捷入口，通过配置开关切换
- P3：LocalWorker 可降级为本地 Daemon 模式（即回退到当前架构）
- P4/P5：Collect 阶段为可选功能，不影响核心流水线运行

---

## 附录 A：关键代码引用索引

> **注意**：行号可能随代码更新而变化，建议以文件内搜索关键字为准。

| 引用点 | 文件 | 关键字 | 说明 |
|--------|------|--------|------|
| SSH 单实例 | `engine/task_runner.py` | `self._ssh` | `Optional[RemoteWorkstation]` |
| SSH 全局锁 | `engine/task_runner.py` | `self._ssh_lock` | `threading.RLock()` |
| 单工作站配置 | `engine/config.py` | `REMOTE_CONFIG` | 单字典 SSH 连接信息 |
| IPC 本地监听 | `engine/config.py` | `IPC_CONFIG` | `"host": "127.0.0.1"` |
| 步骤枚举 | `engine/config.py` | `STEP_NAMES` | `["sw", "sc", "transfer", "meshing", "solver"]` |
| 全局屏障检查 | `engine/scheduler/main.py` | `all_configs_completed_at_step` | 检查所有构型某步骤是否完成 |
| 屏障 Event | `engine/scheduler/barrier.py` | `_barrier_passed` | `dict[str, threading.Event]`（每工作站一个） |
| Worker 线程数 | `engine/scheduler/worker_pool.py` | `_num_workers` | `= 3` |
| 动态分配器 | `engine/scheduler/meshing_assigner.py` | `MeshingAssigner` | 动态任务分配核心逻辑 |
| 工作站 Mesher | `engine/scheduler/workstation_mesher.py` | `WorkstationMesher` | 工作站 Meshing 状态管理 |
| 屏障状态持久化 | `engine/scheduler/main.py` | `is_global_barrier_met` | 启动时恢复屏障状态 |
| 全局屏障查询 | `engine/daemon.py` | `barrier_passed` | IPC 响应中包含屏障状态 |
| Rust STEP_NAMES | `autofluid-tui/src/state/app_state.rs` | `STEP_NAMES` | `pub const STEP_NAMES: [&str; 5]` |
| Rust IPC 默认地址 | `autofluid-tui/src/ipc/client.rs` | `DEFAULT_HOST` | `"127.0.0.1"` |
| Rust Daemon 管理 | `autofluid-tui/src/daemon_mgr.rs` | `DaemonManager` | 本地启动 Daemon 子进程 |
| Python IPC 协议 | `ipc/protocol.py` | `CMD_` | 命令常量定义（11 个） |
| Rust IPC 协议 | `autofluid-tui/src/ipc/protocol.rs` | `CMD_` | 命令常量定义（与 Python 同步） |
| 数据库表结构 | `engine/state_manager.py` | `CREATE TABLE` | `configs` + `steps` + `engine_state` 表 |
| 全局屏障查询方法 | `engine/state_manager.py` | `all_configs_completed_at_step` | 按步骤查询完成状态 |
| SC 进程池 IPC 协议 | `engine/sc_process_pool.py` | `sc_cmd_` / `sc_result_` | 文件协议 JSON 命令/结果 |
| 配置指纹 | `engine/config_fingerprint.py` | `compute_config_fingerprint` | MD5 前 8 位，数据库分片 |
| 主题系统 | `autofluid-tui/src/theme.rs` | `ThemePalette` / `AppTheme` | 10 字段语义色板 + 扩展字段 |

## 附录 B：术语表

| 术语 | 含义 |
|------|------|
| Daemon | 后台守护进程，流水线调度核心 |
| LocalWorker | 本地 PC 上的工作进程，执行 SW/SC 等本地任务 |
| 工作站级屏障 | 每台工作站独立判断自身 Meshing 是否全部完成，完成后立即启动 Solver |
| 全局屏障 | 检查所有构型是否全部完成（当前实现，改造后不再使用） |
| 暂存区 (Staging) | 服务器 A 上临时存储后处理结果的目录 |
| 交付确认 (Delivery Ack) | 本地 PC 确认已成功接收结果的机制 |
| ConfigAssigner | 构型分配器，决定哪些构型分配到哪台工作站（静态分配策略） |
| MeshingAssigner | 动态任务分配器，将构型序列动态分配到空闲工作站槽位 |
| WorkstationMesher | 工作站 Meshing 状态管理器，负责接收任务、处理完成、自感知屏障 |
| 动态分配 | 构型按序列排队，哪个工作站槽位空闲就分配下一个构型 |
| 自感知屏障 | 工作站收到结束信号且所有槽位空闲时，自动判定屏障通过 |
