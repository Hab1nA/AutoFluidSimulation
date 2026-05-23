---
description: "C# 代码规范与质量门禁。编辑 .cs 文件时自动加载，提供命名规范、防污染规则。"
applyTo: ["bridge/**/*.cs", "bridge/**/*.csproj"]
---

# C# 代码规范（SpaceClaim Bridge）

> 本文件仅适用于 C# 代码（`bridge/` 目录）。编辑 Python 代码请参阅 `python.instructions.md`，编辑 Rust 代码请参阅 `rust.instructions.md`。

---

## 🚫 语言隔离规则（最高优先级）

- **禁止**在 `.cs` 文件中插入 Python 代码、`def`/`class`/`import` 语句、Python 文档片段
- **禁止**在 `.cs` 文件中插入 Rust 代码、`fn main()`、`impl` 块、`tokio`/`ratatui` 等 Rust 专用内容
- **禁止**将 Python 或 Rust 的 API 文档、示例代码复制到 C# 文件中
- 如果发现上下文中混入了其他语言的代码或文档，**忽略它们**，仅关注 C# 相关内容

---

## 项目结构

```
bridge/SpaceClaimBridge/
├── SpaceClaimBridge.csproj    # .NET 4.8 项目文件
├── Program.cs                 # 主程序（含完整引用版本）
├── Program.NoRef.cs           # 主程序（无外部引用版本，用于编译验证）
├── compile.bat                # 编译脚本（含引用）
└── compile_noref.bat          # 编译脚本（无引用）
```

---

## 命名规范

| 类别 | 风格 | 示例 |
|------|------|------|
| 命名空间 | `PascalCase.分层` | `AutoFluidSimulation.Bridge` |
| 类名 | `PascalCase` | `Program` |
| 方法 | `PascalCase` | `ParseArguments()`, `ValidateOutput()` |
| 私有字段 | `_camelCase` | `_options`, `_process` |
| 常量 | `PascalCase` | `ProcessName`, `SpaceClaimExePaths` |
| 枚举 | `PascalCase` | `ExitCode` |
| 枚举值 | `PascalCase` | `Success`, `ScriptFailed`, `LaunchFailed` |
| 参数 | `camelCase` | `args`, `timeout`, `scriptPath` |

### 退出码枚举

```csharp
enum ExitCode
{
    Success = 0,
    ScriptFailed = 1,
    LaunchFailed = 2,
    OutputValidationFailed = 3,
    InvalidArgs = 4,
    Timeout = 5,
}
```

---

## 代码风格

- 目标框架：`.NET Framework 4.8`
- 使用 `namespace` 块级声明（非文件级 `namespace`）
- XML 文档注释使用 `///`，用于公共类型和方法
- 异常处理：顶层 `try-catch` 捕获所有未处理异常，返回对应 `ExitCode`
- 进程管理：使用 `Process` 类启动 SpaceClaim，通过轮询检测进程状态

---

## Bridge 与 Python 端的交互

- Bridge 由 Python 端的 `SCProcessPool` 通过 `subprocess` 启动
- 命令行参数格式：`--script <path> --config <id> --stepdir <dir> --scdocdir <dir> [--timeout <sec>]`
- 返回值（ExitCode）被 Python 端用于判断执行结果
- **修改 ExitCode 时必须同步更新 Python 端的退出码处理逻辑**

---

## 质量门禁

修改 C# 代码后，必须通过以下检查：

| 检查项 | 命令 | 标准 |
|--------|------|------|
| 编译 | `compile.bat` 或 `compile_noref.bat` | 编译成功，零错误 |
| 退出码一致性 | 人工检查 | Python 端 `SCProcessPool` 正确处理所有 ExitCode |
