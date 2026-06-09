from __future__ import annotations


def test_register_worker_marks_worker_online() -> None:
    from engine.local_worker_registry import LocalWorkerRegistry

    registry = LocalWorkerRegistry(timeout_seconds=90.0, clock=lambda: 100.0)

    registry.register("local-pc-01", {"sw": True, "sc_slots": 3})

    assert registry.has_online_worker() is True
    worker = registry.get_worker("local-pc-01")
    assert worker is not None
    assert worker["worker_id"] == "local-pc-01"
    assert worker["capabilities"] == {"sw": True, "sc_slots": 3}
    assert worker["online"] is True
    assert worker["network"] == {}


def test_worker_expires_without_heartbeat() -> None:
    from engine.local_worker_registry import LocalWorkerRegistry

    now = 100.0
    registry = LocalWorkerRegistry(timeout_seconds=30.0, clock=lambda: now)
    registry.register("local-pc-01", {})

    now = 131.0

    assert registry.has_online_worker() is False
    worker = registry.get_worker("local-pc-01")
    assert worker is not None
    assert worker["online"] is False


def test_heartbeat_refreshes_worker_deadline() -> None:
    from engine.local_worker_registry import LocalWorkerRegistry

    now = 100.0
    registry = LocalWorkerRegistry(timeout_seconds=30.0, clock=lambda: now)
    registry.register("local-pc-01", {})
    now = 125.0
    registry.heartbeat("local-pc-01")
    now = 150.0

    assert registry.has_online_worker() is True


def test_register_worker_preserves_ocar_reachable_network_metadata() -> None:
    from engine.local_worker_registry import LocalWorkerRegistry

    registry = LocalWorkerRegistry(timeout_seconds=90.0, clock=lambda: 100.0)

    worker = registry.register(
        "local-pc-01",
        {"sw": True},
        network={
            "public_ip": "203.0.113.10",
            "candidate_hosts": ["172.17.135.240", "100.64.1.20"],
            "reachable_host": "100.64.1.20",
            "ssh_port": 22,
            "connectivity_mode": "tailscale",
        },
        remote_addr="198.51.100.5",
    )

    assert worker["network"] == {
        "public_ip": "203.0.113.10",
        "candidate_hosts": ["172.17.135.240", "100.64.1.20"],
        "reachable_host": "100.64.1.20",
        "ssh_port": 22,
        "connectivity_mode": "tailscale",
        "last_seen_remote_addr": "198.51.100.5",
    }


def test_worker_task_queue_roundtrip() -> None:
    from engine.local_worker_registry import LocalWorkerRegistry

    registry = LocalWorkerRegistry(timeout_seconds=90.0, clock=lambda: 100.0)
    registry.register("local-pc-01", {"sc": True})

    queued = registry.enqueue_task("sc", {"config_name": 3})
    polled = registry.poll_task("local-pc-01")

    assert polled is not None
    assert polled["task_id"] == queued["task_id"]
    assert polled["status"] == "running"
    assert polled["worker_id"] == "local-pc-01"

    completed = registry.complete_task(str(polled["task_id"]), "local-pc-01", {"ok": True})

    assert completed["status"] == "completed"
    assert completed["result"] == {"ok": True}
