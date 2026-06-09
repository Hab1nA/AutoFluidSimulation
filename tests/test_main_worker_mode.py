from __future__ import annotations


def test_main_worker_once_dispatches_local_worker(monkeypatch) -> None:
    import main as main_module

    calls: list[bool] = []

    class _Worker:
        @classmethod
        def from_env(cls):
            return cls()

        def register_once(self):
            calls.append(False)
            return {"status": "ok"}

        def heartbeat_once(self):
            calls.append(True)
            return {"status": "ok"}

    monkeypatch.setattr("sys.argv", ["main.py", "--worker-once"])
    monkeypatch.setattr("engine.local_worker.LocalWorker", _Worker)

    main_module.main()

    assert calls == [False, True]


def test_main_worker_dispatches_run_forever_without_pre_register(monkeypatch) -> None:
    import main as main_module

    calls: list[str] = []

    class _Worker:
        @classmethod
        def from_env(cls):
            return cls()

        def register_once(self):
            calls.append("register")
            return {"status": "ok"}

        def run_forever(self):
            calls.append("run_forever")

    monkeypatch.setattr("sys.argv", ["main.py", "--worker"])
    monkeypatch.setattr("engine.local_worker.LocalWorker", _Worker)

    main_module.main()

    assert calls == ["run_forever"]
