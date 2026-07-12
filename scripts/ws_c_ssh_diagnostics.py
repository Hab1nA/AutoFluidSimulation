from __future__ import annotations

import argparse
import json
import socket
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast


WORKSTATION_ID = "WS-C"
REMOTE_BIND_HOST = "127.0.0.1"
REMOTE_BIND_PORT = 2225
TARGET_HOST = "127.0.0.1"
TARGET_PORT = 22
TUNNEL_TARGET = "root@39.98.196.94"
TUNNEL_IDENTITY_FILE = r"C:\Users\bh\.ssh\autofluid_tunnel_ed25519"
INSTALL_DIR = Path(r"C:\ProgramData\AutoFluid\diagnostics")
TUNNEL_INSTALL_DIR = Path(r"C:\ProgramData\AutoFluid\tunnel")
MAX_LOG_BYTES = 1_048_576
MAX_LOG_FILES = 5
HEARTBEAT_SECONDS = 300
COMMAND_TIMEOUT_SECONDS = 10


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_iso_seconds(value: str) -> float | None:
    if not value:
        return None
    try:
        if value.endswith("Z"):
            value = value[:-1] + "+00:00"
        return datetime.fromisoformat(value).timestamp()
    except ValueError:
        return None


def log_path(install_dir: Path) -> Path:
    return install_dir / "logs" / "ws-c-ssh-diagnostics.jsonl"


def state_path(install_dir: Path) -> Path:
    return install_dir / "logs" / "ws-c-ssh-diagnostics-state.json"


def rotate_log(path: Path, *, max_bytes: int = MAX_LOG_BYTES, max_files: int = MAX_LOG_FILES) -> None:
    if max_files < 1 or not path.exists() or path.stat().st_size < max_bytes:
        return
    for index in range(max_files - 1, 0, -1):
        source = path.with_name(f"{path.name}.{index}")
        target = path.with_name(f"{path.name}.{index + 1}")
        if source.exists():
            source.replace(target)
    path.replace(path.with_name(f"{path.name}.1"))


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"last_signature": "", "last_log_at": "", "consecutive_failures": 0, "first_failure_at": ""}
    try:
        raw_state = json.loads(path.read_text(encoding="utf-8-sig"))
        if isinstance(raw_state, dict):
            return cast(dict[str, Any], raw_state)
    except (OSError, json.JSONDecodeError):
        pass
    return {"last_signature": "", "last_log_at": "", "consecutive_failures": 0, "first_failure_at": ""}


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def should_write_snapshot(
    signature: str,
    state: dict[str, Any],
    *,
    now: float | None = None,
    heartbeat_seconds: int = HEARTBEAT_SECONDS,
) -> bool:
    if signature != state.get("last_signature", ""):
        return True
    now = time.time() if now is None else now
    last_log_at = parse_iso_seconds(str(state.get("last_log_at", "")))
    return last_log_at is None or now - last_log_at >= heartbeat_seconds


def run_command(args: list[str], *, timeout: int = COMMAND_TIMEOUT_SECONDS) -> dict[str, Any]:
    started = time.monotonic()
    try:
        completed = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return {
            "ok": completed.returncode == 0,
            "status": "ok" if completed.returncode == 0 else "failed",
            "exit_code": completed.returncode,
            "duration_ms": int((time.monotonic() - started) * 1000),
            "stderr": completed.stderr.strip()[:500],
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "ok": False,
            "status": "timeout",
            "exit_code": 124,
            "duration_ms": int((time.monotonic() - started) * 1000),
            "stderr": str(exc)[:500],
        }
    except OSError as exc:
        return {
            "ok": False,
            "status": "error",
            "exit_code": None,
            "duration_ms": int((time.monotonic() - started) * 1000),
            "stderr": str(exc)[:500],
        }


def test_tcp(host: str, port: int, *, timeout: float = 2.0) -> dict[str, Any]:
    started = time.monotonic()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return {"ok": True, "status": "open", "duration_ms": int((time.monotonic() - started) * 1000)}
    except socket.timeout:
        return {"ok": False, "status": "timeout", "duration_ms": int((time.monotonic() - started) * 1000)}
    except OSError as exc:
        return {
            "ok": False,
            "status": "failed",
            "duration_ms": int((time.monotonic() - started) * 1000),
            "error": str(exc)[:300],
        }


def ssh_base_args(identity_file: str) -> list[str]:
    args = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "-o", "StrictHostKeyChecking=accept-new"]
    if identity_file:
        args.extend(["-i", identity_file])
    return args


def test_remote_banner(
    tunnel_target: str,
    identity_file: str,
    remote_host: str,
    remote_port: int,
) -> dict[str, Any]:
    host_bytes = ",".join(str(ord(ch)) for ch in remote_host)
    remote_command = (
        "python3 -c 'import socket,sys; "
        "s=socket.socket(); s.settimeout(3); "
        f"s.connect((bytes([{host_bytes}]).decode(),{remote_port})); "
        "data=s.recv(4); s.close(); "
        "sys.exit(0 if data==bytes([83,83,72,45]) else 1)'"
    )
    result = run_command(ssh_base_args(identity_file) + [tunnel_target, remote_command])
    if result["status"] == "ok":
        result["status"] = "ready"
    return result


def tunnel_monitor_status(tunnel_install_dir: Path, workstation_id: str, remote_port: int) -> dict[str, Any]:
    path = tunnel_install_dir / "logs" / f"workstation-{workstation_id}-{remote_port}-monitor-status.json"
    if not path.exists():
        return {"exists": False, "path": str(path)}
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"exists": True, "path": str(path), "parse_error": str(exc)[:300]}
    return {
        "exists": True,
        "path": str(path),
        "updated_at": raw.get("updated_at", ""),
        "state": raw.get("state", ""),
        "last_reason": raw.get("last_reason", ""),
        "ssh_pid": raw.get("ssh_pid"),
    }


def supervisor_tail(tunnel_install_dir: Path, workstation_id: str, remote_port: int) -> dict[str, Any]:
    path = tunnel_install_dir / "logs" / f"workstation-{workstation_id}-{remote_port}-supervisor.log"
    if not path.exists():
        return {"exists": False, "path": str(path), "events": []}
    try:
        lines = path.read_text(encoding="utf-8-sig", errors="replace").splitlines()[-40:]
    except OSError as exc:
        return {"exists": True, "path": str(path), "error": str(exc)[:300], "events": []}
    keywords = ("not reachable", "Starting ssh reverse tunnel", "Timed out clearing", "Failed to clear", "Monitor starting")
    events = [line for line in lines if any(keyword in line for keyword in keywords)][-8:]
    return {
        "exists": True,
        "path": str(path),
        "length": path.stat().st_size,
        "mtime": datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat().replace("+00:00", "Z"),
        "events": events,
    }


def pid_status(pid: Any) -> dict[str, Any]:
    if pid in (None, ""):
        return {"known": False, "running": False}
    result = run_command(["tasklist", "/FI", f"PID eq {int(pid)}", "/FO", "CSV", "/NH"], timeout=5)
    stdout = ""
    try:
        completed = subprocess.run(
            ["tasklist", "/FI", f"PID eq {int(pid)}", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        stdout = completed.stdout.strip()
        running = completed.returncode == 0 and str(pid) in stdout and "INFO:" not in stdout.upper()
        return {"known": True, "running": running, "pid": int(pid), "tasklist": stdout[:300]}
    except (subprocess.TimeoutExpired, OSError) as exc:
        return {"known": True, "running": False, "pid": int(pid), "error": str(exc)[:300], "probe": result}


def build_snapshot(args: argparse.Namespace, state: dict[str, Any]) -> dict[str, Any]:
    monitor = tunnel_monitor_status(args.tunnel_install_dir, args.workstation_id, args.remote_bind_port)
    local_target = test_tcp(args.target_host, args.target_port)
    remote_banner = test_remote_banner(
        args.tunnel_target,
        args.tunnel_identity_file,
        args.remote_bind_host,
        args.remote_bind_port,
    )
    process = pid_status(monitor.get("ssh_pid"))
    status = "ok"
    if not local_target["ok"]:
        status = "local-target-failed"
    elif not process.get("running"):
        status = "ssh-process-missing"
    elif not remote_banner["ok"]:
        status = "remote-banner-failed"

    now = iso_now()
    if status == "ok":
        state["consecutive_failures"] = 0
        state["first_failure_at"] = ""
        state["last_success_at"] = now
    else:
        state["consecutive_failures"] = int(state.get("consecutive_failures") or 0) + 1
        state.setdefault("first_failure_at", now)
        if not state.get("first_failure_at"):
            state["first_failure_at"] = now

    return {
        "timestamp": now,
        "workstation_id": args.workstation_id,
        "status": status,
        "target": {
            "local": f"{args.target_host}:{args.target_port}",
            "remote": f"{args.remote_bind_host}:{args.remote_bind_port}",
            "tunnel_target": args.tunnel_target,
        },
        "local_target": local_target,
        "remote_banner": remote_banner,
        "monitor_status": monitor,
        "ssh_process": process,
        "supervisor_tail": supervisor_tail(args.tunnel_install_dir, args.workstation_id, args.remote_bind_port),
        "failure_window": {
            "consecutive_samples": state.get("consecutive_failures", 0),
            "first_failure_at": state.get("first_failure_at", ""),
            "last_success_at": state.get("last_success_at", ""),
        },
    }


def signature(snapshot: dict[str, Any]) -> str:
    monitor = snapshot.get("monitor_status") or {}
    monitor_reason = str(monitor.get("last_reason", ""))
    monitor_state = str(monitor.get("state", ""))
    if snapshot.get("status") == "ok" and not monitor_reason:
        monitor_state = "healthy"
    return "|".join(
        [
            str(snapshot.get("status", "")),
            str((snapshot.get("local_target") or {}).get("status", "")),
            str((snapshot.get("remote_banner") or {}).get("status", "")),
            str((snapshot.get("ssh_process") or {}).get("running", "")),
            monitor_state,
            monitor_reason,
        ]
    )


def write_snapshot(path: Path, snapshot: dict[str, Any], *, max_bytes: int, max_files: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rotate_log(path, max_bytes=max_bytes, max_files=max_files)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")) + "\n")


def run_once(args: argparse.Namespace) -> dict[str, Any]:
    state_file = state_path(args.install_dir)
    state = load_state(state_file)
    snapshot = build_snapshot(args, state)
    snapshot_signature = signature(snapshot)
    if args.force_write or should_write_snapshot(
        snapshot_signature,
        state,
        heartbeat_seconds=args.heartbeat_seconds,
    ):
        write_snapshot(log_path(args.install_dir), snapshot, max_bytes=args.max_log_bytes, max_files=args.max_log_files)
        state["last_signature"] = snapshot_signature
        state["last_log_at"] = snapshot["timestamp"]
    save_state(state_file, state)
    return snapshot


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-once", action="store_true")
    parser.add_argument("--force-write", action="store_true")
    parser.add_argument("--workstation-id", default=WORKSTATION_ID)
    parser.add_argument("--remote-bind-host", default=REMOTE_BIND_HOST)
    parser.add_argument("--remote-bind-port", type=int, default=REMOTE_BIND_PORT)
    parser.add_argument("--target-host", default=TARGET_HOST)
    parser.add_argument("--target-port", type=int, default=TARGET_PORT)
    parser.add_argument("--tunnel-target", default=TUNNEL_TARGET)
    parser.add_argument("--tunnel-identity-file", default=TUNNEL_IDENTITY_FILE)
    parser.add_argument("--install-dir", type=Path, default=INSTALL_DIR)
    parser.add_argument("--tunnel-install-dir", type=Path, default=TUNNEL_INSTALL_DIR)
    parser.add_argument("--heartbeat-seconds", type=int, default=HEARTBEAT_SECONDS)
    parser.add_argument("--max-log-bytes", type=int, default=MAX_LOG_BYTES)
    parser.add_argument("--max-log-files", type=int, default=MAX_LOG_FILES)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.run_once:
        print(json.dumps({"script": str(Path(__file__).resolve()), "mode": "run-once-only"}, ensure_ascii=False))
        return 0
    snapshot = run_once(args)
    print(json.dumps(snapshot, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
