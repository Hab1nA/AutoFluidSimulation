---
description: "Use when: 全面审查 C# Bridge 代码质量、规范性和健壮性，迭代修复直至通过所有检查。触发词：审查 C#、review csharp、C# 代码审查、C# code review。"
agent: "agent"
---

# C# 代码全面审查与修复

> **本 Prompt 仅审查 C# 代码。** Python 代码请使用 `review-python.prompt.md`，Rust 代码请使用 `review-rust.prompt.md`。

## 审查前准备

1. 阅读 `.github/instructions/csharp.instructions.md`，掌握 C# 代码规范
2. 阅读 `AGENTS.md` 了解项目架构、关键交互逻辑和高风险兼容区
3. 阅读 `README.md`，了解项目全貌

## 🚫 语言隔离规则

- **只审查 `bridge/` 下的 `.cs` 和 `.csproj` 文件**，不要查看或引用 `.py` 或 `.rs` 文件的内容
- 如果在审查过程中发现与 Python 的接口交互问题（如 ExitCode 处理），记录为"跨语言接口问题"，但**不要修改**非 C# 文件
- 不要在建议中混入其他语言的代码片段或 API

## 审查范围

仅限 `bridge/SpaceClaimBridge/` 目录下的所有文件：

- `Program.cs` — 主程序（含完整引用版本）
- `Program.NoRef.cs` — 主程序（无外部引用版本）
- `SpaceClaimBridge.csproj` — 项目文件
- `compile.bat` / `compile_noref.bat` — 编译脚本

## 【审查流程】

### 第 1 步：全面系统性审查

审查所有 C# 文件，重点评估：

- **代码质量**：可读性、可维护性、是否符合 .NET 编码惯例
- **编码规范性**：是否符合 `csharp.instructions.md` 中的命名规范、XML 文档注释要求
- **代码健壮性**：异常处理机制、进程管理的可靠性、超时处理
- **进程管理**：SpaceClaim 进程的启动/检测/终止逻辑是否健壮
- **参数解析**：命令行参数解析是否正确、边界条件处理
- **退出码一致性**：`ExitCode` 枚举值是否与 Python 端 `SCProcessPool` 的退出码处理逻辑匹配
- **编译验证**：两个编译脚本是否都能成功编译

### 第 2 步：迭代修复循环

1. 初次审查后记录所有发现的问题
2. 针对问题进行代码修改
3. 修改完成后立即开展二次审查
4. 如二次审查仍发现问题，继续修改并再次审查
5. 如此循环迭代，直至连续两次审查均未发现任何代码问题

### 第 3 步：测试验证

```bash
cd bridge/SpaceClaimBridge
compile_noref.bat
compile.bat
```

两个编译脚本均须零错误通过。如编译失败，修复问题后重新编译，直到成功。

### 第 4 步：代码清理与优化

- 识别并移除所有冗余代码、未使用的变量/方法
- 清理多余的注释内容
- 优化代码组织结构以提升逻辑清晰度和可扩展性
- 确保最终交付的代码达到高质量、高可维护性标准
