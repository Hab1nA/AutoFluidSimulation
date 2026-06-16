import json

from engine import daemon as daemon_module
from engine.daemon import PipelineDaemon


class _State:
    def get_all_statuses(self):
        return {"1": {"sw": "Completed"}}

    def get_engine_status(self):
        return "running"

    def is_sw_macro_started(self):
        return True

    def is_global_barrier_met(self):
        return False


class _LogHandler:
    def __init__(self):
        self.calls = []

    def get_entries(self, **kwargs):
        self.calls.append(kwargs)
        return {
            "entries": [],
            "latest_id": 12,
            "total": 0,
            "has_gap": False,
            "reset": False,
        }


class _LargeLogHandler:
    def get_entries(self, **_kwargs):
        return {
            "entries": [
                {
                    "id": idx,
                    "timestamp": "2026-06-16 22:00:00",
                    "level": "INFO",
                    "source": "remote_ps",
                    "logger_name": "executor.remote_executor",
                    "message": "x" * 250_000,
                    "raw_message": "x" * 250_000,
                }
                for idx in range(1, 8)
            ],
            "latest_id": 7,
            "total": 7,
            "has_gap": False,
            "reset": False,
        }


class _Ssh:
    def __init__(self, connected: bool) -> None:
        self._connected = connected

    def is_connected(self) -> bool:
        return self._connected


class _TransportOnlySsh:
    def __init__(self, active: bool) -> None:
        self._active = active
        self.host = "127.0.0.1"
        self.port = 2222

    def is_connected(self) -> bool:
        raise AssertionError("dashboard health must not send SSH heartbeat probes")

    def connection_is_active(self) -> bool:
        return self._active


class _Runner:
    def __init__(self, ssh_pool):
        self._ssh_pool = ssh_pool

    def get_ssh(self, workstation_id="default"):
        raise AssertionError("dashboard health must not create SSH connections")


def test_handle_get_dashboard_combines_status_engine_and_logs(monkeypatch):
    handler = _LogHandler()
    monkeypatch.setattr(daemon_module, "get_broadcast_handler", lambda: handler)
    monkeypatch.setattr(
        daemon_module,
        "WORKSTATIONS",
        [{"id": "WS-A"}, {"id": "WS-B"}],
    )
    daemon = PipelineDaemon.__new__(PipelineDaemon)
    daemon.state = _State()
    daemon._pipeline_ever_started = True
    daemon._started_at_epoch = 1_000.0
    daemon._config_warnings = ["工作站 WS-B 缺少 reachable 配置"]
    daemon.local_worker_registry = None
    daemon.runner = _Runner({"WS-A": _Ssh(True), "WS-B": _Ssh(False)})
    monkeypatch.setattr(daemon_module.time, "time", lambda: 1_065.7)
    monkeypatch.setattr(
        daemon_module.time,
        "strftime",
        lambda fmt, value: "1970-01-01 00:16:40",
    )
    monkeypatch.setattr(daemon_module.time, "localtime", lambda value: value)

    ok, data, message = daemon.handle_get_dashboard({
        "since_log_id": 7,
        "log_limit": 25,
    })

    assert ok is True
    assert message == ""
    assert data["statuses"] == {"1": {"sw": "Completed"}}
    assert data["engine"] == {
        "engine_status": "running",
        "sw_macro_started": True,
        "barrier_passed": False,
        "pipeline_started": True,
        "daemon_started_at": 1_000.0,
        "daemon_started_at_display": "1970-01-01 00:16:40",
        "daemon_uptime_seconds": 65,
    }
    assert data["health"] == {
        "local_worker_online": False,
        "server_to_local_ssh": "unknown",
        "server_to_workstation_ssh": "ok",
        "workstation_ssh_details": {
            "WS-A": "ok",
            "WS-B": "disconnected",
        },
        "workstation_ssh_targets": {
            "WS-A": {
                "host": "",
                "port": 22,
                "connectivity_mode": "direct",
            },
            "WS-B": {
                "host": "",
                "port": 22,
                "connectivity_mode": "direct",
            },
        },
        "config_warnings": ["工作站 WS-B 缺少 reachable 配置"],
    }
    assert data["logs"]["latest_id"] == 12
    assert handler.calls == [{
        "since_id": 7,
        "limit": 25,
        "level_filter": None,
        "source_filter": None,
        "include_polling": False,
        "include_lifecycle": False,
        "include_config_scoped": False,
    }]


def test_dashboard_trims_large_log_payload(monkeypatch):
    monkeypatch.setattr(daemon_module, "get_broadcast_handler", lambda: _LargeLogHandler())
    monkeypatch.setattr(daemon_module, "WORKSTATIONS", [])
    monkeypatch.setattr(daemon_module, "_MAX_DASHBOARD_LOG_BYTES", 900_000)
    daemon = PipelineDaemon.__new__(PipelineDaemon)
    daemon.state = _State()
    daemon._pipeline_ever_started = True
    daemon._started_at_epoch = 1_000.0
    daemon._config_warnings = []
    daemon.local_worker_registry = None
    daemon.runner = None
    monkeypatch.setattr(daemon_module.time, "time", lambda: 1_001.0)
    monkeypatch.setattr(daemon_module.time, "strftime", lambda _fmt, _value: "now")
    monkeypatch.setattr(daemon_module.time, "localtime", lambda value: value)

    ok, data, _ = daemon.handle_get_dashboard({"since_log_id": 0, "log_limit": 7})

    assert ok is True
    encoded = json.dumps(data, ensure_ascii=False).encode("utf-8")
    assert len(encoded) <= daemon_module._MAX_DASHBOARD_LOG_BYTES
    assert data["logs"]["latest_id"] == 7
    assert data["logs"]["total"] == 7
    assert data["logs"]["truncated"] is True
    assert data["logs"]["entries"]
    assert data["logs"]["entries"][-1]["id"] == 7


def test_dashboard_works_before_server_mode_configs_are_loaded(monkeypatch):
    handler = _LogHandler()
    monkeypatch.setattr(daemon_module, "get_broadcast_handler", lambda: handler)
    monkeypatch.setattr(daemon_module, "WORKSTATIONS", [{"id": "default"}])
    daemon = PipelineDaemon.__new__(PipelineDaemon)
    daemon.state = None
    daemon.runner = None
    daemon.local_worker_registry = None
    daemon._pipeline_ever_started = False
    daemon._started_at_epoch = 2_000.0
    daemon._config_load_error = "ServerMode 等待 LocalWorker 提供构型数据"
    daemon._config_warnings = []
    monkeypatch.setattr(daemon_module.time, "time", lambda: 2_030.0)
    monkeypatch.setattr(
        daemon_module.time,
        "strftime",
        lambda fmt, value: "1970-01-01 00:33:20",
    )
    monkeypatch.setattr(daemon_module.time, "localtime", lambda value: value)

    ok, data, message = daemon.handle_get_dashboard({
        "since_log_id": 3,
        "log_limit": 10,
    })

    assert ok is True
    assert message == ""
    assert data["statuses"] == {}
    assert data["engine"] == {
        "engine_status": "stopped",
        "sw_macro_started": False,
        "barrier_passed": False,
        "pipeline_started": False,
        "daemon_started_at": 2_000.0,
        "daemon_started_at_display": "1970-01-01 00:33:20",
        "daemon_uptime_seconds": 30,
        "config_load_error": "ServerMode 等待 LocalWorker 提供构型数据",
    }
    assert data["health"]["local_worker_online"] is False
    assert data["health"]["server_to_workstation_ssh"] == "unknown"
    assert data["logs"]["latest_id"] == 12
    assert handler.calls == [{
        "since_id": 3,
        "limit": 10,
        "level_filter": None,
        "source_filter": None,
        "include_polling": False,
        "include_lifecycle": False,
        "include_config_scoped": False,
    }]


def test_dashboard_health_reports_server_to_local_from_online_worker(monkeypatch):
    from engine.local_worker_registry import LocalWorkerRegistry

    monkeypatch.setattr(
        daemon_module,
        "WORKSTATIONS",
        [{"id": "default"}],
    )
    registry = LocalWorkerRegistry(timeout_seconds=90.0, clock=lambda: 100.0)
    registry.register(
        "local-pc-01",
        {"sw": True},
        network={
            "reachable_host": "127.0.0.1",
            "ssh_port": 2222,
            "connectivity_mode": "reverse_tunnel",
        },
    )
    daemon = PipelineDaemon.__new__(PipelineDaemon)
    daemon.local_worker_registry = registry
    daemon.runner = _Runner({})
    daemon._config_warnings = []

    health = daemon._build_health_snapshot()

    assert health["local_worker_online"] is True
    assert health["server_to_local_ssh"] == "ok"
    assert health["server_to_workstation_ssh"] == "unknown"
    assert health["workstation_ssh_details"] == {"default": "unknown"}


def test_dashboard_health_reports_server_to_local_disconnected_without_reachable_metadata(
    monkeypatch,
):
    from engine.local_worker_registry import LocalWorkerRegistry

    monkeypatch.setattr(
        daemon_module,
        "WORKSTATIONS",
        [{"id": "default"}],
    )
    registry = LocalWorkerRegistry(timeout_seconds=90.0, clock=lambda: 100.0)
    registry.register(
        "local-pc-01",
        {"sw": True},
        network={"ssh_port": 22},
    )
    daemon = PipelineDaemon.__new__(PipelineDaemon)
    daemon.local_worker_registry = registry
    daemon.runner = _Runner({})
    daemon._config_warnings = []

    health = daemon._build_health_snapshot()

    assert health["local_worker_online"] is True
    assert health["server_to_local_ssh"] == "disconnected"


def test_dashboard_health_reads_workstation_ssh_without_heartbeat(monkeypatch):
    monkeypatch.setattr(
        daemon_module,
        "WORKSTATIONS",
        [{
            "id": "WS-A",
            "host": "172.17.135.240",
            "port": 22,
            "reachable_host": "127.0.0.1",
            "reachable_port": 2222,
            "connectivity_mode": "reverse_tunnel",
        }],
    )
    daemon = PipelineDaemon.__new__(PipelineDaemon)
    daemon.local_worker_registry = None
    daemon.runner = _Runner({"WS-A": _TransportOnlySsh(True)})
    daemon._config_warnings = []

    health = daemon._build_health_snapshot()

    assert health["server_to_workstation_ssh"] == "ok"
    assert health["workstation_ssh_details"] == {"WS-A": "ok"}
    assert health["workstation_ssh_targets"] == {
        "WS-A": {
            "host": "127.0.0.1",
            "port": 2222,
            "connectivity_mode": "reverse_tunnel",
        },
    }


def test_dashboard_health_reports_last_failed_workstation_target(monkeypatch):
    monkeypatch.setattr(
        daemon_module,
        "WORKSTATIONS",
        [{
            "id": "WS-A",
            "host": "172.17.135.240",
            "port": 22,
        }],
    )
    daemon = PipelineDaemon.__new__(PipelineDaemon)
    daemon.local_worker_registry = None
    daemon.runner = _Runner({})
    daemon._last_worker_ssh_checks = {"WS-A": "error: timed out"}
    daemon._config_warnings = ["工作站 WS-A 在 server 模式下缺少 reachable_host"]

    health = daemon._build_health_snapshot()

    assert health["workstation_ssh_details"] == {"WS-A": "error: timed out"}
    assert health["workstation_ssh_targets"] == {
        "WS-A": {
            "host": "172.17.135.240",
            "port": 22,
            "connectivity_mode": "direct",
        },
    }
    assert health["config_warnings"] == ["工作站 WS-A 在 server 模式下缺少 reachable_host"]
