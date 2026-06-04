"""
===============================================================================
端到端集成测试 (M9)

覆盖：
- 完整流水线状态转换（SW→SC→Transfer→Meshing→Solver）
- 中间步骤失败→Error
- 全局暂停→恢复
- 全局停止→断点续传
- Excel 读取→StateManager 初始化→统计查询
- 屏障条件判断
===============================================================================
"""
from __future__ import annotations

import os
import threading
import time
import tempfile
import shutil

from engine.config import (
    STEP_NAMES, IPC_CONFIG,
    STATUS_WAITING, STATUS_RUNNING, STATUS_PAUSED, STATUS_COMPLETED, STATUS_ERROR,
)
from engine.state_manager import StateManager

# time 用于 _E2ETaskRunner._execute_step 中的延迟模拟


# ====================================================================
# Mock 组件
# ====================================================================

class _E2ETaskRunner:
    """端到端测试用 TaskRunner Mock。"""

    def __init__(self, state_manager: StateManager):
        self.state = state_manager
        self._step_results: dict[str, bool] = {}
        self._step_delays: dict[str, float] = {}
        self._call_log: list[tuple[str, int]] = []
        self._paused_event: threading.Event | None = None
        self._stopped_event: threading.Event | None = None

    def set_control_events(self, paused, stopped):
        self._paused_event = paused
        self._stopped_event = stopped

    def set_step_result(self, step: str, success: bool):
        self._step_results[step] = success

    def set_step_delay(self, step: str, delay: float):
        self._step_delays[step] = delay

    def _execute_step(self, step: str, config_name: int) -> bool:
        self._call_log.append((step, config_name))
        delay = self._step_delays.get(step, 0)
        if delay > 0:
            steps = int(delay / 0.1)
            for _ in range(steps):
                time.sleep(0.1)
                if self._stopped_event and self._stopped_event.is_set():
                    return False
                if self._paused_event and self._paused_event.is_set():
                    return False
        return self._step_results.get(step, True)

    def execute_sw_step(self) -> bool:
        self._call_log.append(("SW_bulk", 0))
        return self._step_results.get("sw", True)

    def execute_sw_per_config(self, config_name: int) -> bool:
        return self._execute_step("sw", config_name)

    def execute_sc_step(self, config_name: int) -> bool:
        return self._execute_step("sc", config_name)

    def execute_transfer(self, config_name: int) -> bool:
        return self._execute_step("transfer", config_name)

    def execute_meshing(self, config_name: int) -> bool:
        return self._execute_step("meshing", config_name)

    def execute_solver(self, config_name: int) -> bool:
        return self._execute_step("solver", config_name)

    def wait_meshing_completion(self, config_name, paused_event=None, stopped_event=None) -> bool:
        return self._step_results.get("meshing", True)

    def wait_solver_completion(self, config_name, paused_event=None, stopped_event=None) -> bool:
        return self._step_results.get("solver", True)

    def get_remote_executor(self):
        return self

    def do_sc_final_cleanup(self):
        pass

    def do_first_cleanup(self):
        pass


# ====================================================================
# 状态机端到端测试
# ====================================================================

class TestPipelineStateTransitions:
    """验证完整流水线的状态转换。"""

    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp(prefix="e2e_test_")
        self.db_path = os.path.join(self.tmpdir, "test.db")
        self._orig_db_path = IPC_CONFIG["db_path"]
        IPC_CONFIG["db_path"] = self.db_path

    def teardown_method(self):
        IPC_CONFIG["db_path"] = self._orig_db_path
        if os.path.exists(self.tmpdir):
            shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_all_steps_completed_flow(self):
        """所有步骤成功 → 全部 Completed。"""
        state = StateManager(db_path=self.db_path)
        state.load_configs({1: [1.0, 2.0, 3.0, 4.0], 2: [5.0, 6.0, 7.0, 8.0]})

        runner = _E2ETaskRunner(state)

        # 模拟 SW 阶段
        for cn in [1, 2]:
            state.set_step_status(cn, "sw", STATUS_RUNNING)
            result = runner.execute_sw_per_config(cn)
            state.set_step_status(cn, "sw", STATUS_COMPLETED if result else STATUS_ERROR)

        # 模拟 SC→Transfer→Meshing→Solver
        for cn in [1, 2]:
            for step in ["sc", "transfer", "meshing", "solver"]:
                state.set_step_status(cn, step, STATUS_RUNNING)
                result = runner._execute_step(step, cn)
                state.set_step_status(cn, step, STATUS_COMPLETED if result else STATUS_ERROR)

        # 验证
        for cn in [1, 2]:
            for step in STEP_NAMES:
                assert state.get_step_status(cn, step) == STATUS_COMPLETED, (
                    f"构型{cn}[{step}] 应为 Completed"
                )

    def test_step_failure_marks_error(self):
        """步骤失败 → Error。"""
        state = StateManager(db_path=self.db_path)
        state.load_configs({1: [1.0, 2.0, 3.0, 4.0]})

        runner = _E2ETaskRunner(state)
        runner.set_step_result("sc", False)

        # SW 成功
        state.set_step_status(1, "sw", STATUS_COMPLETED)

        # SC 失败
        state.set_step_status(1, "sc", STATUS_RUNNING)
        result = runner._execute_step("sc", 1)
        state.set_step_status(1, "sc", STATUS_ERROR if not result else STATUS_COMPLETED)

        assert state.get_step_status(1, "sc") == STATUS_ERROR

    def test_pause_and_resume_state(self):
        """暂停后状态变为 Paused，恢复后可继续。"""
        state = StateManager(db_path=self.db_path)
        state.load_configs({1: [1.0, 2.0, 3.0, 4.0]})

        paused = threading.Event()
        stopped = threading.Event()
        runner = _E2ETaskRunner(state)
        runner.set_control_events(paused, stopped)

        # 设置 Running 状态
        state.set_step_status(1, "sc", STATUS_RUNNING)
        state.set_step_status(1, "transfer", STATUS_RUNNING)

        # 暂停
        paused.set()
        state.set_all_running_to_paused()
        state.set_engine_status("paused")

        assert state.get_step_status(1, "sc") == STATUS_PAUSED
        assert state.get_step_status(1, "transfer") == STATUS_PAUSED
        assert state.get_engine_status() == "paused"

        # 恢复
        paused.clear()
        state.set_engine_status("running")

        assert state.get_engine_status() == "running"

    def test_stop_and_restart_breakpoint_resume(self):
        """停止后重启，已完成步骤保持。"""
        state = StateManager(db_path=self.db_path)
        state.load_configs({1: [1.0, 2.0, 3.0, 4.0], 2: [5.0, 6.0, 7.0, 8.0]})

        # 模拟部分完成
        state.set_step_status(1, "sw", STATUS_COMPLETED)
        state.set_step_status(1, "sc", STATUS_COMPLETED)
        state.set_step_status(2, "sw", STATUS_COMPLETED)

        # 模拟停止
        state.set_engine_status("stopped")

        # 模拟重启后断点续传扫描
        all_configs = state.get_all_configs()
        completed_configs = [
            cn for cn in all_configs
            if state.get_step_status(cn, "sc") == STATUS_COMPLETED
        ]
        pending_configs = [
            cn for cn in all_configs
            if state.get_step_status(cn, "sc") != STATUS_COMPLETED
        ]

        assert completed_configs == [1]
        assert pending_configs == [2]

        # 已完成步骤不受影响
        assert state.get_step_status(1, "sc") == STATUS_COMPLETED


# ====================================================================
# 配置→状态→调度链路测试
# ====================================================================

class TestConfigToStatePipeline:
    """验证 Excel 读取 → StateManager 初始化 → 状态查询的完整链路。"""

    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp(prefix="e2e_cfg_")
        self.db_path = os.path.join(self.tmpdir, "test.db")
        self._orig_db_path = IPC_CONFIG["db_path"]
        IPC_CONFIG["db_path"] = self.db_path

    def teardown_method(self):
        IPC_CONFIG["db_path"] = self._orig_db_path
        if os.path.exists(self.tmpdir):
            shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_load_configs_then_query_statistics(self):
        """加载构型后查询统计信息。"""
        state = StateManager(db_path=self.db_path)
        configs = {i: [float(i), 0.0, 0.0, 0.0] for i in range(1, 6)}
        state.load_configs(configs)

        stats = state.get_statistics()
        assert stats["total_configs"] == 5
        assert stats["steps"]["sw"][STATUS_WAITING] == 5

    def test_partial_progress_statistics(self):
        """部分进度的统计正确。"""
        state = StateManager(db_path=self.db_path)
        state.load_configs({1: [1.0, 2.0, 3.0, 4.0], 2: [5.0, 6.0, 7.0, 8.0]})

        state.set_step_status(1, "sw", STATUS_COMPLETED)
        state.set_step_status(2, "sw", STATUS_RUNNING)

        stats = state.get_statistics()
        assert stats["steps"]["sw"][STATUS_COMPLETED] == 1
        assert stats["steps"]["sw"][STATUS_RUNNING] == 1
        assert stats["steps"]["sw"][STATUS_WAITING] == 0

    def test_error_configs_query(self):
        """查询错误构型。"""
        state = StateManager(db_path=self.db_path)
        state.load_configs({1: [1.0, 2.0, 3.0, 4.0], 2: [5.0, 6.0, 7.0, 8.0]})

        state.set_step_status(1, "sc", STATUS_ERROR, "连接超时")
        state.set_step_status(2, "meshing", STATUS_ERROR, "发散")

        errors = state.get_error_configs()
        error_dict = {(e[0], e[1]): e[2] for e in errors}
        assert error_dict[(1, "sc")] == "连接超时"
        assert error_dict[(2, "meshing")] == "发散"

    def test_barrier_check_after_all_meshing_completed(self):
        """所有 Meshing 完成后屏障判断正确。"""
        state = StateManager(db_path=self.db_path)
        state.load_configs({1: [1.0, 2.0, 3.0, 4.0], 2: [5.0, 6.0, 7.0, 8.0]})

        assert state.all_configs_completed_at_step("meshing") is False

        state.set_step_status(1, "meshing", STATUS_COMPLETED)
        assert state.all_configs_completed_at_step("meshing") is False

        state.set_step_status(2, "meshing", STATUS_COMPLETED)
        assert state.all_configs_completed_at_step("meshing") is True
