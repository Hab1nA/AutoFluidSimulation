from __future__ import annotations


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

    worker = LocalWorker(LocalWorkerConfig("local-pc-01", "ocar", 9527))

    assert worker._execute_task("sw", {"config_name": 3}) == {"ok": True}
    assert worker._execute_task("sc", {"config_name": 7}) == {"ok": True}
    assert calls == [("sw", 3), ("sc", 7)]
