"""
===============================================================================
文件监控模块单元测试 (M3)

覆盖：
- FileStableDetector.is_file_ready: 文件稳定/增长/不存在/最终稳定/长时间不稳定
- FileStableDetector.cleanup: 清理追踪记录
- StepFileMonitor.parse_config_name: 有效文件名/大小写/无关文件
- StepFileMonitor._compile_config_regex: 正则编译
- StepFileMonitor 生命周期: start/stop
- StepFileMonitor._scan_directory: 回调触发
- StepFileMonitor 暂停/恢复: pause/resume_only/resume_and_reset
- 内存泄漏防护: _history 被正确清理
===============================================================================
"""
from __future__ import annotations

import time
import threading

from engine.file_monitor import FileStableDetector, StepFileMonitor


# ====================================================================
# FileStableDetector 测试
# ====================================================================

class TestFileStableDetectorReady:
    """验证文件大小稳定的检测。"""

    def test_stable_file_returns_true(self, tmp_path):
        """文件大小在 stable_time 内多次采样不变 → True。"""
        detector = FileStableDetector(stable_time=0.3, check_interval=0.05)
        filepath = tmp_path / "stable.step"
        filepath.write_bytes(b"fixed content")

        # 第一次采样（仅 1 条记录，不足 2 条）
        assert detector.is_file_ready(str(filepath)) is False
        # 第二次采样（窗口内有 2 条记录且大小一致 → True）
        time.sleep(0.1)
        assert detector.is_file_ready(str(filepath)) is True

    def test_growing_file_returns_false(self, tmp_path):
        """文件大小持续增长 → False。"""
        detector = FileStableDetector(stable_time=0.3, check_interval=0.05)
        filepath = tmp_path / "growing.step"
        filepath.write_bytes(b"initial")

        assert detector.is_file_ready(str(filepath)) is False
        time.sleep(0.1)
        filepath.write_bytes(b"more content added")
        assert detector.is_file_ready(str(filepath)) is False

    def test_nonexistent_file_returns_false(self, tmp_path):
        """文件不存在 → False。"""
        detector = FileStableDetector()
        assert detector.is_file_ready(str(tmp_path / "nope.step")) is False

    def test_grows_then_stabilizes(self, tmp_path):
        """文件先增长后稳定 → 最终返回 True。"""
        detector = FileStableDetector(stable_time=0.2, check_interval=0.05)
        filepath = tmp_path / "eventual.step"

        # 写入初始内容
        filepath.write_bytes(b"phase1")
        assert detector.is_file_ready(str(filepath)) is False
        time.sleep(0.08)

        # 追加内容（仍在增长）
        filepath.write_bytes(b"phase1phase2")
        assert detector.is_file_ready(str(filepath)) is False
        time.sleep(0.08)

        # 停止追加，再采样几次使大小一致
        assert detector.is_file_ready(str(filepath)) is False
        time.sleep(0.12)
        assert detector.is_file_ready(str(filepath)) is True

    def test_cleanup_removes_tracking(self, tmp_path):
        """cleanup 清理追踪记录后，重新检测从头开始。"""
        detector = FileStableDetector(stable_time=0.3, check_interval=0.05)
        filepath = tmp_path / "clean.step"
        filepath.write_bytes(b"data")

        detector.is_file_ready(str(filepath))
        assert str(filepath) in detector._history

        detector.cleanup(str(filepath))
        assert str(filepath) not in detector._history
        assert str(filepath) not in detector._first_seen


class TestFileStableDetectorMemoryLeak:
    """验证长时间不稳定文件的内存泄漏防护。"""

    def test_abandons_long_unstable_file(self, tmp_path, caplog):
        """超过 stable_time*3 仍未稳定的文件应触发放弃监控警告。"""
        import logging
        detector = FileStableDetector(stable_time=0.1, check_interval=0.05)
        filepath = tmp_path / "slow.step"

        # 持续写入使文件始终不稳定
        with caplog.at_level(logging.WARNING):
            start = time.time()
            while time.time() - start < 0.5:
                filepath.write_bytes(b"x" * int((time.time() - start) * 1000))
                detector.is_file_ready(str(filepath))
                time.sleep(0.02)

        # 应至少触发一次放弃监控警告
        assert any("放弃监控" in r.message for r in caplog.records)

    def test_history_cleaned_after_ready(self, tmp_path):
        """文件检测成功后 _history 中不应残留该文件记录。"""
        detector = FileStableDetector(stable_time=0.15, check_interval=0.05)
        filepath = tmp_path / "done.step"
        filepath.write_bytes(b"final")

        # 第一次采样
        detector.is_file_ready(str(filepath))
        # 第二次采样（窗口内有 2 条且大小一致 → 成功并清理）
        time.sleep(0.08)
        detector.is_file_ready(str(filepath))

        assert str(filepath) not in detector._history


# ====================================================================
# StepFileMonitor.parse_config_name 测试
# ====================================================================

class TestParseConfigName:
    """验证文件名解析。"""

    def test_valid_filename(self):
        assert StepFileMonitor.parse_config_name("model_gen4.SLDPRT_5.step") == 5

    def test_large_config_number(self):
        assert StepFileMonitor.parse_config_name("model_gen4.SLDPRT_99.step") == 99

    def test_case_insensitive_upper(self):
        assert StepFileMonitor.parse_config_name("MODEL_GEN4.SLDPRT_3.STEP") == 3

    def test_case_insensitive_mixed(self):
        assert StepFileMonitor.parse_config_name("model_gen4.Sldprt_7.step") == 7

    def test_unrelated_file_returns_none(self):
        assert StepFileMonitor.parse_config_name("unrelated.step") is None

    def test_empty_string_returns_none(self):
        assert StepFileMonitor.parse_config_name("") is None

    def test_similar_but_wrong_format(self):
        """类似但格式不符的文件名应返回 None。"""
        assert StepFileMonitor.parse_config_name("model_gen4_5.step") is None


# ====================================================================
# StepFileMonitor._compile_config_regex 测试
# ====================================================================

class TestCompileConfigRegex:
    """验证正则编译。"""

    def test_valid_pattern(self):
        regex = StepFileMonitor._compile_config_regex("model_gen4.SLDPRT_{config}.step")
        assert regex is not None
        match = regex.match("model_gen4.SLDPRT_42.step")
        assert match is not None
        assert match.group("config") == "42"

    def test_pattern_without_placeholder_returns_none(self):
        assert StepFileMonitor._compile_config_regex("no_placeholder_here.step") is None

    def test_regex_case_insensitive(self):
        regex = StepFileMonitor._compile_config_regex("model_gen4.SLDPRT_{config}.step")
        assert regex is not None
        assert regex.match("MODEL_GEN4.SLDPRT_1.STEP") is not None


# ====================================================================
# StepFileMonitor 生命周期测试
# ====================================================================

class TestStepFileMonitorLifecycle:
    """验证监控器启动/停止。"""

    def test_start_and_stop(self, tmp_path):
        monitor = StepFileMonitor(
            step_dir=str(tmp_path),
            on_file_ready=lambda cn, fp: None,
        )
        monitor.start()
        assert monitor.is_running is True

        monitor.stop()
        assert monitor.is_running is False

    def test_double_start_no_error(self, tmp_path):
        """重复调用 start 不应抛异常。"""
        monitor = StepFileMonitor(step_dir=str(tmp_path))
        monitor.start()
        monitor.start()  # 不应报错
        monitor.stop()

    def test_stop_without_start_no_error(self, tmp_path):
        """未启动时调用 stop 不应抛异常。"""
        monitor = StepFileMonitor(step_dir=str(tmp_path))
        monitor.stop()  # 不应报错

    def test_start_creates_missing_step_directory(self, tmp_path):
        """监控启动时应确保 STEP 目录存在，避免启动后持续报告目录缺失。"""
        step_dir = tmp_path / "missing" / "step"
        monitor = StepFileMonitor(step_dir=str(step_dir))

        try:
            monitor.start()
            assert step_dir.is_dir()
        finally:
            monitor.stop()


# ====================================================================
# StepFileMonitor._scan_directory 回调测试
# ====================================================================

def _make_monitor(tmp_path, on_file_ready, paused_event=None):
    """创建 StepFileMonitor 并将 stable_time 降到测试友好值。"""
    monitor = StepFileMonitor(
        step_dir=str(tmp_path),
        on_file_ready=on_file_ready,
        shared_paused_event=paused_event,
    )
    # 将内部检测器的 stable_time 降到可测试范围
    monitor._detector.stable_time = 0.15
    return monitor


class TestScanDirectoryCallback:
    """验证文件扫描回调触发。"""

    def test_callback_triggered_on_stable_file(self, tmp_path):
        """稳定文件出现时回调被调用。"""
        results: list[tuple[int, str]] = []
        monitor = _make_monitor(tmp_path, lambda cn, fp: results.append((cn, fp)))

        # 创建 STEP 文件
        step_file = tmp_path / "model_gen4.SLDPRT_1.step"
        step_file.write_bytes(b"stable content")

        # 第一次扫描（加入 known_files，首次采样）
        monitor._scan_directory()
        assert len(results) == 0

        # 等待后再次扫描（文件大小稳定，第二次采样）
        time.sleep(0.1)
        monitor._scan_directory()
        assert len(results) == 0  # 需要 >=2 条记录

        # 第三次扫描（窗口内有足够记录）
        time.sleep(0.1)
        monitor._scan_directory()
        assert len(results) == 1
        assert results[0][0] == 1

    def test_callback_not_triggered_for_unknown_file(self, tmp_path):
        """非 STEP 格式的文件不触发回调。"""
        results: list[int] = []
        monitor = _make_monitor(tmp_path, lambda cn, fp: results.append(cn))

        (tmp_path / "readme.txt").write_text("not a step file")
        monitor._scan_directory()
        time.sleep(0.1)
        monitor._scan_directory()
        assert results == []

    def test_callback_not_triggered_during_pause(self, tmp_path):
        """暂停期间不触发回调。"""
        results: list[int] = []
        paused = threading.Event()
        monitor = _make_monitor(tmp_path, lambda cn, fp: results.append(cn), paused)

        # 创建文件
        step_file = tmp_path / "model_gen4.SLDPRT_2.step"
        step_file.write_bytes(b"data")

        # 暂停
        paused.set()
        monitor._scan_directory()
        time.sleep(0.1)
        monitor._scan_directory()
        assert results == []

    def test_nonexistent_directory_no_error(self, tmp_path):
        """不存在的目录不抛异常。"""
        monitor = _make_monitor(tmp_path / "nonexistent", lambda cn, fp: None)
        monitor._scan_directory()  # 不应抛异常

    def test_callback_exception_does_not_crash(self, tmp_path):
        """回调抛异常时监控器继续运行不崩溃。"""
        call_count = 0

        def bad_callback(cn, fp):
            nonlocal call_count
            call_count += 1
            raise RuntimeError("callback failed")

        monitor = _make_monitor(tmp_path, bad_callback)
        step_file = tmp_path / "model_gen4.SLDPRT_1.step"
        step_file.write_bytes(b"content")

        # 多次扫描：回调异常不应阻止后续扫描；使用 deadline 避免平台调度抖动。
        deadline = time.time() + 1.0
        while time.time() < deadline and call_count == 0:
            monitor._scan_directory()
            time.sleep(0.05)

        assert call_count >= 1  # 回调至少被调用一次


# ====================================================================
# StepFileMonitor 暂停/恢复测试
# ====================================================================

class TestPauseResume:
    """验证暂停/恢复行为。"""

    def test_pause_sets_flag(self, tmp_path):
        paused = threading.Event()
        monitor = StepFileMonitor(
            step_dir=str(tmp_path),
            shared_paused_event=paused,
        )
        monitor.pause()
        assert paused.is_set() is True

    def test_resume_only_preserves_shared_flag(self, tmp_path):
        """监控器不能清除由调度器拥有的共享暂停标志。"""
        paused = threading.Event()
        monitor = StepFileMonitor(
            step_dir=str(tmp_path),
            shared_paused_event=paused,
        )
        paused.set()
        monitor.resume_only()
        assert paused.is_set() is True

    def test_resume_and_reset_preserves_shared_flag(self, tmp_path):
        """reset 请求不能覆盖并发到达的共享 pause。"""
        paused = threading.Event()
        monitor = StepFileMonitor(
            step_dir=str(tmp_path),
            shared_paused_event=paused,
        )
        paused.set()
        monitor.resume_and_reset()
        assert paused.is_set() is True
        assert monitor._need_reset is True

    def test_reset_only_preserves_pause(self, tmp_path):
        """reset_only 不清除暂停标志。"""
        paused = threading.Event()
        monitor = StepFileMonitor(
            step_dir=str(tmp_path),
            shared_paused_event=paused,
        )
        paused.set()
        monitor.reset_only()
        assert paused.is_set() is True  # 暂停标志保持
        assert monitor._need_reset is True

    def test_clear_tracking_resets_monitor_state(self, tmp_path):
        """SW 重试应通过公共 API 清理全部文件追踪状态。"""
        monitor = StepFileMonitor(step_dir=str(tmp_path))
        monitor._processed_files.add("model_gen4.SLDPRT_1.step")
        monitor._known_files.add("model_gen4.SLDPRT_1.step")
        monitor._detector._history["step"] = [(0.0, 1)]
        monitor._detector._first_seen["step"] = 0.0

        monitor.clear_tracking()

        assert monitor._processed_files == set()
        assert monitor._known_files == set()
        assert monitor._detector._history == {}
        assert monitor._detector._first_seen == {}


# ====================================================================
# 集成测试：文件写入模拟
# ====================================================================

class TestFileWriteSimulation:
    """模拟 SW 导出 STEP 文件的完整流程。"""

    def test_simulated_sw_export(self, tmp_path):
        """模拟 SW 逐个导出 STEP 文件，监控器检测并回调。"""
        completed: list[int] = []
        monitor = _make_monitor(tmp_path, lambda cn, fp: completed.append(cn))

        # 模拟导出 3 个构型（一次性写入完成）
        for config in [1, 2, 3]:
            filepath = tmp_path / f"model_gen4.SLDPRT_{config}.step"
            filepath.write_bytes(b"final stable content here")

        # 第一次扫描（发现文件，首次采样）
        monitor._scan_directory()
        time.sleep(0.08)
        # 第二次扫描
        monitor._scan_directory()
        time.sleep(0.1)
        # 第三次扫描（窗口内有足够记录且大小一致）
        monitor._scan_directory()

        assert sorted(completed) == [1, 2, 3]

    def test_incremental_export(self, tmp_path):
        """模拟增量导出：先有 1 个文件，后来新增 1 个。"""
        completed: list[int] = []
        monitor = _make_monitor(tmp_path, lambda cn, fp: completed.append(cn))

        # 第一批：文件 1
        (tmp_path / "model_gen4.SLDPRT_1.step").write_bytes(b"content1")
        monitor._scan_directory()
        time.sleep(0.08)
        monitor._scan_directory()
        time.sleep(0.1)
        monitor._scan_directory()
        assert completed == [1]

        # 第二批：文件 2
        (tmp_path / "model_gen4.SLDPRT_2.step").write_bytes(b"content2")
        monitor._scan_directory()
        time.sleep(0.08)
        monitor._scan_directory()
        time.sleep(0.1)
        monitor._scan_directory()
        assert sorted(completed) == [1, 2]
