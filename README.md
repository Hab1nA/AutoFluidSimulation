# AutoFluidSimulation — 液氧甲烷火箭发动机仿真自动化跑批系统

> 串联 SolidWorks → SpaceClaim → 远程 ANSYS Fluent 的端到端参数化 CFD 仿真流水线，带断点续传与 Rich TUI 动态监控界面。

---

## 目录

- [1. 项目概述](#1-项目概述)
- [2. 系统架构](#2-系统架构)
- [3. 项目文件结构](#3-项目文件结构)
- [4. 环境要求](#4-环境要求)
- [5. 安装与配置](#5-安装与配置)
- [6. `config.py` 配置说明](#6-configpy-配置说明)
- [7. CLI 使用方法](#7-cli-使用方法)
- [8. 断点续传机制](#8-断点续传机制)
- [9. 远程后台驻留方案](#9-远程后台驻留方案)
- [10. TUI 界面说明](#10-tui-界面说明)
- [11. 故障排查 FAQ](#11-故障排查-faq)
- [12. 许可证](#12-许可证)

---

## 1. 项目概述

**AutoFluidSimulation** 是一个 Python 总控脚本，用于自动化执行液氧甲烷火箭发动机的参数化 CFD 仿真流水线。典型应用场景是为深度学习模型批量生成高保真仿真数据集。

### 核心能力

| 特性 | 说明 |
|------|------|
| **参数化建模** | 通过修改外部 Excel 文件驱动 SolidWorks 构型自动更新 |
| **几何处理** | 无头调用 SpaceClaim 进行面标注与 `.scdoc` 导出 |
| **跨机器调度** | SSH + SFTP 上传至远程工作站，异步启动 Fluent 网格划分与求解 |
| **断点续传** | SQLite 持久化每个构型的状态，程序中断后可无缝恢复 |
| **TUI 监控** | 基于 `rich` 的动态终端界面，实时展示任务进度与状态 |

### 5 阶段流水线

```
┌──────────┐    ┌──────────────┐    ┌──────────┐    ┌───────────────┐    ┌──────────┐
│ 阶段 1   │    │   阶段 2      │    │  阶段 3   │    │    阶段 4      │    │  阶段 5   │
│ SW 建模  │───→│ SC 几何处理   │───→│ SFTP 传输 │───→│ Fluent 远程求解│───→│ 状态轮询  │
│ (本地)   │    │   (本地)      │    │  (网络)   │    │   (远程工作站)  │    │ (本地)    │
└──────────┘    └──────────────┘    └──────────┘    └───────────────┘    └──────────┘
  Excel驱动        SpaceClaim        paramiko          pyFluent            SSH检查
  COM接口          无头模式          SFTP上传          Meshing+Solver      done.txt
```

---

## 2. 系统架构

```
┌─ 本地 PC (Windows 10/11) ─────────────────────────────────────────────┐
│                                                                        │
│  pipeline_controller.py  (主入口，TUI 渲染)                            │
│       │                                                                │
│       ├── modules/solidworks_driver.py  (win32com → SW COM 接口)      │
│       ├── modules/spaceclaim_driver.py  (subprocess → SpaceClaim CLI) │
│       ├── modules/state_manager.py      (SQLite 状态管理)             │
│       └── modules/tui_display.py        (Rich Live 界面)              │
│                                                                        │
└────────────────────┬───────────────────────────────────────────────────┘
                     │  SSH / SFTP (paramiko)
                     ▼
┌─ 远程工作站 (Windows 22H2, 172.17.135.240:22) ────────────────────────┐
│                                                                        │
│  D:\xkz_1020\                                                          │
│       ├── scdoc\                   (接收 .scdoc 文件)                  │
│       ├── batch_meshing_gen4.py    (pyFluent 网格生成脚本)            │
│       ├── batch_solver_gen4.py     (pyFluent 求解脚本)                │
│       └── scdoc\{config_id}_done.txt  (完成标志文件)                  │
│                                                                        │
│  Conda 环境: pyfluent (ANSYS Fluent 2024 R1)                          │
│                                                                        │
└────────────────────────────────────────────────────────────────────────┘
```

---

## 3. 项目文件结构

```
AutoFluidSimulation/
├── pipeline_controller.py          # 主入口，解析 CLI 参数、启动总控循环
├── config.py                       # 所有硬编码路径、远程连接参数、参数组合定义
├── requirements.txt                # Python 依赖列表
├── README.md                       # 本文件
├── modules/
│   ├── __init__.py                 # 包初始化
│   ├── state_manager.py            # 阶段1：SQLite 日志管理与断点续传逻辑
│   ├── solidworks_driver.py        # 阶段2：Excel 驱动 + COM 接口 + VBA 宏调用
│   ├── spaceclaim_driver.py        # 阶段3：subprocess 无头调用 SpaceClaim
│   ├── remote_scheduler.py         # 阶段4：SFTP 上传 + SSH 远程后台执行 + 状态轮询
│   └── tui_display.py              # 阶段5：Rich Live 动态刷新终端面板
└── (生成目录，自动创建)
    ├── step/                       # 导出的 .step 文件
    ├── scdoc/                      # 导出的 .scdoc 文件
    └── logs/
        ├── pipeline_state.db       # SQLite 状态数据库
        └── pipeline.log            # 运行日志
```

---

## 4. 环境要求

### 4.1 本地 PC

| 组件 | 版本要求 | 说明 |
|------|---------|------|
| 操作系统 | Windows 10 / 11 (64-bit) | — |
| Python | 3.9 或更高 | 建议 3.11+ |
| SolidWorks | 2021 或更高 | 需安装并激活，支持 COM 自动化 |
| ANSYS SpaceClaim | 2023 R1 | 需安装 `SpaceClaim.exe`，用于几何处理 |
| Microsoft Excel | 2016 或更高 | 外部参数驱动文件 `model_gen4.xlsx` |

### 4.2 远程工作站

| 组件 | 版本要求 | 说明 |
|------|---------|------|
| 操作系统 | Windows 10 / 11 (64-bit) 或 Windows Server 22H2 | — |
| OpenSSH Server | 内置即可 | 需启用并配置防火墙规则 |
| ANSYS Fluent | **2024 R1** | 需安装并激活 |
| Conda (Miniconda/Anaconda) | 最新版 | 包含 `pyfluent` 环境 |
| Python (远程) | 3.9+ | `pyfluent` 环境内 |

### 4.3 Python 依赖

```
pywin32>=306          # SolidWorks COM 接口（Windows 专用）
openpyxl>=3.1.2       # Excel .xlsx 文件读写
paramiko>=3.4.0       # SSH + SFTP 远程连接
rich>=13.7.0          # 终端 TUI 动态界面
```

安装命令：
```bash
pip install -r requirements.txt
```

---

## 5. 安装与配置

### 5.1 克隆项目

```bash
git clone https://github.com/Hab1nA/AutoFluidSimulation.git
cd AutoFluidSimulation
pip install -r requirements.txt
```

### 5.2 远程工作站配置

#### 步骤 1：启用 OpenSSH Server

在远程 Windows 工作站上以**管理员身份**运行 PowerShell：

```powershell
# 安装 OpenSSH Server
Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0

# 启动并设为开机自启
Start-Service sshd
Set-Service -Name sshd -StartupType 'Automatic'

# 配置防火墙规则
New-NetFirewallRule -Name sshd -DisplayName 'OpenSSH Server (sshd)' -Enabled True -Direction Inbound -Protocol TCP -Action Allow -LocalPort 22
```

> **注意**：确保远程工作站可以通过 IP `172.17.135.240` 从本地 PC 访问。

#### 步骤 2：创建 Conda 环境并安装 pyFluent

```bash
# 创建 pyfluent 环境（Python 3.9+）
conda create -n pyfluent python=3.10 -y
conda activate pyfluent

# 安装 pyFluent（ANSYS Fluent 2024 R1 对应版本）
pip install ansys-fluent-core
```

#### 步骤 3：部署网格与求解脚本

将以下脚本放置到 `D:\xkz_1020\` 目录下：

| 文件 | 说明 |
|------|------|
| `batch_meshing_gen4.py` | pyFluent 网格生成脚本，读取 `.scdoc` 并生成 `.msh` |
| `batch_solver_gen4.py` | pyFluent 求解脚本，读取 `.msh` 并执行 CFD 计算 |

**脚本接口约定**：两个脚本均接收 `--config` 命令行参数传入 `config_id`，并在完成后于 `D:\xkz_1020\scdoc\{config_id}_done.txt` 写入完成标志。

---

## 6. `config.py` 配置说明

项目所有配置集中在 `config.py` 中，分为三个字典/列表：

### 6.1 `LOCAL_CONFIG` — 本地路径

| 键 | 默认值 | 说明 |
|----|-------|------|
| `sw_model_path` | `...\model_gen4.SLDPRT` | SolidWorks 初始模型文件路径 |
| `excel_path` | `...\model_gen4.xlsx` | 外部 Excel 参数驱动文件路径 |
| `sw_macro_path` | `...\Macro1.swp` | SW VBA 宏文件（导出 STEP） |
| `step_dir` | `...\step\` | STEP 文件保存目录 |
| `spaceclaim_exe` | `C:\Program Files\ANSYS Inc\v231\SCDM\SpaceClaim.exe` | SpaceClaim 可执行程序 |
| `sc_script` | `...\spaceclaim_transit.scscript` | SpaceClaim 批处理脚本 |
| `scdoc_dir` | `...\scdoc\` | SCDOC 文件保存目录 |
| `log_dir` | `...\logs\` | 日志与数据库存放目录 |

### 6.2 `REMOTE_CONFIG` — 远程工作站

| 键 | 默认值 | 说明 |
|----|-------|------|
| `host` | `172.17.135.240` | 远程工作站 IP 地址 |
| `port` | `22` | SSH 端口 |
| `username` | `ps` | SSH 用户名 |
| `password` | `abc@123` | SSH 密码 |
| `conda_env` | `pyfluent` | Conda 环境名称 |
| `meshing_script` | `D:\xkz_1020\batch_meshing_gen4.py` | 远程网格脚本 |
| `solver_script` | `D:\xkz_1020\batch_solver_gen4.py` | 远程求解脚本 |
| `ssh_timeout` | `15` | SSH 连接超时（秒） |
| `poll_interval` | `15` | 远程任务轮询间隔（秒） |

### 6.3 `CONFIG_COMBINATIONS` — 参数组合

定义一个 `ConfigCombination` 列表，每个元素包含：
- `id`：构型唯一标识（如 `"R2.5_L30_A15"`）
- `params`：字典，键为 Excel 单元格地址（如 `"B2"`），值为参数数值

**示例**：修改 `config.py` 中的 `CONFIG_COMBINATIONS` 列表来添加仿真构型：

```python
CONFIG_COMBINATIONS: List[ConfigCombination] = [
    ConfigCombination(id="R2.5_L30_A15", params={"B2": 2.5, "B3": 30, "B4": 15}),
    ConfigCombination(id="R3.0_L35_A20", params={"B2": 3.0, "B3": 35, "B4": 20}),
    # 添加更多构型...
    ConfigCombination(id="R4.0_L50_A30", params={"B2": 4.0, "B3": 50, "B4": 30}),
]
```

### 6.4 `GLOBAL_CONFIG` — 全局行为

| 键 | 默认值 | 说明 |
|----|-------|------|
| `max_retry` | `3` | 每个阶段单任务的最大重试次数 |
| `sc_timeout` | `300` | SpaceClaim 进程超时（秒） |
| `sw_retry_interval` | `5` | SW COM 操作重试间隔（秒） |
| `tui_refresh_rate` | `4` | TUI 界面刷新频率（Hz） |

---

## 7. CLI 使用方法

### 基本命令

```bash
# 标准 TUI 模式（推荐）
python pipeline_controller.py

# 无 TUI 模式（纯日志输出，适合后台运行）
python pipeline_controller.py --no-tui

# 指定最大重试次数
python pipeline_controller.py --retry 5

# 组合参数
python pipeline_controller.py --no-tui --retry 5
```

### 全部 CLI 参数

| 参数 | 类型 | 默认值 | 说明 |
|------|------|-------|------|
| `--no-tui` | flag | `False` | 禁用 TUI 界面，仅输出纯文本日志 |
| `--retry N` | int | `3` | 最大重试次数 |
| `--resume-only` | flag | `False` | 仅断点续传，不启动新任务 |
| `--poll-only` | flag | `False` | 仅轮询远程任务状态，跳过本地阶段 |

### 典型使用场景

#### 场景 1：首次完整跑批

```bash
python pipeline_controller.py
```

程序会依次执行所有构型的 SW → SC → 传输 → 远程启动，并在 TUI 界面实时展示进度。

#### 场景 2：中断后断点续传

```bash
# 上次运行被 Ctrl+C 中断后，重新启动即可自动恢复
python pipeline_controller.py
```

程序启动时会自动检查已完成的任务状态：
- `fluent_status = Completed` → 跳过
- `fluent_status = Computing` → 通过 SSH 检查远程 `done.txt` 是否存在
  - 存在 → 标记为 `Completed`
  - 不存在 → 保持 `Computing`，继续轮询

#### 场景 3：后台无人值守运行

```bash
# Windows 终端
python pipeline_controller.py --no-tui > nul 2>&1

# 或使用 PowerShell
Start-Process python -ArgumentList "pipeline_controller.py --no-tui" -WindowStyle Hidden
```

#### 场景 4：仅轮询远程任务（本地 SW/SC 已完成）

```bash
python pipeline_controller.py --poll-only
```

---

## 8. 断点续传机制

### 8.1 状态数据表

程序使用 SQLite 数据库（`logs/pipeline_state.db`）持久化每个构型的状态，表结构如下：

| 字段 | 类型 | 说明 | 可能的值 |
|------|------|------|---------|
| `config_id` | TEXT (PK) | 构型唯一标识 | e.g. `R2.5_L30_A15` |
| `sw_status` | TEXT | SolidWorks 阶段状态 | `Pending`, `InProgress`, `Done`, `Error` |
| `sc_status` | TEXT | SpaceClaim 阶段状态 | 同上 |
| `transfer_status` | TEXT | SFTP 传输状态 | 同上 |
| `fluent_status` | TEXT | Fluent 求解状态 | `Pending`, `Transferred`, `Computing`, `Completed`, `Error` |
| `retry_count` | INTEGER | 当前失败重试次数 | 0 ~ max_retry |
| `error_msg` | TEXT | 最近一次错误信息 | — |
| `updated_at` | TEXT | 最后更新时间戳 | ISO 8601 格式 |

### 8.2 状态流转图

```
Pending ──→ InProgress ──→ Done ──→ (下一阶段)
                  │
                  └──→ Error ──→ retry_count < max_retry?
                                    │
                          Yes ──→ Pending (自动重试)
                          No  ──→ 跳过该构型
```

### 8.3 恢复逻辑

程序启动时自动执行以下检查：

1. 读取数据库中所有构型状态
2. 对于 `fluent_status = Computing` 的任务：
   - SSH 连接远程工作站
   - 检查 `D:\xkz_1020\scdoc\{config_id}_done.txt` 是否存在
   - 存在 → 更新状态为 `Completed`
   - 不存在 → 保持 `Computing`，等待后续轮询
3. 对于 `*_status = Error` 且 `retry_count < max_retry` 的任务：自动重新执行该阶段

---

## 9. 远程后台驻留方案

为确保本地 SSH 连接断开后远程 Fluent 进程继续独立运行，本系统采用**双重保障策略**：

### 方案 A：PowerShell `Start-Process`（优先）

通过 SSH 在远程工作站执行 PowerShell 命令：

```powershell
Start-Process cmd.exe -ArgumentList '/c D:\xkz_1020\run_{config_id}.bat' -WindowStyle Hidden -NoNewWindow
```

远程 `.bat` 文件内容：

```bat
@echo off
call conda activate pyfluent
python D:\xkz_1020\batch_meshing_gen4.py --config {config_id}
if %errorlevel% neq 0 exit /b %errorlevel%
python D:\xkz_1020\batch_solver_gen4.py --config {config_id}
if %errorlevel% neq 0 exit /b %errorlevel%
echo %date% %time% > D:\xkz_1020\scdoc\{config_id}_done.txt
```

### 方案 B：`schtasks` 计划任务（回退）

若方案 A 失败，自动回退到 Windows 计划任务：

```powershell
schtasks /Create /SC ONCE /TN "FluentTask_{config_id}" /TR "cmd /c D:\xkz_1020\run_{config_id}.bat" /F
schtasks /Run /TN "FluentTask_{config_id}"
```

### 完成检测机制

- 本地程序周期性地通过 SSH 检查 `D:\xkz_1020\scdoc\{config_id}_done.txt` 是否存在
- 存在 → 标记 `Completed`，清理临时 `.bat` 文件和计划任务
- 不存在 + 进程已退出 → 标记 `Error`

---

## 10. TUI 界面说明

基于 `rich` 库构建的动态终端界面，刷新率默认为 4 Hz。

### 界面布局

```
┌──────────────────────────────────────────────────────────────┐
│  🔥 液氧甲烷火箭发动机仿真 — 自动化跑批流水线                  │
│  启动时间: 2026-05-05 16:30:00                                │
├──────────────────────────────────────────────────────────────┤
│  ┌─────────────────────────────────────────────────────────┐  │
│  │  任务总览表格                                            │  │
│  │  ID            SW建模   SC处理   文件传输   Fluent求解   │  │
│  │  R2.5_L30_A15  Done     Done      Done      Computing  │  │
│  │  R3.0_L35_A20  Done     InProg... Pending    Pending    │  │
│  │  R3.5_L40_A25  Pending  Pending   Pending    Pending    │  │
│  └─────────────────────────────────────────────────────────┘  │
│                                                               │
│  ═════════════════════════════════════════════ 45% ████▌     │
│                                                               │
│  ┌─────────────────────────────────────────────────────────┐  │
│  │  [16:30:05] [R2.5_L30_A15] Fluent求解 → Computing       │  │
│  │  [16:30:02] [R3.0_L35_A20] SC处理 → InProgress          │  │
│  │  [16:29:58] [R2.5_L30_A15] 文件传输 → Done              │  │
│  └─────────────────────────────────────────────────────────┘  │
│                                                               │
│  已完成: 1/3 │ 计算中: 1 │ 错误: 0 │ 运行时间: 00:05:32      │
└──────────────────────────────────────────────────────────────┘
```

### 颜色编码

| 颜色 | 状态 | 含义 |
|------|------|------|
| 🟢 绿色 | `Done` / `Completed` | 阶段成功完成 |
| 🟡 黄色 | `InProgress` / `Computing` | 正在执行中 |
| 🔴 红色 | `Error` | 执行失败 |
| ⚪ 灰色 | `Pending` | 尚未开始 |
| 🔵 蓝色 | `Transferred` | 文件已上传，等待求解启动 |

---

## 11. 故障排查 FAQ

### Q1：SolidWorks COM 连接失败

**错误信息**：`无法连接到 SolidWorks`

**解决方案**：
1. 确认 SolidWorks 已安装且已激活
2. 以管理员身份运行终端
3. 检查 SW 是否被其他进程占用（关闭所有 SW 窗口后重试）
4. 若持续失败，尝试手动启动一次 SolidWorks 后再运行脚本

### Q2：SpaceClaim 进程超时

**错误信息**：`SpaceClaim 进程超时（300 秒），已强制终止`

**解决方案**：
1. 检查 `.step` 文件是否过大或损坏
2. 增加 `GLOBAL_CONFIG["sc_timeout"]` 的值（单位：秒）
3. 手动在 SpaceClaim GUI 中打开 `.step` 文件，确认脚本 `spaceclaim_transit.scscript` 能否正常运行

### Q3：SSH 连接中断

**错误信息**：`SSH 连接失败: 172.17.135.240:22`

**解决方案**：
1. 确认远程工作站已开机且网络可达：`ping 172.17.135.240`
2. 确认 OpenSSH Server 正在运行：远程执行 `Get-Service sshd`
3. 检查防火墙是否允许端口 22 入站
4. 验证用户名/密码是否正确

### Q4：远程计算进程意外终止

**现象**：轮询时发现 `done.txt` 不存在，且 Fluent 进程已退出

**自动处理**：
1. 系统会在下次轮询时检测到进程不在运行
2. 若 `retry_count < max_retry`，自动重新启动远程任务
3. 若已达最大重试次数，标记为 `Error` 并跳过

**手动检查**：
```powershell
# SSH 登录远程工作站
ssh ps@172.17.135.240

# 检查 Fluent 进程
Get-Process | Where-Object {$_.ProcessName -like "*fluent*"}

# 查看计划任务状态
schtasks /Query /TN "FluentTask_{config_id}"
```

### Q5：程序被 Ctrl+C 中断

**行为**：
- 捕获 `SIGINT` 信号，优雅关闭
- 当前任务状态写入 SQLite 数据库
- 下次启动时自动从断点恢复

**注意**：正在执行的远程 Fluent 任务**不会**被中断（已通过 `Start-Process` 在远程独立运行），程序重启后会自动检测并更新其状态。

### Q6：TUI 显示异常

**症状**：终端界面乱码、刷新卡顿

**解决方案**：
1. 确认终端支持 UTF-8 和 ANSI 转义序列（推荐 Windows Terminal 或 VS Code 内置终端）
2. 使用 `--no-tui` 参数切换到纯日志模式
3. 降低刷新率：修改 `GLOBAL_CONFIG["tui_refresh_rate"]` 为 `2`

---

## 12. 许可证

本项目仅供学术研究与个人学习使用。

---

*最后更新：2026-05-05*