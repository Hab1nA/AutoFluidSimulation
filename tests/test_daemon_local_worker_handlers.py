from __future__ import annotations


def test_daemon_worker_poll_and_completion_handlers() -> None:
    from engine.daemon import PipelineDaemon

    daemon = PipelineDaemon()
    daemon.handle_worker_register({"worker_id": "local-pc-01", "capabilities": {"sw": True}})
    queued = daemon.local_worker_registry.enqueue_task("sw", {})

    ok, task, message = daemon.handle_worker_poll({"worker_id": "local-pc-01"})

    assert ok is True
    assert message == "LocalWorker 已领取任务"
    assert task["task_id"] == queued["task_id"]

    ok, completed, message = daemon.handle_worker_step_complete({
        "worker_id": "local-pc-01",
        "task_id": queued["task_id"],
        "result": {"ok": True},
    })

    assert ok is True
    assert message == "LocalWorker 任务完成"
    assert completed["status"] == "completed"


def test_daemon_worker_poll_returns_none_when_idle() -> None:
    from engine.daemon import PipelineDaemon

    daemon = PipelineDaemon()
    daemon.handle_worker_register({"worker_id": "local-pc-01", "capabilities": {"sw": True}})

    ok, task, message = daemon.handle_worker_poll({"worker_id": "local-pc-01"})

    assert ok is True
    assert task is None
    assert message == "LocalWorker 暂无任务"
