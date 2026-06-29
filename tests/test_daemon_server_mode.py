from __future__ import annotations

import os
import logging
import time


def test_daemon_init_does_not_create_directories_before_config_load(monkeypatch) -> None:
    from engine import daemon as daemon_module
    from engine.daemon import PipelineDaemon

    calls = []
    monkeypatch.setattr(daemon_module, "ensure_directories", lambda: calls.append(True))

    PipelineDaemon()

    assert calls == []


def test_server_mode_starts_control_plane_when_excel_is_missing(monkeypatch, tmp_path) -> None:
    from engine import daemon as daemon_module
    from engine.daemon import PipelineDaemon

    class _FakeIPCServer:
        def __init__(self) -> None:
            self._daemon: PipelineDaemon | None = None

        def register_default_handlers(self, daemon: PipelineDaemon) -> None:
            self._daemon = daemon

        def start(self) -> None:
            assert self._daemon is not None
            self._daemon._stop_event.set()

        def stop(self) -> None:
            pass

    released = []
    config_events = []
    monkeypatch.setenv("AUTOFLUID_SERVER_MODE", "server")
    monkeypatch.setattr(
        "engine.config.reload_config_from_toml",
        lambda: config_events.append("reload"),
    )
    monkeypatch.setattr(
        daemon_module,
        "ensure_directories",
        lambda: config_events.append("ensure"),
    )
    monkeypatch.setattr(daemon_module, "validate_config_details", lambda: [])
    monkeypatch.setattr(daemon_module, "acquire_process_lock", lambda: True)
    monkeypatch.setattr(daemon_module, "release_process_lock", lambda: released.append(True))
    monkeypatch.setattr(daemon_module, "IPCServer", _FakeIPCServer)
    monkeypatch.setattr(PipelineDaemon, "_setup_signal_handlers", lambda self: None)
    monkeypatch.setitem(daemon_module.LOCAL_PATHS, "excel", str(tmp_path / "missing.xlsx"))
    monkeypatch.setitem(daemon_module.LOCAL_PATHS, "data_dir", str(tmp_path / "data"))
    monkeypatch.setitem(daemon_module.IPC_CONFIG, "db_path", str(tmp_path / "server-mode.db"))

    daemon = PipelineDaemon()
    daemon.start()

    assert daemon.state is None
    assert daemon.runner is None
    assert daemon.scheduler is None
    assert daemon._config_load_error is not None
    assert "等待 LocalWorker 提供构型数据" in daemon._config_load_error
    assert config_events == ["reload", "ensure"]
    assert released == [True]
    assert not (tmp_path / "server-mode.db").exists()


def test_daemon_start_logs_config_issues_by_severity(monkeypatch, tmp_path, caplog) -> None:
    from engine import daemon as daemon_module
    from engine.config import ConfigValidationIssue
    from engine.daemon import PipelineDaemon

    class _FakeIPCServer:
        def __init__(self) -> None:
            self._daemon: PipelineDaemon | None = None

        def register_default_handlers(self, daemon: PipelineDaemon) -> None:
            self._daemon = daemon

        def start(self) -> None:
            assert self._daemon is not None
            self._daemon._stop_event.set()

        def stop(self) -> None:
            pass

    monkeypatch.setenv("AUTOFLUID_SERVER_MODE", "server")
    monkeypatch.setattr("engine.config.reload_config_from_toml", lambda: None)
    monkeypatch.setattr(daemon_module, "ensure_directories", lambda: None)
    monkeypatch.setattr(
        daemon_module,
        "validate_config_details",
        lambda: [
            ConfigValidationIssue("local path missing", "info"),
            ConfigValidationIssue("reachable host missing", "warning"),
        ],
    )
    monkeypatch.setattr(daemon_module, "acquire_process_lock", lambda: True)
    monkeypatch.setattr(daemon_module, "release_process_lock", lambda: None)
    monkeypatch.setattr(daemon_module, "IPCServer", _FakeIPCServer)
    monkeypatch.setattr(PipelineDaemon, "_setup_signal_handlers", lambda self: None)
    monkeypatch.setitem(daemon_module.LOCAL_PATHS, "excel", str(tmp_path / "missing.xlsx"))
    monkeypatch.setitem(daemon_module.LOCAL_PATHS, "data_dir", str(tmp_path / "data"))

    daemon = PipelineDaemon()
    with caplog.at_level(logging.INFO):
        daemon.start()

    config_records = [
        record for record in caplog.records
        if record.name == "PipelineDaemon" and record.getMessage().startswith("[CONFIG]")
    ]
    assert [(record.levelno, record.getMessage()) for record in config_records] == [
        (logging.INFO, "[CONFIG] local path missing"),
        (logging.WARNING, "[CONFIG] reachable host missing"),
    ]
    assert daemon._config_warnings == ["local path missing", "reachable host missing"]


def test_server_mode_starts_alert_watcher_after_ipc_start(monkeypatch, tmp_path) -> None:
    from engine import daemon as daemon_module
    from engine.daemon import PipelineDaemon

    class _FakeIPCServer:
        def __init__(self) -> None:
            self._daemon: PipelineDaemon | None = None

        def register_default_handlers(self, daemon: PipelineDaemon) -> None:
            self._daemon = daemon

        def start(self) -> None:
            assert self._daemon is not None

        def stop(self) -> None:
            assert self._daemon is not None
            self._daemon._stop_event.set()

    class _FakeProcess:
        pid = 12345

        def poll(self) -> None:
            return None

        def terminate(self) -> None:
            pass

        def wait(self, timeout: float | None = None) -> None:
            pass

    class _FakeTaskRunner:
        def __init__(self, _state, local_worker_adapter) -> None:
            self.local_worker_adapter = local_worker_adapter

    class _FakeScheduler:
        def __init__(self, _state, _runner) -> None:
            pass

        def stop(self) -> None:
            pass

    popen_calls = []

    def fake_popen(command, **kwargs):
        popen_calls.append((command, kwargs))
        return _FakeProcess()

    monkeypatch.setenv("AUTOFLUID_SERVER_MODE", "server")
    monkeypatch.setenv("AUTOFLUID_OPENCLAW_WEBHOOK_URL", "http://127.0.0.1/webhook")
    monkeypatch.setattr("engine.config.reload_config_from_toml", lambda: None)
    monkeypatch.setattr(daemon_module, "ensure_directories", lambda: None)
    monkeypatch.setattr(daemon_module, "validate_config_details", lambda: [])
    monkeypatch.setattr(daemon_module, "acquire_process_lock", lambda: True)
    monkeypatch.setattr(daemon_module, "release_process_lock", lambda: None)
    monkeypatch.setattr(daemon_module, "IPCServer", _FakeIPCServer)
    monkeypatch.setattr(daemon_module, "TaskRunner", _FakeTaskRunner)
    monkeypatch.setattr(daemon_module, "PipelineScheduler", _FakeScheduler)
    monkeypatch.setattr(daemon_module.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(PipelineDaemon, "_setup_signal_handlers", lambda self: None)
    monkeypatch.setitem(daemon_module.LOCAL_PATHS, "data_dir", str(tmp_path / "data"))
    monkeypatch.setitem(daemon_module.LOCAL_PATHS, "log_dir", str(tmp_path / "logs"))
    monkeypatch.setitem(daemon_module.IPC_CONFIG, "db_path", str(tmp_path / "server-mode.db"))

    daemon = PipelineDaemon()
    daemon._stop_event.set()
    daemon.start()

    alert_calls = [
        (command, kwargs)
        for command, kwargs in popen_calls
        if "tools.autofluid_cli" in command and "alerts" in command and "watch" in command
    ]
    assert len(alert_calls) == 1
    command, kwargs = alert_calls[0]
    assert command == [
        daemon_module.PipelineDaemon._daemon_python_executable(),
        "-m",
        "tools.autofluid_cli",
        "alerts",
        "watch",
    ]
    assert kwargs["cwd"] == daemon_module._PROJECT_ROOT
    assert kwargs["env"]["AUTOFLUID_IPC_HOST"] == str(daemon_module.IPC_CONFIG["host"])
    assert kwargs["env"]["AUTOFLUID_IPC_PORT"] == str(daemon_module.IPC_CONFIG["port"])
    assert daemon._alert_watcher_process is None


def test_shutdown_stops_alert_watcher_before_ipc_server() -> None:
    from engine.daemon import PipelineDaemon

    class _FakeProcess:
        pid = 12345

        def __init__(self) -> None:
            self.calls: list[str] = []

        def poll(self) -> None:
            return None

        def terminate(self) -> None:
            self.calls.append("terminate")

        def wait(self, timeout: float | None = None) -> None:
            self.calls.append(f"wait:{timeout}")

    class _FakeIPCServer:
        def __init__(self, events: list[str]) -> None:
            self._events = events

        def stop(self) -> None:
            self._events.append("ipc_stop")

    events: list[str] = []
    process = _FakeProcess()
    daemon = PipelineDaemon.__new__(PipelineDaemon)
    daemon.scheduler = None
    daemon.runner = None
    daemon.ipc_server = _FakeIPCServer(events)
    daemon._alert_watcher_process = process
    daemon._stop_event = type("_StopEvent", (), {"set": lambda self: None})()

    daemon.shutdown()

    assert process.calls == ["terminate", "wait:5.0"]
    assert events == ["ipc_stop"]
    assert daemon._alert_watcher_process is None


def test_shutdown_preserves_remote_tasks_by_default_when_scheduler_stop_fails() -> None:
    from engine.daemon import PipelineDaemon

    class _Scheduler:
        def stop(self, *, cancel_remote_tasks: bool = True) -> None:
            assert cancel_remote_tasks is False
            raise RuntimeError("stop failed before remote cleanup")

    class _RemoteExecutor:
        def __init__(self) -> None:
            self.cancel_calls = 0

        def cancel_all_tracked_remote_tasks(self) -> dict[str, int]:
            self.cancel_calls += 1
            return {"cancelled": 1, "failed": 0}

    class _Runner:
        def __init__(self, remote_executor: _RemoteExecutor) -> None:
            self.remote_executor = remote_executor

        def get_remote_executor(self) -> _RemoteExecutor:
            return self.remote_executor

    remote_executor = _RemoteExecutor()
    daemon = PipelineDaemon.__new__(PipelineDaemon)
    daemon.scheduler = _Scheduler()
    daemon.runner = _Runner(remote_executor)
    daemon.ipc_server = None
    daemon._alert_watcher_process = None
    daemon._local_worker_process = None
    daemon._stop_event = type("_StopEvent", (), {"set": lambda self: None})()

    daemon.shutdown()

    assert remote_executor.cancel_calls == 0


def test_shutdown_cancels_remote_tasks_only_for_full_stop_after_scheduler_stop_succeeds() -> None:
    from engine.daemon import PipelineDaemon

    class _Scheduler:
        def __init__(self) -> None:
            self.stop_calls = 0
            self.cancel_remote_tasks_args: list[bool] = []

        def stop(self, *, cancel_remote_tasks: bool = True) -> None:
            self.stop_calls += 1
            self.cancel_remote_tasks_args.append(cancel_remote_tasks)

    class _RemoteExecutor:
        def __init__(self) -> None:
            self.cancel_calls = 0

        def cancel_all_tracked_remote_tasks(self) -> dict[str, int]:
            self.cancel_calls += 1
            return {"cancelled": 1, "failed": 0}

    class _Runner:
        def __init__(self, remote_executor: _RemoteExecutor) -> None:
            self.remote_executor = remote_executor

        def get_remote_executor(self) -> _RemoteExecutor:
            return self.remote_executor

    scheduler = _Scheduler()
    remote_executor = _RemoteExecutor()
    daemon = PipelineDaemon.__new__(PipelineDaemon)
    daemon.scheduler = scheduler
    daemon.runner = _Runner(remote_executor)
    daemon.ipc_server = None
    daemon._alert_watcher_process = None
    daemon._local_worker_process = None
    daemon._stop_event = type("_StopEvent", (), {"set": lambda self: None})()

    daemon.shutdown(preserve_pipeline=False)

    assert scheduler.stop_calls == 1
    assert scheduler.cancel_remote_tasks_args == [True]
    assert remote_executor.cancel_calls == 1


def test_worker_restart_preserves_remote_tasks() -> None:
    from engine.daemon import PipelineDaemon

    class _Runner:
        def __init__(self) -> None:
            self.disconnect_calls = 0

        def disconnect_ssh(self) -> None:
            self.disconnect_calls += 1

    class _State:
        def __init__(self) -> None:
            self.delete_all_remote_tasks_calls = 0

        def delete_all_remote_tasks(self) -> None:
            self.delete_all_remote_tasks_calls += 1

    runner = _Runner()
    state = _State()
    daemon = PipelineDaemon.__new__(PipelineDaemon)
    daemon.runner = runner
    daemon.state = state
    daemon.local_worker_registry = type(
        "_Registry",
        (),
        {
            "clear_online_workers": lambda self: None,
            "clear_pending_tasks": lambda self: None,
        },
    )()
    daemon._last_worker_ssh_checks = {"WS-A": "ok"}
    daemon._stop_workstation_ssh_health_monitor = lambda: None
    daemon._start_workstation_ssh_health_monitor = lambda: None
    daemon.handle_worker_start = lambda params=None: (True, {"started": True}, "started")

    ok, data, message = daemon.handle_worker_restart()

    assert ok is True
    assert message == "所有 Worker 已重启"
    assert runner.disconnect_calls == 1
    assert state.delete_all_remote_tasks_calls == 0
    assert data["stop"]["remote_tasks_cleared"] is False


def test_daemon_restores_remote_tasks_from_db_when_components_created() -> None:
    from engine.daemon import PipelineDaemon

    class _RemoteExecutor:
        def __init__(self) -> None:
            self.restore_calls = 0

        def restore_remote_tasks_from_db(self) -> None:
            self.restore_calls += 1

    class _Runner:
        def __init__(self, remote_executor: _RemoteExecutor) -> None:
            self.remote_executor = remote_executor

        def get_remote_executor(self) -> _RemoteExecutor:
            return self.remote_executor

    remote_executor = _RemoteExecutor()
    daemon = PipelineDaemon.__new__(PipelineDaemon)
    daemon.runner = _Runner(remote_executor)

    daemon._restore_remote_tasks_from_db()

    assert remote_executor.restore_calls == 1


def test_child_health_restarts_exited_alert_watcher(monkeypatch) -> None:
    from engine.daemon import PipelineDaemon

    class _DeadProcess:
        def poll(self):
            return 1

    daemon = PipelineDaemon.__new__(PipelineDaemon)
    daemon._alert_watcher_process = _DeadProcess()
    daemon._local_worker_process = None
    daemon._last_child_health_check = 0.0
    daemon._child_health_check_interval_seconds = 0.0
    daemon._alert_watcher_last_start_attempt = 0.0
    daemon._local_worker_last_start_attempt = 0.0
    calls = []

    monkeypatch.setattr("engine.daemon.time.monotonic", lambda: 100.0)
    monkeypatch.setattr(daemon, "_start_alert_watcher", lambda: calls.append("alert"))

    daemon._check_child_process_health_once()

    assert calls == ["alert"]
    assert daemon._alert_watcher_process is None


def test_child_health_restarts_exited_local_worker(monkeypatch) -> None:
    from engine.daemon import PipelineDaemon

    class _DeadProcess:
        def poll(self):
            return 1

    daemon = PipelineDaemon.__new__(PipelineDaemon)
    daemon._alert_watcher_process = None
    daemon._local_worker_process = _DeadProcess()
    daemon._last_child_health_check = 0.0
    daemon._child_health_check_interval_seconds = 0.0
    daemon._alert_watcher_last_start_attempt = 0.0
    daemon._local_worker_last_start_attempt = 0.0
    calls = []

    monkeypatch.setattr("engine.daemon.time.monotonic", lambda: 100.0)
    monkeypatch.setattr(daemon, "_ensure_local_worker_autostarted", lambda: calls.append("worker"))

    daemon._check_child_process_health_once()

    assert calls == ["worker"]
    assert daemon._local_worker_process is None


def test_child_health_does_not_restart_children_after_stop_requested(monkeypatch) -> None:
    from engine.daemon import PipelineDaemon

    class _DeadProcess:
        def poll(self):
            return 1

    daemon = PipelineDaemon()
    daemon._stop_event.set()
    daemon._alert_watcher_process = _DeadProcess()
    daemon._local_worker_process = _DeadProcess()
    calls = []

    monkeypatch.setattr(daemon, "_start_alert_watcher", lambda: calls.append("alert"))
    monkeypatch.setattr(daemon, "_ensure_local_worker_autostarted", lambda: calls.append("worker"))

    daemon._check_child_process_health_once()

    assert calls == []


def test_signal_handler_only_requests_main_loop_shutdown(monkeypatch) -> None:
    from engine.daemon import PipelineDaemon

    registered = {}
    shutdown_calls = []
    daemon = PipelineDaemon.__new__(PipelineDaemon)
    daemon._running = True
    daemon._stop_event = type("_StopEvent", (), {"set": lambda self: shutdown_calls.append("set")})()
    daemon.shutdown = lambda: shutdown_calls.append("shutdown")  # type: ignore[method-assign]

    monkeypatch.setattr("engine.daemon.signal.signal", lambda sig, handler: registered.setdefault(sig, handler))

    daemon._setup_signal_handlers()
    handler = next(iter(registered.values()))
    handler(15, None)

    assert daemon._running is False
    assert shutdown_calls == ["set"]


def test_server_mode_uses_latest_existing_state_db_when_excel_is_missing(
    monkeypatch,
    tmp_path,
) -> None:
    from engine import daemon as daemon_module
    from engine.daemon import PipelineDaemon
    from engine.state_manager import StateManager

    class _FakeIPCServer:
        def __init__(self) -> None:
            self._daemon: PipelineDaemon | None = None

        def register_default_handlers(self, daemon: PipelineDaemon) -> None:
            self._daemon = daemon

        def start(self) -> None:
            assert self._daemon is not None
            self._daemon._stop_event.set()

        def stop(self) -> None:
            pass

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    empty_db = data_dir / "pipeline_state.db"
    StateManager(db_path=str(empty_db))

    active_db = data_dir / "pipeline_state_abc12345.db"
    active_state = StateManager(db_path=str(active_db))
    active_state.load_configs({0: [1.0, 2.0, 3.0, 4.0]})
    os.utime(active_db, (time.time() + 10, time.time() + 10))

    monkeypatch.setenv("AUTOFLUID_SERVER_MODE", "server")
    monkeypatch.setattr("engine.config.reload_config_from_toml", lambda: None)
    monkeypatch.setattr(daemon_module, "ensure_directories", lambda: None)
    monkeypatch.setattr(daemon_module, "validate_config_details", lambda: [])
    monkeypatch.setattr(daemon_module, "acquire_process_lock", lambda: True)
    monkeypatch.setattr(daemon_module, "release_process_lock", lambda: None)
    monkeypatch.setattr(daemon_module, "IPCServer", _FakeIPCServer)
    monkeypatch.setattr(PipelineDaemon, "_setup_signal_handlers", lambda self: None)
    monkeypatch.setitem(daemon_module.LOCAL_PATHS, "excel", str(tmp_path / "missing.xlsx"))
    monkeypatch.setitem(daemon_module.LOCAL_PATHS, "data_dir", str(data_dir))
    monkeypatch.setitem(daemon_module.IPC_CONFIG, "db_path", str(empty_db))

    daemon = PipelineDaemon()
    daemon.start()

    assert daemon.state is not None
    assert daemon.state.db_path == str(active_db)
    assert daemon.state.get_all_configs() == [0]
    assert daemon._config_load_error is None


def test_server_mode_does_not_read_local_excel_on_start(monkeypatch, tmp_path) -> None:
    from engine import daemon as daemon_module
    from engine.daemon import PipelineDaemon

    class _FakeIPCServer:
        def __init__(self) -> None:
            self._daemon: PipelineDaemon | None = None

        def register_default_handlers(self, daemon: PipelineDaemon) -> None:
            self._daemon = daemon

        def start(self) -> None:
            assert self._daemon is not None
            self._daemon._stop_event.set()

        def stop(self) -> None:
            pass

    def fail_read_model_configs(_path: str):
        raise AssertionError("server-mode daemon must not read local Excel")

    monkeypatch.setenv("AUTOFLUID_SERVER_MODE", "server")
    monkeypatch.setattr("engine.config.reload_config_from_toml", lambda: None)
    monkeypatch.setattr(daemon_module, "ensure_directories", lambda: None)
    monkeypatch.setattr(daemon_module, "validate_config_details", lambda: [])
    monkeypatch.setattr(daemon_module, "read_model_configs", fail_read_model_configs)
    monkeypatch.setattr(daemon_module, "acquire_process_lock", lambda: True)
    monkeypatch.setattr(daemon_module, "release_process_lock", lambda: None)
    monkeypatch.setattr(daemon_module, "IPCServer", _FakeIPCServer)
    monkeypatch.setattr(PipelineDaemon, "_setup_signal_handlers", lambda self: None)
    monkeypatch.setitem(daemon_module.LOCAL_PATHS, "data_dir", str(tmp_path / "data"))
    monkeypatch.setitem(daemon_module.IPC_CONFIG, "db_path", str(tmp_path / "server-mode.db"))

    daemon = PipelineDaemon()
    daemon.start()

    assert daemon.state is None
    assert daemon._config_load_error is not None
    assert "等待 LocalWorker 提供构型数据" in daemon._config_load_error
    assert not (tmp_path / "server-mode.db").exists()


def test_server_mode_rejects_pipeline_start_when_configs_are_not_loaded() -> None:
    from engine.daemon import PipelineDaemon

    class _State:
        def __init__(self) -> None:
            self.set_status_calls: list[str] = []

        def get_engine_status(self) -> str:
            return "stopped"

        def set_engine_status(self, status: str) -> None:
            self.set_status_calls.append(status)

    class _Scheduler:
        is_paused = False
        pipeline_alive = False

        def __init__(self) -> None:
            self.start_calls = 0

        def start_pipeline(self) -> None:
            self.start_calls += 1

        def set_pipeline_thread(self, _thread) -> None:
            pass

    daemon = PipelineDaemon.__new__(PipelineDaemon)
    daemon.state = _State()
    daemon.scheduler = _Scheduler()
    daemon._config_load_error = "Excel 文件未找到: missing.xlsx"

    ok, data, message = daemon.handle_start({})

    assert ok is False
    assert data is None
    assert "构型数据未就绪" in message
    assert daemon.state.set_status_calls == []
    assert daemon.scheduler.start_calls == 0


def test_server_mode_does_not_require_worker_after_local_steps_completed(monkeypatch) -> None:
    from engine.daemon import PipelineDaemon

    class _Registry:
        def has_online_worker(self) -> bool:
            return False

    class _State:
        def get_all_configs(self) -> list[int]:
            return [0, 1]

        def get_step_status(self, _config_name: int, step_name: str) -> str:
            if step_name in {"sw", "sc"}:
                return "Completed"
            return "Waiting"

    monkeypatch.setenv("AUTOFLUID_SERVER_MODE", "server")
    daemon = PipelineDaemon.__new__(PipelineDaemon)
    daemon.state = _State()
    daemon.local_worker_registry = _Registry()
    daemon.local_worker_adapter = None

    assert daemon._server_mode_requires_worker() is False


def test_check_summary_does_not_fail_when_local_worker_not_required() -> None:
    from engine.daemon import PipelineDaemon

    results = {
        "summary": {"passed": 1, "failed": 0, "warnings": 0},
        "health": {
            "local_worker_online": False,
            "local_worker_required": False,
            "server_to_local_ssh": "unknown",
            "workstation_ssh_details": {"WS-A": "ok"},
        },
    }

    PipelineDaemon._refresh_check_summary_from_health(results)

    assert results["summary"] == {"passed": 2, "failed": 0, "warnings": 0}
    assert results["overall_ok"] is True
    assert results["status"] == "passed"


def test_check_summary_treats_stale_workstation_ssh_as_warning() -> None:
    from engine.daemon import PipelineDaemon

    results = {
        "summary": {"passed": 1, "failed": 0, "warnings": 0},
        "health": {
            "local_worker_online": False,
            "local_worker_required": False,
            "server_to_local_ssh": "unknown",
            "workstation_ssh_details": {"WS-A": "stale"},
        },
    }

    PipelineDaemon._refresh_check_summary_from_health(results)

    assert results["summary"] == {"passed": 1, "failed": 0, "warnings": 1}
    assert results["overall_ok"] is True
    assert results["status"] == "warning"


def test_server_mode_worker_register_configs_unblocks_pipeline_start(monkeypatch, tmp_path) -> None:
    from engine.daemon import PipelineDaemon
    from engine.local_worker_registry import LocalWorkerRegistry

    class _Scheduler:
        is_paused = False
        pipeline_alive = False

        def __init__(self) -> None:
            self.start_calls = 0
            self.thread_was_set = False

        def start_pipeline(self) -> None:
            self.start_calls += 1

        def set_pipeline_thread(self, _thread) -> None:
            self.thread_was_set = True

    class _ImmediateThread:
        def __init__(self, target, daemon, name):
            self._target = target
            self.daemon = daemon
            self.name = name

        def start(self) -> None:
            self._target()

    worker_db = tmp_path / "worker-configs.db"
    monkeypatch.setattr(
        "engine.daemon.get_db_path_for_fingerprint",
        lambda _fingerprint: str(worker_db),
    )
    monkeypatch.setattr("engine.daemon.threading.Thread", _ImmediateThread)

    daemon = PipelineDaemon.__new__(PipelineDaemon)
    daemon.state = None
    daemon.runner = None
    daemon.scheduler = None
    daemon.local_worker_registry = LocalWorkerRegistry()
    daemon.local_worker_adapter = object()
    daemon._pipeline_ever_started = False
    daemon._config_load_error = "Excel 文件未找到: missing.xlsx"

    ok, data, message = daemon.handle_worker_register({
        "worker_id": "local-pc-01",
        "capabilities": {"sw": True},
        "configs": {"1": [1.0, "2.0", 3, 4], "2": [3, 4, 5, 6]},
    })

    assert ok is True
    assert data["worker_id"] == "local-pc-01"
    assert message == "LocalWorker 已注册"
    assert daemon._config_load_error is None
    assert daemon.state is not None
    assert daemon.state.get_all_configs() == [1, 2]
    assert daemon.state.db_path == str(worker_db)

    daemon.scheduler = _Scheduler()
    daemon._last_worker_ssh_checks = {"default": "ok"}
    ok, data, message = daemon.handle_start({})

    assert ok is True
    assert data is None
    assert message == "流水线已启动"
    assert daemon.state.get_engine_status() == "running"
    assert daemon.scheduler.start_calls == 1
    assert daemon.scheduler.thread_was_set is True


def test_local_worker_autostart_sets_reverse_tunnel_worker_metadata(monkeypatch, tmp_path) -> None:
    from engine import daemon as daemon_module
    from engine.daemon import PipelineDaemon
    from engine.local_worker_registry import LocalWorkerRegistry

    class _FakeProcess:
        pid = 12345

        def poll(self) -> None:
            return None

    popen_calls = []

    def fake_popen(command, **kwargs):
        popen_calls.append((command, kwargs))
        return _FakeProcess()

    monkeypatch.setenv("AUTOFLUID_SERVER_MODE", "server")
    monkeypatch.setattr(daemon_module.sys, "platform", "win32")
    monkeypatch.setattr(daemon_module.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(
        PipelineDaemon,
        "_local_worker_python_executable",
        staticmethod(lambda: "python.exe"),
    )
    monkeypatch.setattr(daemon_module, "_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setattr(daemon_module, "write_pid_file", lambda _path, _pid: None)
    monkeypatch.setattr(daemon_module, "worker_pid_file", lambda _kind: str(tmp_path / "worker.pid"))
    monkeypatch.setitem(daemon_module.LOCAL_PATHS, "log_dir", str(tmp_path / "logs"))
    monkeypatch.setitem(daemon_module.IPC_CONFIG, "host", "127.0.0.1")
    monkeypatch.setitem(daemon_module.IPC_CONFIG, "port", 19527)
    (tmp_path / "main.py").write_text("print('worker')", encoding="utf-8")

    daemon = PipelineDaemon.__new__(PipelineDaemon)
    daemon.local_worker_adapter = object()
    daemon.local_worker_registry = LocalWorkerRegistry(timeout_seconds=0.0)
    daemon._local_worker_process = None
    daemon._local_worker_last_start_attempt = 0.0

    daemon._ensure_local_worker_autostarted()

    assert len(popen_calls) == 1
    _command, kwargs = popen_calls[0]
    env = kwargs["env"]
    assert env["AUTOFLUID_IPC_HOST"] == "127.0.0.1"
    assert env["AUTOFLUID_IPC_PORT"] == "19527"
    assert env["AUTOFLUID_WORKER_REACHABLE_HOST"] == "127.0.0.1"
    assert env["AUTOFLUID_WORKER_SSH_PORT"] == "2223"
    assert env["AUTOFLUID_WORKER_CONNECTIVITY_MODE"] == "reverse_tunnel"


def test_worker_register_configs_are_idempotent(monkeypatch, tmp_path) -> None:
    from engine.daemon import PipelineDaemon
    from engine.local_worker_registry import LocalWorkerRegistry

    class _State:
        def __init__(self, db_path: str) -> None:
            self.db_path = db_path
            self.load_calls = 0

        def load_configs(self, _configs) -> None:
            self.load_calls += 1

        def get_all_configs(self) -> list[int]:
            return [1]

        def get_config_workstation(self, _config_name: int):
            return None

        def set_config_workstation(self, _config_name: int, _workstation_id: str) -> None:
            pass

    class _Runner:
        def __init__(self, _state, local_worker_adapter) -> None:
            self.local_worker_adapter = local_worker_adapter

    class _Scheduler:
        def __init__(self, _state, _runner) -> None:
            pass

    worker_db = tmp_path / "worker-configs.db"
    states: list[_State] = []

    def make_state(db_path: str):
        state = _State(db_path)
        states.append(state)
        return state

    monkeypatch.setattr("engine.daemon.get_db_path_for_fingerprint", lambda _fp: str(worker_db))
    monkeypatch.setattr("engine.daemon.StateManager", make_state)
    monkeypatch.setattr("engine.daemon.TaskRunner", _Runner)
    monkeypatch.setattr("engine.daemon.PipelineScheduler", _Scheduler)

    daemon = PipelineDaemon.__new__(PipelineDaemon)
    daemon.state = None
    daemon.runner = None
    daemon.scheduler = None
    daemon.local_worker_registry = LocalWorkerRegistry()
    daemon.local_worker_adapter = object()
    daemon._pipeline_ever_started = False
    daemon._config_load_error = "等待 LocalWorker 提供构型数据"
    daemon._worker_config_fingerprint = None
    params = {
        "worker_id": "local-pc-01",
        "capabilities": {"sw": True},
        "configs": {"1": [1.0, 2.0, 3.0, 4.0]},
    }

    daemon.handle_worker_register(params)
    daemon.handle_worker_register(params)

    assert len(states) == 1
    assert states[0].load_calls == 1
