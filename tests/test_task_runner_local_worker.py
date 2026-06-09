from __future__ import annotations


def test_task_runner_delegates_sw_and_sc_to_local_worker_in_server_mode(monkeypatch) -> None:
    from engine.task_runner import TaskRunner

    calls: list[tuple[str, int | None]] = []

    class _Adapter:
        def execute_sw_step(self) -> bool:
            calls.append(("sw", None))
            return True

        def execute_sw_per_config(self, config_name: int) -> bool:
            calls.append(("sw_config", config_name))
            return True

        def execute_sc_step(self, config_name: int) -> bool:
            calls.append(("sc", config_name))
            return True

    runner = TaskRunner.__new__(TaskRunner)
    runner._local_worker_adapter = _Adapter()

    monkeypatch.setenv("AUTOFLUID_SERVER_MODE", "server")

    assert runner.execute_sw_step() is True
    assert runner.execute_sw_per_config(3) is True
    assert runner.execute_sc_step(7) is True
    assert calls == [("sw", None), ("sw_config", 3), ("sc", 7)]


def test_task_runner_server_mode_skips_local_sw_cleanup_and_verification(monkeypatch) -> None:
    from engine.task_runner import TaskRunner

    class _SWExecutor:
        def __getattr__(self, name: str):
            raise AssertionError(f"server mode should not call local SW cleanup: {name}")

    class _State:
        def get_all_configs(self) -> list[int]:
            return [1, 2]

        def get_step_status(self, _config_name: int, step_name: str) -> str:
            return "Completed" if step_name == "sw" else "Waiting"

    runner = TaskRunner.__new__(TaskRunner)
    runner.state = _State()
    runner._sw_executor = _SWExecutor()
    runner._local_worker_adapter = object()

    monkeypatch.setenv("AUTOFLUID_SERVER_MODE", "server")

    runner.shutdown_sw_processes()
    runner.do_sw_first_cleanup()
    runner.do_sw_final_cleanup()
    runner.reset_sw_cleanup()
    runner.disconnect_sw_cached()
    assert runner.verify_step_exports("C:/not-on-ocar") == 2
