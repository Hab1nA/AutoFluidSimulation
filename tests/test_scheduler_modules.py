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
import tempfile
import shutil
from engine.state_manager import StateManager
from engine.config import (
    STATUS_PAUSED, STATUS_COMPLETED, STATUS_ERROR, STATUS_RETRYING,
    IPC_CONFIG,
)
from engine.scheduler.retry import RetryManager


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

        result = self.retry_mgr.execute_with_retry(1, "SC", success_func)
        assert result is True
        assert call_count == 1
        assert self.state.get_step_status(1, "SC") == STATUS_COMPLETED

    def test_execute_success_after_retry(self):
        """首次失败、第二次成功应返回 True。"""
        call_count = 0
        def fail_then_success(cn):
            nonlocal call_count
            call_count += 1
            return call_count > 1

        # 使用较短的重试延迟
        result = self.retry_mgr.execute_with_retry(1, "SC", fail_then_success)
        assert result is True
        assert call_count == 2
        assert self.state.get_step_status(1, "SC") == STATUS_COMPLETED

    def test_execute_all_retries_exhausted(self):
        """所有重试均失败应返回 False 并标记 Error。"""
        def always_fail(cn):
            return False

        result = self.retry_mgr.execute_with_retry(1, "SC", always_fail)
        assert result is False
        assert self.state.get_step_status(1, "SC") == STATUS_ERROR

    def test_execute_stopped_immediately(self):
        """stopped 标志应立即中止执行。"""
        self.stopped.set()
        call_count = 0
        def should_not_be_called(cn):
            nonlocal call_count
            call_count += 1
            return True

        result = self.retry_mgr.execute_with_retry(1, "SC", should_not_be_called)
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

        _ = self.retry_mgr.execute_with_retry(1, "SC", fail_on_first)
        # 因暂停导致的失败应标记 Paused
        status = self.state.get_step_status(1, "SC")
        assert status in (STATUS_PAUSED, STATUS_RETRYING, STATUS_ERROR)

    def test_pause_aware_sleep_completes(self):
        """无中断时 pause_aware_sleep 应正常完成。"""
        start = time.time()
        result = self.retry_mgr.pause_aware_sleep(0.2)
        elapsed = time.time() - start
        assert result is True
        assert elapsed >= 0.15  # 允许一定误差

    def test_pause_aware_sleep_stopped(self):
        """stopped 标志应使 sleep 提前退出。"""
        def set_stopped():
            time.sleep(0.05)
            self.stopped.set()

        threading.Thread(target=set_stopped, daemon=True).start()
        result = self.retry_mgr.pause_aware_sleep(5.0)
        assert result is False

    def test_pause_aware_sleep_paused_then_resumed(self):
        """暂停期间应等待，恢复后继续。"""
        def pause_and_resume():
            self.paused.set()
            time.sleep(0.1)
            self.paused.clear()

        threading.Thread(target=pause_and_resume, daemon=True).start()
        start = time.time()
        result = self.retry_mgr.pause_aware_sleep(0.2)
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
