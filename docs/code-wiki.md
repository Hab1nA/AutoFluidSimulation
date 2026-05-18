# AutoFluid 仿真流水线总控系统 — Code Wiki

> **项目全称**：液氧甲烷火箭发动机仿真自动化流水线总控程序
> **版本**：v2.5.1
> **语言栈**：Python（后端引擎） + Rust（TUI 客户端）
> **目标平台**：本地 Windows PC + 远程 Windows 工作站

---

## 目录

1. [项目概述](#1-项目概述)
2. [整体架构](#2-整体架构)
3. [目录结构](#3-目录结构)
4. [核心模块详解](#4-核心模块详解)
   - 4.1 [engine — 调度引擎](#41-engine--调度引擎)
   - 4.2 [ipc — 进程间通信](#42-ipc--进程间通信)
   - 4.3 [utils — 工具模块](#43-utils--工具模块)
   - 4.4 [autofluid-tui — Rust TUI 客户端](#44-autofluid-tui--rust-tui-客户端)
5. [关键类与函数说明](#5-关键类与函数说明)
6. [数据流与状态机](#6-数据流与状态机)
7. [IPC 通信协议](#7-ipc-通信协议)
8. [依赖关系图](#8-依赖关系图)
9. [项目运行方式](#9-项目运行方式)
10. [测试体系](#10-测试体系)
11. [配置与环境变量](#11-配置与环境变量)

---

## 1. 项目概述

AutoFluid 是一套**液氧甲烷火箭发动机仿真自动化流水线**控制系统。它将原本需要人工逐步操作的 CAE 仿真流程自动化为一条五阶段流水线：

| 阶段 | 名称 | 执行位置 | 说明 |
|------|------|----------|------|
| SW | SolidWorks 导出 | 本地 PC | 通过 COM 自动化驱动 SolidWorks，按构型批量导出 STEP 文件 |
| SC | SpaceClaim 转换 | 本地 PC | C# Bridge 进程检测模式：SCProcessPool → SpaceClaimBridge.exe → SpaceClaim + transit.py 脚本，参数由环境变量传递，三相 GUI 就绪检测后自动转换 STEP 为 SCDOC |
| Transfer | 文件传输 | 本地 → 远程 | 通过 SSH/SFTP 将 SCDOC 上传到远程工作站 |
| Meshing | 网格划分 | 远程工作站 | 通过 SSH 启动远程 Fluent Meshing 后台任务 |
| Solver | 仿真求解 | 远程工作站 | 全局屏障通过后，并行启动所有构型的 Fluent Solver |

系统采用 **Daemon-TUI 分离架构**：后台引擎（Daemon）作为独立进程运行，TUI 客户端通过 IPC（TCP Socket）与之通信，支持客户端断连后重连、引擎独立运行。

---

## 2. 整体架构

```
┌─────────────────────────────────────────────────────────────┐
│                      用户交互层                              │
│  ┌──────────────────────────────────────────────────────┐   │
│  │  Rust TUI (ratatui + tokio)                         │   │
│  │  autofluid-tui/                                     │   │
│  └──────────────────────┬───────────────────────────────┘   │
└─────────────────────────┼──────────────────────────────────┘
                          │ IPC (TCP :9527)
┌─────────────────────────┼──────────────────────────────────┐
│                         ▼                                    │
│  ┌──────────────────────────────────────────────────────┐   │
│  │              IPC Server (ipc/server.py)               │   │
│  │              命令路由 & 请求/响应处理                    │   │
│  └──────────────────────┬───────────────────────────────┘   │
│                         │                                    │
│  ┌──────────────────────▼───────────────────────────────┐   │
│  │           PipelineDaemon (engine/daemon.py)           │   │
│  │           后台守护进程 — 协调所有子系统                  │   │
│  └───┬──────────┬──────────────┬──────────────┬─────────┘   │
│      │          │              │              │              │
│  ┌───▼───┐ ┌───▼──────┐ ┌────▼─────┐ ┌─────▼──────┐       │
│  │State  │ │Scheduler │ │TaskRunner│ │FileMonitor │       │
│  │Manager│ │          │ │          │ │            │       │
│  │(SQLite│ │(DAG调度) │ │(步骤执行)│ │(STEP文件   │       │
│  │ WAL)  │ │          │ │          │ │ 写入检测)  │       │
│  └───────┘ └──┬───────┘ └──┬───────┘ └────────────┘       │
│               │            │                                 │
│          ┌────▼────┐  ┌───▼────────┐                        │
│          │Worker   │  │SSH Client  │                        │
│          │ThreadPool│  │(paramiko)  │                        │
│          └─────────┘  └─────┬──────┘                        │
│                              │ SSH/SFTP                      │
│                    ┌────────▼─────────┐                      │
│                    │  远程 Windows 工作站 │                      │
│                    │  (Meshing/Solver) │                      │
│                    └──────────────────┘                      │
│                                                              │
│  ┌──────────────────────────────────────────────────────┐   │
│  │  本地 Windows 执行环境                                  │   │
│  │  ┌───────────┐  ┌─────────────────────────────────┐  │   │
│  │  │SolidWorks │  │  SCProcessPool (3槽位进程池)      │  │   │
│  │  │win32com   │  │  └─ SpaceClaimBridge.exe (C#)   │  │   │
│  │  │COM API    │  │      └─ SpaceClaim /RunScript   │  │   │
│  │  └───────────┘  │          └─ transit.py (V23)    │  │   │
│  │                 └─────────────────────────────────┘  │   │
│  └──────────────────────────────────────────────────────┘   │
│                                                              │
│                   后台引擎进程 (Daemon)                        │
└──────────────────────────────────────────────────────────────┘
```

**关键设计决策**：

- **Daemon-TUI 分离**：引擎与界面独立运行，TUI 崩溃不影响仿真任务
- **SQLite WAL 模式**：支持多进程并发读取状态，Daemon 独占写入
- **Producer-Consumer 模式**：文件监控器（Producer）检测 STEP 文件 → 推入队列 → Worker 线程（Consumer）执行下游步骤
- **SCProcessPool 进程池**：C# `SpaceClaimBridge.exe` 纯进程检测模式管理 SpaceClaim 并发调用（3 槽位 + 等待队列 + 断点续传 + 暂停/停止响应）
- **全局屏障同步**：所有构型 Meshing 完成后才解锁 Solver 阶段
- **断点续传**：状态持久化到 SQLite，重启后自动从断点恢复

---

## 3. 目录结构

```
autofluid/
├── main.py                  # 统一入口（--daemon / --client 模式选择）
├── start_daemon.py          # Daemon 启动脚本
├── start_client.py          # TUI 客户端启动脚本
├── start.bat                # Windows 一键启动批处理
├── requirements.txt         # Python 依赖
├── .env                     # 环境变量配置（敏感信息）
├── .gitignore
│
├── engine/                  # 🔧 核心调度引擎
│   ├── __init__.py
│   ├── config.py            # 全局硬编码配置 & 环境变量覆盖
│   ├── daemon.py            # 后台守护进程（PipelineDaemon）
│   ├── scheduler.py         # DAG 任务调度器（PipelineScheduler，兼容入口）
│   ├── scheduler/           # 调度器子模块包
│   │   ├── __init__.py      # 包入口，导出所有公开接口
│   │   ├── main.py          # 主调度器（PipelineScheduler，协调者）
│   │   ├── worker_pool.py   # 工作线程池管理（WorkerPoolManager）
│   │   ├── barrier.py       # 全局屏障 + Solver 调度（BarrierCoordinator）
│   │   ├── sw_phase.py      # SW 阶段执行、预扫描、重试（SWPhaseHandler）
│   │   └── retry.py         # 重试机制 + 暂停感知 sleep（RetryManager）
│   ├── task_runner.py       # 任务执行器（TaskRunner）
│   ├── state_manager.py     # 共享状态管理器（SQLite WAL）
│   ├── sc_process_pool.py   # SpaceClaim 进程并发池（SCProcessPool）
│   └── file_monitor.py      # STEP 文件目录监控器
│
├── executor/                # 🚀 外部执行器脚本
│   └── spaceclaim_transit.py # SpaceClaim 脚本：STEP → SCDOC 转换（V23 API）
│
├── bridge/                  # 🌉 C# SpaceClaim 桥接程序
│   └── SpaceClaimBridge/
│       ├── Program.cs       # .NET 4.8 主程序（纯进程检测模式）
│       ├── Program.NoRef.cs # C# 5 兼容版（免 ANSYS 引用编译）
│       ├── compile.bat      # 官方编译脚本
│       └── compile_noref.bat# 免引用编译脚本
│
├── ipc/                     # 🔌 进程间通信协议
│   ├── __init__.py
│   ├── protocol.py          # JSON over TCP 协议定义 & 消息序列化
│   └── server.py            # IPC 服务器（运行在 Daemon 中）
│
├── utils/                   # 🛠️ 工具模块
│   ├── __init__.py
│   ├── logger.py            # 日志系统 & 广播处理器
│   ├── ssh_client.py        # SSH/SFTP 客户端（paramiko 封装）
│   ├── excel_reader.py      # Excel 参数表读取器
│   └── tui_launcher.py      # Rust TUI 二进制查找与启动（main.py/start_client.py 共用）
│
├── autofluid-tui/           # 🦀 Rust TUI 客户端（唯一前端界面）
│   ├── Cargo.toml           # Rust 项目配置 & 依赖
│   └── src/
│       ├── main.rs          # 异步主循环（tokio + ratatui，已精简）
│       ├── daemon_mgr.rs    # Daemon 进程管理器
│       ├── ipc.rs           # IPC 模块入口
│       │   ├── protocol.rs  # IPC 协议（serde 序列化）
│       │   └── client.rs    # 异步 IPC 客户端（tokio TcpStream）
│       ├── state.rs         # 状态模块入口
│       │   ├── app_state.rs # 应用全局状态（AppState + 滚动条拖拽状态）
│       │   ├── log_buffer.rs# 日志环形缓冲区（LogBuffer）
│       │   └── filter.rs    # 日志过滤器
│       ├── event_handler.rs # 事件处理模块入口
│       │   ├── key_handler.rs # 键盘事件处理（含 settings 页面快捷键）
│       │   ├── command.rs   # 命令分发与执行
│       │   └── mouse.rs     # 鼠标事件处理（悬停/点击/拖拽/滚轮）
│       ├── settings.rs       # 设置页面模块入口
│       │   ├── mod.rs       # SettingCategory 枚举 & SettingsState 状态管理（5分类38字段）
│       │   ├── settings_ui.rs# 设置对话框渲染（字段编辑、按钮、滚动条）
│       │   ├── config_io.rs  # TOML/ .env 配置文件读写
│       │   └── validation.rs # 配置字段验证（路径、端口、模板占位符等）
│       └── ui.rs            # UI 渲染模块入口
│           ├── layout.rs    # 布局管理（AppLayout）
│           ├── header.rs    # 标题栏 & 信息栏渲染
│           ├── table.rs     # 状态表格渲染
│           ├── logs.rs      # 信息面板 & 详细日志渲染（含水平滚动）
│           ├── command_bar.rs # 命令输入栏 & 快捷按钮
│           ├── dialogs.rs   # 确认对话框 & 自检结果弹窗（含内容滚动）
│           └── scrollbar.rs # 通用滚动条组件（垂直/水平，支持拖拽）
│
├── tests/                   # 🧪 测试
│   ├── test_pause_start.py  # Pause/Start 功能验证（11 个场景）
│   ├── test_sw_step_naming.py # STEP 文件命名 & 环境变量覆盖测试
│   ├── test_detail_log.py   # 详细日志功能测试
│   └── test_sw_export_workflow.py # SW 导出工作流测试
│
├── ROADMAP.md               # 📋 分布式架构改造路线图
├── rebuild.bat              # 全项目一键构建脚本（Rust TUI + C# Bridge + 自动清理）
├── analyze_code.py          # 代码分析工具
├── logs/                    # 运行时日志输出目录
└── data/                    # SQLite 状态数据库目录
```

---

## 4. 核心模块详解

### 4.1 engine — 调度引擎

#### 4.1.1 config.py — 全局配置中心

集中定义所有路径、SSH 连接信息、步骤枚举、状态枚举和超时配置。支持通过环境变量覆盖默认值（`AUTOFLUID_*` 前缀），并可选加载 `.env` 文件。

**关键导出**：

| 常量/函数 | 说明 |
|-----------|------|
| `LOCAL_PATHS` | 本地 PC 路径配置（SW 模型、Excel、STEP 目录等） |
| `REMOTE_CONFIG` | 远程工作站 SSH 配置（host/port/username/password + 远程路径） |
| `STEP_NAMES` | 步骤名称列表：`["SW", "SC", "Transfer", "Meshing", "Solver"]` |
| `STEP_INDEX` | 步骤顺序索引：`{"SW": 0, "SC": 1, ...}` |
| `STEP_DISPLAY` | 步骤中文显示名 |
| `STEP_FILE_PATTERNS` | 步骤对应的文件命名模式 |
| `STATUS_*` | 状态枚举常量：Waiting / Running / Paused / Retrying / Completed / Error |
| `IPC_CONFIG` | IPC 通信配置（host/port/db_path/timeout） |
| `ENGINE_CONFIG` | 引擎运行参数（超时/重试/轮询间隔等） |
| `get_step_filename(step, config)` | 根据模式生成步骤文件名 |
| `ensure_directories()` | 创建必要目录 |
| `validate_config()` | 验证配置完整性 |

#### 4.1.2 daemon.py — 后台守护进程

`PipelineDaemon` 是整个系统的核心协调者，负责：

1. 初始化所有子系统（StateManager / TaskRunner / Scheduler / IPCServer）
2. 从 Excel 加载构型数据并同步到状态库
3. 启动 IPC 服务器接受客户端命令
4. 注册信号处理器实现优雅退出
5. 作为 IPC 命令的处理器（Handler），将客户端请求转发到调度器

**IPC 命令处理器**：

| 方法 | 对应命令 | 说明 |
|------|----------|------|
| `handle_start()` | `start` | 启动/恢复流水线（含状态机判断） |
| `handle_pause()` | `pause` | 暂停流水线 |
| `handle_stop()` | `stop` | 完全退出引擎 |
| `handle_check()` | `check` | 系统自检 |
| `handle_get_all_status()` | `get_all_status` | 获取所有构型状态 |
| `handle_get_statistics()` | `get_statistics` | 获取统计信息 |
| `handle_get_engine_status()` | `get_engine_status` | 获取引擎状态 |
| `handle_reset_step()` | `reset_step` | 重置构型步骤 |
| `handle_clean_step()` | `clean_step` | 清理步骤文件 |
| `handle_get_log_entries()` | `get_log_entries` | 增量拉取日志 |

#### 4.1.3 scheduler — DAG 任务调度器

> 文件：`engine/scheduler/` 包（原 `scheduler.py` 已拆分为职责单一的子模块）

`PipelineScheduler` 实现了基于文件监控的 Producer-Consumer 异步队列和全局同步屏障。拆分后各子模块职责：

| 子模块 | 类 | 职责 |
|--------|-----|------|
| `main.py` | `PipelineScheduler` | 主调度器，协调各子模块，提供 pause/resume/stop/reset 控制接口 |
| `worker_pool.py` | `WorkerPoolManager` | 管理 SC/Transfer/Meshing 工作线程池，从队列取任务执行 |
| `barrier.py` | `BarrierCoordinator` | 全局屏障监控，所有构型 Meshing 完成后启动 Solver |
| `sw_phase.py` | `SWPhaseHandler` | SW 阶段执行、输出文件预扫描（断点续传）、SW 重试准备 |
| `retry.py` | `RetryManager` | 带重试机制的任务执行包装器、暂停感知 sleep |

**调度流程**：

```
start_pipeline()
    │
    ├─ SW 阶段：execute_sw_step() → 批量导出 STEP
    │   └─ 提前启动文件监控器和工作线程池（边导出边处理）
    │
    ├─ 文件监控器（Producer）：StepFileMonitor 检测 STEP 文件写入完成
    │   └─ _on_step_file_ready() → 推入 _sc_queue
    │
    ├─ Worker 线程池（Consumer）：3 个工作线程从队列取任务
    │   └─ _worker_loop() → _process_single_config()
    │       ├─ SC 阶段：通过 SCProcessPool 进程池执行 SpaceClaim 转换
    │       ├─ Transfer 阶段：execute_transfer(config)
    │       └─ Meshing 阶段：execute_meshing(config) + wait_meshing_completion()
    │
    ├─ 全局屏障监控：_barrier_monitor_loop()
    │   └─ 所有构型 Meshing Completed → _dispatch_solver_tasks()
    │
    └─ Solver 调度：每个构型独立线程
        └─ _execute_solver_for_config() → execute_solver() + wait_solver_completion()
```

**并发控制**：

| 机制 | 用途 |
|------|------|
| `_paused` (threading.Event) | 暂停/恢复控制 |
| `_stopped` (threading.Event) | 停止信号 |
| `_barrier_passed` (threading.Event) | 全局屏障通过标志 |
| `_sc_queue` (queue.Queue) | SC 处理队列 |
| `_execute_with_retry()` | 带重试的任务执行包装器（递增等待时间） |

**关键方法**：

| 方法 | 说明 |
|------|------|
| `start_pipeline(_recursion_depth)` | 启动/继续流水线，含递归深度保护（最大3层） |
| `pause()` | 暂停流水线，将所有 Running/Retrying 步骤切换为 Paused |
| `resume()` | 恢复流水线，检测组件存活状态后决定恢复或重启 |
| `stop()` | 停止流水线，等待所有线程退出 |
| `reset_config(config, step)` | 重置指定构型的指定步骤及后续步骤 |
| `_execute_with_retry(config, step, func)` | 带重试机制的任务执行（状态转换：Running → Retrying → Error） |

#### 4.1.4 task_runner.py — 任务执行器

`TaskRunner` 封装了流水线中每个步骤的具体执行逻辑：

| 方法 | 阶段 | 执行方式 |
|------|------|----------|
| `execute_sw_step()` | SW | 三层降级连接 SW（GetActiveObject → Dispatch → subprocess），COM 直接导出 STEP |
| `execute_sc_step(config)` | SC | 通过 SCProcessPool 进程池管理 SpaceClaim 调用（3 槽位并发 + 等待队列、暂停/停止响应），优先 C# Bridge 降级 subprocess |
| `execute_transfer(config)` | Transfer | paramiko SFTP 上传 SCDOC |
| `execute_meshing(config)` | Meshing | SSH + PowerShell Start-Process 启动远程后台任务 |
| `wait_meshing_completion(config)` | Meshing | 轮询远程标志文件 |
| `execute_solver(config)` | Solver | SSH + PowerShell Start-Process 启动远程后台任务 |
| `wait_solver_completion(config)` | Solver | 轮询远程标志文件 |
| `run_system_check()` | — | 本地路径检查 + SSH 连通性 + 远程系统状态 |
| `clean_step_files(step, config)` | — | 清理本地和远程步骤文件 |

**SW 步骤的特殊设计**：

- 方法结构（薄入口 + 子方法编排）：
  - `execute_sw_step()` — 入口（校验参数 → 编排子步骤）
  - `_connect_sw()` — 三层降级连接：GetActiveObject → COM Dispatch → subprocess 启动 exe
  - `_open_sw_model()` — OpenDoc6 打开模型文件
  - `_import_design_table_with_retry()` — 设计表导入带容错重试：InsertFamilyTableOpen → COM 直接设参降级
  - `_rebuild_all_configs()` — ForceRebuildAll 重建所有构型（降级为逐个 EditRebuild3）
  - `_export_all_configs_to_step()` — 逐构型 ShowConfiguration2 → SaveAs 导出 STEP
  - `_verify_step_exports()` — 完成后安全网校验：逐构型检查 STEP 文件是否实际存在
  - `_disconnect_sw()` — COM 资源清理：CloseDoc → ExitApp → del + gc.collect → CoFreeUnusedLibraries

#### 4.1.5 state_manager.py — 共享状态管理器

`StateManager` 基于 SQLite WAL 模式实现持久化状态存储，支持多进程并发读取。

**数据库表结构**：

```sql
-- 构型列表
configs (config_name INTEGER PK, param1 REAL, param2 REAL, param3 REAL, param4 REAL)

-- 步骤状态
steps (id INTEGER PK AUTOINCREMENT,
       config_name INTEGER FK,
       step_name TEXT,
       status TEXT DEFAULT 'Waiting',
       retry_count INTEGER DEFAULT 0,
       error_message TEXT DEFAULT '',
       updated_at REAL,
       UNIQUE(config_name, step_name))

-- 引擎全局状态（KV 表）
engine_state (key TEXT PK, value TEXT)
-- 默认键值：engine_status, sw_macro_started, global_barrier_met, error_count
```

**关键方法**：

| 方法 | 说明 |
|------|------|
| `load_configs(configs)` | 从 Excel 同步构型数据（增量：新增/更新/删除） |
| `get_step_status(config, step)` | 获取步骤状态 |
| `set_step_status(config, step, status, error)` | 设置步骤状态 |
| `get_all_statuses()` | 获取所有构型所有步骤状态（TUI 渲染用） |
| `set_all_running_to_paused()` | 批量暂停 |
| `set_all_paused_to_running()` | 批量恢复 |
| `all_configs_completed_at_step(step)` | 全局屏障判断 |
| `reset_config_steps(config, from_step)` | 重置步骤（含后续步骤） |
| `reset_all()` | 全量重置 |
| `get_statistics()` | 获取全局统计信息 |

#### 4.1.6 file_monitor.py — STEP 文件监控器

`StepFileMonitor` 持续扫描 STEP 目录，检测新生成的 STEP 文件。当文件写入完成（大小稳定）后，通过回调通知调度器。

**核心组件**：

- `FileStableDetector`：通过多次采样文件大小判断写入完成（默认 2 秒稳定时间）
- `StepFileMonitor`：轮询扫描目录，支持暂停/恢复/重置
- 文件名解析：正则匹配 `model_gen4.SLDPRT_{config}.step` 提取构型号

**暂停/恢复机制**：

- `pause()`：设置 `_paused` 事件，监控循环进入等待
- `resume_and_reset()`：清除暂停标志 + 标记重置 + 唤醒监控线程
- 恢复后自动重置已处理文件集合并执行完整扫描

#### 4.1.7 sc_process_pool.py — SpaceClaim 进程并发池

`SCProcessPool` 管理 SpaceClaim 子进程的并发执行，替换了原先 task_runner.py 中内联的 SC 调用逻辑。

**设计动机**：

SpaceClaim 不向外部暴露 out-of-process COM 自动化接口（与 AutoCAD/SolidWorks 不同），因此只能通过命令行 `/RunScript` 驱动。同一时刻只能有一个 SpaceClaim 进程处理 `/RunScript` 命令（后启动的实例仅传递参数给首个实例），因此需要进程池统一管理并发。

**架构**：

```
                    ┌──────────────────────┐
                    │    SCProcessPool     │
                    │  ┌────────────────┐  │
                    │  │  JSON 持久化池   │  │
                    │  │  文件状态恢复    │  │
                    │  └──────┬─────────┘  │
                    │         │             │
                    │  ┌──────▼─────────┐  │
                    │  │  3 槽位进程池    │  │
                    │  │  (Slot/Idle)   │  │
                    │  └──────┬─────────┘  │
                    │         │             │
                    │  ┌──────▼─────────┐  │
                    │  │  等待队列       │  │
                    │  │  (Condition)   │  │
                    │  └────────────────┘  │
                    └──────────────────────┘
                              │
               ┌──────────────┼──────────────┐
               ▼              ▼              ▼
     ┌─────────────────┐ ┌──────────────┐ ┌─────────────────┐
     │SpaceClaimBridge │ │SpaceClaimBridge│ │SpaceClaimBridge │
     │.exe (C#)        │ │.exe (C#)      │ │.exe (C#)       │
     └────────┬────────┘ └──────┬───────┘ └────────┬────────┘
              │                  │                  │
              ▼                  ▼                  ▼
         SpaceClaim         SpaceClaim          SpaceClaim
         /RunScript         /RunScript          /RunScript
```

**C# Bridge 调用约定**：

```
SpaceClaimBridge.exe
  --script   <transit.py 路径>    # 必需：SpaceClaim IronPython 脚本
  --config   <构型编号>            # 必需：如 5
  --stepdir  <STEP 目录>          # 必需：STEP 输入文件目录
  --scdocdir <SCDOC 输出目录>      # 必需：SCDOC 输出目录
  [--timeout <秒数>]              # 可选：默认 300s
  [--sc-exe  <SpaceClaim.exe路径>] # 可选：自动检测

返回值: 0=成功 1=脚本失败 2=启动失败 3=输出验证失败 4=参数错误 5=超时
```

**核心流程**：

1. `acquire(config_name)` — 获取空闲槽位，若无则加入等待队列（Condition Variable 唤醒）
2. `_execute_in_slot(slot_id, config_name)` — 在槽位中执行任务：
   - 构建 Bridge 命令行参数
   - 通过环境变量传递配置参数（`AUTOFLUID_SC_CONFIG`/`AUTOFLUID_SC_STEP_DIR`/`AUTOFLUID_SC_SCDOC_DIR`）
   - 以 `CREATE_NO_WINDOW` 启动 `SpaceClaimBridge.exe` 子进程
   - 轮询 Bridge 退出 + 超时/暂停/停止检测
   - 验证 SCDOC 输出文件
3. `release(slot_id)` — 释放槽位，唤醒等待队列中的下一任务

**降级方案**：若 `sc_bridge` 路径无效或 `SpaceClaimBridge.exe` 不存在，直接以 subprocess 形式启动 `SpaceClaim.exe /RunScript="<script>" /Splash=False /Welcome=False /ExitAfterScript=True` 完成转换。

**Bridge 内部流程**（Program.cs）：

1. 参数解析 → 验证 STEP 输入文件存在
2. `FindSpaceClaimExe()` — 自动搜索多个 ANSYS 版本路径（v231/v232/v241）
3. 以 `/RunScript` + 环境变量传参模式启动 SpaceClaim
4. **三相 GUI 就绪检测**：
   - Phase 1：轮询 `MainWindowHandle` 和 `MainWindowTitle`，最长 120 秒等待进程出现
   - Phase 2：`WaitForInputIdle(15s)` 等待 WPF Dispatcher 消息泵空闲
   - Phase 3：固定延时 15 秒等待 SpaceClaim 完全加载稳定
5. 轮询 SCDOC 输出文件，2 秒间隔，直至超时或文件出现
6. 若 SpaceClaim 进程提前退出，进行最终输出验证
7. 返回 exit code

**池状态持久化**：槽位状态保存到 `{data_dir}/sc_process_pool.json`，支持 Daemon 重启后的进程存活检查与状态恢复。

**全量清理**：
- `do_first_cleanup()` — 首次进入 SC 阶段前 `taskkill /f /im SpaceClaim.exe` 清理所有残留进程
- `do_final_cleanup()` — 所有 SC 任务完成后再次清理
- `shutdown_all()` — 全量清理并重置所有槽位为 idle

---

### 4.2 ipc — 进程间通信

#### 4.2.1 protocol.py — 通信协议

基于 JSON 的 TCP Socket 协议，每条消息以换行符 `\n` 分隔。

**请求格式**：
```json
{"command": "start", "params": {}, "request_id": "a1b2c3d4"}
```

**响应格式**：
```json
{"status": "ok", "data": null, "message": "流水线已启动", "request_id": "a1b2c3d4"}
```

**命令常量**：

| 常量 | 值 | 说明 |
|------|----|------|
| `CMD_START` | `start` | 启动/继续流水线 |
| `CMD_PAUSE` | `pause` | 暂停流水线 |
| `CMD_STOP` | `stop` | 停止引擎 |
| `CMD_CHECK` | `check` | 系统自检 |
| `CMD_RESET_STEP` | `reset_step` | 重置步骤 |
| `CMD_CLEAN_STEP` | `clean_step` | 清理文件 |
| `CMD_GET_ALL_STATUS` | `get_all_status` | 获取所有状态 |
| `CMD_GET_STATISTICS` | `get_statistics` | 获取统计 |
| `CMD_GET_ENGINE_STATUS` | `get_engine_status` | 获取引擎状态 |
| `CMD_GET_LOG_ENTRIES` | `get_log_entries` | 增量拉取日志 |

#### 4.2.2 server.py — IPC 服务器

`IPCServer` 运行在 Daemon 进程中，监听 TCP Socket，每个客户端连接使用独立线程处理。通过命令处理器注册表（`_handlers`）实现命令路由。

**特性**：
- 支持多客户端同时连接
- 命令执行串行化（由 Daemon 的线程锁保护）
- 1 秒 accept 超时以便检查运行标志
- 按换行符分割消息流

---

### 4.3 utils — 工具模块

#### 4.3.1 logger.py — 日志系统

**核心组件**：

| 组件 | 说明 |
|------|------|
| `setup_logger(name)` | 创建 logger（文件 + 控制台双输出） |
| `init_session(type, timestamp)` | 初始化会话日志目录（按进程类型和时间戳分层） |
| `LogEntry` | 结构化日志条目数据类（支持 IPC 传输） |
| `LogBroadcastHandler` | 环形缓冲区日志处理器（供 TUI 增量拉取） |
| `install_broadcast_handler()` | 安装全局广播处理器到 root logger |

**日志来源分类**（`_classify_source`）：

| 来源 | 关键词 |
|------|--------|
| `remote_ps` | SSH, 远程, SFTP, PowerShell |
| `local_ps` | subprocess, SpaceClaim, tasklist |
| `com` | COM, SolidWorks, win32com, OpenDoc6 |
| `scheduler` | 调度, 屏障, pipeline, 流水线 |
| `ipc` | IPC, 客户端, socket |
| `system` | 其他 |

**轮询日志过滤**：自动过滤来自 IPC 轮询命令（`get_all_status`, `get_log_entries`, `get_engine_status`）的日志，避免刷屏。

#### 4.3.2 ssh_client.py — SSH 客户端

`RemoteWorkstation` 封装了 paramiko 的 SSH/SFTP 操作：

| 方法 | 说明 |
|------|------|
| `connect()` / `disconnect()` | 连接/断开 SSH |
| `is_connected()` / `ensure_connected()` | 连接状态检查与自动重连 |
| `upload_file(local, remote)` | SFTP 文件上传 |
| `exec_command(cmd, timeout)` | 同步执行远程命令 |
| `exec_background(cmd, flag_file)` | PowerShell Start-Process 启动独立后台进程 |
| `wait_for_flag(flag_file, timeout)` | 轮询标志文件判断任务完成 |
| `check_system(conda_exe)` | 远程系统自检 |
| `delete_remote_file(path)` | 删除远程文件 |
| `_ensure_remote_dir(dir)` | 递归创建远程目录 |

**后台进程机制**：使用 `PowerShell Start-Process` 启动 `cmd.exe` 子进程，该进程不依附于 SSH 会话，SSH 断开后继续运行。任务完成后写入标志文件。

#### 4.3.3 excel_reader.py — Excel 读取器

`read_model_configs(excel_path)` 从 Excel 文件读取构型参数配置：
- 从第 3 行开始读取（跳过 2 行表头）
- 第 1 列：构型名称（整数）
- 第 2-5 列：4 个参数（浮点数）
- 返回 `Dict[int, List[float]]`

---

### 4.4 autofluid-tui — Rust TUI 客户端

Rust 实现的 TUI 客户端（项目唯一前端界面），使用 `ratatui` 渲染 + `tokio` 异步运行时 + `crossterm` 终端事件。v2.2.0 起移除了旧版 Python Textual TUI，统一使用此 Rust TUI。

#### 4.4.1 main.rs — 异步主循环

`main.rs` 是 Rust TUI 的入口，负责初始化终端、事件循环和 IPC 轮询。同时提供两个通用工具函数供其他模块使用：

- `generate_request_id()` — 基于系统时间纳秒生成 8 位十六进制请求 ID（替代 uuid crate）
- `format_local_time(fmt)` — 通过 `windows-sys` 调用 `GetLocalTime` 实现本地时间格式化（替代 chrono crate）

```rust
main() → 初始化终端（raw mode + alternate screen + mouse capture）
    → run_app():
        ├─ 创建 AppState / LogBuffer / IpcClient / DaemonManager
        ├─ 主循环：
        │   ├─ 点击动画超时检测（按钮/对话框/详细日志行/设置字段 120ms 后清除）
        │   ├─ pending_command 处理（StartDaemon / StopDaemon / FullQuit 等）
        │   ├─ crossterm 事件轮询（50ms 首次 + 0ms 批量排空）
        │   │   ├─ 键盘事件 → key_handler::handle_key()（按 UiMode 路由：Normal / ConfirmDialog / CheckResult / Settings）
        │   │   ├─ 鼠标事件 → event_handler::mouse::handle_mouse()（悬停/点击/拖拽/滚轮；Settings 模式下双击字段编辑）
        │   │   └─ 终端大小变化 → update_terminal_size()
        │   ├─ 条件重绘（needs_redraw 时执行 do_redraw，UiMode::Settings 时渲染设置对话框）
        │   ├─ IPC 定时轮询（1 秒间隔：状态 + 增量日志；5 秒间隔：引擎状态）
        │   └─ 时钟刷新（500ms 间隔触发重绘更新标题栏时间）
        └─ 退出清理：disconnect IPC + stop Daemon
```

**鼠标事件处理**（`event_handler::mouse::handle_mouse`）：

| 事件类型 | 处理逻辑 |
|----------|----------|
| `Moved` | 悬停检测：表格行、快捷按钮、对话框按钮、详细日志行 |
| `ScrollUp/Down` | 按区域滚动：表格/信息面板/详细日志；Ctrl+滚轮水平滚动 |
| `Down` | 滚动条点击/拖拽开始、按钮点击、对话框按钮点击、表格/面板焦点切换、详细日志双击复制 |
| `Drag` | 滚动条拖拽（6 种区域：表格垂直、信息垂直/水平、详细垂直/水平、对话框垂直） |
| `Up` | 滚动条拖拽结束、按钮/对话框按钮释放触发动作 |

#### 4.4.2 state/ — 应用状态

| 结构体 | 说明 |
|--------|------|
| `AppState` | 全局状态（连接、构型数据、引擎信息、UI 模式、焦点、滚动位置、悬停/点击状态、滚动条拖拽状态、信息面板/详细日志自动滚动状态、设置页面状态 `SettingsState`） |
| `EngineInfo` | 引擎状态信息（engine_status / sw_macro_started / barrier_passed） |
| `LogBuffer` | 日志环形缓冲区（detail_buffer: 2000 条 / info_messages: 200 条 / info_generation: 新消息计数器） |
| `LogEntry` | 结构化日志条目（id / timestamp / level / source / message） |
| `FocusZone` | 焦点区域枚举（CommandInput / Table / InfoLog / DetailLog） |
| `UiMode` | UI 模式枚举（Normal / ConfirmDialog / CheckResult / **Settings**） |
| `ConfirmAction` | 确认操作枚举（ResetStep / CleanStep / FullQuit / StopDaemon） |
| `ScrollbarDragZone` | 滚动条拖拽区域枚举（TableVertical / InfoVertical / InfoHorizontal / DetailVertical / DetailHorizontal / DialogVertical） |
| `ScrollbarRenderedInfo` | 渲染后的滚动条位置信息（6 个可选的 (area, total, visible, scroll) 元组） |
| `FilterType` | 过滤类型枚举（Level / Source） |
| `SettingCategory` | 设置分类枚举（LocalPaths / RemoteConnection / RemoteDirs / StepPatterns / EngineConfig / OperationTimeouts） |
| `SettingsState` | 设置页面状态（config / focus / scroll / dirty / edit_buffer / undo_stack / validation_errors / path_status / hovered_field 等），支持 TOML 持久化、Ctrl+Z 撤销、实时路径校验 |
| `SettingsFocus` | 设置焦点（category_index / field_index / editing） |

#### 4.4.3 ipc/ — IPC 通信

| 结构体 | 说明 |
|--------|------|
| `IpcRequest` | IPC 请求（command / params / request_id），serde 序列化 |
| `IpcResponse` | IPC 响应（status / data / message / request_id） |
| `IpcClient` | 异步 IPC 客户端（tokio TcpStream），5 秒超时 |

#### 4.4.4 event_handler/ — 事件处理

| 模块 | 说明 |
|------|------|
| `key_handler` | 键盘事件分发（按 UiMode 和 FocusZone 路由，含设置页面快捷键：Tab/↑↓ 导航、Enter 编辑、←→ 光标移动、Ctrl+S 保存、Ctrl+Z 撤销） |
| `command` | 命令解析与执行（help/start/pause/check/status/reset/clean/daemon/quit/filter/export/settings） |

#### 4.4.5 ui/ — UI 渲染

| 模块 | 说明 |
|------|------|
| `layout` | 布局管理（`AppLayout`：header / info_bar / status_table / info_panel / detail_panel / cmd_input / quick_buttons） |
| `header` | 标题栏 + 信息栏渲染 |
| `table` | 构型状态表格渲染（含垂直滚动条、悬停高亮） |
| `logs` | 信息面板 + 详细日志面板渲染（含 Unicode 宽度感知换行、水平滚动偏移、自动滚动状态指示器） |
| `command_bar` | 命令输入栏 + 9 个快捷按钮（含 ⚙ Settings） |
| `dialogs` | 确认对话框 + 自检结果弹窗 + 设置页面全局背景遮罩（居中弹出层，支持内容滚动、按钮鼠标交互、自检结果自动换行与对称边距） |
| `scrollbar` | 通用滚动条组件（`VerticalScrollbar` / `HorizontalScrollbar`），支持 thumb 计算和拖拽定位 |
| `SettingsRenderInfo` | 设置对话框渲染结果（content_total_lines / content_visible_lines / scrollbar_area / button_bar_y / field_positions） |

#### 4.4.6 daemon_mgr.rs — Daemon 进程管理

`DaemonManager` 管理 Daemon 子进程的生命周期：
- `launch(project_dir)`：启动 Daemon 子进程（Windows 使用 CREATE_NO_WINDOW 标志）
- `stop()`：终止 Daemon 进程（Windows: taskkill /f /pid, Linux: kill）
- `is_running()`：检查进程是否存活

---

## 5. 关键类与函数说明

### Python 核心类

| 类 | 文件 | 职责 |
|----|------|------|
| `PipelineDaemon` | engine/daemon.py | 后台守护进程，协调所有子系统 |
| `PipelineScheduler` | engine/scheduler/main.py | DAG 任务调度，管理流水线执行 |
| `TaskRunner` | engine/task_runner.py | 各阶段任务的具体执行 |
| `StateManager` | engine/state_manager.py | SQLite 持久化状态管理 |
| `StepFileMonitor` | engine/file_monitor.py | STEP 文件目录监控 |
| `FileStableDetector` | engine/file_monitor.py | 文件写入完成检测 |
| `SCProcessPool` | engine/sc_process_pool.py | SpaceClaim 进程并发池（3 槽位 + 等待队列 + 断点续传） |
| `IPCServer` | ipc/server.py | IPC 服务器 |
| `RemoteWorkstation` | utils/ssh_client.py | SSH/SFTP 客户端 |
| `LogBroadcastHandler` | utils/logger.py | 日志广播处理器 |
| `LogEntry` | utils/logger.py | 结构化日志条目 |

### Rust 核心结构体

| 结构体 | 文件 | 职责 |
|--------|------|------|
| `AppState` | state/app_state.rs | 全局状态（含滚动条拖拽、悬停/点击动画状态、信息面板/详细日志自动滚动状态） |
| `LogBuffer` | state/log_buffer.rs | 日志环形缓冲区 |
| `IpcClient` | ipc/client.rs | 异步 IPC 客户端 |
| `IpcRequest` / `IpcResponse` | ipc/protocol.rs | IPC 消息结构 |
| `DaemonManager` | daemon_mgr.rs | Daemon 进程管理 |
| `AppLayout` | ui/layout.rs | UI 布局定义 |
| `VerticalScrollbar` | ui/scrollbar.rs | 垂直滚动条（渲染 + thumb 计算 + 拖拽定位） |
| `HorizontalScrollbar` | ui/scrollbar.rs | 水平滚动条（渲染 + thumb 计算 + 拖拽定位） |
| `ScrollbarThumbInfo` | ui/scrollbar.rs | 滚动条 thumb 位置/尺寸信息 |
| `ScrollbarRenderedInfo` | state/app_state.rs | 渲染后各区域滚动条位置缓存 |
| `SettingCategory` | settings/mod.rs | 设置分类枚举（LocalPaths / RemoteConnection / RemoteDirs / StepPatterns / EngineConfig），5 分类共 38 字段 |
| `SettingsState` | settings/mod.rs | 设置页面状态管理（TOML 持久化、Ctrl+Z 撤销、路径存在性校验、密码双格式存储） |
| `SettingsFocus` | settings/mod.rs | 设置焦点状态（category_index / field_index / editing） |
| `SettingsRenderInfo` | settings/settings_ui.rs | 设置对话框渲染结果信息（内容行数、滚动条区域、字段位置映射） |
| `LocalPaths` | settings/mod.rs | 本地路径配置结构体（9 个字段，TOML deserialize） |
| `RemoteConfig` | settings/mod.rs | 远程配置结构体（13 个字段，TOML deserialize） |
| `StepFilePatterns` | settings/mod.rs | 步骤文件模板结构体（4 个字段，含 `{config}` 占位符校验） |
| `EngineConfig` | settings/mod.rs | 引擎配置结构体（12 个字段，含布尔/数值类型解析） |

### 关键函数

| 函数 | 文件 | 说明 |
|------|------|------|
| `read_model_configs()` | utils/excel_reader.py | 从 Excel 读取构型参数 |
| `setup_logger()` | utils/logger.py | 创建配置好的 logger |
| `install_broadcast_handler()` | utils/logger.py | 安装日志广播处理器 |
| `get_step_filename()` | engine/config.py | 根据步骤和构型生成文件名 |
| `ensure_directories()` | engine/config.py | 创建必要目录 |
| `validate_config()` | engine/config.py | 验证配置完整性 |
| `handle_mouse()` | event_handler/mouse.rs (Rust) | 鼠标事件处理（悬停/点击/拖拽/滚轮） |
| `dispatch_command()` | event_handler/command.rs (Rust) | 命令分发与执行 |
| `handle_key()` | event_handler/key_handler.rs (Rust) | 键盘事件处理 |
| `parse_filter_arg()` | state/filter.rs (Rust) | 解析日志过滤参数 |

---

## 6. 数据流与状态机

### 引擎状态机

```
                  start()
    ┌─────────┐ ──────────► ┌─────────┐
    │ stopped │             │ running │ ◄──┐
    └─────────┘ ◄────────── └─────────┘    │
         ▲          stop()      │  pause()  │ resume()
         │                      ▼           │
         │               ┌─────────┐       │
         └───────────────│ paused  │ ──────┘
             stop()      └─────────┘
```

### 步骤状态机

```
Waiting ──► Running ──► Completed
               │  ▲
               │  │ retry
               ▼  │
           Retrying ──► Error
               │
               ▼
           Paused (用户暂停)
```

### 数据流

```
Excel (.xlsx)
    │ read_model_configs()
    ▼
StateManager (SQLite) ◄──── TUI 读取（IPC 查询）
    │
    ▼
PipelineScheduler
    │
    ├─► TaskRunner.execute_sw_step() ──► STEP 文件目录
    │                                        │
    │                                   StepFileMonitor
    │                                        │
    │                                   _on_step_file_ready()
    │                                        │
    │                                   _sc_queue
    │                                        │
    ├─► Worker 线程 ◄────────────────────────┘
    │   ├─ SCProcessPool → SpaceClaimBridge.exe
    │   │   └─ SpaceClaim (环境变量 /RunScript) ──► SCDOC 文件
    │   ├─ execute_transfer() ──► 远程工作站 (SFTP)
    │   └─ execute_meshing() ──► 远程后台任务 (SSH)
    │
    ├─► BarrierMonitor ──► 全局屏障通过
    │
    └─► Solver 线程 ──► execute_solver() ──► 远程后台任务
```

---

## 7. IPC 通信协议

### 协议规范

- **传输层**：TCP Socket（默认 `127.0.0.1:9527`）
- **编码**：UTF-8 JSON
- **消息分隔**：换行符 `\n`
- **超时**：5 秒（客户端）

### 命令一览

| 命令 | 参数 | 响应数据 | 说明 |
|------|------|----------|------|
| `start` | `{}` | null | 启动/继续流水线 |
| `pause` | `{}` | null | 暂停流水线 |
| `stop` | `{}` | null | 完全退出引擎 |
| `check` | `{}` | `{local_checks, remote_checks}` | 系统自检 |
| `reset_step` | `{config_name, step_name?}` | null | 重置步骤 |
| `clean_step` | `{step_name, config_name?}` | null | 清理文件 |
| `get_all_status` | `{}` | `{config: {step: status}}` | 获取所有状态 |
| `get_statistics` | `{}` | `{total_configs, steps, engine_status}` | 获取统计 |
| `get_engine_status` | `{}` | `{engine_status, sw_macro_started, barrier_passed}` | 获取引擎状态 |
| `get_log_entries` | `{since_id, limit, level_filter?, source_filter?}` | `{entries, latest_id, total}` | 增量拉取日志 |

---

## 8. 依赖关系图

### Python 模块依赖

```
main.py ──► engine/daemon.py ──► engine/config.py
                           ──► engine/state_manager.py ──► engine/config.py
                           ──► engine/task_runner.py ──► engine/config.py
                           │                          ──► utils/ssh_client.py
                           │                          ──► utils/logger.py
                           ──► engine/scheduler/main.py ──► engine/config.py
                           │                       ──► engine/state_manager.py
                           │                       ──► engine/file_monitor.py
                           │                       ──► engine/task_runner.py
                           │                       ──► utils/logger.py
                           ──► ipc/server.py ──► ipc/protocol.py
                           │                  ──► engine/config.py
                           ──► utils/logger.py
                           ──► utils/excel_reader.py
```

### Rust 模块依赖

```
main.rs ──► state/app_state.rs
        ──► state/log_buffer.rs
        ──► state/filter.rs
        ──► ipc/client.rs ──► ipc/protocol.rs
        ──► daemon_mgr.rs
        ──► event_handler/key_handler.rs ──► state/app_state.rs
        ──► event_handler/command.rs ──► ipc/client.rs
        ──► event_handler/mouse.rs  ──► ui/scrollbar.rs
        │                               ──► state/app_state.rs
        │                               ──► state/log_buffer.rs
        │                               ──► state/filter.rs
        ──► ui/layout.rs
        ──► ui/header.rs ──► state/app_state.rs
        ──► ui/table.rs ──► state/app_state.rs
        ──► ui/logs.rs ──► state/log_buffer.rs
        │              ──► state/filter.rs
        ──► ui/command_bar.rs ──► state/app_state.rs
        ──► ui/dialogs.rs ──► state/app_state.rs
        ──► ui/scrollbar.rs（独立组件，无外部依赖）
```

### 外部依赖

**Python**（requirements.txt）：

| 包 | 用途 |
|----|------|
| `openpyxl` | 读取 Excel 参数表 |
| `paramiko` | SSH/SFTP 远程操作 |
| `python-dotenv` | 加载 .env 环境变量 |
| `pywin32` | Windows COM 自动化（SolidWorks） |

**Rust**（Cargo.toml）：

| crate | 用途 |
|-------|------|
| `ratatui` | 终端 UI 渲染框架 |
| `crossterm` | 跨平台终端事件处理（含鼠标捕获） |
| `tokio` | 异步运行时（net + time + io-util + rt-multi-thread） |
| `serde` + `serde_json` | JSON 序列化/反序列化 |
| `unicode-width` | Unicode 字符宽度计算（中日韩文字对齐） |
| `arboard` | 剪贴板操作（双击日志行复制消息） |
| `windows-sys` | Windows API 绑定（`GetLocalTime` 实现本地时间格式化，替代 chrono） |

---

## 9. 项目运行方式

### 方式一：统一入口

```bash
# 启动后台引擎
python main.py --daemon

# 启动 TUI 客户端（Rust TUI）
python main.py --client

# 同时启动 daemon + client
python main.py --all
```

### 方式二：独立脚本

```bash
# 启动后台引擎
python start_daemon.py

# 启动 TUI 客户端（Rust TUI）
python start_client.py
```

### 方式三：Windows 批处理

```cmd
start.bat
```

### 方式四：Rust TUI（需编译）

```bash
cd autofluid-tui
cargo build --release
./target/release/autofluid-tui
```

### 运行顺序

1. **先启动 Daemon**：`python start_daemon.py`
2. **再启动 TUI 客户端**：`python start_client.py`
3. TUI 也可通过 `daemon start` 命令从界面内启动 Daemon

### Rust TUI 构建

```bash
cd autofluid-tui
cargo build --release
```

---

## 10. 测试体系

### test_pause_start.py — Pause/Start 功能验证

无需 SolidWorks/ANSYS 等外部依赖，使用 Mock 对象模拟核心组件行为。

**测试场景（11 个）**：

| # | 场景 | 验证要点 |
|---|------|----------|
| 1 | 正常启动 → 暂停 → 恢复 | 状态切换正确性 |
| 2 | SW 宏执行中暂停 → SW 成功后恢复 | 异步暂停时机 |
| 3 | SW 宏执行中暂停 → SW 失败后恢复 | 失败场景恢复 |
| 4 | SW 直接失败 → 引擎停止 → 重新启动 | 错误恢复 |
| 5 | 快速连续 pause/start | 状态切换稳定性 |
| 6 | Running 状态下重复 start | 幂等性 |
| 7 | Stopped 状态下 pause | 无操作 |
| 8 | 暂停后文件监控停止扫描 | 监控器联动 |
| 9 | 暂停期间 STEP 文件不被捕捉 | 回调阻断 |
| 10 | 恢复后立即触发完整轮询 | 监控器重置 |
| 11 | 多次 pause-start 状态切换稳定性 | 长期稳定性 |

**Mock 对象**：
- `MockTaskRunner`：模拟 TaskRunner 的各阶段执行
- `MockStepFileMonitor`：模拟文件监控器
- `TestContext`：测试上下文（临时数据库 + 调度器 + 断言辅助）

### test_sw_step_naming.py — STEP 文件命名测试

| 测试 | 验证内容 |
|------|----------|
| `test_get_step_filename` | `get_step_filename()` 生成正确文件名 |
| `test_step_file_monitor_parse_config` | 文件名解析构型号 |
| `test_guess_sw_doc_type` | SW 文档类型推断 |
| `test_env_override_local_paths` | 环境变量覆盖路径配置 |

---

## 11. 配置与环境变量

### 配置方式

项目支持两种配置方式：

**方式一：TUI 设置页面（推荐）** — 在 TUI 界面中输入 `settings` 命令或点击 `⚙ Settings` 按钮，以可视化对话框编辑 6 大分类 52 个配置字段，实时校验路径存在性，`Ctrl+S` 保存到 `autofluid_config.toml`，SSH 密码自动写入 `.env` 文件。

**方式二：直接编辑配置文件** — 手动编辑项目根目录的 `autofluid_config.toml`（TOML 格式）和 `.env` 文件（SSH 密码）。TUI 设置页面的修改结果也保存在这两个文件中。

### 环境变量覆盖

所有 `LOCAL_PATHS` 和 `REMOTE_CONFIG` 中的路径均可通过环境变量覆盖：

| 环境变量 | 覆盖目标 |
|----------|----------|
| `AUTOFLUID_SW_EXE` | SolidWorks 可执行文件路径 |
| `AUTOFLUID_SW_MODEL` | SW 模型文件路径 |
| `AUTOFLUID_SW_EXCEL` | Excel 参数表路径 |
| `AUTOFLUID_STEP_DIR` | STEP 文件输出目录 |
| `AUTOFLUID_SC_EXE` | SpaceClaim 可执行文件路径 |
| `AUTOFLUID_SC_SCRIPT` | SpaceClaim 脚本路径 |
| `AUTOFLUID_SC_BRIDGE` | SpaceClaim 桥接程序（SpaceClaimBridge.exe）路径 |
| `AUTOFLUID_SCDOC_DIR` | SCDOC 文件输出目录 |
| `AUTOFLUID_LOG_DIR` | 日志目录 |
| `AUTOFLUID_DATA_DIR` | 数据目录 |
| `AUTOFLUID_SSH_HOST` | SSH 主机地址 |
| `AUTOFLUID_SSH_PORT` | SSH 端口 |
| `AUTOFLUID_SSH_USER` | SSH 用户名 |
| `AUTOFLUID_SSH_PASSWORD` | SSH 密码 |

### .env 文件

项目支持 `.env` 文件（需 `python-dotenv`），将敏感配置（如 SSH 密码）从代码中分离。

### 引擎配置参数（ENGINE_CONFIG）

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `watchdog_interval` | 1.0s | 文件监控轮询间隔 |
| `sw_macro_timeout` | 3600s | SW 宏执行超时 |
| `sw_close_doc_on_finish` | True | 宏完成后关闭模型文档 |
| `sw_exit_on_finish` | True | 宏完成后退出 SolidWorks |
| `sw_visible` | True | 是否显示 SW 主窗口 |
| `sw_max_retries` | 2 | SW 宏最大重试次数 |
| `sc_timeout` | 300s | SpaceClaim 脚本超时 |
| `transfer_timeout` | 120s | 文件传输超时 |
| `meshing_timeout` | 600s | 网格划分超时 |
| `solver_timeout` | 7200s | 求解超时 |
| `max_retries` | 3 | 通用最大重试次数 |
| `state_refresh_interval` | 0.5s | 状态刷新间隔 |

### 操作超时参数（OPERATION_TIMEOUTS）

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `sw_startup` | 60s | SW 启动超时 |
| `sw_dispatch_startup_delay` | 8s | SW COM Dispatch 后等待窗口加载的延迟 |
| `sw_exit_wait_seconds` | 15s | SW ExitApp 后等待进程退出的最大秒数 |
| `sc_poll_interval` | 2.0s | SC 进程轮询间隔 |
| `ssh_connection` | 10s | SSH 连接超时 |
| `dir_recursion_limit` | 32 | 远程目录递归创建的深度限制 |
| `ssh_upload_max_retries` | 3 | 文件上传重试的最大次数 |

### IPC 配置（IPC_CONFIG）

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `host` | 127.0.0.1 | IPC 监听地址 |
| `port` | 9527 | IPC 监听端口 |
| `db_path` | data/pipeline_state.db | SQLite 数据库路径 |
| `timeout` | 5.0s | Socket 超时 |
