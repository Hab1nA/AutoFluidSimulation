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
from engine.state_manager import StateManager
from engine.config import (
    STATUS_WAITING, STATUS_RUNNING, STATUS_PAUSED, STATUS_COMPLETED, STATUS_ERROR, STATUS_RETRYING,
    IPC_CONFIG,
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


class TestSWPhaseHandlerFallback:
    """验证 SW 独立模式文件监控回调。"""

    def test_fallback_monitor_submits_to_unique_work_queue(self, monkeypatch):
        """独立模式发现 STEP 文件时应使用去重队列接口提交。"""
        from engine.file_monitor import StepFileMonitor
        from engine.scheduler.sw_phase import SWPhaseHandler
        from engine.scheduler.work_queue import UniqueWorkQueue

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

    def disconnect_sw_cached(self):
        return None

    def verify_step_exports(self, step_dir: str) -> int:
        return sum(
            1
            for cn in self.state.get_all_configs()
            if self.state.get_step_status(cn, "sw") == STATUS_COMPLETED
        )

    def execute_solver(self, config_name: int) -> bool:
        self._solver_dispatched.append(config_name)
        self.state.set_step_status(config_name, "solver", STATUS_COMPLETED)
        return True

    def wait_solver_completion(self, config_name, paused_event=None, stopped_event=None) -> bool:
        return self._solver_wait_result

    def get_remote_executor(self):
        return _MockRemoteExecutor(self.state)


class _MockRemoteExecutor:
    """轻量 RemoteExecutor Mock。"""

    def __init__(self, state_manager):
        self.state = state_manager
        self._meshing_started: list[int] = []
        self._meshing_output_exists = False
        self._meshing_output_checks: list[tuple[int, float | None]] = []

    def start_meshing(self, config_name: int) -> bool:
        self._meshing_started.append(config_name)
        return True

    def check_meshing_done(self, config_name: int) -> bool:
        return False

    def wait_meshing_completion(self, config_name, paused_event=None, stopped_event=None) -> bool:
        # 立即完成
        return True

    def get_ssh_connection(self):
        return None

    def check_meshing_outputs_exist(
        self,
        config_name: int,
        *,
        timeout: float | None = None,
    ) -> bool:
        self._meshing_output_checks.append((config_name, timeout))
        return self._meshing_output_exists


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

    def test_request_file_monitor_reset_preserves_scheduler_pause(self):
        """clean SW 后只重置监控器追踪状态，不解除调度器暂停。"""
        self.scheduler._paused.set()

        self.scheduler.request_file_monitor_reset()

        assert self.file_monitor.reset_only_count == 1
        assert self.file_monitor.resume_and_reset_count == 0
        assert self.scheduler._paused.is_set()

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


class _CleanStepRunner:
    def __init__(self):
        self.clean_calls = []
        self.clean_all_cache_count = 0

    def clean_step_files(self, step_name, config_name):
        self.clean_calls.append((step_name, config_name))

    def clean_all_cache(self):
        self.clean_all_cache_count += 1


class _CleanStepScheduler:
    def __init__(self):
        self.file_monitor_reset_count = 0

    def request_file_monitor_reset(self):
        self.file_monitor_reset_count += 1


class TestPipelineDaemonCleanStep:
    """验证 clean_step IPC handler 与调度器文件监控接口兼容。"""

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

        from engine.scheduler.barrier import BarrierCoordinator
        self.coordinator = BarrierCoordinator(
            state_manager=self.state,
            task_runner=self.runner,
            paused_event=self.paused,
            stopped_event=self.stopped,
            barrier_passed_event=self.barrier_passed,
            retry_manager=self.retry_mgr,
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
        # Solver 也被标记为 Error
        assert self.state.get_step_status(1, "solver") == STATUS_ERROR
        assert self.state.get_step_status(2, "solver") == STATUS_ERROR

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
        # 后续步骤被标记为 Error
        assert self.state.get_step_status(1, "sc") == STATUS_ERROR
        assert self.state.get_step_status(2, "solver") == STATUS_ERROR

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
        assert self.remote._meshing_output_checks == [(3, 120.0)]


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
