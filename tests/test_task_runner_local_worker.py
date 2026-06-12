from __future__ import annotations


def test_task_runner_delegates_sw_and_sc_to_local_worker_in_server_mode(monkeypatch) -> None:
    from engine.config import ENGINE_CONFIG
    from engine.task_runner import TaskRunner

    calls: list[tuple[str, int | None, float | None]] = []

    class _Adapter:
        def execute_sw_step(self, timeout_seconds: float = 3600.0) -> bool:
            calls.append(("sw", None, timeout_seconds))
            return True

        def execute_sw_per_config(
            self,
            config_name: int,
            timeout_seconds: float = 3600.0,
        ) -> bool:
            calls.append(("sw_config", config_name, timeout_seconds))
            return True

        def execute_sc_step(
            self,
            config_name: int,
            timeout_seconds: float = 3600.0,
        ) -> bool:
            calls.append(("sc", config_name, timeout_seconds))
            return True

    runner = TaskRunner.__new__(TaskRunner)
    runner._local_worker_adapter = _Adapter()

    monkeypatch.setenv("AUTOFLUID_SERVER_MODE", "server")
    monkeypatch.setitem(ENGINE_CONFIG, "sw_macro_timeout", 123)
    monkeypatch.setitem(ENGINE_CONFIG, "sc_timeout", 45)

    assert runner.execute_sw_step() is True
    assert runner.execute_sw_per_config(3) is True
    assert runner.execute_sc_step(7) is True
    assert calls == [("sw", None, 123), ("sw_config", 3, 123), ("sc", 7, 45)]


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


def test_task_runner_reports_delegated_sw_task_in_flight_in_server_mode(monkeypatch) -> None:
    from engine.task_runner import TaskRunner

    calls: list[tuple[str, int | None]] = []

    class _Adapter:
        def has_active_task(self, step: str, config_name: int | None = None) -> bool:
            calls.append((step, config_name))
            return step == "sw" and config_name == 3

    runner = TaskRunner.__new__(TaskRunner)
    runner._local_worker_adapter = _Adapter()

    monkeypatch.setenv("AUTOFLUID_SERVER_MODE", "server")

    assert runner.is_sw_in_flight(3) is True
    assert runner.is_sw_in_flight(4) is False
    assert calls == [("sw", 3), ("sw", 4)]


def test_task_runner_reports_no_delegated_sw_task_in_local_mode(monkeypatch) -> None:
    from engine.task_runner import TaskRunner

    class _Adapter:
        def has_active_task(self, _step: str, _config_name: int | None = None) -> bool:
            raise AssertionError("local mode should not inspect LocalWorker tasks")

    runner = TaskRunner.__new__(TaskRunner)
    runner._local_worker_adapter = _Adapter()

    monkeypatch.delenv("AUTOFLUID_SERVER_MODE", raising=False)

    assert runner.is_sw_in_flight(3) is False
