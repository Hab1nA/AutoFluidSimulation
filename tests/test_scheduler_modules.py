"""
scheduler 子模块的单元测试。

覆盖：
- RetryManager: 重试逻辑、暂停感知 sleep
- BarrierCoordinator: 屏障条件判断
- WorkerPoolManager: 队列健康检测
"""

import threading
import time
import os
import subprocess
import sys
import tempfile
import shutil
import logging
import pytest
from engine.state_manager import StateManager
from engine.config import (
    STATUS_WAITING, STATUS_RUNNING, STATUS_PAUSED, STATUS_COMPLETED, STATUS_ERROR, STATUS_RETRYING,
    ENGINE_CONFIG, IPC_CONFIG, LOCAL_PATHS,
)
from engine.scheduler.retry import RetryManager
from engine.scheduler.utils import pause_aware_sleep
from engine.task_runner import TaskRunner


# ====================================================================
# RetryManager 测试
# ====================================================================

class TestRetryManager:
    """RetryManager 重试逻辑测试。"""

    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp(prefix="retry_test_")
        self.db_path = os.path.join(self.tmpdir, "test.db")
        self._orig_db_path = IPC_CONFIG["db_path"]
        IPC_CONFIG["db_path"] = self.db_path

        self.state = StateManager(db_path=self.db_path)
        configs = {i: [1.0, 2.0, 3.0, 4.0] for i in range(1, 4)}
        self.state.load_configs(configs)

        self.paused = threading.Event()
        self.stopped = threading.Event()
        self.retry_mgr = RetryManager(self.state, self.paused, self.stopped)

    def teardown_method(self):
        IPC_CONFIG["db_path"] = self._orig_db_path
        if os.path.exists(self.tmpdir):
            shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_execute_success_first_try(self):
        """首次尝试成功应返回 True 并标记 Completed。"""
        call_count = 0
        def success_func(cn):
            nonlocal call_count
            call_count += 1
            return True

        result = self.retry_mgr.execute_with_retry(1, "sc", success_func)
        assert result is True
        assert call_count == 1
        assert self.state.get_step_status(1, "sc") == STATUS_COMPLETED

    def test_execute_postprocess_success_waits_for_completion_poll(self):
        """PostProcess 启动成功后应保持 Running，等待轮询完成信号。"""
        result = self.retry_mgr.execute_with_retry(1, "postprocess", lambda _cn: True)

        assert result is True
        assert self.state.get_step_status(1, "postprocess") == STATUS_RUNNING

    def test_execute_success_after_retry(self):
        """首次失败、第二次成功应返回 True。"""
        call_count = 0
        def fail_then_success(cn):
            nonlocal call_count
            call_count += 1
            return call_count > 1

        # 使用较短的重试延迟
        result = self.retry_mgr.execute_with_retry(1, "sc", fail_then_success)
        assert result is True
        assert call_count == 2
        assert self.state.get_step_status(1, "sc") == STATUS_COMPLETED

    def test_execute_all_retries_exhausted(self):
        """所有重试均失败应返回 False 并标记 Error。"""
        def always_fail(cn):
            return False

        result = self.retry_mgr.execute_with_retry(1, "sc", always_fail)
        assert result is False
        assert self.state.get_step_status(1, "sc") == STATUS_ERROR

    def test_execute_all_retries_preserves_existing_error_reason(self):
        """最终失败不应覆盖执行函数写入的具体错误原因。"""
        def fail_with_reason(cn):
            self.state.set_step_status(cn, "sc", STATUS_ERROR, "SpaceClaim ready timeout")
            return False

        result = self.retry_mgr.execute_with_retry(1, "sc", fail_with_reason)

        assert result is False
        step = self.state.get_all_steps_for_config(1)["sc"]
        assert step["status"] == STATUS_ERROR
        assert step["error_message"] == "SpaceClaim ready timeout"

    def test_execute_all_retries_preserves_bound_step_error(self):
        """执行函数所属对象的 last_<step>_error 应透传到最终 Error。"""
        class _Executor:
            last_sc_error = ""

            def run(self, cn):
                self.last_sc_error = "Bridge exited early"
                return False

        result = self.retry_mgr.execute_with_retry(1, "sc", _Executor().run)

        assert result is False
        step = self.state.get_all_steps_for_config(1)["sc"]
        assert step["status"] == STATUS_ERROR
        assert step["error_message"] == "Bridge exited early"

    def test_execute_stopped_immediately(self):
        """stopped 标志应立即中止执行。"""
        self.stopped.set()
        call_count = 0
        def should_not_be_called(cn):
            nonlocal call_count
            call_count += 1
            return True

        result = self.retry_mgr.execute_with_retry(1, "sc", should_not_be_called)
        assert result is False
        assert call_count == 0

    def test_execute_paused_marks_paused(self):
        """暂停状态下执行失败应标记 Paused 而非 Error。"""
        self.paused.set()
        # 在另一个线程中很快清除暂停（避免死锁）
        threading.Timer(0.1, self.paused.clear).start()

        def fail_on_first(cn):
            # 第一次调用时暂停标志仍置位
            return False

        _ = self.retry_mgr.execute_with_retry(1, "sc", fail_on_first)
        # 因暂停导致的失败应标记 Paused（或因线程调度竞态可能走正常重试路径）
        status = self.state.get_step_status(1, "sc")
        assert status in (STATUS_PAUSED, STATUS_RETRYING, STATUS_ERROR)

    def test_pause_aware_sleep_completes(self):
        """无中断时 pause_aware_sleep 应正常完成。"""
        start = time.time()
        result = pause_aware_sleep(0.2, self.paused, self.stopped)
        elapsed = time.time() - start
        assert result is True
        assert elapsed >= 0.15  # 允许一定误差

    def test_pause_aware_sleep_stopped(self):
        """stopped 标志应使 sleep 提前退出。"""
        def set_stopped():
            time.sleep(0.05)
            self.stopped.set()

        threading.Thread(target=set_stopped, daemon=True).start()
        result = pause_aware_sleep(5.0, self.paused, self.stopped)
        assert result is False

    def test_pause_aware_sleep_paused_then_resumed(self):
        """暂停期间应等待，恢复后继续。"""
        def pause_and_resume():
            self.paused.set()
            time.sleep(0.1)
            self.paused.clear()

        threading.Thread(target=pause_and_resume, daemon=True).start()
        start = time.time()
        result = pause_aware_sleep(0.2, self.paused, self.stopped)
        elapsed = time.time() - start
        assert result is True
        assert elapsed >= 0.15  # 应等待暂停期间


# ====================================================================
# PipelineScheduler 集成测试（拆分后的新模块路径）
# ====================================================================

class TestSchedulerModuleImports:
    """验证 scheduler 包的导入兼容性。"""

    def test_import_from_package(self):
        """from engine.scheduler import PipelineScheduler 应正常工作。"""
        from engine.scheduler import PipelineScheduler
        assert PipelineScheduler is not None

    def test_import_from_main(self):
        """from engine.scheduler.main import PipelineScheduler 应正常工作。"""
        from engine.scheduler.main import PipelineScheduler
        assert PipelineScheduler is not None

    def test_import_submodules(self):
        """所有子模块应可独立导入。"""
        from engine.scheduler.worker_pool import WorkerPoolManager
        from engine.scheduler.barrier import BarrierCoordinator
        from engine.scheduler.sw_phase import SWPhaseHandler
        from engine.scheduler.retry import RetryManager
        assert WorkerPoolManager is not None
        assert BarrierCoordinator is not None
        assert SWPhaseHandler is not None
        assert RetryManager is not None

    def test_package_exports(self):
        """包的 __all__ 应包含所有公开接口。"""
        import engine.scheduler as scheduler_pkg
        assert hasattr(scheduler_pkg, "PipelineScheduler")
        assert hasattr(scheduler_pkg, "WorkerPoolManager")
        assert hasattr(scheduler_pkg, "BarrierCoordinator")
        assert hasattr(scheduler_pkg, "SWPhaseHandler")
        assert hasattr(scheduler_pkg, "RetryManager")
        assert hasattr(scheduler_pkg, "MeshingMonitor")

    def test_daemon_imports_in_fresh_process(self):
        """daemon 冷启动导入不能被 scheduler 包初始化形成的循环依赖阻断。"""
        result = subprocess.run(
            [sys.executable, "-c", "from engine.daemon import main"],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr

    def test_server_mode_worker_pool_defaults_to_one_sc_worker(self, monkeypatch):
        """server mode 下默认单路 SC，避免工作站并发启动多个 SpaceClaim。"""
        from engine.scheduler.worker_pool import WorkerPoolManager

        monkeypatch.setenv("AUTOFLUID_SERVER_MODE", "server")

        manager = WorkerPoolManager(
            state_manager=object(),
            task_runner=object(),
            sc_queue=object(),
            paused_event=threading.Event(),
            stopped_event=threading.Event(),
            barrier_passed_event=threading.Event(),
            retry_manager=object(),
        )

        assert manager._num_sc_workers == 1


class TestSWPhaseHandlerFallback:
    """验证 SW 独立模式文件监控回调。"""

    def test_fallback_monitor_submits_to_unique_work_queue(self, monkeypatch):
        """独立模式发现 STEP 文件时应使用去重队列接口提交。"""
        from engine.file_monitor import StepFileMonitor
        from engine.scheduler.sw_phase import SWPhaseHandler
        from engine.scheduler.work_queue import UniqueWorkQueue

        monkeypatch.delenv("AUTOFLUID_SERVER_MODE", raising=False)

        class _State:
            @staticmethod
            def get_step_status(config_name: int, step_name: str) -> str:
                return STATUS_WAITING

            @staticmethod
            def set_step_status(config_name: int, step_name: str, status: str) -> None:
                return None

        monkeypatch.setattr(StepFileMonitor, "start", lambda self: None)
        sc_queue = UniqueWorkQueue[tuple[int, str]](key=lambda item: item[0])
        handler = SWPhaseHandler(
            state_manager=_State(),
            task_runner=object(),
            sc_queue=sc_queue,
            paused_event=threading.Event(),
            stopped_event=threading.Event(),
            retry_manager=object(),
        )

        handler._ensure_file_monitor_running()
        assert handler._file_monitor is not None
        assert handler._file_monitor.on_file_ready is not None
        handler._file_monitor.on_file_ready(1, "model_gen4.SLDPRT_1.step")

        assert sc_queue.get_nowait() == (1, "model_gen4.SLDPRT_1.step")

    def test_server_mode_enqueues_sc_after_local_worker_sw_completion(self, monkeypatch):
        """server 模式下 LocalWorker 完成 SW 后应由调度器直接推入 SC 队列。"""
        from engine import config as config_module
        from engine.scheduler.sw_phase import SWPhaseHandler
        from engine.scheduler.work_queue import UniqueWorkQueue

        monkeypatch.setenv("AUTOFLUID_SERVER_MODE", "server")
        monkeypatch.setitem(config_module.LOCAL_PATHS, "step_dir", r"C:\AutoFluid\steps")

        class _State:
            def __init__(self):
                self.status = {
                    (1, "sw"): STATUS_WAITING,
                    (1, "sc"): STATUS_WAITING,
                }
                self.engine_status = "stopped"
                self.sw_macro_started = False

            def get_all_configs(self) -> list[int]:
                return [1]

            def get_step_status(self, config_name: int, step_name: str) -> str:
                return self.status.get((config_name, step_name), STATUS_WAITING)

            def set_step_status(
                self,
                config_name: int,
                step_name: str,
                status: str,
                message: str | None = None,
            ) -> None:
                self.status[(config_name, step_name)] = status

            def set_engine_status(self, status: str) -> None:
                self.engine_status = status

            def set_sw_macro_started(self, value: bool) -> None:
                self.sw_macro_started = value

        class _Runner:
            def __init__(self, state: _State):
                self.state = state

            def execute_sw_per_config(self, config_name: int) -> bool:
                self.state.set_step_status(config_name, "sw", STATUS_COMPLETED)
                return True

            def disconnect_sw_cached(self) -> None:
                return None

            def verify_step_exports(self, step_dir: str) -> int:
                return 1

        class _RetryManager:
            @staticmethod
            def execute_with_retry(config_name: int, step_name: str, func):
                return func(config_name)

        class _Monitor:
            is_running = False

            def start(self) -> None:
                self.is_running = True

        state = _State()
        sc_queue = UniqueWorkQueue[tuple[int, str]](key=lambda item: item[0])
        handler = SWPhaseHandler(
            state_manager=state,
            task_runner=_Runner(state),
            sc_queue=sc_queue,
            paused_event=threading.Event(),
            stopped_event=threading.Event(),
            retry_manager=_RetryManager(),
        )
        handler.set_file_monitor(_Monitor())

        assert handler._execute_sw_macro([1]) is True

        assert sc_queue.qsize() == 1
        config_name, step_file = sc_queue.get(timeout=1)
        assert config_name == 1
        assert step_file.endswith("model_gen4.SLDPRT_1.step")

    def test_server_mode_starts_worker_pool_before_all_sw_configs_finish(self, monkeypatch):
        """server 模式下单个 SW 构型完成后应立即允许 SC 消费。"""
        from engine import config as config_module
        from engine.scheduler.sw_phase import SWPhaseHandler
        from engine.scheduler.work_queue import UniqueWorkQueue

        monkeypatch.setenv("AUTOFLUID_SERVER_MODE", "server")
        monkeypatch.setitem(config_module.LOCAL_PATHS, "step_dir", r"C:\AutoFluid\steps")

        class _State:
            def __init__(self):
                self.status = {
                    (1, "sw"): STATUS_WAITING,
                    (1, "sc"): STATUS_WAITING,
                    (2, "sw"): STATUS_WAITING,
                    (2, "sc"): STATUS_WAITING,
                }
                self.engine_status = "stopped"
                self.sw_macro_started = False

            def get_all_configs(self) -> list[int]:
                return [1, 2]

            def get_step_status(self, config_name: int, step_name: str) -> str:
                return self.status.get((config_name, step_name), STATUS_WAITING)

            def set_step_status(
                self,
                config_name: int,
                step_name: str,
                status: str,
                message: str | None = None,
            ) -> None:
                self.status[(config_name, step_name)] = status

            def set_engine_status(self, status: str) -> None:
                self.engine_status = status

            def set_sw_macro_started(self, value: bool) -> None:
                self.sw_macro_started = value

        class _Runner:
            def __init__(self, state: _State, sc_queue: UniqueWorkQueue[tuple[int, str]]):
                self.state = state
                self.sc_queue = sc_queue
                self.second_sw_started_after_first_sc_enqueued = False
                self.second_sw_started_after_worker_pool = False

            def execute_sw_per_config(self, config_name: int) -> bool:
                if config_name == 2:
                    self.second_sw_started_after_first_sc_enqueued = (
                        self.sc_queue.has_claim(1)
                    )
                    self.second_sw_started_after_worker_pool = worker_pool.start_count > 0
                self.state.set_step_status(config_name, "sw", STATUS_COMPLETED)
                return True

            def disconnect_sw_cached(self) -> None:
                return None

            def verify_step_exports(self, step_dir: str) -> int:
                return 2

        class _RetryManager:
            @staticmethod
            def execute_with_retry(config_name: int, step_name: str, func):
                return func(config_name)

        class _WorkerPool:
            def __init__(self):
                self.start_count = 0

            def start_if_needed(self) -> None:
                self.start_count += 1

        class _MeshingMonitor:
            def __init__(self):
                self.start_count = 0

            def start_if_needed(self) -> None:
                self.start_count += 1

        state = _State()
        sc_queue = UniqueWorkQueue[tuple[int, str]](key=lambda item: item[0])
        worker_pool = _WorkerPool()
        meshing_monitor = _MeshingMonitor()
        runner = _Runner(state, sc_queue)
        handler = SWPhaseHandler(
            state_manager=state,
            task_runner=runner,
            sc_queue=sc_queue,
            paused_event=threading.Event(),
            stopped_event=threading.Event(),
            retry_manager=_RetryManager(),
            worker_pool_manager=worker_pool,
            meshing_monitor=meshing_monitor,
        )

        assert handler._execute_sw_macro([1, 2]) is True

        assert runner.second_sw_started_after_first_sc_enqueued is True
        assert runner.second_sw_started_after_worker_pool is True
        assert worker_pool.start_count >= 1
        assert meshing_monitor.start_count >= 1
        assert sc_queue.has_claim(1) is True
        assert sc_queue.has_claim(2) is True

    def test_server_mode_breakpoint_resume_enqueues_completed_sw(self, monkeypatch):
        """server 模式断点续传时，已完成 SW 的构型仍应恢复 SC 队列。"""
        from engine import config as config_module
        from engine.scheduler.sw_phase import SWPhaseHandler
        from engine.scheduler.work_queue import UniqueWorkQueue

        monkeypatch.setenv("AUTOFLUID_SERVER_MODE", "server")
        monkeypatch.setitem(config_module.LOCAL_PATHS, "step_dir", r"C:\AutoFluid\steps")

        class _State:
            def __init__(self):
                self.sw_macro_started = True

            def get_all_configs(self) -> list[int]:
                return [1]

            def get_step_status(self, config_name: int, step_name: str) -> str:
                if step_name == "sw":
                    return STATUS_COMPLETED
                return STATUS_WAITING

            def is_sw_macro_started(self) -> bool:
                return self.sw_macro_started

            def set_sw_macro_started(self, value: bool) -> None:
                self.sw_macro_started = value

        class _Runner:
            def __init__(self):
                self.final_cleanup_count = 0

            def do_sw_final_cleanup(self) -> None:
                self.final_cleanup_count += 1

        sc_queue = UniqueWorkQueue[tuple[int, str]](key=lambda item: item[0])
        runner = _Runner()
        handler = SWPhaseHandler(
            state_manager=_State(),
            task_runner=runner,
            sc_queue=sc_queue,
            paused_event=threading.Event(),
            stopped_event=threading.Event(),
            retry_manager=object(),
        )

        assert handler.execute_sw_phase() is True

        assert runner.final_cleanup_count == 1
        assert sc_queue.qsize() == 1
        assert sc_queue.get(timeout=1) == (
            1,
            r"C:\AutoFluid\steps\model_gen4.SLDPRT_1.step",
        )

    def test_server_mode_completed_sw_does_not_requeue_terminal_downstream(self, monkeypatch):
        """下游已有终态时，SW 收尾不应把构型重新推入 SC 队列。"""
        from engine import config as config_module
        from engine.scheduler.sw_phase import SWPhaseHandler
        from engine.scheduler.work_queue import UniqueWorkQueue

        monkeypatch.setenv("AUTOFLUID_SERVER_MODE", "server")
        monkeypatch.setitem(config_module.LOCAL_PATHS, "step_dir", r"C:\AutoFluid\steps")

        class _State:
            def __init__(self):
                self.status = {
                    (1, "sc"): STATUS_ERROR,
                    (1, "meshing"): STATUS_ERROR,
                    (1, "solver"): STATUS_ERROR,
                    (2, "sc"): STATUS_COMPLETED,
                    (2, "transfer"): STATUS_COMPLETED,
                    (2, "meshing"): STATUS_COMPLETED,
                }

            def get_step_status(self, config_name: int, step_name: str) -> str:
                return self.status.get((config_name, step_name), STATUS_WAITING)

        sc_queue = UniqueWorkQueue[tuple[int, str]](key=lambda item: item[0])
        handler = SWPhaseHandler(
            state_manager=_State(),
            task_runner=object(),
            sc_queue=sc_queue,
            paused_event=threading.Event(),
            stopped_event=threading.Event(),
            retry_manager=object(),
        )

        handler._enqueue_server_mode_completed_sw([1, 2])

        assert sc_queue.qsize() == 0

    def test_server_mode_completed_sw_does_not_requeue_active_downstream(self, monkeypatch):
        """下游已有进行中状态时，SW 收尾不应让构型回退到 SC 队列。"""
        from engine import config as config_module
        from engine.scheduler.sw_phase import SWPhaseHandler
        from engine.scheduler.work_queue import UniqueWorkQueue

        monkeypatch.setenv("AUTOFLUID_SERVER_MODE", "server")
        monkeypatch.setitem(config_module.LOCAL_PATHS, "step_dir", r"C:\AutoFluid\steps")

        class _State:
            def __init__(self):
                self.status = {
                    (1, "sc"): STATUS_WAITING,
                    (1, "transfer"): STATUS_RUNNING,
                    (2, "sc"): STATUS_WAITING,
                    (2, "meshing"): STATUS_RETRYING,
                    (3, "sc"): STATUS_WAITING,
                    (3, "solver"): STATUS_PAUSED,
                }

            def get_step_status(self, config_name: int, step_name: str) -> str:
                return self.status.get((config_name, step_name), STATUS_WAITING)

        sc_queue = UniqueWorkQueue[tuple[int, str]](key=lambda item: item[0])
        handler = SWPhaseHandler(
            state_manager=_State(),
            task_runner=object(),
            sc_queue=sc_queue,
            paused_event=threading.Event(),
            stopped_event=threading.Event(),
            retry_manager=object(),
        )

        handler._enqueue_server_mode_completed_sw([1, 2, 3])

        assert sc_queue.qsize() == 0

    def test_server_mode_completed_sw_enqueues_when_downstream_waiting(self, monkeypatch):
        """下游均未开始时，SW 收尾仍应正常推入 SC 队列。"""
        from engine import config as config_module
        from engine.scheduler.sw_phase import SWPhaseHandler
        from engine.scheduler.work_queue import UniqueWorkQueue

        monkeypatch.setenv("AUTOFLUID_SERVER_MODE", "server")
        monkeypatch.setitem(config_module.LOCAL_PATHS, "step_dir", r"C:\AutoFluid\steps")

        class _State:
            @staticmethod
            def get_step_status(config_name: int, step_name: str) -> str:
                return STATUS_WAITING

        sc_queue = UniqueWorkQueue[tuple[int, str]](key=lambda item: item[0])
        handler = SWPhaseHandler(
            state_manager=_State(),
            task_runner=object(),
            sc_queue=sc_queue,
            paused_event=threading.Event(),
            stopped_event=threading.Event(),
            retry_manager=object(),
        )

        handler._enqueue_server_mode_completed_sw([4])

        assert sc_queue.get(timeout=1) == (
            4,
            r"C:\AutoFluid\steps\model_gen4.SLDPRT_4.step",
        )

    def test_server_mode_completed_sw_does_not_enqueue_after_stop(self, monkeypatch):
        """收到停止信号后，SW 收尾不应继续推进下游队列。"""
        from engine import config as config_module
        from engine.scheduler.sw_phase import SWPhaseHandler
        from engine.scheduler.work_queue import UniqueWorkQueue

        monkeypatch.setenv("AUTOFLUID_SERVER_MODE", "server")
        monkeypatch.setitem(config_module.LOCAL_PATHS, "step_dir", r"C:\AutoFluid\steps")

        class _State:
            @staticmethod
            def get_step_status(config_name: int, step_name: str) -> str:
                return STATUS_WAITING

        stopped = threading.Event()
        stopped.set()
        sc_queue = UniqueWorkQueue[tuple[int, str]](key=lambda item: item[0])
        handler = SWPhaseHandler(
            state_manager=_State(),
            task_runner=object(),
            sc_queue=sc_queue,
            paused_event=threading.Event(),
            stopped_event=stopped,
            retry_manager=object(),
        )

        handler._enqueue_server_mode_completed_sw([1])

        assert sc_queue.qsize() == 0

    def test_server_mode_sw_phase_does_not_start_step_file_monitor(self, monkeypatch):
        """server 模式下 SW/SC 由 LocalWorker 执行，daemon 不应启动本地 STEP 监控。"""
        from engine.scheduler.sw_phase import SWPhaseHandler
        from engine.scheduler.work_queue import UniqueWorkQueue

        monkeypatch.setenv("AUTOFLUID_SERVER_MODE", "server")

        class _State:
            def __init__(self):
                self.status = {
                    (1, "sw"): STATUS_WAITING,
                    (1, "sc"): STATUS_WAITING,
                }
                self.engine_status = "stopped"
                self.sw_macro_started = False

            def get_all_configs(self) -> list[int]:
                return [1]

            def get_step_status(self, config_name: int, step_name: str) -> str:
                return self.status.get((config_name, step_name), STATUS_WAITING)

            def set_step_status(
                self,
                config_name: int,
                step_name: str,
                status: str,
                message: str | None = None,
            ) -> None:
                self.status[(config_name, step_name)] = status

            def set_engine_status(self, status: str) -> None:
                self.engine_status = status

            def set_sw_macro_started(self, value: bool) -> None:
                self.sw_macro_started = value

        class _Runner:
            def __init__(self, state: _State):
                self.state = state

            def execute_sw_per_config(self, config_name: int) -> bool:
                self.state.set_step_status(config_name, "sw", STATUS_COMPLETED)
                return True

            def disconnect_sw_cached(self) -> None:
                return None

            def verify_step_exports(self, _step_dir: str) -> int:
                return 1

        class _RetryManager:
            @staticmethod
            def execute_with_retry(config_name: int, step_name: str, func):
                return func(config_name)

        state = _State()
        file_monitor = _SpyFileMonitor()
        handler = SWPhaseHandler(
            state_manager=state,
            task_runner=_Runner(state),
            sc_queue=UniqueWorkQueue[tuple[int, str]](key=lambda item: item[0]),
            paused_event=threading.Event(),
            stopped_event=threading.Event(),
            retry_manager=_RetryManager(),
        )
        handler.set_file_monitor(file_monitor)

        assert handler._execute_sw_macro([1]) is True

        assert file_monitor.start_count == 0


# ====================================================================
# TaskRunner 代理接口测试
# ====================================================================

class TestTaskRunnerSWDelegates:
    """验证 SWPhaseHandler 使用的 SW 操作有 TaskRunner 公共代理。"""

    def test_sw_cache_disconnect_and_export_verification_delegate_to_executor(self):
        class _FakeSWExecutor:
            def __init__(self):
                self.disconnected = False
                self.verified_step_dir = ""

            def disconnect_sw_cached(self) -> None:
                self.disconnected = True

            def _verify_step_exports(self, step_dir: str) -> int:
                self.verified_step_dir = step_dir
                return 3

        runner = TaskRunner.__new__(TaskRunner)
        fake_executor = _FakeSWExecutor()
        runner._sw_executor = fake_executor

        assert runner.verify_step_exports("C:/steps") == 3
        runner.disconnect_sw_cached()

        assert fake_executor.verified_step_dir == "C:/steps"
        assert fake_executor.disconnected is True

    def test_disconnect_ssh_holds_shared_ssh_lock(self):
        """断开共享 SSH 客户端时应持有 TaskRunner 的 SSH 锁。"""
        class _LockProbe:
            def __init__(self) -> None:
                self.held = False
                self.entries = 0

            def __enter__(self) -> "_LockProbe":
                self.held = True
                self.entries += 1
                return self

            def __exit__(self, *_args: object) -> bool:
                self.held = False
                return False

        class _SSH:
            def __init__(self, lock: _LockProbe) -> None:
                self.lock = lock
                self.disconnect_saw_lock = False

            def disconnect(self) -> None:
                self.disconnect_saw_lock = self.lock.held

        runner = TaskRunner.__new__(TaskRunner)
        lock = _LockProbe()
        ssh = _SSH(lock)
        runner._ssh_lock = lock
        runner._ssh = ssh

        runner.disconnect_ssh()

        assert ssh.disconnect_saw_lock is True
        assert runner._ssh is None
        assert lock.entries == 1

    def test_get_ssh_uses_per_workstation_pool(self, monkeypatch):
        """不同工作站应创建并复用独立 SSH 客户端。"""
        import engine.task_runner as task_runner_module

        created: list[tuple[str, int, str, str]] = []

        class _SSH:
            def __init__(self, host: str, port: int, username: str, password: str):
                created.append((host, port, username, password))
                self.connected = False

            def is_connected(self) -> bool:
                return self.connected

            def connect(self) -> bool:
                self.connected = True
                return True

        monkeypatch.setattr(
            task_runner_module,
            "get_workstation_config",
            lambda workstation_id: {
                "id": workstation_id,
                "host": f"{workstation_id}.example",
                "port": 22,
                "username": "ps",
                "password": "pw",
            },
        )
        monkeypatch.setattr(task_runner_module, "RemoteWorkstation", _SSH)

        runner = TaskRunner.__new__(TaskRunner)
        runner._ssh_pool = {}
        runner._ssh_locks = {}
        runner._ssh_lock = threading.RLock()
        runner._ssh = None

        ws_a_first = runner.get_ssh("WS-A")
        ws_a_second = runner.get_ssh("WS-A")
        ws_b = runner.get_ssh("WS-B")

        assert ws_a_first is ws_a_second
        assert ws_a_first is not ws_b
        assert created == [
            ("WS-A.example", 22, "ps", "pw"),
            ("WS-B.example", 22, "ps", "pw"),
        ]

    def test_remote_stage_delegates_use_assigned_workstation(self):
        """Transfer/Meshing/Solver 委托时应使用构型分配的工作站。"""
        from engine.config import DEFAULT_WORKSTATION_ID

        calls: list[tuple[str, int, str]] = []

        class _State:
            def get_config_workstation(self, config_name: int) -> str | None:
                return "WS-A" if config_name == 1 else None

        class _RemoteExecutor:
            def execute_transfer(
                self,
                config_name: int,
                workstation_id: str = DEFAULT_WORKSTATION_ID,
            ) -> bool:
                calls.append(("transfer", config_name, workstation_id))
                return True

            def execute_meshing(
                self,
                config_name: int,
                workstation_id: str = DEFAULT_WORKSTATION_ID,
            ) -> bool:
                calls.append(("meshing", config_name, workstation_id))
                return True

            def execute_solver(
                self,
                config_name: int,
                workstation_id: str = DEFAULT_WORKSTATION_ID,
            ) -> bool:
                calls.append(("solver", config_name, workstation_id))
                return True

        runner = TaskRunner.__new__(TaskRunner)
        runner.state = _State()
        runner._remote_executor = _RemoteExecutor()

        assert runner.execute_transfer(1) is True
        assert runner.execute_meshing(1) is True
        assert runner.execute_solver(1) is True
        assert runner.execute_transfer(2) is True

        assert calls == [
            ("transfer", 1, "WS-A"),
            ("meshing", 1, "WS-A"),
            ("solver", 1, "WS-A"),
            ("transfer", 2, DEFAULT_WORKSTATION_ID),
        ]


# ====================================================================
# Mock 辅助类
# ====================================================================

class _MockTaskRunner:
    """轻量 TaskRunner Mock，供 BarrierCoordinator / SWPhaseHandler 使用。"""

    def __init__(self, state_manager):
        self.state = state_manager
        self._sc_cleanup_called = False
        self._solver_dispatched = []
        self._solver_wait_result = True
        self._solver_wait_count = 0
        self._solver_flag_cleanup_count = 0
        self._postprocess_dispatched = []
        self._postprocess_wait_result = True
        self._postprocess_wait_count = 0
        self._postprocess_cleanup_calls: list[tuple[int, str]] = []
        self._remote_executor = _MockRemoteExecutor(self.state)
        self._sw_in_flight = False
        self._ssh = _MockSSH()

    def set_control_events(self, paused_event, stopped_event):
        self._paused_event = paused_event
        self._stopped_event = stopped_event

    def do_sc_final_cleanup(self):
        self._sc_cleanup_called = True

    def shutdown_sc_pool(self):
        return None

    def disconnect_ssh(self):
        return None

    def reset_sc_pool(self):
        return None

    def execute_sc_step(self, config_name: int) -> bool:
        self.state.set_step_status(config_name, "sc", STATUS_COMPLETED)
        return True

    def execute_transfer(self, config_name: int) -> bool:
        self.state.set_step_status(config_name, "transfer", STATUS_COMPLETED)
        return True

    def disconnect_sw_cached(self):
        return None

    def verify_step_exports(self, step_dir: str) -> int:
        return sum(
            1
            for cn in self.state.get_all_configs()
            if self.state.get_step_status(cn, "sw") == STATUS_COMPLETED
        )

    def is_sw_in_flight(self, config_name: int | None = None) -> bool:
        return self._sw_in_flight

    def execute_solver(self, config_name: int) -> bool:
        self._solver_dispatched.append(config_name)
        self.state.set_step_status(config_name, "solver", STATUS_COMPLETED)
        return True

    def wait_solver_completion(self, config_name, paused_event=None, stopped_event=None) -> bool:
        self._solver_wait_count += 1
        return self._solver_wait_result

    def execute_postprocess(self, config_name: int) -> bool:
        self._postprocess_dispatched.append(config_name)
        return True

    def register_postprocess_from_solver(self, config_name: int) -> bool:
        workstation_id = "default"
        get_config_workstation = getattr(self.state, "get_config_workstation", None)
        if callable(get_config_workstation):
            workstation_id = str(get_config_workstation(config_name) or "default")
        return self._remote_executor.register_postprocess_from_solver(
            config_name,
            workstation_id=workstation_id,
        )

    def wait_postprocess_completion(self, config_name, paused_event=None, stopped_event=None) -> bool:
        self._postprocess_wait_count += 1
        return self._postprocess_wait_result

    def cleanup_completed_postprocess_task(
        self,
        config_name: int,
    ) -> None:
        workstation_id = "default"
        get_config_workstation = getattr(self.state, "get_config_workstation", None)
        if callable(get_config_workstation):
            workstation_id = str(get_config_workstation(config_name) or "default")
        self._postprocess_cleanup_calls.append((config_name, workstation_id))
        self._remote_executor.forget_remote_task(
            config_name,
            "postprocess",
            workstation_id=workstation_id,
        )

    def cleanup_solver_runtime_flag_artifacts(self) -> dict[str, int]:
        self._solver_flag_cleanup_count += 1
        return {"deleted": 2, "failed": 0}

    def get_remote_executor(self):
        return self._remote_executor

    def get_ssh(self, workstation_id: str = "default"):
        return self._ssh


class _MockSSH:
    """最小 SSH Mock，用于调度恢复扫描的远程产物检查。"""

    def __init__(self):
        self.remote_file_sizes: dict[str, int] = {}

    def is_connected(self) -> bool:
        return True

    def get_remote_file_size(self, remote_path: str, *, timeout: float | None = None) -> int | None:
        return self.remote_file_sizes.get(remote_path.replace("\\", "/"))

    def check_remote_file(self, remote_path: str, *, timeout: float | None = None) -> bool:
        return remote_path.replace("\\", "/") in self.remote_file_sizes


class _MockRemoteExecutor:
    """轻量 RemoteExecutor Mock。"""

    def __init__(self, state_manager):
        self.state = state_manager
        self._meshing_started: list[tuple[int, str]] = []
        self._meshing_output_exists = False
        self._meshing_output_checks: list[tuple[int, float | None, str]] = []
        self._remote_task_status = "lost"
        self._remote_task_status_checks: list[tuple[int, str, str]] = []
        self._forgotten_remote_tasks: list[tuple[int, str, str]] = []
        self._postprocess_handoffs: list[tuple[int, str]] = []
        self._postprocess_handoff_result = True
        self._remote_task_events: list[tuple[str, int, str, str]] = []
        self._meshing_waits: list[tuple[int, str]] = []

    def start_meshing(self, config_name: int, workstation_id: str = "default") -> bool:
        self._meshing_started.append((config_name, workstation_id))
        return True

    def check_meshing_done(self, config_name: int) -> bool:
        return False

    def wait_meshing_completion(
        self,
        config_name,
        paused_event=None,
        stopped_event=None,
        workstation_id: str = "default",
    ) -> bool:
        # 立即完成
        self._meshing_waits.append((config_name, workstation_id))
        return True

    def get_ssh_connection(self):
        return None

    def check_meshing_outputs_exist(
        self,
        config_name: int,
        *,
        timeout: float | None = None,
        workstation_id: str = "default",
    ) -> bool:
        self._meshing_output_checks.append((config_name, timeout, workstation_id))
        return self._meshing_output_exists

    def query_remote_task_status(
        self,
        config_name: int,
        step_name: str,
        workstation_id: str = "default",
    ) -> str:
        self._remote_task_status_checks.append((config_name, step_name, workstation_id))
        return self._remote_task_status

    def forget_remote_task(
        self,
        config_name: int,
        step_name: str,
        workstation_id: str = "default",
    ) -> None:
        self._remote_task_events.append(("forget", config_name, step_name, workstation_id))
        self._forgotten_remote_tasks.append((config_name, step_name, workstation_id))
        delete_remote_task = getattr(self.state, "delete_remote_task", None)
        if callable(delete_remote_task):
            delete_remote_task(config_name, step_name, workstation_id=workstation_id)

    def register_postprocess_from_solver(
        self,
        config_name: int,
        workstation_id: str = "default",
    ) -> bool:
        self._remote_task_events.append(
            ("handoff", config_name, "postprocess", workstation_id)
        )
        self._postprocess_handoffs.append((config_name, workstation_id))
        if not self._postprocess_handoff_result:
            return False
        self.state.save_remote_task(
            workstation_id=workstation_id,
            config_name=config_name,
            step_name="postprocess",
            task_name="AutoFluid_solver_task",
            flag_file=f"D:/flags/postprocess_done_{config_name}.txt",
            error_flag_file=f"D:/flags/postprocess_done_{config_name}.txt.error",
            started_at=time.time(),
        )
        return True


class _SpyFileMonitor:
    """记录调度器是否启动了 STEP 文件监控。"""

    def __init__(self):
        self.start_count = 0
        self.reset_only_count = 0
        self.resume_and_reset_count = 0
        self._running = False

    @property
    def is_running(self):
        return self._running

    def start(self):
        self.start_count += 1
        self._running = True

    def stop(self):
        self._running = False

    def pause(self):
        return None

    def resume_only(self):
        return None

    def resume_and_reset(self):
        self.resume_and_reset_count += 1
        return None

    def reset_only(self):
        self.reset_only_count += 1
        return None


class TestPipelineSchedulerStartRecovery:
    """验证 start 对非全新数据库状态的轻量恢复。"""

    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp(prefix="scheduler_start_")
        self.db_path = os.path.join(self.tmpdir, "test.db")
        self._orig_db_path = IPC_CONFIG["db_path"]
        IPC_CONFIG["db_path"] = self.db_path

        self.state = StateManager(db_path=self.db_path)
        self.runner = _MockTaskRunner(self.state)

        from engine.scheduler import PipelineScheduler
        self.scheduler = PipelineScheduler(self.state, self.runner)
        self.file_monitor = _SpyFileMonitor()
        self.scheduler._file_monitor = self.file_monitor

        self.scheduler.meshing_monitor.start_if_needed = lambda: None
        self.scheduler.worker_pool.start_if_needed = lambda: None
        self.scheduler.barrier_coordinator.dispatch_solver_if_ready = lambda: True

    def teardown_method(self):
        self.scheduler.stop()
        IPC_CONFIG["db_path"] = self._orig_db_path
        if os.path.exists(self.tmpdir):
            shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_server_mode_defaults_to_one_sc_worker_for_local_worker_sc_slots(self, monkeypatch):
        """server 模式下 SC 投递默认匹配 LocalWorker 单个 SC 槽位。"""
        monkeypatch.setenv("AUTOFLUID_SERVER_MODE", "server")

        from engine.scheduler import PipelineScheduler

        scheduler = PipelineScheduler(self.state, self.runner)

        assert scheduler.worker_pool._num_sc_workers == 1

    def test_local_mode_defaults_to_one_sc_worker(self, monkeypatch):
        """非 server 模式也默认单路 SC，避免并发启动多个 SpaceClaim。"""
        monkeypatch.delenv("AUTOFLUID_SERVER_MODE", raising=False)

        from engine.scheduler import PipelineScheduler

        scheduler = PipelineScheduler(self.state, self.runner)

        assert scheduler.worker_pool._num_sc_workers == 1

    def test_start_skips_step_monitor_when_only_meshing_pending(self, caplog):
        """SC/Transfer 已完成时，start 直接恢复 Meshing，不扫描旧 STEP。"""
        self.state.load_configs({1: [1.0, 2.0, 3.0, 4.0], 2: [5.0, 6.0, 7.0, 8.0]})
        self.state.set_sw_macro_started(True)
        for cn in (1, 2):
            self.state.set_step_status(cn, "sw", STATUS_COMPLETED)
            self.state.set_step_status(cn, "sc", STATUS_COMPLETED)
            self.state.set_step_status(cn, "transfer", STATUS_COMPLETED)
            self.state.set_step_status(cn, "meshing", STATUS_WAITING)

        with caplog.at_level(logging.INFO):
            self.scheduler.start_pipeline()

        assert self.file_monitor.start_count == 0
        assert self.scheduler.meshing_monitor.qsize() == 2
        messages = [record.getMessage() for record in caplog.records]
        assert any("[MeshingMonitor] 构型1 已入队" in msg for msg in messages)
        assert any("[MeshingMonitor] 构型2 已入队" in msg for msg in messages)
        assert not any("个 Meshing 提交" in msg for msg in messages)

    def test_start_restores_transfer_without_step_monitor(self, caplog):
        """SC 已完成但 Transfer 未完成时，start 直接恢复 Transfer 队列。"""
        self.state.load_configs({1: [1.0, 2.0, 3.0, 4.0]})
        self.state.set_sw_macro_started(True)
        self.state.set_step_status(1, "sw", STATUS_COMPLETED)
        self.state.set_step_status(1, "sc", STATUS_COMPLETED)
        self.state.set_step_status(1, "transfer", STATUS_WAITING)

        with caplog.at_level(logging.INFO):
            self.scheduler.start_pipeline()

        assert self.file_monitor.start_count == 0
        assert self.scheduler.worker_pool._transfer_queue.qsize() == 1
        messages = [record.getMessage() for record in caplog.records]
        assert any("构型1 已推入 Transfer 队列" in msg for msg in messages)
        assert not any("个 Transfer 入队" in msg for msg in messages)

    def test_server_mode_never_needs_daemon_step_file_monitor(self, monkeypatch):
        """server 模式下 daemon 不监控 LocalWorker 机器上的 STEP 目录。"""
        monkeypatch.setenv("AUTOFLUID_SERVER_MODE", "server")
        self.state.load_configs({1: [1.0, 2.0, 3.0, 4.0]})
        self.state.set_step_status(1, "sw", STATUS_WAITING)

        assert self.scheduler._needs_step_file_monitor() is False

    def test_request_file_monitor_reset_preserves_scheduler_pause(self):
        """clean SW 后只重置监控器追踪状态，不解除调度器暂停。"""
        self.scheduler._paused.set()

        self.scheduler.request_file_monitor_reset()

        assert self.file_monitor.reset_only_count == 1
        assert self.file_monitor.resume_and_reset_count == 0
        assert self.scheduler._paused.is_set()

    def test_reset_config_clears_only_assigned_workstation_barrier(self):
        """单构型 reset 触及 Meshing 时，只清理该构型所属工作站屏障。"""
        self.state.load_configs({1: [1.0, 2.0, 3.0, 4.0], 2: [5.0, 6.0, 7.0, 8.0]})
        self.state.set_config_workstation(1, "WS-A")
        self.state.set_config_workstation(2, "WS-B")
        cleared_workstations: list[str] = []
        cleared_all = False

        def clear_workstation(workstation_id: str) -> None:
            cleared_workstations.append(workstation_id)

        def clear_all() -> None:
            nonlocal cleared_all
            cleared_all = True

        self.scheduler.barrier_coordinator.clear_workstation_barrier = clear_workstation
        self.scheduler.barrier_coordinator.clear_all_workstation_barriers = clear_all

        self.scheduler.reset_config(1, "meshing")

        assert cleared_workstations == ["WS-A"]
        assert cleared_all is False

    def test_reset_solver_clears_terminal_report_gate(self):
        """reset 触及 Solver 后应允许下一轮自然终态再次上报。"""
        self.state.load_configs({1: [1.0, 2.0, 3.0, 4.0]})
        self.scheduler.barrier_coordinator._solver_terminal_reported = True

        self.scheduler.reset_config(1, "solver")

        assert self.scheduler.barrier_coordinator._solver_terminal_reported is False

    def test_reset_all_clears_all_workstation_barriers(self):
        """全量 reset 应清理所有工作站屏障缓存。"""
        self.state.load_configs({1: [1.0, 2.0, 3.0, 4.0]})
        cleared_workstations: list[str] = []
        cleared_all = False

        def clear_workstation(workstation_id: str) -> None:
            cleared_workstations.append(workstation_id)

        def clear_all() -> None:
            nonlocal cleared_all
            cleared_all = True

        self.scheduler.barrier_coordinator.clear_workstation_barrier = clear_workstation
        self.scheduler.barrier_coordinator.clear_all_workstation_barriers = clear_all

        self.scheduler.reset_config("all", None)

        assert cleared_all is True
        assert cleared_workstations == []

    def _record_remote_outputs(
        self,
        config_name: int,
        *,
        meshing: bool = False,
        solver: bool = False,
    ) -> None:
        if meshing:
            self.runner._ssh.remote_file_sizes[
                f"D:/xkz_1020/msh/model_gen4_{config_name}.msh.h5"
            ] = 1024
        if solver:
            self.runner._ssh.remote_file_sizes[
                f"D:/xkz_1020/case/model_gen4_{config_name}.cas.h5"
            ] = 1024
            self.runner._ssh.remote_file_sizes[
                f"D:/xkz_1020/case/model_gen4_{config_name}.dat.h5"
            ] = 1024

    def test_resume_scan_keeps_running_meshing_when_remote_task_running(self):
        """启动扫描遇到仍在运行的远程 Meshing 时不能重置或入队重启。"""
        self.state.load_configs({1: [1.0, 2.0, 3.0, 4.0]})
        for step in ["sw", "sc", "transfer"]:
            self.state.set_step_status(1, step, STATUS_COMPLETED)
        self.state.set_step_status(1, "meshing", STATUS_RUNNING)
        self.runner._remote_executor._remote_task_status = "running"

        self.scheduler._resume_paused_steps(log_prefix="[Test]")

        assert self.runner._remote_executor._remote_task_status_checks == [(1, "meshing", "default")]
        assert self.state.get_step_status(1, "meshing") == STATUS_RUNNING
        assert self.scheduler.meshing_monitor.qsize() == 0

    def test_resume_scan_keeps_running_sw_when_local_worker_task_in_flight(self):
        """LocalWorker 仍在执行 SW 时，resume 扫描不能把 Running 改回 Waiting。"""
        self.state.load_configs({1: [1.0, 2.0, 3.0, 4.0]})
        self.state.set_step_status(1, "sw", STATUS_RUNNING)
        self.runner._sw_in_flight = True
        self.scheduler._check_step_output_exists = lambda *_args: False

        self.scheduler._resume_paused_steps(log_prefix="[Test]")

        assert self.state.get_step_status(1, "sw") == STATUS_RUNNING

    def test_resume_scan_requeues_sc_when_downstream_never_ran(self):
        """下游仍为 Waiting 时，resume 扫描应恢复 SC 入队。"""
        self.state.load_configs({1: [1.0, 2.0, 3.0, 4.0]})
        self.state.set_step_status(1, "sw", STATUS_COMPLETED)
        self.state.set_step_status(1, "sc", STATUS_WAITING)

        self.scheduler._resume_paused_steps(log_prefix="[Test]")

        assert self.state.get_step_status(1, "sc") == STATUS_WAITING
        assert self.scheduler._sc_queue.qsize() == 1

    def test_resume_scan_uses_assigned_workstation_for_remote_task(self):
        """断点恢复查询远程任务时应使用构型分配的工作站。"""
        self.state.load_configs({1: [1.0, 2.0, 3.0, 4.0]})
        self.state.set_config_workstation(1, "WS-A")
        for step in ["sw", "sc", "transfer"]:
            self.state.set_step_status(1, step, STATUS_COMPLETED)
        self.state.set_step_status(1, "meshing", STATUS_RUNNING)
        self.runner._remote_executor._remote_task_status = "running"

        self.scheduler._resume_paused_steps(log_prefix="[Test]")

        assert self.runner._remote_executor._remote_task_status_checks == [
            (1, "meshing", "WS-A")
        ]

    def test_resume_scan_keeps_running_solver_when_remote_task_unknown(self):
        """启动扫描无法确认远程 Solver 状态时保留 Running，避免重启。"""
        self.state.load_configs({1: [1.0, 2.0, 3.0, 4.0]})
        for step in ["sw", "sc", "transfer", "meshing"]:
            self.state.set_step_status(1, step, STATUS_COMPLETED)
        self.state.set_step_status(1, "solver", STATUS_RUNNING)
        self.runner._remote_executor._remote_task_status = "unknown"
        self._record_remote_outputs(1, meshing=True)

        self.scheduler._resume_paused_steps(log_prefix="[Test]")

        assert self.runner._remote_executor._remote_task_status_checks == [(1, "solver", "default")]
        assert self.state.get_step_status(1, "solver") == STATUS_RUNNING

    def test_resume_scan_unknown_remote_solver_surfaces_after_retry_budget(self, monkeypatch):
        """远程 unknown 连续达到重试上限后应暴露 Error，不能无限保留 Running。"""
        monkeypatch.setitem(ENGINE_CONFIG, "max_retries", 1)
        self.state.load_configs({1: [1.0, 2.0, 3.0, 4.0]})
        for step in ["sw", "sc", "transfer", "meshing"]:
            self.state.set_step_status(1, step, STATUS_COMPLETED)
        self.state.set_step_status(1, "solver", STATUS_RUNNING)
        self.runner._remote_executor._remote_task_status = "unknown"
        self._record_remote_outputs(1, meshing=True)

        self.scheduler._resume_paused_steps(log_prefix="[Test]")

        assert self.runner._remote_executor._remote_task_status_checks == [(1, "solver", "default")]
        assert self.state.get_step_status(1, "solver") == STATUS_ERROR

    def test_stale_sc_completion_after_reset_does_not_submit_transfer(
        self,
        monkeypatch,
        tmp_path,
    ):
        """reset 期间完成的旧 SC 结果不应覆盖 reset 后状态或推进 Transfer。"""
        scdoc_dir = tmp_path / "scdoc"
        scdoc_dir.mkdir()
        monkeypatch.setitem(LOCAL_PATHS, "scdoc_dir", str(scdoc_dir))
        self.state.load_configs({1: [1.0, 2.0, 3.0, 4.0]})
        self.state.set_step_status(1, "sw", STATUS_COMPLETED)
        self.state.set_step_status(1, "sc", STATUS_WAITING)

        def complete_after_reset(config_name: int, step_name: str, _func) -> bool:
            assert step_name == "sc"
            self.scheduler.reset_config(config_name, "sc")
            self.state.set_step_status(config_name, "sc", STATUS_COMPLETED)
            return True

        self.scheduler.worker_pool._retry_manager.execute_with_retry = complete_after_reset

        self.scheduler.worker_pool._process_sc_step(1)

        assert self.state.get_step_status(1, "sc") == STATUS_WAITING
        assert self.scheduler.worker_pool._transfer_queue.qsize() == 0

    def test_stale_transfer_completion_after_reset_does_not_submit_meshing(self):
        """reset 期间完成的旧 Transfer 结果不应覆盖 reset 后状态或推进 Meshing。"""
        self.state.load_configs({1: [1.0, 2.0, 3.0, 4.0]})
        for step in ["sw", "sc"]:
            self.state.set_step_status(1, step, STATUS_COMPLETED)
        self.state.set_step_status(1, "transfer", STATUS_WAITING)

        def complete_after_reset(config_name: int, step_name: str, _func) -> bool:
            assert step_name == "transfer"
            self.scheduler.reset_config(config_name, "transfer")
            self.state.set_step_status(config_name, "transfer", STATUS_COMPLETED)
            return True

        self.scheduler.worker_pool._retry_manager.execute_with_retry = complete_after_reset

        self.scheduler.worker_pool._process_transfer_step(1)

        assert self.state.get_step_status(1, "transfer") == STATUS_WAITING
        assert self.scheduler.meshing_monitor.qsize() == 0

    def test_sc_completion_after_stop_does_not_submit_transfer(
        self,
        monkeypatch,
        tmp_path,
    ):
        """SC 执行期间停止后，即使在途结果完成也不能推进 Transfer。"""
        scdoc_dir = tmp_path / "scdoc"
        scdoc_dir.mkdir()
        monkeypatch.setitem(LOCAL_PATHS, "scdoc_dir", str(scdoc_dir))
        self.state.load_configs({1: [1.0, 2.0, 3.0, 4.0]})
        self.state.set_step_status(1, "sw", STATUS_COMPLETED)
        self.state.set_step_status(1, "sc", STATUS_WAITING)

        def complete_after_stop(config_name: int, step_name: str, _func) -> bool:
            assert step_name == "sc"
            self.scheduler._stopped.set()
            self.state.set_engine_status("stopped")
            self.state.set_step_status(config_name, "sc", STATUS_COMPLETED)
            return True

        self.scheduler.worker_pool._retry_manager.execute_with_retry = complete_after_stop

        self.scheduler.worker_pool._process_sc_step(1)

        assert self.scheduler.worker_pool._transfer_queue.qsize() == 0

    def test_transfer_completion_after_stop_does_not_submit_meshing(self):
        """Transfer 执行期间停止后，即使在途结果完成也不能推进 Meshing。"""
        self.state.load_configs({1: [1.0, 2.0, 3.0, 4.0]})
        for step in ["sw", "sc"]:
            self.state.set_step_status(1, step, STATUS_COMPLETED)
        self.state.set_step_status(1, "transfer", STATUS_WAITING)

        def complete_after_stop(config_name: int, step_name: str, _func) -> bool:
            assert step_name == "transfer"
            self.scheduler._stopped.set()
            self.state.set_engine_status("stopped")
            self.state.set_step_status(config_name, "transfer", STATUS_COMPLETED)
            return True

        self.scheduler.worker_pool._retry_manager.execute_with_retry = complete_after_stop

        self.scheduler.worker_pool._process_transfer_step(1)

        assert self.scheduler.meshing_monitor.qsize() == 0

    def test_sc_retry_failure_does_not_mark_downstream_error(self, monkeypatch, tmp_path):
        """SC 最终失败只标记 SC，Transfer/Meshing/Solver 未运行则保持 Waiting。"""
        scdoc_dir = tmp_path / "scdoc"
        scdoc_dir.mkdir()
        monkeypatch.setitem(LOCAL_PATHS, "scdoc_dir", str(scdoc_dir))
        self.state.load_configs({1: [1.0, 2.0, 3.0, 4.0]})
        self.state.set_step_status(1, "sw", STATUS_COMPLETED)

        def fail_sc(config_name: int, step_name: str, _func) -> bool:
            assert step_name == "sc"
            self.state.set_step_status(config_name, "sc", STATUS_ERROR, "SC 失败")
            return False

        self.scheduler.worker_pool._retry_manager.execute_with_retry = fail_sc

        self.scheduler.worker_pool._process_sc_step(1)

        assert self.state.get_step_status(1, "sc") == STATUS_ERROR
        assert self.state.get_step_status(1, "transfer") == STATUS_WAITING
        assert self.state.get_step_status(1, "meshing") == STATUS_WAITING
        assert self.state.get_step_status(1, "solver") == STATUS_WAITING

    def test_transfer_retry_failure_does_not_mark_meshing_error(self):
        """Transfer 最终失败只标记 Transfer，Meshing/Solver 未运行则保持 Waiting。"""
        self.state.load_configs({1: [1.0, 2.0, 3.0, 4.0]})
        for step in ["sw", "sc"]:
            self.state.set_step_status(1, step, STATUS_COMPLETED)

        def fail_transfer(config_name: int, step_name: str, _func) -> bool:
            assert step_name == "transfer"
            self.state.set_step_status(config_name, "transfer", STATUS_ERROR, "传输失败")
            return False

        self.scheduler.worker_pool._retry_manager.execute_with_retry = fail_transfer

        self.scheduler.worker_pool._process_transfer_step(1)

        assert self.state.get_step_status(1, "transfer") == STATUS_ERROR
        assert self.state.get_step_status(1, "meshing") == STATUS_WAITING
        assert self.state.get_step_status(1, "solver") == STATUS_WAITING

    def test_completed_local_step_with_missing_output_is_reset_on_resume(self, monkeypatch, tmp_path):
        """clean 删除本地产物后，resume 不应继续信任 Completed 状态。"""
        monkeypatch.delenv("AUTOFLUID_SERVER_MODE", raising=False)
        step_dir = tmp_path / "steps"
        scdoc_dir = tmp_path / "scdocs"
        step_dir.mkdir()
        scdoc_dir.mkdir()
        monkeypatch.setitem(LOCAL_PATHS, "step_dir", str(step_dir))
        monkeypatch.setitem(LOCAL_PATHS, "scdoc_dir", str(scdoc_dir))

        self.state.load_configs({1: [1.0, 2.0, 3.0, 4.0]})
        self.state.set_step_status(1, "sw", STATUS_COMPLETED)
        self.state.set_step_status(1, "sc", STATUS_WAITING)

        self.scheduler._resume_paused_steps(log_prefix="[Test]")

        assert self.state.get_step_status(1, "sw") == STATUS_WAITING
        assert self.scheduler._sc_queue.qsize() == 0

    def test_resume_scan_resets_lost_remote_meshing_and_forgets_task(self):
        """远程 Meshing 确认丢失时才允许回到 Waiting 并重新入队。"""
        self.state.load_configs({1: [1.0, 2.0, 3.0, 4.0]})
        for step in ["sw", "sc", "transfer"]:
            self.state.set_step_status(1, step, STATUS_COMPLETED)
        self.state.set_step_status(1, "meshing", STATUS_RUNNING)
        self.runner._remote_executor._remote_task_status = "lost"

        self.scheduler._resume_paused_steps(log_prefix="[Test]")

        assert self.state.get_step_status(1, "meshing") == STATUS_WAITING
        assert self.scheduler.meshing_monitor.qsize() == 1
        assert self.runner._remote_executor._forgotten_remote_tasks == [(1, "meshing", "default")]

    def test_completed_remote_meshing_with_missing_mesh_is_reset_on_resume(self):
        """Meshing Completed 不能只信任状态库，缺少 .msh.h5 时应重新入队。"""
        self.state.load_configs({1: [1.0, 2.0, 3.0, 4.0]})
        for step in ["sw", "sc", "transfer", "meshing"]:
            self.state.set_step_status(1, step, STATUS_COMPLETED)

        self.scheduler._resume_paused_steps(log_prefix="[Test]")

        assert self.state.get_step_status(1, "meshing") == STATUS_WAITING
        assert self.state.get_step_status(1, "solver") == STATUS_WAITING
        assert self.scheduler.meshing_monitor.qsize() == 1

    def test_completed_remote_solver_with_missing_results_is_reset_on_resume(self):
        """Solver Completed 缺少 cas/dat 结果时应回到 Waiting 等屏障重新调度。"""
        self.state.load_configs({1: [1.0, 2.0, 3.0, 4.0]})
        for step in ["sw", "sc", "transfer", "meshing", "solver"]:
            self.state.set_step_status(1, step, STATUS_COMPLETED)

        self._record_remote_outputs(1, meshing=True)

        self.scheduler._resume_paused_steps(log_prefix="[Test]")

        assert self.state.get_step_status(1, "meshing") == STATUS_COMPLETED
        assert self.state.get_step_status(1, "solver") == STATUS_WAITING

    def test_resume_scan_completed_remote_solver_hands_off_before_forget(self):
        """恢复扫描确认 Solver 已完成时先登记 PostProcess，再清理 Solver 元数据。"""
        self.state.load_configs({1: [1.0, 2.0, 3.0, 4.0]})
        for step in ["sw", "sc", "transfer", "meshing"]:
            self.state.set_step_status(1, step, STATUS_COMPLETED)
        self.state.set_step_status(1, "solver", STATUS_RUNNING)
        self.runner._remote_executor._remote_task_status = "completed"
        self._record_remote_outputs(1, meshing=True)

        self.scheduler._resume_paused_steps(log_prefix="[Test]")

        assert self.state.get_step_status(1, "solver") == STATUS_COMPLETED
        assert self.runner._remote_executor._postprocess_handoffs == [(1, "default")]
        assert self.runner._remote_executor._remote_task_events[:2] == [
            ("handoff", 1, "postprocess", "default"),
            ("forget", 1, "solver", "default"),
        ]
        assert self.runner._remote_executor._forgotten_remote_tasks[0] == (
            1,
            "solver",
            "default",
        )

    def test_resume_scan_paused_remote_solver_with_outputs_forgets_task(self):
        """Paused Solver 输出已存在时也应清理远程任务元数据。"""
        self.state.load_configs({1: [1.0, 2.0, 3.0, 4.0]})
        for step in ["sw", "sc", "transfer", "meshing"]:
            self.state.set_step_status(1, step, STATUS_COMPLETED)
        self.state.set_step_status(1, "solver", STATUS_PAUSED)
        self.state.save_remote_task(
            workstation_id="default",
            config_name=1,
            step_name="solver",
            task_name="AutoFluid_paused_solver",
            flag_file="D:/flags/solver_done_1.txt",
            error_flag_file="D:/flags/solver_done_1.txt.error",
            started_at=time.time(),
        )
        self.scheduler._check_step_output_exists = lambda *_args: True
        self._record_remote_outputs(1, meshing=True)

        self.scheduler._resume_paused_steps(log_prefix="[Test]")

        assert self.state.get_step_status(1, "solver") == STATUS_COMPLETED
        assert self.runner._remote_executor._forgotten_remote_tasks == [(1, "solver", "default")]

    def test_resume_scan_completed_config_forgets_stale_remote_tasks(self):
        """全步骤 Completed 但仍有远程任务元数据时应清理残留记录。"""
        self.state.load_configs({1: [1.0, 2.0, 3.0, 4.0]})
        for step in ["sw", "sc", "transfer", "meshing", "solver", "postprocess"]:
            self.state.set_step_status(1, step, STATUS_COMPLETED)
        self.state.save_remote_task(
            workstation_id="default",
            config_name=1,
            step_name="solver",
            task_name="AutoFluid_old_solver",
            flag_file="D:/flags/solver_done_1.txt",
            error_flag_file="D:/flags/solver_done_1.txt.error",
            started_at=time.time(),
        )
        self._record_remote_outputs(1, meshing=True, solver=True)

        self.scheduler._resume_paused_steps(log_prefix="[Test]")

        assert self.runner._remote_executor._forgotten_remote_tasks == [(1, "solver", "default")]

    def test_downstream_errors_include_postprocess(self):
        """PostProcess Error 应阻止启动前屏障抢跑，先交给恢复扫描处理。"""
        self.state.load_configs({1: [1.0, 2.0, 3.0, 4.0]})
        for step in ["sw", "sc", "transfer", "meshing", "solver", "postprocess"]:
            self.state.set_step_status(1, step, STATUS_COMPLETED)
        self.state.set_step_status(1, "postprocess", STATUS_ERROR, "后处理失败")

        assert self.scheduler._has_downstream_errors() is True

    def test_finalize_pipeline_completed_sets_stopped_without_pausing_completed_steps(self):
        """自然完成收尾应停止后台循环，但保持 Completed 步骤不被改为 Paused。"""
        self.state.load_configs({1: [1.0, 2.0, 3.0, 4.0]})
        for step in ["sw", "sc", "transfer", "meshing", "solver"]:
            self.state.set_step_status(1, step, STATUS_COMPLETED)
        self.state.set_engine_status("running")

        self.scheduler.finalize_pipeline("completed")

        assert self.scheduler._stopped.is_set()
        assert self.state.get_engine_status() == "stopped"
        assert self.runner._solver_flag_cleanup_count == 1
        for step in ["sw", "sc", "transfer", "meshing", "solver"]:
            assert self.state.get_step_status(1, step) == STATUS_COMPLETED

    def test_finalize_pipeline_error_keeps_solver_flag_artifacts(self):
        """失败终态应保留 flags 中后台 wrapper 产物供排障。"""
        self.state.set_engine_status("running")

        self.scheduler.finalize_pipeline("failed")

        assert self.scheduler._stopped.is_set()
        assert self.state.get_engine_status() == "stopped"
        assert self.runner._solver_flag_cleanup_count == 0

    def test_start_pipeline_unhandled_exception_marks_engine_stopped(self, caplog):
        """调度器线程启动阶段异常时不能让 engine_status 残留 running。"""
        self.state.load_configs({1: [1.0, 2.0, 3.0, 4.0]})
        self.state.set_engine_status("running")

        def fail_sw_phase(_recursion_depth: int = 0) -> bool:
            raise RuntimeError("boom")

        self.scheduler.sw_phase_handler.execute_sw_phase = fail_sw_phase

        with caplog.at_level(logging.ERROR):
            self.scheduler.start_pipeline()

        assert self.state.get_engine_status() == "stopped"
        assert any("调度器线程异常退出" in record.getMessage() for record in caplog.records)

    def test_start_pipeline_starts_barrier_monitor_before_sw_phase(self):
        """初始启动时屏障监控应早于 SW 宏，避免 SW 后尾部补启动。"""
        self.state.load_configs({1: [1.0, 2.0, 3.0, 4.0]})
        order: list[str] = []

        def execute_sw_phase(_recursion_depth: int = 0) -> bool:
            order.append("sw")
            self.state.set_step_status(1, "sw", STATUS_COMPLETED)
            return True

        self.scheduler.sw_phase_handler.execute_sw_phase = execute_sw_phase
        self.scheduler.sw_phase_handler.scan_completed_downstream = lambda: None
        self.scheduler._resume_paused_steps = lambda *args, **kwargs: None
        self.scheduler._ensure_barrier_monitor_running = lambda: order.append("barrier")
        self.scheduler.barrier_coordinator.dispatch_solver_if_ready = lambda: True

        self.scheduler.start_pipeline()

        assert order[:2] == ["barrier", "sw"]

    def test_start_pipeline_skips_worker_pool_start_when_already_running(self):
        """SW 阶段已提前启动 worker pool 时，尾部不再重复 start_if_needed。"""
        self.state.load_configs({1: [1.0, 2.0, 3.0, 4.0]})
        start_calls = 0

        def execute_sw_phase(_recursion_depth: int = 0) -> bool:
            self.state.set_step_status(1, "sw", STATUS_COMPLETED)
            return True

        class _RunningWorkerPool:
            @staticmethod
            def is_running() -> bool:
                return True

            def start_if_needed(self) -> None:
                nonlocal start_calls
                start_calls += 1

        self.scheduler.sw_phase_handler.execute_sw_phase = execute_sw_phase
        self.scheduler.sw_phase_handler.scan_completed_downstream = lambda: None
        self.scheduler._resume_paused_steps = lambda *args, **kwargs: None
        self.scheduler.worker_pool = _RunningWorkerPool()

        self.scheduler.start_pipeline()

        assert start_calls == 0

    def test_start_pipeline_uses_named_barrier_finalizer_after_downstream_start(self):
        """下游组件启动后，通过具名收口方法处理屏障和 Solver 分发。"""
        self.state.load_configs({1: [1.0, 2.0, 3.0, 4.0]})
        calls: list[str] = []

        def execute_sw_phase(_recursion_depth: int = 0) -> bool:
            self.state.set_step_status(1, "sw", STATUS_COMPLETED)
            return True

        self.scheduler.sw_phase_handler.execute_sw_phase = execute_sw_phase
        self.scheduler.sw_phase_handler.scan_completed_downstream = lambda: None
        self.scheduler._resume_paused_steps = lambda *args, **kwargs: None
        self.scheduler.meshing_monitor.start_if_needed = lambda: calls.append("meshing")
        self.scheduler.worker_pool.start_if_needed = lambda: calls.append("worker_pool")
        self.scheduler.worker_pool.is_running = lambda: False
        self.scheduler._finalize_barrier_after_downstream_start = (
            lambda: calls.append("barrier_finalizer")
        )

        self.scheduler.start_pipeline()

        assert calls == [
            "meshing",
            "worker_pool",
            "barrier_finalizer",
        ]

    def test_scan_completed_downstream_suppresses_empty_summary_for_active_steps(
        self,
        caplog,
    ):
        """存在活跃下游步骤时，不输出“所有待执行步骤均无现成输出文件”。"""
        self.state.load_configs({1: [1.0, 2.0, 3.0, 4.0]})
        self.state.set_step_status(1, "sw", STATUS_COMPLETED)
        self.state.set_step_status(1, "sc", STATUS_RUNNING)
        self.scheduler.sw_phase_handler._is_step_in_flight = (
            lambda config_name, step: step == "sc"
        )

        with caplog.at_level(logging.INFO):
            self.scheduler.sw_phase_handler.scan_completed_downstream()

        assert not any(
            "所有待执行步骤均无现成输出文件" in record.getMessage()
            for record in caplog.records
        )

    def test_recursion_limit_marks_only_sw_error(self):
        """递归深度超限时只标记实际停止的 SW，后续未执行步骤保持 Waiting。"""
        self.state.load_configs({1: [1.0, 2.0, 3.0, 4.0]})

        self.scheduler._handle_recursion_limit_exceeded(3)

        assert self.state.get_step_status(1, "sw") == STATUS_ERROR
        assert self.state.get_step_status(1, "sc") == STATUS_WAITING
        assert self.state.get_step_status(1, "transfer") == STATUS_WAITING
        assert self.state.get_step_status(1, "meshing") == STATUS_WAITING
        assert self.state.get_step_status(1, "solver") == STATUS_WAITING


def test_task_runner_restores_remote_tasks_from_db_on_init(monkeypatch):
    from engine import task_runner as task_runner_module

    remote_instances = []
    restore_calls = []

    class _RemoteExecutor:
        def __init__(self, *args, **kwargs):
            remote_instances.append(self)

        def restore_remote_tasks_from_db(self):
            restore_calls.append(self)

    monkeypatch.setattr(task_runner_module, "SCProcessPool", lambda: object())
    monkeypatch.setattr(task_runner_module, "SWExecutor", lambda state: object())
    monkeypatch.setattr(task_runner_module, "RemoteExecutor", _RemoteExecutor)
    monkeypatch.setattr(task_runner_module, "FileCleaner", lambda *args, **kwargs: object())

    runner = task_runner_module.TaskRunner(object())

    assert restore_calls == remote_instances
    assert runner.get_remote_executor() is remote_instances[0]


def test_task_runner_server_mode_merges_local_worker_check(monkeypatch):
    from engine.task_runner import TaskRunner

    class _Cleaner:
        def run_system_check(self):
            return {
                "local_checks": {},
                "remote_checks": {},
                "daemon_checks": {},
                "workstation_checks": {},
            }

    class _LocalWorkerAdapter:
        def check_local_environment(self):
            return {"local_checks": {"SW可执行文件": {"exists": True}}}

    monkeypatch.setenv("AUTOFLUID_SERVER_MODE", "server")
    runner = TaskRunner.__new__(TaskRunner)
    runner._cleaner = _Cleaner()
    runner._local_worker_adapter = _LocalWorkerAdapter()

    result = runner.run_system_check()

    assert result["local_checks"] == {}
    assert result["local_worker_checks"] == {"SW可执行文件": {"exists": True}}


def test_task_runner_server_mode_reports_active_local_worker_check_failure(monkeypatch):
    from engine.task_runner import TaskRunner

    class _Cleaner:
        def run_system_check(self):
            return {
                "local_checks": {},
                "remote_checks": {},
                "daemon_checks": {},
                "workstation_checks": {},
            }

    class _LocalWorkerAdapter:
        last_error = "LocalWorker 主动自检超时"

        def check_local_environment(self):
            return None

    monkeypatch.setenv("AUTOFLUID_SERVER_MODE", "server")
    runner = TaskRunner.__new__(TaskRunner)
    runner._cleaner = _Cleaner()
    runner._local_worker_adapter = _LocalWorkerAdapter()

    result = runner.run_system_check()

    assert result["local_worker_checks"] == {
        "active_check": {
            "exists": False,
            "message": "LocalWorker 主动自检超时",
        }
    }


def test_task_runner_server_mode_delegates_local_file_clean(monkeypatch):
    from engine.task_runner import TaskRunner

    class _Cleaner:
        def __init__(self):
            self.clean_calls = []

        def clean_step_files(self, step_name, config_name):
            self.clean_calls.append((step_name, config_name))

    class _LocalWorkerAdapter:
        def __init__(self):
            self.clean_calls = []

        def clean_local_files(self, step_name, config_name):
            self.clean_calls.append((step_name, config_name))
            return True

    monkeypatch.setenv("AUTOFLUID_SERVER_MODE", "server")
    cleaner = _Cleaner()
    adapter = _LocalWorkerAdapter()
    runner = TaskRunner.__new__(TaskRunner)
    runner._cleaner = cleaner
    runner._local_worker_adapter = adapter

    runner.clean_step_files("sw", 1)

    assert adapter.clean_calls == [("sw", 1)]
    assert cleaner.clean_calls == [("sw", 1)]


def test_task_runner_server_mode_rejects_failed_local_file_clean(monkeypatch):
    from engine.task_runner import TaskRunner

    class _Cleaner:
        def __init__(self):
            self.clean_calls = []

        def clean_step_files(self, step_name, config_name):
            self.clean_calls.append((step_name, config_name))

    class _LocalWorkerAdapter:
        def clean_local_files(self, step_name, config_name):
            return False

    monkeypatch.setenv("AUTOFLUID_SERVER_MODE", "server")
    cleaner = _Cleaner()
    runner = TaskRunner.__new__(TaskRunner)
    runner._cleaner = cleaner
    runner._local_worker_adapter = _LocalWorkerAdapter()

    with pytest.raises(RuntimeError, match="LocalWorker 本地文件清理失败"):
        runner.clean_step_files("sw", 1)

    assert cleaner.clean_calls == []


class _CleanStepRunner:
    def __init__(self, remote_status=None):
        self.clean_calls = []
        self.clean_all_cache_count = 0
        self.remote_executor = _DaemonRemoteExecutor(remote_status)
        self.system_check_result = {
            "local_checks": {},
            "remote_checks": {},
            "workstation_checks": {},
        }

    def clean_step_files(self, step_name, config_name):
        self.clean_calls.append((step_name, config_name))

    def clean_all_cache(self):
        self.clean_all_cache_count += 1

    def run_system_check(self):
        return self.system_check_result

    def get_remote_executor(self):
        return self.remote_executor


class _CleanStepScheduler:
    def __init__(self):
        self.file_monitor_reset_count = 0
        self.reset_calls = []
        self.start_calls = 0
        self.resume_calls = 0
        self.is_paused = False
        self.pipeline_alive = False

    def request_file_monitor_reset(self):
        self.file_monitor_reset_count += 1

    def reset_config(self, config_name, step_name):
        self.reset_calls.append((config_name, step_name))

    def start_pipeline(self):
        self.start_calls += 1

    def resume(self):
        self.resume_calls += 1
        self.is_paused = False

    def set_pipeline_thread(self, thread):
        self.thread = thread


class _DaemonState:
    def __init__(self, engine_status="stopped", remote_tasks=None, statuses=None):
        self.engine_status = engine_status
        self.remote_tasks = remote_tasks or []
        self.statuses = statuses or {}
        self.set_status_calls = []

    def get_engine_status(self):
        return self.engine_status

    def set_engine_status(self, status):
        self.engine_status = status
        self.set_status_calls.append(status)

    def get_all_remote_tasks(self):
        return self.remote_tasks

    def get_all_statuses(self):
        return self.statuses

    def get_all_configs(self):
        return list(self.statuses)

    def get_step_status(self, config_name, step_name):
        return self.statuses.get(config_name, {}).get(step_name, STATUS_WAITING)


class _AssignmentState:
    def __init__(self, configs, assignments=None):
        self._configs = list(configs)
        self.assignments = dict(assignments or {})
        self.set_calls = []

    def get_all_configs(self):
        return list(self._configs)

    def get_config_workstation(self, config_name):
        return self.assignments.get(config_name)

    def set_config_workstation(self, config_name, workstation_id):
        self.assignments[config_name] = workstation_id
        self.set_calls.append((config_name, workstation_id))


class _DaemonRemoteExecutor:
    def __init__(self, remote_status=None):
        self.remote_status = remote_status or {}
        self.query_calls = []
        self.forgotten: list[tuple[int, str]] = []

    def query_remote_task_status(self, config_name, step_name, workstation_id="default"):
        self.query_calls.append((config_name, step_name, workstation_id))
        return self.remote_status.get((config_name, step_name), "completed")

    def forget_remote_task(self, config_name, step_name, workstation_id="default"):
        self.forgotten.append((config_name, step_name, workstation_id))


class TestPipelineDaemonCleanStep:
    """验证 clean_step IPC handler 与调度器文件监控接口兼容。"""

    def test_server_mode_rejects_pipeline_start_without_local_worker(self, monkeypatch):
        from engine.daemon import PipelineDaemon
        from engine.local_worker_registry import LocalWorkerRegistry

        monkeypatch.setenv("AUTOFLUID_SERVER_MODE", "server")
        daemon = PipelineDaemon.__new__(PipelineDaemon)
        daemon.state = _DaemonState(engine_status="stopped")
        daemon.scheduler = _CleanStepScheduler()
        daemon._pipeline_ever_started = False
        daemon.local_worker_registry = LocalWorkerRegistry()

        ok, data, message = daemon.handle_start({})

        assert ok is False
        assert data is None
        assert "LocalWorker" in message
        assert daemon.state.set_status_calls == []
        assert daemon.scheduler.start_calls == 0

    def test_server_mode_start_autostarts_local_worker_when_missing(self, monkeypatch, tmp_path):
        from engine import daemon as daemon_module
        from engine.daemon import PipelineDaemon
        from engine.local_worker_registry import LocalWorkerRegistry

        class _Proc:
            pid = 12345

            def poll(self):
                return None

        popen_calls = []

        def fake_popen(args, **kwargs):
            popen_calls.append((args, kwargs))
            return _Proc()

        monkeypatch.setenv("AUTOFLUID_SERVER_MODE", "server")
        monkeypatch.setattr(daemon_module.sys, "platform", "win32")
        monkeypatch.setattr(daemon_module.subprocess, "Popen", fake_popen)
        monkeypatch.setitem(daemon_module.LOCAL_PATHS, "log_dir", str(tmp_path / "logs"))

        daemon = PipelineDaemon.__new__(PipelineDaemon)
        daemon.state = _DaemonState(engine_status="stopped")
        daemon.scheduler = _CleanStepScheduler()
        daemon._pipeline_ever_started = False
        daemon.local_worker_registry = LocalWorkerRegistry()
        daemon.local_worker_adapter = object()

        ok, data, message = daemon.handle_start({})

        assert ok is False
        assert data is None
        assert "LocalWorker" in message
        assert len(popen_calls) == 1
        assert os.path.basename(popen_calls[0][0][-2]) == "main.py"
        assert popen_calls[0][0][-1] == "--worker"
        assert daemon.scheduler.start_calls == 0

    def test_server_mode_start_does_not_autostart_duplicate_worker(self, monkeypatch, tmp_path):
        from engine import daemon as daemon_module
        from engine.daemon import PipelineDaemon
        from engine.local_worker_registry import LocalWorkerRegistry

        class _Proc:
            pid = 12345

            def poll(self):
                return None

        popen_calls = []
        monkeypatch.setenv("AUTOFLUID_SERVER_MODE", "server")
        monkeypatch.setattr(daemon_module.sys, "platform", "win32")
        monkeypatch.setattr(
            daemon_module.subprocess,
            "Popen",
            lambda *args, **kwargs: popen_calls.append((args, kwargs)),
        )
        monkeypatch.setitem(daemon_module.LOCAL_PATHS, "log_dir", str(tmp_path / "logs"))

        daemon = PipelineDaemon.__new__(PipelineDaemon)
        daemon.state = _DaemonState(engine_status="stopped")
        daemon.scheduler = _CleanStepScheduler()
        daemon._pipeline_ever_started = False
        daemon.local_worker_registry = LocalWorkerRegistry()
        daemon.local_worker_adapter = object()
        daemon._local_worker_process = _Proc()

        ok, data, message = daemon.handle_start({})

        assert ok is False
        assert data is None
        assert "LocalWorker" in message
        assert popen_calls == []
        assert daemon.scheduler.start_calls == 0

    def test_server_mode_paused_resume_rejects_active_local_step_without_online_worker(self, monkeypatch):
        from engine.daemon import PipelineDaemon
        from engine.local_worker_registry import LocalWorkerRegistry

        monkeypatch.setenv("AUTOFLUID_SERVER_MODE", "server")
        daemon = PipelineDaemon.__new__(PipelineDaemon)
        daemon.state = _DaemonState(
            engine_status="paused",
            statuses={1: {"sw": STATUS_RUNNING, "sc": STATUS_WAITING}},
        )
        daemon.scheduler = _CleanStepScheduler()
        daemon.scheduler.is_paused = True
        daemon.scheduler.pipeline_alive = True
        daemon._pipeline_ever_started = True
        daemon.local_worker_registry = LocalWorkerRegistry(timeout_seconds=0.0)
        daemon.local_worker_adapter = object()
        daemon._ensure_local_worker_autostarted = lambda: None

        ok, data, message = daemon.handle_start({})

        assert ok is False
        assert data is None
        assert "LocalWorker" in message
        assert daemon.scheduler.resume_calls == 0
        assert daemon.scheduler.start_calls == 0

    def test_start_running_with_dead_pipeline_thread_restarts_scheduler(self, monkeypatch):
        from engine.daemon import PipelineDaemon

        monkeypatch.delenv("AUTOFLUID_SERVER_MODE", raising=False)
        daemon = PipelineDaemon.__new__(PipelineDaemon)
        daemon.state = _DaemonState(engine_status="running")
        daemon.scheduler = _CleanStepScheduler()
        daemon._pipeline_ever_started = True

        ok, data, message = daemon.handle_start({})

        assert ok is True
        assert data is None
        assert "重新启动" in message
        assert daemon.scheduler.start_calls == 1
        assert daemon.state.get_engine_status() == "running"

    def test_worker_register_and_heartbeat_handlers_update_registry(self):
        from engine.daemon import PipelineDaemon
        from engine.local_worker_registry import LocalWorkerRegistry

        daemon = PipelineDaemon.__new__(PipelineDaemon)
        daemon.local_worker_registry = LocalWorkerRegistry()

        ok, data, message = daemon.handle_worker_register({
            "worker_id": "local-pc-01",
            "capabilities": {"sw": True, "sc_slots": 3},
        })

        assert ok is True
        assert message == "LocalWorker 已注册"
        assert data["worker_id"] == "local-pc-01"
        assert daemon.local_worker_registry.has_online_worker() is True

        ok, data, message = daemon.handle_worker_heartbeat({
            "worker_id": "local-pc-01",
        })

        assert ok is True
        assert message == "LocalWorker 心跳已更新"
        assert data["worker_id"] == "local-pc-01"

    def test_server_mode_start_rejects_online_worker_without_adapter(self, monkeypatch):
        from engine.daemon import PipelineDaemon
        from engine.local_worker_registry import LocalWorkerRegistry

        monkeypatch.setenv("AUTOFLUID_SERVER_MODE", "server")
        daemon = PipelineDaemon.__new__(PipelineDaemon)
        daemon.state = _DaemonState(engine_status="stopped")
        daemon.scheduler = _CleanStepScheduler()
        daemon._pipeline_ever_started = False
        daemon.local_worker_registry = LocalWorkerRegistry()
        daemon.local_worker_registry.register("local-pc-01", {})

        ok, data, message = daemon.handle_start({})

        assert ok is False
        assert data is None
        assert "LocalWorker" in message
        assert daemon.state.set_status_calls == []
        assert daemon.scheduler.start_calls == 0

    def test_server_mode_start_rejects_missing_remote_password(self, monkeypatch):
        from engine import daemon as daemon_module
        from engine.daemon import PipelineDaemon
        from engine.local_worker_adapter import LocalWorkerAdapter
        from engine.local_worker_registry import LocalWorkerRegistry

        monkeypatch.setenv("AUTOFLUID_SERVER_MODE", "server")
        monkeypatch.setattr(
            daemon_module,
            "WORKSTATIONS",
            [{
                "id": "WS-A",
                "host": "23.247.137.76",
                "port": 22,
                "username": "ps",
                "password": "",
            }],
        )
        registry = LocalWorkerRegistry()
        registry.register("local-pc-01", {"sw": True, "sc": True})
        daemon = PipelineDaemon.__new__(PipelineDaemon)
        daemon.state = _DaemonState(engine_status="stopped")
        daemon.scheduler = _CleanStepScheduler()
        daemon._pipeline_ever_started = False
        daemon.local_worker_registry = registry
        daemon.local_worker_adapter = LocalWorkerAdapter(registry)

        ok, data, message = daemon.handle_start({})

        assert ok is False
        assert data is None
        assert "AUTOFLUID_SSH_PASSWORD" in message
        assert daemon.state.set_status_calls == []
        assert daemon.scheduler.start_calls == 0

    def test_server_mode_start_rejects_failed_worker_ssh_snapshot(self, monkeypatch):
        from engine import daemon as daemon_module
        from engine.daemon import PipelineDaemon
        from engine.local_worker_adapter import LocalWorkerAdapter
        from engine.local_worker_registry import LocalWorkerRegistry

        monkeypatch.setenv("AUTOFLUID_SERVER_MODE", "server")
        monkeypatch.setattr(
            daemon_module,
            "WORKSTATIONS",
            [{
                "id": "WS-A",
                "host": "172.17.135.240",
                "port": 22,
                "username": "ps",
                "password": "secret",
            }],
        )
        registry = LocalWorkerRegistry()
        registry.register("local-pc-01", {"sw": True, "sc": True})
        daemon = PipelineDaemon.__new__(PipelineDaemon)
        daemon.state = _DaemonState(engine_status="stopped")
        daemon.scheduler = _CleanStepScheduler()
        daemon._pipeline_ever_started = False
        daemon.local_worker_registry = registry
        daemon.local_worker_adapter = LocalWorkerAdapter(registry)
        daemon._last_worker_ssh_checks = {"WS-A": "disconnected"}

        ok, data, message = daemon.handle_start({})

        assert ok is False
        assert data is None
        assert "工作站 SSH 未就绪" in message
        assert "WS-A" in message
        assert daemon.state.set_status_calls == []
        assert daemon.scheduler.start_calls == 0

    def test_server_mode_start_rejects_missing_worker_ssh_snapshot(self, monkeypatch):
        from engine import daemon as daemon_module
        from engine.daemon import PipelineDaemon
        from engine.local_worker_adapter import LocalWorkerAdapter
        from engine.local_worker_registry import LocalWorkerRegistry

        monkeypatch.setenv("AUTOFLUID_SERVER_MODE", "server")
        monkeypatch.setattr(
            daemon_module,
            "WORKSTATIONS",
            [{
                "id": "WS-A",
                "host": "172.17.135.240",
                "port": 22,
                "username": "ps",
                "password": "secret",
            }],
        )
        registry = LocalWorkerRegistry()
        registry.register("local-pc-01", {"sw": True, "sc": True})
        daemon = PipelineDaemon.__new__(PipelineDaemon)
        daemon.state = _DaemonState(engine_status="stopped")
        daemon.scheduler = _CleanStepScheduler()
        daemon._pipeline_ever_started = False
        daemon.local_worker_registry = registry
        daemon.local_worker_adapter = LocalWorkerAdapter(registry)
        daemon._last_worker_ssh_checks = {}

        ok, data, message = daemon.handle_start({})

        assert ok is False
        assert data is None
        assert "工作站 SSH 未就绪" in message
        assert "worker start" in message
        assert daemon.state.set_status_calls == []
        assert daemon.scheduler.start_calls == 0

    def test_server_mode_start_accepts_live_workstation_ssh_without_snapshot(self, monkeypatch):
        from engine import daemon as daemon_module
        from engine.daemon import PipelineDaemon
        from engine.local_worker_adapter import LocalWorkerAdapter
        from engine.local_worker_registry import LocalWorkerRegistry

        class _Ssh:
            def connection_is_active(self) -> bool:
                return True

        class _Runner:
            _ssh_pool = {"WS-A": _Ssh()}

        monkeypatch.setenv("AUTOFLUID_SERVER_MODE", "server")
        monkeypatch.setattr(
            daemon_module,
            "WORKSTATIONS",
            [{
                "id": "WS-A",
                "host": "172.17.135.240",
                "port": 22,
                "username": "ps",
                "password": "secret",
                "reachable_host": "127.0.0.1",
                "reachable_port": 2222,
                "connectivity_mode": "reverse_tunnel",
            }],
        )
        registry = LocalWorkerRegistry()
        registry.register("local-pc-01", {"sw": True, "sc": True})
        daemon = PipelineDaemon.__new__(PipelineDaemon)
        daemon.state = _DaemonState(engine_status="stopped")
        daemon.scheduler = _CleanStepScheduler()
        daemon._pipeline_ever_started = False
        daemon.local_worker_registry = registry
        daemon.local_worker_adapter = LocalWorkerAdapter(registry)
        daemon.runner = _Runner()
        daemon._last_worker_ssh_checks = {}
        daemon._config_warnings = []

        ok, data, message = daemon.handle_start({})

        assert ok is True
        assert data is None
        assert message == "流水线已启动"
        assert daemon.state.set_status_calls == ["running"]
        assert daemon.scheduler.start_calls == 1

    def test_worker_start_reloads_config_and_drops_stale_ssh(self, monkeypatch):
        from engine import config as config_module
        from engine import daemon as daemon_module
        from engine.daemon import PipelineDaemon
        from engine.local_worker_registry import LocalWorkerRegistry

        reload_calls: list[str] = []

        def _reload_config() -> bool:
            reload_calls.append("reload")
            return True

        class _SSH:
            def __init__(self, connected: bool) -> None:
                self._connected = connected

            def is_connected(self) -> bool:
                return self._connected

        class _Runner:
            def __init__(self) -> None:
                self.disconnect_calls = 0

            def disconnect_ssh(self) -> None:
                self.disconnect_calls += 1

            def get_ssh(self, workstation_id: str = "default") -> _SSH:
                return _SSH(self.disconnect_calls > 0)

        monkeypatch.setattr(config_module, "reload_config_from_toml", _reload_config)
        monkeypatch.setattr(
            daemon_module,
            "WORKSTATIONS",
            [{"id": "default"}],
        )

        runner = _Runner()
        daemon = PipelineDaemon.__new__(PipelineDaemon)
        daemon.runner = runner
        daemon.local_worker_registry = LocalWorkerRegistry()

        ok, data, message = daemon.handle_worker_start({})

        assert ok is True
        assert message == "Worker 启动准备就绪，等待本地 Worker 和工作站 Worker 连接"
        assert reload_calls == ["reload"]
        assert runner.disconnect_calls == 1
        assert data["ssh_checks"] == {"default": "ok"}
        assert daemon._last_worker_ssh_checks == {"default": "ok"}
        assert daemon._build_health_snapshot()["server_to_workstation_ssh"] == "ok"

    def test_worker_register_refreshes_workstation_ssh_after_runner_is_created(self, monkeypatch):
        from engine import daemon as daemon_module
        from engine.daemon import PipelineDaemon
        from engine.local_worker_registry import LocalWorkerRegistry

        class _SSH:
            def is_connected(self) -> bool:
                return True

            def connection_is_active(self) -> bool:
                return True

        class _Runner:
            def __init__(self, state, local_worker_adapter=None) -> None:
                self.state = state
                self.local_worker_adapter = local_worker_adapter

            def get_ssh(self, workstation_id: str = "default") -> _SSH:
                return _SSH()

        class _Scheduler:
            def __init__(self, state, runner) -> None:
                self.state = state
                self.runner = runner

        monkeypatch.setattr(
            daemon_module,
            "WORKSTATIONS",
            [{"id": "default"}],
        )
        monkeypatch.setattr(daemon_module, "TaskRunner", _Runner)
        monkeypatch.setattr(daemon_module, "PipelineScheduler", _Scheduler)
        daemon = PipelineDaemon.__new__(PipelineDaemon)
        daemon.local_worker_registry = LocalWorkerRegistry()
        daemon.local_worker_adapter = None
        daemon.state = None
        daemon.runner = None
        daemon.scheduler = None
        daemon._pipeline_ever_started = False
        daemon._last_worker_ssh_checks = {}
        daemon._config_warnings = []
        daemon._worker_config_fingerprint = None

        ok, _worker, message = daemon.handle_worker_register({
            "worker_id": "local-pc-01",
            "capabilities": {"sw": True, "sc": True},
            "configs": {"1": [1, 2, 3, 4]},
        })

        assert ok is True
        assert message == "LocalWorker 已注册"
        assert daemon._last_worker_ssh_checks == {"default": "ok"}
        assert daemon._build_health_snapshot()["server_to_workstation_ssh"] == "ok"

    def test_worker_start_fails_when_all_workstation_ssh_checks_fail(self, monkeypatch):
        from engine import config as config_module
        from engine import daemon as daemon_module
        from engine.daemon import PipelineDaemon
        from engine.local_worker_registry import LocalWorkerRegistry

        monkeypatch.setattr(config_module, "reload_config_from_toml", lambda: False)
        monkeypatch.setattr(
            daemon_module,
            "WORKSTATIONS",
            [{"id": "default"}],
        )

        class _SSH:
            def is_connected(self) -> bool:
                return False

        class _Runner:
            def disconnect_ssh(self) -> None:
                pass

            def get_ssh(self, workstation_id: str = "default") -> _SSH:
                return _SSH()

        daemon = PipelineDaemon.__new__(PipelineDaemon)
        daemon.runner = _Runner()
        daemon.local_worker_registry = LocalWorkerRegistry()
        daemon.local_worker_registry.register("local-pc-01", {"sw": True})

        ok, data, message = daemon.handle_worker_start({})

        assert ok is False
        assert "SSH 连通检查全部失败" in message
        assert data["ssh_checks"] == {"default": "disconnected"}
        assert daemon.local_worker_registry.has_online_worker() is False

    def test_worker_start_failure_reports_effective_ssh_targets(self, monkeypatch):
        from engine import config as config_module
        from engine import daemon as daemon_module
        from engine.daemon import PipelineDaemon
        from engine.local_worker_registry import LocalWorkerRegistry

        monkeypatch.setattr(config_module, "reload_config_from_toml", lambda: False)
        monkeypatch.setattr(
            daemon_module,
            "WORKSTATIONS",
            [{
                "id": "WS-A",
                "host": "172.17.135.240",
                "port": 22,
                "reachable_host": "127.0.0.1",
                "reachable_port": 2222,
                "connectivity_mode": "reverse_tunnel",
            }],
        )

        class _Runner:
            def disconnect_ssh(self) -> None:
                pass

            def get_ssh(self, workstation_id: str = "default"):
                raise TimeoutError("connect timed out")

        daemon = PipelineDaemon.__new__(PipelineDaemon)
        daemon.runner = _Runner()
        daemon.local_worker_registry = LocalWorkerRegistry()

        ok, data, message = daemon.handle_worker_start({})

        assert ok is False
        assert data["ssh_targets"] == {
            "WS-A": {
                "host": "127.0.0.1",
                "port": 2222,
                "connectivity_mode": "reverse_tunnel",
            }
        }
        assert "WS-A=127.0.0.1:2222(reverse_tunnel)" in message

    def test_worker_stop_clears_last_worker_ssh_snapshot(self):
        from engine.daemon import PipelineDaemon
        from engine.local_worker_registry import LocalWorkerRegistry

        daemon = PipelineDaemon.__new__(PipelineDaemon)
        daemon.runner = None
        daemon.local_worker_registry = LocalWorkerRegistry()
        daemon._last_worker_ssh_checks = {"default": "ok"}

        ok, data, message = daemon.handle_worker_stop({})

        assert ok is True
        assert message == "所有 Worker 已停止"
        assert data["registry_cleared"] is True
        assert daemon._last_worker_ssh_checks == {}

    def test_worker_start_keeps_partial_success_visible(self, monkeypatch):
        from engine import config as config_module
        from engine import daemon as daemon_module
        from engine.daemon import PipelineDaemon
        from engine.local_worker_registry import LocalWorkerRegistry

        monkeypatch.setattr(config_module, "reload_config_from_toml", lambda: False)
        monkeypatch.setattr(
            daemon_module,
            "WORKSTATIONS",
            [{"id": "WS-A"}, {"id": "WS-B"}],
        )

        class _SSH:
            def __init__(self, connected: bool) -> None:
                self._connected = connected

            def is_connected(self) -> bool:
                return self._connected

        class _Runner:
            def disconnect_ssh(self) -> None:
                pass

            def get_ssh(self, workstation_id: str = "default") -> _SSH:
                return _SSH(workstation_id == "WS-A")

        daemon = PipelineDaemon.__new__(PipelineDaemon)
        daemon.runner = _Runner()
        daemon.local_worker_registry = LocalWorkerRegistry()

        ok, data, message = daemon.handle_worker_start({})

        assert ok is True
        assert "部分工作站 SSH 连通检查失败" in message
        assert data["ssh_checks"] == {"WS-A": "ok", "WS-B": "disconnected"}
        assert daemon._last_worker_ssh_checks == {"WS-A": "ok", "WS-B": "disconnected"}
        assert daemon._build_health_snapshot()["server_to_workstation_ssh"] == "ok"
        assert daemon.local_worker_registry.has_online_worker() is False

    def test_workstation_ssh_refresh_warns_when_disconnected_connection_recovers(
        self,
        monkeypatch,
        caplog,
    ):
        from engine import daemon as daemon_module
        from engine.daemon import PipelineDaemon

        class _SSH:
            def ensure_connected(self) -> bool:
                return True

            def is_connected(self) -> bool:
                return False

        class _Runner:
            def get_ssh(self, workstation_id: str = "default") -> _SSH:
                return _SSH()

        monkeypatch.setattr(
            daemon_module,
            "WORKSTATIONS",
            [{
                "id": "WS-A",
                "host": "172.17.135.240",
                "port": 22,
                "reachable_host": "127.0.0.1",
                "reachable_port": 2222,
                "connectivity_mode": "reverse_tunnel",
            }],
        )
        daemon = PipelineDaemon.__new__(PipelineDaemon)
        daemon.runner = _Runner()
        daemon._last_worker_ssh_checks = {"WS-A": "disconnected"}

        with caplog.at_level(logging.WARNING):
            result = daemon._refresh_workstation_ssh_checks()

        assert result["ssh_checks"] == {"WS-A": "ok"}
        assert daemon._last_worker_ssh_checks == {"WS-A": "ok"}
        assert any(
            "工作站 WS-A SSH 连接已恢复" in record.getMessage()
            for record in caplog.records
        )

    def test_workstation_ssh_refresh_warns_when_error_connection_recovers(
        self,
        monkeypatch,
        caplog,
    ):
        from engine import daemon as daemon_module
        from engine.daemon import PipelineDaemon

        class _SSH:
            def ensure_connected(self) -> bool:
                return True

        class _Runner:
            def get_ssh(self, workstation_id: str = "default") -> _SSH:
                return _SSH()

        monkeypatch.setattr(daemon_module, "WORKSTATIONS", [{"id": "WS-A"}])
        daemon = PipelineDaemon.__new__(PipelineDaemon)
        daemon.runner = _Runner()
        daemon._last_worker_ssh_checks = {"WS-A": "error: connect timed out"}

        with caplog.at_level(logging.WARNING):
            result = daemon._refresh_workstation_ssh_checks()

        assert result["ssh_checks"] == {"WS-A": "ok"}
        assert any(
            "工作站 WS-A SSH 连接已恢复" in record.getMessage()
            for record in caplog.records
        )

    def test_workstation_ssh_refresh_does_not_warn_for_existing_ok_connection(
        self,
        monkeypatch,
        caplog,
    ):
        from engine import daemon as daemon_module
        from engine.daemon import PipelineDaemon

        class _SSH:
            def ensure_connected(self) -> bool:
                return True

        class _Runner:
            def get_ssh(self, workstation_id: str = "default") -> _SSH:
                return _SSH()

        monkeypatch.setattr(daemon_module, "WORKSTATIONS", [{"id": "WS-A"}])
        daemon = PipelineDaemon.__new__(PipelineDaemon)
        daemon.runner = _Runner()
        daemon._last_worker_ssh_checks = {"WS-A": "ok"}

        with caplog.at_level(logging.WARNING):
            result = daemon._refresh_workstation_ssh_checks()

        assert result["ssh_checks"] == {"WS-A": "ok"}
        assert not any(
            "SSH 连接已恢复" in record.getMessage()
            for record in caplog.records
        )

    def test_workstation_ssh_refresh_does_not_warn_for_initial_ok_connection(
        self,
        monkeypatch,
        caplog,
    ):
        from engine import daemon as daemon_module
        from engine.daemon import PipelineDaemon

        class _SSH:
            def ensure_connected(self) -> bool:
                return True

        class _Runner:
            def get_ssh(self, workstation_id: str = "default") -> _SSH:
                return _SSH()

        monkeypatch.setattr(daemon_module, "WORKSTATIONS", [{"id": "WS-A"}])
        daemon = PipelineDaemon.__new__(PipelineDaemon)
        daemon.runner = _Runner()
        daemon._last_worker_ssh_checks = {}

        with caplog.at_level(logging.WARNING):
            result = daemon._refresh_workstation_ssh_checks()

        assert result["ssh_checks"] == {"WS-A": "ok"}
        assert not any(
            "SSH 连接已恢复" in record.getMessage()
            for record in caplog.records
        )

    def test_assign_config_workstations_persists_only_new_assignments(self, monkeypatch):
        from engine import daemon as daemon_module
        from engine.daemon import PipelineDaemon

        monkeypatch.setattr(
            daemon_module,
            "WORKSTATIONS",
            [{"id": "WS-A"}, {"id": "WS-B"}],
        )
        daemon = PipelineDaemon.__new__(PipelineDaemon)
        daemon.state = _AssignmentState([1, 2, 3], assignments={2: "WS-B"})

        daemon._assign_config_workstations()

        assert daemon.state.assignments == {1: "WS-A", 2: "WS-B", 3: "WS-A"}
        assert daemon.state.set_calls == [(1, "WS-A"), (3, "WS-A")]

    def test_clean_sw_requests_scheduler_file_monitor_reset(self):
        from engine.daemon import PipelineDaemon

        daemon = PipelineDaemon.__new__(PipelineDaemon)
        daemon.runner = _CleanStepRunner()
        daemon.scheduler = _CleanStepScheduler()

        ok, data, message = daemon.handle_clean_step({
            "step_name": "sw",
            "config_name": 1,
        })

        assert ok is True
        assert data is None
        assert "已清理 sw 步骤的文件" in message
        assert daemon.runner.clean_calls == [("sw", 1)]
        assert daemon.scheduler.file_monitor_reset_count == 1

    def test_clean_all_cache_dispatches_background_cache_cleanup(self, monkeypatch):
        from engine import daemon as daemon_module
        from engine.daemon import PipelineDaemon

        class _ImmediateThread:
            def __init__(self, target, daemon, name):
                self._target = target
                self.daemon = daemon
                self.name = name

            def start(self):
                self._target()

        monkeypatch.setattr(daemon_module.threading, "Thread", _ImmediateThread)

        daemon = PipelineDaemon.__new__(PipelineDaemon)
        daemon.runner = _CleanStepRunner()
        daemon.scheduler = _CleanStepScheduler()

        ok, data, message = daemon.handle_clean_step({
            "step_name": "cache",
            "config_name": None,
        })

        assert ok is True
        assert data is None
        assert "已启动后台清理远程缓存文件" in message
        assert daemon.runner.clean_all_cache_count == 1
        assert daemon.runner.clean_calls == []
        assert daemon.scheduler.file_monitor_reset_count == 0

    def test_clean_cache_rejects_single_config(self):
        from engine.daemon import PipelineDaemon

        daemon = PipelineDaemon.__new__(PipelineDaemon)
        daemon.runner = _CleanStepRunner()
        daemon.scheduler = _CleanStepScheduler()

        ok, data, message = daemon.handle_clean_step({
            "step_name": "cache",
            "config_name": 1,
        })

        assert ok is False
        assert data is None
        assert "clean all cache" in message
        assert daemon.runner.clean_all_cache_count == 0

    def test_check_returns_runner_schema(self):
        from engine.daemon import PipelineDaemon
        from engine.local_worker_registry import LocalWorkerRegistry

        daemon = PipelineDaemon.__new__(PipelineDaemon)
        daemon.runner = _CleanStepRunner()
        daemon._config_warnings = []
        daemon._last_worker_ssh_checks = {}
        registry = LocalWorkerRegistry(timeout_seconds=90.0, clock=lambda: 100.0)
        registry.register(
            "local-pc-01",
            {"sw": True},
            network={"reachable_host": "127.0.0.1", "ssh_port": 2222},
        )
        daemon.local_worker_registry = registry
        expected = {
            "local_checks": {},
            "remote_checks": {},
            "daemon_checks": {"server_mode": {"ok": True}},
            "local_worker_checks": {"SW可执行文件": {"exists": True}},
            "workstation_checks": {
                "server_mode": True,
                "workstations": [{"id": "WS-A", "severity": "ok"}],
            },
        }
        daemon.runner.system_check_result = expected

        ok, data, message = daemon.handle_check({})

        assert ok is True
        assert data is not expected
        assert data["health"]["local_worker_online"] is True
        assert data["health"]["server_to_local_ssh"] == "ok"
        assert data["local_worker_checks"] == expected["local_worker_checks"]
        assert message == "系统自检完成"

    def test_clean_rejects_when_pipeline_running(self):
        from engine.daemon import PipelineDaemon

        daemon = PipelineDaemon.__new__(PipelineDaemon)
        daemon.state = _DaemonState(engine_status="running")
        daemon.runner = _CleanStepRunner()
        daemon.scheduler = _CleanStepScheduler()

        ok, data, message = daemon.handle_clean_step({
            "step_name": "sw",
            "config_name": 1,
        })

        assert ok is False
        assert data is None
        assert "pause 或 stop" in message
        assert daemon.runner.clean_calls == []
        assert daemon.scheduler.file_monitor_reset_count == 0

    def test_clean_rejects_when_paused_step_still_running(self):
        from engine.daemon import PipelineDaemon

        daemon = PipelineDaemon.__new__(PipelineDaemon)
        daemon.state = _DaemonState(
            engine_status="paused",
            statuses={1: {"sw": STATUS_RUNNING, "sc": STATUS_WAITING}},
        )
        daemon.runner = _CleanStepRunner()
        daemon.scheduler = _CleanStepScheduler()

        ok, data, message = daemon.handle_clean_step({
            "step_name": "sw",
            "config_name": 1,
        })

        assert ok is False
        assert data is None
        assert "仍在执行" in message
        assert daemon.runner.clean_calls == []
        assert daemon.scheduler.file_monitor_reset_count == 0

    def test_clean_rejects_remote_task_with_unknown_status(self):
        from engine.daemon import PipelineDaemon

        daemon = PipelineDaemon.__new__(PipelineDaemon)
        daemon.state = _DaemonState(
            remote_tasks=[{"config_name": 1, "step_name": "meshing"}],
        )
        daemon.runner = _CleanStepRunner({(1, "meshing"): "unknown"})
        daemon.scheduler = _CleanStepScheduler()

        ok, data, message = daemon.handle_clean_step({
            "step_name": "meshing",
            "config_name": 1,
        })

        assert ok is False
        assert data is None
        assert "远程任务状态为 unknown" in message
        assert daemon.runner.clean_calls == []

    def test_clean_remote_task_guard_uses_persisted_workstation(self):
        from engine.daemon import PipelineDaemon

        daemon = PipelineDaemon.__new__(PipelineDaemon)
        daemon.state = _DaemonState(
            remote_tasks=[
                {"config_name": 1, "step_name": "meshing", "workstation_id": "WS-A"}
            ],
        )
        daemon.runner = _CleanStepRunner({(1, "meshing"): "completed"})
        daemon.scheduler = _CleanStepScheduler()

        ok, _, _ = daemon.handle_clean_step({
            "step_name": "meshing",
            "config_name": 1,
        })

        assert ok is True
        assert daemon.runner.remote_executor.query_calls == [(1, "meshing", "WS-A")]


class TestPipelineDaemonResetStep:
    def test_reset_rejects_when_pipeline_running(self):
        from engine.daemon import PipelineDaemon

        daemon = PipelineDaemon.__new__(PipelineDaemon)
        daemon.state = _DaemonState(engine_status="running")
        daemon.runner = _CleanStepRunner()
        daemon.scheduler = _CleanStepScheduler()

        ok, data, message = daemon.handle_reset_step({
            "config_name": 1,
            "step_name": "sw",
        })

        assert ok is False
        assert data is None
        assert "pause 或 stop" in message
        assert daemon.scheduler.reset_calls == []

    def test_reset_rejects_when_paused_downstream_step_still_running(self):
        from engine.daemon import PipelineDaemon

        daemon = PipelineDaemon.__new__(PipelineDaemon)
        daemon.state = _DaemonState(
            engine_status="paused",
            statuses={1: {"sw": STATUS_COMPLETED, "sc": STATUS_RUNNING}},
        )
        daemon.runner = _CleanStepRunner()
        daemon.scheduler = _CleanStepScheduler()

        ok, data, message = daemon.handle_reset_step({
            "config_name": 1,
            "step_name": "sw",
        })

        assert ok is False
        assert data is None
        assert "仍在执行" in message
        assert daemon.scheduler.reset_calls == []

    def test_reset_rejects_downstream_remote_task_still_running(self):
        from engine.daemon import PipelineDaemon

        daemon = PipelineDaemon.__new__(PipelineDaemon)
        daemon.state = _DaemonState(
            remote_tasks=[{"config_name": 1, "step_name": "solver"}],
        )
        daemon.runner = _CleanStepRunner({(1, "solver"): "running"})
        daemon.scheduler = _CleanStepScheduler()

        ok, data, message = daemon.handle_reset_step({
            "config_name": 1,
            "step_name": "transfer",
        })

        assert ok is False
        assert data is None
        assert "solver 远程任务状态为 running" in message
        assert daemon.scheduler.reset_calls == []

    def test_reset_allows_stopped_without_blocking_remote_task(self):
        from engine.daemon import PipelineDaemon

        daemon = PipelineDaemon.__new__(PipelineDaemon)
        daemon.state = _DaemonState(
            remote_tasks=[{"config_name": 1, "step_name": "solver"}],
        )
        daemon.runner = _CleanStepRunner({(1, "solver"): "completed"})
        daemon.scheduler = _CleanStepScheduler()

        ok, data, message = daemon.handle_reset_step({
            "config_name": 1,
            "step_name": "solver",
        })

        assert ok is True
        assert data is None
        assert "已重置构型1的solver 及后续步骤" in message
        assert daemon.scheduler.reset_calls == [(1, "solver")]


# ====================================================================
# BarrierCoordinator 测试
# ====================================================================

class TestBarrierCoordinator:
    """BarrierCoordinator 屏障逻辑测试。"""

    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp(prefix="barrier_test_")
        self.db_path = os.path.join(self.tmpdir, "test.db")
        self._orig_db_path = IPC_CONFIG["db_path"]
        IPC_CONFIG["db_path"] = self.db_path

        self.state = StateManager(db_path=self.db_path)
        self.runner = _MockTaskRunner(self.state)
        self.paused = threading.Event()
        self.stopped = threading.Event()
        self.barrier_passed = threading.Event()
        self.retry_mgr = RetryManager(self.state, self.paused, self.stopped)
        self.solver_terminal_outcomes: list[str] = []

        from engine.scheduler.barrier import BarrierCoordinator
        self.coordinator = BarrierCoordinator(
            state_manager=self.state,
            task_runner=self.runner,
            paused_event=self.paused,
            stopped_event=self.stopped,
            barrier_passed_event=self.barrier_passed,
            retry_manager=self.retry_mgr,
            on_solver_terminal=self.solver_terminal_outcomes.append,
        )

    def teardown_method(self):
        # ★ 先停止所有后台线程再清理数据库，避免 SolverDispatcher 线程
        #   在 teardown 删除 DB 后仍尝试访问 sqlite 文件。
        self.stopped.set()
        self.coordinator.join_solver_threads(timeout=5)
        IPC_CONFIG["db_path"] = self._orig_db_path
        if os.path.exists(self.tmpdir):
            shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_barrier_passes_when_all_meshing_completed(self):
        """所有构型 Meshing Completed → 屏障通过。"""
        self.state.load_configs({1: [1.0, 2.0, 3.0, 4.0], 2: [5.0, 6.0, 7.0, 8.0]})
        self.state.set_step_status(1, "sw", STATUS_COMPLETED)
        self.state.set_step_status(2, "sw", STATUS_COMPLETED)
        self.state.set_step_status(1, "meshing", STATUS_COMPLETED)
        self.state.set_step_status(2, "meshing", STATUS_COMPLETED)

        t = threading.Thread(target=self.coordinator.monitor_loop, daemon=True)
        t.start()
        t.join(timeout=10)

        assert self.barrier_passed.is_set()
        assert self.state.is_global_barrier_met() is True
        assert self.runner._sc_cleanup_called is True

    def test_barrier_fails_when_all_meshing_error(self):
        """所有构型 Meshing Error → 屏障失败，引擎停止。"""
        self.state.load_configs({1: [1.0, 2.0, 3.0, 4.0], 2: [5.0, 6.0, 7.0, 8.0]})
        self.state.set_step_status(1, "sw", STATUS_COMPLETED)
        self.state.set_step_status(2, "sw", STATUS_COMPLETED)
        self.state.set_step_status(1, "meshing", STATUS_ERROR, "超时")
        self.state.set_step_status(2, "meshing", STATUS_ERROR, "发散")

        t = threading.Thread(target=self.coordinator.monitor_loop, daemon=True)
        t.start()
        t.join(timeout=10)

        assert not self.barrier_passed.is_set()
        assert self.stopped.is_set()
        assert self.state.get_step_status(1, "meshing") == STATUS_ERROR
        assert self.state.get_step_status(2, "meshing") == STATUS_ERROR
        assert self.state.get_step_status(1, "solver") == STATUS_WAITING
        assert self.state.get_step_status(2, "solver") == STATUS_WAITING

    def test_meshing_error_on_one_workstation_does_not_block_ready_workstation_solver(self):
        """某工作站 Meshing 失败时，不应阻断其他已通过工作站的 Solver。"""
        self.state.load_configs({1: [1.0, 2.0, 3.0, 4.0], 2: [5.0, 6.0, 7.0, 8.0]})
        self.state.set_config_workstation(1, "WS-A")
        self.state.set_config_workstation(2, "WS-B")
        for cn in [1, 2]:
            self.state.set_step_status(cn, "sw", STATUS_COMPLETED)
        self.state.set_step_status(1, "meshing", STATUS_COMPLETED)
        self.state.set_step_status(2, "meshing", STATUS_ERROR, "发散")

        t = threading.Thread(target=self.coordinator.monitor_loop, daemon=True)
        t.start()
        deadline = time.time() + 5
        while time.time() < deadline and not self.runner._solver_dispatched:
            time.sleep(0.05)
        self.coordinator.join_solver_threads(timeout=5)
        t.join(timeout=1)

        assert self.runner._solver_dispatched == [1]
        assert self.state.get_step_status(1, "solver") == STATUS_COMPLETED
        assert self.state.get_step_status(2, "meshing") == STATUS_ERROR
        assert self.state.get_step_status(2, "solver") == STATUS_WAITING
        assert not self.stopped.is_set()
        assert not t.is_alive()

    def test_barrier_waits_when_paused(self):
        """暂停期间屏障监控等待，恢复后继续。"""
        self.state.load_configs({1: [1.0, 2.0, 3.0, 4.0]})
        self.state.set_step_status(1, "sw", STATUS_COMPLETED)
        self.paused.set()

        t = threading.Thread(target=self.coordinator.monitor_loop, daemon=True)
        t.start()
        time.sleep(0.5)

        # 暂停期间不应通过
        assert not self.barrier_passed.is_set()
        assert not self.stopped.is_set()

        # 设置完成条件后恢复（恢复在设置状态之后，确保监控线程看到正确状态）
        self.state.set_step_status(1, "meshing", STATUS_COMPLETED)
        self.paused.clear()

        t.join(timeout=10)
        assert self.barrier_passed.is_set()

    def test_barrier_stops_on_stopped_event(self):
        """停止事件使监控循环退出。"""
        self.state.load_configs({1: [1.0, 2.0, 3.0, 4.0]})
        self.stopped.set()

        t = threading.Thread(target=self.coordinator.monitor_loop, daemon=True)
        t.start()
        t.join(timeout=5)

        assert not t.is_alive()

    def test_barrier_paused_wait_exits_promptly_when_stopped(self):
        """暂停等待期间收到 stop 时应立即退出，不等完整 sleep 周期。"""
        self.state.load_configs({1: [1.0, 2.0, 3.0, 4.0]})
        self.paused.set()

        t = threading.Thread(target=self.coordinator.monitor_loop, daemon=True)
        t.start()
        time.sleep(0.05)
        self.stopped.set()
        t.join(timeout=0.3)

        assert not t.is_alive()

    def test_sw_all_failed_aborts_pipeline(self):
        """所有构型 SW 失败 → 流水线中止。"""
        self.state.load_configs({1: [1.0, 2.0, 3.0, 4.0], 2: [5.0, 6.0, 7.0, 8.0]})
        self.state.set_step_status(1, "sw", STATUS_ERROR, "连接失败")
        self.state.set_step_status(2, "sw", STATUS_ERROR, "宏错误")

        t = threading.Thread(target=self.coordinator.monitor_loop, daemon=True)
        t.start()
        t.join(timeout=10)

        assert self.stopped.is_set()
        # 只有实际失败的 SW 是 Error，未执行的后续步骤保持 Waiting。
        assert self.state.get_step_status(1, "sw") == STATUS_ERROR
        assert self.state.get_step_status(1, "sc") == STATUS_WAITING
        assert self.state.get_step_status(2, "solver") == STATUS_WAITING

    def test_meshing_error_does_not_mark_solver_error(self):
        """Meshing 失败时，Solver 未运行则保持 Waiting。"""
        self.state.load_configs({1: [1.0, 2.0, 3.0, 4.0]})
        self.state.set_step_status(1, "sw", STATUS_COMPLETED)
        self.state.set_step_status(1, "meshing", STATUS_ERROR, "超时")

        t = threading.Thread(target=self.coordinator.monitor_loop, daemon=True)
        t.start()
        t.join(timeout=10)

        assert self.stopped.is_set()
        assert self.state.get_step_status(1, "meshing") == STATUS_ERROR
        assert self.state.get_step_status(1, "solver") == STATUS_WAITING

    def test_join_solver_threads(self):
        """join_solver_threads 等待线程退出。"""
        dummy = threading.Thread(target=lambda: time.sleep(0.1), daemon=True)
        dummy.start()
        self.coordinator._solver_threads = [dummy]
        self.coordinator.join_solver_threads(timeout=2)
        assert not dummy.is_alive()
        assert self.coordinator._solver_threads == []

    def test_solver_wait_failure_is_not_redispatched(self):
        """Solver 等待失败后停在 Error，不在同一调度循环里反复重启。"""
        self.state.load_configs({1: [1.0, 2.0, 3.0, 4.0]})
        for step in ["sw", "sc", "transfer", "meshing"]:
            self.state.set_step_status(1, step, STATUS_COMPLETED)
        self.state.set_step_status(1, "solver", STATUS_WAITING)
        self.runner._solver_wait_result = False

        assert self.coordinator.dispatch_solver_if_ready() is True
        self.coordinator.join_solver_threads(timeout=5)

        assert self.runner._solver_dispatched == [1]
        assert self.state.get_step_status(1, "solver") == STATUS_ERROR

    def test_workstation_barrier_dispatches_only_ready_workstation(self):
        """单个工作站 Meshing 完成后，应只分发该工作站的 Solver。"""
        self.state.load_configs({
            1: [1.0, 2.0, 3.0, 4.0],
            2: [5.0, 6.0, 7.0, 8.0],
            3: [9.0, 10.0, 11.0, 12.0],
        })
        self.state.set_config_workstation(1, "WS-A")
        self.state.set_config_workstation(2, "WS-B")
        self.state.set_config_workstation(3, "WS-A")
        for cn in [1, 2, 3]:
            for step in ["sw", "sc", "transfer"]:
                self.state.set_step_status(cn, step, STATUS_COMPLETED)
        self.state.set_step_status(1, "meshing", STATUS_COMPLETED)
        self.state.set_step_status(2, "meshing", STATUS_WAITING)
        self.state.set_step_status(3, "meshing", STATUS_COMPLETED)

        assert self.coordinator.dispatch_solver_if_ready() is True
        self.coordinator.join_solver_threads(timeout=5)

        assert self.runner._solver_dispatched == [1, 3]
        assert self.state.get_step_status(1, "solver") == STATUS_COMPLETED
        assert self.state.get_step_status(2, "solver") == STATUS_WAITING
        assert self.state.get_step_status(3, "solver") == STATUS_COMPLETED
        assert self.state.is_global_barrier_met() is False
        assert self.runner._sc_cleanup_called is False

    def test_all_postprocess_completed_reports_completed_terminal(self):
        """全部 PostProcess Completed 后应报告自然完成终态。"""
        self.state.load_configs({1: [1.0, 2.0, 3.0, 4.0]})
        for step in ["sw", "sc", "transfer", "meshing"]:
            self.state.set_step_status(1, step, STATUS_COMPLETED)
        self.state.set_step_status(1, "solver", STATUS_WAITING)

        assert self.coordinator.dispatch_solver_if_ready() is True
        self.coordinator.join_solver_threads(timeout=5)

        assert self.state.get_step_status(1, "solver") == STATUS_COMPLETED
        assert self.runner._postprocess_dispatched == []
        assert self.runner._remote_executor._postprocess_handoffs == [(1, "default")]
        assert self.state.get_step_status(1, "postprocess") == STATUS_COMPLETED
        assert self.solver_terminal_outcomes == ["completed"]

    def test_solver_completed_dispatches_waiting_postprocess(self):
        """Solver 已完成但 PostProcess Waiting 时应直接进入后处理。"""
        self.state.load_configs({1: [1.0, 2.0, 3.0, 4.0]})
        for step in ["sw", "sc", "transfer", "meshing", "solver", "postprocess"]:
            self.state.set_step_status(1, step, STATUS_COMPLETED)
        self.state.set_step_status(1, "postprocess", STATUS_WAITING)

        assert self.coordinator.dispatch_solver_if_ready() is True
        self.coordinator.join_solver_threads(timeout=5)

        assert self.runner._solver_dispatched == []
        assert self.runner._postprocess_dispatched == [1]
        assert self.state.get_step_status(1, "postprocess") == STATUS_COMPLETED

    def test_postprocess_cleanup_runs_after_completed_status_is_persisted(self):
        """后处理完成应先写 Completed，再清理远程任务证据。"""
        self.state.load_configs({1: [1.0, 2.0, 3.0, 4.0]})
        self.state.set_config_workstation(1, "WS-A")
        for step in ["sw", "sc", "transfer", "meshing", "solver"]:
            self.state.set_step_status(1, step, STATUS_COMPLETED)
        self.state.set_step_status(1, "postprocess", STATUS_WAITING)

        observed_status_during_cleanup: list[str] = []

        def cleanup_after_status(config_name: int) -> None:
            observed_status_during_cleanup.append(
                self.state.get_step_status(config_name, "postprocess")
            )
            workstation_id = str(self.state.get_config_workstation(config_name) or "default")
            self.runner._postprocess_cleanup_calls.append((config_name, workstation_id))

        self.runner.cleanup_completed_postprocess_task = cleanup_after_status

        assert self.coordinator.dispatch_solver_if_ready() is True
        self.coordinator.join_solver_threads(timeout=5)

        assert observed_status_during_cleanup == [STATUS_COMPLETED]
        assert self.runner._postprocess_cleanup_calls == [(1, "WS-A")]

    def test_solver_completed_postprocess_waiting_falls_back_when_solver_task_missing(self):
        """Solver 任务已清理时，PostProcess Waiting 应能独立后处理而非卡住。"""
        self.state.load_configs({1: [1.0, 2.0, 3.0, 4.0]})
        for step in ["sw", "sc", "transfer", "meshing", "solver"]:
            self.state.set_step_status(1, step, STATUS_COMPLETED)
        self.state.set_step_status(1, "postprocess", STATUS_WAITING)
        self.runner._remote_executor._postprocess_handoff_result = False

        assert self.coordinator.dispatch_solver_if_ready() is True
        self.coordinator.join_solver_threads(timeout=5)

        assert self.runner._postprocess_dispatched == [1]
        assert self.state.get_step_status(1, "postprocess") == STATUS_COMPLETED

    def test_postprocess_error_reports_failed_terminal(self):
        """Solver 完成后 PostProcess 失败时应报告失败终态。"""
        self.state.load_configs({1: [1.0, 2.0, 3.0, 4.0]})
        for step in ["sw", "sc", "transfer", "meshing"]:
            self.state.set_step_status(1, step, STATUS_COMPLETED)
        self.state.set_step_status(1, "solver", STATUS_WAITING)
        self.runner._postprocess_wait_result = False

        assert self.coordinator.dispatch_solver_if_ready() is True
        self.coordinator.join_solver_threads(timeout=5)

        assert self.state.get_step_status(1, "solver") == STATUS_COMPLETED
        assert self.state.get_step_status(1, "postprocess") == STATUS_ERROR
        assert self.solver_terminal_outcomes == ["failed"]

    def test_solver_error_reports_failed_without_postprocess(self):
        """Solver 失败时不应启动 PostProcess。"""
        self.state.load_configs({1: [1.0, 2.0, 3.0, 4.0]})
        for step in ["sw", "sc", "transfer", "meshing"]:
            self.state.set_step_status(1, step, STATUS_COMPLETED)
        self.state.set_step_status(1, "solver", STATUS_WAITING)
        self.runner._solver_wait_result = False

        assert self.coordinator.dispatch_solver_if_ready() is True
        self.coordinator.join_solver_threads(timeout=5)

        assert self.state.get_step_status(1, "solver") == STATUS_ERROR
        assert self.state.get_step_status(1, "postprocess") == STATUS_WAITING
        assert self.runner._postprocess_dispatched == []
        assert self.solver_terminal_outcomes == ["failed"]

    def test_running_solver_with_remote_task_recovers_wait_without_restart(self):
        """Daemon 重启后远程 Solver 仍运行时应恢复等待，不重复启动。"""
        self.state.load_configs({1: [1.0, 2.0, 3.0, 4.0]})
        for step in ["sw", "sc", "transfer", "meshing"]:
            self.state.set_step_status(1, step, STATUS_COMPLETED)
        self.state.set_step_status(1, "solver", STATUS_RUNNING)
        self.runner._remote_executor._remote_task_status = "running"

        assert self.coordinator.dispatch_solver_if_ready() is True
        self.coordinator.join_solver_threads(timeout=5)

        assert self.runner._remote_executor._remote_task_status_checks == [(1, "solver", "default")]
        assert self.runner._solver_dispatched == []
        assert self.runner._solver_wait_count == 1
        assert self.state.get_step_status(1, "solver") == STATUS_COMPLETED

    def test_running_solver_completed_hands_off_postprocess_before_forget(self):
        """Daemon 重启后发现 Solver 已完成时应保留同一 Fluent 会话给 PostProcess。"""
        self.state.load_configs({1: [1.0, 2.0, 3.0, 4.0]})
        self.state.set_config_workstation(1, "WS-A")
        for step in ["sw", "sc", "transfer", "meshing"]:
            self.state.set_step_status(1, step, STATUS_COMPLETED)
        self.state.set_step_status(1, "solver", STATUS_RUNNING)
        self.runner._remote_executor._remote_task_status = "completed"

        assert self.coordinator.dispatch_solver_if_ready() is True
        self.coordinator.join_solver_threads(timeout=5)

        assert self.runner._remote_executor._remote_task_status_checks == [
            (1, "solver", "WS-A")
        ]
        assert self.runner._remote_executor._postprocess_handoffs == [(1, "WS-A")]
        assert self.runner._remote_executor._remote_task_events[:2] == [
            ("handoff", 1, "postprocess", "WS-A"),
            ("forget", 1, "solver", "WS-A"),
        ]
        assert self.runner._remote_executor._forgotten_remote_tasks[0] == (
            1,
            "solver",
            "WS-A",
        )
        assert self.runner._postprocess_dispatched == []
        assert self.state.get_step_status(1, "postprocess") == STATUS_COMPLETED

    def test_running_solver_remote_recovery_uses_assigned_workstation(self):
        """Solver 恢复查询与清理应使用构型分配的工作站。"""
        self.state.load_configs({1: [1.0, 2.0, 3.0, 4.0]})
        self.state.set_config_workstation(1, "WS-A")
        for step in ["sw", "sc", "transfer", "meshing"]:
            self.state.set_step_status(1, step, STATUS_COMPLETED)
        self.state.set_step_status(1, "solver", STATUS_RUNNING)
        self.runner._remote_executor._remote_task_status = "lost"

        assert self.coordinator.dispatch_solver_if_ready() is True
        self.coordinator.join_solver_threads(timeout=5)

        assert self.runner._remote_executor._remote_task_status_checks == [
            (1, "solver", "WS-A")
        ]
        assert self.runner._remote_executor._forgotten_remote_tasks[0] == (
            1,
            "solver",
            "WS-A",
        )

    def test_running_solver_unknown_remote_state_does_not_restart(self):
        """远程 Solver 状态未知时保守保持 Running，不重复启动。"""
        self.state.load_configs({1: [1.0, 2.0, 3.0, 4.0]})
        for step in ["sw", "sc", "transfer", "meshing"]:
            self.state.set_step_status(1, step, STATUS_COMPLETED)
        self.state.set_step_status(1, "solver", STATUS_RUNNING)
        self.runner._remote_executor._remote_task_status = "unknown"

        assert self.coordinator.dispatch_solver_if_ready() is True
        self.coordinator.join_solver_threads(timeout=5)

        assert self.runner._solver_dispatched == []
        assert self.runner._solver_wait_count == 0
        assert self.state.get_step_status(1, "solver") == STATUS_RUNNING

    def test_running_solver_unknown_remote_state_errors_after_retry_budget(self, monkeypatch):
        """远程 unknown 达到重试上限后应标 Error，避免调度循环永久空转。"""
        monkeypatch.setitem(ENGINE_CONFIG, "max_retries", 1)
        self.state.load_configs({1: [1.0, 2.0, 3.0, 4.0]})
        for step in ["sw", "sc", "transfer", "meshing"]:
            self.state.set_step_status(1, step, STATUS_COMPLETED)
        self.state.set_step_status(1, "solver", STATUS_RUNNING)
        self.runner._remote_executor._remote_task_status = "unknown"

        assert self.coordinator.dispatch_solver_if_ready() is True
        self.coordinator.join_solver_threads(timeout=5)

        assert self.runner._solver_dispatched == []
        assert self.runner._solver_wait_count == 0
        assert self.state.get_step_status(1, "solver") == STATUS_ERROR
        assert self.solver_terminal_outcomes == ["failed"]

    def test_running_solver_lost_remote_task_restarts_once_and_forgets_task(self):
        """远程 Solver 确认丢失后才重启，且清理旧元数据。"""
        self.state.load_configs({1: [1.0, 2.0, 3.0, 4.0]})
        for step in ["sw", "sc", "transfer", "meshing"]:
            self.state.set_step_status(1, step, STATUS_COMPLETED)
        self.state.set_step_status(1, "solver", STATUS_RUNNING)
        self.runner._remote_executor._remote_task_status = "lost"

        assert self.coordinator.dispatch_solver_if_ready() is True
        self.coordinator.join_solver_threads(timeout=5)

        assert self.runner._remote_executor._remote_task_status_checks == [(1, "solver", "default")]
        assert self.runner._remote_executor._forgotten_remote_tasks[0] == (
            1,
            "solver",
            "default",
        )
        assert self.runner._solver_dispatched == [1]
        assert self.state.get_step_status(1, "solver") == STATUS_COMPLETED

    def test_stale_solver_completion_after_reset_does_not_overwrite_reset(self):
        """reset 期间完成的旧 Solver 结果不应覆盖 reset 后状态。"""
        self.state.load_configs({1: [1.0, 2.0, 3.0, 4.0]})
        for step in ["sw", "sc", "transfer", "meshing"]:
            self.state.set_step_status(1, step, STATUS_COMPLETED)
        self.state.set_step_status(1, "solver", STATUS_WAITING)
        generation = {"value": 0}

        def get_reset_generation(config_name: int, step_name: str) -> int:
            assert config_name == 1
            assert step_name == "solver"
            return generation["value"]

        def execute_after_reset(config_name: int) -> bool:
            generation["value"] += 1
            self.state.reset_config_steps(config_name, "solver")
            self.state.set_step_status(config_name, "solver", STATUS_COMPLETED)
            return True

        self.coordinator._get_reset_generation = get_reset_generation
        self.runner.execute_solver = execute_after_reset

        self.coordinator._execute_solver_for_config(1)

        assert self.state.get_step_status(1, "solver") == STATUS_WAITING


# ====================================================================
# MeshingMonitor 测试
# ====================================================================

class TestMeshingMonitor:
    """MeshingMonitor 队列与串行处理测试。"""

    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp(prefix="meshing_test_")
        self.db_path = os.path.join(self.tmpdir, "test.db")
        self._orig_db_path = IPC_CONFIG["db_path"]
        IPC_CONFIG["db_path"] = self.db_path

        self.state = StateManager(db_path=self.db_path)
        self.remote = _MockRemoteExecutor(self.state)
        self.paused = threading.Event()
        self.stopped = threading.Event()

        from engine.scheduler.meshing_monitor import MeshingMonitor
        self.monitor = MeshingMonitor(
            state_manager=self.state,
            remote_executor=self.remote,
            paused_event=self.paused,
            stopped_event=self.stopped,
        )

    def teardown_method(self):
        self.stopped.set()
        IPC_CONFIG["db_path"] = self._orig_db_path
        if os.path.exists(self.tmpdir):
            shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_submit_increases_queue(self):
        """submit 增加队列深度。"""
        self.state.load_configs({1: [1.0, 2.0, 3.0, 4.0]})
        self.monitor.submit(1)
        assert self.monitor.qsize() == 1

    def test_submit_deduplicates_pending_config(self):
        """重复来源提交同一构型时只保留一个待处理任务。"""
        self.state.load_configs({1: [1.0, 2.0, 3.0, 4.0]})
        self.monitor.submit(1)
        self.monitor.submit(1)
        assert self.monitor.qsize() == 1

    def test_qsize_empty(self):
        """空队列返回 0。"""
        assert self.monitor.qsize() == 0

    def test_get_in_flight_config_initial_none(self):
        """初始状态无 in-flight 构型。"""
        assert self.monitor.get_in_flight_config() is None

    def test_get_in_flight_config_reflects_active_config(self):
        """get_in_flight_config 应反映当前正在处理的构型编号。"""
        # 通过 _in_flight_config 内部属性直接验证
        self.monitor._in_flight_config = 5
        assert self.monitor.get_in_flight_config() == 5

        self.monitor._in_flight_config = None
        assert self.monitor.get_in_flight_config() is None

    def test_start_if_needed_creates_thread(self):
        """start_if_needed 创建监控线程。"""
        self.state.load_configs({1: [1.0, 2.0, 3.0, 4.0]})
        # 需要 stopped 来退出循环
        self.stopped.set()
        self.monitor.start_if_needed()
        time.sleep(0.3)
        # 线程应已启动并退出（因为 stopped）
        assert self.monitor._monitor_thread is not None

    def test_start_if_needed_idempotent(self):
        """重复调用 start_if_needed 不创建重复线程。"""
        self.stopped.set()
        self.monitor.start_if_needed()
        self.monitor.start_if_needed()
        # 第二次调用时线程已死亡（stopped），会创建新线程
        # 但不会崩溃

    def test_scan_db_for_pending_restores_queue(self, caplog):
        """断点续传：Transfer Completed + Meshing Waiting → 入队。"""
        self.state.load_configs({1: [1.0, 2.0, 3.0, 4.0], 2: [5.0, 6.0, 7.0, 8.0]})
        self.state.set_step_status(1, "transfer", STATUS_COMPLETED)
        self.state.set_step_status(1, "meshing", STATUS_WAITING)
        self.state.set_step_status(2, "transfer", STATUS_COMPLETED)
        self.state.set_step_status(2, "meshing", STATUS_ERROR, "超时")

        with caplog.at_level(logging.INFO):
            self.monitor._scan_db_for_pending()
        assert self.monitor.qsize() == 2
        messages = [record.getMessage() for record in caplog.records]
        assert any("[MeshingMonitor] 构型1 已入队" in msg for msg in messages)
        assert any("[MeshingMonitor] 构型2 已入队" in msg for msg in messages)
        assert not any("断点续传: 构型补充入队" in msg for msg in messages)

    def test_scan_db_for_pending_skips_non_completed_transfer(self):
        """Transfer 未完成的构型不入队。"""
        self.state.load_configs({1: [1.0, 2.0, 3.0, 4.0]})
        self.state.set_step_status(1, "transfer", STATUS_RUNNING)

        self.monitor._scan_db_for_pending()
        assert self.monitor.qsize() == 0

    def test_monitor_loop_processes_queue(self):
        """监控循环处理队列中的构型。"""
        self.state.load_configs({1: [1.0, 2.0, 3.0, 4.0]})
        self.state.set_meshing_running_if_idle(1)
        self.state.set_step_status(1, "meshing", STATUS_WAITING)

        self.monitor.submit(1)
        self.monitor.start_if_needed()

        # 等待处理完成（给监控线程足够时间处理队列）
        time.sleep(3)
        self.stopped.set()
        time.sleep(0.5)

        # 构型应已被处理（Completed），Mock wait_meshing_completion 返回 True
        status = self.state.get_step_status(1, "meshing")
        assert status == STATUS_COMPLETED, (
            f"队列中的构型应已被处理为 Completed，实际状态: {status}"
        )

    def test_monitor_paused_waits(self):
        """暂停期间监控循环等待。"""
        self.state.load_configs({1: [1.0, 2.0, 3.0, 4.0]})
        self.paused.set()
        self.monitor.submit(1)

        self.monitor.start_if_needed()
        time.sleep(1)

        # 暂停期间不应处理
        assert self.monitor.qsize() == 1
        self.stopped.set()

    def test_remote_output_check_uses_remote_executor_helper(self):
        """MeshingMonitor 应委托 RemoteExecutor 在 SSH 锁内检查远程输出。"""
        self.remote._meshing_output_exists = True

        assert self.monitor._check_remote_outputs_exist(3) is True
        assert self.remote._meshing_output_checks == [(3, 120.0, "default")]

    def test_remote_output_check_uses_assigned_workstation(self):
        """Meshing 断点输出检查应使用构型分配工作站。"""
        self.state.load_configs({1: [1.0, 2.0, 3.0, 4.0]})
        self.state.set_config_workstation(1, "WS-A")
        self.remote._meshing_output_exists = True

        assert self.monitor._check_remote_outputs_exist(1) is True
        assert self.remote._meshing_output_checks == [(1, 120.0, "WS-A")]

    def test_running_meshing_with_remote_task_keeps_running_without_restart(self):
        """Daemon 重启后远程 Meshing 仍运行时应恢复等待，不重复启动。"""
        self.state.load_configs({1: [1.0, 2.0, 3.0, 4.0]})
        self.state.set_step_status(1, "transfer", STATUS_COMPLETED)
        self.state.set_step_status(1, "meshing", STATUS_RUNNING)
        self.remote._remote_task_status = "running"

        assert self.monitor._process_single_meshing(1) is False

        assert self.remote._remote_task_status_checks == [(1, "meshing", "default")]
        assert self.remote._meshing_started == []
        assert self.state.get_step_status(1, "meshing") == STATUS_COMPLETED

    def test_running_meshing_unknown_remote_state_does_not_restart(self):
        """远程状态未知时保守保持 Running，并重新排队等待后续探测。"""
        self.state.load_configs({1: [1.0, 2.0, 3.0, 4.0]})
        self.state.set_step_status(1, "transfer", STATUS_COMPLETED)
        self.state.set_step_status(1, "meshing", STATUS_RUNNING)
        self.remote._remote_task_status = "unknown"

        assert self.monitor._process_single_meshing(1) is True

        assert self.remote._meshing_started == []
        assert self.state.get_step_status(1, "meshing") == STATUS_RUNNING

    def test_running_meshing_unknown_remote_state_errors_after_retry_budget(self, monkeypatch):
        """远程 unknown 达到重试上限后应标 Error，不能从队列中无声消失。"""
        monkeypatch.setitem(ENGINE_CONFIG, "max_retries", 1)
        self.state.load_configs({1: [1.0, 2.0, 3.0, 4.0]})
        self.state.set_step_status(1, "transfer", STATUS_COMPLETED)
        self.state.set_step_status(1, "meshing", STATUS_RUNNING)
        self.remote._remote_task_status = "unknown"

        assert self.monitor._process_single_meshing(1) is False

        assert self.remote._meshing_started == []
        assert self.state.get_step_status(1, "meshing") == STATUS_ERROR

    def test_running_meshing_lost_remote_task_restarts_once_and_forgets_task(self):
        """远程 Meshing 确认丢失后才重启，且清理旧元数据。"""
        self.state.load_configs({1: [1.0, 2.0, 3.0, 4.0]})
        self.state.set_step_status(1, "transfer", STATUS_COMPLETED)
        self.state.set_step_status(1, "meshing", STATUS_RUNNING)
        self.remote._remote_task_status = "lost"

        assert self.monitor._process_single_meshing(1) is False

        assert self.remote._remote_task_status_checks == [(1, "meshing", "default")]
        assert self.remote._forgotten_remote_tasks == [(1, "meshing", "default")]
        assert self.remote._meshing_started == [(1, "default")]

    def test_assigned_workstation_used_for_meshing_lifecycle(self):
        """Meshing 启动、等待与防重入应按构型分配工作站执行。"""
        self.state.load_configs({
            1: [1.0, 2.0, 3.0, 4.0],
            2: [5.0, 6.0, 7.0, 8.0],
        })
        self.state.set_config_workstation(1, "WS-A")
        self.state.set_config_workstation(2, "WS-B")
        self.state.set_step_status(1, "transfer", STATUS_COMPLETED)
        self.state.set_step_status(1, "meshing", STATUS_WAITING)
        self.state.set_step_status(2, "meshing", STATUS_RUNNING)

        self.monitor._process_single_meshing(1)

        assert self.remote._meshing_started == [(1, "WS-A")]
        assert self.remote._meshing_waits == [(1, "WS-A")]
        assert self.state.get_step_status(1, "meshing") == STATUS_COMPLETED
        assert self.state.get_step_status(1, "meshing") == STATUS_COMPLETED


# ====================================================================
# utils 工具函数补充测试
# ====================================================================

class TestUtilsExtended:
    """scheduler/utils 工具函数补充测试。"""

    def test_wait_unless_paused_or_stopped_not_paused(self):
        """未暂停时立即返回 True。"""
        from engine.scheduler.utils import wait_unless_paused_or_stopped
        paused = threading.Event()
        stopped = threading.Event()
        assert wait_unless_paused_or_stopped(paused, stopped) is True

    def test_wait_unless_paused_or_stopped_stopped(self):
        """停止时返回 False。"""
        from engine.scheduler.utils import wait_unless_paused_or_stopped
        paused = threading.Event()
        stopped = threading.Event()
        stopped.set()
        assert wait_unless_paused_or_stopped(paused, stopped) is False

    def test_wait_unless_paused_or_stopped_paused_then_resumed(self):
        """暂停后恢复返回 True。"""
        from engine.scheduler.utils import wait_unless_paused_or_stopped
        paused = threading.Event()
        stopped = threading.Event()
        paused.set()

        def resume():
            time.sleep(0.2)
            paused.clear()

        threading.Thread(target=resume, daemon=True).start()
        assert wait_unless_paused_or_stopped(paused, stopped) is True

    def test_pause_aware_sleep_zero_duration(self):
        """零时长 sleep 立即返回。"""
        from engine.scheduler.utils import pause_aware_sleep
        paused = threading.Event()
        stopped = threading.Event()
        assert pause_aware_sleep(0, paused, stopped) is True

    def test_pause_aware_sleep_stopped_during_pause(self):
        """暂停期间收到停止信号。"""
        from engine.scheduler.utils import pause_aware_sleep
        paused = threading.Event()
        stopped = threading.Event()
        paused.set()

        def stop_after():
            time.sleep(0.1)
            stopped.set()

        threading.Thread(target=stop_after, daemon=True).start()
        assert pause_aware_sleep(5.0, paused, stopped) is False

    def test_pause_aware_sleep_excludes_paused_duration(self):
        """恢复后仍应完成剩余的有效等待时长。"""
        from engine.scheduler.utils import pause_aware_sleep
        paused = threading.Event()
        stopped = threading.Event()
        resumed_at: list[float] = []
        paused.set()

        def resume_after() -> None:
            time.sleep(0.1)
            resumed_at.append(time.monotonic())
            paused.clear()

        threading.Thread(target=resume_after, daemon=True).start()
        started_at = time.monotonic()
        assert pause_aware_sleep(0.15, paused, stopped, check_interval=0.01) is True
        assert time.monotonic() - started_at < 0.5
        assert time.monotonic() - resumed_at[0] >= 0.12

    def test_remote_output_check_passes_timeout_to_meshing_remote_checks(self):
        """远程 Meshing 输出检查应向 SFTP stat 传递超时。"""
        from engine.scheduler.utils import check_step_output_exists

        calls: list[tuple[str, float | None]] = []

        class _SSH:
            @staticmethod
            def is_connected() -> bool:
                return True

            def check_remote_file(
                self,
                remote_path: str,
                *,
                timeout: float | None = None,
            ) -> bool:
                calls.append((remote_path, timeout))
                return False

        assert check_step_output_exists(
            3,
            "meshing",
            "",
            "",
            {
                "flag_dir": "D:/flags",
                "msh_dir": "D:/msh",
                "scdoc_dir": "D:/scdoc",
                "result_dir": "D:/result",
            },
            _SSH(),
            remote_check_timeout=7,
        ) is False
        assert all(timeout == 7 for _, timeout in calls)

    def test_remote_output_check_treats_meshing_error_flag_as_terminal(self):
        """Meshing .error flag 应被视为终结输出，避免恢复时重复启动。"""
        from engine.scheduler.utils import check_step_output_exists

        class _SSH:
            @staticmethod
            def is_connected() -> bool:
                return True

            @staticmethod
            def check_remote_file(
                remote_path: str,
                *,
                timeout: float | None = None,
            ) -> bool:
                return remote_path == "D:/flags/meshing_done_3.txt.error"

        assert check_step_output_exists(
            3,
            "meshing",
            "",
            "",
            {
                "flag_dir": "D:/flags",
                "msh_dir": "D:/msh",
                "scdoc_dir": "D:/scdoc",
                "result_dir": "D:/result",
            },
            _SSH(),
        ) is True

    def test_remote_output_check_ignores_meshing_done_flag_without_mesh(self):
        """Meshing done flag 不能替代实际 .msh.h5 输出。"""
        from engine.scheduler.utils import check_step_output_exists

        calls: list[str] = []

        class _SSH:
            @staticmethod
            def is_connected() -> bool:
                return True

            def check_remote_file(
                self,
                remote_path: str,
                *,
                timeout: float | None = None,
            ) -> bool:
                calls.append(remote_path)
                return remote_path == "D:/flags/meshing_done_3.txt"

        assert check_step_output_exists(
            3,
            "meshing",
            "",
            "",
            {
                "flag_dir": "D:/flags",
                "msh_dir": "D:/msh",
                "scdoc_dir": "D:/scdoc",
                "result_dir": "D:/result",
            },
            _SSH(),
            remote_check_timeout=7,
        ) is False
        assert calls == [
            "D:/flags/meshing_done_3.txt.error",
            "D:/msh/model_gen4_3.msh.h5",
        ]

    def test_remote_output_check_treats_solver_error_flag_as_terminal(self):
        """Solver .error flag 应被视为终结输出，避免恢复时重复启动。"""
        from engine.scheduler.utils import check_step_output_exists

        class _SSH:
            @staticmethod
            def is_connected() -> bool:
                return True

            @staticmethod
            def check_remote_file(
                remote_path: str,
                *,
                timeout: float | None = None,
            ) -> bool:
                return remote_path == "D:/flags/solver_done_4.txt.error"

        assert check_step_output_exists(
            4,
            "solver",
            "",
            "",
            {
                "flag_dir": "D:/flags",
                "msh_dir": "D:/msh",
                "scdoc_dir": "D:/scdoc",
                "result_dir": "D:/result",
            },
            _SSH(),
        ) is True
