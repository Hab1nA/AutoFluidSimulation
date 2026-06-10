from __future__ import annotations

import base64


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


def test_daemon_worker_step_complete_persists_scdoc_payload(tmp_path, monkeypatch) -> None:
    from engine.config import LOCAL_PATHS
    from engine.daemon import PipelineDaemon

    monkeypatch.setitem(LOCAL_PATHS, "scdoc_dir", str(tmp_path))
    daemon = PipelineDaemon()
    daemon.handle_worker_register({"worker_id": "local-pc-01", "capabilities": {"sc": True}})
    queued = daemon.local_worker_registry.enqueue_task("sc", {"config_name": 7})

    ok, completed, message = daemon.handle_worker_step_complete({
        "worker_id": "local-pc-01",
        "task_id": queued["task_id"],
        "result": {
            "ok": True,
            "scdoc_file": {
                "config_name": 7,
                "filename": "model_gen4_7.scdoc",
                "size": len(b"server payload"),
                "content_b64": base64.b64encode(b"server payload").decode("ascii"),
            },
        },
    })

    assert ok is True
    assert message == "LocalWorker 任务完成"
    assert completed["status"] == "completed"
    assert completed["result"]["scdoc_file"] == {
        "config_name": 7,
        "filename": "model_gen4_7.scdoc",
        "size": len(b"server payload"),
        "server_path": str(tmp_path / "model_gen4_7.scdoc"),
    }
    assert (tmp_path / "model_gen4_7.scdoc").read_bytes() == b"server payload"


def test_daemon_worker_poll_returns_local_clean_task() -> None:
    from engine.daemon import PipelineDaemon

    daemon = PipelineDaemon()
    daemon.handle_worker_register({"worker_id": "local-pc-01", "capabilities": {"clean": True}})
    daemon.local_worker_registry.enqueue_task(
        "clean_local_files",
        {"step_name": "sw", "config_name": 3},
    )

    ok, task, message = daemon.handle_worker_poll({"worker_id": "local-pc-01"})

    assert ok is True
    assert message == "LocalWorker 已领取任务"
    assert task["step"] == "clean_local_files"
    assert task["params"] == {"step_name": "sw", "config_name": 3}
