from __future__ import annotations


def test_local_worker_builds_register_and_heartbeat_requests() -> None:
    from engine.local_worker import LocalWorker, LocalWorkerConfig
    from ipc.protocol import CMD_WORKER_HEARTBEAT, CMD_WORKER_REGISTER

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
