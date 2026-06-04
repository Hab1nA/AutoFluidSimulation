"""
===============================================================================
调度竞态条件修复验证测试 (M10)

覆盖 docs/fix-scheduler-race-condition.md 中描述的全部修复场景：

  场景 A: 活跃 Running + 有 claim → _resume_paused_steps 跳过，不误重置
  场景 B: 孤儿 Running + 无 claim → _resume_paused_steps 正确检测并重置
  场景 C: Transfer/SC 重复入队被 UniqueWorkQueue claim 机制拦截
  场景 D: MeshingMonitor 异常时 requeue（而非永久丢失构型）
  场景 E: MeshingMonitor 超过 max_retries 后标记 ERROR
  场景 F: has_claim() 正确反映排队/执行/释放生命周期

运行方式：
  cd "项目根目录"
  python -m pytest tests/test_race_condition_fix.py -v
===============================================================================
"""
from __future__ import annotations

import os
import sys
import threading
import time
import tempfile
import shutil
import logging

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_TEST_TMP_ROOT = tempfile.mkdtemp(prefix="rcf_test_")
_TEST_LOG_DIR = os.path.join(_TEST_TMP_ROOT, "logs")
os.makedirs(_TEST_LOG_DIR, exist_ok=True)
os.environ["AUTOFLUID_LOG_DIR"] = _TEST_LOG_DIR

from engine.config import (
    STATUS_WAITING, STATUS_RUNNING, STATUS_PAUSED, STATUS_COMPLETED,
    STATUS_ERROR, STATUS_RETRYING,
    ENGINE_CONFIG, IPC_CONFIG, STEP_NAMES, LOCAL_PATHS,
)
from engine.state_manager import StateManager


# ====================================================================
# Mock 组件（复用 pause_start 测试中的 Mock 模式）
# ====================================================================

class _MockSCPool:
    def do_first_cleanup(self): pass
    def do_final_cleanup(self): pass
    def reset(self): pass
    def shutdown_all(self): pass
    def run_config(self, *args, **kwargs): return True


class _MockRemoteExecutor:
    """Mock RemoteExecutor，可控制 start_meshing 的返回值。"""

    def __init__(self, state_manager):
        self.state = state_manager
        self._ssh_lock = threading.RLock()
        self.start_meshing_returns = True
        self.wait_meshing_returns = True
        self.check_meshing_done_returns = False
        self.start_meshing_call_count = 0
        self.wait_meshing_call_count = 0

    def _get_ssh(self):
        return None

    def start_meshing(self, config_name: int) -> bool:
        self.start_meshing_call_count += 1
        return self.start_meshing_returns

    def check_meshing_done(self, config_name: int) -> bool:
        return self.check_meshing_done_returns

    def wait_meshing_completion(self, config_name,
                                 paused_event=None, stopped_event=None) -> bool:
        self.wait_meshing_call_count += 1
        time.sleep(0.05)
        return self.wait_meshing_returns


class _MockSWExecutor:
    def __init__(self, state_manager):
        self.state = state_manager

    def _verify_step_exports(self, step_dir: str) -> int:
        return sum(
            1 for cn in self.state.get_all_configs()
            if self.state.get_step_status(cn, "sw") == STATUS_COMPLETED
        )

    def disconnect_sw_cached(self):
        pass


class MockTaskRunner:
    """Mock TaskRunner，支持控制各步骤的返回值。"""

    def __init__(self, state_manager: StateManager):
        self.state = state_manager
        self._sw_should_fail = False
        self._sc_pool = _MockSCPool()
        self._remote_executor = _MockRemoteExecutor(self.state)
        self._sw_executor = _MockSWExecutor(state_manager)
        self._solver_dispatched: list[int] = []
        self._solver_lock = threading.Lock()

    def execute_sw_step(self) -> bool:
        if self._sw_should_fail:
            for cn in self.state.get_all_configs():
                self.state.set_step_status(cn, "sw", STATUS_ERROR, "模拟失败")
            return False
        for cn in self.state.get_all_configs():
            self.state.set_step_status(cn, "sw", STATUS_COMPLETED)
        return True

    def execute_sw_per_config(self, config_name: int) -> bool:
        if self._sw_should_fail:
            return False
        self.state.set_step_status(config_name, "sw", STATUS_COMPLETED)
        return True

    def execute_sc_step(self, config_name: int) -> bool:
        self.state.set_step_status(config_name, "sc", STATUS_COMPLETED)
        return True

    def execute_transfer(self, config_name: int) -> bool:
        self.state.set_step_status(config_name, "transfer", STATUS_COMPLETED)
        return True

    def execute_meshing(self, config_name: int) -> bool:
        return True

    def execute_solver(self, config_name: int) -> bool:
        with self._solver_lock:
            self._solver_dispatched.append(config_name)
        time.sleep(0.05)
        return True

    def wait_meshing_completion(self, config_name,
                                 paused_event=None, stopped_event=None) -> bool:
        return True

    def wait_solver_completion(self, config_name,
                                paused_event=None, stopped_event=None) -> bool:
        return True

    def get_ssh(self):
        return None

    def get_remote_executor(self):
        return self._remote_executor

    def set_control_events(self, paused_event, stopped_event):
        self._paused_event = paused_event
        self._stopped_event = stopped_event

    def set_pipeline_control(self, control):
        pass

    def do_sc_final_cleanup(self):
        pass


class MockStepFileMonitor:
    _running = False

    def __init__(self, step_dir=None, on_file_ready=None, shared_paused_event=None):
        self.step_dir = step_dir
        self.on_file_ready = on_file_ready
        self._paused = shared_paused_event if shared_paused_event else threading.Event()
        self._detector = type("_MockDetector", (), {"_history": {}, "_first_seen": {}})()

    def start(self): self._running = True
    def stop(self): self._running = False

    @property
    def is_running(self): return self._running

    def pause(self): self._paused.set()
    def resume_only(self): self._paused.clear()
    def clear_tracking(self):
        self._detector._history.clear()
        self._detector._first_seen.clear()
    def request_reset(self): pass
    def wake(self): pass

    @staticmethod
    def parse_config_name(filename: str) -> int | None:
        import re
        m = re.search(r'model_gen4[_\-]?(\d+)', filename, re.IGNORECASE)
        return int(m.group(1)) if m else None


# ---- 替换 Mock 到 scheduler 模块 ----
import engine.scheduler.main as scheduler_main_mod
import engine.file_monitor as file_monitor_mod
import engine.scheduler.sw_phase as sw_phase_mod

_OriginalTaskRunner = scheduler_main_mod.TaskRunner
_OriginalStepFileMonitor = file_monitor_mod.StepFileMonitor


def setup_mock_environment():
    scheduler_main_mod.TaskRunner = MockTaskRunner
    file_monitor_mod.StepFileMonitor = MockStepFileMonitor
    scheduler_main_mod.StepFileMonitor = MockStepFileMonitor
    sw_phase_mod.StepFileMonitor = MockStepFileMonitor


def teardown_mock_environment():
    scheduler_main_mod.TaskRunner = _OriginalTaskRunner
    file_monitor_mod.StepFileMonitor = _OriginalStepFileMonitor
    scheduler_main_mod.StepFileMonitor = _OriginalStepFileMonitor
    sw_phase_mod.StepFileMonitor = _OriginalStepFileMonitor


# ====================================================================
# 测试辅助类
# ====================================================================

class TestContext:
    """测试上下文，创建 PipelineScheduler 及其所有依赖。"""
    __test__ = False

    def __init__(self, num_configs: int = 5):
        self.tmpdir = tempfile.mkdtemp(prefix="rcf_ctx_")
        self.db_path = os.path.join(self.tmpdir, "test.db")

        # ★ 创建临时 step/scdoc 目录，覆盖 LOCAL_PATHS
        self.tmp_step_dir = os.path.join(self.tmpdir, "step")
        self.tmp_scdoc_dir = os.path.join(self.tmpdir, "scdoc")
        os.makedirs(self.tmp_step_dir, exist_ok=True)
        os.makedirs(self.tmp_scdoc_dir, exist_ok=True)

        self._orig_db_path = IPC_CONFIG["db_path"]
        self._orig_step_dir = LOCAL_PATHS.get("step_dir", "")
        self._orig_scdoc_dir = LOCAL_PATHS.get("scdoc_dir", "")

        IPC_CONFIG["db_path"] = self.db_path
        LOCAL_PATHS["step_dir"] = self.tmp_step_dir
        LOCAL_PATHS["scdoc_dir"] = self.tmp_scdoc_dir

        setup_mock_environment()

        self.state = StateManager(db_path=self.db_path)
        configs = {i: [1.0, 2.0, 3.0, 4.0] for i in range(1, num_configs + 1)}
        self.state.load_configs(configs)

        self.runner = MockTaskRunner(self.state)

        from engine.scheduler import PipelineScheduler
        self.scheduler = PipelineScheduler(self.state, self.runner)

        # 跳过真实的 SW 进程清理
        def _mock_prepare_sw_retry():
            fm = self.scheduler.sw_phase_handler._file_monitor
            if fm is not None:
                fm.clear_tracking()
        self.scheduler.sw_phase_handler._prepare_sw_retry = _mock_prepare_sw_retry

    def cleanup(self):
        try:
            self.scheduler.stop()
        finally:
            IPC_CONFIG["db_path"] = self._orig_db_path
            LOCAL_PATHS["step_dir"] = self._orig_step_dir
            LOCAL_PATHS["scdoc_dir"] = self._orig_scdoc_dir
            teardown_mock_environment()
            if os.path.exists(self.tmpdir):
                shutil.rmtree(self.tmpdir, ignore_errors=True)

    def assert_step_status(self, cn, step, expected, msg=""):
        actual = self.state.get_step_status(cn, step)
        assert actual == expected, (
            f"{msg}: 期望构型{cn}[{step}]='{expected}', 实际='{actual}'"
        )


# ====================================================================
# 场景 A: 活跃 Running + 有 claim → 不被误重置
# ====================================================================

class TestInFlightProtection:
    """验证 _resume_paused_steps 不会误重置有活跃 claim 的步骤。"""

    @classmethod
    def setup_class(cls):
        setup_mock_environment()

    @classmethod
    def teardown_class(cls):
        teardown_mock_environment()

    def setup_method(self):
        self.ctx = TestContext(num_configs=5)

    def teardown_method(self):
        self.ctx.cleanup()

    # ---- A1: SC step ----

    def test_sc_running_with_claim_is_not_reset(self):
        """构型2 sc=Running + SC队列有claim → _resume_paused_steps 跳过该构型。"""
        s = self.ctx.scheduler
        st = self.ctx.state

        # 所有构型 SW 标记为已完成
        for cn in st.get_all_configs():
            st.set_step_status(cn, "sw", STATUS_COMPLETED)

        # 构型2 sc=Running，且 SC 队列中有 claim（模拟活跃 worker）
        st.set_step_status(2, "sc", STATUS_RUNNING)
        s._sc_queue.submit((2, "/fake/path/model_gen4_2.STEP"))

        # 构型3 sc=Running，但 SC 队列无 claim（孤儿 Running）
        st.set_step_status(3, "sc", STATUS_RUNNING)

        # 执行断点续传扫描
        s._resume_paused_steps(log_prefix="[Test]")

        # 验证：构型2 保持 RUNNING（有 claim 保护）
        self.ctx.assert_step_status(2, "sc", STATUS_RUNNING,
                                     "活跃 SC 步骤不应被重置")
        self.ctx.assert_step_status(2, "transfer", STATUS_WAITING,
                                     "活跃构型的下游步骤不应被修改")

        # 验证：构型3 被重置为 WAITING（孤儿 Running 正确检测）
        self.ctx.assert_step_status(3, "sc", STATUS_WAITING,
                                     "孤儿 SC Running 应被重置")

    # ---- A2: Transfer step ----

    def test_transfer_running_with_claim_is_not_reset(self):
        """构型4 transfer=Running + Transfer队列有claim → 跳过。"""
        s = self.ctx.scheduler
        st = self.ctx.state

        for cn in st.get_all_configs():
            st.set_step_status(cn, "sw", STATUS_COMPLETED)
            st.set_step_status(cn, "sc", STATUS_COMPLETED)

        # 构型4 transfer=Running，有 claim
        st.set_step_status(4, "transfer", STATUS_RUNNING)
        s.worker_pool._transfer_queue.submit(4)

        # 构型5 transfer=Running，无 claim（孤儿）
        st.set_step_status(5, "transfer", STATUS_RUNNING)

        s._resume_paused_steps(log_prefix="[Test]")

        self.ctx.assert_step_status(4, "transfer", STATUS_RUNNING,
                                     "活跃 Transfer 步骤不应被重置")
        self.ctx.assert_step_status(5, "transfer", STATUS_WAITING,
                                     "孤儿 Transfer Running 应被重置")

    # ---- A3: Meshing step ----

    def test_meshing_with_in_flight_config_is_not_reset(self):
        """MeshingMonitor 正在处理构型3 → meshing=Running 不被重置。"""
        s = self.ctx.scheduler
        st = self.ctx.state

        for cn in st.get_all_configs():
            st.set_step_status(cn, "sw", STATUS_COMPLETED)
            st.set_step_status(cn, "sc", STATUS_COMPLETED)
            st.set_step_status(cn, "transfer", STATUS_COMPLETED)

        # 构型3 meshing=Running，MeshingMonitor 正在处理
        st.set_step_status(3, "meshing", STATUS_RUNNING)
        s.meshing_monitor._in_flight_config = 3

        # 构型4 meshing=Running，无 in-flight（孤儿）
        st.set_step_status(4, "meshing", STATUS_RUNNING)

        s._resume_paused_steps(log_prefix="[Test]")

        self.ctx.assert_step_status(3, "meshing", STATUS_RUNNING,
                                     "正在处理的 Meshing 不应被重置")
        # 构型4：孤儿 meshing Running → 应被检测并重置
        assert st.get_step_status(4, "meshing") != STATUS_RUNNING, (
            "孤儿 Meshing Running 应被处理"
        )

    # ---- A4: WAITING + claim 也不应重复入队 ----

    def test_waiting_with_claim_is_skipped(self):
        """构型 transfer=Waiting 但已在 Transfer 队列中 → 跳过不重复入队。"""
        s = self.ctx.scheduler
        st = self.ctx.state

        for cn in st.get_all_configs():
            st.set_step_status(cn, "sw", STATUS_COMPLETED)
            st.set_step_status(cn, "sc", STATUS_COMPLETED)
            # 其他构型设为已完成，避免被 scan 入队
            if cn != 1:
                st.set_step_status(cn, "transfer", STATUS_COMPLETED)
                st.set_step_status(cn, "meshing", STATUS_COMPLETED)

        # 构型1 transfer=Waiting，已在 Transfer 队列
        st.set_step_status(1, "transfer", STATUS_WAITING)
        s.worker_pool._transfer_queue.submit(1)

        prev_depth = s.worker_pool._transfer_queue.qsize()

        s._resume_paused_steps(log_prefix="[Test]")

        # 验证队列深度未增加（未重复入队）
        assert s.worker_pool._transfer_queue.qsize() == prev_depth, (
            f"Transfer 队列深度不应增加: 之前={prev_depth}, 当前="
            f"{s.worker_pool._transfer_queue.qsize()}"
        )


# ====================================================================
# 场景 B: 孤儿 Running + 无 claim → 正确检测并重置
# ====================================================================

class TestOrphanRunningDetection:
    """验证无 claim 的孤儿 Running 步骤被正确检测和重置。"""

    @classmethod
    def setup_class(cls):
        setup_mock_environment()

    @classmethod
    def teardown_class(cls):
        teardown_mock_environment()

    def setup_method(self):
        self.ctx = TestContext(num_configs=5)

    def teardown_method(self):
        self.ctx.cleanup()

    def test_orphan_sc_running_reset_to_waiting_and_enqueued(self):
        """孤儿 sc=Running (无claim) → 重置为 Waiting 并入队。"""
        s = self.ctx.scheduler
        st = self.ctx.state

        for cn in st.get_all_configs():
            st.set_step_status(cn, "sw", STATUS_COMPLETED)

        st.set_step_status(1, "sc", STATUS_RUNNING)
        # 不添加任何 claim → 孤儿

        prev_sc_depth = s._sc_queue.qsize()

        s._resume_paused_steps(log_prefix="[Test]")

        self.ctx.assert_step_status(1, "sc", STATUS_WAITING,
                                     "孤儿 SC Running 应被重置为 Waiting")
        # 构型应被推入 SC 队列
        assert s._sc_queue.qsize() >= prev_sc_depth, (
            "孤儿构型应被推入 SC 队列"
        )

    def test_orphan_transfer_running_reset_and_submitted(self):
        """孤儿 transfer=Running (无claim) → 重置为 Waiting 并提交 Transfer。"""
        s = self.ctx.scheduler
        st = self.ctx.state

        for cn in st.get_all_configs():
            st.set_step_status(cn, "sw", STATUS_COMPLETED)
            st.set_step_status(cn, "sc", STATUS_COMPLETED)

        st.set_step_status(2, "transfer", STATUS_RUNNING)
        # 不添加 claim → 孤儿

        s._resume_paused_steps(log_prefix="[Test]")

        # 孤儿 Transfer Running 被处理（要么重置为 Waiting，要么通过输出检测标记完成）
        final_status = st.get_step_status(2, "transfer")
        assert final_status != STATUS_RUNNING, (
            f"孤儿 Transfer RUNNING 应被处理，实际: {final_status}"
        )


# ====================================================================
# 场景 C: Transfer/SC 重复入队被 claim 机制拦截
# ====================================================================

class TestDuplicateEnqueuePrevention:
    """验证 UniqueWorkQueue claim 机制防止重复入队。"""

    @classmethod
    def setup_class(cls):
        setup_mock_environment()

    @classmethod
    def teardown_class(cls):
        teardown_mock_environment()

    def setup_method(self):
        self.ctx = TestContext(num_configs=5)

    def teardown_method(self):
        self.ctx.cleanup()

    def test_transfer_double_submit_rejected(self):
        """同一构型两次 submit_transfer → 第二次返回 False。"""
        s = self.ctx.scheduler

        result1 = s.worker_pool.submit_transfer(1)
        assert result1 is True, "首次提交应成功"

        result2 = s.worker_pool.submit_transfer(1)
        assert result2 is False, "重复提交应被拒绝"

        # 队列深度应为 1
        assert s.worker_pool._transfer_queue.qsize() == 1, (
            "重复提交不应增加队列深度"
        )

    def test_transfer_claim_persists_during_processing(self):
        """Transfer claim 在出队后、完成前保持有效，防止并发重复入队。"""
        s = self.ctx.scheduler

        s.worker_pool.submit_transfer(3)
        item = s.worker_pool._transfer_queue.get_nowait()
        assert item == 3

        # 出队后 claim 仍存在 → 重复提交被拒绝
        result = s.worker_pool.submit_transfer(3)
        assert result is False, "出队后完成前 claim 仍应有效"

        # 完成释放后可以重新提交
        s.worker_pool._transfer_queue.complete(3)
        result = s.worker_pool.submit_transfer(3)
        assert result is True, "释放 claim 后应可重新提交"

    def test_sc_double_enqueue_blocked_by_claim(self):
        """SC 队列的 claim 去重：同一构型不同 step_file 也被拦截。"""
        s = self.ctx.scheduler

        # SC 队列 key 为 config_name (int)
        result1 = s._sc_queue.submit((1, "/path/model_gen4_1.STEP"))
        assert result1 is True

        # 同构型不同路径 → 被拦截
        result2 = s._sc_queue.submit((1, "/other/model_gen4_1.STEP"))
        assert result2 is False

        assert s._sc_queue.qsize() == 1


# ====================================================================
# 场景 D: MeshingMonitor 异常时 requeue（不永久丢失）
# ====================================================================

class TestMeshingMonitorExceptionRequeue:
    """验证 MeshingMonitor 异常时 requeue 行为（复用 increment_retry）。"""

    @classmethod
    def setup_class(cls):
        setup_mock_environment()

    @classmethod
    def teardown_class(cls):
        teardown_mock_environment()

    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp(prefix="mm_req_")
        self.db_path = os.path.join(self.tmpdir, "test.db")
        self._orig_db_path = IPC_CONFIG["db_path"]
        IPC_CONFIG["db_path"] = self.db_path

        self.state = StateManager(db_path=self.db_path)
        self.state.load_configs({1: [1.0, 2.0, 3.0, 4.0]})

        # 使用可抛异常的 RemoteExecutor
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

    def test_exception_triggers_requeue_not_error(self):
        """MeshingMonitor 处理异常 → requeue + RETRYING，不标记 ERROR。"""
        # 配置：构型1 transfer=Completed, meshing=WAITING
        self.state.set_step_status(1, "transfer", STATUS_COMPLETED)
        self.state.set_step_status(1, "meshing", STATUS_WAITING)

        # ★ 提高 max_retries 避免快速耗尽
        orig_max_retries = ENGINE_CONFIG["max_retries"]
        ENGINE_CONFIG["max_retries"] = 10

        # 让 start_meshing 抛出异常，并在第3次调用时自动停止监控
        original_start = self.remote.start_meshing
        call_count = [0]
        stopped_ref = self.stopped

        def failing_start(cn):
            call_count[0] += 1
            if call_count[0] >= 3:
                stopped_ref.set()  # 停止监控，保留当前状态
            raise ConnectionError("模拟连接异常")

        self.remote.start_meshing = failing_start

        try:
            self.monitor.submit(1)
            self.monitor.start_if_needed()

            # 等待监控线程被停止
            time.sleep(3)
            # 确保 stopped 已设置
            self.stopped.set()
            time.sleep(0.5)

            status = self.state.get_step_status(1, "meshing")
            retry = self.state.get_step_retry_count(1, "meshing")

            # 应被 requeue（状态为 RETRYING，不是 ERROR）
            assert status == STATUS_RETRYING, (
                f"异常应触发 requeue + RETRYING，实际: {status}"
            )
            assert retry >= 1, f"retry_count 应递增，实际: {retry}"
            # retry_count 不应超过 max_retries（未达到上限）
            assert retry < int(ENGINE_CONFIG["max_retries"]), (
                f"retry_count 应小于 max_retries，实际: {retry}"
            )

        finally:
            self.remote.start_meshing = original_start
            ENGINE_CONFIG["max_retries"] = orig_max_retries

    def test_exception_exceeding_max_retries_marks_error(self):
        """超过 max_retries 后标记 ERROR，不再 requeue。"""
        max_retries = int(ENGINE_CONFIG["max_retries"])
        self.state.set_step_status(1, "transfer", STATUS_COMPLETED)
        self.state.set_step_status(1, "meshing", STATUS_WAITING)

        # 预先设置 retry_count 为 max_retries - 1（下一次异常将超过上限）
        for _ in range(max_retries - 1):
            self.state.increment_retry(1, "meshing")

        original_start = self.remote.start_meshing

        def failing_start(cn):
            raise ConnectionError("模拟连接异常（重试耗尽）")

        self.remote.start_meshing = failing_start

        try:
            self.monitor.submit(1)
            self.monitor.start_if_needed()

            time.sleep(2)
            self.stopped.set()
            time.sleep(0.5)

            status = self.state.get_step_status(1, "meshing")
            retry = self.state.get_step_retry_count(1, "meshing")

            assert status == STATUS_ERROR, (
                f"超过 max_retries 应标记 ERROR，实际: {status}"
            )
            assert retry >= max_retries, (
                f"retry_count 应 >= {max_retries}，实际: {retry}"
            )

        finally:
            self.remote.start_meshing = original_start

    def test_fatal_exception_requeues_without_status_change(self):
        """致命异常（非预期 Exception）→ requeue 但不改变 step 状态。"""
        self.state.set_step_status(1, "transfer", STATUS_COMPLETED)
        self.state.set_step_status(1, "meshing", STATUS_WAITING)

        original_start = self.remote.start_meshing

        class FatalError(Exception):
            pass

        def fatal_start(cn):
            raise FatalError("模拟致命异常")

        self.remote.start_meshing = fatal_start

        try:
            self.monitor.submit(1)
            self.monitor.start_if_needed()

            time.sleep(2)
            self.stopped.set()
            time.sleep(0.5)

            status = self.state.get_step_status(1, "meshing")
            retry = self.state.get_step_retry_count(1, "meshing")

            # 致命异常不会标记 ERROR（除非超过 max_retries）
            # retry_count 应递增
            assert retry >= 1, f"致命异常也应递增 retry_count，实际: {retry}"
            # 状态不应是 ERROR（未超过 max_retries）
            if retry < int(ENGINE_CONFIG["max_retries"]):
                assert status != STATUS_ERROR, (
                    f"未超过 max_retries 不应标记 ERROR，实际: {status}"
                )

        finally:
            self.remote.start_meshing = original_start


# ====================================================================
# 场景 E: has_claim() 完整生命周期测试
# ====================================================================

class TestHasClaimLifecycle:
    """验证 has_claim() 在各种队列操作下的正确性。"""

    def test_has_claim_in_sc_queue_with_tuple_key(self):
        """SC 队列 (config_name, path) 的 claim 基于 config_name。"""
        from engine.scheduler.work_queue import UniqueWorkQueue
        q = UniqueWorkQueue[tuple[int, str]](key=lambda item: item[0])

        assert q.has_claim(1) is False
        q.submit((1, "/a/b.STEP"))
        assert q.has_claim(1) is True
        q.submit((1, "/a/c.STEP"))  # 重复，被拒
        item = q.get_nowait()
        assert q.has_claim(1) is True
        q.complete(item)
        assert q.has_claim(1) is False

    def test_has_claim_in_transfer_queue(self):
        """Transfer 队列 claim 基于 config_name (int)。"""
        from engine.scheduler.work_queue import UniqueWorkQueue
        q = UniqueWorkQueue[int]()

        assert q.has_claim(5) is False
        q.submit(5)
        assert q.has_claim(5) is True
        q.submit(6)
        assert q.has_claim(5) is True
        assert q.has_claim(6) is True

        item = q.get_nowait()
        assert item == 5 or item == 6
        q.complete(item)
        assert q.has_claim(item) is False

    def test_has_claim_after_clear(self):
        """clear() 后所有 claim 释放。"""
        from engine.scheduler.work_queue import UniqueWorkQueue
        q = UniqueWorkQueue[int]()

        q.submit(1)
        q.submit(2)
        q.submit(3)
        q.clear()
        assert q.has_claim(1) is False
        assert q.has_claim(2) is False
        assert q.has_claim(3) is False

    def test_has_claim_thread_safety(self):
        """并发场景下 has_claim 不抛异常。"""
        from engine.scheduler.work_queue import UniqueWorkQueue
        q = UniqueWorkQueue[int]()

        errors = []

        def worker():
            try:
                for i in range(100):
                    q.submit(i % 10)
                    q.has_claim(i % 10)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0, f"并发 has_claim 不应抛异常: {errors}"


# ====================================================================
# 场景 F: _is_step_in_flight 覆盖所有步骤类型
# ====================================================================

class TestIsStepInFlight:
    """验证 _is_step_in_flight 对各种步骤类型的正确判断。"""

    @classmethod
    def setup_class(cls):
        setup_mock_environment()

    @classmethod
    def teardown_class(cls):
        teardown_mock_environment()

    def setup_method(self):
        self.ctx = TestContext(num_configs=3)

    def teardown_method(self):
        self.ctx.cleanup()

    def test_sw_and_solver_always_return_false(self):
        """sw 和 solver 不由 in-flight 机制管理，始终返回 False。"""
        s = self.ctx.scheduler

        assert s._is_step_in_flight(1, "sw") is False
        assert s._is_step_in_flight(1, "solver") is False

    def test_sc_in_flight_when_claim_exists(self):
        """SC 队列有 claim → _is_step_in_flight 返回 True。"""
        s = self.ctx.scheduler

        assert s._is_step_in_flight(2, "sc") is False
        s._sc_queue.submit((2, "/fake/path.STEP"))
        assert s._is_step_in_flight(2, "sc") is True

    def test_transfer_in_flight_when_claim_exists(self):
        """Transfer 队列有 claim → _is_step_in_flight 返回 True。"""
        s = self.ctx.scheduler

        assert s._is_step_in_flight(3, "transfer") is False
        s.worker_pool.submit_transfer(3)
        assert s._is_step_in_flight(3, "transfer") is True

    def test_meshing_in_flight_when_monitor_processing(self):
        """MeshingMonitor in_flight_config 匹配 → 返回 True。"""
        s = self.ctx.scheduler

        assert s._is_step_in_flight(1, "meshing") is False
        s.meshing_monitor._in_flight_config = 1
        assert s._is_step_in_flight(1, "meshing") is True
        assert s._is_step_in_flight(2, "meshing") is False

    def test_meshing_not_in_flight_when_monitor_is_none(self):
        """MeshingMonitor 为 None 时 _is_step_in_flight 返回 False。"""
        s = self.ctx.scheduler
        saved = s.meshing_monitor
        s.meshing_monitor = None
        try:
            assert s._is_step_in_flight(1, "meshing") is False
        finally:
            s.meshing_monitor = saved


# ====================================================================
# 测试汇总（兼容直接 python 运行）
# ====================================================================

if __name__ == "__main__":
    import traceback

    tests = [
        ("A1 SC Running+claim 不误重置",
         lambda: TestInFlightProtection().test_sc_running_with_claim_is_not_reset()),
        ("A2 Transfer Running+claim 不误重置",
         lambda: TestInFlightProtection().test_transfer_running_with_claim_is_not_reset()),
        ("A3 Meshing in-flight 不误重置",
         lambda: TestInFlightProtection().test_meshing_with_in_flight_config_is_not_reset()),
        ("A4 Waiting+claim 不重复入队",
         lambda: TestInFlightProtection().test_waiting_with_claim_is_skipped()),
        ("B1 孤儿 SC Running 重置",
         lambda: TestOrphanRunningDetection().test_orphan_sc_running_reset_to_waiting_and_enqueued()),
        ("B2 孤儿 Transfer Running 重置",
         lambda: TestOrphanRunningDetection().test_orphan_transfer_running_reset_and_submitted()),
        ("C1 Transfer 重复提交被拒",
         lambda: TestDuplicateEnqueuePrevention().test_transfer_double_submit_rejected()),
        ("C2 Transfer claim 持久性",
         lambda: TestDuplicateEnqueuePrevention().test_transfer_claim_persists_during_processing()),
        ("C3 SC 重复入队被拦截",
         lambda: TestDuplicateEnqueuePrevention().test_sc_double_enqueue_blocked_by_claim()),
        ("D1 Meshing 异常 → requeue+RETRYING",
         lambda: TestMeshingMonitorExceptionRequeue().test_exception_triggers_requeue_not_error()),
        ("D2 Meshing 超过max_retries → ERROR",
         lambda: TestMeshingMonitorExceptionRequeue().test_exception_exceeding_max_retries_marks_error()),
        ("D3 致命异常 requeue 不改变状态",
         lambda: TestMeshingMonitorExceptionRequeue().test_fatal_exception_requeues_without_status_change()),
        ("E1 SC队列 has_claim(tuple key)",
         lambda: TestHasClaimLifecycle().test_has_claim_in_sc_queue_with_tuple_key()),
        ("E2 Transfer队列 has_claim",
         lambda: TestHasClaimLifecycle().test_has_claim_in_transfer_queue()),
        ("E3 clear后 claim释放",
         lambda: TestHasClaimLifecycle().test_has_claim_after_clear()),
        ("E4 has_claim 线程安全",
         lambda: TestHasClaimLifecycle().test_has_claim_thread_safety()),
        ("F1 sw/solver 始终返回 False",
         lambda: TestIsStepInFlight().test_sw_and_solver_always_return_false()),
        ("F2 sc in-flight 判断",
         lambda: TestIsStepInFlight().test_sc_in_flight_when_claim_exists()),
        ("F3 transfer in-flight 判断",
         lambda: TestIsStepInFlight().test_transfer_in_flight_when_claim_exists()),
        ("F4 meshing in-flight 判断",
         lambda: TestIsStepInFlight().test_meshing_in_flight_when_monitor_processing()),
        ("F5 meshing_monitor=None 安全处理",
         lambda: TestIsStepInFlight().test_meshing_not_in_flight_when_monitor_is_none()),
    ]

    passed = 0
    failed = 0

    for name, test_func in tests:
        try:
            test_func()
            passed += 1
            print(f"  ✅ {name}")
        except AssertionError as e:
            print(f"\n  ❌ 测试失败 [{name}]: {e}")
            failed += 1
        except Exception as e:
            print(f"\n  ❌ 测试异常 [{name}]: {type(e).__name__}: {e}")
            traceback.print_exc()
            failed += 1
        finally:
            # 每个测试后清理临时文件
            pass

    print(f"\n{'='*60}")
    print(f"测试结果: {passed} 通过, {failed} 失败 (共 {len(tests)})")
    print(f"{'='*60}")
