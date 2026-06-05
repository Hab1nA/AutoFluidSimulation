# 远程任务恢复能力诊断报告

> 审计日期: 2026-06-05
> 涉及范围: Meshing/Solver 远程任务的启动、轮询、取消、恢复、数据库状态同步
> 严重程度: **Critical** — Daemon 重启后可能导致重复启动远程 Fluent 任务

---

## 一、问题概述

当 Daemon 进程重启（崩溃、用户手动重启、系统断电恢复等）后，如果远程 Windows 工作站上仍有 Fluent Meshing/Solver 计划任务在运行，当前代码**无法重新识别这些任务**，存在以下风险：

1. **重复启动远程任务**：DB 中状态为 `Running`，恢复逻辑将其重置为 `Waiting`，然后重新启动一个全新的远程 Fluent 任务，导致远程工作站上同时运行两个相同构型的 Fluent 实例。
2. **远程资源泄漏**：旧的 Windows 计划任务不会被终止或清理。
3. **结果冲突**：两个 Fluent 实例操作同一文件可能导致损坏。

---

## 二、架构审计

### 2.1 远程任务生命周期

```
RemoteExecutor.start_meshing(config_name)
  → SSH exec_background(command, flag_file)
    → 生成 cmd 脚本 → schtasks /Create → schtasks /Run → schtasks /Change /DISABLE
    → 返回 (success, task_name)    ← task_name = "AutoFluid_{md5_hash[:12]}"
  → self._remote_tasks[config_name] = task_name  ← 仅保存在内存 dict 中
```

**关键发现**：`_remote_tasks` 是 `RemoteExecutor.__init__` 中的 `dict[int, str]` 字段，**没有任何持久化机制**。

### 2.2 Windows 计划任务命名规则

```python
# ssh_client.py: exec_background()
task_hash = hashlib.md5(
    f"{time.time_ns()}:{command}:{flag_file}".encode("utf-8")
).hexdigest()[:12]
task_name = f"AutoFluid_{task_hash}"
```

- `time.time_ns()` 使得任务名**不可从 config_name 推导**
- 但任务名遵循 `AutoFluid_*` 前缀，可通过 `schtasks /Query` 模糊匹配
- 每个任务附带的文件（均路径可从 hash 推导）：
  - 脚本: `{flag_dir}/autofluid_bg_{hash}.cmd`
  - 日志: `{flag_dir}/autofluid_bg_{hash}.log`
  - PID: `{flag_dir}/autofluid_bg_{hash}.pid`

### 2.3 标志文件（Flag Files）

| 步骤 | 完成标志 | 错误标志 |
|------|---------|---------|
| Meshing | `{flag_dir}/meshing_done_{config}.txt` | `{flag_dir}/meshing_done_{config}.txt.error` |
| Solver | `{flag_dir}/solver_done_{config}.txt` | `{flag_dir}/solver_done_{config}.txt.error` |

标志文件路径**可从 config_name 确定性推导**。

### 2.4 数据库结构

```sql
-- steps 表：仅有 config_name, step_name, status, retry_count, error_message, updated_at
-- 没有任何字段存储远程任务元数据
CREATE TABLE steps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    config_name INTEGER NOT NULL,
    step_name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'Waiting',
    retry_count INTEGER NOT NULL DEFAULT 0,
    error_message TEXT DEFAULT '',
    updated_at REAL NOT NULL,
    UNIQUE(config_name, step_name)
);
```

---

## 三、各场景行为分析

### 3.1 场景 A：仅 SSH 断连，Daemon 仍在

**当前行为**：✅ 正常

- `RemoteWorkstation.ensure_connected()` 检测到断连后自动重连（paramiko `AutoAddPolicy`）
- `_remote_tasks` 在内存中保留，后续 `wait_meshing_completion` / `wait_solver_completion` 可继续轮询标志文件
- SSH 连接设 `keepalive=30` 秒，断连后指数退避重试

**风险**：无。如文档所述，此场景已妥善处理。

### 3.2 场景 B：Daemon 重启，Fluent 仍在远程运行 ⚠️ **核心 Bug**

**当前行为**：❌ **会导致重复启动**

以 Meshing 为例，追踪代码路径：

1. Daemon 启动 → `PipelineScheduler.start_pipeline()` → `_resume_paused_steps()`

2. `_resume_paused_steps()` 中对于 `meshing=Running` 的构型：
   ```python
   # main.py: _resume_paused_steps()
   if status == STATUS_RUNNING:
       if self._check_step_output_exists(cn, step, step_dir, scdoc_dir):
           self.state.set_step_status(cn, step, STATUS_COMPLETED)
           continue
       else:
           self.state.set_step_status(cn, step, STATUS_WAITING)  # ← BUG! 重置为 Waiting
           if step == "meshing":
               self.meshing_monitor.submit(cn)  # ← 重新入队
           break
   ```
   **注意**：`_check_step_output_exists` 对 meshing 检查的是标志文件或 `.msh` 文件。如果 Fluent 仍在运行且未产出这些文件，将返回 `False`。

3. MeshingMonitor 从队列取出构型 → `_process_single_meshing()`:
   ```python
   # meshing_monitor.py: _process_single_meshing()
   if meshing_status == STATUS_RUNNING:
       if self._remote_executor.check_meshing_done(config_name):
           # 标记完成
       else:
           self.state.set_step_status(config_name, "meshing", STATUS_WAITING)  # ← 重复重置
           # 接下来执行 start_meshing() → 再次启动远程 Fluent！
   ```

4. `_run_meshing_command()` → `ssh.exec_background()` → 创建**新的** Windows 计划任务

**结果**：远程工作站上同时运行两个 Fluent Meshing 实例。

**Solver 存在完全相同的问题**：
- `_resume_paused_steps()` 中 `solver=Running` 的处理逻辑相同
- `_next_solver_config()` 返回 `Waiting/Paused/Retrying` 的构型 → 重新启动 Solver

### 3.3 场景 C：计划任务存在，done/error flag 尚未生成

**当前行为**：❌ **无法检测，会被当作未启动处理**

- 标志文件不存在 → `check_meshing_done()` 返回 False
- `check_step_output_exists()` 返回 False
- 恢复逻辑重置为 `Waiting` → 重复启动

**根本原因**：没有任何代码路径会查询远程 `schtasks` 状态。

### 3.4 场景 D：输出文件已存在

**当前行为**：✅ 正常

- `_check_remote_outputs_exist()` 检查标志文件和 `.msh` 文件
- `_check_step_output_exists()` 同样检查这些文件
- 任一存在则标记 `Completed`，不重复启动

**注意**：此场景仅在 Fluent 已完成并写入标志/输出文件后才能被检测到。如果 Fluent 刚启动还没产出任何文件（场景 C），则无法检测。

### 3.5 场景 E：任务失败但错误 flag 或日志存在

**当前行为**：⚠️ 部分正确

- `wait_meshing_completion()` 轮询时检测到 `.error` 标志文件 → 清理并返回 False
- 但 `_resume_paused_steps()` 的处理顺序是先检查 `Running` 状态：
  - 如果 `meshing=Running`，先检查输出文件 → 不包含 `.error` 文件检查
  - 只有 `_check_step_output_exists` 中的 flag_file 检查可能间接捕获

**问题**：`_check_step_output_exists` 对 meshing 仅检查 `meshing_done_{config}.txt` 和 `.msh` 文件，**不检查 `.error` 文件**。因此错误场景中：
- 如果 `.error` 文件存在但 `.msh` 不存在 → 被判定为"输出不存在" → 重置为 Waiting → 重复启动

---

## 四、根因分析

### 4.1 核心缺陷：远程任务元数据未持久化

```
┌─────────────────────────────┐
│ RemoteExecutor._remote_tasks │ ← 仅内存，daemon 重启即丢失
│ {config_name: task_name}     │
└─────────────────────────────┘
         │
         │ Daemon 重启后
         ▼
┌─────────────────────────────┐
│      (空 dict)              │ ← 无法关联 config ↔ 远程任务
└─────────────────────────────┘
```

### 4.2 恢复逻辑的检测盲区

恢复时能检测的条件：
- ✅ 标志文件存在 → `Completed`
- ✅ 输出文件存在（`.msh`、`.cas.h5`、`.dat.h5`）→ `Completed`
- ❌ 远程计划任务正在运行但未产出任何文件 → **检测不到**
- ❌ 远程进程存在但未创建标志文件 → **检测不到**

### 4.3 不检查 `.error` 文件

`_check_step_output_exists()` 和 `_resume_paused_steps()` 中的输出检查不包含 `.error` 标志文件。

---

## 五、影响评估

| 影响项 | 严重程度 | 说明 |
|--------|---------|------|
| 重复启动 Fluent Meshing | 🔴 Critical | 两个实例同时操作同一工作目录，可能导致文件锁冲突、结果损坏 |
| 重复启动 Fluent Solver | 🔴 Critical | 同上 |
| 远程资源泄漏 | 🟠 High | 旧计划任务不会被终止，占用远程 CPU/内存 |
| 超时计时器重置 | 🟡 Medium | 重启后超时计时器从零开始，已运行时间不计入 |
| 重试计数混乱 | 🟡 Medium | 重置为 Waiting 可能触发不必要的重试 |

---

## 六、修复计划

### 6.1 方案概述

**核心思路**：持久化远程任务元数据 + 恢复时查询远程计划任务状态。

```
┌──────────────┐    启动任务     ┌───────────────────────┐
│ RemoteExecutor│───────────────→│ 远程 Windows 计划任务   │
│              │                │ AutoFluid_{hash}       │
│              │    写入元数据   └───────────────────────┘
│              │───────────────→┌───────────────────────┐
│              │                │ DB: remote_tasks 表     │
└──────────────┘                │ task_name, flag_file,   │
                                │ log_file, started_at... │
                                └───────────────────────┘
                                         │
                                         │ Daemon 重启后
                                         ▼
                                ┌───────────────────────┐
                                │ 恢复逻辑:               │
                                │ 1. 读取 remote_tasks    │
                                │ 2. schtasks 查询状态     │
                                │ 3. 检查 flag/output     │
                                │ 4. 决定: 继续/完成/重启  │
                                └───────────────────────┘
```

### 6.2 具体改动

#### 改动 1：新增 `remote_tasks` 数据库表

**文件**：`engine/state_manager.py`

```sql
CREATE TABLE IF NOT EXISTS remote_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    config_name INTEGER NOT NULL,
    step_name TEXT NOT NULL,           -- 'meshing' | 'solver'
    task_name TEXT NOT NULL,           -- Windows 计划任务名 (AutoFluid_*)
    flag_file TEXT NOT NULL,           -- 完成标志文件路径
    error_flag_file TEXT NOT NULL,     -- 错误标志文件路径
    log_file TEXT,                     -- 远程日志文件路径
    pid_file TEXT,                     -- 远程 PID 文件路径
    script_file TEXT,                  -- 远程 CMD 脚本路径
    started_at REAL NOT NULL,          -- 启动时间戳
    UNIQUE(config_name, step_name)
);
```

新增方法：
- `save_remote_task(config_name, step_name, task_name, flag_file, ...)` → INSERT/UPDATE
- `get_remote_task(config_name, step_name)` → 查询单条
- `get_all_remote_tasks()` → 查询所有（恢复时用）
- `delete_remote_task(config_name, step_name)` → 清理已完成任务
- `delete_remote_tasks_for_config(config_name)` → 重置构型时级联删除

#### 改动 2：RemoteExecutor 启动时写入元数据

**文件**：`executor/remote_executor.py`

在 `_run_meshing_command()` 和 `execute_solver()` 中，`exec_background()` 成功后调用 `StateManager.save_remote_task()` 持久化：

```python
# _run_meshing_command() 中，exec_background 成功后：
self.state.save_remote_task(
    config_name=config_name,
    step_name="meshing",
    task_name=task_name,
    flag_file=flag_file,
    error_flag_file=f"{flag_file}.error",
    log_file=f"{flag_dir}/autofluid_bg_{hash}.log",
    pid_file=f"{flag_dir}/autofluid_bg_{hash}.pid",
    script_file=f"{flag_dir}/autofluid_bg_{hash}.cmd",
    started_at=time.time(),
)
```

同样在 `_cleanup_completed_remote_task()` 和 `_kill_remote_task_for_config()` 中调用 `delete_remote_task()` 删除记录。

#### 改动 3：RemoteExecutor 新增远程任务状态查询方法

**文件**：`executor/remote_executor.py`

新增公共方法，供恢复逻辑调用：

```python
def query_remote_task_status(self, config_name: int, step_name: str) -> str:
    """查询远程任务的当前状态。

    逻辑优先级：
    1. 标志文件存在 → 'completed'
    2. 错误标志文件存在 → 'failed'
    3. Windows 计划任务仍存在 → 'running'
    4. 计划任务不存在 → 'lost'

    Returns:
        'completed' | 'failed' | 'running' | 'lost'
    """
```

查询流程：
1. 先通过 `ssh.check_remote_file(flag_file)` 检查完成标志
2. 检查 `.error` 文件
3. 通过 `schtasks /Query /TN "{task_name}" /FO CSV /NH` 查询计划任务是否存在
4. 如果计划任务存在，通过 `schtasks /Query /TN "{task_name}" /FO CSV /NH /V` 检查 `Status` 字段判断是否正在运行
5. 通过 PID 文件查询进程是否存活（`tasklist /PID {pid}`）

#### 改动 4：修复恢复逻辑

**文件**：`engine/scheduler/main.py` — `_resume_paused_steps()`

对于 `meshing=Running` 或 `solver=Running` 的构型，**不要直接重置为 Waiting**，而是：

```python
if status == STATUS_RUNNING and step in ("meshing", "solver"):
    remote_status = self._query_remote_task_recovery(cn, step)
    if remote_status == "completed":
        self.state.set_step_status(cn, step, STATUS_COMPLETED)
        continue
    elif remote_status == "failed":
        # 检查重试次数
        retry_count = self.state.get_step_retry_count(cn, step)
        if retry_count < max_retries:
            self.state.set_step_status(cn, step, STATUS_WAITING)
            # 入队重试
        else:
            self.state.set_step_status(cn, step, STATUS_ERROR)
        break
    elif remote_status == "running":
        # ★ 核心修复：保持 Running，不重置！
        # 启动轮询线程继续等待完成
        self._resume_remote_polling(cn, step)
        break
    elif remote_status == "lost":
        # 远程任务不存在且无产物，安全重置
        self.state.set_step_status(cn, step, STATUS_WAITING)
        # 入队重试
        break
```

新增辅助方法 `_query_remote_task_recovery()` 和 `_resume_remote_polling()`。

**文件**：`engine/scheduler/meshing_monitor.py` — `_process_single_meshing()`

同样修复 `meshing_status == STATUS_RUNNING` 分支，加入远程任务状态查询。

#### 改动 5：恢复时重建 `_remote_tasks`

**文件**：`executor/remote_executor.py`

新增方法：

```python
def restore_remote_tasks_from_db(self) -> None:
    """Daemon 重启后从数据库恢复 _remote_tasks 映射。"""
    for task_info in self.state.get_all_remote_tasks():
        config_name = task_info["config_name"]
        self._remote_tasks[config_name] = task_info["task_name"]
```

在 `PipelineDaemon.start()` 或 `PipelineScheduler.__init__()` 中调用。

#### 改动 6：修复 `.error` 文件检查

**文件**：`engine/scheduler/utils.py` — `check_step_output_exists()`

在 meshing 和 solver 检查中加入 `.error` 文件的检查：

```python
if step_name == "meshing":
    flag_file = f"{remote_config['flag_dir']}/meshing_done_{config_name}.txt"
    error_flag = f"{flag_file}.error"
    mesh_name = get_step_filename("meshing", config_name)
    mesh_file = f"{remote_config['msh_dir']}/{mesh_name}" if mesh_name else None
    try:
        if ssh.check_remote_file(flag_file):
            return True
        if ssh.check_remote_file(error_flag):
            return True  # 错误也视为"有输出"，避免重复启动
        return mesh_file is not None and ssh.check_remote_file(mesh_file)
    except Exception:
        return False
```

#### 改动 7：超时计时器恢复

**文件**：`executor/remote_executor.py`

在 `wait_meshing_completion()` 和 `wait_solver_completion()` 中，支持从 `started_at` 恢复超时计时器，避免重启后重新从零计算：

```python
def wait_meshing_completion(self, config_name, ...):
    # 查找持久化的启动时间
    task_info = self.state.get_remote_task(config_name, "meshing")
    if task_info and task_info.get("started_at"):
        elapsed = time.time() - task_info["started_at"]
        remaining_timeout = max(0, timeout - elapsed)
    else:
        remaining_timeout = timeout
```

### 6.3 改动文件清单

| 文件 | 改动类型 | 说明 |
|------|---------|------|
| `engine/state_manager.py` | 新增表 + 方法 | `remote_tasks` 表、CRUD 方法 |
| `executor/remote_executor.py` | 修改 + 新增 | 启动时持久化、恢复时查询、`restore_remote_tasks_from_db()` |
| `engine/scheduler/main.py` | 修改 | `_resume_paused_steps()` 加入远程任务状态查询 |
| `engine/scheduler/meshing_monitor.py` | 修改 | `_process_single_meshing()` 加入远程任务状态查询 |
| `engine/scheduler/utils.py` | 修改 | `check_step_output_exists()` 加入 `.error` 检查 |
| `tests/test_remote_task_recovery.py` | 新增 | 恢复场景测试 |

### 6.4 测试计划

#### 6.4.1 单元测试

新建 `tests/test_remote_task_recovery.py`：

| 测试用例 | 说明 |
|---------|------|
| `test_save_and_get_remote_task` | 验证 remote_tasks 表的 CRUD |
| `test_restore_remote_tasks_from_db` | 验证 Daemon 重启后 `_remote_tasks` 重建 |
| `test_recovery_running_task_keeps_running` | ★ 核心：DB=Running + 远程任务存在 + 无 flag → 保持 Running |
| `test_recovery_running_task_completed_flag` | DB=Running + flag 存在 → 标记 Completed |
| `test_recovery_running_task_lost` | DB=Running + 远程任务不存在 + 无 flag → 重置 Waiting |
| `test_recovery_running_task_error_flag` | DB=Running + .error 文件存在 → 标记 Error |
| `test_no_duplicate_meshing_launch` | ★ 核心：确认不会重复启动 Meshing |
| `test_no_duplicate_solver_launch` | ★ 核心：确认不会重复启动 Solver |
| `test_cleanup_deletes_remote_task_record` | 清理时同步删除 remote_tasks 记录 |
| `test_timeout_respects_persisted_start_time` | 超时计时器从持久化启动时间恢复 |
| `test_error_flag_detected_in_output_check` | `.error` 文件被 `check_step_output_exists` 检测 |

#### 6.4.2 集成测试

| 测试用例 | 说明 |
|---------|------|
| `test_daemon_restart_during_meshing` | 模拟 Daemon 重启场景：写入 DB=Running + remote_tasks 记录，重启 Scheduler，验证不会重复启动 |
| `test_daemon_restart_during_solver` | 同上，Solver 场景 |

#### 6.4.3 手动测试

| 步骤 | 预期 |
|------|------|
| 1. 启动 Meshing，确认远程计划任务创建 | `schtasks /Query /TN "AutoFluid_*"` 能看到任务 |
| 2. 在 Meshing 运行期间重启 Daemon | Daemon 日志显示"检测到远程任务仍在运行" |
| 3. 等待 Meshing 完成 | 状态正确更新为 Completed |
| 4. 确认无重复任务 | `schtasks /Query` 仅有一个 AutoFluid 任务 |

---

## 七、风险与注意事项

### 7.1 远程计划任务查询的可靠性

- `schtasks /Query` 在某些 Windows 版本下输出格式可能不同，建议使用 `/FO CSV` 格式并解析 CSV
- 如果远程 Windows 有 360 安全管家等安全软件，`schtasks` 命令可能被拦截（见下文 7.3）

### 7.2 向后兼容性

- 新增 `remote_tasks` 表使用 `CREATE TABLE IF NOT EXISTS`，不影响已有数据库
- Daemon 旧版本没有 `remote_tasks` 记录时，恢复逻辑会走 fallback 路径（检查标志文件 + 重置为 Waiting），与当前行为一致

### 7.3 安全软件注意事项

如果远程 Windows 工作站安装了 360 安全管家或其他安全软件：

- `schtasks /Create` 和 `schtasks /Run` 可能被拦截
- `taskkill` 命令可能被拦截
- 解决方案：将以下路径加入安全软件白名单：
  - `cmd.exe`
  - `schtasks.exe`
  - `taskkill.exe`
  - Conda 环境中的 `python.exe`
  - Fluent 可执行文件路径

### 7.4 数据库 schema 变更

- `remote_tasks` 表是新增表，不影响 `configs` 和 `steps` 表
- 不需要数据库迁移脚本（`CREATE TABLE IF NOT EXISTS` 自动处理）

---

## 八、附录：代码路径追踪

### 8.1 Meshing 启动路径

```
PipelineScheduler.start_pipeline()
  → _resume_paused_steps()
    → [meshing=Running] → 重置为 Waiting → meshing_monitor.submit()
  → MeshingMonitor.start_if_needed()
    → _monitor_loop()
      → _scan_db_for_pending()   ← 补充队列
      → _process_single_meshing()
        → [meshing=Running] → check_meshing_done() → False → 重置为 Waiting
        → start_meshing()
          → _run_meshing_command()
            → ssh.exec_background()
            → _remote_tasks[config] = task_name  ← 仅内存
```

### 8.2 Solver 启动路径

```
PipelineScheduler.start_pipeline()
  → _resume_paused_steps()
    → [solver=Running] → 重置为 Waiting
  → BarrierCoordinator.dispatch_solver_if_ready()
    → _dispatch_solver_tasks()
      → _solver_dispatch_loop()
        → _execute_solver_for_config()
          → RetryManager.execute_with_retry()
            → RemoteExecutor.execute_solver()
              → ssh.exec_background()
              → _remote_tasks[config] = task_name  ← 仅内存
```

### 8.3 关键代码位置

| 文件 | 行号（约） | 问题 |
|------|-----------|------|
| `executor/remote_executor.py` | `__init__` | `_remote_tasks` 仅内存，无持久化 |
| `executor/remote_executor.py` | `_run_meshing_command` | 启动成功后未持久化元数据 |
| `executor/remote_executor.py` | `execute_solver` | 同上 |
| `engine/scheduler/main.py` | `_resume_paused_steps` | `Running` 状态无条件重置为 `Waiting` |
| `engine/scheduler/meshing_monitor.py` | `_process_single_meshing` | `Running` 状态无条件重置为 `Waiting` |
| `engine/scheduler/utils.py` | `check_step_output_exists` | 不检查 `.error` 文件 |
