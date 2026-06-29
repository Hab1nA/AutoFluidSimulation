"""Deploy and inspect workstation-owned reverse SSH tunnels."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import shlex
import subprocess
import tempfile
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol


DEFAULT_INSTALL_DIR = "C:/ProgramData/AutoFluid/tunnel"
DEFAULT_TUNNEL_TARGET = "ocar"
SCRIPT_NAME = "start_workstation_owned_reverse_tunnel.ps1"
REMOTE_TUNNEL_PROBE_ATTEMPTS = 2
REMOTE_TUNNEL_PROBE_RETRY_DELAY_SECONDS = 0.25
TRANSIENT_REMOTE_TUNNEL_PROBE_ERRORS = (
    "connection reset",
    "connection closed",
    "kex_exchange_identification",
    "banner exchange",
    "broken pipe",
)


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
    tunnel_identity_file: str = ""


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
                tunnel_identity_file=_default_tunnel_identity_file(str(workstation.get("username") or "")),
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


def _default_tunnel_identity_file(username: str) -> str:
    username = username.strip()
    if not username:
        return ""
    return f"C:/Users/{username}/.ssh/autofluid_tunnel_ed25519"


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
        "-TunnelIdentityFile",
        _quote_ps_value(spec.tunnel_identity_file.replace("/", "\\")) if spec.tunnel_identity_file else '""',
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
        try:
            identity_file = _ensure_workstation_tunnel_key(ssh, spec)
        except RuntimeError as exc:
            return _result(spec, False, "key_failed", str(exc))
        if identity_file:
            spec = replace(spec, tunnel_identity_file=identity_file)
        if not ssh.upload_file(str(local_script), remote_script):
            return _result(spec, False, "upload_failed", "")
        install_cmd = _ps_file_command(remote_script, "-Install", spec, install_dir)
        out, err, code = ssh.exec_command(install_cmd, timeout=60)
        if code != 0:
            fallback_dir = _user_install_dir(spec.username)
            if install_dir == DEFAULT_INSTALL_DIR and fallback_dir:
                mkdir_command = _mkdir_command(fallback_dir)
                _, fallback_mkdir_err, fallback_mkdir_code = ssh.exec_command(mkdir_command, timeout=30)
                if fallback_mkdir_code != 0:
                    return _result(spec, False, "mkdir_failed", fallback_mkdir_err or err or out)
                install_dir = fallback_dir
                remote_script = _remote_script_path(install_dir)
                if not ssh.upload_file(str(local_script), remote_script):
                    return _result(spec, False, "upload_failed", "")
                install_cmd = _ps_file_command(remote_script, "-Install", spec, install_dir)
                out, err, code = ssh.exec_command(install_cmd, timeout=60)
                if code == 0:
                    status_result = status_workstation_tunnel(spec, install_dir=install_dir, ssh_factory=lambda **_: ssh)
                    result = _result(spec, True, "installed", out)
                    result["installed"] = True
                    result["install_dir"] = install_dir
                    result["status_check"] = status_result
                    return result
                fallback_out, fallback_err, fallback_code = _install_cmd_lifecycle_fallback(
                    ssh,
                    remote_script,
                    spec,
                    install_dir,
                )
                if fallback_code == 0:
                    result = _result(spec, True, "installed", fallback_out)
                    result["installed"] = True
                    result["install_dir"] = install_dir
                    result["lifecycle_fallback"] = True
                    return result
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


def _ensure_workstation_tunnel_key(ssh: WorkstationSsh, spec: WorkstationTunnelSpec) -> str:
    identity_file = spec.tunnel_identity_file
    if not identity_file:
        return ""
    key_path = identity_file.replace("/", "\\")
    key_dir = "\\".join(key_path.split("\\")[:-1])
    mkdir_cmd = f"cmd.exe /d /c if not exist {_quote_cmd_value(key_dir)} mkdir {_quote_cmd_value(key_dir)}"
    _, mkdir_err, mkdir_code = ssh.exec_command(mkdir_cmd, timeout=30)
    if mkdir_code != 0:
        raise RuntimeError(f"failed to create workstation ssh key directory: {mkdir_err}")
    keygen_cmd = (
        "cmd.exe /d /c "
        f"if not exist {_quote_cmd_value(key_path)} "
        f"ssh-keygen.exe -t ed25519 -N {_quote_cmd_value('')} "
        f"-C {_quote_cmd_value(f'autofluid-{spec.id}-tunnel')} -f {_quote_cmd_value(key_path)}"
    )
    _, keygen_err, keygen_code = ssh.exec_command(keygen_cmd, timeout=60)
    if keygen_code != 0:
        raise RuntimeError(f"failed to create workstation ssh tunnel key: {keygen_err}")
    pub_cmd = f"cmd.exe /d /c type {_quote_cmd_value(key_path + '.pub')}"
    public_key, pub_err, pub_code = ssh.exec_command(pub_cmd, timeout=30)
    if pub_code != 0 or not public_key.strip():
        raise RuntimeError(f"failed to read workstation ssh tunnel public key: {pub_err}")
    _authorize_tunnel_public_key(public_key.strip(), auth_target=spec.tunnel_target)
    return identity_file


def _authorize_tunnel_public_key(public_key: str, *, auth_target: str = DEFAULT_TUNNEL_TARGET) -> None:
    script = (
        "import fcntl,os,sys\n"
        "key=sys.stdin.read().strip()\n"
        "path=os.path.expanduser('~/.ssh/authorized_keys')\n"
        "os.makedirs(os.path.dirname(path), exist_ok=True)\n"
        "with open(path, 'a+', encoding='utf-8') as fh:\n"
        "    fcntl.flock(fh, fcntl.LOCK_EX)\n"
        "    fh.seek(0)\n"
        "    existing=fh.read().splitlines()\n"
        "    if key and key not in existing:\n"
        "        fh.write(key+'\\n')\n"
    )
    result = subprocess.run(
        ["ssh", "-o", "StrictHostKeyChecking=accept-new", auth_target, f"python3 -c {shlex.quote(script)}"],
        input=public_key,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr or result.stdout or "failed to authorize workstation tunnel key")


def _deauthorize_tunnel_public_key(public_key: str, *, auth_target: str) -> None:
    script = (
        "import fcntl,os,sys\n"
        "key=sys.stdin.read().strip()\n"
        "path=os.path.expanduser('~/.ssh/authorized_keys')\n"
        "if not key or not os.path.exists(path):\n"
        "    raise SystemExit(0)\n"
        "with open(path, 'r+', encoding='utf-8') as fh:\n"
        "    fcntl.flock(fh, fcntl.LOCK_EX)\n"
        "    lines=[line for line in fh.read().splitlines() if line.strip() != key]\n"
        "    fh.seek(0)\n"
        "    fh.truncate()\n"
        "    if lines:\n"
        "        fh.write('\\n'.join(lines)+'\\n')\n"
    )
    result = subprocess.run(
        ["ssh", "-o", "StrictHostKeyChecking=accept-new", auth_target, f"python3 -c {shlex.quote(script)}"],
        input=public_key,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr or result.stdout or "failed to deauthorize workstation tunnel key")


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
            installed = bool(payload.get("task_exists")) or bool(payload.get("registry_run_exists"))
            ssh_processes = int(payload.get("ssh_processes") or 0)
            ok = installed and ssh_processes > 0 and bool(payload.get("remote_tunnel_ok"))
            result = _result(spec, ok, "ok" if ok else "not_ready", "")
            result["install_dir"] = candidate_dir
            result["status_payload"] = payload
            if failed_attempts:
                result["failed_attempts"] = failed_attempts
            return result
        cmd_status = _status_cmd_lifecycle_fallback(ssh, spec, install_dir)
        if cmd_status["installed"] or cmd_status["remote_tunnel_ok"]:
            ok = (
                bool(cmd_status["installed"])
                and int(cmd_status.get("ssh_processes") or 0) > 0
                and bool(cmd_status["remote_tunnel_ok"])
            )
            result = _result(spec, ok, "ok" if ok else "not_ready", "")
            result["install_dir"] = cmd_status["install_dir"]
            result["status_payload"] = cmd_status
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
        cleanup_install_dir = install_dir
        for candidate_dir in _candidate_install_dirs(install_dir, spec.username):
            remote_script = _remote_script_path(candidate_dir)
            command = _ps_file_command(remote_script, "-Uninstall", spec, candidate_dir)
            out, err, code = ssh.exec_command(command, timeout=30)
            attempts.append({"install_dir": candidate_dir, "exit_code": code, "detail": err or out})
            if code == 0:
                ok = True
                detail = err or out
                cleanup_install_dir = candidate_dir
        lifecycle_cleanup = _cleanup_cmd_lifecycle_fallback(ssh, spec, install_dir=cleanup_install_dir)
        key_cleanup = _cleanup_workstation_tunnel_key(ssh, spec)
        cleanup_failures = [
            f"{name}:{cleanup.get('stage', 'cleanup')}:{cleanup.get('detail', '')}"
            for name, cleanup in (
                ("lifecycle_cleanup", lifecycle_cleanup),
                ("key_cleanup", key_cleanup),
            )
            if not cleanup.get("ok")
        ]
        ok = ok and not cleanup_failures
        if cleanup_failures:
            cleanup_detail = "; ".join(cleanup_failures)
            detail = f"{detail}; {cleanup_detail}" if detail else cleanup_detail
        result = _result(
            spec,
            ok,
            "ok" if ok else "uninstall_failed",
            detail or str(attempts[-1]["detail"]),
        )
        result["install_dir"] = install_dir
        result["attempts"] = attempts
        result["lifecycle_cleanup"] = lifecycle_cleanup
        result["key_cleanup"] = key_cleanup
        return result
    finally:
        ssh.disconnect()


def _cleanup_workstation_tunnel_key(ssh: WorkstationSsh, spec: WorkstationTunnelSpec) -> dict[str, Any]:
    if not spec.tunnel_identity_file:
        return {"ok": True, "skipped": True}
    key_path = spec.tunnel_identity_file.replace("/", "\\")
    pub_cmd = f"cmd.exe /d /c if exist {_quote_cmd_value(key_path + '.pub')} type {_quote_cmd_value(key_path + '.pub')}"
    public_key, pub_err, pub_code = ssh.exec_command(pub_cmd, timeout=30)
    deauthorized = False
    if pub_code == 0 and public_key.strip():
        try:
            _deauthorize_tunnel_public_key(public_key.strip(), auth_target=spec.tunnel_target)
            deauthorized = True
        except RuntimeError as exc:
            return {"ok": False, "stage": "deauthorize", "detail": str(exc)}
    elif pub_err:
        return {"ok": False, "stage": "read_public_key", "detail": pub_err}
    del_cmd = (
        "cmd.exe /d /c "
        f"if exist {_quote_cmd_value(key_path)} del /q {_quote_cmd_value(key_path)} "
        f"& if exist {_quote_cmd_value(key_path + '.pub')} del /q {_quote_cmd_value(key_path + '.pub')}"
    )
    _, del_err, del_code = ssh.exec_command(del_cmd, timeout=30)
    if del_code != 0:
        return {"ok": False, "stage": "delete_key", "detail": del_err}
    return {"ok": True, "deauthorized": deauthorized}


def _install_cmd_lifecycle_fallback(
    ssh: WorkstationSsh,
    remote_script: str,
    spec: WorkstationTunnelSpec,
    install_dir: str,
) -> tuple[str, str, int]:
    task_name = f"AutoFluidWorkstationTunnel-{spec.id}-{spec.remote_bind_port}"
    monitor_command = _monitor_command(remote_script, spec, install_dir)
    scheduled_cmd = (
        "cmd.exe /d /c "
        f"schtasks.exe /Create /SC MINUTE /MO 1 /TN {_quote_cmd_value(task_name)} "
        f"/TR {_quote_cmd_value(monitor_command)} /F "
        f"&& (schtasks.exe /Run /TN {_quote_cmd_value(task_name)} & exit /b 0)"
    )
    out, err, code = ssh.exec_command(scheduled_cmd, timeout=60)
    if code == 0:
        return out, err, code
    cmd_path = _cmd_supervisor_remote_path(install_dir, spec)
    _cleanup_cmd_lifecycle_fallback(ssh, spec, install_dir=install_dir)
    if not _ensure_cmd_supervisor_ssh_client(ssh, install_dir):
        return "", "failed to provision ssh.exe for cmd supervisor", 1
    if not _upload_cmd_supervisor(ssh, cmd_path, spec, install_dir):
        return "", "failed to upload cmd supervisor", 1
    credentialed_out, credentialed_err, credentialed_code = _install_credentialed_scheduled_cmd_supervisor(
        ssh,
        cmd_path,
        spec,
    )
    if credentialed_code == 0:
        return credentialed_out, credentialed_err, credentialed_code
    run_key = r"HKCU\Software\Microsoft\Windows\CurrentVersion\Run"
    cmd_path_win = cmd_path.replace("/", "\\")
    run_cmd = (
        "cmd.exe /d /c "
        f"reg.exe add {_quote_cmd_value(run_key)} /v {_quote_cmd_value(task_name)} "
        f"/t REG_SZ /d {_quote_cmd_value(cmd_path_win)} /f "
        f"&& start {_quote_cmd_value(task_name)} {_quote_cmd_value(cmd_path_win)}"
    )
    return ssh.exec_command(run_cmd, timeout=60)


def _cleanup_cmd_lifecycle_fallback(
    ssh: WorkstationSsh,
    spec: WorkstationTunnelSpec,
    *,
    install_dir: str = DEFAULT_INSTALL_DIR,
) -> dict[str, Any]:
    task_name = f"AutoFluidWorkstationTunnel-{spec.id}-{spec.remote_bind_port}"
    run_key = r"HKCU\Software\Microsoft\Windows\CurrentVersion\Run"
    cmd_names = [
        _cmd_supervisor_remote_path(path, spec).replace("/", "\\")
        for path in _candidate_install_dirs(install_dir, spec.username)
    ]
    forward_spec = f"{spec.remote_bind_host}:{spec.remote_bind_port}:127.0.0.1:22"
    cmd_cleanup = " & ".join(
        [
            f'taskkill /F /FI "IMAGENAME eq cmd.exe" /FI "WINDOWTITLE eq {task_name}" 2>nul',
            f'wmic process where "name=\'ssh.exe\' and CommandLine like \'%%{forward_spec}%%\'" call terminate 2>nul',
            *[f"if exist {_quote_cmd_value(path)} del /q {_quote_cmd_value(path)}" for path in cmd_names],
        ]
    )
    verify_cmd = (
        f'tasklist /FI "IMAGENAME eq cmd.exe" /FI "WINDOWTITLE eq {task_name}" | findstr /I "cmd.exe" >nul '
        f'&& exit /b 1 || wmic process where "name=\'ssh.exe\' and CommandLine like \'%%{forward_spec}%%\'" '
        'get ProcessId /value | findstr /R "^ProcessId=" >nul && exit /b 1 || exit /b 0'
    )
    cleanup_cmd = (
        "cmd.exe /d /c "
        f"schtasks.exe /Delete /TN {_quote_cmd_value(task_name)} /F 2>nul "
        f"& reg.exe delete {_quote_cmd_value(run_key)} /v {_quote_cmd_value(task_name)} /f 2>nul "
        f"& {cmd_cleanup} "
        f"& {verify_cmd}"
    )
    out, err, code = ssh.exec_command(cleanup_cmd, timeout=30)
    return {"ok": code == 0, "detail": err or out}


def _cmd_supervisor_remote_path(install_dir: str, spec: WorkstationTunnelSpec) -> str:
    return f"{install_dir.rstrip('/')}/AutoFluidWorkstationTunnel-{spec.id}-{spec.remote_bind_port}.cmd"


def _ensure_cmd_supervisor_ssh_client(ssh: WorkstationSsh, install_dir: str) -> bool:
    remote_ssh = f"{install_dir.rstrip('/')}/ssh.exe".replace("/", "\\")
    local_ssh = os.environ.get("AUTOFLUID_WORKSTATION_TUNNEL_SSH_EXE_SOURCE", "").strip()
    if local_ssh:
        return _is_plausible_windows_executable(Path(local_ssh)) and ssh.upload_file(local_ssh, remote_ssh)
    check_cmd = (
        "cmd.exe /d /c "
        "if exist C:\\Windows\\System32\\OpenSSH\\ssh.exe (exit /b 0) "
        f"else if exist {_quote_cmd_value(remote_ssh)} (exit /b 0) "
        "else (exit /b 1)"
    )
    _, _, code = ssh.exec_command(check_cmd, timeout=30)
    if code == 0:
        return True
    discovered_ssh = shutil.which("ssh.exe") or shutil.which("ssh")
    if not discovered_ssh:
        candidate = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "OpenSSH" / "ssh.exe"
        if candidate.exists():
            discovered_ssh = str(candidate)
    if not discovered_ssh:
        return False
    return ssh.upload_file(discovered_ssh, remote_ssh)


def _is_plausible_windows_executable(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            header = handle.read(2)
        return header == b"MZ" and path.stat().st_size > 100_000
    except OSError:
        return False


def _install_credentialed_scheduled_cmd_supervisor(
    ssh: WorkstationSsh,
    cmd_path: str,
    spec: WorkstationTunnelSpec,
) -> tuple[str, str, int]:
    if not spec.password:
        return "", "workstation password is required for credentialed scheduled task fallback", 1
    account_out, _, account_code = ssh.exec_command("cmd.exe /d /c whoami", timeout=30)
    account = account_out.strip() if account_code == 0 and account_out.strip() else spec.username
    task_name = f"AutoFluidWorkstationTunnel-{spec.id}-{spec.remote_bind_port}"
    cmd_path = cmd_path.replace("/", "\\")
    # Windows schtasks has no secure stdin form for /RP. This fallback is used
    # only for hosts that reject both remote PowerShell and normal task creation.
    scheduled_cmd = (
        "cmd.exe /d /c "
        f"schtasks.exe /Create /SC MINUTE /MO 1 /TN {_quote_cmd_value(task_name)} "
        f"/TR {_quote_cmd_value(cmd_path)} /RU {_quote_cmd_value(account)} /RP {_quote_cmd_value(spec.password)} /F "
        f"&& (schtasks.exe /Run /TN {_quote_cmd_value(task_name)} & exit /b 0)"
    )
    return ssh.exec_command(scheduled_cmd, timeout=60)


def _upload_cmd_supervisor(
    ssh: WorkstationSsh,
    remote_cmd_path: str,
    spec: WorkstationTunnelSpec,
    install_dir: str,
) -> bool:
    script = _render_cmd_supervisor(spec, install_dir)
    tmp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="\r\n", suffix=".cmd", delete=False) as handle:
            handle.write(script)
            tmp_path = handle.name
        return ssh.upload_file(tmp_path, remote_cmd_path)
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def _render_cmd_supervisor(spec: WorkstationTunnelSpec, install_dir: str) -> str:
    log_dir = install_dir.rstrip("/").replace("/", "\\") + "\\logs"
    forward_spec = f"{spec.remote_bind_host}:{spec.remote_bind_port}:127.0.0.1:22"
    identity = spec.tunnel_identity_file.replace("/", "\\")
    remote_probe = (
        "python3 -c 'import socket,sys; s=socket.socket(); s.settimeout(3); "
        f"s.connect((bytes([49,50,55,46,48,46,48,46,49]).decode(),{spec.remote_bind_port})); "
        "data=s.recv(4); s.close(); sys.exit(0 if data==bytes([83,83,72,45]) else 1)'"
    )
    stale_cleanup = f"bash -lc 'fuser -k {spec.remote_bind_port}/tcp >/dev/null 2>&1 || true'"
    return f"""@echo off
setlocal EnableExtensions EnableDelayedExpansion
title AutoFluidWorkstationTunnel-{spec.id}-{spec.remote_bind_port}
set "SSH_EXE=%~dp0ssh.exe"
if not exist "%SSH_EXE%" set "SSH_EXE=C:\\Windows\\System32\\OpenSSH\\ssh.exe"
if not exist "%SSH_EXE%" set "SSH_EXE=ssh.exe"
set "IDENTITY={identity}"
set "FORWARD_SPEC={forward_spec}"
set "TUNNEL_TARGET={spec.tunnel_target}"
set "LOG_DIR={log_dir}"
set "LOCK_FILE=%LOG_DIR%\\workstation-{spec.id}-{spec.remote_bind_port}-cmd-supervisor.lock"
set "REMOTE_PROBE_FAILURES=0"
set "REMOTE_PROBE_FAILURE_THRESHOLD=3"
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%" >nul 2>nul
if exist "%LOCK_FILE%" (
  call :has_ssh
  if not errorlevel 1 (
    echo [%date% %time%] another supervisor instance already owns %FORWARD_SPEC%; exiting>>"%LOG_DIR%\\workstation-{spec.id}-{spec.remote_bind_port}-cmd-supervisor.log"
    exit /b 0
  )
  call :probe_remote
  if not errorlevel 1 (
    echo [%date% %time%] tunnel is already healthy; exiting duplicate supervisor>>"%LOG_DIR%\\workstation-{spec.id}-{spec.remote_bind_port}-cmd-supervisor.log"
    exit /b 0
  )
  del /q "%LOCK_FILE%" >nul 2>nul
)
echo %date% %time%>%LOCK_FILE%
:loop
if not exist "%SSH_EXE%" (
  echo [%date% %time%] ssh.exe not found at %SSH_EXE%>>"%LOG_DIR%\\workstation-{spec.id}-{spec.remote_bind_port}-cmd-supervisor.log"
  timeout /t 30 /nobreak >nul
  goto loop
)
call :clear_remote_forward
:restart_ssh
echo [%date% %time%] starting ssh reverse tunnel %FORWARD_SPEC% via %TUNNEL_TARGET%>>"%LOG_DIR%\\workstation-{spec.id}-{spec.remote_bind_port}-cmd-supervisor.log"
if defined IDENTITY (
  start "AutoFluidTunnelSsh-{spec.id}-{spec.remote_bind_port}" /B "%SSH_EXE%" -i "%IDENTITY%" -o BatchMode=yes -o ExitOnForwardFailure=yes -o ServerAliveInterval=5 -o ServerAliveCountMax=3 -o TCPKeepAlive=yes -o StrictHostKeyChecking=accept-new -N -R "%FORWARD_SPEC%" "%TUNNEL_TARGET%" 1>>"%LOG_DIR%\\workstation-{spec.id}-{spec.remote_bind_port}-cmd-ssh.stdout.log" 2>>"%LOG_DIR%\\workstation-{spec.id}-{spec.remote_bind_port}-cmd-ssh.stderr.log"
) else (
  start "AutoFluidTunnelSsh-{spec.id}-{spec.remote_bind_port}" /B "%SSH_EXE%" -o BatchMode=yes -o ExitOnForwardFailure=yes -o ServerAliveInterval=5 -o ServerAliveCountMax=3 -o TCPKeepAlive=yes -o StrictHostKeyChecking=accept-new -N -R "%FORWARD_SPEC%" "%TUNNEL_TARGET%" 1>>"%LOG_DIR%\\workstation-{spec.id}-{spec.remote_bind_port}-cmd-ssh.stdout.log" 2>>"%LOG_DIR%\\workstation-{spec.id}-{spec.remote_bind_port}-cmd-ssh.stderr.log"
)
timeout /t 5 /nobreak >nul
:probe
call :has_ssh
if errorlevel 1 (
  echo [%date% %time%] ssh process exited; restarting>>"%LOG_DIR%\\workstation-{spec.id}-{spec.remote_bind_port}-cmd-supervisor.log"
  timeout /t 5 /nobreak >nul
  goto loop
)
call :probe_remote
if errorlevel 1 (
  set /a REMOTE_PROBE_FAILURES+=1
  echo [%date% %time%] remote tunnel probe failed !REMOTE_PROBE_FAILURES!/%REMOTE_PROBE_FAILURE_THRESHOLD%>>"%LOG_DIR%\\workstation-{spec.id}-{spec.remote_bind_port}-cmd-supervisor.log"
  if !REMOTE_PROBE_FAILURES! lss %REMOTE_PROBE_FAILURE_THRESHOLD% (
    timeout /t 5 /nobreak >nul
    goto probe
  )
  echo [%date% %time%] remote tunnel probe failed; restarting>>"%LOG_DIR%\\workstation-{spec.id}-{spec.remote_bind_port}-cmd-supervisor.log"
  call :kill_ssh
  call :clear_remote_forward
  set "REMOTE_PROBE_FAILURES=0"
  timeout /t 5 /nobreak >nul
  goto restart_ssh
)
set "REMOTE_PROBE_FAILURES=0"
timeout /t 5 /nobreak >nul
goto probe

:probe_remote
if defined IDENTITY (
  "%SSH_EXE%" -i "%IDENTITY%" -o BatchMode=yes -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new "%TUNNEL_TARGET%" "{remote_probe}" >nul 2>nul
) else (
  "%SSH_EXE%" -o BatchMode=yes -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new "%TUNNEL_TARGET%" "{remote_probe}" >nul 2>nul
)
exit /b %ERRORLEVEL%

:clear_remote_forward
if defined IDENTITY (
  "%SSH_EXE%" -i "%IDENTITY%" -o BatchMode=yes -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new "%TUNNEL_TARGET%" "{stale_cleanup}" >nul 2>nul
) else (
  "%SSH_EXE%" -o BatchMode=yes -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new "%TUNNEL_TARGET%" "{stale_cleanup}" >nul 2>nul
)
exit /b 0

:has_ssh
wmic process where "name='ssh.exe' and CommandLine like '%%{forward_spec}%%'" get ProcessId /value | findstr /R "^ProcessId=" >nul
exit /b %ERRORLEVEL%

:kill_ssh
wmic process where "name='ssh.exe' and CommandLine like '%%{forward_spec}%%'" call terminate >nul 2>nul
exit /b 0
"""


def _status_cmd_lifecycle_fallback(
    ssh: WorkstationSsh,
    spec: WorkstationTunnelSpec,
    install_dir: str,
) -> dict[str, Any]:
    task_name = f"AutoFluidWorkstationTunnel-{spec.id}-{spec.remote_bind_port}"
    run_key = r"HKCU\Software\Microsoft\Windows\CurrentVersion\Run"
    for candidate_dir in _candidate_install_dirs(install_dir, spec.username):
        cmd_path = _cmd_supervisor_remote_path(candidate_dir, spec).replace("/", "\\")
        status_cmd = (
            "cmd.exe /d /c "
            f"if exist {_quote_cmd_value(cmd_path)} (echo script_exists=1) else (echo script_exists=0) "
            f"& reg.exe query {_quote_cmd_value(run_key)} /v {_quote_cmd_value(task_name)} "
            "& exit /b 0"
        )
        out, err, code = ssh.exec_command(status_cmd, timeout=30)
        if code != 0:
            continue
        installed = "script_exists=1" in out or task_name in out
        if installed:
            forward_spec = f"{spec.remote_bind_host}:{spec.remote_bind_port}:127.0.0.1:22"
            process_cmd = (
                "cmd.exe /d /c "
                f'wmic process where "name=\'ssh.exe\' and CommandLine like \'%%{forward_spec}%%\'" '
                'get ProcessId /value'
            )
            process_out, _, _ = ssh.exec_command(process_cmd, timeout=30)
            ssh_processes = process_out.count("ProcessId=")
            remote_ok = ssh_processes > 0 and _probe_remote_tunnel_endpoint(spec)
            return {
                "workstation_id": spec.id,
                "task_name": task_name,
                "install_dir": candidate_dir,
                "cmd_supervisor_exists": "script_exists=1" in out,
                "registry_run_exists": task_name in out,
                "ssh_processes": ssh_processes,
                "remote_tunnel_ok": remote_ok,
                "detail": err or out,
                "installed": installed,
            }
    return {
        "workstation_id": spec.id,
        "task_name": task_name,
        "install_dir": install_dir,
        "cmd_supervisor_exists": False,
        "registry_run_exists": False,
        "ssh_processes": 0,
        "remote_tunnel_ok": False,
        "installed": False,
    }


def _probe_remote_tunnel_endpoint(spec: WorkstationTunnelSpec) -> bool:
    script = (
        "import socket\n"
        "s=socket.socket()\n"
        "s.settimeout(3)\n"
        f"s.connect(({spec.remote_bind_host!r},{spec.remote_bind_port}))\n"
        "data=s.recv(4)\n"
        "s.close()\n"
        "raise SystemExit(0 if data == b'SSH-' else 1)\n"
    )
    command = [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=10",
    ]
    command.extend([spec.tunnel_target, f"python3 -c {shlex.quote(script)}"])
    for attempt in range(REMOTE_TUNNEL_PROBE_ATTEMPTS):
        try:
            result = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=15,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        if result.returncode == 0:
            return True
        if attempt + 1 >= REMOTE_TUNNEL_PROBE_ATTEMPTS or not _is_transient_remote_probe_failure(result):
            return False
        time.sleep(REMOTE_TUNNEL_PROBE_RETRY_DELAY_SECONDS)
    return False


def _is_transient_remote_probe_failure(result: subprocess.CompletedProcess[str]) -> bool:
    output = result.stderr.lower()
    return any(marker in output for marker in TRANSIENT_REMOTE_TUNNEL_PROBE_ERRORS)


def _monitor_command(remote_script: str, spec: WorkstationTunnelSpec, install_dir: str) -> str:
    args = [
        "powershell.exe",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        remote_script.replace("/", "\\"),
        "-Monitor",
        "-WorkstationId",
        spec.id,
        "-RemoteBindHost",
        spec.remote_bind_host,
        "-RemoteBindPort",
        str(spec.remote_bind_port),
        "-TargetHost",
        "127.0.0.1",
        "-TargetPort",
        "22",
        "-TunnelTarget",
        spec.tunnel_target,
        "-TunnelIdentityFile",
        spec.tunnel_identity_file.replace("/", "\\"),
        "-InstallDir",
        install_dir.replace("/", "\\"),
    ]
    return " ".join(_cmd_arg(item) for item in args)


def _cmd_arg(value: str) -> str:
    if not value:
        return '""'
    if any(ch in value for ch in ' \t"&|<>'):
        return _quote_cmd_value(value)
    return value


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
