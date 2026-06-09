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
