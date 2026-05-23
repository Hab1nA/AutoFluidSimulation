# SpaceClaim Bridge 接口文档

> 本文档记录 C# SpaceClaimBridge.exe 的对外接口，供 Python 端 (`SCProcessPool`) 对接使用。
>
> 最后更新：2026-05-23

---

## 1. 命令行参数

### 1.1 单次模式（默认）

```bash
SpaceClaimBridge.exe
  --script   <transit脚本路径>    # 必需
  --config   <构型编号>           # 必需
  --stepdir  <STEP文件目录>       # 必需
  --scdocdir <SCDOC输出目录>      # 必需
  [--timeout <秒数>]              # 可选，默认 300
  [--sc-exe  <SpaceClaim.exe路径>] # 可选，覆盖自动检测
```

### 1.2 常驻模式

```bash
SpaceClaimBridge.exe
  --persistent                    # 启用常驻模式
  --script   <transit脚本路径>    # 必需
  --cmddir   <IPC命令目录>       # 必需
  [--slotid  <槽位ID>]           # 可选，默认 0
  [--sc-exe  <SpaceClaim.exe路径>] # 可选，覆盖自动检测
```

---

## 2. 退出码（ExitCode）

| 值 | 枚举名 | 含义 |
|----|--------|------|
| 0 | `Success` | 成功，SCDOC 文件已生成 |
| 1 | `ScriptFailed` | 脚本执行失败 / 未处理异常 |
| 2 | `LaunchFailed` | SpaceClaim 启动失败（进程未出现、GUI 就绪超时） |
| 3 | `OutputValidationFailed` | STEP 文件不存在 / SpaceClaim 退出但未生成 SCDOC |
| 4 | `InvalidArgs` | 参数错误 |
| 5 | `Timeout` | 执行超时 |

**Python 端对照**：`SCProcessPool` 通过 `slot.process.returncode` 读取退出码，目前仅用于日志记录，未对不同退出码做差异化处理。

---

## 3. 环境变量

Bridge 支持通过环境变量传入配置（由 Python 端在 `subprocess.Popen` 时注入）。

### 3.1 通用变量

| 变量名 | 说明 | 默认值 |
|--------|------|--------|
| `AUTOFLUID_SC_EXE` | SpaceClaim.exe 路径（命令行 `--sc-exe` 优先） | 自动检测 |
| `AUTOFLUID_SC_PROCESS_APPEAR_TIMEOUT` | 等待 SC 进程出现的超时秒数 | 120 |
| `AUTOFLUID_SC_GUI_READY_TIMEOUT` | 等待 GUI 就绪的超时秒数 | 30 |
| `AUTOFLUID_SC_GUI_STABLE_DELAY` | GUI 就绪后的额外稳定延时秒数 | 15 |

### 3.2 常驻模式专用变量

| 变量名 | 说明 |
|--------|------|
| `AUTOFLUID_SC_PERSISTENT` | 设为 `1` 启用常驻模式 |
| `AUTOFLUID_SC_NOEXIT` | 设为 `1`，传递给 transit 脚本 |
| `AUTOFLUID_SC_CMD_DIR` | IPC 命令目录（与 `--cmddir` 相同） |
| `AUTOFLUID_SC_SLOT_ID` | 槽位 ID（与 `--slotid` 相同） |

> **注意**：命令行参数优先于环境变量。Python 端目前同时通过命令行和环境变量传递 `cmddir` / `slotid`，两者均可。

---

## 4. GUI 就绪检测（WaitForGuiReady）

Bridge 启动 SpaceClaim 后，通过三阶段检测确保 GUI 就绪：

```
Phase 1: 轮询 MainWindowHandle != IntPtr.Zero（超时由 AUTOFLUID_SC_GUI_READY_TIMEOUT 控制）
    ↓ 超时
Phase 2: WaitForInputIdle(15s) 等待主线程空闲
    ↓ 超时
Phase 3: 固定延时等待加载稳定（由 AUTOFLUID_SC_GUI_STABLE_DELAY 控制）
```

Phase 3 的环境变量读取逻辑：

```csharp
int guiWaitSeconds = GetEnvInt("AUTOFLUID_SC_GUI_STABLE_DELAY", 15);
```

---

## 5. 常驻模式文件协议

### 5.1 文件命名规则

所有 IPC 文件位于 `--cmddir` 指定的目录下：

| 文件 | 用途 | 方向 |
|------|------|------|
| `sc_ready_{slot_id}.json` | 就绪标志 | Bridge → Python |
| `sc_cmd_{slot_id}.json` | 命令文件 | Python → Bridge |
| `sc_result_{slot_id}_{run_id}.json` | 结果文件 | Transit 脚本 → Python |

### 5.2 命令文件格式

Python 端写入 `sc_cmd_{slot_id}.json`：

```json
{
  "command": "process",
  "run_id": "abc123def456",
  "config": 3,
  "stepdir": "C:\\path\\to\\step",
  "scdocdir": "C:\\path\\to\\scdoc"
}
```

退出命令：
```json
{
  "command": "quit"
}
```

### 5.3 就绪标志

Bridge 等待 transit 脚本写入 `sc_ready_{slot_id}.json`（内容任意，文件存在即视为就绪）。超时 120 秒。

### 5.4 结果文件

由 transit 脚本写入，Bridge 本身**不读取**结果文件。Bridge 通过轮询 SCDOC 输出文件 + 进程状态判断完成。

---

## 6. 单次模式工作流程

```
1. 解析参数
2. 检查 STEP 文件存在
3. 自动检测 SpaceClaim.exe
4. 启动 SpaceClaim（/RunScript + 环境变量传参）
5. WaitForProcessAppear：轮询新进程出现
6. WaitForGuiReady：三阶段 GUI 就绪检测
7. 轮询 SCDOC 文件生成（或进程退出）
8. 返回 ExitCode
```

---

## 7. 常驻模式工作流程

```
1. 解析参数
2. 启动 SpaceClaim（/RunScript，无 /ExitAfterScript）
3. WaitForProcessAppear
4. WaitForGuiReady
5. 等待 sc_ready_{slot_id}.json 出现
6. 命令循环：
   a. 检测 sc_cmd_{slot_id}.json → 解析 command
   b. 若 command == "quit" → 删除命令文件 → 退出循环
   c. 若进程已退出 → 退出循环
   d. Sleep(1000) → 回到 a
7. 返回 ExitCode.Success
```

---

## 8. Python 端对接要点

### 8.1 启动 Bridge

```python
cmd = [
    self._bridge_path,
    "--persistent",
    "--script", self._sc_script,
    "--cmddir", self._persistent_cmd_dir,
    "--slotid", str(slot.slot_id),
]
```

### 8.2 环境变量注入

```python
sc_env["AUTOFLUID_SC_PROCESS_APPEAR_TIMEOUT"] = str(
    OPERATION_TIMEOUTS.get("sc_process_appear_timeout", 120)
)
sc_env["AUTOFLUID_SC_GUI_READY_TIMEOUT"] = str(
    OPERATION_TIMEOUTS.get("sc_gui_ready_timeout", 30)
)
sc_env["AUTOFLUID_SC_GUI_STABLE_DELAY"] = str(
    OPERATION_TIMEOUTS.get("sc_gui_stable_delay", 15)
)
```

### 8.3 优雅关闭

Python 端 shutdown 时写入 `sc_cmd_{slot_id}.json`：

```python
quit_data = {"command": "quit"}
with open(cmd_file, "w") as f:
    json.dump(quit_data, f)
```

Bridge 命令循环检测到 `"quit"` 后会删除命令文件并退出。

### 8.4 超时配置对照

| Python `OPERATION_TIMEOUTS` 键 | Bridge 环境变量 | 默认值 |
|-------------------------------|-----------------|--------|
| `sc_process_appear_timeout` | `AUTOFLUID_SC_PROCESS_APPEAR_TIMEOUT` | 120 |
| `sc_gui_ready_timeout` | `AUTOFLUID_SC_GUI_READY_TIMEOUT` | 30 |
| `sc_gui_stable_delay` | `AUTOFLUID_SC_GUI_STABLE_DELAY` | 15 |

---

## 9. 日志前缀

Bridge 所有日志以 `[BRIDGE]` 或 `[BRIDGE_ERROR]` 为前缀，输出到 stdout/stderr。Python 端可通过 `subprocess.DEVNULL` 丢弃，或重定向到日志文件。

---

## 10. SpaceClaim.exe 自动检测

优先级从高到低：

1. 命令行 `--sc-exe` 参数
2. 环境变量 `AUTOFLUID_SC_EXE`
3. 硬编码路径列表：
   - `C:\Program Files\ANSYS Inc\v231\SCDM\SpaceClaim.exe`
   - `C:\Program Files\ANSYS Inc\v232\SCDM\SpaceClaim.exe`
   - `C:\Program Files\ANSYS Inc\v241\SCDM\SpaceClaim.exe`
