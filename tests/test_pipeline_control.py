"""流水线 pause/resume/stop 控制层测试。"""

from __future__ import annotations

import threading

from engine.scheduler.control import PipelineControl


def test_prepare_start_preserves_pause() -> None:
    """start 只能清除旧 stop，不能越过已到达的 pause。"""
    control = PipelineControl()
    control.stop()
    control.pause()

    assert control.prepare_start() is False
    assert control.paused_event.is_set() is True
    assert control.stopped_event.is_set() is False


def test_background_start_does_not_clear_stop() -> None:
    """resume 派生的后台启动线程不能覆盖后来到达的 stop。"""
    control = PipelineControl()
    control.stop()

    assert control.prepare_start(clear_stopped=False) is False
    assert control.stopped_event.is_set() is True


def test_pause_arriving_during_resume_wins_after_reconciliation() -> None:
    """resume 扫描期间到达的 pause 必须在 resume 完成后仍然有效。"""
    control = PipelineControl()
    control.pause()
    reconcile_started = threading.Event()
    allow_resume_to_finish = threading.Event()
    pause_finished = threading.Event()

    def resume() -> None:
        with control.resume_transition():
            reconcile_started.set()
            allow_resume_to_finish.wait(timeout=2)

    def pause() -> None:
        control.pause()
        pause_finished.set()

    resume_thread = threading.Thread(target=resume)
    resume_thread.start()
    assert reconcile_started.wait(timeout=1)

    pause_thread = threading.Thread(target=pause)
    pause_thread.start()
    assert pause_finished.wait(timeout=0.05) is False

    allow_resume_to_finish.set()
    resume_thread.join(timeout=1)
    pause_thread.join(timeout=1)

    assert pause_finished.is_set() is True
    assert control.paused_event.is_set() is True


def test_external_start_does_not_block_pause_acknowledgement() -> None:
    """pause 不等待已准入的长耗时副作用完成，后续副作用会被拒绝。"""
    control = PipelineControl()
    pause_finished = threading.Event()

    def pause() -> None:
        control.pause()
        pause_finished.set()

    with control.external_start() as allowed:
        assert allowed is True
        pause_thread = threading.Thread(target=pause)
        pause_thread.start()
        assert pause_finished.wait(timeout=1) is True

    pause_thread.join(timeout=1)
    assert pause_finished.is_set() is True
    with control.external_start() as allowed:
        assert allowed is False


def test_external_start_rejects_stopped_pipeline() -> None:
    """停止状态不能启动新的外部副作用。"""
    control = PipelineControl()
    control.stop()

    with control.external_start() as allowed:
        assert allowed is False


def test_sw_export_does_not_initialize_com_while_paused() -> None:
    """SW 单构型入口应在任何 COM 副作用前拒绝暂停状态。"""
    from executor.sw_executor import SWExecutor

    control = PipelineControl()
    control.pause()
    executor = SWExecutor(state_manager=None)
    executor.set_pipeline_control(control)

    assert executor.export_sw_per_config(1) is False
    assert executor._com_initialized is False


def test_sc_run_does_not_touch_slots_while_paused() -> None:
    """SC 入口应在槽位清理或启动前拒绝暂停状态。"""
    from engine.sc_process_pool import SCProcessPool

    control = PipelineControl()
    control.pause()
    pool = SCProcessPool.__new__(SCProcessPool)

    assert pool.run_config(1, pipeline_control=control) is False
