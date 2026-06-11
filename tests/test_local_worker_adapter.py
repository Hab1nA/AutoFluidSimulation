from __future__ import annotations

import threading
import time


def test_local_worker_adapter_waits_for_worker_completion() -> None:
    from engine.local_worker_adapter import LocalWorkerAdapter
    from engine.local_worker_registry import LocalWorkerRegistry

    registry = LocalWorkerRegistry(timeout_seconds=90.0, clock=lambda: 100.0)
    registry.register("local-pc-01", {"sw": True})
    adapter = LocalWorkerAdapter(registry, result_poll_interval=0.01)

    result_holder: dict[str, bool] = {}

    def wait_for_result() -> None:
        result_holder["ok"] = adapter.execute_sw_step(timeout_seconds=2.0)

    thread = threading.Thread(target=wait_for_result)
    thread.start()

    task = registry.poll_task("local-pc-01")
    assert task is not None
    assert task["step"] == "sw"
    assert task["params"] == {}

    registry.complete_task(str(task["task_id"]), "local-pc-01", {"ok": True})
    thread.join(timeout=2.0)

    assert result_holder == {"ok": True}


def test_local_worker_adapter_returns_false_on_worker_error() -> None:
    from engine.local_worker_adapter import LocalWorkerAdapter
    from engine.local_worker_registry import LocalWorkerRegistry

    registry = LocalWorkerRegistry(timeout_seconds=90.0, clock=lambda: 100.0)
    registry.register("local-pc-01", {"sc": True})
    adapter = LocalWorkerAdapter(registry, result_poll_interval=0.01)

    result_holder: dict[str, bool] = {}

    def wait_for_result() -> None:
        result_holder["ok"] = adapter.execute_sc_step(7, timeout_seconds=2.0)

    thread = threading.Thread(target=wait_for_result)
    thread.start()

    task = registry.poll_task("local-pc-01")
    assert task is not None
    assert task["step"] == "sc"
    assert task["params"] == {"config_name": 7}

    registry.fail_task(str(task["task_id"]), "local-pc-01", "SC failed")
    thread.join(timeout=2.0)

    assert result_holder == {"ok": False}


def test_local_worker_adapter_delegates_local_clean_files() -> None:
    from engine.local_worker_adapter import LocalWorkerAdapter
    from engine.local_worker_registry import LocalWorkerRegistry

    registry = LocalWorkerRegistry(timeout_seconds=90.0, clock=lambda: 100.0)
    registry.register("local-pc-01", {"clean": True})
    adapter = LocalWorkerAdapter(registry, result_poll_interval=0.01)

    result_holder: dict[str, bool] = {}

    def wait_for_result() -> None:
        result_holder["ok"] = adapter.clean_local_files(
            "sw",
            config_name=7,
            timeout_seconds=2.0,
        )

    thread = threading.Thread(target=wait_for_result)
    thread.start()

    task = registry.poll_task("local-pc-01")
    assert task is not None
    assert task["step"] == "clean_local_files"
    assert task["params"] == {"step_name": "sw", "config_name": 7}

    registry.complete_task(str(task["task_id"]), "local-pc-01", {"ok": True})
    thread.join(timeout=2.0)

    assert result_holder == {"ok": True}


def test_local_worker_adapter_times_out_when_no_worker_reports() -> None:
    from engine.local_worker_adapter import LocalWorkerAdapter
    from engine.local_worker_registry import LocalWorkerRegistry

    registry = LocalWorkerRegistry(timeout_seconds=90.0, clock=lambda: 100.0)
    registry.register("local-pc-01", {"sw": True})
    adapter = LocalWorkerAdapter(registry, result_poll_interval=0.01)

    started = time.monotonic()

    assert adapter.execute_sw_step(timeout_seconds=0.03) is False
    assert time.monotonic() - started < 1.0
    assert registry.poll_task("local-pc-01") is None


def test_local_worker_adapter_reports_active_registry_task() -> None:
    from engine.local_worker_adapter import LocalWorkerAdapter
    from engine.local_worker_registry import LocalWorkerRegistry

    registry = LocalWorkerRegistry(timeout_seconds=90.0, clock=lambda: 100.0)
    registry.register("local-pc-01", {"sw": True})
    adapter = LocalWorkerAdapter(registry, result_poll_interval=0.01)

    task = registry.enqueue_task("sw", {"config_name": 7})

    assert adapter.has_active_task("sw", 7) is True
    assert adapter.has_active_task("sw", 8) is False

    registry.complete_task(str(task["task_id"]), "local-pc-01", {"ok": True})

    assert adapter.has_active_task("sw", 7) is False
