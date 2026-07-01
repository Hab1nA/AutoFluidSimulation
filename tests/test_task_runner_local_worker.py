from __future__ import annotations


def test_task_runner_delegates_sw_and_sc_to_local_worker_in_server_mode(monkeypatch) -> None:
    from engine.config import ENGINE_CONFIG
    from engine.task_runner import TaskRunner

    calls: list[tuple[str, int | None, float | None]] = []

    class _Adapter:
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

    assert runner.execute_sw_per_config(3) is True
    assert runner.execute_sc_step(7) is True
    assert calls == [("sw_config", 3, 123), ("sc", 7, 45)]


def test_task_runner_captures_delegated_sw_failure_reason(monkeypatch) -> None:
    from engine.config import ENGINE_CONFIG
    from engine.task_runner import TaskRunner

    class _Adapter:
        last_error = "SolidWorks connection failed"

        def execute_sw_per_config(
            self,
            config_name: int,
            timeout_seconds: float = 3600.0,
        ) -> bool:
            assert config_name == 3
            assert timeout_seconds == 123
            return False

    runner = TaskRunner.__new__(TaskRunner)
    runner._local_worker_adapter = _Adapter()

    monkeypatch.setenv("AUTOFLUID_SERVER_MODE", "server")
    monkeypatch.setitem(ENGINE_CONFIG, "sw_macro_timeout", 123)

    assert runner.execute_sw_per_config(3) is False
    assert runner.last_sw_error == "SolidWorks connection failed"


def test_task_runner_captures_local_sw_failure_reason(monkeypatch) -> None:
    from engine.task_runner import TaskRunner

    class _SWExecutor:
        last_error = "SolidWorks SaveAs returned false"

        def export_sw_per_config(self, config_name: int) -> bool:
            assert config_name == 3
            return False

    runner = TaskRunner.__new__(TaskRunner)
    runner._local_worker_adapter = None
    runner._sw_executor = _SWExecutor()

    monkeypatch.delenv("AUTOFLUID_SERVER_MODE", raising=False)

    assert runner.execute_sw_per_config(3) is False
    assert runner.last_sw_error == "SolidWorks SaveAs returned false"


def test_task_runner_server_mode_delegates_sw_cleanup_and_verification(monkeypatch) -> None:
    from engine.task_runner import TaskRunner

    class _SWExecutor:
        def __getattr__(self, name: str):
            raise AssertionError(f"server mode should not call local SW cleanup: {name}")

    class _Adapter:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []

        def cleanup_stage(self, step_name: str, phase: str, timeout_seconds: float = 300.0) -> bool:
            self.calls.append((step_name, phase))
            return True

    class _State:
        def get_all_configs(self) -> list[int]:
            return [1, 2]

        def get_step_status(self, _config_name: int, step_name: str) -> str:
            return "Completed" if step_name == "sw" else "Waiting"

    runner = TaskRunner.__new__(TaskRunner)
    runner.state = _State()
    runner._sw_executor = _SWExecutor()
    runner._local_worker_adapter = _Adapter()

    monkeypatch.setenv("AUTOFLUID_SERVER_MODE", "server")

    runner.shutdown_sw_processes()
    runner.do_sw_first_cleanup()
    runner.do_sw_final_cleanup()
    runner.reset_sw_cleanup()
    runner.disconnect_sw_cached()
    assert runner.verify_step_exports("C:/not-on-ocar") == 2
    assert runner._local_worker_adapter.calls == [
        ("sw", "shutdown"),
        ("sw", "first"),
        ("sw", "final"),
        ("sw", "reset"),
        ("sw", "disconnect"),
    ]


def test_task_runner_server_mode_uses_short_local_worker_shutdown_timeout(monkeypatch) -> None:
    from engine.task_runner import TaskRunner

    class _Adapter:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str, float]] = []

        def cleanup_stage(
            self,
            step_name: str,
            phase: str,
            timeout_seconds: float = 300.0,
        ) -> bool:
            self.calls.append((step_name, phase, timeout_seconds))
            return False

    runner = TaskRunner.__new__(TaskRunner)
    runner._local_worker_adapter = _Adapter()

    monkeypatch.setenv("AUTOFLUID_SERVER_MODE", "server")

    runner.shutdown_sw_processes()
    runner.shutdown_sc_pool()

    assert runner._local_worker_adapter.calls == [
        ("sw", "shutdown", 5.0),
        ("sc", "shutdown", 5.0),
    ]


def test_task_runner_server_mode_delegates_sc_final_cleanup(monkeypatch) -> None:
    from engine.task_runner import TaskRunner

    class _SCPool:
        def do_final_cleanup(self) -> None:
            raise AssertionError("server mode should not clean daemon-local SC pool")

    class _Adapter:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []

        def cleanup_stage(self, step_name: str, phase: str, timeout_seconds: float = 300.0) -> bool:
            self.calls.append((step_name, phase))
            return True

    runner = TaskRunner.__new__(TaskRunner)
    runner._sc_pool = _SCPool()
    runner._local_worker_adapter = _Adapter()

    monkeypatch.setenv("AUTOFLUID_SERVER_MODE", "server")

    runner.shutdown_sc_pool()
    runner.do_sc_final_cleanup()
    runner.reset_sc_pool()

    assert runner._local_worker_adapter.calls == [
        ("sc", "shutdown"),
        ("sc", "final"),
        ("sc", "reset"),
    ]


def test_task_runner_server_mode_clean_all_continues_without_local_worker(monkeypatch) -> None:
    from engine.task_runner import TaskRunner

    class _Cleaner:
        def __init__(self) -> None:
            self.calls: list[tuple[str, int | str | None]] = []

        def clean_step_files(self, step_name: str, config_name: int | str | None = None) -> None:
            self.calls.append((step_name, config_name))

    class _Adapter:
        last_error = "没有在线 LocalWorker"

        def __init__(self) -> None:
            self.calls: list[tuple[str, int | str | None]] = []

        def clean_local_files(self, step_name: str, config_name: int | str | None = None) -> bool:
            self.calls.append((step_name, config_name))
            return False

    monkeypatch.setenv("AUTOFLUID_SERVER_MODE", "server")
    runner = TaskRunner.__new__(TaskRunner)
    runner._cleaner = _Cleaner()
    runner._local_worker_adapter = _Adapter()

    runner.clean_step_files("all", None)

    assert runner._local_worker_adapter.calls == [("all", None)]
    assert runner._cleaner.calls == [("all", None)]


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


def test_task_runner_initializes_last_sc_error(tmp_path) -> None:
    from engine.state_manager import StateManager
    from engine.task_runner import TaskRunner

    runner = TaskRunner(StateManager(str(tmp_path / "state.db")))

    assert runner.last_sc_error == ""


def test_task_runner_captures_delegated_sc_failure_reason(monkeypatch) -> None:
    from engine.config import ENGINE_CONFIG
    from engine.task_runner import TaskRunner

    class _Adapter:
        last_error = "SpaceClaim ready timeout"

        def execute_sc_step(
            self,
            config_name: int,
            timeout_seconds: float = 3600.0,
        ) -> bool:
            assert config_name == 7
            assert timeout_seconds == 45
            return False

    runner = TaskRunner.__new__(TaskRunner)
    runner._local_worker_adapter = _Adapter()
    monkeypatch.setenv("AUTOFLUID_SERVER_MODE", "server")
    monkeypatch.setitem(ENGINE_CONFIG, "sc_timeout", 45)

    assert runner.execute_sc_step(7) is False
    assert runner.last_sc_error == "SpaceClaim ready timeout"


def test_task_runner_captures_sc_pool_failure_reason(monkeypatch, tmp_path) -> None:
    from engine.config import LOCAL_PATHS
    import engine.task_runner as task_runner_module
    from engine.task_runner import TaskRunner

    step_dir = tmp_path / "step"
    scdoc_dir = tmp_path / "scdoc"
    sc_exe = tmp_path / "SpaceClaim.exe"
    sc_script = tmp_path / "script.py"
    step_dir.mkdir()
    scdoc_dir.mkdir()
    sc_exe.write_text("", encoding="utf-8")
    sc_script.write_text("", encoding="utf-8")
    (step_dir / "model_gen4.SLDPRT_7.step").write_text("step", encoding="utf-8")
    monkeypatch.setitem(LOCAL_PATHS, "step_dir", str(step_dir))
    monkeypatch.setitem(LOCAL_PATHS, "scdoc_dir", str(scdoc_dir))
    monkeypatch.setitem(LOCAL_PATHS, "sc_exe", str(sc_exe))
    monkeypatch.setitem(LOCAL_PATHS, "sc_script", str(sc_script))
    monkeypatch.setitem(task_runner_module.LOCAL_PATHS, "step_dir", str(step_dir))
    monkeypatch.setitem(task_runner_module.LOCAL_PATHS, "scdoc_dir", str(scdoc_dir))
    monkeypatch.setitem(task_runner_module.LOCAL_PATHS, "sc_exe", str(sc_exe))
    monkeypatch.setitem(task_runner_module.LOCAL_PATHS, "sc_script", str(sc_script))
    monkeypatch.delenv("AUTOFLUID_SERVER_MODE", raising=False)

    class _Pool:
        last_error = "Bridge exited early"

        def run_config(self, *_args, **_kwargs) -> bool:
            return False

    class _State:
        def set_step_status(self, *_args, **_kwargs) -> None:
            raise AssertionError("valid inputs should reach SC pool before state error updates")

    runner = TaskRunner.__new__(TaskRunner)
    runner.state = _State()
    runner._local_worker_adapter = object()
    runner._sc_pool = _Pool()
    runner._paused_event = None
    runner._stopped_event = None
    runner._pipeline_control = None

    assert runner.execute_sc_step(7) is False
    assert runner.last_sc_error == "Bridge exited early"
