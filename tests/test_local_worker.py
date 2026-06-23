from __future__ import annotations

import base64
from collections import deque
import threading
import time


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
            capabilities={"sw": True, "sc_slots": 1},
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
        "capabilities": {"sw": True, "sc_slots": 1},
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


def test_local_worker_from_env_defaults_to_one_sc_slot(monkeypatch) -> None:
    from engine.local_worker import LocalWorker

    monkeypatch.setenv("AUTOFLUID_WORKER_ID", "local-pc-01")
    monkeypatch.setenv("AUTOFLUID_SERVER_HOST", "ocar.example.test")
    monkeypatch.setenv("AUTOFLUID_DISCOVER_PUBLIC_IP", "0")
    monkeypatch.setattr("engine.local_worker.detect_candidate_hosts", lambda: [])

    worker = LocalWorker.from_env()

    assert worker.config.capabilities["sc_slots"] == 1
    assert worker._lane_limit("sc") == 1


def test_local_worker_from_env_does_not_reuse_workstation_reachable_metadata(monkeypatch) -> None:
    from engine.local_worker import LocalWorker

    monkeypatch.setenv("AUTOFLUID_WORKER_ID", "local-pc-01")
    monkeypatch.setenv("AUTOFLUID_SERVER_HOST", "ocar.example.test")
    monkeypatch.setenv("AUTOFLUID_IPC_PORT", "9527")
    monkeypatch.setenv("AUTOFLUID_SSH_REACHABLE_HOST", "127.0.0.1")
    monkeypatch.setenv("AUTOFLUID_SSH_REACHABLE_PORT", "2222")
    monkeypatch.setenv("AUTOFLUID_SSH_CONNECTIVITY_MODE", "reverse_tunnel")
    monkeypatch.setenv("AUTOFLUID_DISCOVER_PUBLIC_IP", "0")
    monkeypatch.setattr("engine.local_worker.detect_candidate_hosts", lambda: [])

    worker = LocalWorker.from_env()

    assert worker.config.network == {
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


def test_local_worker_register_reuses_cached_local_excel_configs(monkeypatch) -> None:
    import engine.local_worker as local_worker_module
    from engine.local_worker import LocalWorker, LocalWorkerConfig

    calls: list[str] = []

    def fake_read_model_configs(path: str):
        calls.append(path)
        return {1: [1.0, 2.0, 3.0, 4.0]}

    monkeypatch.setattr(local_worker_module, "read_model_configs", fake_read_model_configs)
    worker = LocalWorker(LocalWorkerConfig("local-pc-01", "ocar", 9527))

    first = worker.build_register_request()
    second = worker.build_register_request()

    assert calls == [local_worker_module.LOCAL_PATHS["excel"]]
    assert first["params"]["configs"] == second["params"]["configs"]


def test_local_worker_drops_pending_report_after_daemon_ack(monkeypatch) -> None:
    from engine.local_worker import LocalWorker, LocalWorkerConfig
    from ipc.protocol import CMD_WORKER_STEP_ERROR

    sent_reports: list[dict] = []
    worker = LocalWorker(LocalWorkerConfig("local-pc-01", "ocar", 9527))
    report = worker.build_step_error_request("local-19", "SpaceClaim ready timeout")
    worker._pending_reports.append(report)

    def fake_send_request(request: dict) -> dict:
        sent_reports.append(request)
        return {
            "status": "ok",
            "data": {"status": "error", "error": "engine stopped"},
            "message": "LocalWorker 任务已丢弃: engine stopped",
        }

    monkeypatch.setattr(worker, "_send_request", fake_send_request)

    assert worker._send_next_finished_report() == "error"
    assert sent_reports == [report]
    assert report["command"] == CMD_WORKER_STEP_ERROR
    assert worker._pending_reports == []


def test_local_worker_task_runner_reuses_registered_excel_configs(monkeypatch) -> None:
    import engine.local_worker as local_worker_module
    from engine.local_worker import LocalWorker, LocalWorkerConfig

    calls: list[str] = []
    loaded_configs: list[dict[int, list[float]]] = []

    def fake_read_model_configs(path: str):
        calls.append(path)
        return {1: [1.0, 2.0, 3.0, 4.0]}

    class _State:
        def load_configs(self, configs):
            loaded_configs.append(configs)

    class _Runner:
        def execute_sw_per_config(self, config_name: int) -> bool:
            assert config_name == 1
            return True

    monkeypatch.setattr(local_worker_module, "read_model_configs", fake_read_model_configs)
    monkeypatch.setattr(local_worker_module, "StateManager", lambda **_kwargs: _State())
    monkeypatch.setattr(local_worker_module, "TaskRunner", lambda _state: _Runner())

    worker = LocalWorker(LocalWorkerConfig("local-pc-01", "ocar", 9527))

    worker.build_register_request()
    assert worker._execute_task("sw", {"config_name": 1}) == {"ok": True}

    assert calls == [local_worker_module.LOCAL_PATHS["excel"]]
    assert loaded_configs == [{1: [1.0, 2.0, 3.0, 4.0]}]


def test_local_worker_task_runner_executes_local_steps_without_redelegating(monkeypatch) -> None:
    import engine.local_worker as local_worker_module
    from engine.local_worker import LocalWorker, LocalWorkerConfig

    class _State:
        def load_configs(self, configs):
            pass

    class _Runner:
        def __init__(self, _state):
            self._local_worker_adapter = None

        def _should_delegate_local_steps(self) -> bool:
            return self._local_worker_adapter is not None

    monkeypatch.setenv("AUTOFLUID_SERVER_MODE", "server")
    monkeypatch.setattr(local_worker_module, "read_model_configs", lambda _path: {1: [1.0]})
    monkeypatch.setattr(local_worker_module, "StateManager", lambda **_kwargs: _State())
    monkeypatch.setattr(local_worker_module, "TaskRunner", _Runner)

    worker = LocalWorker(LocalWorkerConfig("local-pc-01", "ocar", 9527))
    runner = worker._get_default_runner()

    assert runner._should_delegate_local_steps() is False


def test_local_worker_default_runner_uses_fingerprinted_state_db(monkeypatch, tmp_path) -> None:
    import engine.local_worker as local_worker_module
    from engine.config_fingerprint import compute_config_fingerprint
    from engine.local_worker import LocalWorker, LocalWorkerConfig

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    default_db = data_dir / "pipeline_state.db"
    configs = {1: [1.0, 2.0, 3.0, 4.0]}
    fingerprint = compute_config_fingerprint(configs)
    expected_db = data_dir / f"pipeline_state_{fingerprint}.db"

    monkeypatch.setitem(local_worker_module.LOCAL_PATHS, "data_dir", str(data_dir))
    monkeypatch.setattr(local_worker_module, "read_model_configs", lambda _path: configs)

    worker = LocalWorker(LocalWorkerConfig("local-pc-01", "ocar", 9527))
    runner = worker._get_default_runner()

    assert runner.state.db_path == str(expected_db)
    assert expected_db.exists()
    assert not default_db.exists()


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


def test_local_worker_reports_timed_out_isolated_sw_task(monkeypatch) -> None:
    from engine.local_worker import LocalWorker, LocalWorkerConfig

    worker = LocalWorker(
        LocalWorkerConfig(
            worker_id="local-pc-01",
            server_host="ocar.example.test",
            server_port=9527,
        )
    )

    calls: list[tuple[str, dict[str, object], float]] = []
    cleanup_steps: list[str] = []

    def fake_execute_in_subprocess(
        step: str,
        params: dict[str, object],
        timeout_seconds: float,
    ) -> dict[str, object]:
        calls.append((step, params, timeout_seconds))
        raise TimeoutError("LocalWorker 子任务超时")

    monkeypatch.setattr(
        worker,
        "_execute_task_in_subprocess",
        fake_execute_in_subprocess,
        raising=False,
    )
    monkeypatch.setattr(
        worker,
        "_cleanup_after_task_timeout",
        lambda step: cleanup_steps.append(step),
        raising=False,
    )

    response = worker.handle_polled_task({
        "task_id": "task-timeout",
        "step": "sw",
        "params": {"config_name": 7},
        "timeout_seconds": 0.25,
    })

    assert calls == [("sw", {"config_name": 7}, 0.25)]
    assert cleanup_steps == ["sw"]
    assert response["command"] == "worker_step_error"
    assert response["params"]["task_id"] == "task-timeout"
    assert "超时" in response["params"]["error"]


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

    assert worker.run_once(now=100.0) == "dispatched"
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline and len(sent_commands) == 1:
        time.sleep(0.01)
    assert worker.run_once(now=101.0) == "completed"
    assert sent_commands == ["worker_poll", "worker_step_complete"]


def test_local_worker_runs_sc_parallel_but_keeps_sw_single_lane() -> None:
    from engine.local_worker import LocalWorker, LocalWorkerConfig

    started: list[str] = []
    worker = LocalWorker(
        LocalWorkerConfig(
            worker_id="local-pc-01",
            server_host="ocar.example.test",
            server_port=9527,
            capabilities={"sc_slots": 3},
        ),
        task_handlers={
            "sw": lambda _params: started.append("sw") or {"ok": True},
            "sc": lambda _params: started.append("sc") or {"ok": True},
        },
    )

    worker._active_lane_counts["sw"] = 1
    assert worker._try_start_task({
        "task_id": "sc-while-sw",
        "step": "sc",
        "params": {"config_name": 1},
    }) is True

    worker._active_lane_counts["sw"] = 1
    assert worker._try_start_task({
        "task_id": "sw-while-sw",
        "step": "sw",
        "params": {"config_name": 2},
    }) is False

    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline and "sc" not in started:
        time.sleep(0.01)
    assert started == ["sc"]


def test_local_worker_allows_three_parallel_sc_tasks_but_single_sw() -> None:
    from engine.local_worker import LocalWorker, LocalWorkerConfig

    worker = LocalWorker(
        LocalWorkerConfig(
            worker_id="local-pc-01",
            server_host="ocar.example.test",
            server_port=9527,
            capabilities={"sc_slots": 3},
        )
    )

    worker._active_lane_counts["sc"] = 2
    assert worker._has_available_lane() is True
    assert worker._task_lane("sc") == "sc"
    assert worker._lane_limit("sc") == 3

    worker._active_lane_counts["sc"] = 3
    assert worker._try_start_task({
        "task_id": "sc-full",
        "step": "sc",
        "params": {"config_name": 4},
    }) is False

    worker._active_lane_counts["sw"] = 1
    assert worker._try_start_task({
        "task_id": "sw-full",
        "step": "sw",
        "params": {"config_name": 5},
    }) is False


def test_local_worker_run_once_allows_sc_parallel_without_parallel_sw() -> None:
    from engine.local_worker import LocalWorker, LocalWorkerConfig

    tasks = deque([
        {"task_id": "sw-1", "step": "sw", "params": {"config_name": 1}},
        {"task_id": "sc-1", "step": "sc", "params": {"config_name": 1}},
        {"task_id": "sw-2", "step": "sw", "params": {"config_name": 2}},
        {"task_id": "sw-3", "step": "sw", "params": {"config_name": 3}},
    ])
    reports: list[str] = []
    active_sw = 0
    max_active_sw = 0
    lock = threading.Lock()
    sc_started = threading.Event()
    sw2_started = threading.Event()
    sw3_started = threading.Event()
    sc_release = threading.Event()
    sw2_release = threading.Event()

    def sw_handler(params: dict[str, object]) -> dict[str, object]:
        nonlocal active_sw, max_active_sw
        config_name = int(params["config_name"])
        with lock:
            active_sw += 1
            max_active_sw = max(max_active_sw, active_sw)
        try:
            if config_name == 2:
                sw2_started.set()
                assert sc_started.wait(timeout=1.0)
                assert sw2_release.wait(timeout=1.0)
            elif config_name == 3:
                sw3_started.set()
            return {"ok": True}
        finally:
            with lock:
                active_sw -= 1

    def sc_handler(params: dict[str, object]) -> dict[str, object]:
        assert int(params["config_name"]) == 1
        sc_started.set()
        assert sc_release.wait(timeout=1.0)
        return {"ok": True}

    worker = LocalWorker(
        LocalWorkerConfig(
            worker_id="local-pc-01",
            server_host="ocar.example.test",
            server_port=9527,
            capabilities={"sc_slots": 3},
        ),
        task_handlers={
            "sw": sw_handler,
            "sc": sc_handler,
        },
    )

    def fake_send(request):
        command = str(request["command"])
        if command == "worker_poll":
            if tasks:
                return {"status": "ok", "data": tasks.popleft()}
            return {"status": "ok", "data": None}
        reports.append(str(request["params"]["task_id"]))
        return {"status": "ok", "data": {}}

    worker._send_request = fake_send

    assert worker.run_once(now=100.0) == "dispatched"
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline and "sw-1" not in reports:
        worker.run_once(now=100.1)
        time.sleep(0.01)

    assert "sw-1" in reports
    assert worker.run_once(now=101.0) == "dispatched"
    assert sc_started.wait(timeout=1.0)
    assert worker.run_once(now=102.0) == "dispatched"
    assert sw2_started.wait(timeout=1.0)
    assert sc_started.is_set()
    assert worker.run_once(now=103.0) == "deferred"
    assert sw3_started.is_set() is False
    assert max_active_sw == 1

    sc_release.set()
    sw2_release.set()
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline and not {"sc-1", "sw-2"}.issubset(reports):
        worker.run_once(now=104.0)
        time.sleep(0.01)

    assert {"sc-1", "sw-2"}.issubset(reports)
    assert worker.run_once(now=105.0) == "dispatched"
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline and "sw-3" not in reports:
        worker.run_once(now=106.0)
        time.sleep(0.01)

    assert {"sc-1", "sw-2", "sw-3"}.issubset(reports)
    assert max_active_sw == 1


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


def test_local_worker_register_retry_budget_cools_down_without_exiting(monkeypatch) -> None:
    from engine.local_worker import LocalWorker, LocalWorkerConfig

    calls: list[str] = []
    sleeps: list[float] = []
    monotonic_values = iter([0.0, 1.0, 2.0, 123.0, 124.0])
    worker = LocalWorker(
        LocalWorkerConfig(
            "local-pc-01",
            "127.0.0.1",
            19527,
            register_retry_interval=2.0,
            register_max_consecutive_failures=3,
            register_max_recovery_seconds=120.0,
            register_failure_cooldown_seconds=30.0,
        )
    )

    def fake_register() -> dict[str, object]:
        calls.append("register")
        if len(calls) <= 3:
            raise RuntimeError("daemon is not ready")
        return {"status": "ok"}

    def fake_run_once() -> str:
        calls.append("run_once")
        raise KeyboardInterrupt

    monkeypatch.setattr(worker, "register_once", fake_register)
    monkeypatch.setattr(worker, "run_once", fake_run_once)
    monkeypatch.setattr("engine.local_worker.time.monotonic", lambda: next(monotonic_values))
    monkeypatch.setattr("engine.local_worker.time.sleep", lambda seconds: sleeps.append(seconds))

    try:
        worker.run_forever()
    except KeyboardInterrupt:
        pass

    assert calls == ["register", "register", "register", "register", "run_once"]
    assert sleeps == [2.0, 2.0, 30.0, worker.config.poll_interval]


def test_local_worker_register_retry_warning_is_rate_limited(monkeypatch, caplog) -> None:
    import logging

    from engine.local_worker import LocalWorker, LocalWorkerConfig

    now_values = iter([0.0, 1.0, 2.0, 3.0, 4.0])
    worker = LocalWorker(
        LocalWorkerConfig(
            "local-pc-01",
            "127.0.0.1",
            19527,
            register_retry_interval=1.0,
            register_max_consecutive_failures=4,
            register_max_recovery_seconds=120.0,
            register_log_repeat_seconds=60.0,
        )
    )

    def fake_register() -> dict[str, object]:
        raise RuntimeError("daemon is not ready")

    monkeypatch.setattr(worker, "register_once", fake_register)
    monkeypatch.setattr("engine.local_worker.time.monotonic", lambda: next(now_values))
    monkeypatch.setattr("engine.local_worker.time.sleep", lambda _seconds: None)

    with caplog.at_level(logging.WARNING):
        try:
            worker.run_forever()
        except StopIteration:
            pass

    retry_logs = [
        record
        for record in caplog.records
        if record.message == "daemon is not ready"
    ]
    exhausted_logs = [
        record
        for record in caplog.records
        if record.message.startswith("LocalWorker 注册重试预算耗尽:")
    ]
    recovery_logs = [record for record in caplog.records if "注册恢复窗口失败" in record.message]
    assert len(retry_logs) == 2
    assert len(exhausted_logs) == 1
    assert len(recovery_logs) == 1

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


def test_local_worker_main_persistent_mode_does_not_pre_register(monkeypatch) -> None:
    import engine.local_worker as local_worker_module

    calls: list[str] = []

    class _Worker:
        @classmethod
        def from_env(cls):
            calls.append("from_env")
            return cls()

        def register_once(self):
            calls.append("register")
            return {"status": "ok"}

        def run_forever(self):
            calls.append("run_forever")

    monkeypatch.setattr("sys.argv", ["engine.local_worker"])
    monkeypatch.setattr(local_worker_module.LocalWorker, "from_env", _Worker.from_env)

    assert local_worker_module.main() == 0
    assert calls == ["from_env", "run_forever"]


def test_local_worker_main_once_mode_registers_and_heartbeats(monkeypatch) -> None:
    import engine.local_worker as local_worker_module

    calls: list[str] = []

    class _Worker:
        @classmethod
        def from_env(cls):
            calls.append("from_env")
            return cls()

        def register_once(self):
            calls.append("register")
            return {"status": "ok"}

        def heartbeat_once(self):
            calls.append("heartbeat")
            return {"status": "ok"}

    monkeypatch.setattr("sys.argv", ["engine.local_worker", "--once"])
    monkeypatch.setattr(local_worker_module.LocalWorker, "from_env", _Worker.from_env)

    assert local_worker_module.main() == 0
    assert calls == ["from_env", "register", "heartbeat"]


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
    monkeypatch.setattr(local_worker_module, "StateManager", lambda **_kwargs: _State())
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
    monkeypatch.setattr(local_worker_module, "StateManager", lambda **_kwargs: _State())
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


def test_local_worker_default_handlers_support_stage_cleanup(monkeypatch) -> None:
    import engine.local_worker as local_worker_module
    from engine.local_worker import LocalWorker, LocalWorkerConfig

    calls: list[str] = []

    class _Runner:
        def do_sw_final_cleanup(self) -> None:
            calls.append("sw_final")

        def do_sc_final_cleanup(self) -> None:
            calls.append("sc_final")

        def shutdown_sw_processes(self) -> None:
            calls.append("sw_shutdown")

        def shutdown_sc_pool(self) -> None:
            calls.append("sc_shutdown")

    class _State:
        def load_configs(self, _configs):
            pass

    monkeypatch.setattr(local_worker_module, "read_model_configs", lambda _path: {1: [1.0]})
    monkeypatch.setattr(local_worker_module, "StateManager", lambda **_kwargs: _State())
    monkeypatch.setattr(local_worker_module, "TaskRunner", lambda _state: _Runner())

    worker = LocalWorker(LocalWorkerConfig("local-pc-01", "ocar", 9527))

    assert worker._execute_task("cleanup_stage", {"step_name": "sw", "phase": "final"}) == {
        "ok": True,
    }
    assert worker._execute_task("cleanup_stage", {"step_name": "sc", "phase": "final"}) == {
        "ok": True,
    }
    assert worker._execute_task("cleanup_stage", {"step_name": "sw", "phase": "shutdown"}) == {
        "ok": True,
    }
    assert worker._execute_task("cleanup_stage", {"step_name": "sc", "phase": "shutdown"}) == {
        "ok": True,
    }
    assert calls == ["sw_final", "sc_final", "sw_shutdown", "sc_shutdown"]


def test_local_worker_sc_timeout_task_runs_in_process_to_reuse_pool(monkeypatch) -> None:
    from engine.local_worker import LocalWorker, LocalWorkerConfig

    worker = LocalWorker(
        LocalWorkerConfig("local-pc-01", "ocar", 9527),
        task_handlers={"sc": lambda params: {"ok": True, "config": params["config_name"]}},
    )

    def fail_if_isolated(*_args, **_kwargs):
        raise AssertionError("SC tasks must reuse the LocalWorker process and SCProcessPool")

    monkeypatch.setattr(worker, "_execute_task_in_subprocess", fail_if_isolated)

    response = worker.handle_polled_task({
        "task_id": "sc-1",
        "step": "sc",
        "params": {"config_name": 7},
        "timeout_seconds": 45,
    })

    assert response["command"] == "worker_step_complete"
    assert response["params"]["result"] == {"ok": True, "config": 7}


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
    monkeypatch.setattr(local_worker_module, "StateManager", lambda **_kwargs: _State())
    monkeypatch.setattr(local_worker_module, "TaskRunner", lambda _state: _Runner())

    worker = LocalWorker(LocalWorkerConfig("local-pc-01", "ocar", 9527))

    result = worker._execute_task("sc", {"config_name": 7})

    assert result["ok"] is True
    payload = result["scdoc_file"]
    assert payload["config_name"] == 7
    assert payload["filename"] == "model_gen4_7.scdoc"
    assert payload["size"] == len(b"scdoc payload")
    assert base64.b64decode(payload["content_b64"]) == b"scdoc payload"


def test_local_worker_sc_task_includes_failure_reason(monkeypatch) -> None:
    import engine.local_worker as local_worker_module
    from engine.local_worker import LocalWorker, LocalWorkerConfig

    class _Runner:
        last_sc_error = "SpaceClaim ready timeout"

        def execute_sc_step(self, config_name: int) -> bool:
            assert config_name == 7
            return False

    class _State:
        def load_configs(self, _configs):
            pass

    monkeypatch.setattr(local_worker_module, "read_model_configs", lambda _path: {7: [1.0]})
    monkeypatch.setattr(local_worker_module, "StateManager", lambda **_kwargs: _State())
    monkeypatch.setattr(local_worker_module, "TaskRunner", lambda _state: _Runner())

    worker = LocalWorker(LocalWorkerConfig("local-pc-01", "ocar", 9527))

    result = worker._execute_task("sc", {"config_name": 7})

    assert result == {"ok": False, "error": "SpaceClaim ready timeout"}


def test_local_worker_sw_task_includes_failure_reason(monkeypatch) -> None:
    import engine.local_worker as local_worker_module
    from engine.local_worker import LocalWorker, LocalWorkerConfig

    class _Runner:
        last_sw_error = "SolidWorks SaveAs returned false"

        def execute_sw_per_config(self, config_name: int) -> bool:
            assert config_name == 7
            return False

    class _State:
        def load_configs(self, _configs):
            pass

    monkeypatch.setattr(local_worker_module, "read_model_configs", lambda _path: {7: [1.0]})
    monkeypatch.setattr(local_worker_module, "StateManager", lambda **_kwargs: _State())
    monkeypatch.setattr(local_worker_module, "TaskRunner", lambda _state: _Runner())

    worker = LocalWorker(LocalWorkerConfig("local-pc-01", "ocar", 9527))

    result = worker._execute_task("sw", {"config_name": 7})

    assert result == {"ok": False, "error": "SolidWorks SaveAs returned false"}
