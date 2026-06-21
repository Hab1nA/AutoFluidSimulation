import json
import subprocess
from types import SimpleNamespace

from tools import autofluid_cli


class _FakeClient:
    def __init__(self, responses=None):
        self.responses = list(responses or [])
        self.calls = []

    def request(self, command, params=None, timeout=None):
        self.calls.append((command, params or {}, timeout))
        if self.responses:
            return self.responses.pop(0)
        return {"status": "ok", "data": {"command": command}, "message": "ok"}


def _run(argv, client=None, runner=None, webhook=None):
    result = autofluid_cli.run_cli(
        argv,
        client_factory=lambda _settings: client or _FakeClient(),
        command_runner=runner,
        webhook_sender=webhook,
    )
    return result, json.loads(result.stdout)


def test_start_outputs_json_and_sends_ipc_start():
    client = _FakeClient()

    result, payload = _run(["start"], client=client)

    assert result.exit_code == 0
    assert payload["ok"] is True
    assert payload["command"] == "start"
    assert payload["data"] == {"command": "start"}
    assert client.calls == [("start", {}, None)]


def test_check_uses_longer_timeout():
    client = _FakeClient()

    result, payload = _run(["check"], client=client)

    assert result.exit_code == 0
    assert payload["ok"] is True
    assert client.calls == [("check", {}, autofluid_cli.CHECK_TIMEOUT_SECONDS)]


def test_clean_rejects_localworker_owned_steps_before_ipc():
    client = _FakeClient()

    result, payload = _run(["clean", "sw"], client=client)

    assert result.exit_code == 2
    assert payload["ok"] is False
    assert "LocalWorker" in payload["message"]
    assert client.calls == []


def test_reset_rejects_implicit_all_steps_before_ipc():
    client = _FakeClient()

    result, payload = _run(["reset", "3"], client=client)

    assert result.exit_code == 2
    assert payload["ok"] is False
    assert "LocalWorker" in payload["message"]
    assert client.calls == []


def test_clean_remote_step_sends_clean_step():
    client = _FakeClient()

    result, payload = _run(["clean", "meshing", "--config", "7"], client=client)

    assert result.exit_code == 0
    assert payload["ok"] is True
    assert client.calls == [("clean_step", {"step_name": "meshing", "config_name": 7}, None)]


def test_reset_remote_step_sends_reset_step():
    client = _FakeClient()

    result, payload = _run(["reset", "all", "solver"], client=client)

    assert result.exit_code == 0
    assert payload["ok"] is True
    assert client.calls == [("reset_step", {"config_name": "all", "step_name": "solver"}, None)]


def test_worker_restart_sends_worker_restart():
    client = _FakeClient()

    result, payload = _run(["worker", "restart"], client=client)

    assert result.exit_code == 0
    assert payload["ok"] is True
    assert client.calls == [("worker_restart", {}, autofluid_cli.WORKER_TIMEOUT_SECONDS)]


def test_worker_start_uses_longer_timeout():
    client = _FakeClient()

    result, payload = _run(["worker", "start"], client=client)

    assert result.exit_code == 0
    assert payload["ok"] is True
    assert client.calls == [("worker_start", {}, autofluid_cli.WORKER_TIMEOUT_SECONDS)]


def test_worker_stop_cleans_local_watchdogs_and_pid_processes(monkeypatch):
    client = _FakeClient([
        {
            "status": "ok",
            "data": {"remote": "stopped"},
            "message": "stopped",
        }
    ])

    monkeypatch.setattr(
        autofluid_cli.process_utils,
        "cleanup_tunnel_watchdog_tasks",
        lambda: {"Workstation": {"status": "uninstalled"}},
    )
    monkeypatch.setattr(
        autofluid_cli.process_utils,
        "cleanup_worker_processes_from_pid_files",
        lambda: {"tunnel_workstation": {"pid": 1234, "status": "terminated"}},
    )

    result, payload = _run(["worker", "stop"], client=client)

    assert result.exit_code == 0
    assert payload["ok"] is True
    assert client.calls == [("worker_stop", {}, autofluid_cli.WORKER_TIMEOUT_SECONDS)]
    assert payload["data"]["remote"] == "stopped"
    assert payload["data"]["local_watchdog_cleanup"] == {
        "Workstation": {"status": "uninstalled"},
    }
    assert payload["data"]["local_process_cleanup"] == {
        "tunnel_workstation": {"pid": 1234, "status": "terminated"},
    }


def test_worker_stop_still_cleans_local_resources_when_ipc_is_down(monkeypatch):
    class _FailingClient:
        def request(self, *_args, **_kwargs):
            raise OSError("ipc unavailable")

    monkeypatch.setattr(
        autofluid_cli.process_utils,
        "cleanup_tunnel_watchdog_tasks",
        lambda: {"LocalWorker": {"status": "uninstalled"}},
    )
    monkeypatch.setattr(
        autofluid_cli.process_utils,
        "cleanup_worker_processes_from_pid_files",
        lambda: {"tunnel_localworker": {"pid": 5678, "status": "terminated"}},
    )

    result, payload = _run(["worker", "stop"], client=_FailingClient())

    assert result.exit_code == 1
    assert payload["ok"] is False
    assert payload["command"] == "worker stop"
    assert payload["message"] == "ipc unavailable"
    assert payload["data"]["local_watchdog_cleanup"] == {
        "LocalWorker": {"status": "uninstalled"},
    }
    assert payload["data"]["local_process_cleanup"] == {
        "tunnel_localworker": {"pid": 5678, "status": "terminated"},
    }


def test_daemon_systemctl_actions_use_expected_arguments(monkeypatch):
    monkeypatch.delenv("AUTOFLUID_DAEMON_SERVICE", raising=False)
    calls = []

    def runner(args):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, stdout="done\n", stderr="")

    for action in ["start", "stop", "restart", "status"]:
        result, payload = _run(["daemon", action], runner=runner)
        assert result.exit_code == 0
        assert payload["ok"] is True

    assert calls == [
        ["systemctl", "start", "autofluid-daemon"],
        ["systemctl", "stop", "autofluid-daemon"],
        ["systemctl", "restart", "autofluid-daemon"],
        ["systemctl", "status", "autofluid-daemon", "--no-pager"],
    ]


def test_daemon_systemctl_uses_custom_service_name(monkeypatch):
    monkeypatch.setenv("AUTOFLUID_DAEMON_SERVICE", "autofluid-daemon-prod")
    calls = []

    def runner(args):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, stdout="done\n", stderr="")

    result, payload = _run(["daemon", "stop"], runner=runner)

    assert result.exit_code == 0
    assert payload["ok"] is True
    assert calls == [["systemctl", "stop", "autofluid-daemon-prod"]]


def test_alert_watcher_posts_warning_once_per_cooldown(monkeypatch):
    sent = []
    client = _FakeClient([
        {
            "status": "ok",
            "data": {
                "entries": [
                    {
                        "id": 10,
                        "timestamp": "2026-06-12 10:00:00",
                        "level": "WARNING",
                        "source": "scheduler",
                        "raw_message": "[engine.scheduler] warn",
                        "message": "formatted warn",
                    }
                ],
                "latest_id": 10,
            },
            "message": "",
        },
        {
            "status": "ok",
            "data": {
                "entries": [
                    {
                        "id": 11,
                        "timestamp": "2026-06-12 10:00:01",
                        "level": "WARNING",
                        "source": "scheduler",
                        "raw_message": "[engine.scheduler] warn",
                        "message": "formatted warn",
                    }
                ],
                "latest_id": 11,
            },
            "message": "",
        },
    ])
    monkeypatch.setenv("AUTOFLUID_OPENCLAW_WEBHOOK_URL", "http://openclaw/hook")

    result = autofluid_cli.run_cli(
        ["alerts", "watch", "--once", "--once"],
        client_factory=lambda _settings: client,
        webhook_sender=lambda url, token, payload, timeout: sent.append((url, token, payload, timeout)),
        sleep=lambda _seconds: None,
        now=lambda: 1000.0,
    )
    payload = json.loads(result.stdout)

    assert result.exit_code == 0
    assert payload["ok"] is True
    assert payload["data"]["sent"] == 1
    assert len(sent) == 1
    assert sent[0][0] == "http://openclaw/hook"
    assert sent[0][2]["level"] == "WARNING"
    assert sent[0][2]["fingerprint"]
    assert client.calls == [
        ("get_log_entries", {"since_id": 0, "limit": 50, "level_filter": "WARNING"}, None),
        ("get_log_entries", {"since_id": 10, "limit": 50, "level_filter": "WARNING"}, None),
    ]


def test_ipc_client_reads_env_and_auth(monkeypatch):
    sent = []

    class _Socket:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def settimeout(self, timeout):
            self.timeout = timeout

        def connect(self, addr):
            self.addr = addr

        def sendall(self, data):
            sent.append(json.loads(data.decode("utf-8")))

        def makefile(self, _mode, encoding=None):
            return SimpleNamespace(readline=lambda: json.dumps({
                "status": "ok",
                "data": {},
                "message": "ok",
                "request_id": sent[-1]["request_id"],
            }) + "\n")

    monkeypatch.setenv("AUTOFLUID_IPC_HOST", "ocar")
    monkeypatch.setenv("AUTOFLUID_IPC_PORT", "19527")
    monkeypatch.setenv("AUTOFLUID_IPC_AUTH_TOKEN", "secret")
    monkeypatch.setattr(autofluid_cli.socket, "create_connection", lambda addr, timeout: _Socket())

    client = autofluid_cli.IpcClient(autofluid_cli.CliSettings.from_env())
    response = client.request("status")

    assert response["status"] == "ok"
    assert sent[0]["auth_token"] == "secret"
    assert sent[0]["command"] == "status"


def test_invalid_args_return_json_error():
    result = autofluid_cli.run_cli(["worker", "delete"])
    payload = json.loads(result.stdout)

    assert result.exit_code == 2
    assert payload["ok"] is False
    assert payload["command"] == "parse"
    assert "invalid choice" in payload["message"]
