# C# Bridge 第二轮审查报告

> **审查范围**: `bridge/SpaceClaimBridge/` 全部 `.cs`、`.csproj`、`.bat` 文件
> **接口比对**: Python 端 `engine/sc_process_pool.py` 的 ExitCode 处理与 Bridge 交互逻辑
> **审查重点**: 功能重复实现、不规范代码、边界条件与竞态条件等逻辑缺陷
> **审查日期**: 2026-06-05
> **说明**: 仅汇总建议，不执行代码修改

---

## 🔴 严重问题 (3 个)

### 1. 常驻模式就绪超时 Python/C# 不匹配

| 项目 | 值 | 位置 |
|------|----|------|
| C# 端 | `PersistentReadyTimeoutSeconds = 120` | `Program.cs` 第 55 行 |
| Python 端 | `ENGINE_CONFIG.get("sc_persistent_ready_timeout", 180)` | `sc_process_pool.py` 第 365 行 |

**问题描述**: C# Bridge 等待 `sc_ready_{slot}.json` 的超时硬编码为 120 秒，Python 端等待同一文件的超时为 180 秒。当 SpaceClaim 启动耗时介于 120–180 秒之间时：

1. C# Bridge 以 `ExitCode.Timeout (5)` 退出
2. Python 端仍认为 Bridge 存活，继续等待就绪文件
3. Python 端随后检测到 Bridge 进程已退出，记录为 `LaunchFailed` 而非 `Timeout`
4. **根因**：超时语义被错误转换——本应是"超时"，日志中却变成"启动失败"

**建议修复**: 将 `PersistentReadyTimeoutSeconds` 改为通过 `GetEnvInt("AUTOFLUID_SC_PERSISTENT_READY_TIMEOUT", 180)` 读取，与 Python 端默认值保持一致。Python 端已支持通过 `ENGINE_CONFIG["sc_persistent_ready_timeout"]` 配置此值，两端共享同一环境变量即可。

---

### 2. 常驻模式异常退出时 exitCode 恒为 0

**位置**: `Program.cs` `ExecutePersistent` 方法，约第 440 行

```csharp
int exitCode = (int)ExitCode.Success;  // 初始化为 0
// ... 命令循环中所有 break 路径均未修改 exitCode ...
return exitCode;  // 永远返回 0
```

**问题描述**: 无论 SpaceClaim 是正常退出（收到 quit 命令）还是异常退出（崩溃、进程消失、连续检测失败），`Main()` 始终返回 0（Success）。

**影响链**:
- Python 端 `sc_process_pool.py` 第 481 行通过 `slot.process.poll() is not None` 检测到 Bridge 退出
- 第 483 行调用 `_format_bridge_exit(slot.process.returncode)`，由于 returncode=0，日志显示 `exit=0 (Success)`
- Python 端将此视为正常退出，**不会触发重试或错误恢复**
- 实际上 SpaceClaim 可能因脚本错误、内存不足等原因崩溃，这些异常被静默吞掉

**补充**: Python 端 `_load_bridge_monitor_info` 方法（第 602 行）虽然读取了 monitor 文件的 `status` 字段，但**仅做日志记录，未基于该字段做任何判断**。即使 C# 写入了 `status: "spaceclaim_exited"`，Python 端也不会据此采取行动。

**建议修复**:
- quit 命令触发退出 → `exitCode = (int)ExitCode.Success`（保持不变）
- SpaceClaim 进程正常退出（`HasExited` 为 true）→ `exitCode = (int)ExitCode.ScriptFailed`
- SpaceClaim 进程对象已释放（`InvalidOperationException`）→ `exitCode = (int)ExitCode.ScriptFailed`
- 进程检测连续失败达阈值 → `exitCode = (int)ExitCode.LaunchFailed`
- 同步建议 Python 端在检测到 Bridge 退出后，检查 monitor 文件的 status 字段判断是否为异常退出

---

### 3. quit 命令检测与进程退出检测的竞态条件

**位置**: `Program.cs` `ExecutePersistent` 命令循环，约第 444–497 行

**当前执行顺序**:

```
while (true) {
    ① 检查 quit 命令文件    ← 先执行
    ② 检查进程是否已退出    ← 后执行
}
```

**竞态场景**: Python 端写入 quit 文件后，SpaceClaim 在同一轮循环迭代中恰好崩溃退出。由于 quit 检查在前，Bridge 以"收到 quit 命令"路径退出，monitor 文件的 status 未更新（仍为 "launched"），掩盖了 SpaceClaim 的异常退出。

**建议修复**: 调整检测顺序——先检查进程状态，再检查 quit 命令。具体修改：

```
while (true) {
    ① 检查进程是否已退出
       → 若已退出，写 monitor 文件，根据是否同时存在 quit 文件决定 exitCode
    ② 检查 quit 命令文件
       → 若收到 quit，先检查进程是否仍在运行，再决定退出路径
}
```

---

## 🟡 中等问题 (4 个)

### 4. WaitForGuiReady Phase 1 对进程死亡的异常处理不完整

**位置**: `Program.cs` `WaitForGuiReady` 方法，Phase 1 循环

```csharp
catch (Exception ex)
{
    Console.Error.WriteLine($"[BRIDGE] Warning: 主窗口检测异常: {ex.Message}");
    // 继续循环 → 进程已死仍等待最多 timeoutSec 秒
}
```

**问题描述**: `InvalidOperationException`（进程对象已释放）或 `Win32Exception`（进程句柄无效）明确表明进程已死亡，但当前代码仅打印警告后继续轮询。在进程已退出的情况下，Bridge 仍白白等待最多 `DefaultGuiReadyTimeoutSeconds`（30 秒）。

**建议修复**: 在 catch 块中增加对 `InvalidOperationException` 的特殊处理——直接抛出或设置标志跳出循环，让调用方快速判断 SpaceClaim 未成功启动。其他异常保留当前的警告+继续逻辑。

---

### 5. WaitForProcessAppear 进程资源泄漏

**位置**: `Program.cs` `WaitForProcessAppear` 方法

存在两处 `Process` 对象泄漏：

**(a) StartTime 访问异常时泄漏**

```csharp
foreach (Process p in procs)
{
    try
    {
        if (!existingPids.Contains(p.Id) && p.StartTime.ToUniversalTime() >= after)
        { ... }
    }
    catch (Exception ex)
    {
        Console.Error.WriteLine(...);
        // ← p 未被 Dispose，泄漏
    }
}
```

当 `p.StartTime` 抛出 `Win32Exception`（权限不足）或 `InvalidOperationException`（进程已退出）时，`p` 不会被释放。

**(b) 匹配成功时的提前返回**

找到匹配进程后，代码释放"其他进程"然后 `return`。但当前 `foreach` 之前迭代中 `StartTime` 抛异常的进程已在 (a) 中泄漏，且这些泄漏对象无法被后续的 `foreach (var p in procs) p.Dispose()` 清理（因为已经 return 了）。

**建议修复**:
- 在 catch 块中增加 `p.Dispose()` 调用
- 或改用 `try-finally` 模式确保所有路径都释放进程对象
- 长时间运行场景下，句柄泄漏会逐渐累积，最终导致 `Process.GetProcessesByName` 失败

---

### 6. 参数值缺失时错误信息不明确

**位置**: `Program.cs` `ParseArguments` 方法

当用户输入 `SpaceClaimBridge.exe --script`（末尾无值）时：
- `++i < args.Length` 为 false，`options.ScriptPath` 保持 null
- 外层循环正常结束，进入必需参数检查
- 打印 `"缺少必要参数"` — 但用户无法分辨是"没传 --script"还是"传了 --script 但没给值"

**建议修复**: 在 `++i >= args.Length` 时，打印具体错误信息：

```csharp
case "--script":
    if (++i < args.Length) { options.ScriptPath = args[i]; }
    else { Console.Error.WriteLine("[BRIDGE_ERROR] 参数 --script 缺少值"); return null; }
    break;
```

同理适用于 `--config`、`--stepdir`、`--scdocdir`、`--cmddir`、`--slotid` 等所有需要值的参数。

---

### 7. 命令循环无心跳监控文件更新

**位置**: `Program.cs` `ExecutePersistent` 方法

`WritePersistentMonitorFile` 仅在两处被调用：
1. SpaceClaim 启动后（`status = "launched"`）— 约第 420 行
2. SpaceClaim 进程正常退出时（`status = "spaceclaim_exited"`）— 约第 478 行

在长时间运行的命令循环期间（可能持续数小时），monitor 文件的 `timestamp_utc` 始终停留在启动时刻。如果 Bridge 进程本身挂起（死锁、无限循环、线程阻塞），Python 端无法通过 monitor 文件检测到异常。

**补充**: 当进程检测连续失败达阈值（第 494 行的 `break`）时，也没有调用 `WritePersistentMonitorFile`，monitor 文件的 status 停留在 "launched"。

**建议修复**:
- 在命令循环中增加周期性更新（例如每 30 秒），写入当前时间戳和 `"running"` 状态
- 在所有退出路径（包括异常退出）都调用 `WritePersistentMonitorFile`
- Python 端可检查 `timestamp_utc` 是否过期来判断 Bridge 是否挂起

---

## 🟢 低级问题 (3 个)

### 8. cmd 文件读取存在 TOCTOU 竞态

**位置**: `Program.cs` 命令循环中的 quit 检测

```csharp
if (File.Exists(cmdFile))           // ← 检查点
{
    string content = File.ReadAllText(cmdFile).Trim();  // ← 使用点
```

`File.Exists` 与 `File.ReadAllText` 之间文件可能被 transit 脚本删除或重命名。虽然 `catch (IOException)` 兜底了这种情况，但还有更微妙的问题：如果 transit 脚本正在以覆盖模式写入文件，`ReadAllText` 可能读取到不完整的 JSON 内容。

**建议修复**: 改用带共享标志的 `FileStream`：

```csharp
using (var fs = new FileStream(cmdFile, FileMode.Open, FileAccess.Read,
    FileShare.ReadWrite | FileShare.Delete))
using (var sr = new StreamReader(fs))
{
    string content = sr.ReadToEnd().Trim();
    // ...
}
```

---

### 9. JSON 手动拼接缺乏转义

**位置**: `Program.cs` `WritePersistentMonitorFile` 方法

```csharp
string json = "{" +
    $"\"slot_id\":{opts.SlotId}," +
    // ...
    $"\"status\":\"{status}\"," +    // ← status 未做 JSON 转义
    // ...
```

当前 `status` 参数均为硬编码的已知值（`"launched"`、`"spaceclaim_exited"`），不存在实际风险。但作为防御性编程，若未来传入包含 `"` 或 `\` 的字符串，将生成无效 JSON，导致 Python 端 `json.load()` 失败。

**建议修复**: 增加简单的 JSON 字符串转义：

```csharp
private static string EscapeJsonString(string s)
{
    return s.Replace("\\", "\\\\").Replace("\"", "\\\"")
            .Replace("\n", "\\n").Replace("\r", "\\r").Replace("\t", "\\t");
}
```

---

### 10. 环境变量在两层进程中冗余设置

**位置**:
- `Program.cs` `ExecutePersistent`（约第 355 行）→ 给 SpaceClaim 进程
- `sc_process_pool.py` `_launch_persistent_process`（约第 296 行）→ 给 Bridge 进程

| 环境变量 | Python 设置给 Bridge | C# 设置给 SpaceClaim |
|----------|---------------------|---------------------|
| `AUTOFLUID_SC_NOEXIT` | ✅ | ✅ |
| `AUTOFLUID_SC_PERSISTENT` | ✅ | ✅ |
| `AUTOFLUID_SC_CMD_DIR` | ✅ | ✅ |
| `AUTOFLUID_SC_SLOT_ID` | ✅ | ✅ |

虽然两处作用于不同层级的进程（Python→Bridge vs Bridge→SpaceClaim），功能上不冲突，但同一变量在两处设置容易造成维护时改一处忘改另一处。

**建议修复**: 在 Bridge 代码中添加注释明确标注各环境变量的来源和目标进程层级。或者考虑让 Bridge 从自身环境中读取并透传给 SpaceClaim，而非重新构建。

---

## 📊 审查总结

| 严重程度 | 编号 | 问题概要 | 影响 |
|----------|------|----------|------|
| 🔴 严重 | #1 | 就绪超时 Python 120s vs C# 180s | 超时语义被错误转换 |
| 🔴 严重 | #2 | exitCode 恒为 0 | 异常退出被静默吞掉 |
| 🔴 严重 | #3 | quit/进程退出竞态 | 异常退出被掩盖 |
| 🟡 中等 | #4 | GUI 就绪异常处理不完整 | 进程已死仍等待 30s |
| 🟡 中等 | #5 | 进程资源泄漏 | 长时间运行句柄耗尽 |
| 🟡 中等 | #6 | 参数值缺失错误信息不明确 | 用户体验差 |
| 🟡 中等 | #7 | 命令循环无心跳 | Bridge 挂起无法检测 |
| 🟢 低级 | #8 | cmd 文件 TOCTOU 竞态 | 极低概率读到脏数据 |
| 🟢 低级 | #9 | JSON 手动拼接无转义 | 未来扩展时有风险 |
| 🟢 低级 | #10 | 环境变量冗余设置 | 维护风险 |

## 建议修复顺序

1. **#2 exitCode 恒为 0** — 影响 Python 端错误恢复逻辑，修复最简单（在各 break 路径前设置 exitCode）
2. **#1 超时不匹配** — 影响错误诊断准确性，改动量极小（一行改为 GetEnvInt）
3. **#3 quit 竞态** — 与 #2 关联，可一并修复（调整检测顺序 + exitCode 联动）
4. **#5 进程资源泄漏** — 长时间运行时导致句柄泄漏（catch 中加 Dispose）
5. **#7 无心跳监控** — 提升 Bridge 挂起时的可观测性
6. **#4 GUI 就绪异常处理** — 减少无效等待时间
7. **#6 参数校验** — 改善用户体验
8. **#8 #9 #10** — 按优先级逐步修复
