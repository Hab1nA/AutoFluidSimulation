---
description: "Use when: 审查并更新 README.md 技术细节，同步版本号到全项目。触发词：更新文档、更新版本、同步 README、版本升级、文档审查。"
argument-hint: "可选：指定版本号（如 2.7.0）或版本类型（major/minor/patch），否则自动推断"
agent: "agent"
---

# README 技术审查 & 版本号同步

你是一个严谨的项目文档维护代理。本任务分两大阶段执行：**技术审查更新 README** 和 **版本号同步**。

---

## 阶段一：README 技术审查与更新

### 1. 读取现有 README

使用 `read_file` 读取 `README.md` 全文，逐节理解当前文档结构和内容。

### 2. 深度审查代码库技术栈

对以下区域做全面审查，记录所有与 README 不一致之处：

| 审查区域 | 检查内容 | 关键文件 |
|----------|----------|----------|
| **Python 依赖** | `requirements.txt`、`requirements-dev.txt` 中的实际包及版本 | `requirements.txt`, `requirements-dev.txt` |
| **Rust 依赖** | `Cargo.toml` 中的 crate 及版本 | `autofluid-tui/Cargo.toml` |
| **C# 目标框架** | `.csproj` 或编译脚本中的 .NET 版本 | `bridge/SpaceClaimBridge/*.csproj`, `compile.bat` |
| **项目结构** | 实际目录/文件是否与 README 描述一致 | 全项目目录树 |
| **架构描述** | 组件职责、数据流、端口号是否准确 | `engine/`, `ipc/`, `executor/` 源码 |
| **配置体系** | TOML 字段数量、分类是否与代码一致 | `autofluid_config.toml`, `engine/config.py` |
| **TUI 特性** | ratatui 功能、设置页面分类数/字段数 | `autofluid-tui/src/` |
| **IPC 协议** | 命令集、端口、消息格式 | `ipc/protocol.py`, `ipc/server.py` |
| **流水线阶段** | 各阶段执行器的实际行为和依赖 | `executor/*.py`, `engine/task_runner.py` |
| **测试覆盖** | 测试文件数量和覆盖范围 | `tests/` 目录 |
| **构建脚本** | `rebuild.bat`、`compile.bat` 等的最新内容 | 根目录和 bridge 下的 `.bat` 文件 |

### 3. 审查原则

- **只写事实**：每个技术细节必须有代码证据支撑，不凭记忆臆断
- **逐节对比**：对 README 每个章节逐一与代码对比，标注差异
- **保留结构**：保留 README 现有的 Markdown 结构和排版风格
- **中文优先**：技术描述保持中文，专有名词保留英文

### 4. 更新 README

对发现的每处不一致，使用 `replace_string_in_file` 精确修改。常见需更新场景：

- 依赖版本号变更（如 ratatui 升级、新增 crate）
- 新增/删除的模块或文件
- 架构组件职责变化
- 端口号、配置字段数变化
- 流水线阶段行为变更
- 目录树与实际不符

---

## 阶段二：版本号确定与同步

### 1. 确定新版本号

**规则**：
- 当前版本号格式为 `MAJOR.MINOR.PATCH`（语义化版本）
- 如果用户在参数中指定了版本号，直接使用
- 如果用户指定了版本类型（major/minor/patch），基于当前版本递增
- 如果未指定任何参数，根据本次变更的性质自动判断：
  - 仅文档更新 → `PATCH` 递增
  - 新增功能模块 → `MINOR` 递增
  - 架构重大变更 → `MAJOR` 递增

**当前版本获取方式**：读取 `autofluid-tui/Cargo.toml` 中的 `version` 字段。

### 2. 全项目版本号同步

以下位置包含项目版本号，必须全部同步更新：

| 文件 | 位置 | 格式 |
|------|------|------|
| `README.md` | 标题行 `Pipeline Daemon Engine vX.Y.Z` | `vX.Y.Z` |
| `README.md` | 底部版本标记 `版本: vX.Y.Z` | `vX.Y.Z` |
| `autofluid-tui/Cargo.toml` | `version = "X.Y.Z"` | `X.Y.Z`（无 v 前缀） |
| `docs/architecture-refactoring-plan.md` | `AutoFluid vX.Y.Z` | `vX.Y.Z` |

**注意**：
- `autofluid-tui/Cargo.lock` 中的版本会由 `cargo` 自动更新，不要手动修改
- 使用 `grep_search` 搜索旧版本号字符串，确保没有遗漏任何位置
- 每处修改使用 `replace_string_in_file`，附带 3-5 行上下文确保精确匹配

### 3. 验证

- 使用 `grep_search` 搜索旧版本号，确认已无残留
- 使用 `grep_search` 搜索新版本号，确认所有位置已同步

---

## 输出格式

最终回复按以下结构输出：

```
## 审查结果

### 发现的差异
- [差异1]：文件 X 第 N 行，原文 → 更新后
- [差异2]：...

### 已更新的文件
- `README.md`：更新了 N 处
- `autofluid-tui/Cargo.toml`：版本号 X.Y.Z → A.B.C
- ...

### 版本号变更
- 旧版本：vX.Y.Z
- 新版本：vA.B.C
- 变更理由：...

### 验证结果
- 旧版本号残留：0 处 ✓
- 新版本号同步：N 处 ✓

### 未修改项（无需更新）
- [组件A]：代码与文档一致
- ...
```
