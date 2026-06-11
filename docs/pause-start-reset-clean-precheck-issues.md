# Pause/Start + Reset/Clean 预检查问题记录

本报告基于 pause/start、reset/clean 操作组合测试清单，对当前代码做集中预检查。
2026-06-11 已按阶段完成修复，并补充回归测试覆盖下列边界状态。

## 修复进度

- [x] P1 reset 后 solver 终态报告 gate 已在 reset 触及 solver 时清理。
- [x] P1 远程任务状态 unknown 已增加重试计数与上限后 Error 暴露，避免长期静默 Running。
- [x] P1 pause 后 reset 与旧运行步骤完成竞态已通过按构型/步骤 reset generation 防护。
- [x] P2 clean 后 start/resume 对本地 SW/SC Completed 产物缺失进行复核并重置。
- [x] P2 engine_status=running 但主调度线程已死亡时，start 会重启调度线程。
- [x] P2 resume 与 stop 组件启动/清理窗口已增加 stopped 边界检查。
- [x] P3 stop 后 STEP 文件回调会检查 stopped，且 stop 先停止文件监控再清 SC 队列。

新增关键回归测试：

- `test_reset_solver_clears_terminal_report_gate`
- `test_resume_scan_unknown_remote_solver_surfaces_after_retry_budget`
- `test_stale_sc_completion_after_reset_does_not_submit_transfer`
- `test_stale_transfer_completion_after_reset_does_not_submit_meshing`
- `test_completed_local_step_with_missing_output_is_reset_on_resume`
- `test_running_solver_unknown_remote_state_errors_after_retry_budget`
- `test_stale_solver_completion_after_reset_does_not_overwrite_reset`
- `test_running_meshing_unknown_remote_state_errors_after_retry_budget`
- `test_start_running_with_dead_pipeline_thread_restarts_scheduler`
- `test_step_file_ready_after_stop_does_not_enqueue_sc`

## 验证基线

已运行现有相关测试，当前均通过：

- `.venv\Scripts\python.exe -m pytest tests/test_pipeline_control.py tests/test_pause_start.py tests/test_race_condition_fix.py -q` -> 43 passed
- `.venv\Scripts\python.exe -m pytest tests/test_scheduler_modules.py -q` -> 102 passed
- `.venv\Scripts\python.exe -m pytest tests/test_remote_executor_full.py tests/test_cleaner_utils.py -q` -> 58 passed

因此，下列问题不是现有测试失败项，而是测试清单覆盖到更复杂组合时应优先暴露的风险点。

## P1: reset 后 solver 终态报告标志未清理，重跑完成后可能不再自然收尾

### 代码证据

- `engine/scheduler/barrier.py:60` 初始化 `_solver_terminal_reported = False`。
- `engine/scheduler/barrier.py:457-475` `_report_solver_terminal_if_ready()` 一旦报告终态，就将 `_solver_terminal_reported` 置为 True；之后再次进入会直接 return。
- `engine/scheduler/main.py:845-870` `reset_config()` 会清 `_barrier_passed` 和 workstation barrier cache，但不会清 `_solver_terminal_reported`。

### 触发场景

1. 所有构型 solver 完成，`_report_solver_terminal_if_ready()` 已调用 `finalize_pipeline()`。
2. 用户执行 `reset all solver`、`reset all meshing`、`reset all`，或单构型 reset 触及 meshing/solver 后再 start。
3. 新一轮 solver 全部完成后，`_solver_terminal_reported` 仍为 True，终态报告被跳过。

### 预期与风险

预期：reset 触及 solver/meshing 后，新一轮流水线完成仍应触发自然收尾。

风险：重跑完成后不再调用 `finalize_pipeline()`，可能导致 engine 终态、SSH 断开、worker join、TUI 可见终态与实际运行结果不一致。

### 建议补测

- `test_reset_solver_after_terminal_allows_second_terminal_report`
- `test_reset_meshing_after_terminal_allows_pipeline_finalize_again`

## P1: 远程任务状态 unknown 会保留 Running 且退出当前处理，可能长期阻塞

### 代码证据

- `executor/remote_executor.py:295-339` `query_remote_task_status()` 在 flag 检查或计划任务查询发生 `OSError/ConnectionError` 时返回 `"unknown"`。
- `engine/scheduler/meshing_monitor.py:203-241` Meshing 为 Running 且远程状态 unknown 时，保留 Running 并 `return False`。
- `engine/scheduler/meshing_monitor.py:172-179` `_process_single_meshing()` 返回 False 后队列 claim 被 complete，不会 requeue。
- `engine/scheduler/barrier.py:371-409` Solver 为 Running 且远程状态 unknown 时，保留 Running 并直接 return。
- `engine/scheduler/main.py:515-550` resume 扫描遇到 Running + remote unknown 时，也保留 Running 并跳过重启。

### 触发场景

1. Daemon 重启或 resume 时，DB 中某个 meshing/solver 为 Running。
2. 工作站网络、SSH 或计划任务查询暂时异常，远程状态返回 unknown。
3. 调度器保留 Running，但没有继续排队、重试计数或超时降级路径。

### 预期与风险

预期：unknown 可以短期保守处理以避免重复启动，但应有后续重新探测、重排队或错误暴露机制。

风险：构型长期停在 Running；Meshing 队列已释放 claim，Solver dispatch loop 也退出，后续 start/resume 仍可能重复遇到 unknown 并继续跳过，流水线阻塞且不进入明确 Error。

### 建议补测

- `test_meshing_running_unknown_requeues_or_reports_after_resume`
- `test_solver_running_unknown_does_not_leave_pipeline_silent_stuck`
- `test_resume_scan_unknown_remote_status_eventually_surfaces_error`

## P1: paused 状态允许 reset/clean，但当前运行步骤仍可自然完成并覆盖 reset 后状态

### 代码证据

- `engine/scheduler/main.py:432-445` `pause()` 只设置暂停位和 engine_status，不把 Running 步骤改 Paused，注释明确“正在运行的步骤将继续执行直到完成”。
- `engine/daemon.py:963-974` reset/clean guard 只拒绝 `engine_status == "running"`；engine_status 为 paused 时允许执行。
- `engine/scheduler/worker_pool.py:351-402` SC worker 已取到任务后会继续执行，成功后可设置 sc Completed 并提交 transfer。
- `engine/scheduler/worker_pool.py:476-517` Transfer worker 成功后可提交 MeshingMonitor。
- `engine/scheduler/barrier.py:337-358` Solver dispatch thread 只检查 stopped，不感知 reset 操作；reset 也没有 join/停止 solver dispatch thread。
- `engine/scheduler/main.py:805-876` `reset_config()` 修改 DB 状态、清 barrier/queue，但没有等待当前 worker/solver dispatch 线程停止。

### 触发场景

1. 某构型 SC/Transfer/Solver 已经开始执行。
2. 用户 pause，engine_status 变为 paused，但当前步骤继续运行。
3. 用户立即 reset 该构型或 reset all。
4. 旧 worker/solver 完成后继续写 Completed/Error，或继续提交下游队列。

### 预期与风险

预期：pause 后 reset 应建立一个稳定边界，reset 后旧运行步骤不应再覆盖新状态或继续推进下游。

风险：reset 后 DB 被旧执行线程回写，出现“重置后的步骤被标记完成”“下游被重新提交”“部分构型漏跑或重复推进”等非预期状态。

### 建议补测

- `test_pause_reset_all_while_sc_worker_in_progress_does_not_overwrite_reset`
- `test_pause_reset_transfer_while_transfer_worker_in_progress_does_not_submit_meshing`
- `test_pause_reset_solver_while_solver_dispatch_active_does_not_write_stale_status`

## P2: clean 不改状态，clean 后 start 可能跳过已清理产物对应的上游步骤

### 代码证据

- `executor/cleaner.py:248-281` `clean_step_files()` 只删除步骤文件。
- `executor/cleaner.py:334-377` clean 会删除 SW STEP、SC SCDOC 以及远程 meshing/solver 产物。
- `engine/state_manager.py:686-737` reset 才负责修改步骤状态；clean 路径没有调用 reset。
- `engine/scheduler/main.py:496-501` resume 扫描遇到 `STATUS_COMPLETED` 会直接 continue，不检查该 Completed 步骤产物是否仍存在。

### 触发场景

1. 构型 SW 已 Completed，SC/Transfer/Meshing 尚未全部完成。
2. 用户 pause 后执行 `clean sw` 或 `clean all sw`，STEP 文件被删除，但 DB 中 SW 仍为 Completed。
3. 用户 start/resume，恢复扫描跳过 SW，不重新生成 STEP。
4. 后续 SC 需要 STEP 文件时可能无法执行，或构型停在等待/错误状态。

### 预期与风险

预期：如果 clean 后 start 仍允许继续流水线，应避免“状态 Completed 但产物缺失”导致后续步骤漏跑或阻塞；至少测试应固定当前契约。

风险：用户以为 clean 后再 start 会自动补齐，但调度器按 DB 状态跳过上游步骤，导致缺失产物的下游无法推进。

### 建议补测

- `test_clean_sw_without_reset_then_start_does_not_silently_skip_required_step`
- `test_clean_sc_without_reset_then_start_surfaces_missing_scdoc_contract`

## P2: engine_status=running 但主调度线程已死亡时，start 不会尝试恢复

### 代码证据

- `engine/daemon.py:408-415` `handle_start()` 在 `engine_status == "running"` 分支只检查 `scheduler.is_paused`，否则直接返回“流水线已在运行中”。
- `engine/daemon.py:426-436` 只有 paused 分支检查 `scheduler.pipeline_alive` 并在必要时重启。
- `engine/scheduler/main.py:453-455` 已提供 `pipeline_alive` 属性。

### 触发场景

1. DB 或 engine_status 留在 running。
2. 调度主线程已退出或未成功启动，但 pause 标志未置位。
3. 用户执行 start，希望恢复流水线。
4. Daemon 返回“已在运行中”，不会创建新的 scheduler thread。

### 预期与风险

预期：running 状态也应校验主调度线程是否仍存活，避免假运行状态。

风险：TUI 显示运行中，但没有调度线程推进队列，构型停滞。

### 建议补测

- `test_start_when_engine_running_but_pipeline_thread_dead_restarts`

## P2: resume 与 stop/full-quit 的组件启动和停止窗口未整体串行化

### 代码证据

- `engine/scheduler/main.py:703-724` `resume()` 只在 `_control.resume_transition()` 内执行断点扫描和设置 running；随后启动 file monitor、MeshingMonitor、worker pool、barrier/solver 分发时已经离开 transition lock。
- `engine/scheduler/main.py:737-786` `stop()` 可并发设置 stopped、清队列、join 线程、停止 file monitor、断开 SSH。

### 触发场景

1. 用户 start/resume 后，`resume()` 刚离开 transition lock。
2. 用户立即 full quit/daemon shutdown，进入 `scheduler.stop()`。
3. `resume()` 仍继续启动组件或 pipeline thread，`stop()` 同时清理组件。

### 预期与风险

预期：resume 后续组件启动与 stop 清理应有一致的串行边界，stop 后不得留下新启动的 monitor/worker/thread。

风险：可能出现 stop 后短暂启动组件、队列残留、线程状态和 engine_status 不一致。

### 建议补测

- `test_concurrent_resume_and_stop_do_not_leave_orphaned_components`
- `test_stop_during_resume_component_start_window_keeps_engine_stopped`

## P3: stop 清 SC 队列早于停止文件监控，STEP 回调仍可能重新入队

### 代码证据

- `engine/scheduler/main.py:400-403` `_on_step_file_ready()` 只检查 paused，不检查 stopped。
- `engine/scheduler/main.py:737-776` `stop()` 先 `_control.stop()`，该操作会清 paused；随后 `_sc_queue.clear()`，最后才 `_file_monitor.stop()`。
- `engine/scheduler/main.py:764-776` 清队列和停止 file monitor 之间存在窗口。

### 触发场景

1. stop/full quit 期间，paused 已被清除且 stopped 已设置。
2. file monitor 还未 stop，刚好触发 STEP ready 回调。
3. `_on_step_file_ready()` 因 paused 为 False 继续 `_enqueue_sc()`。

### 预期与风险

预期：stopped 状态下 STEP ready 回调不应再入队。

风险：停止过程中留下孤立 SC 队列项；下次 start 可能依赖 claim/clear 逻辑自愈，但日志和状态会混乱。

### 建议补测

- `test_step_file_ready_after_stop_does_not_enqueue_sc`
- `test_stop_orders_file_monitor_before_queue_clear`
