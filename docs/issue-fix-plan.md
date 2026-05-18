# AutoFluid Issues 综合修复改进计划

> 涵盖 Issue #23, #27, #32, #33, #34, #35, #36, #37, #38  
> 文档版本：v1.0  
> 创建日期：2026-05-18  
> 适用项目：液氧甲烷火箭发动机仿真流水线系统 (AutoFluid)

---

## 一、概述

本计划针对 AutoFluid 项目在 GitHub 上提出的 9 个 Issues 进行根因分析并制定修复方案。
这些问题涵盖：远程网格划分执行、暂停/恢复逻辑、远程文件清理、任务队列阻塞、SC进程管理、SW文件名识别、清理时机、并发控制、以及配置可维护性。

按优先级分为三档：
- **P0（紧急）**：#27 pause/start逻辑、#33 transfer阻塞、#37 meshing并发
- **P1（重要）**：#23 meshing无效、#32 clean失效、#36 清理时机、#34 SC进程复用
- **P2（改进）**：#35 SW文件名、#38 settings页面

---

## 二、逐Issue分析与方案

### Issue #23: fluent meshing步骤无效

**问题描述**
Fluent Meshing 步骤启动后未实际执行。Python 版本已被检测到并向工作站输出了指令，但 Meshing 未启动。

**根因分析**
- 远程网格划分通过 `executor/remote_executor.py` 的 `execute_meshing()` 执行
- 命令构建：`"{conda_exe}" run -n {conda_env} python "{meshing_script}" {config_name}`
- `ssh.exec_background()` 使用 PowerShell `Start-Process` 在远程后台执行
- 可能原因：
  1. Conda 环境激活失败：`conda run` 在非交互式 SSH 会话中可能不正确初始化 PATH
  2. 远程 `batch_meshing_gen4.py` 脚本内部异常但无日志输出
  3. 标志文件目录 `flags/` 权限或路径问题
  4. `exec_background` 的 PowerShell 启动命令在远程 conda 环境下有兼容性问题

**修复方案**
1. **增强远程命令诊断**（`executor/remote_executor.py`）：
   - 在 `execute_meshing()` 中添加命令执行前的环境验证：先执行 `conda run -n {env} python --version` 并检查返回值
   - 将远程命令的 stderr/stdout 重定向到日志文件，便于事后诊断
2. **改进 `exec_background` 实现**（`utils/ssh_client.py`）：
   - 使用 `conda activate` 替代 `conda run`，或使用 conda 的完整 Python 路径
   - 在 PowerShell Start-Process 中添加 `-RedirectStandardError` 和 `-RedirectStandardOutput` 参数
3. **添加 meshing 脚本自检**：在远程 `batch_meshing_gen4.py` 开头添加版本和环境打印
4. **配置项新增**：将 conda activate 方式作为可配置项（与 #38 联动）

**涉及文件**
- `executor/remote_executor.py` — `execute_meshing()` 方法
- `utils/ssh_client.py` — `exec_background()` 方法
- `autofluid_config.toml` — 新增 `conda_activate_mode` 配置项

---

### Issue #27: pause/start逻辑问题

**问题描述**
三个子问题：
1. pause 再 start 后，3 个 Paused 状态中只有 2 个优先重新 Running，第三个启动了新任务
2. SW 步骤在 pause 状态下手动关闭 SW 进程，程序检测到意外关闭后越过 pause 进入 retry
3. pause 状态下会主动杀死 SW 进程（不必要）

**根因分析**
- `resume()` 流程（`engine/scheduler/main.py`）：
  1. `_resume_paused_steps()`：处理 SW 和 SC 的 PAUSED 状态
  2. `set_all_paused_to_running(exclude_steps=["SW","SC"])`：恢复 Transfer/Meshing
- 问题 1 的根因：`_resume_paused_steps()` 对 SC 的处理逻辑中，部分 SC PAUSED 构型被重新入队，但入队后 worker 线程在 `_execute_with_retry` 中将其状态设为 Running。如果 3 个 worker 中有 2 个正在处理之前的 SC，第三个 worker 拿到新入队的任务后标记为 Running，但原始的第三个 PAUSED 状态的构型可能被 `set_all_paused_to_running` 覆盖为 Running 后又被新任务覆盖。
- 问题 2 的根因：`_resume_paused_steps()` 中检查 STEP 文件是否存在来决定 SW 步骤状态。如果 SW 进程被手动关闭，STEP 文件不存在，SW 步骤被标记为 Error，然后 `needs_recurse=True` 触发重新执行 SW，绕过了 pause 状态。
- 问题 3 的根因：`pause()` 调用 `set_all_running_to_paused()`，该方法将所有 Running 状态改为 Paused。但对于 SW 步骤，由于 SW 是通过 COM 驱动的长进程，pause 时并没有实际暂停 SW 进程，只是改了状态。后续的文件监控或 SC 池的暂停处理可能误杀了 SW 进程。

**修复方案**
1. **修复 resume 状态一致性**（`engine/scheduler/main.py` — `_resume_paused_steps()`）：
   - SC 恢复逻辑改为：先检查输出文件 → 存在则 Completed；不存在则保持 PAUSED 并标记需入队，入队后由 worker 统一处理状态转换
   - 避免 `set_all_paused_to_running` 与 `_resume_paused_steps` 的状态覆盖冲突
2. **SW pause 不主动杀死进程**（`engine/scheduler/main.py` — `pause()`）：
   - `pause()` 不应触发 SW 进程终止。SW 的 COM 调用是同步阻塞的，pause 只需标记状态，等当前构型导出完成后自然暂停
   - `_paused` 事件的检查点已在 `_rebuild_and_export_per_config()` 逐构型循环中实现
3. **SW 意外关闭不绕过 pause**（`engine/scheduler/sw_phase.py` — `_handle_sw_breakpoint_resume()`）：
   - 在 `_handle_sw_breakpoint_resume()` 中，对 SW PAUSED 状态的处理应先检查引擎是否处于 paused 状态
   - 若引擎 paused，即使 STEP 文件缺失也不标记为 Error，而是保持 PAUSED，等用户显式 resume 后再决定
4. **添加 pause 状态下的 SW 进程保护**：
   - 在 `SCProcessPool._terminate_bridge_and_sc()` 中，检查暂停事件是否置位，暂停时不主动 kill SC 进程（已在实现中），确认 SW 侧无类似逻辑

**涉及文件**
- `engine/scheduler/main.py` — `pause()`, `resume()`, `_resume_paused_steps()`
- `engine/scheduler/sw_phase.py` — `_handle_sw_breakpoint_resume()`
- `engine/state_manager.py` — `set_all_running_to_paused()` 确认逻辑

---

### Issue #32: clean操作在工作站上失效

**问题描述**
已知 scdoc 文件未被工作站有效清除。

**根因分析**
- `executor/cleaner.py` 的 `clean_step_files()` 方法通过 SSH 执行远程文件删除
- 远程清理逻辑：`ssh.delete_remote_file(remote_path)`
- 可能原因：
  1. SSH 连接在 clean 时已断开但未重连
  2. `delete_remote_file()` 内部使用 SFTP 的 `remove()` 方法，路径格式问题（Windows 反斜杠 vs Linux 正斜杠）
  3. 远程文件被占用（例如 SpaceClaim 进程正在访问 scdoc 文件）
  4. 权限不足
  5. `_clean_single_step` 中 `remote_patterns` 的 `SC` 条目为 `None`（Transfer 之前的 SC 文件是本地的），但 clean 操作期望清理的是远程 scdoc 文件。实际上 SC 的 scdoc 文件是通过 Transfer 上传到远程的，清理远程 scdoc 应在 Transfer 或 Meshing 的 clean 逻辑中。

**修复方案**
1. **添加 clean 操作的详细日志**（`executor/cleaner.py`）：
   - 在 `_clean_single_step` 中添加 SSH 连接状态检查
   - 记录每个文件删除操作的成功/失败结果
2. **添加 SC 远程清理支持**（`executor/cleaner.py`）：
   - `remote_patterns` 中 `"SC"` 条目应支持清理远程 scdoc 目录：`("scdoc_dir", STEP_FILE_PATTERNS["SC"], None)`
   - 同时清理本地和远程的 scdoc 文件
3. **添加 clean 操作的错误聚合**：
   - 收集所有删除失败的文件，汇总报告给用户
4. **SSH 连接自动重连**（`utils/ssh_client.py`）：
   - 在 `delete_remote_file` 中添加连接断开时的自动重连逻辑

**涉及文件**
- `executor/cleaner.py` — `_clean_single_step()`, `remote_patterns`
- `utils/ssh_client.py` — `delete_remote_file()` 增强

---

### Issue #33: 构型未能正确进入transfer序列

**问题描述**
SC 和 Transfer 处理队列遇到阻塞：处理前 3 个进入 SC 序列的构型后停滞，一段时间后处理第 4 个，然后再次停滞。Transfer 在处理第 1 个后停滞，与 SC 第 4 个构型相近的时间点处理第 2、3 个。

**根因分析**
- 工作线程池有 3 个 worker（`_num_workers = 3`），每个 worker 从 `_sc_queue` 取任务后执行 SC → Transfer → Meshing 全流程
- **关键发现**：SC 步骤使用 `SCProcessPool`，其 `MAX_SLOTS = 3`。当 3 个 worker 同时尝试执行 SC 时，前 3 个各获取一个槽位，第 4 个必须等待槽位释放。但 SC 的槽位释放是在 `_execute_in_slot` 完成后（`finally: self.release(slot_id)`），而 `_execute_in_slot` 包含了进程启动和超时轮询。
- **阻塞机制**：3 个 worker 同时处理 3 个构型的 SC 步骤（各占 1 个 SC 槽位）。当某个 worker 的 SC 完成后，该 worker 继续执行 Transfer 和 Meshing。如果 Transfer 和 Meshing 也阻塞（例如 SSH 传输慢或远程 meshing 超时），该 worker 线程会长时间占用，无法回来取新任务。
- **队列阻塞的时序**：3 个构型进入 SC（3 worker 各处理 1 个）→ SC 槽位满 → 第 4 个构型在 `acquire()` 中等待 → 前 3 个中某一个 SC 完成释放槽位 → 第 4 个获取槽位 → 但此时前 3 个中的完成者已进入 Transfer，如果 Transfer 使用 SSH 锁（`_ssh_lock`），多个 worker 竞争 SSH 锁导致串行化

**修复方案**
1. **SC 和 Transfer/Meshing 解耦**（`engine/scheduler/worker_pool.py`）：
   - 将 worker 线程分为两类：SC 专用 worker（受 SCProcessPool MAX_SLOTS 限制）和 Transfer/Meshing worker
   - 或者使用信号量控制 SC 并发数，但 Transfer/Meshing 不受 SC 槽位限制
2. **添加 Transfer/Meshing 独立队列**：
   - SC 完成后将构型推入 Transfer 队列，由独立的 Transfer worker 消费
   - 避免单个 worker 线程阻塞在 Transfer/Meshing 时无法处理新 SC 任务
3. **SSH 锁粒度优化**（`utils/ssh_client.py`）：
   - 当前 `_ssh_lock` 是全局锁，多个 worker 的 Transfer 操作串行化
   - 考虑使用连接池或 per-operation 锁
4. **添加队列深度监控和告警**：
   - 当 SC 队列深度持续增长且无 worker 处理时，输出警告日志

**涉及文件**
- `engine/scheduler/worker_pool.py` — 重构 worker 架构
- `engine/scheduler/main.py` — 队列创建与管理
- `utils/ssh_client.py` — SSH 锁粒度优化

---

### Issue #34: SC执行步骤优化（进程复用）

**问题描述**
当前 SC（SpaceClaim）步骤在每个构型执行完后关闭进程，下次执行需重新启动。期望改为进程复用模式。

**根因分析**
- `SCProcessPool._execute_in_slot()` 中：
  - 每次 `run_config()` → `acquire()` → `_execute_in_slot()` → `release()`
  - `_execute_in_slot()` 启动 `SpaceClaimBridge.exe` 进程，等待完成后释放槽位
  - Bridge 进程启动 SpaceClaim，SpaceClaim 执行完后退出（`AUTOFLUID_SC_NOEXIT=1` 环境变量暗示曾尝试保持 SC 不退出）
- 当前环境变量 `AUTOFLUID_SC_NOEXIT=1` 已传递给 Bridge，但 Bridge 仍然每次启动新进程

**修复方案**
1. **SpaceClaim 进程池化**（`engine/sc_process_pool.py`）：
   - 将 SC 进程池改为持久化进程模式：首次启动 MAX_SLOTS 个 SpaceClaim 实例，保持运行
   - 每个槽位对应一个持久的 SpaceClaim 进程，通过 IPC 或 COM 发送转换命令
   - 需要修改 `SpaceClaimBridge` 的通信方式：从"启动-执行-退出"改为"等待命令-执行-返回结果-等待下一命令"
2. **Bridge 协议改造**（`bridge/SpaceClaimBridge/Program.cs`）：
   - Bridge 进程改为常驻模式：启动后监听 stdin/pipe/socket 等待命令
   - 每收到一个 config_name，执行转换，输出结果标志，继续等待
   - 添加 `quit` 命令用于正常退出
3. **SCProcessPool 改造**（`engine/sc_process_pool.py`）：
   - `_execute_in_slot()` 改为：向已有 Bridge 进程发送 config_name，等待完成标志
   - `acquire()` 改为：仅在无空闲持久进程时启动新进程
   - 添加进程健康检查：定期轮询进程存活状态
4. **配置项**（`autofluid_config.toml`）：
   - 新增 `sc_process_mode = "pooled"` | `"per_config"` 切换模式
   - 新增 `sc_persistent_idle_timeout = 300` 空闲超时自动退出

**涉及文件**
- `engine/sc_process_pool.py` — 进程池化改造
- `bridge/SpaceClaimBridge/Program.cs` — Bridge 常驻模式
- `executor/spaceclaim_transit.py` — SC 脚本适配
- `autofluid_config.toml` — 新增配置项
- `engine/config.py` — 读取新配置

---

### Issue #35: solidworks无法识别原模型文件名

**问题描述**
SolidWorks 无法识别原模型文件名。需要确定是否与路径相关。

**根因分析**
- `executor/sw_executor.py` 的 `_open_sw_model()` 使用 `OpenDoc6` 打开模型
- 模型路径来自 `LOCAL_PATHS["sw_model"]`
- `OpenDoc6` 返回错误码时记录警告但继续执行
- 可能原因：
  1. 模型文件路径包含空格或特殊字符
  2. SW 的 `OpenDoc6` 对文件路径的编码处理（中文路径问题）
  3. SW 版本与文件格式不兼容
  4. 文件被其他进程锁定

**修复方案**
1. **添加详细错误诊断**（`executor/sw_executor.py`）：
   - `OpenDoc6` 返回非零错误码时，查询 `sw_app.GetErrorInfo()` 获取详细错误信息
   - 记录完整的文件路径、文件大小、修改时间
2. **路径规范化**：
   - 在调用 `OpenDoc6` 前，将路径转为绝对路径并规范化（`os.path.realpath()`）
   - 确保路径使用 Windows 分隔符
3. **文件名编码测试**：
   - 添加诊断函数：尝试使用短路径名（`win32api.GetShortPathName`）打开
   - 测试将模型复制到纯 ASCII 路径下再打开
4. **OpenDoc6 错误码查表**：
   - 实现 SolidWorks `swFileLoadError_e` 枚举的完整错误码映射
   - 根据不同错误码给出具体建议

**涉及文件**
- `executor/sw_executor.py` — `_open_sw_model()` 增强错误处理
- `engine/config.py` — 路径规范化

---

### Issue #36: SC全量清理时机过于超前

**问题描述**
SC 全量清理预期在有第一个构型进入 SC 步骤时触发，实际在 start 后将所有模型 SW 步骤标记为 running 后就立刻触发了。

**根因分析**
- `do_first_cleanup()` 在两个位置被调用：
  1. `SWPhaseHandler._execute_sw_macro()` 中（SW 宏执行前）
  2. `PipelineScheduler.start_pipeline()` 步骤 2 中（SW 宏执行后、文件监控启动前）
- `_execute_sw_macro()` 是在 SW 步骤开始时调用的，此时还没有构型进入 SC 步骤
- `_first_cleanup_done` 标志保证幂等，但首次调用时机过早
- `start_pipeline()` 的步骤 2 在 SW 宏执行完成后立即调用 `do_first_cleanup()`，此时只是 STEP 文件已生成，还没有构型真正进入 SC 步骤

**修复方案**
1. **延迟首次清理到第一个 SC 任务实际执行时**（`engine/sc_process_pool.py`）：
   - 将 `do_first_cleanup()` 的调用从 `start_pipeline()` 和 `_execute_sw_macro()` 中移除
   - 改为在 `SCProcessPool.run_config()` 内部、首次 `acquire()` 时触发：
     ```python
     def run_config(self, config_name, ...):
         with self._lock:
             if not self._first_cleanup_done:
                 self.shutdown_all()
                 self._first_cleanup_done = True
         # 继续正常的 acquire → execute → release 流程
     ```
2. **或在 worker_pool 中 SC 阶段开始前触发**（`engine/scheduler/worker_pool.py`）：
   - 在 `_process_single_config()` 的 SC 阶段开始前，检查是否为首个进入 SC 的构型
   - 使用状态数据库查询：是否有其他构型的 SC 状态为 Running/Completed
   - 仅当当前构型是第一个进入 SC 的构型时触发清理

**涉及文件**
- `engine/sc_process_pool.py` — `run_config()` 或 `acquire()` 中触发
- `engine/scheduler/main.py` — 移除过早的 `do_first_cleanup()` 调用
- `engine/scheduler/sw_phase.py` — 移除过早的 `do_first_cleanup()` 调用

---

### Issue #37: 网格划分步骤未确保单线程

**问题描述**
网格划分应当单线程运行，但出现两个构型同时被标记为 Meshing Running。

**根因分析**
- `WorkerPoolManager` 有 3 个 worker 线程，每个 worker 执行完整的 SC → Transfer → Meshing 流程
- `_process_single_config()` 中 Meshing 阶段调用 `execute_meshing()` 启动远程网格划分，然后调用 `wait_meshing_completion()` 轮询等待
- **问题**：3 个 worker 可以同时进入 Meshing 阶段。Worker A 启动构型 1 的 meshing 后进入轮询等待；Worker B 此时完成 SC 和 Transfer，也启动构型 2 的 meshing
- 远程工作站的 `batch_meshing_gen4.py` 脚本可能不支持并行执行（Fluent Meshing 通常需要独占 GPU/CPU 资源）

**修复方案**
1. **添加 Meshing 全局互斥锁**（`engine/scheduler/worker_pool.py`）：
   - 在 `WorkerPoolManager` 中添加 `threading.Lock()` 作为 meshing 互斥量
   - 在 `_process_single_config()` 的 Meshing 阶段前获取锁，完成后释放
   - 确保同一时刻只有一个构型在执行网格划分
   ```python
   # WorkerPoolManager.__init__ 中
   self._meshing_lock = threading.Lock()
   
   # _process_single_config 的 Meshing 阶段
   with self._meshing_lock:
       if not self._execute_with_retry(...):
           return
       # wait_meshing_completion 也需在锁内
   ```
2. **或使用信号量控制 Meshing 并发数**：
   - 从配置读取 `max_concurrent_meshing = 1`（默认单线程）
   - 使用 `threading.Semaphore(max_concurrent_meshing)` 实现
   - 便于未来扩展到多工作站场景（#38 联动）
3. **远程脚本层面的锁**（`batch_meshing_gen4.py`）：
   - 在远程 meshing 脚本中添加文件锁，防止多个实例同时运行
   - 作为防御性措施，与本地互斥锁配合

**涉及文件**
- `engine/scheduler/worker_pool.py` — 添加 meshing 互斥锁
- `autofluid_config.toml` — 新增 `max_concurrent_meshing` 配置
- `engine/config.py` — 读取新配置

---

### Issue #38: 更新settings页面

**问题描述**
SW 和 SC 步骤有一些硬编码参数，需要更新 settings 页面使其可配置。

**根因分析**
当前 TUI settings 页面已覆盖 `EngineConfig` 和 `OperationTimeouts` 两个配置组，但以下参数仍硬编码在 Python 代码中：

**SW 相关硬编码参数**：
1. `SWExecutor._SW_OPEN_SILENT = 1` — OpenDoc6 选项
2. `SWExecutor._SW_OPEN_READONLY = 2` — OpenDoc6 选项  
3. `SWExecutor._SW_RUN_MACRO_DEFAULT = 0` — RunMacro2 选项
4. `SWExecutor._SW_RUN_MACRO_UNLOAD_AFTER = 1` — RunMacro2 选项
5. worker_pool `_num_workers = 3` — 工作线程数

**SC 相关硬编码参数**：
6. `SCProcessPool.MAX_SLOTS = 3` — SC 最大并发槽位数
7. `SCProcessPool._first_cleanup_done` — 首次清理标志（行为参数）
8. Bridge 环境变量 `AUTOFLUID_SC_NOEXIT` — 是否保持 SC 不退出

**其他硬编码**：
9. `RetryManager` 中重试间隔 `5 * attempt` 秒
10. `StepFileMonitor` 的 `stable_time=2.0` 文件稳定检测时间
11. `BarrierCoordinator` 的轮询间隔 `5.0` 秒

**修复方案**
1. **提取硬编码参数到 `autofluid_config.toml`**：
   ```toml
   [engine_config]
   num_workers = 3              # SC/Transfer/Meshing 工作线程数
   max_sc_slots = 3             # SC 最大并发槽位数
   max_concurrent_meshing = 1   # Meshing 最大并发数
   retry_base_interval = 5      # 重试基础间隔（秒）
   file_stable_time = 2.0       # 文件稳定检测时间（秒）
   barrier_poll_interval = 5.0  # 屏障轮询间隔（秒）
   ```
2. **更新 Python 配置读取**（`engine/config.py`）：
   - 在 `ENGINE_CONFIG` 字典中添加新字段及其默认值
   - 确保向后兼容：缺失的配置项使用默认值
3. **更新 TUI Settings 页面**（Rust 端）：
   - `autofluid-tui/src/settings/mod.rs`：
     - `EngineConfig` 结构体添加新字段
     - `SettingCategory::EngineConfig` 的 `field_count` 从 12 增加到 18
     - 添加对应的 `field_name`、`display_label` 条目
   - `autofluid-tui/src/settings/validation.rs`：添加新字段的验证规则
   - `autofluid-tui/src/settings/config_io.rs`：确保序列化/反序列化兼容
4. **代码中引用配置**（各模块）：
   - `WorkerPoolManager.__init__`：`self._num_workers = ENGINE_CONFIG.get("num_workers", 3)`
   - `SCProcessPool.__init__`：`self.MAX_SLOTS = ENGINE_CONFIG.get("max_sc_slots", 3)`
   - `RetryManager`：重试间隔从配置读取
   - `StepFileMonitor`：stable_time 从配置读取

**涉及文件**
- `autofluid_config.toml` — 新增配置项
- `engine/config.py` — ENGINE_CONFIG 扩展
- `autofluid-tui/src/settings/mod.rs` — EngineConfig 结构体扩展、字段映射
- `autofluid-tui/src/settings/validation.rs` — 新字段验证
- `autofluid-tui/src/settings/config_io.rs` — 序列化兼容
- `autofluid-tui/src/settings/settings_ui.rs` — UI 渲染（自动扩展）
- `engine/scheduler/worker_pool.py` — 引用配置
- `engine/sc_process_pool.py` — 引用配置
- `engine/scheduler/retry.py` — 引用配置
- `engine/file_monitor.py` — 引用配置
- `engine/scheduler/barrier.py` — 引用配置

---

## 三、实施优先级与依赖关系

### 阶段一：P0 紧急修复（预计 2-3 天）

| 序号 | Issue | 任务 | 依赖 |
|------|-------|------|------|
| 1.1 | #37 | 添加 Meshing 全局互斥锁 | 无 |
| 1.2 | #27 | 修复 pause/resume 状态一致性 | 无 |
| 1.3 | #27 | SW pause 不主动杀死进程 | 1.2 |
| 1.4 | #33 | SC/Transfer 解耦（添加独立 Transfer 队列） | 无 |

### 阶段二：P1 重要修复（预计 2-3 天）

| 序号 | Issue | 任务 | 依赖 |
|------|-------|------|------|
| 2.1 | #36 | 延迟 SC 首次清理到第一个 SC 任务执行时 | 无 |
| 2.2 | #32 | 远程 clean 操作增强 | 无 |
| 2.3 | #23 | 远程 meshing 命令诊断增强 | 无 |
| 2.4 | #34 | SC 进程复用（Bridge 常驻模式） | 独立，可与 2.1 并行 |

### 阶段三：P2 改进项（预计 1-2 天）

| 序号 | Issue | 任务 | 依赖 |
|------|-------|------|------|
| 3.1 | #38 | 提取硬编码参数到配置文件 | 无 |
| 3.2 | #38 | 更新 TUI Settings 页面 | 3.1 |
| 3.3 | #35 | SW OpenDoc6 诊断增强 | 无 |

---

## 四、验证计划

### 自动化验证
1. 现有单元测试全部通过：`python -m pytest tests/`
2. ruff linting 无新增告警
3. mypy 类型检查无新增错误
4. Rust 端 `cargo check` 和 `cargo clippy` 通过

### 手动验证
1. **#27**：start → pause → start 循环，确认 3 个 Paused 构型全部正确恢复
2. **#37**：启动多构型任务，确认同一时刻只有 1 个构型处于 Meshing Running
3. **#33**：处理 10+ 构型，确认 Transfer 队列无阻塞
4. **#36**：start 后确认 SC 全量清理在第一个构型实际进入 SC 时才执行
5. **#32**：执行 clean all，确认远程 scdoc 文件被删除
6. **#23**：执行完整 meshing 流程，确认远程网格划分正常启动
7. **#34**：（可选）测试 SC 进程复用模式
8. **#35**：测试不同路径下的 SW 模型打开
9. **#38**：在 TUI Settings 页面验证新配置项可读写

---

## 五、风险与注意事项

1. **#33 SC/Transfer 解耦**是架构级改动，影响面最大，需要充分测试断点续传、暂停恢复等场景
2. **#34 SC 进程复用**需要同步修改 C# Bridge 程序，需确保 SpaceClaim API 支持多文档顺序处理
3. **#27 pause/start 修复**涉及多个模块的状态同步，需仔细处理竞态条件
4. **#38 配置扩展**需要保持向后兼容，缺失的配置项必须有合理默认值
5. 所有改动需遵循 `docs/code-style-guide.md` 和 `.github/instructions/implementation-planning.instructions.md` 规范
