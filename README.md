# AutoFluidSimulation

AutoFluidSimulation 是一个 **CFD 仿真流水线自动化工具**，用于批量对 SolidWorks 几何模型的不同构型执行"几何导出 → SpaceClaim 转换 → 远程传输 → 网格划分 → 仿真求解"全流程任务。

## 目录

- [工作流概览](#工作流概览)
- [项目结构](#项目结构)
- [环境依赖](#环境依赖)
- [快速开始](#快速开始)
  - [1. 安装依赖](#1-安装依赖)
  - [2. 修改配置](#2-修改配置)
  - [3. 设置环境变量](#3-设置环境变量)
  - [4. 准备 Excel 参数表](#4-准备-excel-参数表)
  - [5. 启动后台守护进程](#5-启动后台守护进程)
  - [6. 启动 TUI 客户端](#6-启动-tui-客户端)
- [TUI 客户端命令](#tui-客户端命令)
- [流水线阶段说明](#流水线阶段说明)
- [断点续传与容错](#断点续传与容错)
- [并发控制](#并发控制)
- [日志](#日志)
- [常见问题](#常见问题)
- [许可证](#许可证)

---

## 工作流概览

```
  ┌─────────────┐    ┌──────────────┐    ┌────────────┐    ┌───────────────┐    ┌──────────────┐
  │ SolidWorks  │───▶│  SpaceClaim  │───▶│   Transfer  │───▶│   Meshing     │───▶│    Solver    │
  │  宏导出STEP  │    │  STEP→SCDOC  │    │ SFTP 上传   │    │ 远程网格划分   │    │  远程仿真求解  │
  └─────────────┘    └──────────────┘    └────────────┘    └───────────────┘    └──────────────┘
      (本地)              (本地)            (本地→远程)         (远程工作站)          (远程工作站)
```

1. **SolidWorks**：运行 VBA 宏，根据 Excel 中的参数表批量导出各构型的 STEP 文件。
2. **SpaceClaim**：在无头模式下运行 SpaceClaim 脚本，将 STEP 转换为 `.scdoc` 格式。
3. **Transfer**：通过 SFTP 将 SCDOC 文件上传至远程仿真工作站。
4. **Meshing**：SSH 连接远程工作站，触发 Fluent Meshing 脚本。
5. **Solver**：等待所有构型网格划分完成后，统一触发 Fluent 求解脚本（避免网格/求解争抢资源）。

---

## 项目结构

```
AutoFluidSimulation/
├── README.md                  # 本文件
├── requirements.txt           # Python 依赖清单
├── run_daemon.py              # 后台守护进程入口
├── run_client.py              # TUI 客户端入口
├── autofluid/
│   ├── __init__.py            # 包初始化
│   ├── config.py              # 硬编码配置（本地路径、远程连接等，需修改）
│   ├── constants.py           # 流水线阶段枚举与常量
│   ├── models.py              # 数据模型（构型参数行）
│   ├── engine.py              # 后台调度引擎（核心逻辑）
│   ├── daemon.py              # 守护进程组装
│   ├── client.py              # Rich TUI 终端界面
│   ├── ipc.py                 # 本地 Socket IPC 通信
│   ├── state_store.py         # SQLite 状态持久化
│   ├── logger.py              # 日志系统
│   └── utils.py               # 工具函数（Excel 读取、文件查找、子进程）
```

---

## 环境依赖

### 本地 Windows 机器

| 软件 | 用途 |
|------|------|
| Python 3.8+ | 运行流水线脚本 |
| SolidWorks | 参数化几何建模与 STEP 导出 |
| ANSYS SpaceClaim 2023 R1 | STEP → SCDOC 转换 |
| Excel 参数表 | 定义各构型的几何参数 |

### 远程仿真工作站

| 软件 | 用途 |
|------|------|
| ANSYS Fluent (含 Meshing) | 网格划分与 CFD 求解 |
| Python + `pyfluent` | 调用 Fluent API |
| OpenSSH Server | 接收 SSH 连接 |
| Conda (建议) | 管理远程 Python 环境 |

### Python 包

见 [`requirements.txt`](requirements.txt)：

```
openpyxl==3.1.5       # 读取 Excel 参数表
paramiko==3.4.0       # SSH / SFTP 远程连接
rich==13.9.4          # TUI 终端界面
pywin32==306          # Windows COM 自动化（驱动 SolidWorks）
```

---

## 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

### 2. 修改配置

编辑 **[`autofluid/config.py`](autofluid/config.py)**，将以下内容替换为自己的实际路径和信息：

#### 本地路径 (`LocalPaths`)

| 配置项 | 说明 |
|--------|------|
| `solidworks_model` | SolidWorks 模型文件路径（`.SLDPRT`） |
| `excel_path` | Excel 参数表路径（`.xlsx`） |
| `macro_path` | SolidWorks VBA 宏文件路径（`.swp`） |
| `step_dir` | STEP 文件输出目录 |
| `spaceclaim_exe` | SpaceClaim 可执行文件路径 |
| `sc_script` | SpaceClaim 转换脚本路径（`.scscript`） |
| `scdoc_dir` | SCDOC 文件输出目录 |
| `log_dir` | 日志和数据库存放目录 |

#### 远程配置 (`RemoteConfig`)

| 配置项 | 说明 |
|--------|------|
| `host` | 远程工作站 IP 地址 |
| `port` | SSH 端口（默认 22） |
| `username` | SSH 用户名 |
| `password` | 不建议硬编码，优先使用环境变量（见下节） |
| `project_root` | 远程项目根目录 |
| `remote_scdoc_dir` | 远程 SCDOC 存放目录 |
| `conda_env` | 远程 Conda 环境名称 |
| `meshing_script` | 远程网格划分脚本路径 |
| `solver_script` | 远程求解脚本路径 |
| `meshing_clean_cmd` | （可选）网格产物清理命令 |
| `solver_clean_cmd` | （可选）求解产物清理命令 |

#### 流水线参数 (`PipelineConfig`)

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `ipc_host` | `127.0.0.1` | IPC 监听地址 |
| `ipc_port` | `9234` | IPC 监听端口 |
| `poll_interval` | `2.0` | STEP 文件轮询间隔（秒） |
| `sc_workers` | `2` | SpaceClaim 并行数 |
| `transfer_workers` | `2` | 传输并行数 |
| `meshing_workers` | `2` | 网格划分并行数 |
| `solver_workers` | `2` | 求解并行数 |
| `max_retries` | `2` | 失败最大重试次数 |

### 3. 设置环境变量

远程工作站 SSH 密码通过环境变量提供，避免明文写入源码：

**Windows CMD：**
```cmd
set AUTOFLUID_SSH_PASSWORD=你的密码
```

**Windows PowerShell：**
```powershell
$env:AUTOFLUID_SSH_PASSWORD="你的密码"
```

> **注意**：启动守护进程前必须设置此环境变量，否则无法连接远程工作站。

### 4. 准备 Excel 参数表

Excel 文件应包含以下列（表头名称需严格匹配）：

| name | param1 | param2 | param3 | param4 |
|------|--------|--------|--------|--------|
| config_001 | 10.0 | 20.0 | 5.0 | 3.0 |
| config_002 | 12.0 | 22.0 | 6.0 | 3.5 |
| ... | ... | ... | ... | ... |

- `name`：构型名称，仅允许字母、数字、下划线和连字符（`A-Za-z0-9_-`）
- `param1` ~ `param4`：几何参数，由 SolidWorks 宏读取并驱动模型重建

### 5. 启动后台守护进程

打开一个终端，运行：

```bash
python run_daemon.py
```

守护进程启动后会：
- 读取 Excel 参数表，初始化状态数据库
- 在 `127.0.0.1:9234` 上监听 IPC 连接
- 启动各阶段的工作线程
- 自动执行自检，记录缺失路径和 SSH 连通性问题

### 6. 启动 TUI 客户端

打开另一个终端，运行：

```bash
python run_client.py
```

客户端启动后，将显示一张实时刷新的流水线状态表：

```
┌──────────────┬─────┬─────┬────────┬──────────┬──────────┐
│ 构型          │ SW  │ SC  │ 传输    │ 网格划分  │ 仿真运行   │
├──────────────┼─────┼─────┼────────┼──────────┼──────────┤
│ config_001   │ ✓   │ ✓   │ ✓      │ Running  │ Waiting   │
│ config_002   │ ✓   │ ✓   │ Waiting │ Waiting  │ Waiting   │
└──────────────┴─────┴─────┴────────┴──────────┴──────────┘
```

在 `命令` 提示符后输入 `start` 即可启动流水线。

---

## TUI 客户端命令

| 命令 | 参数 | 说明 |
|------|------|------|
| `start` | — | 启动 / 继续执行流水线 |
| `pause` | — | 暂停流水线（当前任务完成后不再取新任务） |
| `check` | — | 执行系统自检（路径存在性 + SSH 连通性） |
| `reset <name> <step>` | 构型名称 + 步骤别名 | 重置指定构型的指定步骤及下游 |
| `reset all` | — | 重置全部构型（需确认） |
| `clean <step>` | 步骤别名 | 清理指定步骤的全部产物 |
| `clean all` | — | 清理全部步骤产物（需确认） |
| `quit` | — | 退出 TUI 客户端，后台流水线继续运行 |
| `full_quit` | — | 退出 TUI 客户端并停止后台引擎（需确认） |

**步骤别名参考**：`sw` / `solidworks`、`sc` / `spaceclaim`、`transfer`、`mesh` / `meshing`、`solver`

### 命令示例

```text
命令 start              # 启动流水线
命令 pause              # 暂停流水线
命令 check              # 自检
命令 reset config_001 mesh    # 重置 config_001 的网格划分及下游
命令 reset all          # 重置全部构型
命令 clean sc           # 清理所有 SCDOC 文件
命令 clean all          # 清理全部步骤产物
命令 quit               # 退出客户端
```

---

## 流水线阶段说明

### 1. SolidWorks (SW)
- 通过 Windows COM 接口驱动 SolidWorks 应用程序
- 运行 VBA 宏，根据 Excel 参数表批量重建模型并导出 STEP 文件
- 状态监测：后台轮询 `step_dir` 目录，发现新 STEP 文件后标记完成

### 2. SpaceClaim (SC)
- 以无头模式（`/Headless`）启动 SpaceClaim
- 执行 `.scscript` 脚本，将 STEP 几何转换为 Fluent 可用的 SCDOC 格式
- 支持多 Worker 并行转换

### 3. Transfer
- 通过 SFTP 将本地 SCDOC 文件上传至远程工作站的指定目录
- 使用 SSH 主机密钥验证，需提前在本地 `known_hosts` 中登记远程主机密钥

### 4. Meshing（网格划分）
- SSH 连接远程工作站
- 在 Conda 环境中通过 `python` 执行网格划分脚本
- 使用 `Start-Process` 后台启动，确保 SSH 断开后进程不终止

### 5. Solver（仿真求解）
- **栅栏同步**：等待所有构型的网格划分全部完成后，统一启动求解
- 同样通过 SSH 远程后台执行，避免与网格划分争抢计算资源

---

## 断点续传与容错

- **状态持久化**：所有任务状态存储在 SQLite 数据库（路径由 `config.py` 中 `db_path` 指定）中
- **断点续传**：守护进程重启后自动检查历史状态，继续执行未完成的任务
- **失败重试**：每个步骤失败后自动重试（最多 `max_retries` 次），状态标记为 `Retrying`
- **暂停/恢复**：`pause` 后当前执行中的任务会继续完成，但不再取新任务；`start` 即可恢复
- **手动重置**：可通过 `reset` 命令将出错步骤及其下游重置为 `Waiting`，重新执行

### 状态枚举

| 状态 | 含义 |
|------|------|
| `Waiting` | 等待执行 |
| `Running` | 正在执行 |
| `Retrying` | 执行失败，正在重试 |
| `Completed` | 执行成功 |
| `Error` | 执行失败且重试次数耗尽 |

---

## 并发控制

各阶段独立线程池，并行度可在 `PipelineConfig` 中调整：

- **sc_workers**：同时转换的 SpaceClaim 任务数
- **transfer_workers**：同时上传的文件数
- **meshing_workers**：同时进行的远程网格划分数
- **solver_workers**：同时进行的远程求解数

> **建议**：根据本地 CPU / 远程集群资源合理设置并行数，避免资源争抢。网格划分与求解通过栅栏机制隔离。

---

## 日志

- 守护进程日志输出到 `log_dir/pipeline.log`
- 日志文件自动轮转（单文件最大 5MB，保留 3 个备份）
- 同时输出到控制台
- 日志级别：`INFO`（可在 `logger.py` 中调整）

---

## 常见问题

### Q: 启动守护进程后提示 "未配置 SSH 密码"
**A**: 请确保已设置环境变量 `AUTOFLUID_SSH_PASSWORD`，然后重新启动守护进程。

### Q: SSH 连接失败
**A**:
1. 检查远程工作站 IP、端口、用户名是否正确
2. 确认远程工作站 SSH 服务已启动
3. 确保本地 `known_hosts` 已登记远程主机密钥（可先手动 `ssh user@host` 一次）

### Q: SolidWorks 宏执行失败
**A**:
1. 确认 SolidWorks 已安装且可正常启动
2. 确保宏文件路径正确
3. 检查 Excel 参数表格式是否符合要求
4. 确认 `pywin32` 已正确安装（仅 Windows 支持）

### Q: SpaceClaim 转换失败
**A**:
1. 确认 SpaceClaim 安装路径与配置一致
2. 确保 `sc_script` 文件存在
3. 在命令提示符下手动运行 SpaceClaim 命令行验证脚本是否正常

### Q: 想在同一台电脑上同时运行客户端和守护进程
**A**: 这正是本项目的设计用法。先在一个终端启动 `run_daemon.py`，再在另一个终端启动 `run_client.py`。两者通过 `127.0.0.1:9234` 本地通信。

---

## 许可证

本项目仅供学术研究使用。