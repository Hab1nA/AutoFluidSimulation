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


def test_handle_get_dashboard_combines_status_engine_and_logs(monkeypatch):
    handler = _LogHandler()
    monkeypatch.setattr(daemon_module, "get_broadcast_handler", lambda: handler)
    daemon = PipelineDaemon.__new__(PipelineDaemon)
    daemon.state = _State()
    daemon._pipeline_ever_started = True

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
