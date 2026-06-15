from __future__ import annotations


def test_main_worker_once_dispatches_local_worker(monkeypatch) -> None:
    import main as main_module

    calls: list[str] = []

    class _Worker:
        @classmethod
        def from_env(cls):
            calls.append("from_env")
            return cls()

        def register_once(self):
            calls.append("register")
            return {"status": "ok"}

        def heartbeat_once(self):
            calls.append("heartbeat")
            return {"status": "ok"}

    monkeypatch.setattr("sys.argv", ["main.py", "--worker-once"])
    monkeypatch.setattr("engine.config.reload_config_from_toml", lambda: calls.append("reload"))
    monkeypatch.setattr("engine.config.ensure_directories", lambda: calls.append("ensure_dirs"))
    monkeypatch.setattr("engine.local_worker.LocalWorker", _Worker)

    main_module.main()

    assert calls == ["reload", "ensure_dirs", "from_env", "register", "heartbeat"]


def test_main_worker_dispatches_run_forever_without_pre_register(monkeypatch) -> None:
    import main as main_module

    calls: list[str] = []

    class _Worker:
        @classmethod
        def from_env(cls):
            calls.append("from_env")
            return cls()

        def register_once(self):
            calls.append("register")
            return {"status": "ok"}

        def run_forever(self):
            calls.append("run_forever")

    monkeypatch.setattr("sys.argv", ["main.py", "--worker"])
    monkeypatch.setattr("engine.config.reload_config_from_toml", lambda: calls.append("reload"))
    monkeypatch.setattr("engine.config.ensure_directories", lambda: calls.append("ensure_dirs"))
    monkeypatch.setattr("engine.local_worker.LocalWorker", _Worker)

    main_module.main()

    assert calls == ["reload", "ensure_dirs", "from_env", "run_forever"]


def test_main_stop_cleans_worker_pid_files(monkeypatch) -> None:
    import main as main_module

    calls: list[str] = []

    monkeypatch.setattr("sys.argv", ["main.py", "--stop"])
    monkeypatch.setattr(main_module, "run_taskkill", lambda _pid: True)
    monkeypatch.setattr(main_module.subprocess, "run", lambda *args, **kwargs: type("R", (), {"stdout": ""})())
    monkeypatch.setattr(main_module, "_stop_daemon_subprocess", lambda: calls.append("stop_daemon"))
    monkeypatch.setattr(
        main_module,
        "cleanup_worker_processes_from_pid_files",
        lambda: calls.append("pid_cleanup") or {},
    )

    main_module.main()

    assert calls == ["pid_cleanup", "stop_daemon"]
