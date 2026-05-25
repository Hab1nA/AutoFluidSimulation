---
description: "实施规划索引文件。根据目标代码的语言，路由到对应的语言专用指令文件。包含项目架构概览、跨语言实施流程和质量保障规范。"
applyTo: ["**/*.py", "**/*.rs", "**/*.cs", "**/*.toml", "**/*.ini", "**/*.bat", "**/*.csproj"]
---

# AutoFluid 实施规范（路由索引）

> **本文件是入口索引。** 在修改任何代码之前，必须先根据目标文件的语言，阅读对应的语言专用指令文件。

---

## 🔀 语言路由表

**根据你要修改的文件，立即阅读对应的指令文件：**

| 目标文件 | 语言 | 必须阅读的指令文件 |
|----------|------|-------------------|
| `*.py` | Python | `.github/instructions/python.instructions.md` |
| `*.rs`（在 `autofluid-tui/` 下） | Rust | `.github/instructions/rust.instructions.md` |
| `*.cs` / `*.csproj`（在 `bridge/` 下） | C# | `.github/instructions/csharp.instructions.md` |
| `*.toml` / `*.ini` / `*.bat` | 配置/脚本 | 根据内容关联的语言，阅读对应指令文件 |

**操作规则：**
- 修改 Python 文件时，**只阅读** `python.instructions.md`，不要加载 Rust 或 C# 的指令
- 修改 Rust 文件时，**只阅读** `rust.instructions.md`，不要加载 Python 或 C# 的指令
- 修改 C# 文件时，**只阅读** `csharp.instructions.md`，不要加载 Python 或 Rust 的指令
- 如果一次任务涉及多种语言，**分别阅读**各自指令文件，但不要在修改 A 语言代码时引用 B 语言的规范

---

## 🚫 跨语言污染防护（最高优先级）

以下规则适用于所有代码修改：

1. **禁止跨语言代码插入**：不得将一种语言的代码、文档片段、API 示例插入到另一种语言的文件中
2. **禁止跨语言上下文引用**：修改 A 语言代码时，不得参考 B 语言的类型系统、命名规范或库文档
3. **忽略无关语言上下文**：如果当前会话的上下文中混入了其他语言的代码或文档，**完全忽略**它们
4. **单一语言聚焦**：每次代码修改操作应聚焦于单一语言，避免混合操作

---

## 项目架构概览

### 三层结构

```
Daemon (Python)  ←→  IPC (TCP :9527)  ←→  TUI (Rust)
     ↕                                        ↕
  执行器层                               Settings 页面
  (Python)                                可写 TOML
     ↕
  SpaceClaim Bridge (C#)
```

### 核心模块职责

| 模块 | 语言 | 职责 |
|------|------|------|
| `engine/` | Python | 调度引擎：PipelineDaemon、PipelineScheduler、TaskRunner、StateManager |
| `engine/scheduler/` | Python | 调度器子包：barrier、worker_pool、sw_phase、meshing_monitor、retry |
| `executor/` | Python | 任务执行器：SWExecutor、RemoteExecutor、FileCleaner、SCProcessPool |
| `ipc/` | Python | IPC 服务端：JSON over TCP 协议 |
| `bridge/` | C# | SpaceClaim 自动化桥接：进程检测模式 |
| `autofluid-tui/` | Rust | TUI 前端：tokio + ratatui 异步轮询 IPC |
| `utils/` | Python | 工具模块：SSH 客户端、日志、进程管理 |

### 关键交互逻辑

- `PipelineDaemon` 通过 `IPCServer` 注册命令处理器，协调 Scheduler / TaskRunner / StateManager
- `PipelineScheduler` 基于 Producer-Consumer 异步队列 + `BarrierCoordinator` 全局屏障同步
- `StateManager` 基于 SQLite WAL 持久化，支持 Daemon 写入 / TUI 并发读取
- `TaskRunner` 委托给专用执行器：`SWExecutor` → `SCProcessPool` → `RemoteExecutor`
- Rust TUI 通过 `tokio` + `ratatui` 异步轮询 IPC 状态

### 配置体系

```
autofluid_config.toml  ←→  Rust TUI Settings 页面读写
         ↓
Python reload_config_from_toml() 读取
         ↓
OPERATION_TIMEOUTS / ENGINE_CONFIG / LOCAL_PATHS / ...
```

- 常量优先级：环境变量 > TOML > Python 硬编码默认值
- 配置字段名在 Python 和 Rust 两侧必须**完全一致**（`snake_case`）

---

## 实施流程（五阶段）

### 阶段一：改动量评估与计划制定

在动手编码前，必须完成：

1. **语言路由**：确定本次改动涉及哪些语言，阅读对应的语言指令文件
2. **影响范围分析**：列出本次改动将涉及的所有模块、文件、函数/方法
3. **代码改动量评估**：估算每个文件的增删改行数及复杂度
4. **制定实施计划**：
   - 将改动拆分为独立的子任务
   - 明确每个子任务的**验收标准**
   - 确定子任务的执行顺序（考虑依赖关系）
5. **风险识别**：标记可能影响现有功能的高风险改动点

### 阶段二：逐项实施与单元验证

按照计划**逐个**实施子任务：

1. **一次只改一项**：完成一个子任务后再开始下一个
2. **每项完成后立即验证**：
   - 编写或更新单元测试覆盖改动代码
   - 运行相关测试确认通过
   - 运行对应语言的质量门禁（参见各语言指令文件）
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

运行所有涉及语言的质量门禁（详见各语言指令文件）：

| 语言 | 检查项 | 必须通过 |
|------|--------|----------|
| Python | ruff linting + mypy + pytest | ✅ |
| Rust | cargo check + cargo clippy + cargo test | ✅ |
| C# | compile.bat 编译成功 | ✅ |

跨语言检查：

| 检查项 | 标准 | 必须通过 |
|--------|------|----------|
| IPC 兼容性 | 协议不变或向后兼容 | ✅ |
| 状态机一致性 | SQLite 状态转换正确 | ✅ |
| 退出码一致性 | C# ExitCode 与 Python 端处理匹配 | ✅ |

### 阶段五：交付确认

- 确认所有验收标准已满足
- 更新 `docs/roadmap.md`（如涉及架构级变更或远期规划项）

---

## 禁止事项

- ❌ **禁止**在未阅读对应语言指令文件的情况下直接开始编码
- ❌ **禁止**一次性提交大量未经测试的改动
- ❌ **禁止**跳过测试直接交付
- ❌ **禁止**基于猜测修改项目架构或关键交互逻辑——必须先向用户确认
- ❌ **禁止**修改 IPC 协议而不同步更新 Rust 端 `ipc/` 模块
- ❌ **禁止**修改 `StateManager` 的数据库 schema 而不考虑向后兼容
- ❌ **禁止**修改 C# `ExitCode` 而不更新 Python 端的退出码处理
- ❌ **禁止**将一种语言的代码或文档混入另一种语言的文件中

---

## 项目技术栈速查

| 层级 | 语言 | 关键依赖 |
|------|------|----------|
| 配置体系 | TOML + Python | toml, python-dotenv, TypedDict |
| 调度引擎 | Python 3.10 | SQLite WAL, threading, queue |
| 任务执行 | Python 3.10 | pywin32 (COM), paramiko (SSH/SFTP) |
| IPC | Python → Rust | JSON over TCP (port 9527) |
| TUI 前端 | Rust | tokio, ratatui, crossterm, serde, toml |
| 代码桥接 | C# .NET 4.8 | SpaceClaim API |
| 测试 | Python | pytest |
| 代码质量 | Python + Rust | ruff, mypy, cargo clippy |
