# AutoFluidSimulation 全链路冷启动及运行审查报告

> **审查日期**: 2026-06-24  
> **审查分支**: `codex/three-workstation-dynamic-settings`  
> **审查范围**: client → daemon start → worker start → check → start → 全流水线执行（SW→SC→Transfer→Meshing→Solver→PostProcess）→ 三工作站构型分配 → full quit  
> **审查目标**: 发现可能导致程序无法正确完成预定功能的阻塞级 bug

---

## 调查过程

### 已检查的资料

| 文件 | 发现 |
|------|------|
| `main.py` | 入口点，子进程管理、PID 文件、IPC 就绪探测 |
| `start_daemon.py` | Daemon 预检（已有实例检测）、日志初始化 |
| `start_client.py` | Rust TUI 启动器，环境变量注入 |
| `engine/daemon.py` (1-1350) | 核心守护进程完整生命周期：进程锁、TOML 加载、状态管理初始化、IPC 服务器、主循环、shutdown、所有 IPC handler |
| `engine/config.py` (1-600) | TOML/环境变量双层配置加载、WORKSTATIONS 初始化、ENGINE_CONFIG、IPC_CONFIG |
| `autofluid_config.toml` | WS-A (password), WS-B (key), WS-C (password, offline) 三工作站配置 |
| `engine/scheduler/main.py` (1-1500+) | DAG 调度器：start_pipeline、pause、resume、stop、reset、断点续传 _resume_paused_steps |
| `engine/scheduler/control.py` | PipelineControl 并发控制：pause_transition、resume_transition、external_start、finalize_external_start |
| `engine/scheduler/worker_pool.py` (1-620) | SC/Transfer 解耦工作线程池、workstation_slots claim 集成 |
| `engine/scheduler/workstation_slots.py` | 工作站槽位协调器：claim/release/seed_from_state |
| `engine/scheduler/barrier.py` (1-200) | 全局屏障：多工作站模式、_ready_workstations_for_solver |
| `engine/scheduler/meshing_monitor.py` (1-400) | Meshing 按工作站单槽串行执行、断点续传 |
| `engine/state_manager.py` (1-200) | SQLite WAL 模式状态持久化、remote_tasks 表迁移 |
| `engine/task_runner.py` (1-200) | SSH 连接池（按工作站）、SCProcessPool、SWExecutor/RemoteExecutor 协调 |
| `engine/local_worker.py` (1-200) | LocalWorker 注册/心跳/poll 循环 |
| `engine/local_worker_adapter.py` (1-130) | Daemon 侧 Worker 任务排队与等待 |
| `engine/sc_process_pool.py` (1-200) | SpaceClaim 常驻进程池、oneshot 降级 |
| `engine/file_monitor.py` (1-150) | STEP 文件大小稳定检测、文件名正则匹配 |
| `executor/sw_executor.py` (1-200) | SW COM 连接（三层降级）、逐构型 STEP 导出 |
| `executor/remote_executor.py` (1-200) | Transfer/Meshing/Solver 远程执行、按工作站路由 |
| `ipc/protocol.py` | JSON 行协议、命令常量定义 |
| `ipc/server.py` | TCP 服务器、HMAC 认证、命令处理器注册 |
| `autofluid-tui/src/main.rs` | Rust TUI 入口 |
| `autofluid-tui/src/daemon_mgr.rs` | Daemon 进程启动/停止、IPC 重连、server 模式隧道管理 |

---

## 根因分析

### 假设 1: `handle_start` 中 running 状态 + pause 标志 + 死线程的三重不一致导致用户无法恢复

**置信度：高**

- **证据链**：`engine/daemon.py` 第 625-670 行的 `handle_start` 方法按以下顺序检查：
  1. `engine_status == "running"` → 检查 `scheduler.is_paused`
  2. 若 `is_paused=True` 且 DB 中 `latest_status == "paused"` → 直接返回"流水线已暂停"
  3. `pipeline_alive` 的检查仅在 `is_paused=False` 时才执行（第 640 行）
- **触发条件**：SW 宏执行期间 crash → `_handle_scheduler_thread_exception` 调用 `_control.stop()` 设置 `_stopped` 并 `_paused.clear()` → 但如果在 crash 前恰好收到 pause 命令，`_paused` 已被 `pause_transition()` 设置 → crash 后 `_control.stop()` 清除了 `_paused` 但 DB 状态已在 `pause()` 中写为 "paused" → 此时 `pipeline_alive=False`, `is_paused=False`, `engine_status="paused"` 或 "running"
- **代码引用**：
  - `engine/daemon.py:625-670` — `handle_start` 状态机分支
  - `engine/scheduler/main.py:515` — `_handle_scheduler_thread_exception` 调用 `_control.stop()`
  - `engine/scheduler/control.py:59-62` — `stop()` 清除 `_paused` 标志

**具体场景**：当 `engine_status="running"` 且 `scheduler.is_paused=True` 且 `scheduler.pipeline_alive=False` 时：
- 代码命中 `if self.scheduler.is_paused:` 分支（第 635 行）
- 检查 `latest_status = self.state.get_engine_status()` 可能是 `"paused"`（DB 已由 `pause()` 同步）
- 返回 `"流水线已暂停，使用 start 可在状态稳定后恢复"` — **但线程已死，resume 不会生效**
- 用户再次 start 仍进入同一条路径，形成死循环

---

### 假设 2: `handle_worker_start` 中 TOML 重载后 `WorkstationSlotCoordinator` 未重新初始化

**置信度：高**

- **证据链**：
  - `handle_worker_start` (`engine/daemon.py:1149`) 调用 `reload_config_from_toml()` 更新 `WORKSTATIONS` 全局列表
  - `PipelineScheduler.__init__` (`engine/scheduler/main.py:115-122`) 在初始化时根据 `WORKSTATIONS` 创建 `WorkstationSlotCoordinator`
  - `WorkstationSlotCoordinator` 持有 `self._workstation_ids` 列表，之后不再更新
  - 如果 TOML 中新增/删除了工作站（如 WS-C 刚上线），slot coordinator 仍使用旧的 ID 列表
- **代码引用**：
  - `engine/daemon.py:1149-1190` — `handle_worker_start`
  - `engine/scheduler/main.py:115-122` — slot coordinator 初始化
  - `engine/scheduler/workstation_slots.py:33-35` — `_workstation_ids` 不可变

**影响**：新工作站无法获得 Transfer→Meshing 槽位分配，构型无法路由到新工作站。

---

### 假设 3: `handle_worker_stop` 未停止 daemon 自动唤起的 LocalWorker 子进程

**置信度：中**

- **证据链**：
  - `handle_worker_stop` (`engine/daemon.py:1311-1345`) 断开 SSH、清理 Registry，但**不调用 `_stop_local_worker_process()`**
  - `_stop_local_worker_process()` 仅在 `shutdown()` (第 374 行) 中调用
  - 如果用户执行 `worker_stop` 再 `worker_start`，旧的 LocalWorker 子进程继续运行并周期性尝试重新注册
- **代码引用**：
  - `engine/daemon.py:1311-1345` — `handle_worker_stop`
  - `engine/daemon.py:407-420` — `_stop_local_worker_process`
  - `engine/daemon.py:374` — `shutdown()` 中调用

**影响**：`worker_stop` → `worker_start` 序列可能产生僵尸 LocalWorker 进程，旧进程与新进程竞争注册。

---

### 假设 4: `WorkstationSlotCoordinator.claim()` 返回 None 后 Transfer worker 仅等待 1 秒就重试，可能造成 CPU 空转

**置信度：中**

- **证据链**：
  - `worker_pool.py:551-562`：当所有槽位忙时，`pause_aware_sleep(1.0, ...)` 等待 1 秒后返回 True，`_process_transfer_step` 返回 True，导致 Transfer worker 循环立即重新取队列中的同一构型
  - 如果三个工作站槽位全满且 Transfer 队列有多个构型，Transfer worker 将每 1 秒尝试 claim 一次
  - `MeshingMonitor` 中有类似的退避逻辑（`_BUSY_SLOT_REQUEUE_SLEEP_SECONDS` 递增到 5 秒），但 Transfer 端缺少
- **代码引用**：
  - `engine/scheduler/worker_pool.py:551-562` — Transfer claim 失败等待
  - `engine/scheduler/meshing_monitor.py:38-39` — MeshingMonitor 的退避常数

**影响**：高负载场景下 Transfer worker 空转，日志噪音大但不会阻塞功能。

---

### 假设 5: `finalize_pipeline` 与 `shutdown()` 存在双重 `_control.stop()` 调用

**置信度：低**

- **证据链**：
  - `finalize_pipeline` (`main.py:535`) 在流水线自然终止时调用 `self._control.stop()` 和 `self.state.set_engine_status("stopped")`
  - `PipelineDaemon.shutdown()` (`daemon.py:378`) 调用 `self.scheduler.stop()` → 再次调用 `_control.stop()` 和 `set_all_running_to_paused()` + `set_engine_status("stopped")`
  - `_control.stop()` 是幂等的（只设置 `_stopped` 事件），但 `set_all_running_to_paused()` 在 `finalize_pipeline` 中未调用
- **代码引用**：
  - `engine/scheduler/main.py:535-560` — `finalize_pipeline`
  - `engine/scheduler/main.py:1040-1090` — `stop()`

**影响**：低风险。`set_all_running_to_paused()` 和 `set_engine_status("stopped")` 都是幂等的，双重调用不产生副作用。

---

### 假设 6: `_resume_paused_steps` 中 server 模式跳过本地文件验证

**置信度：低**

- **证据链**：
  - `main.py:1195-1197`：`_completed_step_output_exists` 对 SW/SC 步骤在 server 模式下直接返回 `True`
  - 这意味着 server 模式不会验证本地 SW/SC 输出文件是否存在
  - 如果 LocalWorker 上报 SW/SC Completed 但实际文件未写入磁盘，daemon 不会检测到
- **代码引用**：
  - `engine/scheduler/main.py:1191-1197`

**影响**：server 模式下依赖 LocalWorker 的正确上报，本地不验证。这是一个设计选择而非 bug。

---

## 修复方案

### 方案 A: 修复 `handle_start` running+paused+dead-thread 死锁 ⭐⭐⭐ 推荐

- **描述**：在 `handle_start` 的 `engine_status == "running"` 分支中，`is_paused` 检查后补充 `pipeline_alive` 检查。若暂停标志置位但线程已死，执行线程重建而非返回"已在暂停状态"。
- **涉及文件**：
  - `engine/daemon.py:635-645` — 在 `if self.scheduler.is_paused:` 块内添加 `elif not self.scheduler.pipeline_alive:` 检查
- **改动量**：< 20 行
- **复杂度**：简单
- **副作用风险**：
  - 低风险：线程重建路径已在现有代码中验证（`handle_start` 第 640-650 行已有类似逻辑）
  - 需确保重建线程前清除 `_paused` 事件
- **需要同步修改的测试**：查看 `test_daemon_server_mode.py` / `test_ipc_server_integration.py` 中是否有 start 状态机测试

---

### 方案 B: 修复 `handle_worker_start` 后 slot coordinator 不更新 ⭐⭐⭐ 推荐

- **描述**：在 `handle_worker_start` 重载 TOML 后，若 `WORKSTATIONS` 有变化，通过 scheduler 接口重建 `WorkstationSlotCoordinator` 或更新其工作站 ID 列表。
- **涉及文件**：
  - `engine/daemon.py:1149-1190` — 在 `handle_worker_start` 中添加 slot coordinator 更新
  - `engine/scheduler/workstation_slots.py` — 添加 `update_workstation_ids()` 方法
  - `engine/scheduler/main.py` — 添加公开接口转发
- **改动量**：20-50 行
- **复杂度**：中等（需确保 slot 状态在更新时不丢失）
- **副作用风险**：
  - 中等：若正在执行的构型占用了即将被移除的工作站槽位，需决定如何处理（建议：标记该工作站为 draining，等待当前任务完成后移除）
- **需要同步修改的测试**：新增 `test_workstation_slots_reload`

---

### 方案 C: 补充 `handle_worker_stop` 的 LocalWorker 进程清理 ⭐⭐ 可行

- **描述**：在 `handle_worker_stop` 末尾调用 `_stop_local_worker_process()` 确保自动唤起的 worker 子进程被终止。
- **涉及文件**：
  - `engine/daemon.py:1311-1345` — 在 return 前添加 `self._stop_local_worker_process()`
- **改动量**：< 5 行
- **复杂度**：简单
- **副作用风险**：
  - 低：`_stop_local_worker_process()` 已有完善的异常处理
  - 如果在 `worker_stop` 后立即需要 worker（如 `worker_restart`），autostart 机制会自动唤起新的 worker
- **需要同步修改的测试**：无（现有测试不覆盖 worker_stop 场景）

---

### 方案 D: Transfer worker 槽位等待增加退避机制 ⭐ 可行

- **描述**：在 `_process_transfer_step` 中 `claim()` 返回 None 时，使用递增退避时间（如 `min(5.0, 1.0 * retry_count)`）替代固定 1 秒等待。
- **涉及文件**：
  - `engine/scheduler/worker_pool.py:551-562`
- **改动量**：< 10 行
- **复杂度**：简单
- **副作用风险**：
  - 低：仅影响等待间隔，不影响正确性
- **需要同步修改的测试**：无

---

## 建议下一步

1. **优先修复方案 A + B**：这两个是真正可能在运行时导致阻塞的问题
2. **方案 C** 随方案 B 一起修复（改动量极小）
3. **方案 D** 作为性能优化，可在后续迭代中处理
4. **验证步骤**：
   - 模拟 SW crash + pause 竞态场景，确认方案 A 修复后用户可通过 `start` 恢复
   - 在运行中修改 TOML 添加新工作站，执行 `worker_start` 后 `start`，确认构型能分配到新工作站
5. **回归测试**：运行 `pytest tests/ -k "daemon or scheduler or ipc"` 确认无回归

---

## 审查总结

| 严重级别 | 数量 | 关键问题 |
|---------|------|---------|
| 🔴 高 | 2 | handle_start 死锁、slot coordinator 不更新 |
| 🟡 中 | 2 | worker_stop 残留进程、Transfer 无退避 |
| 🟢 低 | 2 | 双重 stop 调用、server 模式跳过验证 |

**总体评估**：项目冷启动链路的整体架构设计良好，状态机、并发控制、断点续传机制均经过仔细设计。发现的 2 个高置信度问题属于边界条件场景（pause 竞态 + 死线程、TOML 热重载），在正常操作流程中不易触发，但在异常恢复场景下可能造成阻塞。
