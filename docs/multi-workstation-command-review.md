# 多工作站架构指令审查报告

> 审查日期：2026-06-24  
> 审查范围：reset、clean、pause、start、check、status 指令在多工作站架构下的正确性  
> 分支：`codex/three-workstation-dynamic-settings`

---

## 问题概述

- **现象**：对 AutoFluidSimulation 多工作站架构下的六条核心 IPC 指令进行系统性审查，评估其正确性、潜在故障及设计缺陷。
- **影响范围**：IPC 命令处理层（`engine/daemon.py`）、调度器（`engine/scheduler/main.py`）、状态管理（`engine/state_manager.py`）、清理模块（`executor/cleaner.py`）、并发控制（`engine/scheduler/control.py`）
- **问题域**：状态管理 / 并发控制 / 架构设计

---

## 调查过程

### 已检查的资料

| 文件 | 发现内容 |
|------|----------|
| `engine/daemon.py` | IPC 命令处理器：handle_start / pause / stop / check / reset_step / clean_step / get_all_status / get_engine_status / get_statistics / get_dashboard / reload_config / worker_start / worker_stop / worker_restart |
| `engine/scheduler/main.py` | 调度器核心：start_pipeline、pause、resume、stop、reset_config、_resume_paused_steps、finalize_pipeline |
| `engine/scheduler/control.py` | 并发控制原语：PipelineControl 的 pause / resume / stop / external_start 锁语义 |
| `engine/scheduler/workstation_slots.py` | 工作站槽位分配器：claim / release / clear |
| `engine/scheduler/barrier.py` | 全局屏障协调器：多工作站屏障逻辑 |
| `engine/scheduler/utils.py` | PauseGuard、pause_aware_sleep、check_step_output_exists |
| `engine/state_manager.py` | SQLite 持久化层：reset_config_steps、reset_all、get_all_remote_tasks |
| `executor/cleaner.py` | 文件清理：clean_step_files、clean_local_step_files、clean_all_cache |
| `engine/config.py` | 配置加载：WORKSTATIONS、reload_config_from_toml、validate_config |
| `ipc/protocol.py` | IPC 协议常量定义 |
| `ipc/server.py` | IPC 命令路由与认证 |
| `engine/local_worker_registry.py` | LocalWorker 注册表：clear_pending_tasks、clear_online_workers |
| `tests/test_pause_start.py` | 暂停/启动功能测试 |
| `tests/test_autofluid_cli.py` | CLI 命令发送测试 |
| `tools/autofluid_cli.py` | CLI 入口参数解析 |

### 关键发现

- **发现 1**：暂停/启动之间存在 TOCTOU 竞态窗口 —— `pause()` 先设置 `_paused` 标志再更新 `engine_status`，两者之间存在间隙被 `handle_start` 利用。
- **发现 2**：`worker_stop` 清理不彻底 —— 内存中 registry 的 pending tasks 被清除，但 SQLite 中的 `remote_tasks` 表记录未被清除。
- **发现 3**：`clean` 命令不释放工作站槽位 —— 与 `reset` 语义不一致。
- **发现 4**：断点续传扫描遗漏 `transfer` 步骤的输出文件完整性校验。
- **发现 5**：`external_start()` 上下文管理器存在 TOCTOU —— 释放锁后的状态写入可被并发 pause 覆盖。

---

## 根因分析

### 假设 1: pause() 与 handle_start 之间竞态条件导致暂停被恢复

**置信度：高**

- **证据链**：`PipelineScheduler.pause()` → `PipelineControl.pause()` 设置 `_paused`（持有 `_transition_lock`）→ 释放锁 → 后续 `state.set_engine_status("paused")` 在锁外执行。`PipelineDaemon.handle_start()` 先读 `engine_status`（此时为 "running"），再读 `is_paused`（已被 pause 置位），然后调用 `resume()`。pause() 的 `set_engine_status("paused")` 在 resume 执行后才生效 —— 结果：调度器正在运行但引擎状态显示 "paused"。
- **代码引用**：
  - `engine/scheduler/main.py:492-505` — `pause()` 方法
  - `engine/scheduler/control.py:40-45` — `pause_transition()`
  - `engine/daemon.py:510-530` — `handle_start()` 中 running 分支
- **时序**：
  ```
  T1 (pause):  lock → set _paused → unlock
  T2 (start):  read engine_status = "running"
  T1 (pause):  set_engine_status("paused")  ← 在锁外！
  T2 (start):  read is_paused = True → call resume() → 流水线继续运行
  结果: engine_status = "paused"，但调度器实际在运行
  ```

### 假设 2: worker_stop 后残留远程任务记录

**置信度：高**

- **证据链**：`handle_worker_stop()` 调用 `local_worker_registry.clear_pending_tasks()` 仅清除内存中 `_pending_task_ids`，不涉及 SQLite。同时 `handle_worker_stop` 调用 `self.runner.disconnect_ssh()` 断开连接，但 StateManager 中的 `remote_tasks` 表数据完整保留。当下次 `get_all_remote_tasks()` 被调用时（例如通过 `_validate_mutation_safe`），会返回已不存在的工作站上的任务记录。
- **代码引用**：
  - `engine/daemon.py:1272-1298` — `handle_worker_stop()`
  - `engine/local_worker_registry.py:135-145` — `clear_pending_tasks()`：仅操作内存 `self._pending_task_ids`
  - `engine/state_manager.py:576-610` — `get_all_remote_tasks()`：读取 SQLite

### 假设 3: clean 命令不清除工作站槽位导致状态不一致

**置信度：中**

- **证据链**：`handle_clean_step()` 仅调用 `runner.clean_step_files()` 删除文件 → 不涉及 `workstation_slots.release_config()`。而 `handle_reset_step()` 调用 `scheduler.reset_config()` → 内部调用 `workstation_slots.release_config()` 和 `workstation_slots.clear()`。clean 后工作站槽位仍标记为忙碌，影响后续构型分配。
- **代码引用**：
  - `engine/daemon.py:1556-1655` — `handle_clean_step()`
  - `engine/scheduler/main.py:1100-1190` — `reset_config()` 中 `workstation_slots` 操作
  - `engine/scheduler/workstation_slots.py:41-61` — `claim()` / `release()`
- **影响**：如果用户单独执行 clean（不跟 reset），文件被删除但槽位仍被占用，新构型无法分配到该工作站。

### 假设 4: 断点续传扫描不校验 transfer 步骤输出文件

**置信度：高**

- **证据链**：`_resume_paused_steps()` 中用于校验已完成步骤输出文件存在性的步骤集合为 `{"sw", "sc", "meshing", "solver", "postprocess"}`，明确遗漏 `"transfer"`。
- **代码引用**：`engine/scheduler/main.py:557-560`
  ```python
  if (
      step in {"sw", "sc", "meshing", "solver", "postprocess"}
      and self._completed_step_output_exists(...) is False
  ):
  ```
- **影响**：如果 transfer 步骤的状态为 Completed 但远程 SCDOC 文件被误删，断点续传不会检测到此不一致，后续 meshing 会因找不到输入文件而失败。

### 假设 5: external_start() 上下文管理器存在 TOCTOU

**置信度：高**

- **证据链**：`PipelineControl.external_start()` 仅在持有锁时读取 `_paused` 和 `_stopped`，然后 `yield` 释放锁。调用方在 yield 之后的操作（如 `set_engine_status("running")`）不在锁保护范围内。如果 pause 命令在 yield 之后、set_engine_status 之前到达，引擎状态会被错误地写回 "running"。
- **代码引用**：
  - `engine/scheduler/control.py:62-70` — `external_start()`
  - `engine/scheduler/main.py:395-412` — `start_pipeline` 中 `can_finalize` 后的 `set_engine_status`
  - `engine/scheduler/main.py:1004-1008` — `resume` 中 `can_finalize` 后的 `set_engine_status`
- **注**：注释说明这是有意的设计权衡（"Long operations must not block pause() acknowledgement"），但 `set_engine_status("running")` 是快速操作，可以安全地放入锁内。

---

## 修复方案

### 方案 A: 修复 pause/start TOCTOU 竞态 ⭐⭐⭐ 推荐

- **描述**：在 `PipelineScheduler.pause()` 中将 `set_engine_status("paused")` 移入 `pause_transition()` 锁内；或在 `handle_start` 的 running 分支中检查 `scheduler.pipeline_alive` 而非仅依赖 `is_paused`。
- **涉及文件**：
  - `engine/scheduler/main.py:492-505` — 将 `self.state.set_engine_status("paused")` 移入 `with self._control.pause_transition():` 块内
  - `engine/scheduler/control.py:41-46` — 可选：修改 `pause_transition` 为接受状态更新回调
- **改动量**：< 20 行
- **复杂度**：简单
- **副作用风险**：低。`set_engine_status` 是快速 SQLite 写操作，不会阻塞 IPC。不需要修改测试。
- **需要同步修改的测试**：无

### 方案 B: worker_stop 后清理数据库 remote_tasks ⭐⭐⭐ 推荐

- **描述**：在 `handle_worker_stop()` 中增加数据库清理逻辑，删除所有工作站的远程任务记录。
- **涉及文件**：
  - `engine/daemon.py:1280` — 在 `clear_pending_tasks()` 之后添加数据库清理
  - `engine/state_manager.py` — 新增 `delete_all_remote_tasks()` 方法
- **改动量**：~20 行
- **复杂度**：简单
- **副作用风险**：极低。worker_stop 的语义就是"断开所有远程连接"，清理对应的任务记录是合理行为。
- **需要同步修改的测试**：`tests/test_autofluid_cli.py` 中 worker restart 测试可能需要更新

### 方案 C: 在 clean 流程中增加工作站槽位释放建议 ⭐⭐ 可行

- **描述**：在 `handle_clean_step()` 的日志中增加提示，引导用户在 clean 远程步骤后执行 reset 以释放槽位。或在 clean 相关步骤（meshing/solver/postprocess）时输出 warning 日志。
- **涉及文件**：
  - `engine/daemon.py:1600-1655` — 增加日志提示
- **改动量**：< 10 行
- **复杂度**：简单
- **副作用风险**：无
- **推荐度**：⭐⭐ 可行（更彻底的修复是在 clean 中调用 `workstation_slots.release_config()`，但需评估 clean 的语义边界）

### 方案 D: 修复 _resume_paused_steps 遗漏 transfer 步骤 ⭐⭐⭐ 推荐

- **描述**：将 `"transfer"` 加入已完成步骤输出文件检查集合。
- **涉及文件**：
  - `engine/scheduler/main.py:559` — 将 `{"sw", "sc", "meshing", "solver", "postprocess"}` 改为 `{"sw", "sc", "transfer", "meshing", "solver", "postprocess"}`
- **改动量**：1 行
- **复杂度**：简单
- **副作用风险**：无。transfer 步骤检查逻辑已存在于 `check_step_output_exists()` 中。
- **需要同步修改的测试**：无

### 方案 E: 缩小 external_start TOCTOU 窗口 ⭐⭐ 可行

- **描述**：将 `start_pipeline` 和 `resume` 中 `can_finalize` 之后的 `set_engine_status("running")` 操作移入 `external_start` 上下文管理器内。
- **涉及文件**：
  - `engine/scheduler/main.py:395-412` — start_pipeline 末尾
  - `engine/scheduler/main.py:1004-1008` — resume 末尾
- **改动量**：~5 行
- **复杂度**：简单
- **副作用风险**：低。`set_engine_status` 已在锁外执行，移入锁内不会增加明显的阻塞时间。
- **需要同步修改的测试**：可能需要更新 `test_pause_start.py`

---

## 其他值得关注的设计问题

### 1. StateManager._reset_single_config 中 "meshing" → "transfer" 的隐式映射

- **位置**：`engine/state_manager.py:753`
- **代码**：`effective_from_step = "transfer" if from_step == "meshing" else from_step`
- **说明**：意图正确（meshing 依赖 transfer 产出，重置 meshing 应同时清理 transfer），但隐式映射容易引起误解。建议增加注释说明原因。

### 2. handle_worker_restart 缺少引擎状态校验

- **位置**：`engine/daemon.py:1300-1307`
- **说明**：restart 直接调用 stop → start，未检查流水线是否正在运行。如果流水线运行中执行 worker restart，可能导致正在执行的远程任务异常中断。

### 3. get_statistics 在 server 模式下部分字段无意义

- **位置**：`engine/daemon.py:1333-1343`
- **说明**：`sw_macro_started` 和 `barrier_passed` 在 server 模式下由 LocalWorker 控制，直接返回可能产生误导。建议增加 server_mode 标记。

### 4. clean_step 对 "transfer" 步骤有特殊处理但 clean_step_files 未区分

- **位置**：`executor/cleaner.py:653-690`
- **说明**：`clean_step_files` 对 transfer 步骤的处理未在 `_clean_single_step` 中体现。当前实现依赖 `_clean_remote_single_step` → 查找 flag 文件，但 transfer 步骤没有 flag 文件（只有 SCDOC 文件）。建议验证 transfer 清理的完整性。

---

## 建议执行顺序

1. **优先修复**：方案 A（pause/start 竞态）+ 方案 D（transfer 遗漏）—— 改动量极小、风险为零、解决明确 bug。
2. **其次修复**：方案 B（remote_tasks 残留）+ 方案 E（external_start TOCTOU）—— 中等优先级，解决数据一致性问题。
3. **后续评估**：方案 C（clean 槽位提示）—— 需确认 clean 语义是否需要包含槽位释放。
4. **验证步骤**：
   ```bash
   # 运行核心测试确认无回归
   .venv\Scripts\python.exe -m pytest tests/test_pause_start.py tests/test_autofluid_cli.py tests/test_cleaner_utils.py tests/test_workstation_slots.py -v
   # 代码质量检查
   .venv\Scripts\python.exe -m ruff check .
   .venv\Scripts\python.exe -m mypy .
   ```
