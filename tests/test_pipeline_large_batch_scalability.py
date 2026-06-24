from __future__ import annotations

from pathlib import Path


def test_large_batch_status_counts_remain_bounded(tmp_path):
    from engine.config import STATUS_COMPLETED
    from engine.state_manager import StateManager

    state = StateManager(db_path=str(tmp_path / "state.db"))
    state.load_configs({
        config_name: [1.0, 2.0, 3.0, 4.0]
        for config_name in range(1, 151)
    })

    for config_name in range(1, 151):
        state.set_step_status(config_name, "meshing", STATUS_COMPLETED)

    counts = state.get_step_status_counts("meshing")

    assert counts[STATUS_COMPLETED] == 150


def test_sc_timeout_path_retires_persistent_slot_source_guard():
    source = Path("engine/sc_process_pool.py").read_text(encoding="utf-8")

    assert "_retire_persistent_slot_after_failure" in source
    assert "self._retire_persistent_slot_after_failure(slot, reason)" in source


def test_tui_has_no_main_loop_blocking_dashboard_poll_source_guard():
    source = Path("autofluid-tui/src/lib.rs").read_text(encoding="utf-8")

    assert "block_on(ipc.get_dashboard" not in source


def test_tui_has_no_main_loop_blocking_command_ipc_source_guard():
    source = Path("autofluid-tui/src/lib.rs").read_text(encoding="utf-8")

    forbidden = [
        "block_on(command::dispatch_command",
        "block_on(command::execute_confirm_action",
        "block_on(ctx.ipc.full_quit",
    ]
    for needle in forbidden:
        assert needle not in source
