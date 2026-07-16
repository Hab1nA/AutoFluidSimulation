from __future__ import annotations

import subprocess
import sys
import threading

from engine import daemon as daemon_module
from engine import workstation_ssh_recovery as recovery_module
from engine.daemon import PipelineDaemon
from engine.workstation_ssh_recovery import (
    CompositeWorkstationTunnelRepairer,
    LocalWorkerWorkstationTunnelRepairer,
    SubprocessWorkstationTunnelRepairer,
    WorkstationSshRecoveryManager,
    WorkstationSshRecoveryPolicy,
)


class _Ssh:
    def __init__(self, connected: bool) -> None:
        self._connected = connected

    def is_connected(self) -> bool:
        return self._connected


class _Runner:
    def __init__(self, connected_by_id: dict[str, bool]) -> None:
        self._connected_by_id = connected_by_id
        self.disconnect_calls: list[str | None] = []
        self.get_ssh_calls: list[str] = []

    def get_ssh(self, workstation_id: str = "default", *, log_failure: bool = True) -> _Ssh:
        self.get_ssh_calls.append(workstation_id)
        return _Ssh(self._connected_by_id[workstation_id])

    def disconnect_ssh(
        self,
        workstation_id: str | None = None,
        *,
        lock_timeout: float | None = None,
    ) -> None:
        self.disconnect_calls.append(workstation_id)


class _State:
    def __init__(
        self,
        engine_status: str,
        remote_tasks: list[dict] | None = None,
        statuses: dict[int, dict[str, str]] | None = None,
    ) -> None:
        self.engine_status = engine_status
        self.remote_tasks = list(remote_tasks or [])
        self.statuses = dict(statuses or {})

    def get_engine_status(self) -> str:
        return self.engine_status

    def get_all_remote_tasks(self) -> list[dict]:
        return list(self.remote_tasks)

    def get_all_statuses(self) -> dict[int, dict[str, str]]:
        return dict(self.statuses)


class _FlippingRunner:
    def __init__(self) -> None:
        self.connected = False
        self.disconnect_calls: list[str | None] = []

    def get_ssh(self, workstation_id: str = "default", *, log_failure: bool = True) -> _Ssh:
        return _Ssh(self.connected)

    def disconnect_ssh(
        self,
        workstation_id: str | None = None,
        *,
        lock_timeout: float | None = None,
    ) -> None:
        self.disconnect_calls.append(workstation_id)


class _Repairer:
    def __init__(self, ok: bool = True) -> None:
        self.ok = ok
        self.calls: list[tuple[str, dict, float]] = []

    def repair(
        self,
        workstation_id: str,
        target: dict,
        *,
        timeout_seconds: float,
        cancel_event: threading.Event | None = None,
    ) -> dict:
        self.calls.append((workstation_id, dict(target), timeout_seconds))
        return {
            "ok": self.ok,
            "status": "repaired" if self.ok else "repair_failed",
            "detail": "repair complete" if self.ok else "repair failed",
        }


def test_recovery_manager_repairs_reverse_tunnel_after_threshold() -> None:
    repairer = _Repairer()
    manager = WorkstationSshRecoveryManager(
        policy=WorkstationSshRecoveryPolicy(
            enabled=True,
            failure_threshold=2,
            min_repair_interval_seconds=60.0,
            repair_timeout_seconds=42.0,
        ),
        repairer=repairer,
        clock=lambda: 100.0,
    )
    target = {"host": "127.0.0.1", "port": 2225, "connectivity_mode": "reverse_tunnel"}

    first = manager.record_check("WS-C", "disconnected", target, source="active_probe")
    second = manager.record_check("WS-C", "disconnected", target, source="active_probe")

    assert first["repair"]["status"] == "waiting_for_threshold"
    assert second["repair"]["status"] == "repair_succeeded"
    assert repairer.calls == [("WS-C", target, 42.0)]
    snapshot = manager.snapshot()["WS-C"]
    assert snapshot["failure_count"] == 2
    assert snapshot["last_repair_status"] == "repair_succeeded"


def test_recovery_manager_throttles_repair_and_resets_on_ok() -> None:
    now = {"value": 100.0}
    repairer = _Repairer()
    manager = WorkstationSshRecoveryManager(
        policy=WorkstationSshRecoveryPolicy(
            enabled=True,
            failure_threshold=1,
            min_repair_interval_seconds=60.0,
        ),
        repairer=repairer,
        clock=lambda: now["value"],
    )
    target = {"host": "127.0.0.1", "port": 2226, "connectivity_mode": "reverse_tunnel"}

    first = manager.record_check("WS-D", "disconnected", target, source="active_probe")
    second = manager.record_check("WS-D", "disconnected", target, source="active_probe")
    now["value"] = 200.0
    manager.record_check("WS-D", "ok", target, source="active_probe")

    assert first["repair"]["status"] == "repair_succeeded"
    assert second["repair"]["status"] == "throttled"
    assert len(repairer.calls) == 1
    assert manager.snapshot()["WS-D"]["failure_count"] == 0


def test_recovery_manager_ok_clears_stale_repair_failure_detail() -> None:
    repairer = _Repairer(ok=False)
    manager = WorkstationSshRecoveryManager(
        policy=WorkstationSshRecoveryPolicy(enabled=True, failure_threshold=1),
        repairer=repairer,
        clock=lambda: 100.0,
    )
    target = {"host": "127.0.0.1", "port": 2226, "connectivity_mode": "reverse_tunnel"}

    manager.record_check("WS-D", "disconnected", target, source="active_probe")
    manager.record_check("WS-D", "ok", target, source="active_probe")

    snapshot = manager.snapshot()["WS-D"]
    assert snapshot["failure_count"] == 0
    assert snapshot["last_repair_status"] == "healthy"
    assert "last_repair_detail" not in snapshot


def test_recovery_manager_reset_history_clears_stale_throttle() -> None:
    repairer = _Repairer()
    manager = WorkstationSshRecoveryManager(
        policy=WorkstationSshRecoveryPolicy(
            enabled=True,
            failure_threshold=1,
            min_repair_interval_seconds=300.0,
        ),
        repairer=repairer,
        clock=lambda: 100.0,
    )
    target = {"host": "127.0.0.1", "port": 2226, "connectivity_mode": "reverse_tunnel"}
    manager.record_check("WS-D", "disconnected", target, source="active_probe")

    assert manager.reset_history() is True
    result = manager.record_check("WS-D", "disconnected", target, source="active_probe")

    assert result["repair"]["status"] == "repair_succeeded"
    assert len(repairer.calls) == 2


def test_recovery_manager_shutdown_gate_stays_closed_until_reset() -> None:
    repairer = _Repairer()
    manager = WorkstationSshRecoveryManager(
        policy=WorkstationSshRecoveryPolicy(enabled=True, failure_threshold=1),
        repairer=repairer,
        clock=lambda: 100.0,
    )
    target = {"host": "127.0.0.1", "port": 2226, "connectivity_mode": "reverse_tunnel"}

    assert manager.shutdown(timeout=0.01) is True
    shutdown_result = manager.record_check(
        "WS-D",
        "disconnected",
        target,
        source="active_probe",
    )

    assert shutdown_result["repair"]["status"] == "shutdown"
    assert repairer.calls == []

    assert manager.reset_history() is True
    repaired_result = manager.record_check(
        "WS-D",
        "disconnected",
        target,
        source="active_probe",
    )

    assert repaired_result["repair"]["status"] == "repair_succeeded"
    assert repairer.calls == [("WS-D", target, 180.0)]


def test_recovery_manager_ignores_direct_connectivity_failures() -> None:
    repairer = _Repairer()
    manager = WorkstationSshRecoveryManager(
        policy=WorkstationSshRecoveryPolicy(enabled=True, failure_threshold=1),
        repairer=repairer,
        clock=lambda: 100.0,
    )
    target = {"host": "172.17.135.115", "port": 22, "connectivity_mode": "direct"}

    result = manager.record_check("WS-C", "disconnected", target, source="active_probe")

    assert result["repair"]["status"] == "not_reverse_tunnel"
    assert repairer.calls == []


def test_active_probe_interval_can_be_tuned_by_env(monkeypatch) -> None:
    monkeypatch.setenv("AUTOFLUID_WORKSTATION_SSH_ACTIVE_PROBE_INTERVAL", "3")

    assert daemon_module._workstation_ssh_active_probe_interval_seconds() == 3.0


def test_local_worker_repairer_delegates_to_local_worker_adapter() -> None:
    class _Adapter:
        def __init__(self) -> None:
            self.calls = []

        def has_online_worker(self) -> bool:
            return True

        def ensure_workstation_tunnel(self, workstation_id: str, *, timeout_seconds: float):
            self.calls.append((workstation_id, timeout_seconds))
            return {"ok": True, "results": [{"id": workstation_id, "ok": True}]}

    adapter = _Adapter()
    repairer = LocalWorkerWorkstationTunnelRepairer(adapter)

    result = repairer.repair(
        "WS-C",
        {"connectivity_mode": "reverse_tunnel"},
        timeout_seconds=12.0,
    )

    assert result["ok"] is True
    assert result["status"] == "repair_succeeded"
    assert adapter.calls == [("WS-C", 12.0)]


def test_local_worker_repairer_skips_offline_adapter_dispatch() -> None:
    class _OfflineAdapter:
        def has_online_worker(self) -> bool:
            return False

        def ensure_workstation_tunnel(self, workstation_id: str, *, timeout_seconds: float):
            raise AssertionError("offline LocalWorker must not receive tunnel tasks")

    repairer = LocalWorkerWorkstationTunnelRepairer(_OfflineAdapter())

    result = repairer.repair(
        "WS-C",
        {"connectivity_mode": "reverse_tunnel"},
        timeout_seconds=12.0,
    )

    assert result == {
        "ok": False,
        "status": "local_worker_unavailable",
        "detail": "没有在线 LocalWorker",
        "target": {"connectivity_mode": "reverse_tunnel"},
    }


def test_composite_repairer_prefers_local_worker_before_subprocess() -> None:
    class _FailingRepairer:
        def repair(self, workstation_id, target, *, timeout_seconds, cancel_event=None):
            return {"ok": False, "status": "no_local_worker", "detail": "offline"}

    subprocess_repairer = _Repairer()
    repairer = CompositeWorkstationTunnelRepairer([
        _FailingRepairer(),
        subprocess_repairer,
    ])

    result = repairer.repair(
        "WS-C",
        {"connectivity_mode": "reverse_tunnel"},
        timeout_seconds=12.0,
    )

    assert result["ok"] is True
    assert result["status"] == "repaired"
    assert subprocess_repairer.calls == [(
        "WS-C",
        {"connectivity_mode": "reverse_tunnel"},
        12.0,
    )]


def test_composite_repairer_reports_all_failures() -> None:
    class _FailingRepairer:
        def __init__(self, status: str) -> None:
            self.status = status

        def repair(self, workstation_id, target, *, timeout_seconds, cancel_event=None):
            return {"ok": False, "status": self.status, "detail": workstation_id}

    repairer = CompositeWorkstationTunnelRepairer([
        _FailingRepairer("local_failed"),
        _FailingRepairer("subprocess_failed"),
    ])

    result = repairer.repair(
        "WS-C",
        {"connectivity_mode": "reverse_tunnel"},
        timeout_seconds=12.0,
    )

    assert result["ok"] is False
    assert result["status"] == "all_repairers_failed"
    assert [attempt["status"] for attempt in result["attempts"]] == [
        "local_failed",
        "subprocess_failed",
    ]


def test_recovery_manager_reports_disabled_and_missing_repairer() -> None:
    target = {"host": "127.0.0.1", "port": 2226, "connectivity_mode": "reverse_tunnel"}
    disabled_manager = WorkstationSshRecoveryManager(
        policy=WorkstationSshRecoveryPolicy(enabled=False, failure_threshold=1),
        repairer=_Repairer(),
        clock=lambda: 100.0,
    )
    no_repairer_manager = WorkstationSshRecoveryManager(
        policy=WorkstationSshRecoveryPolicy(enabled=True, failure_threshold=1),
        repairer=None,
        clock=lambda: 100.0,
    )

    disabled_result = disabled_manager.record_check(
        "WS-D",
        "disconnected",
        target,
        source="active_probe",
    )
    no_repairer_result = no_repairer_manager.record_check(
        "WS-D",
        "disconnected",
        target,
        source="active_probe",
    )

    assert disabled_result["repair"]["status"] == "disabled"
    assert no_repairer_result["repair"]["status"] == "no_repairer"


def test_daemon_active_probe_records_recovery_and_disconnects_stale_pool(monkeypatch) -> None:
    monkeypatch.setattr(
        daemon_module,
        "WORKSTATIONS",
        [{
            "id": "WS-C",
            "host": "172.17.135.115",
            "port": 22,
            "reachable_host": "127.0.0.1",
            "reachable_port": 2225,
            "connectivity_mode": "reverse_tunnel",
        }],
    )
    runner = _Runner({"WS-C": False})
    repairer = _Repairer()
    manager = WorkstationSshRecoveryManager(
        policy=WorkstationSshRecoveryPolicy(enabled=True, failure_threshold=1),
        repairer=repairer,
        clock=lambda: 100.0,
    )
    daemon = PipelineDaemon.__new__(PipelineDaemon)
    daemon.runner = runner
    daemon._last_worker_ssh_checks = {}
    daemon._workstation_ssh_recovery = manager

    result = daemon._refresh_workstation_ssh_checks(source="active_probe")

    assert result["ssh_checks"] == {"WS-C": "disconnected"}
    assert result["ssh_recovery"]["WS-C"]["repair"]["status"] == "repair_succeeded"
    assert runner.disconnect_calls == ["WS-C"]
    assert daemon._build_health_snapshot()["workstation_ssh_recovery"]["WS-C"][
        "last_repair_status"
    ] == "repair_verified_failed"
    assert (
        "last_repair_started_at"
        not in daemon._build_health_snapshot()["workstation_ssh_recovery"]["WS-C"]
    )


def test_daemon_post_repair_type_error_does_not_abort_other_workstations(monkeypatch) -> None:
    monkeypatch.setattr(
        daemon_module,
        "WORKSTATIONS",
        [
            {
                "id": "WS-C",
                "host": "172.17.135.115",
                "port": 22,
                "reachable_host": "127.0.0.1",
                "reachable_port": 2225,
                "connectivity_mode": "reverse_tunnel",
            },
            {
                "id": "WS-D",
                "host": "172.17.135.116",
                "port": 22,
                "reachable_host": "127.0.0.1",
                "reachable_port": 2226,
                "connectivity_mode": "reverse_tunnel",
            },
        ],
    )

    class _TypeErrorRunner(_Runner):
        def __init__(self, connected_by_id: dict[str, bool]) -> None:
            super().__init__(connected_by_id)
            self.get_calls: dict[str, int] = {}

        def get_ssh(self, workstation_id: str = "default", *, log_failure: bool = True) -> _Ssh:
            self.get_calls[workstation_id] = self.get_calls.get(workstation_id, 0) + 1
            if workstation_id == "WS-C" and self.get_calls[workstation_id] >= 2:
                raise TypeError("internal paramiko type error")
            return _Ssh(self._connected_by_id[workstation_id])

    daemon = PipelineDaemon.__new__(PipelineDaemon)
    daemon.runner = _TypeErrorRunner({"WS-C": False, "WS-D": True})
    daemon._last_worker_ssh_checks = {}
    daemon._workstation_ssh_recovery = WorkstationSshRecoveryManager(
        policy=WorkstationSshRecoveryPolicy(enabled=True, failure_threshold=1),
        repairer=_Repairer(),
        clock=lambda: 100.0,
    )

    result = daemon._refresh_workstation_ssh_checks(source="active_probe")

    assert result["ssh_checks"] == {"WS-C": "disconnected", "WS-D": "ok"}
    assert (
        daemon._workstation_ssh_recovery.snapshot()["WS-C"]["last_repair_status"]
        == "repair_verified_failed"
    )


def test_daemon_rechecks_ssh_after_successful_repair(monkeypatch) -> None:
    monkeypatch.setattr(
        daemon_module,
        "WORKSTATIONS",
        [{
            "id": "WS-C",
            "host": "172.17.135.115",
            "port": 22,
            "reachable_host": "127.0.0.1",
            "reachable_port": 2225,
            "connectivity_mode": "reverse_tunnel",
        }],
    )
    runner = _FlippingRunner()

    class _RepairerThatRestoresTunnel(_Repairer):
        def repair(
            self,
            workstation_id: str,
            target: dict,
            *,
            timeout_seconds: float,
            cancel_event: threading.Event | None = None,
        ) -> dict:
            runner.connected = True
            return super().repair(
                workstation_id,
                target,
                timeout_seconds=timeout_seconds,
                cancel_event=cancel_event,
            )

    daemon = PipelineDaemon.__new__(PipelineDaemon)
    daemon.runner = runner
    daemon._last_worker_ssh_checks = {}
    daemon._workstation_ssh_recovery = WorkstationSshRecoveryManager(
        policy=WorkstationSshRecoveryPolicy(enabled=True, failure_threshold=1),
        repairer=_RepairerThatRestoresTunnel(),
        clock=lambda: 100.0,
    )

    result = daemon._refresh_workstation_ssh_checks(source="active_probe")

    assert result["ssh_checks"] == {"WS-C": "ok"}
    assert result["ssh_recovery"]["WS-C"]["repair"]["status"] == "repair_succeeded"
    assert runner.disconnect_calls == ["WS-C"]
    assert daemon._last_worker_ssh_checks == {"WS-C": "ok"}
    assert daemon._workstation_ssh_recovery.snapshot()["WS-C"]["failure_count"] == 0


def test_worker_stop_shuts_down_recovery_without_workstation_tunnel_cleanup(monkeypatch) -> None:
    cleanup_calls: list[dict] = []

    def fake_cleanup(*args, **kwargs):
        cleanup_calls.append(dict(kwargs))
        return {"status": "ok"}

    class _Recovery:
        def __init__(self) -> None:
            self.shutdown_calls = 0

        def shutdown(self) -> None:
            self.shutdown_calls += 1

    monkeypatch.setattr(daemon_module, "cleanup_tunnel_watchdog_tasks", fake_cleanup)
    recovery = _Recovery()
    daemon = PipelineDaemon.__new__(PipelineDaemon)
    daemon.state = None
    daemon.runner = None
    daemon.local_worker_registry = type(
        "_Registry",
        (),
        {
            "clear_online_workers": lambda self: None,
            "clear_pending_tasks": lambda self: None,
        },
    )()
    daemon._last_worker_ssh_checks = {"WS-C": "disconnected"}
    daemon._workstation_ssh_recovery = recovery

    ok, data, _message = daemon.handle_worker_stop({})

    assert ok is True
    assert recovery.shutdown_calls == 1
    assert cleanup_calls == [{}]
    assert data["watchdog_cleanup"] == {"status": "ok"}


def test_worker_start_all_failed_still_starts_health_monitor_for_recovery(monkeypatch) -> None:
    monkeypatch.setattr(
        daemon_module,
        "WORKSTATIONS",
        [{
            "id": "WS-C",
            "host": "172.17.135.115",
            "port": 22,
            "reachable_host": "127.0.0.1",
            "reachable_port": 2225,
            "connectivity_mode": "reverse_tunnel",
        }],
    )
    started = []
    daemon = PipelineDaemon.__new__(PipelineDaemon)
    daemon.runner = _Runner({"WS-C": False})
    daemon.local_worker_registry = type(
        "_Registry",
        (),
        {
            "clear_online_workers": lambda self: None,
            "clear_pending_tasks": lambda self: None,
        },
    )()
    daemon._workstation_ssh_recovery = WorkstationSshRecoveryManager(
        policy=WorkstationSshRecoveryPolicy(enabled=True, failure_threshold=1),
        repairer=_Repairer(),
        clock=lambda: 100.0,
    )
    daemon._last_worker_ssh_active_probe_time = 500.0
    daemon._start_workstation_ssh_health_monitor = lambda: started.append(True)

    ok, data, message = daemon.handle_worker_start({})

    assert ok is False
    assert "SSH 连通检查全部失败" in message
    assert data["ssh_checks"] == {"WS-C": "disconnected"}
    assert started == [True]
    assert daemon._last_worker_ssh_active_probe_time == 0.0


def test_worker_start_partial_failure_forces_followup_recovery(monkeypatch) -> None:
    monkeypatch.setattr(
        daemon_module,
        "WORKSTATIONS",
        [
            {
                "id": "WS-A",
                "host": "172.17.135.240",
                "port": 22,
                "reachable_host": "127.0.0.1",
                "reachable_port": 2222,
                "connectivity_mode": "reverse_tunnel",
            },
            {
                "id": "WS-C",
                "host": "172.17.135.115",
                "port": 22,
                "reachable_host": "127.0.0.1",
                "reachable_port": 2225,
                "connectivity_mode": "reverse_tunnel",
            },
        ],
    )
    monkeypatch.setattr(daemon_module.time, "time", lambda: 1000.0)
    events = []
    repairer = _Repairer()
    daemon = PipelineDaemon.__new__(PipelineDaemon)
    daemon.runner = _Runner({"WS-A": True, "WS-C": False})
    daemon.state = _State("stopped")
    daemon.local_worker_registry = type(
        "_Registry",
        (),
        {"clear_online_workers": lambda self: events.append("registry_cleared")},
    )()
    daemon._workstation_ssh_recovery = WorkstationSshRecoveryManager(
        policy=WorkstationSshRecoveryPolicy(enabled=True, failure_threshold=2),
        repairer=repairer,
        clock=lambda: 1000.0,
    )
    daemon._last_worker_ssh_active_probe_time = 1000.0
    daemon._ssh_health_active_probe_interval_seconds = 300.0
    request_active_probe = daemon._request_workstation_ssh_active_probe

    def request_probe() -> None:
        events.append("probe_requested")
        request_active_probe()

    daemon._request_workstation_ssh_active_probe = request_probe
    daemon._start_workstation_ssh_health_monitor = lambda: events.append("monitor_started")

    ok, data, message = daemon.handle_worker_start({})
    followup = daemon._run_workstation_ssh_health_check_once()

    assert ok is True
    assert "部分工作站 SSH 连通检查失败" in message
    assert data["ssh_checks"] == {"WS-A": "ok", "WS-C": "disconnected"}
    assert events == ["registry_cleared", "probe_requested", "monitor_started"]
    assert followup["ssh_recovery"]["WS-C"]["repair"]["status"] == "repair_succeeded"
    assert [call[0] for call in repairer.calls] == ["WS-C"]


def test_health_monitor_runs_initial_active_probe_when_forced(monkeypatch) -> None:
    calls = []
    daemon = PipelineDaemon.__new__(PipelineDaemon)
    daemon.runner = object()
    daemon._ssh_health_stop_event = threading.Event()
    daemon._ssh_health_stop_event.set()
    daemon._ssh_health_interval_seconds = 30.0
    daemon._ssh_health_active_probe_interval_seconds = 300.0
    daemon._last_worker_ssh_active_probe_time = 1000.0

    monkeypatch.setattr(daemon_module.time, "time", lambda: 1000.0)

    def fake_refresh(*, connect=True, source="active_probe"):
        calls.append({"connect": connect, "source": source})
        return {"ssh_checks": {}, "ssh_targets": {}}

    daemon._refresh_workstation_ssh_checks = fake_refresh
    daemon._request_workstation_ssh_active_probe()

    daemon._workstation_ssh_health_loop()

    assert calls == [{"connect": True, "source": "active_probe"}]


def test_stopped_pipeline_health_is_passive_without_recovery_demand(monkeypatch) -> None:
    monkeypatch.setattr(
        daemon_module,
        "WORKSTATIONS",
        [{
            "id": "WS-C",
            "host": "172.17.135.115",
            "port": 22,
            "reachable_host": "127.0.0.1",
            "reachable_port": 2225,
            "connectivity_mode": "reverse_tunnel",
        }],
    )
    monkeypatch.setattr(daemon_module.time, "time", lambda: 1000.0)
    runner = _Runner({"WS-C": False})
    repairer = _Repairer()
    daemon = PipelineDaemon.__new__(PipelineDaemon)
    daemon.runner = runner
    daemon.state = _State("stopped")
    daemon._last_worker_ssh_checks = {"WS-C": "disconnected"}
    daemon._last_worker_ssh_active_probe_time = 0.0
    daemon._ssh_health_active_probe_interval_seconds = 300.0
    daemon._workstation_ssh_recovery = WorkstationSshRecoveryManager(
        policy=WorkstationSshRecoveryPolicy(enabled=True, failure_threshold=1),
        repairer=repairer,
        clock=lambda: 1000.0,
    )

    result = daemon._run_workstation_ssh_health_check_once()

    assert result["ssh_checks"] == {"WS-C": "disconnected"}
    assert runner.get_ssh_calls == []
    assert repairer.calls == []
    assert "ssh_recovery" not in result


def test_running_and_paused_pipelines_keep_active_ssh_recovery(monkeypatch) -> None:
    monkeypatch.setattr(daemon_module.time, "time", lambda: 1000.0)
    for engine_status in ("running", "paused"):
        calls = []
        daemon = PipelineDaemon.__new__(PipelineDaemon)
        daemon.runner = object()
        daemon.state = _State(engine_status)
        daemon._last_worker_ssh_active_probe_time = 0.0
        daemon._ssh_health_active_probe_interval_seconds = 300.0

        def fake_refresh(*, connect=True, source="active_probe"):
            calls.append({"connect": connect, "source": source})
            return {"ssh_checks": {}, "ssh_targets": {}}

        daemon._refresh_workstation_ssh_checks = fake_refresh

        daemon._run_workstation_ssh_health_check_once()

        assert calls == [{"connect": True, "source": "active_probe"}]


def test_stopped_pipeline_with_remote_tasks_keeps_active_ssh_recovery(monkeypatch) -> None:
    monkeypatch.setattr(daemon_module.time, "time", lambda: 1000.0)
    calls = []
    daemon = PipelineDaemon.__new__(PipelineDaemon)
    daemon.runner = object()
    daemon.state = _State(
        "stopped",
        [{"workstation_id": "WS-C", "config_name": 1, "step_name": "solver"}],
    )
    daemon._last_worker_ssh_active_probe_time = 0.0
    daemon._ssh_health_active_probe_interval_seconds = 300.0

    def fake_refresh(*, connect=True, source="active_probe"):
        calls.append({"connect": connect, "source": source})
        return {"ssh_checks": {}, "ssh_targets": {}}

    daemon._refresh_workstation_ssh_checks = fake_refresh

    daemon._run_workstation_ssh_health_check_once()

    assert calls == [{"connect": True, "source": "active_probe"}]


def test_stopped_pipeline_with_active_remote_status_keeps_recovery(monkeypatch) -> None:
    monkeypatch.setattr(daemon_module.time, "time", lambda: 1000.0)
    calls = []
    daemon = PipelineDaemon.__new__(PipelineDaemon)
    daemon.runner = object()
    daemon.state = _State(
        "stopped",
        statuses={1: {"sw": "Completed", "solver": "UnknownRemote"}},
    )
    daemon._last_worker_ssh_active_probe_time = 0.0
    daemon._ssh_health_active_probe_interval_seconds = 300.0

    def fake_refresh(*, connect=True, source="active_probe"):
        calls.append({"connect": connect, "source": source})
        return {"ssh_checks": {}, "ssh_targets": {}}

    daemon._refresh_workstation_ssh_checks = fake_refresh

    daemon._run_workstation_ssh_health_check_once()

    assert calls == [{"connect": True, "source": "active_probe"}]


def test_recovery_demand_read_failure_fails_open_and_logs_once(monkeypatch, caplog) -> None:
    monkeypatch.setattr(daemon_module.time, "time", lambda: 1000.0)
    calls = []

    class _BrokenState:
        def get_engine_status(self) -> str:
            raise RuntimeError("database unavailable")

    daemon = PipelineDaemon.__new__(PipelineDaemon)
    daemon.runner = object()
    daemon.state = _BrokenState()
    daemon._last_worker_ssh_active_probe_time = 0.0
    daemon._ssh_health_active_probe_interval_seconds = 300.0

    def fake_refresh(*, connect=True, source="active_probe"):
        calls.append({"connect": connect, "source": source})
        return {"ssh_checks": {}, "ssh_targets": {}}

    daemon._refresh_workstation_ssh_checks = fake_refresh

    daemon._run_workstation_ssh_health_check_once()
    daemon._last_worker_ssh_active_probe_time = 0.0
    daemon._run_workstation_ssh_health_check_once()
    daemon.state = _State("stopped")
    daemon._run_workstation_ssh_health_check_once()

    assert calls == [
        {"connect": True, "source": "active_probe"},
        {"connect": True, "source": "active_probe"},
        {"connect": False, "source": "active_probe"},
    ]
    assert caplog.text.count("无法判断工作站 SSH 主动恢复需求") == 1
    assert caplog.text.count("工作站 SSH 主动恢复需求检查已恢复") == 1


def test_forced_probe_bypasses_stopped_pipeline_gate_once(monkeypatch) -> None:
    monkeypatch.setattr(daemon_module.time, "time", lambda: 1000.0)
    calls = []
    daemon = PipelineDaemon.__new__(PipelineDaemon)
    daemon.runner = object()
    daemon.state = _State("stopped")
    daemon._last_worker_ssh_active_probe_time = 1000.0
    daemon._ssh_health_active_probe_interval_seconds = 300.0

    def fake_refresh(*, connect=True, source="active_probe"):
        calls.append({"connect": connect, "source": source})
        return {"ssh_checks": {}, "ssh_targets": {}}

    daemon._refresh_workstation_ssh_checks = fake_refresh

    daemon._request_workstation_ssh_active_probe()
    daemon._run_workstation_ssh_health_check_once()
    daemon._run_workstation_ssh_health_check_once()

    assert calls == [
        {"connect": True, "source": "active_probe"},
        {"connect": False, "source": "active_probe"},
    ]


def test_forced_active_probe_wakes_running_health_monitor(monkeypatch) -> None:
    calls = []
    first_call = threading.Event()
    daemon = PipelineDaemon.__new__(PipelineDaemon)
    daemon.runner = object()
    daemon._ssh_health_stop_event = threading.Event()
    daemon._ssh_health_interval_seconds = 60.0
    daemon._ssh_health_active_probe_interval_seconds = 300.0
    daemon._last_worker_ssh_active_probe_time = 1000.0

    monkeypatch.setattr(daemon_module.time, "time", lambda: 1000.0)

    def fake_refresh(*, connect=True, source="active_probe"):
        calls.append({"connect": connect, "source": source})
        if len(calls) == 1:
            first_call.set()
        if len(calls) == 2:
            daemon._ssh_health_stop_event.set()
            daemon._ensure_ssh_health_wake_event().set()
        return {"ssh_checks": {}, "ssh_targets": {}}

    daemon._refresh_workstation_ssh_checks = fake_refresh
    thread = threading.Thread(target=daemon._workstation_ssh_health_loop)
    thread.start()
    assert first_call.wait(timeout=2)

    daemon._request_workstation_ssh_active_probe()
    thread.join(timeout=2)

    assert not thread.is_alive()
    assert calls == [
        {"connect": False, "source": "active_probe"},
        {"connect": True, "source": "active_probe"},
    ]


def test_subprocess_repairer_uses_bounded_process_and_no_shell(monkeypatch, tmp_path) -> None:
    calls: list[dict] = []

    class _Process:
        returncode = 0

        def communicate(self, timeout=None):
            return '{"ok": true, "results": []}', ""

        def poll(self):
            return 0

    def fake_popen(command, **kwargs):
        calls.append({"command": command, **kwargs})
        return _Process()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    repairer = SubprocessWorkstationTunnelRepairer(
        project_dir=str(tmp_path),
        python_exe=sys.executable,
        jobs=2,
    )

    result = repairer.repair(
        "WS-C",
        {"host": "127.0.0.1", "port": 2225, "connectivity_mode": "reverse_tunnel"},
        timeout_seconds=12.0,
    )

    assert result["ok"] is True
    assert calls[0]["command"] == [
        sys.executable,
        "-m",
        "tools.workstation_tunnel",
        "ensure",
        "--workstation",
        "WS-C",
        "--jobs",
        "2",
        "--progress-jsonl",
        "--project-dir",
        str(tmp_path),
    ]
    assert calls[0]["shell"] is False


def test_subprocess_repairer_timeout_kills_process_tree(monkeypatch, tmp_path) -> None:
    killed = []
    monkeypatch.setattr(recovery_module.os, "name", "posix")

    class _Process:
        pid = 12345

        def __init__(self) -> None:
            self.returncode = None

        def communicate(self, timeout=None):
            if self.returncode is None:
                raise subprocess.TimeoutExpired(cmd=["python"], timeout=timeout or 0)
            return "", ""

        def poll(self):
            return self.returncode

        def kill(self):
            killed.append("kill")
            self.returncode = -9

    def fake_popen(*_args, **_kwargs):
        return _Process()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    repairer = SubprocessWorkstationTunnelRepairer(
        project_dir=str(tmp_path),
        python_exe=sys.executable,
    )

    result = repairer.repair(
        "WS-C",
        {"host": "127.0.0.1", "port": 2225, "connectivity_mode": "reverse_tunnel"},
        timeout_seconds=1.0,
    )

    assert result["ok"] is False
    assert result["status"] == "timeout"
    assert killed == ["kill"]


def test_recovery_shutdown_cancels_inflight_subprocess_repair(monkeypatch, tmp_path) -> None:
    killed = []
    process_started = threading.Event()
    monkeypatch.setattr(recovery_module.os, "name", "posix")

    class _Process:
        pid = 12345

        def __init__(self) -> None:
            self.returncode = None

        def communicate(self, timeout=None):
            process_started.set()
            if self.returncode is None:
                raise subprocess.TimeoutExpired(cmd=["python"], timeout=timeout or 0)
            return "", ""

        def poll(self):
            return self.returncode

        def kill(self):
            killed.append("kill")
            self.returncode = -9

    monkeypatch.setattr(subprocess, "Popen", lambda *_args, **_kwargs: _Process())
    manager = WorkstationSshRecoveryManager(
        policy=WorkstationSshRecoveryPolicy(
            enabled=True,
            failure_threshold=1,
            repair_timeout_seconds=30.0,
        ),
        repairer=SubprocessWorkstationTunnelRepairer(
            project_dir=str(tmp_path),
            python_exe=sys.executable,
        ),
        clock=lambda: 100.0,
    )
    target = {"host": "127.0.0.1", "port": 2225, "connectivity_mode": "reverse_tunnel"}
    thread = threading.Thread(
        target=lambda: manager.record_check("WS-C", "disconnected", target, source="active_probe")
    )
    thread.start()
    assert process_started.wait(timeout=2)

    assert manager.shutdown(timeout=2.0) is True
    thread.join(timeout=2.0)

    assert not thread.is_alive()
    assert killed == ["kill"]


def test_recovery_manager_shutdown_waits_for_inflight_repairs() -> None:
    release = threading.Event()

    class _BlockingRepairer:
        def repair(
            self,
            workstation_id: str,
            target: dict,
            *,
            timeout_seconds: float,
            cancel_event: threading.Event | None = None,
        ) -> dict:
            assert release.wait(timeout=2)
            return {"ok": True, "status": "repaired"}

    manager = WorkstationSshRecoveryManager(
        policy=WorkstationSshRecoveryPolicy(enabled=True, failure_threshold=1),
        repairer=_BlockingRepairer(),
        clock=lambda: 100.0,
    )
    target = {"host": "127.0.0.1", "port": 2225, "connectivity_mode": "reverse_tunnel"}
    thread = threading.Thread(
        target=lambda: manager.record_check("WS-C", "disconnected", target, source="active_probe")
    )
    thread.start()
    release.set()

    manager.shutdown(timeout=2.0)
    thread.join(timeout=2.0)

    assert not thread.is_alive()


def test_daemon_stop_health_monitor_joins_thread_before_recovery_shutdown() -> None:
    events: list[str] = []

    class _Thread:
        def is_alive(self) -> bool:
            return True

        def join(self, timeout: float | None = None) -> None:
            events.append(f"join:{timeout}")

    class _Recovery:
        def shutdown(self) -> bool:
            events.append("shutdown")
            return True

    daemon = PipelineDaemon.__new__(PipelineDaemon)
    daemon._ssh_health_stop_event = threading.Event()
    daemon._ssh_health_wake_event = threading.Event()
    daemon._ssh_health_thread = _Thread()
    daemon._workstation_ssh_recovery = _Recovery()

    daemon._stop_workstation_ssh_health_monitor()

    assert events == ["join:2.0", "shutdown"]
    assert daemon._ssh_health_stop_event.is_set()
    assert daemon._ssh_health_wake_event.is_set()
    assert daemon._ssh_health_thread is None


def test_recovery_manager_shutdown_reports_timeout_for_stuck_repair() -> None:
    release = threading.Event()
    started = threading.Event()

    class _BlockingRepairer:
        def repair(
            self,
            workstation_id: str,
            target: dict,
            *,
            timeout_seconds: float,
            cancel_event: threading.Event | None = None,
        ) -> dict:
            started.set()
            release.wait(timeout=2)
            return {"ok": True, "status": "repaired"}

    manager = WorkstationSshRecoveryManager(
        policy=WorkstationSshRecoveryPolicy(enabled=True, failure_threshold=1),
        repairer=_BlockingRepairer(),
        clock=lambda: 100.0,
    )
    target = {"host": "127.0.0.1", "port": 2225, "connectivity_mode": "reverse_tunnel"}
    thread = threading.Thread(
        target=lambda: manager.record_check("WS-C", "disconnected", target, source="active_probe")
    )
    thread.start()
    assert started.wait(timeout=2)

    assert manager.shutdown(timeout=0.01) is False
    release.set()
    thread.join(timeout=2.0)
