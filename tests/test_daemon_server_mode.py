from __future__ import annotations


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
    monkeypatch.setattr(daemon_module, "validate_config", lambda: [])
    monkeypatch.setattr(daemon_module, "acquire_process_lock", lambda: True)
    monkeypatch.setattr(daemon_module, "release_process_lock", lambda: released.append(True))
    monkeypatch.setattr(daemon_module, "IPCServer", _FakeIPCServer)
    monkeypatch.setattr(PipelineDaemon, "_setup_signal_handlers", lambda self: None)
    monkeypatch.setitem(daemon_module.LOCAL_PATHS, "excel", str(tmp_path / "missing.xlsx"))
    monkeypatch.setitem(daemon_module.IPC_CONFIG, "db_path", str(tmp_path / "server-mode.db"))

    daemon = PipelineDaemon()
    daemon.start()

    assert daemon.state is not None
    assert daemon.runner is not None
    assert daemon.scheduler is not None
    assert daemon._config_load_error is not None
    assert "Excel 文件未找到" in daemon._config_load_error
    assert config_events == ["reload", "ensure"]
    assert released == [True]


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
    ok, data, message = daemon.handle_start({})

    assert ok is True
    assert data is None
    assert message == "流水线已启动"
    assert daemon.state.get_engine_status() == "running"
    assert daemon.scheduler.start_calls == 1
    assert daemon.scheduler.thread_was_set is True
