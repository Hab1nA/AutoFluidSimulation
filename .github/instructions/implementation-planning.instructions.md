---
description: "Use when: implementing features, making code changes, refactoring, fixing bugs, adding new functionality, or any modification to the AutoFluid codebase. Enforces a structured implementation workflow with planning, incremental delivery, testing gates, and quality assurance."
applyTo: ["**/*.py", "**/*.rs", "**/*.toml", "**/*.ini", "**/*.bat"]
----------------------------------------------------------

# AutoFluid 项目实施与质量保障规范

> 适用于本项目的所有代码改动：新功能开发、Bug 修复、重构优化等。
---
## 实施前置条件

### 1. 架构理解（必须）

在进行任何代码改动之前，必须先阅读以下文档，全面理解项目：

- `docs/code-style-guide.md` — 编码规范（命名、风格、错误处理）
- `docs/roadmap.md` — 架构全景与远期规划

需要理解的关键内容：

- **项目整体架构**：Daemon ↔ IPC (TCP :9527) ↔ Rust TUI 的三层结构
- **核心模块职责**：
  - `engine/` — 调度引擎：`PipelineDaemon`（守护进程/IPC 命令处理）、`PipelineScheduler`（流水线调度）、`TaskRunner`（阶段执行协调）、`StateManager`（SQLite WAL 持久化状态）、`config.py`（全局配置）及 `config_fingerprint.py`（配置指纹/数据库分片）
  - `engine/scheduler/` — 调度器子包：`barrier`（全局屏障）、`worker_pool`（工作线程池）、`sw_phase`（SW 阶段处理）、`meshing_monitor`（Meshing 监控）、`retry`（重试管理）
  - `executor/` — 任务执行器：`SWExecutor`（SolidWorks COM 自动化）、`RemoteExecutor`（SSH/SFTP 远程任务）、`FileCleaner`（文件清理）、`SCProcessPool`（SpaceClaim 进程池）
  - `ipc/` — 进程间通信：JSON over TCP 协议，Python 侧 `IPCServer` ↔ Rust 侧 `IpcClient`
  - `bridge/` — SpaceClaim 自动化桥接：C# .NET 程序（`SpaceClaimBridge`），通过进程检测模式辅助 SC 转换
  - `autofluid-tui/` — Rust TUI 前端
  - `utils/` — 工具模块：SSH 客户端、日志、进程管理等
- **配置体系**：`autofluid_config.toml`（TOML 配置文件）→ `engine/config.py`（TypedDict 类型化配置 + `.env` 环境变量覆盖）→ `StateManager`（配置指纹自动分片数据库）
- **关键交互逻辑**：
  - `PipelineDaemon` 通过 `IPCServer` 注册命令处理器，协调 Scheduler / TaskRunner / StateManager 所有子系统
  - `PipelineScheduler` 基于 Producer-Consumer 异步队列 + `BarrierCoordinator` 全局屏障同步
  - `StateManager` 基于 SQLite WAL 模式的持久化状态，支持 Daemon 写入 / TUI 并发读取
  - `TaskRunner` 将各阶段委托给专用执行器：`SWExecutor`（SW）、`SCProcessPool`（SC）、`RemoteExecutor`（Transfer/Meshing/Solver）
  - Rust TUI 通过 `tokio` + `ratatui` 异步轮询 IPC 状态

> 如需补充项目结构、交互逻辑等信息，**随时向用户提出询问**，不要基于假设进行开发。

---

## 实施流程（五阶段）

### 阶段一：改动量评估与计划制定

在动手编码前，必须完成：

1. **影响范围分析**：列出本次改动将涉及的所有模块、文件、函数/方法
2. **代码改动量评估**：估算每个文件的增删改行数及复杂度
3. **制定实施计划**：
   - 将改动拆分为独立的子任务
   - 明确每个子任务的**验收标准**
   - 确定子任务的执行顺序（考虑依赖关系）
4. **风险识别**：标记可能影响现有功能的高风险改动点

### 阶段二：逐项实施与单元验证

在此阶段，你必须先阅读 `docs/code-style-guide.md`，理解代码编写规范，然后遵循规范，按照计划**逐个**实施子任务：

1. **一次只改一项**：完成一个子任务后再开始下一个
2. **每项完成后立即验证**：
   - 编写或更新单元测试覆盖改动代码
   - 运行相关测试确认通过
   - 检查 ruff linting 无新增告警
   - 检查 mypy 类型检查无新增错误
3. **原子提交**：每完成一个子任务，确认无问题后再进行下一项

### 阶段三：整体功能测试

全部子任务完成后：

1. **功能测试**：按照验收标准逐项验证
2. **兼容性验证**：
   - 新功能不影响现有流水线阶段（SW/SC/Transfer/Meshing/Solver）
   - IPC 协议兼容（不破坏现有命令格式）
   - Rust TUI 客户端与 Python Daemon 的通信正常
3. **边界测试**：异常输入、超时、并发、断点续传等场景

### 阶段四：代码质量检查

1. **编码规范**：
   - Python 代码符合 `ruff.toml` 规则（忽略 E402/E501）
   - Python 代码通过 `mypy.ini` 类型检查
   - Rust 代码通过 `cargo check` 和 `cargo clippy`
2. **无新 Bug**：运行全量测试套件确认无回归
3. **性能检查**：确认程序整体运行稳定，无明显性能下降

### 阶段五：交付确认

- 确认所有验收标准已满足
- 更新 `docs/roadmap.md`（如涉及架构级变更或远期规划项）

---

## 质量保障硬性要求

| 检查项         | 标准                | 必须通过 |
| -------------- | ------------------- | -------- |
| ruff linting   | 无新增 E/F/W 告警   | ✅       |
| mypy 类型检查  | 无新增类型错误      | ✅       |
| cargo check    | Rust 编译通过       | ✅       |
| 现有单元测试   | 全部通过，无回归    | ✅       |
| 新增代码覆盖率 | 核心逻辑被测试覆盖  | ✅       |
| IPC 兼容性     | 协议不变或向后兼容  | ✅       |
| 状态机一致性   | SQLite 状态转换正确 | ✅       |

---

## 禁止事项

- ❌ **禁止**在未阅读 `docs/code-style-guide.md` 和 `docs/roadmap.md` 的情况下直接开始编码
- ❌ **禁止**一次性提交大量未经测试的改动
- ❌ **禁止**跳过测试直接交付
- ❌ **禁止**基于猜测修改项目架构或关键交互逻辑——必须先向用户确认
- ❌ **禁止**修改 IPC 协议而不同步更新 Rust 端 `ipc/protocol.rs`
- ❌ **禁止**修改 `StateManager` 的数据库 schema 而不考虑向后兼容

---

## 项目技术栈速查

| 层级       | 语言           | 关键依赖                                          |
| ---------- | -------------- | ------------------------------------------------- |
| 配置体系   | TOML + Python  | toml, python-dotenv, TypedDict                    |
| 调度引擎   | Python 3.10    | SQLite WAL, threading, queue                      |
| 任务执行   | Python 3.10    | pywin32 (COM), paramiko (SSH/SFTP)                |
| IPC        | Python → Rust  | JSON over TCP (port 9527)                         |
| TUI 前端   | Rust           | tokio, ratatui, crossterm, serde, toml            |
| 代码桥接   | C# .NET 4.8    | SpaceClaim API                                    |
| 测试       | Python         | pytest                                            |
| 代码质量   | Python + Rust  | ruff, mypy, cargo clippy                          |
