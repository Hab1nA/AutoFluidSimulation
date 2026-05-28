# AutoFluid 系统性测试部署计划

> 生成日期：2026-05-27
> 最后更新：2026-05-28
> 状态：✅ 全部完成

## 实施状态

| 模块 | 状态 | 测试文件 | 用例数 |
|------|------|----------|--------|
| M1 配置系统 | ✅ 完成 | `test_config.py` | 69 |
| M2 StateManager | ✅ 完成 | `test_state_manager.py` | 65 |
| M3 文件监控 | ✅ 完成 | `test_file_monitor.py` | 36 |
| M4 调度器子模块 | ✅ 完成 | `test_scheduler_modules.py` | 31 |
| M5 IPC 服务器集成 | ✅ 完成 | `test_ipc_server_integration.py` | 15 |
| M6 SC 进程池 | ✅ 完成 | `test_sc_process_pool.py` | 15 |
| M7 远程执行器 | ✅ 完成 | `test_remote_executor_full.py` | 12 |
| M8 Cleaner+工具 | ✅ 完成 | `test_cleaner_utils.py` | 15 |
| M9 端到端 | ✅ 完成 | `test_pipeline_e2e.py` | 8 |
| **合计** | | **9 个新测试文件** | **262 新增** |

全量回归: 423 passed / 425 collected (2 个预先存在的失败)，ruff 0 errors

## 项目技术概要

AutoFluid 是三层架构的自动流体仿真流水线系统：

```
Python Daemon  ←→  IPC (TCP :9527)  ←→  TUI (Rust)
     ↕
  执行器层 (Python)
     ↕
  SpaceClaim Bridge (C#)
```

### 核心模块清单

| 模块 | 文件 | 关键职责 |
|------|------|----------|
| 配置系统 | `engine/config.py`, `config_fingerprint.py` | TypedDict 配置、环境变量覆盖、指纹分片 |
| 状态管理 | `engine/state_manager.py` | SQLite WAL，3 表（configs/steps/engine_state） |
| 调度器 | `engine/scheduler/main.py` | DAG 调度入口，暂停/恢复/停止控制 |
| 重试 | `engine/scheduler/retry.py` | `RetryManager`，暂停感知 sleep |
| 屏障 | `engine/scheduler/barrier.py` | Meshing 全部完成后解锁 Solver |
| 工作线程池 | `engine/scheduler/worker_pool.py` | SC/Transfer 解耦双队列 |
| SW 阶段 | `engine/scheduler/sw_phase.py` | SW 宏启动、断点续传、递归恢复 |
| Meshing 监控 | `engine/scheduler/meshing_monitor.py` | 串行 Meshing、远程标志文件轮询 |
| 文件监控 | `engine/file_monitor.py` | watchdog + 文件大小稳定检测 |
| SC 进程池 | `engine/sc_process_pool.py` | 常驻 Bridge 槽位、文件协议 IPC |
| SW 执行器 | `executor/sw_executor.py` | COM 自动化（三层降级连接） |
| 远程执行器 | `executor/remote_executor.py` | Transfer/Meshing/Solver |
| 清理器 | `executor/cleaner.py` | 系统自检、文件清理 |
| IPC | `ipc/protocol.py`, `server.py` | JSON-over-TCP 11 条命令 |
| 工具 | `utils/logger.py`, `excel_reader.py`, `ssh_client.py`, `process_utils.py` | 日志广播、Excel 读取、SSH、PID 管理 |

---

## 现有测试覆盖分析

| 测试文件 | 覆盖模块 | 测试数量 | 评估 |
|----------|----------|----------|------|
| `test_ipc_protocol.py` | `ipc/protocol.py` | 7 | ✅ 覆盖完整 |
| `test_ipc_server.py` | `ipc/server.py` (process_message) | 4 | ⚠️ 仅覆盖消息分发，缺连接/并发 |
| `test_pause_start.py` | Scheduler 状态机 | 12 | ✅ 核心场景覆盖好 |
| `test_scheduler_modules.py` | retry, pause_aware_sleep | 13 | ⚠️ 缺 barrier/worker_pool |
| `test_detail_log.py` | `utils/logger.py` | 30+ | ✅ 覆盖全面 |
| `test_sw_export_workflow.py` | `executor/sw_executor.py` | ~15 | ✅ COM Mock 覆盖好 |
| `test_sw_executor_cleanup.py` | SW cleanup 路径 | 3 | ⚠️ 只覆盖异常清理 |
| `test_sw_step_naming.py` | 命名/配置 | 4 | ✅ 基本覆盖 |
| `test_remote_executor.py` | Transfer | 4 | ⚠️ 缺 Meshing/Solver |
| `test_ssh_client.py` | `utils/ssh_client.py` | 3 | ⚠️ 只覆盖后台执行 |
| `test_sc_ipc_runid.py` | SC 文件协议 | 7 | ✅ run_id + mtime 过滤 |

---

## 完整测试模块规划

### 模块 1：`engine/config.py` + `config_fingerprint.py` — 配置系统测试

**优先级：🔴 高** | 新文件：`tests/test_config.py`

| 测试场景 | 验证点 |
|----------|--------|
| `LOCAL_PATHS` 默认值完整性 | 所有必需键存在且非空 |
| 环境变量覆盖 `_env_override` | 设置→生效、空字符串→回退、空白→回退 |
| `validate_config()` 正常路径 | 所有路径存在时通过 |
| `validate_config()` 缺失路径 | 缺失路径进入警告列表 |
| `ensure_directories()` 创建缺失目录 | 自动创建 step_dir/scdoc_dir 等 |
| `compute_config_fingerprint()` 确定性 | 相同输入→相同 MD5 |
| `compute_config_fingerprint()` 灵敏度 | 修改任一参数→指纹变化 |
| `get_db_path_for_fingerprint()` 路径格式 | 包含指纹前缀 |
| `reload_config_from_toml()` 正常加载 | TOML 值覆盖 Python 默认值 |
| `reload_config_from_toml()` 缺失文件 | 保持现有配置不变 |
| `get_step_filename()` 全步骤 | SW/SC/Transfer/Meshing/Solver 各自文件名格式 |
| STEP_FILE_PATTERNS 正则匹配 | 正确文件名→匹配、错误格式→不匹配 |

### 模块 2：`engine/state_manager.py` — 状态管理器测试

**优先级：🔴 高** | 新文件：`tests/test_state_manager.py`

| 测试场景 | 验证点 |
|----------|--------|
| 初始化建表 | configs/steps/engine_state 表存在 |
| `load_configs()` 正常载入 | configs 表行数正确 |
| `load_configs()` 增量更新 | 新增构型不影响已有状态 |
| `get/set_step_status()` CRUD | 写入后读回一致 |
| 状态转换验证 | Waiting→Running→Completed 合法 |
| 错误消息持久化 | set_error 后 get_error_message 正确 |
| `increment_retry_count()` | 计数递增 |
| `get_all_configs()` 返回排序列表 | 有序且完整 |
| `is_global_barrier_met()` | 读写一致 |
| `set/reset_engine_status()` | engine_state 表更新 |
| `get_step_statistics()` 聚合 | 各状态计数正确 |
| **并发读写安全** | 多线程同时读/写不抛异常 |
| **WAL 模式** | PRAGMA journal_mode=WAL 生效 |
| `reset_step()` 级联清理 | 步骤状态回到 Waiting，下游步骤也被重置 |
| `clear_all_steps()` | 所有步骤回到 Waiting |
| `load_configs()` 空字典 | 不崩溃 |

### 模块 3：`engine/file_monitor.py` — 文件监控测试

**优先级：🔴 高** | 新文件：`tests/test_file_monitor.py`

| 测试场景 | 验证点 |
|----------|--------|
| `FileStableDetector.is_file_ready()` 文件稳定 | 大小不变 stable_time 后返回 True |
| `FileStableDetector` 文件仍在写入 | 大小持续增长→返回 False |
| `FileStableDetector` 文件不存在 | 返回 False |
| `FileStableDetector` 文件从 0 增长到稳定 | 最终返回 True |
| `StepFileMonitor.parse_config_name()` | 正确文件名→构型号、无关文件→None |
| `StepFileMonitor` 大小写不敏感 | .STEP/.step 均可 |
| `StepFileMonitor._on_step_file_ready` 回调 | 文件就绪时回调被调用 |
| `StepFileMonitor` 暂停时不触发回调 | paused_event 置位→不回调 |
| **内存泄漏防护** | _history 被正确清理 |

### 模块 4：`engine/scheduler/` — 调度器子模块测试

**优先级：🟡 中高** | 扩展现有 `tests/test_scheduler_modules.py`

#### 4a. BarrierCoordinator

| 测试场景 | 验证点 |
|----------|--------|
| 全部 Meshing Completed → 屏障通过 | `_barrier_passed` 被 set |
| 存在 Error 且无 Waiting/Running → 屏障失败 | 日志报告错误 |
| 存在 Paused → 屏障等待 | 不提前通过 |
| `join_solver_threads()` | 线程正确退出 |
| 暂停/停止事件响应 | 监控循环正确退出 |

#### 4b. WorkerPoolManager

| 测试场景 | 验证点 |
|----------|--------|
| `start_if_needed()` 创建线程 | SC/Transfer 线程启动 |
| 重复调用不创建重复线程 | 幂等性 |
| `join_worker_threads()` | 线程正确退出 |
| SC 队列→Transfer 队列流动 | 构型正确传递 |
| SC 全部完成触发清理 | `_sc_cleanup_triggered` 标志 |
| 暂停响应 | 工作线程暂停 |

#### 4c. SWPhaseHandler

| 测试场景 | 验证点 |
|----------|--------|
| 初次执行 SW 宏 | 调用 TaskRunner |
| 断点续传：部分构型 Completed | 跳过已完成 |
| 断点续传：SW Error → needs_recurse | 递归标志设置 |
| sw_macro_started 自愈 | 全部 Completed 但标志 false→自动修复 |
| 暂停响应 | SW 阶段暂停 |

#### 4d. MeshingMonitor

| 测试场景 | 验证点 |
|----------|--------|
| `submit()` 入队 | 队列深度增加 |
| 串行处理 | 同一时刻只有 1 个 in_flight |
| 远程标志文件轮询 | done 标志→Completed |
| 超时处理 | 超时→Error |
| 断点续传扫描 DB | 补充队列 |
| 暂停/停止响应 | 监控循环正确退出 |

### 模块 5：`ipc/` — IPC 服务器集成测试

**优先级：🟡 中高** | 新文件：`tests/test_ipc_server_integration.py`

| 测试场景 | 验证点 |
|----------|--------|
| 启动/停止服务器生命周期 | 端口绑定/释放 |
| 连接数限制 | 超限拒绝 |
| 完整命令→响应往返 | start/pause/stop/get_all_status 等 |
| 异常处理器 | handler 抛异常→error 响应 |
| 多客户端并发连接 | 不崩溃 |
| 超时断开 | 客户端无活动→断开 |
| 大消息处理 | 接近缓冲区上限 |

### 模块 6：`engine/sc_process_pool.py` — SC 进程池测试

**优先级：🟡 中** | 新文件：`tests/test_sc_process_pool.py`

| 测试场景 | 验证点 |
|----------|--------|
| `_get_or_create_persistent_slot()` 新建槽位 | slot_id 递增 |
| MAX_SLOTS 上限 | 满时返回 None |
| 文件协议命令写入 | JSON 格式正确 |
| 就绪文件检测 | `sc_ready_{id}.json` 解析 |
| 结果文件读取 | `sc_result_{id}_{runid}.json` 解析 |
| `_shutdown_all_internal()` | 所有槽位清理 |
| `do_first_cleanup()` 幂等 | 多次调用安全 |
| run_id 隔离 | 不同 run 的结果文件不混淆 |
| 暂停/停止期间行为 | 等待响应控制事件 |

### 模块 7：`executor/remote_executor.py` — 远程执行器完整测试

**优先级：🟡 中** | 扩展现有 `tests/test_remote_executor.py`

| 测试场景 | 验证点 |
|----------|--------|
| `execute_meshing()` 正常流程 | SSH 命令发出、标志文件轮询 |
| `execute_meshing()` 超时 | 超时→Error |
| `execute_solver()` 正常流程 | SSH 命令发出 |
| `execute_solver()` 超时 | 超时→Error |
| 远程脚本同步 `_sync_remote_scripts` | 目录变更检测、旧文件清理 |
| 同步状态持久化 `_load/save_last_sync_paths` | JSON 读写正确 |
| Transfer 暂停不标记 Error | 中断上传不误标 |

### 模块 8：`executor/cleaner.py` + `utils/` — 工具模块测试

**优先级：🟢 中低** | 新文件：`tests/test_cleaner.py`, 扩展 `tests/test_utils.py`

| 测试场景 | 验证点 |
|----------|--------|
| `FileCleaner.run_system_check()` 本地检查 | 路径存在/不存在 |
| `FileCleaner.clean_step_files()` 本地清理 | 文件删除 |
| `FileCleaner.clean_step_files()` 远程清理 | SSH 命令发出 |
| `read_model_configs()` 正常 Excel | 读取构型参数 |
| `read_model_configs()` 空行终止 | 遇到空行停止 |
| `read_model_configs()` 无效数据跳过 | 警告日志、不崩溃 |
| `read_model_configs()` 文件不存在 | FileNotFoundError |
| `is_process_alive()` 当前进程 | 返回 True |
| `read/write/remove_pid_file()` | CRUD 一致 |
| `run_taskkill()` | 正常/无效 PID |
| `check_ipc_ready()` | 端口可达/不可达 |

### 模块 9：端到端集成测试

**优先级：🟢 低（长期目标）** | 新文件：`tests/test_pipeline_e2e.py`

| 测试场景 | 验证点 |
|----------|--------|
| 完整流水线 Mock（SW→SC→Transfer→Meshing→Solver） | 所有步骤 Completed |
| 中间步骤失败→重试→成功 | RetryManager 生效 |
| 全局暂停→恢复→继续 | 状态机完整流转 |
| 全局停止→重启→断点续传 | 已完成步骤跳过 |
| Excel 读取→StateManager 初始化→调度 | 配置到执行的完整链路 |
| 配置重载 | 运行时 reload 不崩溃 |

---

## 实施优先级与依赖关系

```
M1（配置系统）─┬──→ M4（调度器子模块）──→ M9（端到端）
M2（StateManager）┼──→ M7（远程执行器）──→ M9
M3（文件监控）────┘
M5（IPC 服务器集成）──────────────────────→ M9
M6（SC 进程池）────────────────────────────→ M9
M8（工具模块）──→ 独立，无下游依赖
```

### 建议实施顺序

1. **第一批（基础层）**：M1 + M2 + M3 — 配置、状态、文件监控，无外部依赖
2. **第二批（核心调度）**：M4 + M5 — 调度器子模块 + IPC 集成
3. **第三批（执行层）**：M6 + M7 + M8 — SC 池、远程执行、工具
4. **第四批（端到端）**：M9 — 完整流水线集成

---

## 预期效果

| 指标 | 当前 | 目标 |
|------|------|------|
| 测试文件数 | 11 | 17+ |
| 模块覆盖 | ~40% | ~90% |
| 核心路径覆盖 | IPC/Scheduler 重试/SW Export | +StateManager/Config/FileMonitor/Barrier/WorkerPool/MeshingMonitor/SCPool |
| 并发测试 | 仅 pause_start 12 个场景 | +StateManager 并发/IPC 多客户端/线程池 |
| 外部依赖 | 需要 pytest | 不变，Mock 所有 COM/SSH/SpaceClaim |
