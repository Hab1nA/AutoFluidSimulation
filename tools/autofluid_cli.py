from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, NoReturn

from ipc.protocol import (
    CMD_CHECK,
    CMD_CLEAN_STEP,
    CMD_GET_DASHBOARD,
    CMD_GET_LOG_ENTRIES,
    CMD_PAUSE,
    CMD_RESET_STEP,
    CMD_START,
    CMD_STOP_STEP,
    CMD_WORKER_RESTART,
    CMD_WORKER_START,
    CMD_WORKER_STOP,
    create_request,
    serialize,
)
from tools import workstation_tunnel
from utils import process_utils

DEFAULT_IPC_HOST = "127.0.0.1"
DEFAULT_IPC_PORT = 9527
DEFAULT_TIMEOUT_SECONDS = 5.0
CHECK_TIMEOUT_SECONDS = 60.0
WORKER_TIMEOUT_SECONDS = 60.0
DEFAULT_ALERT_LIMIT = 50
DEFAULT_ALERT_INTERVAL_SECONDS = 5.0
DEFAULT_ALERT_COOLDOWN_SECONDS = 600.0
ALERT_FINGERPRINT_PRUNE_INTERVAL = 20
ALERT_FINGERPRINT_PRUNE_THRESHOLD = 1000
REMOTE_CLEAN_STEPS = {"transfer", "meshing", "solver", "solverdata", "postprocess", "cache"}
REMOTE_RESET_STEPS = {"transfer", "meshing", "solver", "solverdata", "postprocess"}
LOCALWORKER_OWNED_STEPS = {"all", "sw", "sc"}


class CliParseError(ValueError):
    pass


class JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise CliParseError(message)

    def exit(self, status: int = 0, message: str | None = None) -> NoReturn:
        if status:
            raise CliParseError(message or f"argument parsing failed with status {status}")
        raise CliParseError(message or "argument parsing exited")


@dataclass(frozen=True)
class CliSettings:
    host: str
    port: int
    auth_token: str
    daemon_service: str
    webhook_url: str
    webhook_token: str

    @classmethod
    def from_env(cls) -> "CliSettings":
        return cls(
            host=os.environ.get("AUTOFLUID_IPC_HOST", DEFAULT_IPC_HOST),
            port=int(os.environ.get("AUTOFLUID_IPC_PORT", str(DEFAULT_IPC_PORT))),
            auth_token=os.environ.get("AUTOFLUID_IPC_AUTH_TOKEN", ""),
            daemon_service=os.environ.get("AUTOFLUID_DAEMON_SERVICE", "autofluid-daemon"),
            webhook_url=os.environ.get("AUTOFLUID_OPENCLAW_WEBHOOK_URL", ""),
            webhook_token=os.environ.get("AUTOFLUID_OPENCLAW_WEBHOOK_TOKEN", ""),
        )


@dataclass(frozen=True)
class CliResult:
    exit_code: int
    stdout: str


class IpcClient:
    def __init__(self, settings: CliSettings):
        self._settings = settings

    def request(
        self,
        command: str,
        params: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        request = create_request(
            command,
            params or {},
            auth_token=self._settings.auth_token or None,
        )
        effective_timeout = timeout or DEFAULT_TIMEOUT_SECONDS
        with socket.create_connection(
            (self._settings.host, self._settings.port),
            timeout=effective_timeout,
        ) as sock:
            sock.settimeout(effective_timeout)
            sock.sendall(serialize(request))
            line = sock.makefile("r", encoding="utf-8").readline()
        if not line:
            raise RuntimeError("IPC server closed without a response")
        response = json.loads(line)
        if not isinstance(response, dict):
            raise RuntimeError("IPC response is not a JSON object")
        if response.get("request_id") != request["request_id"]:
            raise RuntimeError("IPC response request_id mismatch")
        return response


def _json_result(
    *,
    ok: bool,
    command: str,
    message: str = "",
    data: Any = None,
    exit_code: int = 0,
) -> CliResult:
    payload = {
        "ok": ok,
        "command": command,
        "message": message,
        "data": data,
        "exit_code": exit_code,
    }
    return CliResult(exit_code=exit_code, stdout=json.dumps(payload, ensure_ascii=False) + "\n")


def _ipc_result(command: str, response: dict[str, Any]) -> CliResult:
    ok = response.get("status") == "ok"
    exit_code = 0 if ok else 1
    return _json_result(
        ok=ok,
        command=command,
        message=str(response.get("message") or ""),
        data=response.get("data"),
        exit_code=exit_code,
    )


def _add_local_worker_stop_cleanup(response: dict[str, Any]) -> dict[str, Any]:
    data = response.get("data")
    if not isinstance(data, dict):
        data = {"remote_data": data}
        response["data"] = data

    try:
        data["local_watchdog_cleanup"] = process_utils.cleanup_tunnel_watchdog_tasks()
    except Exception as exc:
        data["local_watchdog_cleanup"] = {"status": "failed", "error": str(exc)}

    try:
        data["local_process_cleanup"] = process_utils.cleanup_worker_processes_from_pid_files()
    except Exception as exc:
        data["local_process_cleanup"] = {"status": "failed", "error": str(exc)}

    return response


def _safe_int_or_all(value: str) -> int | str:
    if value == "all":
        return value
    return int(value)


def _localworker_guard_message(action: str) -> str:
    return (
        f"server CLI 拒绝执行会影响 LocalWorker 所属 SW/SC 信息的 {action}；"
        "请在本地 TUI/LocalWorker 控制面处理 sw/sc/all。"
    )


def _run_systemctl(
    action: str,
    settings: CliSettings,
    command_runner: Callable[[list[str]], subprocess.CompletedProcess[str]] | None,
) -> CliResult:
    args = ["systemctl", action, settings.daemon_service]
    if action == "status":
        args.append("--no-pager")
    runner = command_runner or (
        lambda command: subprocess.run(
            command,
            check=False,
            text=True,
            capture_output=True,
        )
    )
    completed = runner(args)
    ok = completed.returncode == 0
    return _json_result(
        ok=ok,
        command=f"daemon {action}",
        message=(completed.stdout or completed.stderr).strip(),
        data={
            "args": args,
            "returncode": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
        },
        exit_code=0 if ok else completed.returncode or 1,
    )


def _send_webhook(
    url: str,
    token: str,
    payload: dict[str, Any],
    timeout: float,
) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        response.read()


def _alert_fingerprint(entry: dict[str, Any]) -> str:
    text = "|".join([
        str(entry.get("level") or ""),
        str(entry.get("source") or ""),
        str(entry.get("raw_message") or entry.get("message") or ""),
    ])
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _alert_payload(entry: dict[str, Any], fingerprint: str) -> dict[str, Any]:
    level = str(entry.get("level") or "WARNING")
    source = str(entry.get("source") or "system")
    raw_message = str(entry.get("raw_message") or entry.get("message") or "")
    return {
        "title": f"AutoFluid {level}",
        "level": level,
        "message": raw_message,
        "source": source,
        "timestamp": entry.get("timestamp"),
        "log_id": entry.get("id"),
        "fingerprint": fingerprint,
        "name": "AutoFluid Alert",
        "deliver": True,
        "channel": "qqbot",
        "to": "qqbot:c2c:95D5600461EFF47937CB8CF0C3E2AB2D",
    }


def _prune_expired_fingerprints(
    seen_until: dict[str, float],
    current_time: float,
) -> None:
    """Remove expired alert cooldown fingerprints in-place."""
    expired = [
        fingerprint
        for fingerprint, expires_at in seen_until.items()
        if expires_at <= current_time
    ]
    for fingerprint in expired:
        seen_until.pop(fingerprint, None)


def _watch_alerts(
    args: argparse.Namespace,
    client: IpcClient,
    settings: CliSettings,
    webhook_sender: Callable[[str, str, dict[str, Any], float], Any],
    sleep: Callable[[float], Any],
    now: Callable[[], float],
) -> CliResult:
    if not settings.webhook_url:
        return _json_result(
            ok=False,
            command="alerts watch",
            message="缺少 AUTOFLUID_OPENCLAW_WEBHOOK_URL",
            exit_code=2,
        )

    iterations = args.once or None
    since_id = 0
    sent = 0
    skipped = 0
    failures = 0
    seen_until: dict[str, float] = {}
    loops = 0

    while True:
        response = client.request(
            CMD_GET_LOG_ENTRIES,
            {
                "since_id": since_id,
                "limit": args.limit,
                "level_filter": "WARNING",
            },
        )
        if response.get("status") != "ok":
            return _ipc_result("alerts watch", response)
        data = response.get("data") or {}
        entries = data.get("entries") or []
        latest_id = data.get("latest_id")
        if isinstance(latest_id, int):
            since_id = latest_id
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            fingerprint = _alert_fingerprint(entry)
            current_time = now()
            if seen_until.get(fingerprint, 0.0) > current_time:
                skipped += 1
                continue
            try:
                webhook_sender(
                    settings.webhook_url,
                    settings.webhook_token,
                    _alert_payload(entry, fingerprint),
                    args.webhook_timeout,
                )
            except (OSError, urllib.error.URLError, TimeoutError) as exc:
                failures += 1
                if args.fail_fast:
                    return _json_result(
                        ok=False,
                        command="alerts watch",
                        message=f"OpenClaw webhook 发送失败: {exc}",
                        data={"sent": sent, "skipped": skipped, "failures": failures},
                        exit_code=1,
                    )
            else:
                sent += 1
                seen_until[fingerprint] = current_time + args.cooldown

        loops += 1
        if (
            loops % ALERT_FINGERPRINT_PRUNE_INTERVAL == 0
            or len(seen_until) > ALERT_FINGERPRINT_PRUNE_THRESHOLD
        ):
            _prune_expired_fingerprints(seen_until, now())
        if iterations is not None and loops >= iterations:
            break
        sleep(args.interval)

    return _json_result(
        ok=failures == 0,
        command="alerts watch",
        message="告警监听完成" if iterations is not None else "",
        data={"sent": sent, "skipped": skipped, "failures": failures, "latest_id": since_id},
        exit_code=0 if failures == 0 else 1,
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = JsonArgumentParser(prog="autofluid-cli")
    sub = parser.add_subparsers(dest="command", required=True, parser_class=JsonArgumentParser)

    sub.add_parser("start")
    sub.add_parser("pause")
    sub.add_parser("check")
    sub.add_parser("status")

    clean = sub.add_parser("clean")
    clean.add_argument("step")
    clean.add_argument("--config", dest="config_name")

    reset = sub.add_parser("reset")
    reset.add_argument("config_name")
    reset.add_argument("step", nargs="?")

    stop_step = sub.add_parser("stop-step")
    stop_step.add_argument("config_name")
    stop_step.add_argument("step")
    stop_step.add_argument("--reason", default="用户请求停止远程任务")

    daemon = sub.add_parser("daemon")
    daemon.add_argument("action", choices=["start", "stop", "restart", "status"])

    worker = sub.add_parser("worker")
    worker.add_argument("action", choices=["start", "stop", "restart", "status"])

    workstation_tunnel_parser = sub.add_parser("workstation-tunnel")
    workstation_tunnel_parser.add_argument("action", choices=["ensure", "repair", "status", "uninstall"])
    workstation_tunnel_parser.add_argument("--all", action="store_true")
    workstation_tunnel_parser.add_argument("--install-dir", default=workstation_tunnel.DEFAULT_INSTALL_DIR)
    workstation_tunnel_parser.add_argument("--tunnel-target", default=None)
    workstation_tunnel_parser.add_argument("--workstation", default=None)
    workstation_tunnel_parser.add_argument("--jobs", type=int, default=4)
    workstation_tunnel_parser.add_argument("--progress-jsonl", action="store_true")

    alerts = sub.add_parser("alerts")
    alerts_sub = alerts.add_subparsers(
        dest="alerts_command",
        required=True,
        parser_class=JsonArgumentParser,
    )
    watch = alerts_sub.add_parser("watch")
    watch.add_argument("--once", action="count", default=0)
    watch.add_argument("--interval", type=float, default=DEFAULT_ALERT_INTERVAL_SECONDS)
    watch.add_argument("--limit", type=int, default=DEFAULT_ALERT_LIMIT)
    watch.add_argument("--cooldown", type=float, default=DEFAULT_ALERT_COOLDOWN_SECONDS)
    watch.add_argument("--webhook-timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    watch.add_argument("--fail-fast", action="store_true")
    return parser


def run_cli(
    argv: list[str] | None = None,
    *,
    client_factory: Callable[[CliSettings], IpcClient] | None = None,
    command_runner: Callable[[list[str]], subprocess.CompletedProcess[str]] | None = None,
    webhook_sender: Callable[[str, str, dict[str, Any], float], Any] | None = None,
    sleep: Callable[[float], Any] = time.sleep,
    now: Callable[[], float] = time.monotonic,
) -> CliResult:
    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
    except CliParseError as exc:
        return _json_result(ok=False, command="parse", message=str(exc).strip(), exit_code=2)
    if args.command == "workstation-tunnel":
        try:
            spec_kwargs = {"tunnel_target": args.tunnel_target}
            if args.workstation:
                spec_kwargs["workstation_id"] = args.workstation
            specs = workstation_tunnel.configured_workstation_specs(**spec_kwargs)
            progress = (
                workstation_tunnel.jsonl_progress_writer()
                if args.progress_jsonl
                else None
            )
            results = workstation_tunnel.run_for_specs(
                args.action,
                specs,
                install_dir=args.install_dir,
                jobs=args.jobs,
                progress=progress,
            )
            ok = workstation_tunnel.all_results_ok(results)
            return _json_result(
                ok=ok,
                command=f"workstation-tunnel {args.action}",
                message="ok" if ok else "工作站隧道未就绪",
                data={"results": results},
                exit_code=0 if ok else 1,
            )
        except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
            return _json_result(
                ok=False,
                command=f"workstation-tunnel {getattr(args, 'action', '')}".strip(),
                message=str(exc),
                exit_code=1,
            )
    settings = CliSettings.from_env()
    client = (client_factory or IpcClient)(settings)

    try:
        if args.command == "start":
            return _ipc_result("start", client.request(CMD_START))
        if args.command == "pause":
            return _ipc_result("pause", client.request(CMD_PAUSE))
        if args.command == "check":
            return _ipc_result("check", client.request(CMD_CHECK, timeout=CHECK_TIMEOUT_SECONDS))
        if args.command == "status":
            return _ipc_result("status", client.request(CMD_GET_DASHBOARD, {"since_log_id": 0, "log_limit": 0}))
        if args.command == "daemon":
            return _run_systemctl(args.action, settings, command_runner)
        if args.command == "worker":
            if args.action == "status":
                return _ipc_result("worker status", client.request(CMD_GET_DASHBOARD, {"since_log_id": 0, "log_limit": 0}))
            worker_commands = {
                "start": CMD_WORKER_START,
                "stop": CMD_WORKER_STOP,
                "restart": CMD_WORKER_RESTART,
            }
            command = worker_commands[args.action]
            response = client.request(command, timeout=WORKER_TIMEOUT_SECONDS)
            if args.action == "stop":
                response = _add_local_worker_stop_cleanup(response)
            return _ipc_result(f"worker {args.action}", response)
        if args.command == "clean":
            step = str(args.step).lower()
            if step in LOCALWORKER_OWNED_STEPS:
                return _json_result(
                    ok=False,
                    command="clean",
                    message=_localworker_guard_message("clean"),
                    exit_code=2,
                )
            if step not in REMOTE_CLEAN_STEPS:
                return _json_result(ok=False, command="clean", message=f"无效步骤名: {step}", exit_code=2)
            params: dict[str, Any] = {"step_name": step}
            if args.config_name is not None:
                params["config_name"] = _safe_int_or_all(args.config_name)
            return _ipc_result("clean", client.request(CMD_CLEAN_STEP, params))
        if args.command == "reset":
            step = str(args.step or "all").lower()
            if step in LOCALWORKER_OWNED_STEPS:
                return _json_result(
                    ok=False,
                    command="reset",
                    message=_localworker_guard_message("reset"),
                    exit_code=2,
                )
            if step not in REMOTE_RESET_STEPS:
                return _json_result(ok=False, command="reset", message=f"无效步骤名: {step}", exit_code=2)
            params = {"config_name": _safe_int_or_all(args.config_name), "step_name": step}
            return _ipc_result("reset", client.request(CMD_RESET_STEP, params))
        if args.command == "stop-step":
            step = str(args.step).lower()
            if step not in {"meshing", "solver", "postprocess"}:
                return _json_result(ok=False, command="stop-step", message=f"无效步骤名: {step}", exit_code=2)
            params = {
                "config_name": _safe_int_or_all(args.config_name),
                "step_name": step,
                "reason": args.reason,
            }
            return _ipc_result("stop-step", client.request(CMD_STOP_STEP, params))
        if args.command == "alerts" and args.alerts_command == "watch":
            return _watch_alerts(
                args,
                client,
                settings,
                webhook_sender or _send_webhook,
                sleep,
                now,
            )
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        if getattr(args, "command", None) == "worker" and getattr(args, "action", None) == "stop":
            response = _add_local_worker_stop_cleanup({
                "status": "error",
                "message": str(exc),
                "data": {},
            })
            return _json_result(
                ok=False,
                command="worker stop",
                message=str(exc),
                data=response.get("data"),
                exit_code=1,
            )
        return _json_result(
            ok=False,
            command=str(getattr(args, "command", "unknown")),
            message=str(exc),
            exit_code=1,
        )
    return _json_result(ok=False, command="unknown", message="未支持的命令", exit_code=2)


def main(argv: list[str] | None = None) -> int:
    result = run_cli(argv)
    sys.stdout.write(result.stdout)
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
