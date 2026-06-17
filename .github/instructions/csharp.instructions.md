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
├── SpaceClaimBridge.NoRef.csproj # 无引用项目文件（编译验证用）
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

### XML 文档注释规范

公共类型和方法必须使用 `///` 注释，包含 `<summary>` 和 `<param>` 标签：

```csharp
/// <summary>
/// 解析命令行参数并返回结构化选项。
/// </summary>
/// <param name="args">原始命令行参数数组</param>
/// <returns>解析后的选项对象，解析失败返回 null</returns>
static Options? ParseArguments(string[] args)
{
    // ...
}
```

- 所有 `public` 方法、属性和类应有 `<summary>`
- 带参数的方法应有 `<param>` 标签
- 有返回值的方法应有 `<returns>` 标签
- 可能为 `null` 的返回值需在注释中注明

### 异常处理模式

```csharp
static ExitCode Main(string[] args)
{
    try
    {
        // 主逻辑
        var options = ParseArguments(args);
        if (options == null) return ExitCode.InvalidArgs;

        RunScript(options);
        return ExitCode.Success;
    }
    catch (TimeoutException)
    {
        Console.Error.WriteLine("操作超时");
        return ExitCode.Timeout;
    }
    catch (Exception ex)
    {
        Console.Error.WriteLine($"未处理异常: {ex.Message}");
        return ExitCode.ScriptFailed;
    }
}
```

- 顶层 `try-catch` 必须覆盖所有 `ExitCode` 枚举值
- 已知异常（如超时）优先捕获并返回专用退出码
- 未知异常兜底返回 `ScriptFailed`（ExitCode = 1）
- 使用 `Console.Error.WriteLine` 输出错误信息（stderr）

### 进程检测轮询规范

```csharp
// ✅ 使用轮询 + 超时检测进程状态
var sw = Stopwatch.StartNew();
while (!process.HasExited)
{
    if (sw.ElapsedMilliseconds > timeoutMs)
    {
        process.Kill();
        throw new TimeoutException("SpaceClaim 进程超时");
    }
    Thread.Sleep(1000); // 每秒轮询一次
}
```

- 轮询间隔建议 1 秒（`Thread.Sleep(1000)`），避免 CPU 空转
- 必须设置超时上限（默认值在调用方通过 `--timeout` 传入）
- 超时后先 `Kill()` 进程再抛异常
- 使用 `Stopwatch` 而非 `DateTime` 测量耗时

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
