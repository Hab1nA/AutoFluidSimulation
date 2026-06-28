"""Deploy and inspect workstation-owned reverse SSH tunnels."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol


DEFAULT_INSTALL_DIR = "C:/ProgramData/AutoFluid/tunnel"
DEFAULT_TUNNEL_TARGET = "ocar"
SCRIPT_NAME = "start_workstation_owned_reverse_tunnel.ps1"


class WorkstationSsh(Protocol):
    def upload_file(self, local_path: str, remote_path: str, max_retries: int = 3) -> bool: ...

    def exec_command(self, command: str, timeout: int = 30) -> tuple[str, str, int]: ...

    def disconnect(self) -> None: ...


SshFactory = Callable[..., WorkstationSsh]


@dataclass(frozen=True)
class WorkstationTunnelSpec:
    id: str
    host: str
    port: int
    username: str
    password: str
    auth_method: str
    key_filename: str | None
    remote_bind_host: str
    remote_bind_port: int
    tunnel_target: str = DEFAULT_TUNNEL_TARGET


def default_workstation_tunnel_port(index: int) -> int:
    if index == 0:
        return 2222
    return 2224 + max(0, index - 1)


def specs_from_workstations(
    workstations: list[Mapping[str, Any]],
    *,
    tunnel_target: str | None = None,
) -> list[WorkstationTunnelSpec]:
    target = _workstation_tunnel_target(tunnel_target)
    specs: list[WorkstationTunnelSpec] = []
    for index, workstation in enumerate(workstations):
        ws_id = str(workstation.get("id") or f"WS-{index + 1}")
        raw_host = str(workstation.get("host") or "").strip()
        if not raw_host:
            continue
        raw_port = int(workstation.get("port") or 22)
        reachable_host = str(workstation.get("reachable_host") or "127.0.0.1").strip() or "127.0.0.1"
        reachable_port = int(workstation.get("reachable_port") or default_workstation_tunnel_port(index))
        specs.append(
            WorkstationTunnelSpec(
                id=ws_id,
                host=raw_host,
                port=raw_port,
                username=str(workstation.get("username") or ""),
                password=str(workstation.get("password") or ""),
                auth_method=str(workstation.get("auth_method") or "password"),
                key_filename=str(workstation.get("key_filename") or "") or None,
                remote_bind_host=reachable_host,
                remote_bind_port=reachable_port,
                tunnel_target=target,
            )
        )
    return specs


def _workstation_tunnel_target(tunnel_target: str | None = None) -> str:
    target = tunnel_target or os.environ.get("AUTOFLUID_WORKSTATION_TUNNEL_TARGET")
    if target:
        return target
    legacy_target = os.environ.get("AUTOFLUID_WORKSTATION_TUNNEL_HOST")
    if legacy_target:
        return legacy_target
    return _resolve_ssh_config_target(DEFAULT_TUNNEL_TARGET) or DEFAULT_TUNNEL_TARGET


def _resolve_ssh_config_target(alias: str) -> str | None:
    try:
        result = subprocess.run(
            ["ssh", "-G", alias],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    values: dict[str, str] = {}
    for line in result.stdout.splitlines():
        key, _, value = line.partition(" ")
        key = key.strip().lower()
        value = value.strip()
        if key in {"hostname", "user"} and value:
            values[key] = value
    hostname = values.get("hostname", "")
    if not hostname or hostname == alias:
        return None
    user = values.get("user", "")
    if user:
        return f"{user}@{hostname}"
    return hostname


def configured_workstation_specs(*, tunnel_target: str | None = None) -> list[WorkstationTunnelSpec]:
    from engine.config import WORKSTATIONS, reload_config_from_toml

    reload_config_from_toml()
    return specs_from_workstations(list(WORKSTATIONS), tunnel_target=tunnel_target)


def _quote_ps_value(value: str) -> str:
    if not value:
        return '""'
    if all(ch not in value for ch in ' \t"\'`;&|<>'):
        return value
    return '"' + value.replace("`", "``").replace('"', '`"') + '"'


def _remote_script_path(install_dir: str) -> str:
    return f"{install_dir.rstrip('/')}/{SCRIPT_NAME}"


def _user_install_dir(username: str) -> str:
    username = username.strip()
    if not username:
        return ""
    return f"C:/Users/{username}/AppData/Local/AutoFluid/tunnel"


def _normalize_install_dir(install_dir: str) -> str:
    return install_dir.replace("\\", "/").rstrip("/")


def _candidate_install_dirs(install_dir: str, username: str) -> list[str]:
    candidates = [install_dir]
    fallback_dir = _user_install_dir(username)
    normalized_candidates = {_normalize_install_dir(path) for path in candidates}
    if fallback_dir and _normalize_install_dir(fallback_dir) not in normalized_candidates:
        candidates.append(fallback_dir)
    return candidates


def _ps_file_command(script_path: str, action: str, spec: WorkstationTunnelSpec, install_dir: str) -> str:
    args = [
        "powershell.exe",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        _quote_ps_value(script_path.replace("/", "\\")),
        action,
        "-WorkstationId",
        _quote_ps_value(spec.id),
        "-RemoteBindHost",
        _quote_ps_value(spec.remote_bind_host),
        "-RemoteBindPort",
        str(spec.remote_bind_port),
        "-TargetHost",
        "127.0.0.1",
        "-TargetPort",
        "22",
        "-TunnelTarget",
        _quote_ps_value(spec.tunnel_target),
        "-InstallDir",
        _quote_ps_value(install_dir.replace("/", "\\")),
    ]
    return " ".join(args)


def _ssh_factory(factory: SshFactory | None) -> SshFactory:
    if factory is not None:
        return factory
    from utils.ssh_client import RemoteWorkstation

    return RemoteWorkstation


def repair_workstation_tunnel(
    spec: WorkstationTunnelSpec,
    *,
    script_path: Path | None = None,
    install_dir: str = DEFAULT_INSTALL_DIR,
    ssh_factory: SshFactory | None = None,
) -> dict[str, Any]:
    local_script = script_path or Path(__file__).resolve().parents[1] / "scripts" / SCRIPT_NAME
    remote_script = _remote_script_path(install_dir)
    factory = _ssh_factory(ssh_factory)
    ssh = factory(
        host=spec.host,
        port=spec.port,
        username=spec.username,
        password=spec.password,
        key_filename=spec.key_filename,
        auth_method=spec.auth_method,
    )
    try:
        mkdir_command = _mkdir_command(install_dir)
        _, mkdir_err, mkdir_code = ssh.exec_command(mkdir_command, timeout=30)
        if mkdir_code != 0:
            fallback_dir = _user_install_dir(spec.username)
            if not fallback_dir:
                return _result(spec, False, "mkdir_failed", mkdir_err)
            mkdir_command = _mkdir_command(fallback_dir)
            _, fallback_err, fallback_code = ssh.exec_command(mkdir_command, timeout=30)
            if fallback_code != 0:
                return _result(spec, False, "mkdir_failed", fallback_err or mkdir_err)
            install_dir = fallback_dir
            remote_script = _remote_script_path(install_dir)
        if not ssh.upload_file(str(local_script), remote_script):
            return _result(spec, False, "upload_failed", "")
        install_cmd = _ps_file_command(remote_script, "-Install", spec, install_dir)
        out, err, code = ssh.exec_command(install_cmd, timeout=60)
        if code != 0:
            return _result(spec, False, "install_failed", err or out)
        status_result = status_workstation_tunnel(spec, install_dir=install_dir, ssh_factory=lambda **_: ssh)
        result = _result(spec, True, "installed", out)
        result["installed"] = True
        result["install_dir"] = install_dir
        result["status_check"] = status_result
        return result
    finally:
        ssh.disconnect()


def _mkdir_command(install_dir: str) -> str:
    escaped_dir = _quote_cmd_value(install_dir.replace("/", "\\"))
    return f"cmd.exe /d /c if not exist {escaped_dir} mkdir {escaped_dir}"


def _quote_cmd_value(value: str) -> str:
    escaped = value.replace("^", "^^").replace("%", "^%").replace('"', '""')
    return f'"{escaped}"'


def status_workstation_tunnel(
    spec: WorkstationTunnelSpec,
    *,
    install_dir: str = DEFAULT_INSTALL_DIR,
    ssh_factory: SshFactory | None = None,
) -> dict[str, Any]:
    factory = _ssh_factory(ssh_factory)
    ssh = factory(
        host=spec.host,
        port=spec.port,
        username=spec.username,
        password=spec.password,
        key_filename=spec.key_filename,
        auth_method=spec.auth_method,
    )
    try:
        failed_attempts: list[dict[str, str]] = []
        for candidate_dir in _candidate_install_dirs(install_dir, spec.username):
            remote_script = _remote_script_path(candidate_dir)
            status_cmd = _ps_file_command(remote_script, "-Status", spec, candidate_dir)
            out, err, code = ssh.exec_command(status_cmd, timeout=30)
            if code != 0:
                failed_attempts.append({"install_dir": candidate_dir, "detail": err or out})
                continue
            try:
                payload = json.loads(out.strip() or "{}")
            except json.JSONDecodeError:
                payload = {"raw": out.strip()}
            ok = bool(payload.get("task_exists")) and bool(payload.get("remote_tunnel_ok"))
            result = _result(spec, ok, "ok" if ok else "not_ready", "")
            result["install_dir"] = candidate_dir
            result["status_payload"] = payload
            if failed_attempts:
                result["failed_attempts"] = failed_attempts
            return result
        result = _result(spec, False, "status_failed", failed_attempts[-1]["detail"] if failed_attempts else "")
        result["install_dir"] = install_dir
        result["failed_attempts"] = failed_attempts
        return result
    finally:
        ssh.disconnect()


def uninstall_workstation_tunnel(
    spec: WorkstationTunnelSpec,
    *,
    install_dir: str = DEFAULT_INSTALL_DIR,
    ssh_factory: SshFactory | None = None,
) -> dict[str, Any]:
    factory = _ssh_factory(ssh_factory)
    ssh = factory(
        host=spec.host,
        port=spec.port,
        username=spec.username,
        password=spec.password,
        key_filename=spec.key_filename,
        auth_method=spec.auth_method,
    )
    try:
        attempts: list[dict[str, str | int]] = []
        ok = False
        detail = ""
        for candidate_dir in _candidate_install_dirs(install_dir, spec.username):
            remote_script = _remote_script_path(candidate_dir)
            command = _ps_file_command(remote_script, "-Uninstall", spec, candidate_dir)
            out, err, code = ssh.exec_command(command, timeout=30)
            attempts.append({"install_dir": candidate_dir, "exit_code": code, "detail": err or out})
            if code == 0:
                ok = True
                detail = err or out
        result = _result(spec, ok, "ok" if ok else "uninstall_failed", detail or str(attempts[-1]["detail"]))
        result["install_dir"] = install_dir
        result["attempts"] = attempts
        return result
    finally:
        ssh.disconnect()


def _result(spec: WorkstationTunnelSpec, ok: bool, status: str, detail: str) -> dict[str, Any]:
    return {
        "id": spec.id,
        "ok": ok,
        "status": status,
        "detail": detail,
        "bootstrap_target": f"{spec.host}:{spec.port}",
        "remote_endpoint": f"{spec.remote_bind_host}:{spec.remote_bind_port}",
        "workstation_target": "127.0.0.1:22",
    }


def run_for_specs(
    action: str,
    specs: list[WorkstationTunnelSpec],
    *,
    install_dir: str = DEFAULT_INSTALL_DIR,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for spec in specs:
        if action == "repair":
            results.append(repair_workstation_tunnel(spec, install_dir=install_dir))
        elif action == "status":
            results.append(status_workstation_tunnel(spec, install_dir=install_dir))
        elif action == "uninstall":
            results.append(uninstall_workstation_tunnel(spec, install_dir=install_dir))
        else:
            raise ValueError(f"unsupported action: {action}")
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tools.workstation_tunnel")
    parser.add_argument("action", choices=["repair", "status", "uninstall"])
    parser.add_argument("--all", action="store_true", help="accepted for compatibility; all workstations are always used")
    parser.add_argument("--project-dir", default="", help="project directory for TUI callers")
    parser.add_argument("--install-dir", default=DEFAULT_INSTALL_DIR)
    parser.add_argument("--tunnel-target", default=None)
    args = parser.parse_args(argv)
    if args.project_dir:
        os.chdir(args.project_dir)
    specs = configured_workstation_specs(tunnel_target=args.tunnel_target)
    results = run_for_specs(args.action, specs, install_dir=args.install_dir)
    payload = {"ok": bool(results) and any(item["ok"] for item in results), "results": results}
    print(json.dumps(payload, ensure_ascii=False))
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
