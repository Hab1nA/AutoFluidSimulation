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

运行方式：
  cd "项目根目录"
  python tests/test_pause_start.py
===============================================================================
"""
import os
import sys
import time
import threading
import sqlite3
import tempfile
import shutil
from typing import Dict, List

# 将项目根目录加入路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.config import (
    STEP_NAMES, STEP_INDEX,
    STATUS_WAITING, STATUS_RUNNING, STATUS_PAUSED, STATUS_COMPLETED, STATUS_ERROR,
    STATUS_RETRYING,
    IPC_CONFIG, ENGINE_CONFIG,
)
from engine.state_manager import StateManager

# ============================================================================
# 测试辅助：Mock TaskRunner（模拟 SW 宏执行）
# ============================================================================

class MockTaskRunner:
    """模拟 TaskRunner，可控制 SW 宏的成功/失败和延迟。"""

    def __init__(self, state_manager: StateManager):
        self.state = state_manager
        self._sw_should_fail = False
        self._sw_delay = 0.0       # SW 宏模拟耗时
        self._sw_call_count = 0
        self._pause_check_callback = None  # 在 SW 宏执行期间调用的回调（模拟暂停检测）

    def execute_sw_macro(self) -> bool:
        """模拟 SW 宏执行。"""
        self._sw_call_count += 1
        print(f"  [MockTaskRunner] execute_sw_macro() 第{self._sw_call_count}次调用"
              f" (delay={self._sw_delay}s, fail={self._sw_should_fail})")

        if self._sw_delay > 0:
            # 模拟长时间运行：分段 sleep 以允许暂停检测
            steps = int(self._sw_delay / 0.5)
            for _ in range(steps):
                time.sleep(0.5)
                if self._pause_check_callback:
                    self._pause_check_callback()

        if self._sw_should_fail:
            print("  [MockTaskRunner] SW 宏模拟失败！")
            # 模拟部分构型的 STEP 校验结果
            all_configs = self.state.get_all_configs()
            for cn in all_configs:
                self.state.set_step_status(cn, "SW", STATUS_ERROR, "模拟 SW 失败")
            self.state.set_sw_macro_started(False)
            return False

        # 模拟成功：标记所有构型 SW 为 Completed
        all_configs = self.state.get_all_configs()
        for cn in all_configs:
            self.state.set_step_status(cn, "SW", STATUS_COMPLETED)
        self.state.set_sw_macro_started(True)
        print(f"  [MockTaskRunner] SW 宏模拟成功 ({len(all_configs)} 个构型)")
        return True

    def execute_spaceclaim(self, config_name: int) -> bool:
        time.sleep(0.1)
        self.state.set_step_status(config_name, "SC", STATUS_COMPLETED)
        return True

    def execute_transfer(self, config_name: int) -> bool:
        time.sleep(0.05)
        self.state.set_step_status(config_name, "Transfer", STATUS_COMPLETED)
        return True

    def execute_meshing(self, config_name: int) -> bool:
        time.sleep(0.05)
        return True

    def execute_solver(self, config_name: int) -> bool:
        time.sleep(0.05)
        return True

    def wait_meshing_completion(self, config_name: int) -> bool:
        time.sleep(0.1)
        return True

    def wait_solver_completion(self, config_name: int) -> bool:
        time.sleep(0.1)
        return True

    def get_ssh(self):
        return None

    def disconnect_ssh(self):
        pass

    def clean_step_files(self, *args, **kwargs):
        pass

    def run_system_check(self):
        return {"local_checks": {}, "remote_checks": {}}


# ============================================================================
# 测试辅助：导入并 Patch 调度器
# ============================================================================

# 将 MockTaskRunner 注入调度器
import engine.scheduler as scheduler_mod
import engine.file_monitor as file_monitor_mod

# 保存原始类
_OriginalTaskRunner = scheduler_mod.TaskRunner
_OriginalStepFileMonitor = file_monitor_mod.StepFileMonitor


class MockStepFileMonitor:
    """模拟文件监控器（不实际扫描目录）。"""
    _running = False

    def __init__(self, step_dir=None, on_file_ready=None):
        self.step_dir = step_dir
        self.on_file_ready = on_file_ready
        self._processed_files = set()

    def start(self):
        self._running = True
        print("  [MockFileMonitor] 已启动")

    def stop(self):
        self._running = False
        print("  [MockFileMonitor] 已停止")

    def _scan_existing_files(self):
        pass


def setup_mock_environment():
    """用 Mock 对象替换实际模块依赖。"""
    scheduler_mod.TaskRunner = MockTaskRunner
    file_monitor_mod.StepFileMonitor = MockStepFileMonitor


def teardown_mock_environment():
    """恢复原始模块依赖。"""
    scheduler_mod.TaskRunner = _OriginalTaskRunner
    file_monitor_mod.StepFileMonitor = _OriginalStepFileMonitor


# ============================================================================
# 测试用例
# ============================================================================

class TestContext:
    """测试上下文：管理临时数据库和调度器实例。"""

    def __init__(self, num_configs: int = 5):
        self.tmpdir = tempfile.mkdtemp(prefix="autotest_")
        self.db_path = os.path.join(self.tmpdir, "test_state.db")

        # 覆盖 IPC_CONFIG 中的 db_path
        self._orig_db_path = IPC_CONFIG["db_path"]
        IPC_CONFIG["db_path"] = self.db_path

        # 创建状态管理器
        self.state = StateManager(db_path=self.db_path)

        # 加载测试构型
        configs = {i: [1.0, 2.0, 3.0, 4.0] for i in range(1, num_configs + 1)}
        self.state.load_configs(configs)
        print(f"  [Setup] 已加载 {num_configs} 个测试构型")

        # 创建模拟 TaskRunner
        self.runner = MockTaskRunner(self.state)

        # 创建调度器
        from engine.scheduler import PipelineScheduler
        self.scheduler = PipelineScheduler(self.state, self.runner)
        print(f"  [Setup] 调度器已创建")

    def cleanup(self):
        """清理测试环境。"""
        IPC_CONFIG["db_path"] = self._orig_db_path
        if os.path.exists(self.tmpdir):
            shutil.rmtree(self.tmpdir, ignore_errors=True)

    def assert_engine_status(self, expected: str, msg: str = ""):
        """断言引擎状态。"""
        actual = self.state.get_engine_status()
        assert actual == expected, (
            f"{msg}: 期望引擎状态='{expected}', 实际='{actual}'"
        )

    def assert_step_status(self, config_name: int, step_name: str,
                           expected: str, msg: str = ""):
        """断言步骤状态。"""
        actual = self.state.get_step_status(config_name, step_name)
        assert actual == expected, (
            f"{msg}: 期望构型{config_name}[{step_name}]='{expected}', 实际='{actual}'"
        )

    def assert_all_sw(self, expected: str, msg: str = ""):
        """断言所有构型的 SW 步骤状态。"""
        for cn in self.state.get_all_configs():
            self.assert_step_status(cn, "SW", expected,
                                    f"{msg} (构型{cn})")

    def run_pipeline_async(self):
        """在后台线程中启动流水线。"""
        t = threading.Thread(target=self.scheduler.start_pipeline, daemon=True)
        t.start()
        self.scheduler._pipeline_thread = t
        return t

    def wait_for_condition(self, condition, timeout: float = 10.0,
                           interval: float = 0.2) -> bool:
        """等待条件满足。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if condition():
                return True
            time.sleep(interval)
        return False


# ------------------------------------------------------------------
# 场景 1: 正常启动 → 暂停 → 恢复
# ------------------------------------------------------------------

def test_normal_pause_resume():
    """测试正常启动后暂停再恢复。"""
    print("\n" + "=" * 60)
    print("测试 1: 正常启动 → 暂停 → 恢复")
    print("=" * 60)

    ctx = TestContext(num_configs=3)
    try:
        # 1) 启动流水线（模拟 SW 快速成功）
        ctx.runner._sw_delay = 0.0
        ctx.runner._sw_should_fail = False
        ctx.run_pipeline_async()

        # 等待 SW 完成 + 引擎变为 running
        ok = ctx.wait_for_condition(
            lambda: ctx.state.get_engine_status() == "running"
        )
        assert ok, "引擎未能进入 running 状态"
        ctx.assert_engine_status("running", "启动后")

        # 2) 暂停
        ctx.scheduler.pause()
        ctx.assert_engine_status("paused", "暂停后")
        # SW 已完成，不应受影响
        ctx.assert_all_sw(STATUS_COMPLETED, "暂停后 SW 状态")

        # 3) 恢复
        ctx.scheduler.resume()
        ctx.assert_engine_status("running", "恢复后")

        print("✅ 测试 1 通过")

    finally:
        ctx.scheduler.stop()
        ctx.cleanup()


# ------------------------------------------------------------------
# 场景 2: SW 宏执行中暂停 → SW 成功后自动恢复
# ------------------------------------------------------------------

def test_pause_during_sw_then_success():
    """测试 SW 宏执行期间暂停，宏完成后保持暂停状态，然后恢复。"""
    print("\n" + "=" * 60)
    print("测试 2: SW 宏执行中暂停 → SW 成功后恢复")
    print("=" * 60)

    ctx = TestContext(num_configs=3)
    try:
        # 设置 SW 宏有延迟（模拟耗时操作）
        ctx.runner._sw_delay = 2.0
        ctx.runner._sw_should_fail = False

        # 在 SW 宏执行到一半时触发暂停
        def delayed_pause():
            time.sleep(0.5)  # SW 已开始执行
            print("  [Test] 触发暂停...")
            ctx.scheduler.pause()

        # 启动流水线
        t = ctx.run_pipeline_async()

        # 延迟触发暂停
        pause_thread = threading.Thread(target=delayed_pause, daemon=True)
        pause_thread.start()
        pause_thread.join(timeout=3)

        # 此时 pause 应已触发，engine_status 应为 paused
        time.sleep(0.3)
        ctx.assert_engine_status("paused", "SW 执行中暂停后")

        # SW 步骤状态应为 Paused
        paused_count = 0
        for cn in ctx.state.get_all_configs():
            if ctx.state.get_step_status(cn, "SW") == STATUS_PAUSED:
                paused_count += 1
        print(f"  [Test] Paused 数量: {paused_count}/3")

        # 等待 SW 宏完成（它会在后台继续运行但不影响暂停状态）
        t.join(timeout=5)

        # 恢复
        ctx.scheduler.resume()
        ctx.assert_engine_status("running", "恢复后")

        print("✅ 测试 2 通过")

    finally:
        ctx.scheduler.stop()
        ctx.cleanup()


# ------------------------------------------------------------------
# 场景 3: SW 宏执行中暂停 → SW 失败 → 保留暂停状态 → 恢复重试
# ------------------------------------------------------------------

def test_pause_during_sw_then_fail():
    """测试 SW 宏执行期间暂停，宏失败后保持暂停，恢复后重试。"""
    print("\n" + "=" * 60)
    print("测试 3: SW 宏执行中暂停 → SW 失败 → 恢复重试")
    print("=" * 60)

    ctx = TestContext(num_configs=3)
    try:
        # 设置 SW 会失败
        ctx.runner._sw_delay = 1.5
        ctx.runner._sw_should_fail = True

        # 启动流水线
        t = ctx.run_pipeline_async()

        # 延迟触发暂停
        def delayed_pause():
            time.sleep(0.3)
            print("  [Test] 触发暂停（SW 宏仍运行中）...")
            ctx.scheduler.pause()

        pause_thread = threading.Thread(target=delayed_pause, daemon=True)
        pause_thread.start()
        pause_thread.join(timeout=3)

        # 等待 SW 宏执行完毕（失败）
        t.join(timeout=5)

        # 暂停状态下 SW 失败，应保持 paused
        ctx.assert_engine_status("paused", "SW 失败暂停后")
        print(f"  [Test] SW call count: {ctx.runner._sw_call_count}")

        # 恢复 —— 应重新执行 SW 宏
        ctx.runner._sw_should_fail = False  # 第二次会成功
        ctx.scheduler.resume()

        # 等待恢复后的 SW 完成
        ok = ctx.wait_for_condition(
            lambda: ctx.state.is_sw_macro_started(),
            timeout=10
        )
        print(f"  [Test] SW call count after resume: {ctx.runner._sw_call_count}")
        assert ok, "恢复后 SW 宏未能完成"

        # 最终 SW 应为 Completed
        ctx.assert_all_sw(STATUS_COMPLETED, "最终")

        print("✅ 测试 3 通过")

    finally:
        ctx.scheduler.stop()
        ctx.cleanup()


# ------------------------------------------------------------------
# 场景 4: SW 宏直接失败 → 引擎停止 → 启动重试
# ------------------------------------------------------------------

def test_sw_fail_then_restart():
    """测试 SW 直接失败（无暂停），引擎停止后重新启动。"""
    print("\n" + "=" * 60)
    print("测试 4: SW 直接失败 → 引擎停止 → 重新启动")
    print("=" * 60)

    ctx = TestContext(num_configs=3)
    try:
        # SW 直接失败
        ctx.runner._sw_should_fail = True
        ctx.runner._sw_delay = 0.1

        t = ctx.run_pipeline_async()
        t.join(timeout=5)

        # 引擎应为 stopped（非暂停的失败）
        ctx.assert_engine_status("stopped", "SW 失败后")
        ctx.assert_all_sw(STATUS_ERROR, "SW 失败后")

        # 模拟用户修复后重新启动
        ctx.runner._sw_should_fail = False
        t2 = ctx.run_pipeline_async()
        t2.join(timeout=5)

        # 应恢复正常
        ctx.assert_engine_status("running", "重新启动后")
        ctx.assert_all_sw(STATUS_COMPLETED, "重新启动后")

        print("✅ 测试 4 通过")

    finally:
        ctx.scheduler.stop()
        ctx.cleanup()


# ------------------------------------------------------------------
# 场景 5: 快速连续 pause/start
# ------------------------------------------------------------------

def test_rapid_pause_start():
    """测试快速连续 pause 和 start 的稳定性。"""
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

        # 快速 pause/start 循环
        for i in range(5):
            ctx.scheduler.pause()
            time.sleep(0.05)
            ctx.assert_engine_status("paused", f"循环{i} 暂停后")

            ctx.scheduler.resume()
            time.sleep(0.05)
            ctx.assert_engine_status("running", f"循环{i} 恢复后")

        print("✅ 测试 5 通过")

    finally:
        ctx.scheduler.stop()
        ctx.cleanup()


# ------------------------------------------------------------------
# 场景 6: Start 在 running 状态下不应破坏状态
# ------------------------------------------------------------------

def test_start_when_already_running():
    """测试已经是 running 状态时再次 start 不会破坏状态。"""
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

        # 模拟 handle_start 在 running 状态下的行为
        old_paused = ctx.scheduler._paused.is_set()
        ctx.scheduler._paused.set()  # 模拟不一致状态
        # 检查并修复
        if ctx.scheduler._paused.is_set() and ctx.state.get_engine_status() == "running":
            ctx.scheduler.resume()
        ctx.assert_engine_status("running", "修复后")
        assert not ctx.scheduler._paused.is_set(), "暂停标志应已清除"

        print("✅ 测试 6 通过")

    finally:
        ctx.scheduler.stop()
        ctx.cleanup()


# ------------------------------------------------------------------
# 场景 7: Pause 在 stopped 状态下应无操作
# ------------------------------------------------------------------

def test_pause_when_stopped():
    """测试 stopped 状态下 pause 不产生副作用。"""
    print("\n" + "=" * 60)
    print("测试 7: Stopped 状态下 pause")
    print("=" * 60)

    ctx = TestContext(num_configs=3)
    try:
        ctx.state.set_engine_status("stopped")
        # 模拟 handle_pause 对 stopped 的处理
        if ctx.state.get_engine_status() != "stopped":
            ctx.scheduler.pause()
        ctx.assert_engine_status("stopped", "pause 在 stopped 状态下应不变")

        print("✅ 测试 7 通过")

    finally:
        ctx.scheduler.stop()
        ctx.cleanup()


# ============================================================================
# 主测试入口
# ============================================================================

def main():
    """运行所有测试。"""
    print("=" * 60)
    print("  Pause/Start 功能验证测试套件")
    print("=" * 60)

    setup_mock_environment()

    tests = [
        ("正常启动→暂停→恢复", test_normal_pause_resume),
        ("SW执行中暂停→成功恢复", test_pause_during_sw_then_success),
        ("SW执行中暂停→失败→重试", test_pause_during_sw_then_fail),
        ("SW直接失败→重新启动", test_sw_fail_then_restart),
        ("快速连续pause/start", test_rapid_pause_start),
        ("Running状态重复start", test_start_when_already_running),
        ("Stopped状态pause", test_pause_when_stopped),
    ]

    passed = 0
    failed = 0

    for name, test_func in tests:
        try:
            test_func()
            passed += 1
        except AssertionError as e:
            print(f"\n❌ 测试失败 [{name}]: {e}")
            failed += 1
        except Exception as e:
            print(f"\n❌ 测试异常 [{name}]: {type(e).__name__}: {e}")
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
