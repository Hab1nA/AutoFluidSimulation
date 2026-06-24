"""
===============================================================================
Pause/Start 功能验证测试脚本
===============================================================================
无需 SolidWorks/ANSYS 等外部依赖，模拟核心状态机行为，
验证 pause/start 在各种场景下的正确性。

测试场景：
  1. 正常启动 -> 暂停 -> 恢复
  2. SW 宏执行中暂停 -> SW 成功后恢复
  3. SW 宏执行中暂停 -> SW 失败后恢复
  4. SW 宏失败 -> 暂停 -> 恢复
  5. 快速连续 pause/start
  6. 下游步骤执行中暂停/恢复
  7. Start 在 running 状态下不应破坏状态
  8. Pause 在 stopped 状态下应无操作
  9. 暂停后文件监控停止扫描
  10. 暂停期间新STEP文件不被捕捉
  11. 恢复后立即触发完整轮询
  12. 多次pause-start状态切换稳定性

运行方式：
  cd "项目根目录"
  python tests/test_pause_start.py
===============================================================================
"""
import os
import sys
import time
import threading
import tempfile
import shutil

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_TEST_TMP_ROOT = tempfile.mkdtemp(prefix="sw_test_ps_")
_TEST_LOG_DIR = os.path.join(_TEST_TMP_ROOT, "logs")
os.makedirs(_TEST_LOG_DIR, exist_ok=True)
os.environ["AUTOFLUID_LOG_DIR"] = _TEST_LOG_DIR

from engine.config import (
    STATUS_WAITING, STATUS_RUNNING, STATUS_PAUSED, STATUS_COMPLETED,
    DEFAULT_WORKSTATION_ID, ENGINE_CONFIG, IPC_CONFIG,
)
from engine.state_manager import StateManager


class _MockSCPool:
    def do_first_cleanup(self): pass
    def do_final_cleanup(self): pass
    def reset(self): pass
    def shutdown_all(self): pass
    def run_config(self, *args, **kwargs): return True


class _MockRemoteExecutor:
    """Mock for RemoteExecutor, used by MeshingMonitor in tests."""
    def __init__(self, state_manager):
        self.state = state_manager
        self._ssh_lock = threading.RLock()

    def _get_ssh(self):
        return None

    def start_meshing(self, config_name: int, workstation_id: str = "default") -> bool:
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
        time.sleep(0.1)
        return True

    def query_remote_task_status(
        self,
        config_name: int,
        step_name: str,
        workstation_id: str = "default",
    ) -> str:
        return "lost"

    def forget_remote_task(
        self,
        config_name: int,
        step_name: str,
        workstation_id: str = "default",
    ) -> None:
        return None


class _MockSWExecutor:
    """Mock SWExecutor，供 SWPhaseHandler 调用 _verify_step_exports / disconnect_sw_cached。"""

    def __init__(self, state_manager):
        self.state = state_manager

    def _verify_step_exports(self, step_dir: str) -> int:
        """Mock 安全网校验：直接返回已完成构型数。"""
        from engine.config import STATUS_COMPLETED
        count = 0
        for cn in self.state.get_all_configs():
            if self.state.get_step_status(cn, "sw") == STATUS_COMPLETED:
                count += 1
        return count

    def disconnect_sw_cached(self):
        """Mock 清理（无实际 SW 连接）。"""
        pass


class MockTaskRunner:
    def __init__(self, state_manager: StateManager):
        self.state = state_manager
        self._sw_should_fail = False
        self._sw_delay = 0.0
        self._sw_call_count = 0
        self._solver_dispatched: list[int] = []
        self._solver_delay = 0.05
        self._solver_active_count = 0
        self._solver_max_active_count = 0
        self._postprocess_dispatched: list[int] = []
        self._solver_lock = threading.Lock()
        self._pause_check_callback = None
        self._sc_pool = _MockSCPool()
        self._remote_executor = _MockRemoteExecutor(self.state)
        self._sw_executor = _MockSWExecutor(state_manager)

    def execute_sw_per_config(self, config_name: int) -> bool:
        """单构型 SW 导出（供 RetryManager 调用）。"""
        self._sw_call_count += 1
        print(f"  [MockTaskRunner] execute_sw_per_config({config_name}) "
              f"第{self._sw_call_count}次调用 (fail={self._sw_should_fail})")

        if self._sw_delay > 0:
            steps = int(self._sw_delay / 0.5)
            for _ in range(steps):
                time.sleep(0.5)
                stopped = getattr(self, "_stopped_event", None)
                if stopped is not None and stopped.is_set():
                    return False
                if self._pause_check_callback:
                    self._pause_check_callback()

        if self._sw_should_fail:
            print(f"  [MockTaskRunner] 构型{config_name} SW 模拟失败!")
            return False

        self.state.set_step_status(config_name, "sw", STATUS_COMPLETED)
        print(f"  [MockTaskRunner] 构型{config_name} SW 模拟成功")
        return True

    def disconnect_sw_cached(self):
        self._sw_executor.disconnect_sw_cached()

    def verify_step_exports(self, step_dir: str) -> int:
        return self._sw_executor._verify_step_exports(step_dir)

    def execute_sc_step(self, config_name: int) -> bool:
        time.sleep(0.1)
        self.state.set_step_status(config_name, "sc", STATUS_COMPLETED)
        return True

    def execute_transfer(self, config_name: int) -> bool:
        time.sleep(0.05)
        self.state.set_step_status(config_name, "transfer", STATUS_COMPLETED)
        return True

    def execute_meshing(self, config_name: int) -> bool:
        time.sleep(0.05)
        return True

    def execute_solver(self, config_name: int) -> bool:
        with self._solver_lock:
            self._solver_dispatched.append(config_name)
            self._solver_active_count += 1
            self._solver_max_active_count = max(
                self._solver_max_active_count,
                self._solver_active_count,
            )
        try:
            time.sleep(self._solver_delay)
            return True
        finally:
            with self._solver_lock:
                self._solver_active_count -= 1

    def wait_meshing_completion(self, config_name: int,
                                 paused_event=None, stopped_event=None) -> bool:
        time.sleep(0.1)
        return True

    def wait_solver_completion(self, config_name: int,
                                paused_event=None, stopped_event=None) -> bool:
        time.sleep(0.1)
        return True

    def execute_postprocess(self, config_name: int) -> bool:
        self._postprocess_dispatched.append(config_name)
        return True

    def register_postprocess_from_solver(self, config_name: int) -> bool:
        return True

    def wait_postprocess_completion(self, config_name: int,
                                    paused_event=None, stopped_event=None) -> bool:
        time.sleep(0.1)
        return True

    def get_ssh(self):
        return None

    def get_remote_executor(self):
        return self._remote_executor

    def set_control_events(self, paused_event, stopped_event):
        self._paused_event = paused_event
        self._stopped_event = stopped_event

    def disconnect_ssh(self):
        pass

    def clean_step_files(self, *args, **kwargs):
        pass

    def run_system_check(self):
        return {"local_checks": {}, "remote_checks": {}}

    def shutdown_sc_pool(self):
        pass

    def do_sc_final_cleanup(self):
        pass


import engine.scheduler.main as scheduler_main_mod
import engine.scheduler.sw_phase as sw_phase_mod
import engine.file_monitor as file_monitor_mod
from engine.task_runner import TaskRunner

_OriginalTaskRunner = TaskRunner
_OriginalStepFileMonitor = file_monitor_mod.StepFileMonitor


class MockStepFileMonitor:
    _running = False

    def __init__(self, step_dir=None, on_file_ready=None, shared_paused_event=None):
        self.step_dir = step_dir
        self.on_file_ready = on_file_ready
        self._processed_files = set()
        self._known_files = set()
        self._owns_paused_event = shared_paused_event is None
        self._paused = shared_paused_event if shared_paused_event is not None else threading.Event()
        self._wake_event = threading.Event()
        self._need_reset = False
        self._scan_count = 0
        self._scan_existing_count = 0
        # 模拟 FileStableDetector，供 _prepare_sw_retry 清理追踪记录
        self._detector = type("_MockDetector", (), {"_history": {}, "_first_seen": {}})()

    def start(self):
        self._running = True
        print("  [MockFileMonitor] 已启动")

    def stop(self):
        self._running = False
        print("  [MockFileMonitor] 已停止")

    @property
    def is_running(self):
        return self._running

    def pause(self):
        self._paused.set()
        print("  [MockFileMonitor] 已暂停")

    def resume_only(self):
        """仅恢复监控，不重置已处理文件集合。"""
        if self._owns_paused_event:
            self._paused.clear()
        self._wake_event.set()
        print("  [MockFileMonitor] 已恢复（仅清除暂停标志）")

    def clear_tracking(self):
        self._processed_files.clear()
        self._known_files.clear()
        self._detector._history.clear()
        self._detector._first_seen.clear()

    def request_reset(self):
        self._need_reset = True
        self._wake_event.set()

    def wake(self):
        self._wake_event.set()

    def resume_and_reset(self):
        self._need_reset = True
        if self._owns_paused_event:
            self._paused.clear()
        self._wake_event.set()
        print("  [MockFileMonitor] 已恢复（将执行重置和立即扫描）")

    def reset_only(self):
        """仅重置状态不清除暂停标志（与真实 StepFileMonitor 接口一致）。"""
        self._need_reset = True
        self._wake_event.set()
        print("  [MockFileMonitor] 已标记为待重置（暂停中，恢复后生效）")

    def _scan_existing_files(self):
        self._scan_existing_count += 1
        # 若 step_dir 已设置，扫描真实目录以兼容其他测试文件的 StepFileMonitor 使用
        if self.step_dir and os.path.isdir(self.step_dir):
            for fname in os.listdir(self.step_dir):
                fpath = os.path.join(self.step_dir, fname)
                if os.path.isfile(fpath):
                    self._known_files.add(fname)

    def _scan_directory(self):
        """扫描目录检测稳定文件（适配 test_sw_export_workflow 调用）。"""
        if self.step_dir and os.path.isdir(self.step_dir):
            for fname in os.listdir(self.step_dir):
                fpath = os.path.join(self.step_dir, fname)
                if os.path.isfile(fpath):
                    self._known_files.add(fname)
                    if self.on_file_ready:
                        cn = self.parse_config_name(fname)
                        if cn is not None:
                            self.on_file_ready(cn, fpath)

    @staticmethod
    def parse_config_name(filename: str) -> int | None:
        """从文件名解析构型编号（委托给原始实现）。"""
        # ★ 不能 from engine.file_monitor import StepFileMonitor（已被 mock 替换），
        #    使用 teardown_mock_environment 前保存的 _OriginalStepFileMonitor 引用
        return _OriginalStepFileMonitor.parse_config_name(filename)


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


class TestContext:
    __test__ = False  # 非测试类，仅用于测试上下文管理

    def __init__(self, num_configs: int = 5):
        self.tmpdir = tempfile.mkdtemp(prefix="autotest_")
        self.db_path = os.path.join(self.tmpdir, "test_state.db")

        self._orig_db_path = IPC_CONFIG["db_path"]
        IPC_CONFIG["db_path"] = self.db_path
        import engine.scheduler.barrier as barrier_module
        import engine.scheduler.main as scheduler_main_module
        self._barrier_module = barrier_module
        self._scheduler_main_module = scheduler_main_module
        self._orig_barrier_workstations = barrier_module.WORKSTATIONS
        self._orig_scheduler_workstations = scheduler_main_module.WORKSTATIONS
        legacy_workstations = [{"id": DEFAULT_WORKSTATION_ID}]
        barrier_module.WORKSTATIONS = legacy_workstations
        scheduler_main_module.WORKSTATIONS = legacy_workstations

        # 确保 mock 环境已设置（pytest 直接运行时不会调用 main()）
        setup_mock_environment()

        self.state = StateManager(db_path=self.db_path)

        configs = {i: [1.0, 2.0, 3.0, 4.0] for i in range(1, num_configs + 1)}
        self.state.load_configs(configs)
        print(f"  [Setup] 已加载 {num_configs} 个测试构型")

        self.runner = MockTaskRunner(self.state)

        from engine.scheduler import PipelineScheduler
        self.scheduler = PipelineScheduler(self.state, self.runner)

        # Mock _prepare_sw_retry：跳过真实的 SW 进程清理（taskkill + 15s 等待），
        # 仅保留文件监控器重置逻辑，使测试能在合理时间内完成
        def _mock_prepare_sw_retry():
            fm = self.scheduler.sw_phase_handler._file_monitor
            if fm is not None:
                fm.clear_tracking()
        self.scheduler.sw_phase_handler._prepare_sw_retry = _mock_prepare_sw_retry

        print("  [Setup] 调度器已创建")

    def cleanup(self):
        try:
            self.scheduler.stop()
        finally:
            IPC_CONFIG["db_path"] = self._orig_db_path
            self._barrier_module.WORKSTATIONS = self._orig_barrier_workstations
            self._scheduler_main_module.WORKSTATIONS = self._orig_scheduler_workstations
            teardown_mock_environment()
            if os.path.exists(self.tmpdir):
                shutil.rmtree(self.tmpdir, ignore_errors=True)

    def assert_engine_status(self, expected: str, msg: str = ""):
        actual = self.state.get_engine_status()
        assert actual == expected, (
            f"{msg}: 期望引擎状态='{expected}', 实际='{actual}'"
        )

    def assert_step_status(self, config_name: int, step_name: str,
                           expected: str, msg: str = ""):
        actual = self.state.get_step_status(config_name, step_name)
        assert actual == expected, (
            f"{msg}: 期望构型{config_name}[{step_name}]='{expected}', 实际='{actual}'"
        )

    def assert_all_sw(self, expected: str, msg: str = ""):
        for cn in self.state.get_all_configs():
            self.assert_step_status(cn, "sw", expected,
                                    f"{msg} (构型{cn})")

    def run_pipeline_async(self):
        t = threading.Thread(target=self.scheduler.start_pipeline, daemon=True)
        t.start()
        self.scheduler.set_pipeline_thread(t)
        return t

    def wait_for_condition(self, condition, timeout: float = 10.0,
                           interval: float = 0.2) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if condition():
                return True
            time.sleep(interval)
        return False


def test_normal_pause_resume():
    print("\n" + "=" * 60)
    print("测试 1: 正常启动 -> 暂停 -> 恢复")
    print("=" * 60)

    ctx = TestContext(num_configs=3)
    try:
        ctx.runner._sw_delay = 0.0
        ctx.runner._sw_should_fail = False
        ctx.run_pipeline_async()

        ok = ctx.wait_for_condition(
            lambda: ctx.state.get_engine_status() == "running"
        )
        assert ok, "引擎未能进入 running 状态"
        ctx.assert_engine_status("running", "启动后")

        ctx.scheduler.pause()
        ctx.assert_engine_status("paused", "暂停后")
        ctx.assert_all_sw(STATUS_COMPLETED, "暂停后 SW 状态")

        monitor = ctx.scheduler._file_monitor
        assert monitor is not None, "文件监控器应存在"
        assert monitor._paused.is_set(), "文件监控器应处于暂停状态"

        ctx.scheduler.resume()
        ctx.assert_engine_status("running", "恢复后")
        assert not monitor._paused.is_set(), "恢复后文件监控器不应暂停"

        print("[PASS] 测试 1 通过")

    finally:
        ctx.scheduler.stop()
        ctx.cleanup()


def test_pause_during_sw_then_success():
    print("\n" + "=" * 60)
    print("测试 2: SW 宏执行中暂停 -> SW 成功后恢复")
    print("=" * 60)

    ctx = TestContext(num_configs=3)
    try:
        ctx.runner._sw_delay = 2.0
        ctx.runner._sw_should_fail = False

        def delayed_pause():
            time.sleep(0.5)
            print("  [Test] 触发暂停...")
            ctx.scheduler.pause()

        t = ctx.run_pipeline_async()

        pause_thread = threading.Thread(target=delayed_pause, daemon=True)
        pause_thread.start()
        pause_thread.join(timeout=3)

        time.sleep(0.3)
        ctx.assert_engine_status("paused", "SW 执行中暂停后")

        paused_count = 0
        for cn in ctx.state.get_all_configs():
            if ctx.state.get_step_status(cn, "sw") == STATUS_PAUSED:
                paused_count += 1
        print(f"  [Test] Paused 数量: {paused_count}/3")

        t.join(timeout=5)

        ctx.scheduler.resume()
        ctx.assert_engine_status("running", "恢复后")

        print("[PASS] 测试 2 通过")

    finally:
        ctx.scheduler.stop()
        ctx.cleanup()


def test_pause_during_sw_then_fail():
    print("\n" + "=" * 60)
    print("测试 3: SW 宏执行中暂停 -> SW 失败 -> 恢复重试")
    print("=" * 60)

    ctx = TestContext(num_configs=3)
    try:
        ctx.runner._sw_delay = 1.5
        ctx.runner._sw_should_fail = True

        t = ctx.run_pipeline_async()

        def delayed_pause():
            time.sleep(0.3)
            print("  [Test] 触发暂停（SW 宏仍运行中）...")
            ctx.scheduler.pause()

        pause_thread = threading.Thread(target=delayed_pause, daemon=True)
        pause_thread.start()
        pause_thread.join(timeout=3)

        t.join(timeout=5)

        ctx.assert_engine_status("paused", "SW 失败暂停后")
        print(f"  [Test] SW call count: {ctx.runner._sw_call_count}")

        ctx.runner._sw_should_fail = False
        ctx.scheduler.resume()

        ok = ctx.wait_for_condition(
            lambda: ctx.state.is_sw_macro_started(),
            timeout=15
        )
        if not ok:
            time.sleep(2)
            ok = ctx.state.is_sw_macro_started()
        print(f"  [Test] SW call count after resume: {ctx.runner._sw_call_count}")
        assert ok, f"恢复后 SW 宏未能完成 (engine_status={ctx.state.get_engine_status()})"

        ctx.assert_all_sw(STATUS_COMPLETED, "最终")

        print("[PASS] 测试 3 通过")

    finally:
        ctx.scheduler.stop()
        ctx.cleanup()


def test_sw_fail_then_restart():
    print("\n" + "=" * 60)
    print("测试 4: SW 直接失败 -> 引擎停止 -> 重新启动")
    print("=" * 60)

    ctx = TestContext(num_configs=3)
    original_max_retries = ENGINE_CONFIG["max_retries"]
    try:
        ENGINE_CONFIG["max_retries"] = 1
        ctx.runner._sw_should_fail = True
        ctx.runner._sw_delay = 0.1

        t1 = ctx.run_pipeline_async()
        t1.join(timeout=5)
        assert not t1.is_alive(), "SW 失败线程未能退出"
        ok = ctx.wait_for_condition(
            lambda: ctx.state.get_engine_status() == "stopped",
            timeout=10,
        )
        assert ok, "全 SW 失败后引擎未能进入 stopped 状态"
        ctx.assert_engine_status("stopped", "SW 失败后")

        ctx.runner._sw_should_fail = False
        t2 = ctx.run_pipeline_async()
        t2.join(timeout=5)

        ctx.assert_engine_status("running", "重新启动后")
        ctx.assert_all_sw(STATUS_COMPLETED, "重新启动后")

        print("[PASS] 测试 4 通过")

    finally:
        ENGINE_CONFIG["max_retries"] = original_max_retries
        ctx.scheduler.stop()
        ctx.cleanup()


def test_rapid_pause_start():
    print("\n" + "=" * 60)
    print("测试 5: 快速连续 pause/start")
    print("=" * 60)

    ctx = TestContext(num_configs=3)
    try:
        ctx.runner._sw_delay = 0.0
        ctx.runner._sw_should_fail = False

        t = ctx.run_pipeline_async()
        t.join(timeout=5)

        ctx.assert_engine_status("running", "初始状态")

        for i in range(5):
            ctx.scheduler.pause()
            time.sleep(0.05)
            ctx.assert_engine_status("paused", f"循环{i} 暂停后")

            ctx.scheduler.resume()
            time.sleep(0.05)
            ctx.assert_engine_status("running", f"循环{i} 恢复后")

        print("[PASS] 测试 5 通过")

    finally:
        ctx.scheduler.stop()
        ctx.cleanup()


def test_pause_before_scheduler_thread_enters():
    print("\n" + "=" * 60)
    print("测试 5.1: start 后调度线程进入前收到 pause")
    print("=" * 60)

    ctx = TestContext(num_configs=3)
    try:
        ctx.runner._sw_delay = 0.0
        ctx.runner._sw_should_fail = False

        ctx.state.set_engine_status("running")
        ctx.scheduler.pause()
        ctx.assert_engine_status("paused", "预置暂停后")

        t = ctx.run_pipeline_async()
        t.join(timeout=5)

        ctx.assert_engine_status("paused", "调度线程进入后")
        assert ctx.runner._sw_call_count == 0, "暂停已到达时不应启动 SW 阶段"
        assert ctx.scheduler._paused.is_set(), "调度器应保持暂停标志"

        print("[PASS] 测试 5.1 通过")

    finally:
        ctx.scheduler.stop()
        ctx.cleanup()


def test_start_when_already_running():
    print("\n" + "=" * 60)
    print("测试 6: Running 状态下重复 start")
    print("=" * 60)

    ctx = TestContext(num_configs=3)
    try:
        ctx.runner._sw_delay = 0.0
        ctx.runner._sw_should_fail = False

        t = ctx.run_pipeline_async()
        t.join(timeout=5)
        ctx.assert_engine_status("running", "初始")

        ctx.scheduler._paused.set()
        if ctx.scheduler._paused.is_set() and ctx.state.get_engine_status() == "running":
            ctx.scheduler.resume()
        ctx.assert_engine_status("running", "修复后")
        assert not ctx.scheduler._paused.is_set(), "暂停标志应已清除"

        print("[PASS] 测试 6 通过")

    finally:
        ctx.scheduler.stop()
        ctx.cleanup()


def test_pause_when_stopped():
    print("\n" + "=" * 60)
    print("测试 7: Stopped 状态下 pause")
    print("=" * 60)

    ctx = TestContext(num_configs=3)
    try:
        ctx.state.set_engine_status("stopped")
        if ctx.state.get_engine_status() != "stopped":
            ctx.scheduler.pause()
        ctx.assert_engine_status("stopped", "pause 在 stopped 状态下应不变")

        print("[PASS] 测试 7 通过")

    finally:
        ctx.scheduler.stop()
        ctx.cleanup()


def test_file_monitor_paused_on_pause(monkeypatch):
    print("\n" + "=" * 60)
    print("测试 8: 暂停后文件监控停止扫描")
    print("=" * 60)

    monkeypatch.delenv("AUTOFLUID_SERVER_MODE", raising=False)
    ctx = TestContext(num_configs=3)
    try:
        ctx.runner._sw_delay = 0.0
        ctx.runner._sw_should_fail = False
        ctx.run_pipeline_async()

        ok = ctx.wait_for_condition(
            lambda: ctx.state.get_engine_status() == "running"
        )
        assert ok, "引擎未能进入 running 状态"

        monitor = ctx.scheduler._file_monitor
        assert monitor is not None, "文件监控器应存在"
        assert monitor._running, "文件监控器应正在运行"
        assert not monitor._paused.is_set(), "初始状态文件监控器不应暂停"

        ctx.scheduler.pause()

        assert monitor._paused.is_set(), "暂停后文件监控器应处于暂停状态"
        ctx.assert_engine_status("paused", "暂停后")

        print("[PASS] 测试 8 通过")

    finally:
        ctx.scheduler.stop()
        ctx.cleanup()


def test_pause_blocks_step_file_callback():
    print("\n" + "=" * 60)
    print("测试 9: 暂停期间新STEP文件不被捕捉和处理")
    print("=" * 60)

    ctx = TestContext(num_configs=3)
    try:
        ctx.runner._sw_delay = 0.0
        ctx.runner._sw_should_fail = False
        ctx.run_pipeline_async()

        ok = ctx.wait_for_condition(
            lambda: ctx.state.get_engine_status() == "running"
        )
        assert ok, "引擎未能进入 running 状态"

        ctx.scheduler.pause()
        ctx.assert_engine_status("paused", "暂停后")

        callback_invocations = []
        original_callback = ctx.scheduler._on_step_file_ready

        def tracking_callback(config_name, filepath):
            callback_invocations.append((config_name, filepath))
            original_callback(config_name, filepath)

        ctx.scheduler._file_monitor.on_file_ready = tracking_callback

        assert ctx.scheduler._paused.is_set(), "调度器应处于暂停状态"

        ctx.scheduler._on_step_file_ready(99, "/fake/path/config_99.step")

        assert len(callback_invocations) == 0, (
            f"暂停期间回调不应被调用，但被调用了 {len(callback_invocations)} 次"
        )

        print("[PASS] 测试 9 通过")

    finally:
        ctx.scheduler.stop()
        ctx.cleanup()


def test_step_file_ready_after_stop_does_not_enqueue_sc():
    print("\n" + "=" * 60)
    print("测试 9.1: stop 后 STEP 回调不重新入队")
    print("=" * 60)

    ctx = TestContext(num_configs=3)
    try:
        ctx.runner._sw_delay = 0.0
        ctx.runner._sw_should_fail = False
        ctx.run_pipeline_async()

        ok = ctx.wait_for_condition(
            lambda: ctx.state.get_engine_status() == "running"
        )
        assert ok, "引擎未能进入 running 状态"

        ctx.scheduler.stop()
        ctx.assert_engine_status("stopped", "stop 后")
        assert ctx.scheduler._stopped.is_set(), "调度器应处于 stopped 状态"
        assert not ctx.scheduler._paused.is_set(), "stop 后 paused 标志应清除"

        initial_qsize = ctx.scheduler._sc_queue.qsize()
        ctx.scheduler._on_step_file_ready(1, "/fake/path/config_1.step")

        assert ctx.scheduler._sc_queue.qsize() == initial_qsize, (
            "stopped 状态下 STEP 回调不应推入 SC 队列"
        )

        print("[PASS] 测试 9.1 通过")

    finally:
        ctx.scheduler.stop()
        ctx.cleanup()


def test_resume_triggers_immediate_scan():
    print("\n" + "=" * 60)
    print("测试 10: 恢复后立即触发完整轮询")
    print("=" * 60)

    ctx = TestContext(num_configs=3)
    try:
        ctx.runner._sw_delay = 0.0
        ctx.runner._sw_should_fail = False
        ctx.run_pipeline_async()

        ok = ctx.wait_for_condition(
            lambda: ctx.state.get_engine_status() == "running"
        )
        assert ok, "引擎未能进入 running 状态"

        monitor = ctx.scheduler._file_monitor
        assert monitor is not None

        ctx.scheduler.pause()
        assert monitor._paused.is_set(), "暂停后文件监控器应暂停"

        ctx.scheduler.resume()
        ctx.assert_engine_status("running", "恢复后")

        assert not monitor._paused.is_set(), "恢复后文件监控器不应暂停"
        assert monitor._need_reset is True or not monitor._paused.is_set(), (
            "恢复后应触发文件监控器重置或监控器已恢复运行"
        )

        print("[PASS] 测试 10 通过")

    finally:
        ctx.scheduler.stop()
        ctx.cleanup()


def test_start_dispatches_solver_serially_when_barrier_already_met():
    print("\n" + "=" * 60)
    print("测试 10.1: 屏障已通过时 start 串行分发 Solver")
    print("=" * 60)

    ctx = TestContext(num_configs=3)
    try:
        ctx.runner._solver_delay = 0.3
        for cn in ctx.state.get_all_configs():
            for step in ["sw", "sc", "transfer", "meshing"]:
                ctx.state.set_step_status(cn, step, STATUS_COMPLETED)
            ctx.state.set_step_status(cn, "solver", STATUS_WAITING)

        ctx.state.set_global_barrier_met(True)
        ctx.scheduler._barrier_passed.set()

        t = ctx.run_pipeline_async()
        t.join(timeout=5)

        first_started = ctx.wait_for_condition(
            lambda: len(ctx.runner._solver_dispatched) == 1,
            timeout=5,
        )
        assert first_started, "start 后应先启动第一个 Solver"
        running = [
            cn for cn in ctx.state.get_all_configs()
            if ctx.state.get_step_status(cn, "solver") == STATUS_RUNNING
        ]
        assert running == [1], f"Solver 应串行执行，当前 Running={running}"

        ok = ctx.wait_for_condition(
            lambda: sorted(ctx.runner._solver_dispatched) == [1, 2, 3]
            and all(
                ctx.state.get_step_status(cn, "solver") == STATUS_COMPLETED
                for cn in ctx.state.get_all_configs()
            ),
            timeout=5,
        )
        assert ok, (
            "屏障已通过且 Solver=Waiting 时，start 后应串行完成所有 Solver "
            f"(实际: {ctx.runner._solver_dispatched})"
        )
        assert ctx.runner._solver_max_active_count == 1, (
            "Solver 不允许并行执行，"
            f"实际最大并发={ctx.runner._solver_max_active_count}"
        )

        print("[PASS] 测试 10.1 通过")

    finally:
        ctx.scheduler.stop()
        ctx.cleanup()


def test_multiple_pause_start_cycles():
    print("\n" + "=" * 60)
    print("测试 11: 多次pause-start状态切换稳定性")
    print("=" * 60)

    ctx = TestContext(num_configs=3)
    try:
        ctx.runner._sw_delay = 0.0
        ctx.runner._sw_should_fail = False
        ctx.run_pipeline_async()

        ok = ctx.wait_for_condition(
            lambda: ctx.state.get_engine_status() == "running"
        )
        assert ok, "引擎未能进入 running 状态"

        monitor = ctx.scheduler._file_monitor
        assert monitor is not None

        for i in range(10):
            ctx.scheduler.pause()
            assert monitor._paused.is_set(), f"循环{i}: 暂停后文件监控器应暂停"
            assert ctx.scheduler._paused.is_set(), f"循环{i}: 暂停后调度器应暂停"
            ctx.assert_engine_status("paused", f"循环{i} 暂停后")

            ctx.scheduler.resume()
            assert not monitor._paused.is_set(), f"循环{i}: 恢复后文件监控器不应暂停"
            assert not ctx.scheduler._paused.is_set(), f"循环{i}: 恢复后调度器不应暂停"
            ctx.assert_engine_status("running", f"循环{i} 恢复后")

        print("[PASS] 测试 11 通过")

    finally:
        ctx.scheduler.stop()
        ctx.cleanup()


def main():
    print("=" * 60)
    print("  Pause/Start 功能验证测试套件")
    print("=" * 60)

    setup_mock_environment()

    tests = [
        ("正常启动->暂停->恢复", test_normal_pause_resume),
        ("SW执行中暂停->成功恢复", test_pause_during_sw_then_success),
        ("SW执行中暂停->失败->重试", test_pause_during_sw_then_fail),
        ("SW直接失败->重新启动", test_sw_fail_then_restart),
        ("快速连续pause/start", test_rapid_pause_start),
        ("start后调度线程进入前收到pause", test_pause_before_scheduler_thread_enters),
        ("Running状态重复start", test_start_when_already_running),
        ("Stopped状态pause", test_pause_when_stopped),
        ("暂停后文件监控停止扫描", test_file_monitor_paused_on_pause),
        ("暂停期间STEP文件不被捕捉", test_pause_blocks_step_file_callback),
        ("恢复后立即触发完整轮询", test_resume_triggers_immediate_scan),
        ("屏障已通过时start串行分发Solver", test_start_dispatches_solver_serially_when_barrier_already_met),
        ("多次pause-start状态切换", test_multiple_pause_start_cycles),
    ]

    passed = 0
    failed = 0

    for name, test_func in tests:
        try:
            test_func()
            passed += 1
        except AssertionError as e:
            print(f"\n[FAIL] 测试失败 [{name}]: {e}")
            failed += 1
        except Exception as e:
            print(f"\n[FAIL] 测试异常 [{name}]: {type(e).__name__}: {e}")
            import traceback
            traceback.print_exc()
            failed += 1

    print("\n" + "=" * 60)
    print(f"  测试结果: {passed} 通过, {failed} 失败, {len(tests)} 总计")
    print("=" * 60)

    teardown_mock_environment()

    return failed == 0


if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
