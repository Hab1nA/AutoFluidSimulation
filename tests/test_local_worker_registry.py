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


def test_online_workers_returns_only_live_network_snapshots() -> None:
    from engine.local_worker_registry import LocalWorkerRegistry

    now = 100.0
    registry = LocalWorkerRegistry(timeout_seconds=30.0, clock=lambda: now)
    registry.register(
        "local-pc-01",
        {"sw": True},
        network={
            "reachable_host": "127.0.0.1",
            "ssh_port": 2222,
            "connectivity_mode": "reverse_tunnel",
        },
    )
    registry.register(
        "stale-pc-01",
        {"sw": True},
        network={
            "reachable_host": "100.64.1.20",
            "ssh_port": 22,
            "connectivity_mode": "tailscale",
        },
    )
    now = 120.0
    registry.heartbeat("local-pc-01")
    now = 140.0

    workers = registry.online_workers()

    assert [worker["worker_id"] for worker in workers] == ["local-pc-01"]
    assert workers[0]["network"] == {
        "reachable_host": "127.0.0.1",
        "ssh_port": 2222,
        "connectivity_mode": "reverse_tunnel",
    }
    workers[0]["network"]["reachable_host"] = "mutated"
    assert registry.get_worker("local-pc-01")["network"]["reachable_host"] == "127.0.0.1"


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


def test_get_task_returns_snapshot() -> None:
    from engine.local_worker_registry import LocalWorkerRegistry

    registry = LocalWorkerRegistry(timeout_seconds=90.0, clock=lambda: 100.0)

    queued = registry.enqueue_task("check_local_environment", {"probe": True})
    snapshot = registry.get_task(str(queued["task_id"]))

    assert snapshot is not None
    assert snapshot["step"] == "check_local_environment"
    snapshot["params"]["probe"] = False

    assert registry.get_task(str(queued["task_id"]))["params"]["probe"] is True


def test_has_active_step_task_tracks_pending_and_running_config_tasks() -> None:
    from engine.local_worker_registry import LocalWorkerRegistry

    registry = LocalWorkerRegistry(timeout_seconds=90.0, clock=lambda: 100.0)
    registry.register("local-pc-01", {"sw": True})

    queued = registry.enqueue_task("sw", {"config_name": 3})

    assert registry.has_active_step_task("sw", 3) is True
    assert registry.has_active_step_task("sw", 4) is False

    polled = registry.poll_task("local-pc-01")
    assert polled is not None
    assert polled["task_id"] == queued["task_id"]
    assert registry.has_active_step_task("sw", 3) is True

    registry.complete_task(str(polled["task_id"]), "local-pc-01", {"ok": True})

    assert registry.has_active_step_task("sw", 3) is False


def test_has_active_step_task_treats_batch_task_as_matching_any_config() -> None:
    from engine.local_worker_registry import LocalWorkerRegistry

    registry = LocalWorkerRegistry(timeout_seconds=90.0, clock=lambda: 100.0)
    registry.register("local-pc-01", {"sw": True})

    task = registry.enqueue_task("sw", {})

    assert registry.has_active_step_task("sw", 1) is True
    assert registry.has_active_step_task("sw", None) is True

    registry.fail_task(str(task["task_id"]), "local-pc-01", "cancelled")

    assert registry.has_active_step_task("sw", 1) is False
