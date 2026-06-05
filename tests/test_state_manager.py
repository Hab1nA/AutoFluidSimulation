"""
===============================================================================
StateManager 单元测试 (M2)

覆盖：
- 初始化：建表、PRAGMA WAL 模式
- load_configs: 正常载入、增量更新、移除旧构型、空字典
- get_all_configs: 返回排序列表
- get_config_params: 参数读取
- get/set_step_status: CRUD 往返
- get_all_steps_for_config: 全步骤详情
- get_all_statuses: 全构型全步骤状态
- increment_retry / get_step_retry_count: 重试计数
- set_meshing_running_if_idle: 原子防护
- reset_config_steps: 单构型/全构型/from_step
- reset_all: 全量重置
- get/set_engine_status: 引擎状态 CRUD
- set_all_running_to_paused: 批量状态切换
- is/set_sw_macro_started: SW 宏标志
- is/set_global_barrier_met: 全局屏障标志
- get_configs_at_step: 过滤查询
- all_configs_completed_at_step: 屏障判断
- get_error_configs: 错误查询
- get_statistics: 聚合统计
- 并发读写安全
===============================================================================
"""
from __future__ import annotations

import os
import threading
import tempfile
import shutil

import pytest

from engine.config import (
    STEP_NAMES,
    STATUS_WAITING, STATUS_RUNNING, STATUS_PAUSED, STATUS_RETRYING,
    STATUS_COMPLETED, STATUS_ERROR,
    IPC_CONFIG,
)
from engine.state_manager import StateManager


class _TmpDB:
    """临时数据库上下文管理器，确保每个测试使用隔离的数据库。"""

    def __init__(self):
        self.tmpdir = tempfile.mkdtemp(prefix="sm_test_")
        self.db_path = os.path.join(self.tmpdir, "test.db")
        self._orig_db_path = IPC_CONFIG["db_path"]
        IPC_CONFIG["db_path"] = self.db_path

    def __enter__(self) -> StateManager:
        return StateManager(db_path=self.db_path)

    def __exit__(self, *_):
        IPC_CONFIG["db_path"] = self._orig_db_path
        if os.path.exists(self.tmpdir):
            shutil.rmtree(self.tmpdir, ignore_errors=True)





# ====================================================================
# 初始化测试
# ====================================================================

class TestInit:
    """验证数据库初始化。"""

    def test_tables_created(self):
        with _TmpDB() as sm:
            import sqlite3
            conn = sqlite3.connect(sm.db_path)
            tables = {row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()}
            conn.close()
            assert "configs" in tables
            assert "steps" in tables
            assert "engine_state" in tables

    def test_wal_mode(self):
        with _TmpDB() as sm:
            import sqlite3
            conn = sqlite3.connect(sm.db_path)
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
            conn.close()
            assert mode.lower() == "wal"

    def test_engine_state_defaults(self):
        with _TmpDB() as sm:
            assert sm.get_engine_status() == "stopped"
            assert sm.is_sw_macro_started() is False
            assert sm.is_global_barrier_met() is False

    def test_indexes_created(self):
        with _TmpDB() as sm:
            import sqlite3
            conn = sqlite3.connect(sm.db_path)
            indexes = {row[1] for row in conn.execute(
                "SELECT * FROM sqlite_master WHERE type='index'"
            ).fetchall()}
            conn.close()
            assert "idx_steps_config" in indexes
            assert "idx_steps_status" in indexes

# ====================================================================
# load_configs 测试
# ====================================================================

class TestLoadConfigs:
    """验证构型数据同步。"""

    def test_normal_load(self):
        with _TmpDB() as sm:
            sm.load_configs({1: [1.0, 2.0, 3.0, 4.0], 2: [5.0, 6.0, 7.0, 8.0]})
            assert sm.get_all_configs() == [1, 2]

    def test_creates_step_records(self):
        with _TmpDB() as sm:
            sm.load_configs({1: [1.0, 2.0, 3.0, 4.0]})
            for step in STEP_NAMES:
                assert sm.get_step_status(1, step) == STATUS_WAITING

    def test_incremental_update_preserves_status(self):
        with _TmpDB() as sm:
            sm.load_configs({1: [1.0, 2.0, 3.0, 4.0]})
            sm.set_step_status(1, "sw", STATUS_COMPLETED)
            # 重新加载相同构型
            sm.load_configs({1: [1.0, 2.0, 3.0, 4.0]})
            assert sm.get_step_status(1, "sw") == STATUS_COMPLETED

    def test_new_config_added_incrementally(self):
        with _TmpDB() as sm:
            sm.load_configs({1: [1.0, 2.0, 3.0, 4.0]})
            sm.load_configs({1: [1.0, 2.0, 3.0, 4.0], 2: [5.0, 6.0, 7.0, 8.0]})
            assert sm.get_all_configs() == [1, 2]
            assert sm.get_step_status(2, "sw") == STATUS_WAITING

    def test_removed_config_deleted(self):
        with _TmpDB() as sm:
            sm.load_configs({1: [1.0, 2.0, 3.0, 4.0], 2: [5.0, 6.0, 7.0, 8.0]})
            sm.set_step_status(1, "sw", STATUS_COMPLETED)
            # 移除构型 1
            sm.load_configs({2: [5.0, 6.0, 7.0, 8.0]})
            assert sm.get_all_configs() == [2]

    def test_empty_dict_clears_all(self):
        with _TmpDB() as sm:
            sm.load_configs({1: [1.0, 2.0, 3.0, 4.0]})
            sm.load_configs({})
            assert sm.get_all_configs() == []

    def test_params_updated(self):
        with _TmpDB() as sm:
            sm.load_configs({1: [1.0, 2.0, 3.0, 4.0]})
            sm.load_configs({1: [10.0, 20.0, 30.0, 40.0]})
            params = sm.get_config_params(1)
            assert params == [10.0, 20.0, 30.0, 40.0]


# ====================================================================
# get_config_params 测试
# ====================================================================

class TestGetConfigParams:
    """验证参数读取。"""

    def test_returns_params(self):
        with _TmpDB() as sm:
            sm.load_configs({5: [1.1, 2.2, 3.3, 4.4]})
            assert sm.get_config_params(5) == [1.1, 2.2, 3.3, 4.4]

    def test_nonexistent_returns_none(self):
        with _TmpDB() as sm:
            assert sm.get_config_params(999) is None


# ====================================================================
# get/set_step_status 测试
# ====================================================================

class TestStepStatus:
    """验证步骤状态 CRUD。"""

    def test_set_and_get(self):
        with _TmpDB() as sm:
            sm.load_configs({1: [1.0, 2.0, 3.0, 4.0]})
            sm.set_step_status(1, "sw", STATUS_RUNNING)
            assert sm.get_step_status(1, "sw") == STATUS_RUNNING

    def test_error_message_persisted(self):
        with _TmpDB() as sm:
            sm.load_configs({1: [1.0, 2.0, 3.0, 4.0]})
            sm.set_step_status(1, "sc", STATUS_ERROR, "连接失败")
            # 通过 get_all_steps_for_config 验证错误消息
            steps = sm.get_all_steps_for_config(1)
            assert steps["sc"]["error_message"] == "连接失败"

    def test_invalid_status_raises(self):
        with _TmpDB() as sm:
            sm.load_configs({1: [1.0, 2.0, 3.0, 4.0]})
            with pytest.raises(ValueError):
                sm.set_step_status(1, "sw", "InvalidStatus")

    def test_nonexistent_step_name_does_not_crash(self):
        """不存在的步骤名不会导致崩溃（UPDATE 影响 0 行，查询返回默认值）。"""
        with _TmpDB() as sm:
            sm.load_configs({1: [1.0, 2.0, 3.0, 4.0]})
            # 非法步骤名不抛异常（UPDATE 影响 0 行）
            sm.set_step_status(1, "InvalidStep", STATUS_RUNNING)
            # 查询不存在的步骤返回默认值 Waiting
            assert sm.get_step_status(1, "InvalidStep") == STATUS_WAITING

    def test_nonexistent_step_returns_waiting(self):
        with _TmpDB() as sm:
            # 未 load_configs，步骤记录不存在
            assert sm.get_step_status(999, "sw") == STATUS_WAITING


# ====================================================================
# get_all_steps_for_config 测试
# ====================================================================

class TestGetAllStepsForConfig:
    """验证全步骤详情查询。"""

    def test_returns_all_steps(self):
        with _TmpDB() as sm:
            sm.load_configs({1: [1.0, 2.0, 3.0, 4.0]})
            steps = sm.get_all_steps_for_config(1)
            assert set(steps.keys()) == set(STEP_NAMES)
            for step_name in STEP_NAMES:
                assert "status" in steps[step_name]
                assert "retry_count" in steps[step_name]
                assert "error_message" in steps[step_name]

    def test_nonexistent_config_returns_empty(self):
        with _TmpDB() as sm:
            steps = sm.get_all_steps_for_config(999)
            assert steps == {}


# ====================================================================
# get_all_statuses 测试
# ====================================================================

class TestGetAllStatuses:
    """验证全构型全步骤状态查询。"""

    def test_returns_all_configs_and_steps(self):
        with _TmpDB() as sm:
            sm.load_configs({1: [1.0, 2.0, 3.0, 4.0], 2: [5.0, 6.0, 7.0, 8.0]})
            statuses = sm.get_all_statuses()
            assert 1 in statuses
            assert 2 in statuses
            assert set(statuses[1].keys()) == set(STEP_NAMES)

    def test_status_values_correct(self):
        with _TmpDB() as sm:
            sm.load_configs({1: [1.0, 2.0, 3.0, 4.0]})
            sm.set_step_status(1, "sw", STATUS_COMPLETED)
            sm.set_step_status(1, "sc", STATUS_RUNNING)
            statuses = sm.get_all_statuses()
            assert statuses[1]["sw"] == STATUS_COMPLETED
            assert statuses[1]["sc"] == STATUS_RUNNING


# ====================================================================
# increment_retry / get_step_retry_count 测试
# ====================================================================

class TestRetryCount:
    """验证重试计数。"""

    def test_increment_from_zero(self):
        with _TmpDB() as sm:
            sm.load_configs({1: [1.0, 2.0, 3.0, 4.0]})
            assert sm.get_step_retry_count(1, "sc") == 0
            count = sm.increment_retry(1, "sc")
            assert count == 1

    def test_increment_multiple(self):
        with _TmpDB() as sm:
            sm.load_configs({1: [1.0, 2.0, 3.0, 4.0]})
            sm.increment_retry(1, "sc")
            sm.increment_retry(1, "sc")
            count = sm.increment_retry(1, "sc")
            assert count == 3
            assert sm.get_step_retry_count(1, "sc") == 3

    def test_nonexistent_step_returns_zero(self):
        with _TmpDB() as sm:
            assert sm.get_step_retry_count(999, "sw") == 0

    def test_increment_nonexistent_step_logs_warning(self, caplog):
        """缺失步骤记录仍保持旧返回值，但要暴露数据不一致信号。"""
        caplog.set_level("WARNING")
        with _TmpDB() as sm:
            assert sm.increment_retry(999, "sw") == 0
        assert "increment_retry: 构型999 步骤sw 记录不存在" in caplog.text


# ====================================================================
# set_meshing_running_if_idle 测试
# ====================================================================

class TestSetMeshingRunningIfIdle:
    """验证 Meshing 原子防护。"""

    def test_first_set_succeeds(self):
        with _TmpDB() as sm:
            sm.load_configs({1: [1.0, 2.0, 3.0, 4.0], 2: [5.0, 6.0, 7.0, 8.0]})
            assert sm.set_meshing_running_if_idle(1) is True
            assert sm.get_step_status(1, "meshing") == STATUS_RUNNING

    def test_second_set_fails(self):
        with _TmpDB() as sm:
            sm.load_configs({1: [1.0, 2.0, 3.0, 4.0], 2: [5.0, 6.0, 7.0, 8.0]})
            sm.set_meshing_running_if_idle(1)
            assert sm.set_meshing_running_if_idle(2) is False

    def test_after_completed_allows_new(self):
        with _TmpDB() as sm:
            sm.load_configs({1: [1.0, 2.0, 3.0, 4.0], 2: [5.0, 6.0, 7.0, 8.0]})
            sm.set_meshing_running_if_idle(1)
            sm.set_step_status(1, "meshing", STATUS_COMPLETED)
            assert sm.set_meshing_running_if_idle(2) is True


# ====================================================================
# reset_config_steps 测试
# ====================================================================

class TestResetConfigSteps:
    """验证步骤重置。"""

    def test_reset_single_config_all_steps(self):
        with _TmpDB() as sm:
            sm.load_configs({1: [1.0, 2.0, 3.0, 4.0]})
            sm.set_step_status(1, "sw", STATUS_COMPLETED)
            sm.set_step_status(1, "sc", STATUS_ERROR, "失败")
            sm.reset_config_steps(1)
            assert sm.get_step_status(1, "sw") == STATUS_WAITING
            assert sm.get_step_status(1, "sc") == STATUS_WAITING

    def test_reset_from_step(self):
        with _TmpDB() as sm:
            sm.load_configs({1: [1.0, 2.0, 3.0, 4.0]})
            sm.set_step_status(1, "sw", STATUS_COMPLETED)
            sm.set_step_status(1, "sc", STATUS_COMPLETED)
            sm.set_step_status(1, "transfer", STATUS_RUNNING)
            sm.set_step_status(1, "meshing", STATUS_RUNNING)
            sm.set_step_status(1, "solver", STATUS_WAITING)
            sm.reset_config_steps(1, from_step="sc")
            assert sm.get_step_status(1, "sw") == STATUS_COMPLETED
            assert sm.get_step_status(1, "sc") == STATUS_WAITING
            assert sm.get_step_status(1, "transfer") == STATUS_WAITING
            assert sm.get_step_status(1, "meshing") == STATUS_WAITING
            assert sm.get_step_status(1, "solver") == STATUS_WAITING

    def test_reset_all_configs(self):
        with _TmpDB() as sm:
            sm.load_configs({1: [1.0, 2.0, 3.0, 4.0], 2: [5.0, 6.0, 7.0, 8.0]})
            sm.set_step_status(1, "sw", STATUS_COMPLETED)
            sm.set_step_status(2, "sw", STATUS_ERROR, "err")
            sm.reset_config_steps("all")
            assert sm.get_step_status(1, "sw") == STATUS_WAITING
            assert sm.get_step_status(2, "sw") == STATUS_WAITING

    def test_reset_sw_clears_sw_macro_started_when_no_completed(self):
        with _TmpDB() as sm:
            sm.load_configs({1: [1.0, 2.0, 3.0, 4.0]})
            sm.set_sw_macro_started(True)
            sm.set_step_status(1, "sw", STATUS_ERROR)
            sm.reset_config_steps(1, from_step="sw")
            assert sm.is_sw_macro_started() is False

    def test_reset_sw_keeps_sw_macro_started_when_other_completed(self):
        with _TmpDB() as sm:
            sm.load_configs({1: [1.0, 2.0, 3.0, 4.0], 2: [5.0, 6.0, 7.0, 8.0]})
            sm.set_sw_macro_started(True)
            sm.set_step_status(1, "sw", STATUS_COMPLETED)
            sm.set_step_status(2, "sw", STATUS_ERROR, "err")
            # 仅重置构型 2
            sm.reset_config_steps(2, from_step="sw")
            # 构型 1 仍有 SW=Completed，标志应保持
            assert sm.is_sw_macro_started() is True

    def test_retry_count_reset_to_zero(self):
        with _TmpDB() as sm:
            sm.load_configs({1: [1.0, 2.0, 3.0, 4.0]})
            sm.increment_retry(1, "sc")
            sm.increment_retry(1, "sc")
            sm.set_step_status(1, "sc", STATUS_ERROR, "连接失败")
            sm.reset_config_steps(1)
            assert sm.get_step_retry_count(1, "sc") == 0
            # error_message 也应被清除
            steps = sm.get_all_steps_for_config(1)
            assert steps["sc"]["error_message"] == ""


# ====================================================================
# reset_all 测试
# ====================================================================

class TestResetAll:
    """验证全量重置。"""

    def test_resets_all_steps_and_flags(self):
        with _TmpDB() as sm:
            sm.load_configs({1: [1.0, 2.0, 3.0, 4.0], 2: [5.0, 6.0, 7.0, 8.0]})
            sm.set_step_status(1, "sw", STATUS_COMPLETED)
            sm.set_step_status(2, "solver", STATUS_RUNNING)
            sm.set_sw_macro_started(True)
            sm.set_global_barrier_met(True)
            sm.reset_all()
            assert sm.get_step_status(1, "sw") == STATUS_WAITING
            assert sm.get_step_status(2, "solver") == STATUS_WAITING
            assert sm.is_sw_macro_started() is False
            assert sm.is_global_barrier_met() is False


# ====================================================================
# get/set_engine_status 测试
# ====================================================================

class TestEngineStatus:
    """验证引擎状态 CRUD。"""

    def test_default_is_stopped(self):
        with _TmpDB() as sm:
            assert sm.get_engine_status() == "stopped"

    def test_set_running(self):
        with _TmpDB() as sm:
            sm.set_engine_status("running")
            assert sm.get_engine_status() == "running"

    def test_set_paused(self):
        with _TmpDB() as sm:
            sm.set_engine_status("paused")
            assert sm.get_engine_status() == "paused"

    def test_invalid_status_raises(self):
        with _TmpDB() as sm:
            with pytest.raises(ValueError):
                sm.set_engine_status("invalid")


# ====================================================================
# set_all_running_to_paused 测试
# ====================================================================

class TestSetAllRunningToPaused:
    """验证批量状态切换。"""

    def test_running_to_paused(self):
        with _TmpDB() as sm:
            sm.load_configs({1: [1.0, 2.0, 3.0, 4.0]})
            sm.set_step_status(1, "sw", STATUS_RUNNING)
            sm.set_all_running_to_paused()
            assert sm.get_step_status(1, "sw") == STATUS_PAUSED

    def test_retrying_to_paused(self):
        with _TmpDB() as sm:
            sm.load_configs({1: [1.0, 2.0, 3.0, 4.0]})
            sm.set_step_status(1, "sc", STATUS_RETRYING)
            sm.set_all_running_to_paused()
            assert sm.get_step_status(1, "sc") == STATUS_PAUSED

    def test_completed_not_affected(self):
        with _TmpDB() as sm:
            sm.load_configs({1: [1.0, 2.0, 3.0, 4.0]})
            sm.set_step_status(1, "sw", STATUS_COMPLETED)
            sm.set_all_running_to_paused()
            assert sm.get_step_status(1, "sw") == STATUS_COMPLETED

    def test_waiting_not_affected(self):
        with _TmpDB() as sm:
            sm.load_configs({1: [1.0, 2.0, 3.0, 4.0]})
            sm.set_all_running_to_paused()
            assert sm.get_step_status(1, "sw") == STATUS_WAITING


# ====================================================================
# is/set_sw_macro_started 测试
# ====================================================================

class TestSwMacroStarted:
    """验证 SW 宏启动标志。"""

    def test_default_false(self):
        with _TmpDB() as sm:
            assert sm.is_sw_macro_started() is False

    def test_set_true(self):
        with _TmpDB() as sm:
            sm.set_sw_macro_started(True)
            assert sm.is_sw_macro_started() is True

    def test_set_false(self):
        with _TmpDB() as sm:
            sm.set_sw_macro_started(True)
            sm.set_sw_macro_started(False)
            assert sm.is_sw_macro_started() is False


# ====================================================================
# is/set_global_barrier_met 测试
# ====================================================================

class TestGlobalBarrierMet:
    """验证全局屏障标志。"""

    def test_default_false(self):
        with _TmpDB() as sm:
            assert sm.is_global_barrier_met() is False

    def test_set_true(self):
        with _TmpDB() as sm:
            sm.set_global_barrier_met(True)
            assert sm.is_global_barrier_met() is True

    def test_set_false(self):
        with _TmpDB() as sm:
            sm.set_global_barrier_met(True)
            sm.set_global_barrier_met(False)
            assert sm.is_global_barrier_met() is False


# ====================================================================
# get_configs_at_step 测试
# ====================================================================

class TestGetConfigsAtStep:
    """验证过滤查询。"""

    def test_with_status_filter(self):
        with _TmpDB() as sm:
            sm.load_configs({1: [1.0, 2.0, 3.0, 4.0], 2: [5.0, 6.0, 7.0, 8.0]})
            sm.set_step_status(1, "sw", STATUS_COMPLETED)
            sm.set_step_status(2, "sw", STATUS_RUNNING)
            completed = sm.get_configs_at_step("sw", STATUS_COMPLETED)
            assert completed == [1]

    def test_without_status_filter(self):
        with _TmpDB() as sm:
            sm.load_configs({1: [1.0, 2.0, 3.0, 4.0], 2: [5.0, 6.0, 7.0, 8.0]})
            all_at_sw = sm.get_configs_at_step("sw")
            assert sorted(all_at_sw) == [1, 2]

    def test_empty_result(self):
        with _TmpDB() as sm:
            sm.load_configs({1: [1.0, 2.0, 3.0, 4.0]})
            assert sm.get_configs_at_step("solver", STATUS_COMPLETED) == []


# ====================================================================
# all_configs_completed_at_step 测试
# ====================================================================

class TestAllConfigsCompletedAtStep:
    """验证屏障判断。"""

    def test_all_completed(self):
        with _TmpDB() as sm:
            sm.load_configs({1: [1.0, 2.0, 3.0, 4.0], 2: [5.0, 6.0, 7.0, 8.0]})
            sm.set_step_status(1, "meshing", STATUS_COMPLETED)
            sm.set_step_status(2, "meshing", STATUS_COMPLETED)
            assert sm.all_configs_completed_at_step("meshing") is True

    def test_not_all_completed(self):
        with _TmpDB() as sm:
            sm.load_configs({1: [1.0, 2.0, 3.0, 4.0], 2: [5.0, 6.0, 7.0, 8.0]})
            sm.set_step_status(1, "meshing", STATUS_COMPLETED)
            assert sm.all_configs_completed_at_step("meshing") is False

    def test_empty_configs_returns_true(self):
        with _TmpDB() as sm:
            assert sm.all_configs_completed_at_step("meshing") is True


# ====================================================================
# get_error_configs 测试
# ====================================================================

class TestGetErrorConfigs:
    """验证错误查询。"""

    def test_returns_errors(self):
        with _TmpDB() as sm:
            sm.load_configs({1: [1.0, 2.0, 3.0, 4.0], 2: [5.0, 6.0, 7.0, 8.0]})
            sm.set_step_status(1, "sc", STATUS_ERROR, "超时")
            sm.set_step_status(2, "solver", STATUS_ERROR, "发散")
            errors = sm.get_error_configs()
            assert len(errors) == 2
            error_tuples = {(e[0], e[1], e[2]) for e in errors}
            assert (1, "sc", "超时") in error_tuples
            assert (2, "solver", "发散") in error_tuples

    def test_no_errors_returns_empty(self):
        with _TmpDB() as sm:
            sm.load_configs({1: [1.0, 2.0, 3.0, 4.0]})
            assert sm.get_error_configs() == []


# ====================================================================
# get_statistics 测试
# ====================================================================

class TestGetStatistics:
    """验证聚合统计。"""

    def test_total_configs(self):
        with _TmpDB() as sm:
            sm.load_configs({1: [1.0, 2.0, 3.0, 4.0], 2: [5.0, 6.0, 7.0, 8.0]})
            stats = sm.get_statistics()
            assert stats["total_configs"] == 2

    def test_step_counts(self):
        with _TmpDB() as sm:
            sm.load_configs({1: [1.0, 2.0, 3.0, 4.0], 2: [5.0, 6.0, 7.0, 8.0]})
            sm.set_step_status(1, "sw", STATUS_COMPLETED)
            sm.set_step_status(2, "sw", STATUS_RUNNING)
            stats = sm.get_statistics()
            assert stats["steps"]["sw"][STATUS_COMPLETED] == 1
            assert stats["steps"]["sw"][STATUS_RUNNING] == 1
            assert stats["steps"]["sw"][STATUS_WAITING] == 0

    def test_error_count(self):
        with _TmpDB() as sm:
            sm.load_configs({1: [1.0, 2.0, 3.0, 4.0], 2: [5.0, 6.0, 7.0, 8.0]})
            sm.set_step_status(1, "sc", STATUS_ERROR, "err1")
            sm.set_step_status(2, "solver", STATUS_ERROR, "err2")
            stats = sm.get_statistics()
            assert stats["error_count"] == 2

    def test_empty_database(self):
        with _TmpDB() as sm:
            stats = sm.get_statistics()
            assert stats["total_configs"] == 0
            assert stats["error_count"] == 0
            for step_name in STEP_NAMES:
                for status in ["Waiting", "Running", "Paused", "Retrying", "Completed", "Error"]:
                    assert stats["steps"][step_name][status] == 0


# ====================================================================
# 并发读写安全测试
# ====================================================================

class TestConcurrency:
    """验证多线程同时读写不抛异常。"""

    def test_concurrent_writes(self):
        with _TmpDB() as sm:
            configs = {i: [float(i), 0.0, 0.0, 0.0] for i in range(1, 11)}
            sm.load_configs(configs)
            errors: list[Exception] = []

            def writer(cn: int):
                try:
                    for _ in range(20):
                        sm.set_step_status(cn, "sw", STATUS_RUNNING)
                        sm.set_step_status(cn, "sw", STATUS_COMPLETED)
                except Exception as e:
                    errors.append(e)

            threads = [threading.Thread(target=writer, args=(cn,)) for cn in range(1, 11)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=10)
            assert not errors, f"并发写入出错: {errors}"

    def test_concurrent_reads_and_writes(self):
        with _TmpDB() as sm:
            configs = {i: [float(i), 0.0, 0.0, 0.0] for i in range(1, 6)}
            sm.load_configs(configs)
            errors: list[Exception] = []
            stop_event = threading.Event()

            def reader():
                try:
                    while not stop_event.is_set():
                        sm.get_all_configs()
                        sm.get_all_statuses()
                        sm.get_engine_status()
                except Exception as e:
                    errors.append(e)

            def writer():
                try:
                    for cn in range(1, 6):
                        sm.set_step_status(cn, "sc", STATUS_RUNNING)
                        sm.set_step_status(cn, "sc", STATUS_COMPLETED)
                except Exception as e:
                    errors.append(e)

            readers = [threading.Thread(target=reader) for _ in range(3)]
            writers = [threading.Thread(target=writer) for _ in range(2)]
            for t in readers + writers:
                t.start()
            for t in writers:
                t.join(timeout=10)
            stop_event.set()
            for t in readers:
                t.join(timeout=5)
            assert not errors, f"并发读写出错: {errors}"

    def test_concurrent_increment_retry(self):
        with _TmpDB() as sm:
            sm.load_configs({1: [1.0, 2.0, 3.0, 4.0]})
            errors: list[Exception] = []

            def increment():
                try:
                    for _ in range(50):
                        sm.increment_retry(1, "sc")
                except Exception as e:
                    errors.append(e)

            threads = [threading.Thread(target=increment) for _ in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=10)
            assert not errors, f"并发重试计数出错: {errors}"
            assert sm.get_step_retry_count(1, "sc") == 200
