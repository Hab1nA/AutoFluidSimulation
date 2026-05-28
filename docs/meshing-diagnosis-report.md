# Meshing 阶段问题诊断报告

> 生成日期: 2026-05-28  
> 诊断范围: `executor/remote_scripts/batch_meshing_gen4.py` 及相关 meshing 流水线  
> 远程工作站: `ps@172.17.135.240`  
> pyfluent 版本: **0.37.2** | Fluent 版本: **ANSYS Fluent 2024 R1 (v241)**

---

## 一、问题现象

Meshing 阶段启动 Fluent 后报错自动退出。所有构型（0-9）均失败。

---

## 二、根因分析

### 🔴 问题 1（致命）：`conda run` 的 GBK 编码崩溃

**严重程度**: 致命 — 脚本根本无法正常执行

**现象**: 远程工作站 `D:\xkz_1020\flags\` 目录下所有 `.log` 文件（共 6 个）均显示相同的 conda 崩溃错误：

```
UnicodeEncodeError: 'gbk' codec can't encode character '\ufffd' in position 2: illegal multibyte sequence
```

错误发生在 `conda/cli/main_run.py` 第 118 行：
```python
print(response.stdout, file=sys.stdout)
```

**根因链**:

1. `batch_meshing_gen4.py` 通过 `pyfluent.launch_fluent()` 启动 Fluent
2. Fluent 进程输出包含非 UTF-8 字节（如 GBK 编码的中文时间戳 `中国标准时间`、ANSYS 版权信息等）
3. Python 的 `subprocess` 捕获 stdout 时，将无效字节解码为 Unicode 替换字符 `U+FFFD`
4. `conda run` 将子进程的 stdout 打印到控制台时，控制台使用 GBK 编码（中文 Windows 默认）
5. GBK 编码器无法编码 `U+FFFD` → `UnicodeEncodeError` → **conda 进程崩溃**
6. 由于 `.cmd` 脚本中 `conda run` 是主命令，conda 崩溃导致整个后台任务以非零退出码结束

**关键证据** — 远程标志目录中的 `.cmd` 文件：
```batch
@echo off
setlocal
"C:\ProgramData\anaconda3\Scripts\conda.exe" run -n pyfluent python "D:\xkz_1020\scripts/batch_meshing_gen4.py" 0 --mpi-bin-dir "C:\Program Files\ANSYS Inc\v241\fluent\fluent24.1.0\multiport\mpi\win64\intel2021\bin" ...
```

**影响**: 所有 meshing 构型均在 `conda run` 层面崩溃，Python 脚本的实际执行结果被 conda 吞掉，无法正确判断 Fluent 是否真正启动成功。

---

### 🟠 问题 2（严重）：Journal 文件中 `cx-gui-do` GUI 命令依赖

**严重程度**: 严重 — 即使修复问题 1，仍会导致 Fluent 报错

**现象**: `meshing_gen4.jou` 包含大量 `cx-gui-do` 命令：

```scheme
(cx-gui-do cx-activate-item "MenuBar*ToolsSubMenu*Auto Node Move...")
(cx-gui-do cx-set-toggle-button2 "Quality Measure*ToggleBox1(Measure)*Aspect Ratio" #t)
...
```

**问题分析**:

- `cx-gui-do` 是 Fluent 的 Scheme GUI 操控命令，**要求 Fluent 以 GUI 模式运行**
- `batch_meshing_gen4.py` 中设置了 `ui_mode="gui"`，这在本地有显示器时可以工作
- 但在远程工作站通过 SSH + 计划任务启动时，**没有交互式桌面会话**，GUI 窗口无法正常创建
- 即使 `ui_mode="gui"` 参数传递正确，无桌面会话的环境下 `cx-gui-do` 命令会失败

**pyfluent 0.37.2 UIMode 枚举**:
| 枚举值 | 说明 |
|--------|------|
| `UIMode.GUI` | 完整 GUI 模式（需要桌面会话） |
| `UIMode.HIDDEN_GUI` | 隐藏 GUI（后台渲染，无需桌面） |
| `UIMode.NO_GUI` | 无 GUI |
| `UIMode.NO_GRAPHICS` | 无图形 |
| `UIMode.NO_GUI_OR_GRAPHICS` | 完全无界面 |

---

### 🟡 问题 3（中等）：WFT 工作流文件版本不匹配

**严重程度**: 中等 — 可能导致工作流加载异常

**现象**: 部署到远程的 `meshing_gen4.wft` 文件第 3 行：

```json
"version": "23.1"
```

但远程工作站安装的是 **Fluent 2024 R1 (v241)**。版本不匹配可能导致：
- 工作流加载时出现兼容性警告
- 某些 TaskObject 的参数格式不被识别
- `ExecuteUpstreamNonExecutedAndThisTask()` 行为异常

---

### 🟡 问题 4（中等）：WFT 文件就地修改导致状态污染

**严重程度**: 中等 — 重试时可能导致二次失败

**现象**: `batch_meshing_gen4.py` 第 101-106 行就地修改 wft 文件：

```python
with open(args.workflow_path, 'r', encoding='utf-8') as f:
    content = f.read()
content = content.replace('{config}', str(config_id))
with open(args.workflow_path, 'w', encoding='utf-8') as f:
    f.write(content)
```

**问题**:
- 修改的是远程工作站上的**共享 wft 文件**（所有构型共用同一个 `meshing_gen4.wft`）
- 如果脚本在修改后、完成前崩溃，wft 文件中的 `{config}` 已被替换为具体数字
- 下一个构型重试时，`{config}` 已不存在，替换无效，导致文件路径错误
- 例如：构型 0 运行后 wft 中变为 `model_gen4_0.scdoc`，构型 1 重试时找不到 `{config}` 占位符

---

### 🟢 问题 5（轻微）：Fluent Transcript 中的路径乱码

**严重程度**: 轻微 — 仅影响日志可读性

**现象**: Fluent 转录文件 `fluent-20260520-143231-16024.trn` 中：

```
Auto-Transcript Start Time:  14:32:31, 20 May 2026 ?й???׼ʱ??
```

以及：
```
(cx-set-file-dialog-entries "Select File" '( "D:/xkz_1020/?ݴ?/msh/model_gen4_1.msh.h5") ...)
```

**根因**: Fluent 进程使用系统默认编码（GBK）输出中文，但转录文件以二进制方式写入，pyfluent 客户端以 UTF-8 解码时产生乱码。

---

## 三、涉及文件清单

| 文件 | 角色 | 问题 |
|------|------|------|
| `executor/remote_scripts/batch_meshing_gen4.py` | 远程 meshing 主脚本 | 问题 1、2、4 |
| `executor/remote_scripts/meshing_gen4.jou` | Fluent Journal 文件 | 问题 2 |
| `executor/remote_scripts/meshing_gen4.wft` | Fluent 工作流模板 | 问题 3、4 |
| `executor/remote_executor.py` | 远程执行器（构建命令） | 问题 1（命令构建） |
| `utils/ssh_client.py` | SSH 客户端（后台任务启动） | 问题 1（.cmd 脚本生成） |

---

## 四、解决方案

### 修复 1：解决 `conda run` GBK 编码崩溃

**方案 A（推荐）：在 `.cmd` 脚本中设置 `PYTHONUTF8=1`**

修改 `utils/ssh_client.py` 的 `_build_background_cmd_script` 方法，在 `setlocal` 之后添加环境变量设置：

```python
return (
    "@echo off\r\n"
    "setlocal\r\n"
    "set PYTHONUTF8=1\r\n"          # ← 新增
    "set PYTHONIOENCODING=utf-8\r\n" # ← 新增
    f"{command} >> \"{cmd_log}\" 2>&1\r\n"
    ...
)
```

**方案 B：绕过 `conda run`，直接使用 conda 环境的 Python 解释器**

修改 `executor/remote_executor.py` 的 `_build_meshing_command` 方法，将：

```python
command = (
    f'"{conda_exe}" run -n {conda_env} python "{scripts_dir}/batch_meshing_gen4.py" ...'
)
```

改为：

```python
# 获取 conda 环境的 Python 路径
command = (
    f'"{conda_exe}" run -n {conda_env} --no-banner '
    f'python -X utf8 "{scripts_dir}/batch_meshing_gen4.py" ...'
)
```

或更彻底地，直接使用 conda 环境中的 python.exe 路径：

```python
python_exe = f"{conda_root}/envs/{conda_env}/python.exe"
command = f'"{python_exe}" -X utf8 "{scripts_dir}/batch_meshing_gen4.py" ...'
```

**方案 C：在 `batch_meshing_gen4.py` 脚本开头强制 UTF-8**

在脚本最顶部添加：

```python
import sys
import os
os.environ["PYTHONUTF8"] = "1"
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
sys.stderr.reconfigure(encoding='utf-8', errors='replace')
```

> **推荐**: 方案 A + 方案 C 组合使用，双重保障。

---

### 修复 2：将 `cx-gui-do` GUI 命令替换为 pyfluent Python API

**问题**: `meshing_gen4.jou` 中的 `cx-gui-do` 命令依赖 GUI 模式，在远程无桌面环境下不可靠。

**方案**: 将 Journal 文件中的 GUI 操作改写为 pyfluent 的 Python API 调用，直接在 `batch_meshing_gen4.py` 中执行。

具体替换：

| Journal 中的 `cx-gui-do` 操作 | pyfluent Python API 替代 |
|-------------------------------|-------------------------|
| Auto Node Move (Aspect Ratio) | `meshing_session.tui.mesh.modify_zone_auto_node_move(...)` |
| Auto Node Move (Orthogonal Quality) | 同上，切换质量指标 |
| Auto Node Move (Skewness) | 同上，切换质量指标 |

**或者**：将 Auto Node Move 操作也写入 wft 工作流的 TaskObject 中，让 `ExecuteUpstreamNonExecutedAndThisTask()` 统一执行。

**同时**：将 `ui_mode` 从 `"gui"` 改为 `"hidden_gui"` 或 `"no_gui"`，避免在远程环境尝试创建 GUI 窗口：

```python
meshing_session = pyfluent.launch_fluent(
    mode=pyfluent.FluentMode.MESHING,
    precision=pyfluent.Precision.DOUBLE,
    processor_count=args.processor_count,
    product_version=pyfluent.FluentVersion.v241,
    cleanup_on_exit=True,
    ui_mode="hidden_gui",  # ← 改为 hidden_gui
    env={"lang": "zh"}
)
```

---

### 修复 3：更新 WFT 文件版本号

将 `meshing_gen4.wft` 第 3 行的版本号从 `"23.1"` 更新为 `"24.1"`：

```json
"version": "24.1"
```

---

### 修复 4：避免就地修改 WFT 文件

**方案 A（推荐）：创建临时副本**

```python
import tempfile
import shutil

# 创建临时副本
temp_wft = args.workflow_path + f'.{config_id}.tmp'
shutil.copy2(args.workflow_path, temp_wft)

# 在副本上执行替换
with open(temp_wft, 'r', encoding='utf-8') as f:
    content = f.read()
content = content.replace('{config}', str(config_id))
with open(temp_wft, 'w', encoding='utf-8') as f:
    f.write(content)

# 使用副本加载工作流
meshing_session.tui.file.read_journal(...)  # journal 中引用 temp_wft

# 完成后清理
os.remove(temp_wft)
```

**方案 B：通过 pyfluent API 动态设置参数**

不修改 wft 文件，而是在加载工作流后通过 API 动态设置文件路径：

```python
meshing_session.tui.file.read_journal(args.journal_path)
# 通过 workflow API 动态设置 Import Geometry 的文件路径
workflow = meshing_session.workflow
workflow.TaskObject['Import Geometry'].Arguments = {
    "FileName": f"D:/xkz_1020/scdoc/model_gen4_{config_id}.scdoc",
    "NumParts": "1"
}
```

---

## 五、修复优先级

| 优先级 | 问题 | 修复方案 | 预计工作量 |
|--------|------|----------|-----------|
| **P0** | conda GBK 编码崩溃 | 修复 1（方案 A+C） | 小 |
| **P1** | cx-gui-do GUI 依赖 | 修复 2 | 中 |
| **P2** | WFT 版本不匹配 | 修复 3 | 极小 |
| **P2** | WFT 就地修改污染 | 修复 4 | 小 |

---

## 六、验证步骤

修复后按以下步骤验证：

1. **修复 1 验证**: 在远程工作站手动执行修改后的 `.cmd` 脚本，确认不再出现 `UnicodeEncodeError`
2. **修复 2 验证**: 使用 `ui_mode="hidden_gui"` 启动 Fluent Meshing，确认 Auto Node Move 操作正常完成
3. **修复 3 验证**: 确认 wft 文件加载无版本警告
4. **修复 4 验证**: 连续运行两个不同构型，确认第二个构型的 wft 文件中 `{config}` 占位符仍存在
5. **端到端验证**: 选择单个构型（如构型 0）运行完整 meshing 流程，确认 `.msh.h5` 文件生成成功

---

## 七、附录

### A. 远程工作站环境信息

| 项目 | 值 |
|------|-----|
| OS | Windows 10 (10.0.19045) |
| CPU | AMD EPYC 9654 96-Core Processor (384 核) |
| Conda | 24.9.2 (anaconda3) |
| Python (base) | 3.12.7 |
| pyfluent | 0.37.2 |
| Fluent | ANSYS Fluent 2024 R1 (v241) |
| AWP_ROOT241 | `C:\Program Files\ANSYS Inc\v241` |
| pyfluent 环境 | conda `pyfluent` |

### B. pyfluent 0.37.2 `launch_fluent` 关键参数

```python
pyfluent.launch_fluent(
    mode=FluentMode.MESHING,        # 启动 Meshing 模式
    precision=Precision.DOUBLE,      # 双精度
    processor_count=64,              # 64 核
    product_version=FluentVersion.v241,  # Fluent 2024 R1
    cleanup_on_exit=True,
    ui_mode="gui",                   # GUI 模式（需桌面会话）
    env={"lang": "zh"},              # 中文环境
)
```

### C. 远程目录结构

```
D:\xkz_1020\
├── scripts\          # 远程脚本（7 个文件）
│   ├── batch_meshing_gen4.py
│   ├── batch_solver_gen4.py
│   ├── meshing_gen4.jou
│   ├── meshing_gen4.wft
│   ├── solver_gen4.jou
│   ├── solver_gen4.set
│   └── solver_post_gen4.jou
├── scdoc\            # SCDOC 输入（当前为空）
├── msh\              # 网格输出（当前为空）
├── flags\            # 标志文件 + 后台任务日志
├── workingdir\       # Fluent 工作目录
│   ├── animation-t\
│   ├── animation-v\
│   └── fluent-20260520-143231-16024.trn
├── fluent_chemkin_files\  # 仿真引用文件（4 个）
├── case\             # 求解结果输出
```

### D. 受影响的构型列表

所有后台任务日志显示以下构型均因 conda GBK 错误失败：

| 构型 ID | 任务哈希 | 错误 |
|---------|---------|------|
| 0 | e54f76eca008 | UnicodeEncodeError |
| 1 | e25fd91567e3 | UnicodeEncodeError |
| 4 | be05db58f884 | UnicodeEncodeError |
| 6 | cc7b04fdd273 | UnicodeEncodeError |
| 7 | d21759e45ac3 | UnicodeEncodeError |
| 9 | 1a51ee5bff73 | UnicodeEncodeError |
