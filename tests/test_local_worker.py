from __future__ import annotations

import base64


def test_local_worker_builds_register_and_heartbeat_requests(monkeypatch) -> None:
    import engine.local_worker as local_worker_module
    from engine.local_worker import LocalWorker, LocalWorkerConfig
    from ipc.protocol import (
        CMD_WORKER_HEARTBEAT,
        CMD_WORKER_POLL,
        CMD_WORKER_REGISTER,
        CMD_WORKER_STEP_COMPLETE,
        CMD_WORKER_STEP_ERROR,
    )

    monkeypatch.setattr(
        local_worker_module,
        "read_model_configs",
        lambda _path: (_ for _ in ()).throw(FileNotFoundError("missing")),
    )
    worker = LocalWorker(
        LocalWorkerConfig(
            worker_id="local-pc-01",
            server_host="ocar.example.test",
            server_port=9527,
            auth_token="secret",
            capabilities={"sw": True, "sc_slots": 3},
            network={
                "candidate_hosts": ["172.17.135.240", "100.64.1.20"],
                "reachable_host": "100.64.1.20",
                "connectivity_mode": "tailscale",
                "ssh_port": 22,
            },
        )
    )

    register = worker.build_register_request()
    heartbeat = worker.build_heartbeat_request()

    assert register["command"] == CMD_WORKER_REGISTER
    assert register["auth_token"] == "secret"
    assert register["params"] == {
        "worker_id": "local-pc-01",
        "capabilities": {"sw": True, "sc_slots": 3},
        "network": {
            "candidate_hosts": ["172.17.135.240", "100.64.1.20"],
            "reachable_host": "100.64.1.20",
            "connectivity_mode": "tailscale",
            "ssh_port": 22,
        },
    }
    assert heartbeat["command"] == CMD_WORKER_HEARTBEAT
    assert heartbeat["auth_token"] == "secret"
    assert heartbeat["params"] == {"worker_id": "local-pc-01"}

    poll = worker.build_poll_request()
    assert poll["command"] == CMD_WORKER_POLL
    assert poll["params"] == {"worker_id": "local-pc-01"}

    complete = worker.build_step_complete_request("task-1", {"ok": True})
    assert complete["command"] == CMD_WORKER_STEP_COMPLETE
    assert complete["params"] == {
        "worker_id": "local-pc-01",
        "task_id": "task-1",
        "result": {"ok": True},
    }

    error = worker.build_step_error_request("task-2", "failed")
    assert error["command"] == CMD_WORKER_STEP_ERROR
    assert error["params"] == {
        "worker_id": "local-pc-01",
        "task_id": "task-2",
        "error": "failed",
    }


def test_local_worker_from_env_uses_reachable_host_metadata(monkeypatch) -> None:
    from engine.local_worker import LocalWorker

    monkeypatch.setenv("AUTOFLUID_WORKER_ID", "local-pc-01")
    monkeypatch.setenv("AUTOFLUID_SERVER_HOST", "ocar.example.test")
    monkeypatch.setenv("AUTOFLUID_IPC_PORT", "9527")
    monkeypatch.setenv("AUTOFLUID_IPC_AUTH_TOKEN", "secret")
    monkeypatch.setenv("AUTOFLUID_WORKER_CANDIDATE_HOSTS", "172.17.135.240,100.64.1.20")
    monkeypatch.setenv("AUTOFLUID_WORKER_REACHABLE_HOST", "100.64.1.20")
    monkeypatch.setenv("AUTOFLUID_WORKER_CONNECTIVITY_MODE", "tailscale")
    monkeypatch.setenv("AUTOFLUID_WORKER_SSH_PORT", "22")
    monkeypatch.setenv("AUTOFLUID_DISCOVER_PUBLIC_IP", "0")
    monkeypatch.setattr("engine.local_worker.detect_candidate_hosts", lambda: [])

    worker = LocalWorker.from_env()

    assert worker.config.worker_id == "local-pc-01"
    assert worker.config.server_host == "ocar.example.test"
    assert worker.config.auth_token == "secret"
    assert worker.config.network == {
        "candidate_hosts": ["172.17.135.240", "100.64.1.20"],
        "reachable_host": "100.64.1.20",
        "connectivity_mode": "tailscale",
        "ssh_port": 22,
    }


def test_local_worker_from_env_reloads_toml_config(monkeypatch) -> None:
    from engine.local_worker import LocalWorker

    calls: list[str] = []

    monkeypatch.setattr(
        "engine.local_worker.reload_config_from_toml",
        lambda: calls.append("reload"),
        raising=False,
    )
    monkeypatch.setenv("AUTOFLUID_DISCOVER_PUBLIC_IP", "0")
    monkeypatch.setattr("engine.local_worker.detect_candidate_hosts", lambda: [])

    LocalWorker.from_env()

    assert calls == ["reload"]


def test_local_worker_register_includes_local_excel_configs(monkeypatch) -> None:
    import engine.local_worker as local_worker_module
    from engine.local_worker import LocalWorker, LocalWorkerConfig

    monkeypatch.setattr(
        local_worker_module,
        "read_model_configs",
        lambda _path: {1: [1.0, 2.0, 3.0, 4.0], 2: [3.5, 4.5, 5.5, 6.5]},
    )
    worker = LocalWorker(LocalWorkerConfig("local-pc-01", "ocar", 9527))

    request = worker.build_register_request()

    assert request["params"]["configs"] == {
        "1": [1.0, 2.0, 3.0, 4.0],
        "2": [3.5, 4.5, 5.5, 6.5],
    }


def test_local_worker_executes_polled_task_with_injected_handler() -> None:
    from engine.local_worker import LocalWorker, LocalWorkerConfig

    calls: list[dict[str, object]] = []
    worker = LocalWorker(
        LocalWorkerConfig(
            worker_id="local-pc-01",
            server_host="ocar.example.test",
            server_port=9527,
        ),
        task_handlers={
            "sc": lambda params: calls.append(params) or {"ok": True},
        },
    )

    response = worker.handle_polled_task({
        "task_id": "task-1",
        "step": "sc",
        "params": {"config_name": 7},
    })

    assert calls == [{"config_name": 7}]
    assert response["command"] == "worker_step_complete"
    assert response["params"]["task_id"] == "task-1"
    assert response["params"]["result"] == {"ok": True}


def test_local_worker_run_once_polls_and_reports_task_completion() -> None:
    from engine.local_worker import LocalWorker, LocalWorkerConfig

    sent_commands: list[str] = []
    worker = LocalWorker(
        LocalWorkerConfig(
            worker_id="local-pc-01",
            server_host="ocar.example.test",
            server_port=9527,
        ),
        task_handlers={"sw": lambda _params: {"ok": True}},
    )

    def fake_send(request):
        sent_commands.append(str(request["command"]))
        if request["command"] == "worker_poll":
            return {
                "status": "ok",
                "data": {"task_id": "task-1", "step": "sw", "params": {}},
            }
        return {"status": "ok", "data": {}}

    worker._send_request = fake_send

    assert worker.run_once(now=100.0) == "completed"
    assert sent_commands == ["worker_poll", "worker_step_complete"]


def test_local_worker_run_once_heartbeats_only_when_idle_deadline_reached() -> None:
    from engine.local_worker import LocalWorker, LocalWorkerConfig

    sent_commands: list[str] = []
    worker = LocalWorker(
        LocalWorkerConfig(
            worker_id="local-pc-01",
            server_host="ocar.example.test",
            server_port=9527,
            heartbeat_interval=30.0,
        )
    )

    def fake_send(request):
        sent_commands.append(str(request["command"]))
        return {"status": "ok", "data": None}

    worker._send_request = fake_send

    assert worker.run_once(now=100.0) == "idle"
    assert worker.run_once(now=129.0) == "idle"
    assert worker.run_once(now=130.0) == "heartbeat"
    assert sent_commands == [
        "worker_poll",
        "worker_poll",
        "worker_poll",
        "worker_heartbeat",
    ]


def test_local_worker_run_forever_retries_initial_register(monkeypatch) -> None:
    from engine.local_worker import LocalWorker, LocalWorkerConfig

    calls: list[str] = []
    sleeps: list[float] = []
    worker = LocalWorker(LocalWorkerConfig("local-pc-01", "127.0.0.1", 19527))

    def fake_register() -> dict[str, object]:
        calls.append("register")
        if len(calls) == 1:
            raise RuntimeError("daemon is not ready")
        return {"status": "ok"}

    def fake_run_once() -> str:
        calls.append("run_once")
        raise KeyboardInterrupt

    monkeypatch.setattr(worker, "register_once", fake_register)
    monkeypatch.setattr(worker, "run_once", fake_run_once)
    monkeypatch.setattr("engine.local_worker.time.sleep", lambda seconds: sleeps.append(seconds))

    try:
        worker.run_forever()
    except KeyboardInterrupt:
        pass

    assert calls == ["register", "register", "run_once"]
    assert sleeps == [2.0, worker.config.poll_interval]


def test_local_worker_run_forever_recovers_after_runtime_ipc_error(monkeypatch) -> None:
    from engine.local_worker import LocalWorker, LocalWorkerConfig

    calls: list[str] = []
    sleeps: list[float] = []
    worker = LocalWorker(LocalWorkerConfig("local-pc-01", "127.0.0.1", 19527))

    def fake_register() -> dict[str, object]:
        calls.append("register")
        return {"status": "ok"}

    def fake_run_once() -> str:
        calls.append("run_once")
        if calls.count("run_once") == 1:
            raise RuntimeError("daemon connection reset")
        raise KeyboardInterrupt

    monkeypatch.setattr(worker, "register_once", fake_register)
    monkeypatch.setattr(worker, "run_once", fake_run_once)
    monkeypatch.setattr("engine.local_worker.time.sleep", lambda seconds: sleeps.append(seconds))

    try:
        worker.run_forever()
    except KeyboardInterrupt:
        pass

    assert calls == ["register", "run_once", "register", "run_once"]
    assert sleeps == [worker.config.poll_interval, worker.config.poll_interval]


def test_local_worker_send_request_reports_reset_as_runtime_error(monkeypatch) -> None:
    import engine.local_worker as local_worker_module
    from engine.local_worker import LocalWorker, LocalWorkerConfig

    class _Socket:
        def __enter__(self):
            return self

        def __exit__(self, _exc_type, _exc, _tb):
            return False

        def sendall(self, _payload: bytes) -> None:
            pass

        def recv(self, _size: int) -> bytes:
            raise ConnectionResetError(10054, "远程主机强迫关闭了一个现有的连接。")

    monkeypatch.setattr(
        local_worker_module.socket,
        "create_connection",
        lambda _endpoint, timeout: _Socket(),
    )
    worker = LocalWorker(LocalWorkerConfig("local-pc-01", "127.0.0.1", 19527))

    try:
        worker.register_once()
    except RuntimeError as exc:
        message = str(exc)
    else:
        raise AssertionError("register_once should fail when the IPC connection is reset")

    assert "LocalWorker 无法连接 daemon IPC" in message
    assert "127.0.0.1:19527" in message


def test_local_worker_default_handlers_delegate_to_local_task_runner(monkeypatch) -> None:
    import engine.local_worker as local_worker_module
    from engine.local_worker import LocalWorker, LocalWorkerConfig

    calls: list[tuple[str, int | None]] = []

    class _Runner:
        def execute_sw_step(self) -> bool:
            calls.append(("sw", None))
            return True

        def execute_sw_per_config(self, config_name: int) -> bool:
            calls.append(("sw", config_name))
            return True

        def execute_sc_step(self, config_name: int) -> bool:
            calls.append(("sc", config_name))
            return True

    class _State:
        def load_configs(self, configs):
            self.configs = configs

    monkeypatch.setattr(local_worker_module, "read_model_configs", lambda _path: {1: [1.0]})
    monkeypatch.setattr(local_worker_module, "StateManager", lambda: _State())
    monkeypatch.setattr(local_worker_module, "TaskRunner", lambda _state: _Runner())
    monkeypatch.setattr(
        LocalWorker,
        "_build_scdoc_payload",
        lambda _self, config_name: {"config_name": config_name},
    )

    worker = LocalWorker(LocalWorkerConfig("local-pc-01", "ocar", 9527))

    assert worker._execute_task("sw", {"config_name": 3}) == {"ok": True}
    assert worker._execute_task("sc", {"config_name": 7}) == {
        "ok": True,
        "scdoc_file": {"config_name": 7},
    }
    assert calls == [("sw", 3), ("sc", 7)]


def test_local_worker_default_handlers_support_check_and_local_clean(monkeypatch) -> None:
    import engine.local_worker as local_worker_module
    from engine.local_worker import LocalWorker, LocalWorkerConfig

    calls: list[tuple[str, object]] = []

    class _Runner:
        def run_local_system_check(self) -> dict[str, object]:
            calls.append(("check", None))
            return {"local_checks": {"Excel参数表": {"path": "model.xlsx", "exists": True}}}

        def clean_local_step_files(self, step_name: str, config_name=None) -> None:
            calls.append((step_name, config_name))

    class _State:
        def load_configs(self, configs):
            self.configs = configs

    monkeypatch.setattr(local_worker_module, "read_model_configs", lambda _path: {1: [1.0]})
    monkeypatch.setattr(local_worker_module, "StateManager", lambda: _State())
    monkeypatch.setattr(local_worker_module, "TaskRunner", lambda _state: _Runner())

    worker = LocalWorker(LocalWorkerConfig("local-pc-01", "ocar", 9527))

    assert worker._execute_task("check_local_environment", {}) == {
        "ok": True,
        "local_checks": {"Excel参数表": {"path": "model.xlsx", "exists": True}},
    }
    assert worker._execute_task("clean_local_files", {"step_name": "sw", "config_name": 3}) == {
        "ok": True,
    }
    assert calls == [("check", None), ("sw", 3)]


def test_local_worker_sc_task_includes_scdoc_payload(tmp_path, monkeypatch) -> None:
    import engine.local_worker as local_worker_module
    from engine.config import LOCAL_PATHS
    from engine.local_worker import LocalWorker, LocalWorkerConfig

    scdoc_dir = tmp_path / "scdoc"
    scdoc_dir.mkdir()
    scdoc_file = scdoc_dir / "model_gen4_7.scdoc"
    scdoc_file.write_bytes(b"scdoc payload")

    class _Runner:
        def execute_sc_step(self, config_name: int) -> bool:
            assert config_name == 7
            return True

    class _State:
        def load_configs(self, _configs):
            pass

    monkeypatch.setitem(LOCAL_PATHS, "scdoc_dir", str(scdoc_dir))
    monkeypatch.setattr(local_worker_module, "read_model_configs", lambda _path: {7: [1.0]})
    monkeypatch.setattr(local_worker_module, "StateManager", lambda: _State())
    monkeypatch.setattr(local_worker_module, "TaskRunner", lambda _state: _Runner())

    worker = LocalWorker(LocalWorkerConfig("local-pc-01", "ocar", 9527))

    result = worker._execute_task("sc", {"config_name": 7})

    assert result["ok"] is True
    payload = result["scdoc_file"]
    assert payload["config_name"] == 7
    assert payload["filename"] == "model_gen4_7.scdoc"
    assert payload["size"] == len(b"scdoc payload")
    assert base64.b64decode(payload["content_b64"]) == b"scdoc payload"
