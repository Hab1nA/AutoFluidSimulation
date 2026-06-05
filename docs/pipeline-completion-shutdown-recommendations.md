# 流水线完成后的正确收尾修改建议

## 背景

当前流水线的主路径是：

1. `SW -> SC -> Transfer -> Meshing` 按构型推进。
2. `BarrierCoordinator` 检测所有构型 `meshing == Completed` 后，通过全局屏障。
3. 屏障通过后执行 `runner.do_sc_final_cleanup()`，然后启动 `SolverDispatcher` 串行执行所有构型的 solver。
4. 单个 solver 完成后，`_execute_solver_for_config()` 将该构型的 `solver` 状态置为 `Completed`。

核查发现，所有 solver 都完成后，`_solver_dispatch_loop()` 只会因为 `_next_solver_config()` 返回 `None` 而退出调度线程。它不会把 `engine_status` 从 `running` 切回 `stopped`，也不会通知其他后台循环退出。

这会导致 TUI 或 IPC 侧仍可能看到引擎状态为 `running`，即使所有构型的所有步骤已经完成。SC/Transfer/Meshing 等后台线程也会继续空闲轮询，直到用户显式 stop/full_quit 或 daemon 退出。

## 建议目标

完成收尾应满足以下条件：

- 所有构型 `solver == Completed` 后，系统应进入明确的终态。
- TUI 的 `engine_status` 不应继续显示 `running`。
- 空闲后台线程应收到停止信号并退出，避免无意义轮询。
- 不应把已经 `Completed` 的步骤批量改成 `Paused`。
- 不应直接杀掉 daemon。daemon 作为 IPC 服务可以继续存在，等待用户查询结果、reset 或重新 start。

## 最小修改建议

在 `engine/scheduler/barrier.py` 中，为 `BarrierCoordinator` 增加一个完成回调，例如：

```python
on_pipeline_complete: Callable[[], None] | None = None
```

然后在 `_solver_dispatch_loop()` 退出前判断是否真的完成：

```python
if (
    not self._stopped.is_set()
    and not self._paused.is_set()
    and self._all_solver_completed()
):
    self._mark_pipeline_complete()
```

其中 `_all_solver_completed()` 只检查所有构型的 `solver` 是否均为 `Completed`。不要只依赖 `_next_solver_config() is None`，因为没有待执行 solver 也可能意味着 solver 全部是 `Error`，或当前处于暂停/停止路径。

`_mark_pipeline_complete()` 可以记录日志并调用注入的完成回调：

```python
def _mark_pipeline_complete(self) -> None:
    logger.info("=" * 60)
    logger.info(">>> 全部构型处理完成，流水线进入收尾状态 <<<")
    logger.info("=" * 60)
    if self._on_pipeline_complete is not None:
        self._on_pipeline_complete()
```

## PipelineScheduler 侧收尾入口

建议在 `engine/scheduler/main.py` 中增加专用方法，例如 `complete_pipeline()`。这个方法与 `stop()` 分开，不要复用 `stop()` 的完整逻辑。

原因是 `stop()` 当前会调用 `set_all_running_to_paused()`，它适合用户主动停止或 daemon 关闭，但不适合自然完成收尾。自然完成时所有步骤应保持 `Completed`。

推荐 `complete_pipeline()` 做这些事：

1. 设置共享 stopped event，让 worker、meshing monitor、barrier monitor 的循环自然退出。
2. 设置 `engine_status = "stopped"`，让 IPC/TUI 看到终态。
3. 停止 STEP 文件监控器。
4. 等待 SC/Transfer/Meshing 等后台线程短暂退出。
5. 可选断开 SSH 连接。

伪代码：

```python
def complete_pipeline(self) -> None:
    logger.info("[Scheduler] 全部构型处理完成，开始自然收尾")
    self._control.stop()
    self.state.set_engine_status("stopped")

    if self._file_monitor is not None:
        self._file_monitor.stop()

    self.worker_pool.join_worker_threads(timeout=3)
    # 不要在 SolverDispatcher 线程中 join 当前 solver 线程。
    self.runner.disconnect_ssh()
    logger.info("[Scheduler] 自然收尾完成")
```

如果该方法会由 `SolverDispatcher` 线程调用，需要避免调用 `barrier_coordinator.join_solver_threads()`，否则可能 join 当前线程。可以只清理非 solver 后台线程，solver 线程会在回调返回后自然退出。

## 可选的完成态设计

更完整但改动更大的方案是新增 engine 状态：

```text
running | paused | stopped | completed
```

优点：

- TUI 可以明确显示“已完成”，而不是复用“已停止”。
- `handle_start()` 可以区分用户主动停止后的 start 和完整完成后的 start。

代价：

- `StateManager.set_engine_status()` 需要接受 `completed`。
- `autofluid-tui` 的状态显示需要增加 `completed` 文案。
- IPC、测试和文档都需要同步。

如果只是修复当前非预期行为，建议先使用 `stopped` 作为自然完成终态，后续再评估是否引入 `completed`。

## 失败路径也需要区分

当前已有两类失败终止会设置 stopped：

- 所有 SW 均失败。
- Meshing 屏障无法通过，所有 Meshing 终结但存在 Error。

建议保留这些路径，不要和自然完成路径混在一起。自然完成应只在所有 solver 均为 `Completed` 时触发。

如果所有 solver 终结但存在 Error，应将 `engine_status` 也切到 `stopped`，但日志和错误状态要明确是失败停止，不要写成“全部完成”。

## 建议测试

建议补充以下测试：

1. `test_solver_all_completed_marks_engine_stopped`
   - 构造所有 meshing completed、solver waiting。
   - 调用 `dispatch_solver_if_ready()`。
   - 等待 solver 线程退出。
   - 断言所有 solver 为 `Completed`。
   - 断言 `engine_status == "stopped"`。
   - 断言 stopped event 已置位。

2. `test_solver_error_does_not_report_pipeline_completed`
   - 让某个 solver wait 返回失败。
   - 断言该 solver 为 `Error`。
   - 断言不会输出“全部构型处理完成”的日志。
   - 可根据实现选择断言 `engine_status == "stopped"`。

3. `test_pipeline_completion_does_not_pause_completed_steps`
   - 完成后检查所有步骤保持 `Completed`。
   - 防止误用 `PipelineScheduler.stop()` 导致状态被批量改为 `Paused`。

4. `test_completion_cleanup_does_not_join_current_solver_thread`
   - 覆盖完成回调从 solver 线程内触发的场景。
   - 确认不会死锁。

## 验证命令

修改 Python 代码后，建议至少运行：

```powershell
.venv\Scripts\python.exe -m pytest tests/test_scheduler_modules.py -k "barrier or solver"
.venv\Scripts\python.exe -m pytest tests/test_pause_start.py
.venv\Scripts\python.exe -m ruff check .
.venv\Scripts\python.exe -m mypy .
```

如果引入 `completed` engine 状态，还需要运行 Rust TUI 检查：

```powershell
cd autofluid-tui
cargo check
cargo clippy -- -D warnings
cargo fmt --check
cargo test
```

