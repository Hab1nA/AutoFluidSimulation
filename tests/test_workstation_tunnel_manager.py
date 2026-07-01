from __future__ import annotations

import json
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tools import workstation_tunnel


class FakeRemoteWorkstation:
    instances: list["FakeRemoteWorkstation"] = []

    def __init__(
        self,
        host: str,
        port: int,
        username: str,
        password: str,
        key_filename: str | None = None,
        auth_method: str = "password",
    ) -> None:
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.key_filename = key_filename
        self.auth_method = auth_method
        self.uploads: list[tuple[str, str]] = []
        self.commands: list[str] = []
        FakeRemoteWorkstation.instances.append(self)

    def upload_file(self, local_path: str, remote_path: str, max_retries: int = 3) -> bool:
        self.uploads.append((local_path, remote_path))
        return True

    def exec_command(self, command: str, timeout: int = 30) -> tuple[str, str, int]:
        self.commands.append(command)
        return "{}", "", 0

    def disconnect(self) -> None:
        self.commands.append("disconnect")


class ProgramDataDeniedRemoteWorkstation(FakeRemoteWorkstation):
    def exec_command(self, command: str, timeout: int = 30) -> tuple[str, str, int]:
        self.commands.append(command)
        if "C:\\ProgramData\\AutoFluid\\tunnel" in command:
            return "", "拒绝访问。", 1
        return "{}", "", 0


class ProgramDataInstallDeniedRemoteWorkstation(FakeRemoteWorkstation):
    def exec_command(self, command: str, timeout: int = 30) -> tuple[str, str, int]:
        self.commands.append(command)
        if " -Install " in command:
            return "", "拒绝访问。", 1
        return '{"task_exists": false, "registry_run_exists": true, "ssh_processes": 1, "remote_tunnel_ok": true}', "", 0


class SchTasksDeniedRemoteWorkstation(ProgramDataInstallDeniedRemoteWorkstation):
    def exec_command(self, command: str, timeout: int = 30) -> tuple[str, str, int]:
        self.commands.append(command)
        if " -Install " in command:
            return "", "拒绝访问。", 1
        if "schtasks.exe /Create" in command:
            return "", "schtasks denied", 1
        return '{"task_exists": false, "registry_run_exists": true, "ssh_processes": 1, "remote_tunnel_ok": true}', "", 0


class CredentialedSchTasksRemoteWorkstation(ProgramDataInstallDeniedRemoteWorkstation):
    def exec_command(self, command: str, timeout: int = 30) -> tuple[str, str, int]:
        self.commands.append(command)
        if " -Install " in command:
            return "", "拒绝访问。", 1
        if command == "cmd.exe /d /c whoami":
            return "DESKTOP-TD0FQ8U\\ps\r\n", "", 0
        if "schtasks.exe /Create" in command and " /RU " not in command:
            return "", "拒绝访问。", 1
        return '{"task_exists": true, "ssh_processes": 1, "remote_tunnel_ok": true}', "", 0


class ProgramDataMissingRemoteWorkstation(FakeRemoteWorkstation):
    def exec_command(self, command: str, timeout: int = 30) -> tuple[str, str, int]:
        self.commands.append(command)
        if "C:\\ProgramData\\AutoFluid\\tunnel" in command:
            return "", "script not found", 1
        return '{"task_exists": true, "ssh_processes": 1, "remote_tunnel_ok": true}', "", 0


class RegistryRunStatusRemoteWorkstation(FakeRemoteWorkstation):
    def exec_command(self, command: str, timeout: int = 30) -> tuple[str, str, int]:
        self.commands.append(command)
        return '{"task_exists": false, "registry_run_exists": true, "ssh_processes": 1, "remote_tunnel_ok": true}', "", 0


class RegistryRunWithoutSshProcessRemoteWorkstation(FakeRemoteWorkstation):
    def exec_command(self, command: str, timeout: int = 30) -> tuple[str, str, int]:
        self.commands.append(command)
        return '{"task_exists": false, "registry_run_exists": true, "ssh_processes": 0, "remote_tunnel_ok": true}', "", 0


class WedgedMonitorStatusRemoteWorkstation(FakeRemoteWorkstation):
    def exec_command(self, command: str, timeout: int = 30) -> tuple[str, str, int]:
        self.commands.append(command)
        return (
            json.dumps(
                {
                    "task_exists": True,
                    "task_state": "Running",
                    "registry_run_exists": False,
                    "monitor_processes": 1,
                    "ssh_processes": 0,
                    "local_target_ok": True,
                    "remote_tunnel_ok": False,
                    "monitor_last_seen_at": "2000-01-01T00:00:00Z",
                    "monitor_state": "loop",
                    "monitor_last_reason": "remote-probe-timeout",
                }
            ),
            "",
            0,
        )


class StatusTimeoutRecordingRemoteWorkstation(FakeRemoteWorkstation):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.timeouts: list[int] = []

    def exec_command(self, command: str, timeout: int = 30) -> tuple[str, str, int]:
        self.commands.append(command)
        self.timeouts.append(timeout)
        return (
            json.dumps(
                {
                    "script_version": workstation_tunnel.OWNED_TUNNEL_SCRIPT_VERSION,
                    "task_exists": True,
                    "registry_run_exists": False,
                    "monitor_processes": 1,
                    "ssh_processes": 1,
                    "remote_tunnel_ok": True,
                }
            ),
            "",
            0,
        )


class PowerShellStatusDeniedCmdInstalledRemoteWorkstation(FakeRemoteWorkstation):
    def exec_command(self, command: str, timeout: int = 30) -> tuple[str, str, int]:
        self.commands.append(command)
        if " -Status " in command:
            return "", "拒绝访问。", 1
        if "script_exists" in command:
            return (
                "script_exists=1\r\n"
                "HKEY_CURRENT_USER\\Software\\Microsoft\\Windows\\CurrentVersion\\Run\r\n"
                "    AutoFluidWorkstationTunnel-WS-A-2222    REG_SZ    C:\\Users\\ps\\AppData\\Local\\AutoFluid\\tunnel\\AutoFluidWorkstationTunnel-WS-A-2222.cmd\r\n",
                "",
                0,
            )
        if "CommandLine like" in command:
            return "ProcessId=1234\r\n", "", 0
        return "{}", "", 0


class UserDirCmdInstalledRemoteWorkstation(FakeRemoteWorkstation):
    def exec_command(self, command: str, timeout: int = 30) -> tuple[str, str, int]:
        self.commands.append(command)
        if " -Status " in command:
            return "", "拒绝访问。", 1
        if "script_exists" in command and "C:\\ProgramData\\AutoFluid\\tunnel" in command:
            return "script_exists=0\r\n", "错误: 系统找不到指定的注册表项或值。\r\n", 0
        if "script_exists" in command and "C:\\Users\\ps\\AppData\\Local\\AutoFluid\\tunnel" in command:
            return "script_exists=1\r\n", "", 0
        if "CommandLine like" in command:
            return "ProcessId=1234\r\n", "", 0
        return "{}", "", 0


class PowerShellStatusDeniedCmdNoSshProcessRemoteWorkstation(PowerShellStatusDeniedCmdInstalledRemoteWorkstation):
    def exec_command(self, command: str, timeout: int = 30) -> tuple[str, str, int]:
        if "CommandLine like" in command:
            self.commands.append(command)
            return "", "", 0
        return super().exec_command(command, timeout)


class BothDirsDeniedRemoteWorkstation(FakeRemoteWorkstation):
    def exec_command(self, command: str, timeout: int = 30) -> tuple[str, str, int]:
        self.commands.append(command)
        return "", "拒绝访问。", 1


class CleanupDeniedRemoteWorkstation(FakeRemoteWorkstation):
    def exec_command(self, command: str, timeout: int = 30) -> tuple[str, str, int]:
        self.commands.append(command)
        return "", "cleanup denied", 1


class UninstallOkCleanupDeniedRemoteWorkstation(FakeRemoteWorkstation):
    def exec_command(self, command: str, timeout: int = 30) -> tuple[str, str, int]:
        self.commands.append(command)
        if " -Uninstall " in command:
            return "uninstalled", "", 0
        if "schtasks.exe /Delete" in command:
            return "", "cleanup denied", 1
        return '{"task_exists": true, "ssh_processes": 1, "remote_tunnel_ok": true}', "", 0


class UninstallOkKeyCleanupDeniedRemoteWorkstation(FakeRemoteWorkstation):
    def exec_command(self, command: str, timeout: int = 30) -> tuple[str, str, int]:
        self.commands.append(command)
        if " -Uninstall " in command:
            return "uninstalled", "", 0
        if "type" in command and "autofluid_tunnel_ed25519.pub" in command:
            return "", "public key read denied", 1
        return '{"task_exists": true, "ssh_processes": 1, "remote_tunnel_ok": true}', "", 0


class KeyProvisioningRemoteWorkstation(FakeRemoteWorkstation):
    def exec_command(self, command: str, timeout: int = 30) -> tuple[str, str, int]:
        self.commands.append(command)
        if "type" in command and "autofluid_tunnel_ed25519.pub" in command:
            return "ssh-ed25519 AAAATEST autofluid-test-tunnel\r\n", "", 0
        return '{"task_exists": true, "ssh_processes": 1, "remote_tunnel_ok": true}', "", 0


def test_repair_deploys_workstation_owned_tunnel_to_raw_workstation_host(tmp_path: Path) -> None:
    script = tmp_path / "start_workstation_owned_reverse_tunnel.ps1"
    script.write_text("script", encoding="utf-8")
    FakeRemoteWorkstation.instances.clear()
    spec = workstation_tunnel.WorkstationTunnelSpec(
        id="WS-B",
        host="172.17.135.89",
        port=22,
        username="ps",
        password="secret",
        auth_method="password",
        key_filename=None,
        remote_bind_host="127.0.0.1",
        remote_bind_port=2224,
        tunnel_target="ocar",
    )

    result = workstation_tunnel.repair_workstation_tunnel(
        spec,
        script_path=script,
        ssh_factory=FakeRemoteWorkstation,
    )

    assert result["ok"] is True
    remote = FakeRemoteWorkstation.instances[0]
    assert (remote.host, remote.port) == ("172.17.135.89", 22)
    assert remote.uploads == [
        (
            str(script),
            "C:/ProgramData/AutoFluid/tunnel/start_workstation_owned_reverse_tunnel.ps1",
        )
    ]
    install_command = "\n".join(remote.commands)
    assert "-Install" in install_command
    assert "-WorkstationId WS-B" in install_command
    assert "-RemoteBindPort 2224" in install_command
    assert "-TargetHost 127.0.0.1" in install_command
    assert "-TargetPort 22" in install_command
    assert "OwnerMarkerPath" not in install_command
    assert "OwnerPid" not in install_command


def test_repair_falls_back_to_user_install_dir_when_programdata_is_denied(tmp_path: Path) -> None:
    script = tmp_path / "start_workstation_owned_reverse_tunnel.ps1"
    script.write_text("script", encoding="utf-8")
    FakeRemoteWorkstation.instances.clear()
    spec = workstation_tunnel.WorkstationTunnelSpec(
        id="WS-A",
        host="172.17.135.240",
        port=22,
        username="ps",
        password="secret",
        auth_method="password",
        key_filename=None,
        remote_bind_host="127.0.0.1",
        remote_bind_port=2222,
        tunnel_target="ocar",
    )

    result = workstation_tunnel.repair_workstation_tunnel(
        spec,
        script_path=script,
        ssh_factory=ProgramDataDeniedRemoteWorkstation,
    )

    assert result["ok"] is True
    remote = FakeRemoteWorkstation.instances[0]
    assert remote.uploads == [
        (
            str(script),
            "C:/Users/ps/AppData/Local/AutoFluid/tunnel/start_workstation_owned_reverse_tunnel.ps1",
        )
    ]
    all_commands = "\n".join(remote.commands)
    assert "C:\\ProgramData\\AutoFluid\\tunnel" in all_commands
    assert "C:\\Users\\ps\\AppData\\Local\\AutoFluid\\tunnel" in all_commands
    assert "-Install" in all_commands


def test_repair_falls_back_to_user_install_dir_when_programdata_install_is_denied(tmp_path: Path) -> None:
    script = tmp_path / "start_workstation_owned_reverse_tunnel.ps1"
    script.write_text("script", encoding="utf-8")
    FakeRemoteWorkstation.instances.clear()
    spec = workstation_tunnel.WorkstationTunnelSpec(
        id="WS-A",
        host="172.17.135.240",
        port=22,
        username="ps",
        password="secret",
        auth_method="password",
        key_filename=None,
        remote_bind_host="127.0.0.1",
        remote_bind_port=2222,
        tunnel_target="root@39.98.196.94",
    )

    result = workstation_tunnel.repair_workstation_tunnel(
        spec,
        script_path=script,
        ssh_factory=ProgramDataInstallDeniedRemoteWorkstation,
    )

    assert result["ok"] is True
    assert result["install_dir"] == "C:/Users/ps/AppData/Local/AutoFluid/tunnel"
    assert result["lifecycle_fallback"] is True
    remote = FakeRemoteWorkstation.instances[0]
    assert remote.uploads[-1] == (
        str(script),
        "C:/Users/ps/AppData/Local/AutoFluid/tunnel/start_workstation_owned_reverse_tunnel.ps1",
    )
    all_commands = "\n".join(remote.commands)
    assert "C:\\ProgramData\\AutoFluid\\tunnel" in all_commands
    assert "C:\\Users\\ps\\AppData\\Local\\AutoFluid\\tunnel" in all_commands
    assert "schtasks.exe /Create" in all_commands


def test_repair_uses_registry_run_when_schtasks_lifecycle_fallback_is_denied(tmp_path: Path) -> None:
    script = tmp_path / "start_workstation_owned_reverse_tunnel.ps1"
    script.write_text("script", encoding="utf-8")
    FakeRemoteWorkstation.instances.clear()
    spec = workstation_tunnel.WorkstationTunnelSpec(
        id="WS-A",
        host="172.17.135.240",
        port=22,
        username="ps",
        password="secret",
        auth_method="password",
        key_filename=None,
        remote_bind_host="127.0.0.1",
        remote_bind_port=2222,
        tunnel_target="root@39.98.196.94",
    )

    result = workstation_tunnel.repair_workstation_tunnel(
        spec,
        script_path=script,
        ssh_factory=SchTasksDeniedRemoteWorkstation,
    )

    assert result["ok"] is True
    assert result["lifecycle_fallback"] is True
    all_commands = "\n".join(FakeRemoteWorkstation.instances[0].commands)
    assert "schtasks.exe /Create" in all_commands
    assert "reg.exe add" in all_commands
    assert "AutoFluidWorkstationTunnel-WS-A-2222.cmd" in all_commands
    assert 'start "AutoFluidWorkstationTunnel-WS-A-2222"' in all_commands
    assert any(
        remote_path.endswith("AutoFluidWorkstationTunnel-WS-A-2222.cmd")
        for _, remote_path in FakeRemoteWorkstation.instances[0].uploads
    )


def test_repair_uses_credentialed_scheduled_cmd_fallback(tmp_path: Path) -> None:
    script = tmp_path / "start_workstation_owned_reverse_tunnel.ps1"
    script.write_text("script", encoding="utf-8")
    FakeRemoteWorkstation.instances.clear()
    spec = workstation_tunnel.WorkstationTunnelSpec(
        id="WS-A",
        host="172.17.135.240",
        port=22,
        username="ps",
        password="secret",
        auth_method="password",
        key_filename=None,
        remote_bind_host="127.0.0.1",
        remote_bind_port=2222,
        tunnel_target="root@39.98.196.94",
    )

    result = workstation_tunnel.repair_workstation_tunnel(
        spec,
        script_path=script,
        ssh_factory=CredentialedSchTasksRemoteWorkstation,
    )

    assert result["ok"] is True
    assert result["lifecycle_fallback"] is True
    all_commands = "\n".join(FakeRemoteWorkstation.instances[0].commands)
    assert "cmd.exe /d /c whoami" in all_commands
    assert "/RU \"DESKTOP-TD0FQ8U\\ps\"" in all_commands
    assert "/RP \"secret\"" in all_commands
    assert "AutoFluidWorkstationTunnel-WS-A-2222.cmd" in all_commands


def test_monitor_command_quotes_script_paths_with_spaces() -> None:
    spec = workstation_tunnel.WorkstationTunnelSpec(
        id="WS-A",
        host="172.17.135.240",
        port=22,
        username="ps",
        password="secret",
        auth_method="password",
        key_filename=None,
        remote_bind_host="127.0.0.1",
        remote_bind_port=2222,
        tunnel_target="root@39.98.196.94",
        tunnel_identity_file="C:/Users/ps/.ssh/autofluid_tunnel_ed25519",
    )

    command = workstation_tunnel._monitor_command(  # noqa: SLF001
        "C:/Users/ps/AppData/Local/AutoFluid/tunnel/start workstation tunnel.ps1",
        spec,
        "C:/Users/ps/AppData/Local/AutoFluid/tunnel",
    )

    assert '-File "C:\\Users\\ps\\AppData\\Local\\AutoFluid\\tunnel\\start workstation tunnel.ps1"' in command
    assert "-TunnelIdentityFile C:\\Users\\ps\\.ssh\\autofluid_tunnel_ed25519" in command


def test_cmd_supervisor_prefers_colocated_ssh_client() -> None:
    spec = workstation_tunnel.WorkstationTunnelSpec(
        id="WS-A",
        host="172.17.135.240",
        port=22,
        username="ps",
        password="secret",
        auth_method="password",
        key_filename=None,
        remote_bind_host="127.0.0.1",
        remote_bind_port=2222,
        tunnel_target="root@39.98.196.94",
        tunnel_identity_file="C:/Users/ps/.ssh/autofluid_tunnel_ed25519",
    )

    script = workstation_tunnel._render_cmd_supervisor(  # noqa: SLF001
        spec,
        "C:/Users/ps/AppData/Local/AutoFluid/tunnel",
    )

    assert 'set "SSH_EXE=%~dp0ssh.exe"' in script
    assert "setlocal EnableExtensions EnableDelayedExpansion" in script
    assert 'set "SSH_EXE=C:\\Windows\\System32\\OpenSSH\\ssh.exe"' in script
    assert 'set "LOCK_FILE=%LOG_DIR%\\workstation-WS-A-2222-cmd-supervisor.lock"' in script
    assert 'set "REMOTE_PROBE_FAILURE_THRESHOLD=3"' in script
    assert "another supervisor instance already owns" in script
    assert 'call :probe_remote' in script
    assert 'call :clear_remote_forward' in script
    assert "s.recv(4)" in script
    assert "fuser -k 2222/tcp" in script
    assert "remote tunnel probe failed !REMOTE_PROBE_FAILURES!/%REMOTE_PROBE_FAILURE_THRESHOLD%" in script
    assert "if !REMOTE_PROBE_FAILURES! lss %REMOTE_PROBE_FAILURE_THRESHOLD%" in script
    assert "call :kill_ssh" in script
    assert "goto restart_ssh" in script
    assert '-R "%FORWARD_SPEC%" "%TUNNEL_TARGET%"' in script


def test_cmd_supervisor_ssh_source_env_forces_upload(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "ssh.exe"
    source.write_bytes(b"MZ" + (b"\0" * 100_001))
    FakeRemoteWorkstation.instances.clear()
    remote = FakeRemoteWorkstation(
        host="172.17.135.240",
        port=22,
        username="ps",
        password="secret",
    )
    monkeypatch.setenv("AUTOFLUID_WORKSTATION_TUNNEL_SSH_EXE_SOURCE", str(source))

    ok = workstation_tunnel._ensure_cmd_supervisor_ssh_client(  # noqa: SLF001
        remote,
        "C:/Users/ps/AppData/Local/AutoFluid/tunnel",
    )

    assert ok is True
    assert remote.uploads == [
        (
            str(source),
            "C:\\Users\\ps\\AppData\\Local\\AutoFluid\\tunnel\\ssh.exe",
        )
    ]
    assert remote.commands == []


def test_status_uses_cmd_fallback_when_powershell_status_is_denied(monkeypatch) -> None:
    FakeRemoteWorkstation.instances.clear()

    def fake_run(*args, **kwargs) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=args[0], returncode=0, stdout="", stderr="")

    monkeypatch.setattr(workstation_tunnel.subprocess, "run", fake_run)
    spec = workstation_tunnel.WorkstationTunnelSpec(
        id="WS-A",
        host="172.17.135.240",
        port=22,
        username="ps",
        password="secret",
        auth_method="password",
        key_filename=None,
        remote_bind_host="127.0.0.1",
        remote_bind_port=2222,
        tunnel_target="root@39.98.196.94",
    )

    result = workstation_tunnel.status_workstation_tunnel(
        spec,
        ssh_factory=PowerShellStatusDeniedCmdInstalledRemoteWorkstation,
    )

    assert result["ok"] is True
    payload = result["status_payload"]
    assert payload["cmd_supervisor_exists"] is True
    assert payload["registry_run_exists"] is True
    assert payload["remote_tunnel_ok"] is True
    all_commands = "\n".join(FakeRemoteWorkstation.instances[0].commands)
    assert "-Status" in all_commands
    assert "script_exists=1" in all_commands


def test_status_rejects_cmd_fallback_without_owned_ssh_process(monkeypatch) -> None:
    FakeRemoteWorkstation.instances.clear()

    def fake_run(*args, **kwargs) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=args[0], returncode=0, stdout="", stderr="")

    monkeypatch.setattr(workstation_tunnel.subprocess, "run", fake_run)
    spec = workstation_tunnel.WorkstationTunnelSpec(
        id="WS-A",
        host="172.17.135.240",
        port=22,
        username="ps",
        password="secret",
        auth_method="password",
        key_filename=None,
        remote_bind_host="127.0.0.1",
        remote_bind_port=2222,
        tunnel_target="root@39.98.196.94",
    )

    result = workstation_tunnel.status_workstation_tunnel(
        spec,
        ssh_factory=PowerShellStatusDeniedCmdNoSshProcessRemoteWorkstation,
    )

    assert result["ok"] is False
    assert result["status"] == "not_ready"
    assert result["status_payload"]["ssh_processes"] == 0


def test_status_rejects_cmd_fallback_when_banner_probe_fails(monkeypatch) -> None:
    FakeRemoteWorkstation.instances.clear()
    attempts = {"count": 0}

    def fake_run(*args, **kwargs) -> subprocess.CompletedProcess[str]:
        attempts["count"] += 1
        return subprocess.CompletedProcess(args=args[0], returncode=1, stdout="", stderr="probe failed")

    monkeypatch.setattr(workstation_tunnel.subprocess, "run", fake_run)
    spec = workstation_tunnel.WorkstationTunnelSpec(
        id="WS-A",
        host="172.17.135.240",
        port=22,
        username="ps",
        password="secret",
        auth_method="password",
        key_filename=None,
        remote_bind_host="127.0.0.1",
        remote_bind_port=2222,
        tunnel_target="root@39.98.196.94",
    )

    result = workstation_tunnel.status_workstation_tunnel(
        spec,
        ssh_factory=PowerShellStatusDeniedCmdInstalledRemoteWorkstation,
    )

    assert result["ok"] is False
    assert result["status"] == "not_ready"
    assert result["status_payload"]["ssh_processes"] == 1
    assert result["status_payload"]["remote_tunnel_ok"] is False
    assert attempts["count"] == 1


def test_status_retries_transient_banner_probe_reset(monkeypatch) -> None:
    FakeRemoteWorkstation.instances.clear()
    attempts = {"count": 0}

    def fake_run(*args, **kwargs) -> subprocess.CompletedProcess[str]:
        attempts["count"] += 1
        if attempts["count"] == 1:
            return subprocess.CompletedProcess(
                args=args[0],
                returncode=255,
                stdout="",
                stderr="kex_exchange_identification: read: Connection reset",
            )
        return subprocess.CompletedProcess(args=args[0], returncode=0, stdout="", stderr="")

    monkeypatch.setattr(workstation_tunnel.subprocess, "run", fake_run)
    spec = workstation_tunnel.WorkstationTunnelSpec(
        id="WS-A",
        host="172.17.135.240",
        port=22,
        username="ps",
        password="secret",
        auth_method="password",
        key_filename=None,
        remote_bind_host="127.0.0.1",
        remote_bind_port=2222,
        tunnel_target="root@39.98.196.94",
    )

    result = workstation_tunnel.status_workstation_tunnel(
        spec,
        ssh_factory=PowerShellStatusDeniedCmdInstalledRemoteWorkstation,
    )

    assert result["ok"] is True
    assert attempts["count"] == 2


def test_status_fails_after_transient_banner_probe_retries_exhausted(monkeypatch) -> None:
    FakeRemoteWorkstation.instances.clear()
    attempts = {"count": 0}

    def fake_run(*args, **kwargs) -> subprocess.CompletedProcess[str]:
        attempts["count"] += 1
        return subprocess.CompletedProcess(
            args=args[0],
            returncode=255,
            stdout="",
            stderr="kex_exchange_identification: read: Connection reset",
        )

    monkeypatch.setattr(workstation_tunnel.subprocess, "run", fake_run)
    spec = workstation_tunnel.WorkstationTunnelSpec(
        id="WS-A",
        host="172.17.135.240",
        port=22,
        username="ps",
        password="secret",
        auth_method="password",
        key_filename=None,
        remote_bind_host="127.0.0.1",
        remote_bind_port=2222,
        tunnel_target="root@39.98.196.94",
    )

    result = workstation_tunnel.status_workstation_tunnel(
        spec,
        ssh_factory=PowerShellStatusDeniedCmdInstalledRemoteWorkstation,
    )

    assert result["ok"] is False
    assert result["status_payload"]["remote_tunnel_ok"] is False
    assert attempts["count"] == 2


def test_status_does_not_retry_remote_port_connection_refused(monkeypatch) -> None:
    FakeRemoteWorkstation.instances.clear()
    attempts = {"count": 0}

    def fake_run(*args, **kwargs) -> subprocess.CompletedProcess[str]:
        attempts["count"] += 1
        return subprocess.CompletedProcess(
            args=args[0],
            returncode=1,
            stdout="",
            stderr="ConnectionRefusedError: [Errno 111] Connection refused",
        )

    monkeypatch.setattr(workstation_tunnel.subprocess, "run", fake_run)
    spec = workstation_tunnel.WorkstationTunnelSpec(
        id="WS-A",
        host="172.17.135.240",
        port=22,
        username="ps",
        password="secret",
        auth_method="password",
        key_filename=None,
        remote_bind_host="127.0.0.1",
        remote_bind_port=2222,
        tunnel_target="root@39.98.196.94",
    )

    result = workstation_tunnel.status_workstation_tunnel(
        spec,
        ssh_factory=PowerShellStatusDeniedCmdInstalledRemoteWorkstation,
    )

    assert result["ok"] is False
    assert result["status_payload"]["remote_tunnel_ok"] is False
    assert attempts["count"] == 1


def test_status_probe_does_not_use_workstation_local_tunnel_identity_file(monkeypatch) -> None:
    FakeRemoteWorkstation.instances.clear()
    calls: list[list[str]] = []

    def fake_run(*args, **kwargs) -> subprocess.CompletedProcess[str]:
        calls.append(list(args[0]))
        return subprocess.CompletedProcess(args=args[0], returncode=0, stdout="", stderr="")

    monkeypatch.setattr(workstation_tunnel.subprocess, "run", fake_run)
    spec = workstation_tunnel.WorkstationTunnelSpec(
        id="WS-A",
        host="172.17.135.240",
        port=22,
        username="ps",
        password="secret",
        auth_method="password",
        key_filename=None,
        remote_bind_host="127.0.0.1",
        remote_bind_port=2222,
        tunnel_target="root@39.98.196.94",
        tunnel_identity_file="C:/Users/ps/.ssh/autofluid_tunnel_ed25519",
    )

    result = workstation_tunnel.status_workstation_tunnel(
        spec,
        ssh_factory=PowerShellStatusDeniedCmdInstalledRemoteWorkstation,
    )

    assert result["ok"] is True
    assert "-i" not in calls[0]


def test_status_checks_user_dir_when_programdata_cmd_is_missing(monkeypatch) -> None:
    FakeRemoteWorkstation.instances.clear()

    def fake_run(*args, **kwargs) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=args[0], returncode=0, stdout="", stderr="")

    monkeypatch.setattr(workstation_tunnel.subprocess, "run", fake_run)
    spec = workstation_tunnel.WorkstationTunnelSpec(
        id="WS-A",
        host="172.17.135.240",
        port=22,
        username="ps",
        password="secret",
        auth_method="password",
        key_filename=None,
        remote_bind_host="127.0.0.1",
        remote_bind_port=2222,
        tunnel_target="root@39.98.196.94",
    )

    result = workstation_tunnel.status_workstation_tunnel(
        spec,
        ssh_factory=UserDirCmdInstalledRemoteWorkstation,
    )

    assert result["ok"] is True
    assert result["install_dir"] == "C:/Users/ps/AppData/Local/AutoFluid/tunnel"
    all_commands = "\n".join(FakeRemoteWorkstation.instances[0].commands)
    assert "C:\\ProgramData\\AutoFluid\\tunnel" in all_commands
    assert "C:\\Users\\ps\\AppData\\Local\\AutoFluid\\tunnel" in all_commands


def test_repair_generates_and_authorizes_workstation_tunnel_key(tmp_path: Path, monkeypatch) -> None:
    script = tmp_path / "start_workstation_owned_reverse_tunnel.ps1"
    script.write_text("script", encoding="utf-8")
    FakeRemoteWorkstation.instances.clear()
    auth_calls: list[tuple[list[str], str | None]] = []

    def fake_run(*args, **kwargs) -> subprocess.CompletedProcess[str]:
        auth_calls.append((args[0], kwargs.get("input")))
        return subprocess.CompletedProcess(args=args[0], returncode=0, stdout="", stderr="")

    monkeypatch.setattr(workstation_tunnel.subprocess, "run", fake_run)
    spec = workstation_tunnel.WorkstationTunnelSpec(
        id="WS-B",
        host="172.17.135.89",
        port=22,
        username="ps",
        password="secret",
        auth_method="password",
        key_filename=None,
        remote_bind_host="127.0.0.1",
        remote_bind_port=2224,
        tunnel_target="root@39.98.196.94",
        tunnel_identity_file="C:/Users/ps/.ssh/autofluid_tunnel_ed25519",
    )

    result = workstation_tunnel.repair_workstation_tunnel(
        spec,
        script_path=script,
        ssh_factory=KeyProvisioningRemoteWorkstation,
    )

    assert result["ok"] is True
    remote = FakeRemoteWorkstation.instances[0]
    all_commands = "\n".join(remote.commands)
    assert "ssh-keygen.exe -t ed25519" in all_commands
    assert "autofluid_tunnel_ed25519.pub" in all_commands
    assert "-TunnelIdentityFile C:\\Users\\ps\\.ssh\\autofluid_tunnel_ed25519" in all_commands
    assert auth_calls
    assert auth_calls[0][0][:4] == ["ssh", "-o", "StrictHostKeyChecking=accept-new", "root@39.98.196.94"]
    assert len(auth_calls[0][0]) == 5
    assert auth_calls[0][0][4].startswith("python3 -c ")
    assert auth_calls[0][1] == "ssh-ed25519 AAAATEST autofluid-test-tunnel"


def test_uninstall_deauthorizes_and_deletes_workstation_tunnel_key(monkeypatch) -> None:
    FakeRemoteWorkstation.instances.clear()
    auth_calls: list[tuple[list[str], str | None]] = []

    def fake_run(*args, **kwargs) -> subprocess.CompletedProcess[str]:
        auth_calls.append((args[0], kwargs.get("input")))
        return subprocess.CompletedProcess(args=args[0], returncode=0, stdout="", stderr="")

    monkeypatch.setattr(workstation_tunnel.subprocess, "run", fake_run)
    spec = workstation_tunnel.WorkstationTunnelSpec(
        id="WS-B",
        host="172.17.135.89",
        port=22,
        username="ps",
        password="secret",
        auth_method="password",
        key_filename=None,
        remote_bind_host="127.0.0.1",
        remote_bind_port=2224,
        tunnel_target="root@39.98.196.94",
        tunnel_identity_file="C:/Users/ps/.ssh/autofluid_tunnel_ed25519",
    )

    result = workstation_tunnel.uninstall_workstation_tunnel(
        spec,
        ssh_factory=KeyProvisioningRemoteWorkstation,
    )

    assert result["ok"] is True
    assert result["key_cleanup"] == {"ok": True, "deauthorized": True}
    assert result["lifecycle_cleanup"]["ok"] is True
    remote = FakeRemoteWorkstation.instances[0]
    all_commands = "\n".join(remote.commands)
    assert "schtasks.exe /Delete" in all_commands
    assert "reg.exe delete" in all_commands
    assert "taskkill /F" in all_commands
    assert "WINDOWTITLE eq AutoFluidWorkstationTunnel-WS-B-2224" in all_commands
    assert "CommandLine like '%%127.0.0.1:2224:127.0.0.1:22%%'" in all_commands
    assert "autofluid_tunnel_ed25519.pub" in all_commands
    assert "del /q" in all_commands
    assert auth_calls[0][0][:4] == ["ssh", "-o", "StrictHostKeyChecking=accept-new", "root@39.98.196.94"]
    assert len(auth_calls[0][0]) == 5
    assert auth_calls[0][0][4].startswith("python3 -c ")
    assert auth_calls[0][1] == "ssh-ed25519 AAAATEST autofluid-test-tunnel"


def test_repair_reports_mkdir_failed_when_programdata_and_user_dir_are_denied(tmp_path: Path) -> None:
    script = tmp_path / "start_workstation_owned_reverse_tunnel.ps1"
    script.write_text("script", encoding="utf-8")
    FakeRemoteWorkstation.instances.clear()
    spec = workstation_tunnel.WorkstationTunnelSpec(
        id="WS-A",
        host="172.17.135.240",
        port=22,
        username="ps",
        password="secret",
        auth_method="password",
        key_filename=None,
        remote_bind_host="127.0.0.1",
        remote_bind_port=2222,
        tunnel_target="ocar",
    )

    result = workstation_tunnel.repair_workstation_tunnel(
        spec,
        script_path=script,
        ssh_factory=BothDirsDeniedRemoteWorkstation,
    )

    assert result["ok"] is False
    assert result["status"] == "mkdir_failed"
    assert FakeRemoteWorkstation.instances[0].uploads == []


def test_repair_does_not_try_user_fallback_without_username(tmp_path: Path) -> None:
    script = tmp_path / "start_workstation_owned_reverse_tunnel.ps1"
    script.write_text("script", encoding="utf-8")
    FakeRemoteWorkstation.instances.clear()
    spec = workstation_tunnel.WorkstationTunnelSpec(
        id="WS-A",
        host="172.17.135.240",
        port=22,
        username="",
        password="secret",
        auth_method="password",
        key_filename=None,
        remote_bind_host="127.0.0.1",
        remote_bind_port=2222,
        tunnel_target="ocar",
    )

    result = workstation_tunnel.repair_workstation_tunnel(
        spec,
        script_path=script,
        ssh_factory=BothDirsDeniedRemoteWorkstation,
    )

    assert result["ok"] is False
    assert result["status"] == "mkdir_failed"
    remote = FakeRemoteWorkstation.instances[0]
    assert len([command for command in remote.commands if "mkdir" in command]) == 1


def test_status_falls_back_to_user_install_dir_when_programdata_script_is_missing() -> None:
    FakeRemoteWorkstation.instances.clear()
    spec = workstation_tunnel.WorkstationTunnelSpec(
        id="WS-A",
        host="172.17.135.240",
        port=22,
        username="ps",
        password="secret",
        auth_method="password",
        key_filename=None,
        remote_bind_host="127.0.0.1",
        remote_bind_port=2222,
        tunnel_target="ocar",
    )

    result = workstation_tunnel.status_workstation_tunnel(
        spec,
        ssh_factory=ProgramDataMissingRemoteWorkstation,
    )

    assert result["ok"] is True
    assert result["install_dir"] == "C:/Users/ps/AppData/Local/AutoFluid/tunnel"
    remote = FakeRemoteWorkstation.instances[0]
    all_commands = "\n".join(remote.commands)
    assert "C:\\ProgramData\\AutoFluid\\tunnel" in all_commands
    assert "C:\\Users\\ps\\AppData\\Local\\AutoFluid\\tunnel" in all_commands


def test_status_accepts_registry_run_fallback_without_scheduled_task() -> None:
    FakeRemoteWorkstation.instances.clear()
    spec = workstation_tunnel.WorkstationTunnelSpec(
        id="WS-A",
        host="172.17.135.240",
        port=22,
        username="ps",
        password="secret",
        auth_method="password",
        key_filename=None,
        remote_bind_host="127.0.0.1",
        remote_bind_port=2222,
        tunnel_target="root@39.98.196.94",
    )

    result = workstation_tunnel.status_workstation_tunnel(
        spec,
        ssh_factory=RegistryRunStatusRemoteWorkstation,
    )

    assert result["ok"] is True


def test_status_rejects_remote_port_without_owned_ssh_process() -> None:
    FakeRemoteWorkstation.instances.clear()
    spec = workstation_tunnel.WorkstationTunnelSpec(
        id="WS-A",
        host="172.17.135.240",
        port=22,
        username="ps",
        password="secret",
        auth_method="password",
        key_filename=None,
        remote_bind_host="127.0.0.1",
        remote_bind_port=2222,
        tunnel_target="root@39.98.196.94",
    )

    result = workstation_tunnel.status_workstation_tunnel(
        spec,
        ssh_factory=RegistryRunWithoutSshProcessRemoteWorkstation,
    )

    assert result["ok"] is False
    assert result["status"] == "not_ready"


def test_status_reports_failed_when_all_status_paths_are_denied() -> None:
    FakeRemoteWorkstation.instances.clear()
    spec = workstation_tunnel.WorkstationTunnelSpec(
        id="WS-A",
        host="172.17.135.240",
        port=22,
        username="ps",
        password="secret",
        auth_method="password",
        key_filename=None,
        remote_bind_host="127.0.0.1",
        remote_bind_port=2222,
        tunnel_target="root@39.98.196.94",
    )

    result = workstation_tunnel.status_workstation_tunnel(
        spec,
        ssh_factory=BothDirsDeniedRemoteWorkstation,
    )

    assert result["ok"] is False
    assert result["status"] == "status_failed"
    assert result["failed_attempts"]


def test_uninstall_attempts_programdata_and_user_install_dirs() -> None:
    FakeRemoteWorkstation.instances.clear()
    spec = workstation_tunnel.WorkstationTunnelSpec(
        id="WS-A",
        host="172.17.135.240",
        port=22,
        username="ps",
        password="secret",
        auth_method="password",
        key_filename=None,
        remote_bind_host="127.0.0.1",
        remote_bind_port=2222,
        tunnel_target="ocar",
    )

    result = workstation_tunnel.uninstall_workstation_tunnel(
        spec,
        ssh_factory=ProgramDataMissingRemoteWorkstation,
    )

    assert result["ok"] is True
    remote = FakeRemoteWorkstation.instances[0]
    all_commands = "\n".join(remote.commands)
    assert all_commands.count("-Uninstall") == 2
    assert "C:\\ProgramData\\AutoFluid\\tunnel" in all_commands
    assert "C:\\Users\\ps\\AppData\\Local\\AutoFluid\\tunnel" in all_commands


def test_uninstall_reports_failure_when_lifecycle_cleanup_fails() -> None:
    FakeRemoteWorkstation.instances.clear()
    spec = workstation_tunnel.WorkstationTunnelSpec(
        id="WS-A",
        host="172.17.135.240",
        port=22,
        username="ps",
        password="secret",
        auth_method="password",
        key_filename=None,
        remote_bind_host="127.0.0.1",
        remote_bind_port=2222,
        tunnel_target="ocar",
    )

    result = workstation_tunnel.uninstall_workstation_tunnel(
        spec,
        ssh_factory=UninstallOkCleanupDeniedRemoteWorkstation,
    )

    assert result["ok"] is False
    assert result["status"] == "uninstall_failed"
    assert "lifecycle_cleanup" in result["detail"]
    assert "cleanup denied" in result["detail"]


def test_uninstall_reports_failure_when_key_cleanup_fails() -> None:
    FakeRemoteWorkstation.instances.clear()
    spec = workstation_tunnel.WorkstationTunnelSpec(
        id="WS-A",
        host="172.17.135.240",
        port=22,
        username="ps",
        password="secret",
        auth_method="password",
        key_filename=None,
        remote_bind_host="127.0.0.1",
        remote_bind_port=2222,
        tunnel_target="ocar",
        tunnel_identity_file="C:\\Users\\ps\\.ssh\\autofluid_tunnel_ed25519",
    )

    result = workstation_tunnel.uninstall_workstation_tunnel(
        spec,
        ssh_factory=UninstallOkKeyCleanupDeniedRemoteWorkstation,
    )

    assert result["ok"] is False
    assert result["status"] == "uninstall_failed"
    assert "key_cleanup" in result["detail"]
    assert "public key read denied" in result["detail"]


def test_cmd_lifecycle_cleanup_removes_tasks_run_key_processes_and_scripts() -> None:
    FakeRemoteWorkstation.instances.clear()
    remote = FakeRemoteWorkstation(
        host="172.17.135.240",
        port=22,
        username="ps",
        password="secret",
    )
    spec = workstation_tunnel.WorkstationTunnelSpec(
        id="WS-A",
        host="172.17.135.240",
        port=22,
        username="ps",
        password="secret",
        auth_method="password",
        key_filename=None,
        remote_bind_host="127.0.0.1",
        remote_bind_port=2222,
        tunnel_target="root@39.98.196.94",
    )

    result = workstation_tunnel._cleanup_cmd_lifecycle_fallback(  # noqa: SLF001
        remote,
        spec,
        install_dir="C:/ProgramData/AutoFluid/tunnel",
    )

    assert result["ok"] is True
    all_commands = "\n".join(remote.commands)
    assert "schtasks.exe /Delete" in all_commands
    assert "reg.exe delete" in all_commands
    assert "WINDOWTITLE eq AutoFluidWorkstationTunnel-WS-A-2222" in all_commands
    assert "CommandLine like '%%127.0.0.1:2222:127.0.0.1:22%%'" in all_commands
    assert "AutoFluidWorkstationTunnel-WS-A-2222.cmd" in all_commands
    assert "del /q" in all_commands


def test_cmd_lifecycle_cleanup_reports_failure() -> None:
    FakeRemoteWorkstation.instances.clear()
    remote = CleanupDeniedRemoteWorkstation(
        host="172.17.135.240",
        port=22,
        username="ps",
        password="secret",
    )
    spec = workstation_tunnel.WorkstationTunnelSpec(
        id="WS-A",
        host="172.17.135.240",
        port=22,
        username="ps",
        password="secret",
        auth_method="password",
        key_filename=None,
        remote_bind_host="127.0.0.1",
        remote_bind_port=2222,
        tunnel_target="root@39.98.196.94",
    )

    result = workstation_tunnel._cleanup_cmd_lifecycle_fallback(  # noqa: SLF001
        remote,
        spec,
        install_dir="C:/ProgramData/AutoFluid/tunnel",
    )

    assert result == {"ok": False, "detail": "cleanup denied"}


def test_candidate_install_dirs_deduplicates_backslash_fallback_path() -> None:
    candidates = workstation_tunnel._candidate_install_dirs(  # noqa: SLF001
        "C:\\Users\\ps\\AppData\\Local\\AutoFluid\\tunnel",
        "ps",
    )

    assert candidates == ["C:\\Users\\ps\\AppData\\Local\\AutoFluid\\tunnel"]


def test_candidate_install_dirs_skips_user_fallback_without_username() -> None:
    candidates = workstation_tunnel._candidate_install_dirs(  # noqa: SLF001
        "C:/ProgramData/AutoFluid/tunnel",
        "",
    )

    assert candidates == ["C:/ProgramData/AutoFluid/tunnel"]


def test_mkdir_command_uses_idempotent_cmd_mkdir() -> None:
    command = workstation_tunnel._mkdir_command(  # noqa: SLF001
        "C:/Users/ps/AppData/Local/AutoFluid/tunnel"
    )

    assert command == (
        'cmd.exe /d /c if not exist "C:\\Users\\ps\\AppData\\Local\\AutoFluid\\tunnel" '
        'mkdir "C:\\Users\\ps\\AppData\\Local\\AutoFluid\\tunnel"'
    )


def test_mkdir_command_escapes_cmd_percent_expansion() -> None:
    command = workstation_tunnel._mkdir_command("C:/Users/p%s/AutoFluid/tunnel")  # noqa: SLF001

    assert '"C:\\Users\\p^%s\\AutoFluid\\tunnel"' in command


def test_workstation_tunnel_specs_use_reachable_port_but_raw_host_for_bootstrap() -> None:
    workstations = [
        {
            "id": "WS-A",
            "host": "172.17.135.240",
            "port": 22,
            "username": "ps",
            "password": "",
            "auth_method": "none",
            "reachable_host": "127.0.0.1",
            "reachable_port": 2222,
            "connectivity_mode": "reverse_tunnel",
        }
    ]

    specs = workstation_tunnel.specs_from_workstations(workstations)

    assert len(specs) == 1
    assert specs[0].host == "172.17.135.240"
    assert specs[0].port == 22
    assert specs[0].remote_bind_host == "127.0.0.1"
    assert specs[0].remote_bind_port == 2222
    assert specs[0].auth_method == "none"
    assert specs[0].tunnel_identity_file == "C:/Users/ps/.ssh/autofluid_tunnel_ed25519"


def test_workstation_tunnel_specs_can_filter_one_workstation() -> None:
    workstations = [
        {"id": "WS-A", "host": "172.17.135.240", "port": 22},
        {"id": "WS-C", "host": "172.17.135.115", "port": 22, "reachable_port": 2225},
    ]

    specs = workstation_tunnel.specs_from_workstations(
        workstations,
        workstation_id="ws-c",
    )

    assert [spec.id for spec in specs] == ["WS-C"]
    assert specs[0].host == "172.17.135.115"
    assert specs[0].remote_bind_port == 2225


def test_workstation_tunnel_target_prefers_new_target_env(monkeypatch) -> None:
    monkeypatch.setenv("AUTOFLUID_WORKSTATION_TUNNEL_TARGET", "ocar-tunnel")
    monkeypatch.setenv("AUTOFLUID_WORKSTATION_TUNNEL_HOST", "legacy-ocar")

    specs = workstation_tunnel.specs_from_workstations(
        [
            {
                "id": "WS-A",
                "host": "172.17.135.240",
                "port": 22,
                "username": "ps",
            }
        ]
    )

    assert specs[0].tunnel_target == "ocar-tunnel"


def test_default_workstation_tunnel_target_resolves_ssh_alias(monkeypatch) -> None:
    monkeypatch.delenv("AUTOFLUID_WORKSTATION_TUNNEL_TARGET", raising=False)
    monkeypatch.delenv("AUTOFLUID_WORKSTATION_TUNNEL_HOST", raising=False)

    def fake_run(*args, **kwargs) -> subprocess.CompletedProcess[str]:
        assert args[0] == ["ssh", "-G", "ocar"]
        return subprocess.CompletedProcess(
            args=args[0],
            returncode=0,
            stdout="user root\nhostname 39.98.196.94\nport 22\n",
            stderr="",
        )

    monkeypatch.setattr(workstation_tunnel.subprocess, "run", fake_run)

    specs = workstation_tunnel.specs_from_workstations(
        [
            {
                "id": "WS-A",
                "host": "172.17.135.240",
                "port": 22,
                "username": "ps",
            }
        ]
    )

    assert specs[0].tunnel_target == "root@39.98.196.94"


def test_default_workstation_tunnel_target_ignores_nonstandard_ssh_config_port(monkeypatch) -> None:
    monkeypatch.delenv("AUTOFLUID_WORKSTATION_TUNNEL_TARGET", raising=False)
    monkeypatch.delenv("AUTOFLUID_WORKSTATION_TUNNEL_HOST", raising=False)

    def fake_run(*args, **kwargs) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=args[0],
            returncode=0,
            stdout="user root\nhostname 203.0.113.10\nport 22222\n",
            stderr="",
        )

    monkeypatch.setattr(workstation_tunnel.subprocess, "run", fake_run)

    specs = workstation_tunnel.specs_from_workstations(
        [
            {
                "id": "WS-A",
                "host": "172.17.135.240",
                "port": 22,
                "username": "ps",
            }
        ]
    )

    assert specs[0].tunnel_target == "root@203.0.113.10"


def test_default_workstation_tunnel_target_falls_back_when_ssh_config_lookup_fails(monkeypatch) -> None:
    monkeypatch.delenv("AUTOFLUID_WORKSTATION_TUNNEL_TARGET", raising=False)
    monkeypatch.delenv("AUTOFLUID_WORKSTATION_TUNNEL_HOST", raising=False)

    def fake_run(*args, **kwargs) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=args[0], returncode=255, stdout="", stderr="no host")

    monkeypatch.setattr(workstation_tunnel.subprocess, "run", fake_run)

    specs = workstation_tunnel.specs_from_workstations(
        [
            {
                "id": "WS-A",
                "host": "172.17.135.240",
                "port": 22,
                "username": "ps",
            }
        ]
    )

    assert specs[0].tunnel_target == "ocar"


def test_default_workstation_tunnel_target_falls_back_when_hostname_is_alias(monkeypatch) -> None:
    monkeypatch.delenv("AUTOFLUID_WORKSTATION_TUNNEL_TARGET", raising=False)
    monkeypatch.delenv("AUTOFLUID_WORKSTATION_TUNNEL_HOST", raising=False)

    def fake_run(*args, **kwargs) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=args[0],
            returncode=0,
            stdout="user ps\nhostname ocar\nport 22\n",
            stderr="",
        )

    monkeypatch.setattr(workstation_tunnel.subprocess, "run", fake_run)

    specs = workstation_tunnel.specs_from_workstations(
        [
            {
                "id": "WS-A",
                "host": "172.17.135.240",
                "port": 22,
                "username": "ps",
            }
        ]
    )

    assert specs[0].tunnel_target == "ocar"


def test_default_workstation_tunnel_target_falls_back_when_ssh_is_unavailable(monkeypatch) -> None:
    monkeypatch.delenv("AUTOFLUID_WORKSTATION_TUNNEL_TARGET", raising=False)
    monkeypatch.delenv("AUTOFLUID_WORKSTATION_TUNNEL_HOST", raising=False)

    def fake_run(*args, **kwargs) -> subprocess.CompletedProcess[str]:
        raise OSError("ssh unavailable")

    monkeypatch.setattr(workstation_tunnel.subprocess, "run", fake_run)

    specs = workstation_tunnel.specs_from_workstations(
        [
            {
                "id": "WS-A",
                "host": "172.17.135.240",
                "port": 22,
                "username": "ps",
            }
        ]
    )

    assert specs[0].tunnel_target == "ocar"


def test_explicit_workstation_tunnel_target_is_not_resolved(monkeypatch) -> None:
    def fail_run(*args, **kwargs) -> subprocess.CompletedProcess[str]:
        raise AssertionError("explicit tunnel target should not call ssh -G")

    monkeypatch.setattr(workstation_tunnel.subprocess, "run", fail_run)

    specs = workstation_tunnel.specs_from_workstations(
        [
            {
                "id": "WS-A",
                "host": "172.17.135.240",
                "port": 22,
                "username": "ps",
            }
        ],
        tunnel_target="root@198.51.100.10",
    )

    assert specs[0].tunnel_target == "root@198.51.100.10"


def _test_tunnel_spec(ws_id: str, port: int) -> workstation_tunnel.WorkstationTunnelSpec:
    return workstation_tunnel.WorkstationTunnelSpec(
        id=ws_id,
        host="172.17.135.240",
        port=22,
        username="ps",
        password="secret",
        auth_method="password",
        key_filename=None,
        remote_bind_host="127.0.0.1",
        remote_bind_port=port,
        tunnel_target="ocar",
    )


def test_ensure_skips_repair_when_status_is_ready(monkeypatch) -> None:
    spec = _test_tunnel_spec("WS-A", 2222)
    repair_calls: list[str] = []

    monkeypatch.setattr(
        workstation_tunnel,
        "status_workstation_tunnel",
        lambda spec, install_dir=workstation_tunnel.DEFAULT_INSTALL_DIR: {
            "id": spec.id,
            "ok": True,
            "status": "ok",
        },
    )
    monkeypatch.setattr(
        workstation_tunnel,
        "repair_workstation_tunnel",
        lambda spec, install_dir=workstation_tunnel.DEFAULT_INSTALL_DIR: repair_calls.append(spec.id),
    )

    result = workstation_tunnel.ensure_workstation_tunnel(spec)

    assert result["ok"] is True
    assert result["ensure_action"] == "skipped"
    assert repair_calls == []


def test_status_marks_running_monitor_without_ssh_and_stale_heartbeat_as_wedged() -> None:
    spec = _test_tunnel_spec("WS-A", 2222)
    FakeRemoteWorkstation.instances.clear()

    result = workstation_tunnel.status_workstation_tunnel(
        spec,
        ssh_factory=WedgedMonitorStatusRemoteWorkstation,
    )

    payload = result["status_payload"]
    assert result["ok"] is False
    assert result["status"] == "not_ready"
    assert payload["monitor_last_seen_at"] == "2000-01-01T00:00:00Z"
    assert payload["monitor_state"] == "loop"
    assert payload["monitor_last_reason"] == "remote-probe-timeout"
    assert payload["monitor_wedged"] is True


def test_monitor_health_helpers_handle_fresh_stale_and_invalid_values() -> None:
    fresh = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    stale = "2000-01-01T00:00:00.123456789Z"

    assert workstation_tunnel._as_bool(True) is True
    assert workstation_tunnel._as_bool("true") is True
    assert workstation_tunnel._as_bool("1") is True
    assert workstation_tunnel._as_bool(False) is False
    assert workstation_tunnel._as_bool("false") is False
    assert workstation_tunnel._as_bool("ready") is False
    assert workstation_tunnel._as_bool(None) is False
    assert workstation_tunnel._as_int("5") == 5
    assert workstation_tunnel._as_int("") == 0
    assert workstation_tunnel._as_int("not-an-int") == 0
    assert workstation_tunnel._parse_monitor_timestamp(fresh) is not None
    assert workstation_tunnel._parse_monitor_timestamp(stale) is not None
    assert workstation_tunnel._parse_monitor_timestamp("") is None
    assert workstation_tunnel._parse_monitor_timestamp(None) is None
    assert workstation_tunnel._parse_monitor_timestamp("not-a-timestamp") is None


def test_monitor_health_requires_all_wedged_conditions() -> None:
    fresh = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat().replace("+00:00", "Z")
    stale = "2000-01-01T00:00:00Z"

    cases = [
        (
            {
                "monitor_processes": 1,
                "ssh_processes": 0,
                "remote_tunnel_ok": False,
                "monitor_last_seen_at": stale,
            },
            True,
        ),
        (
            {
                "monitor_processes": 1,
                "ssh_processes": 1,
                "remote_tunnel_ok": False,
                "monitor_last_seen_at": stale,
            },
            False,
        ),
        (
            {
                "monitor_processes": 1,
                "ssh_processes": 0,
                "remote_tunnel_ok": True,
                "monitor_last_seen_at": stale,
            },
            False,
        ),
        (
            {
                "monitor_processes": 1,
                "ssh_processes": 0,
                "remote_tunnel_ok": False,
                "monitor_last_seen_at": fresh,
            },
            False,
        ),
    ]

    for payload, expected in cases:
        assert workstation_tunnel._annotate_monitor_health(payload)["monitor_wedged"] is expected


def test_status_uses_timeout_that_covers_remote_probe_budget() -> None:
    spec = _test_tunnel_spec("WS-A", 2222)
    StatusTimeoutRecordingRemoteWorkstation.instances.clear()

    result = workstation_tunnel.status_workstation_tunnel(
        spec,
        ssh_factory=StatusTimeoutRecordingRemoteWorkstation,
    )

    remote = StatusTimeoutRecordingRemoteWorkstation.instances[0]
    assert result["ok"] is True
    assert remote.timeouts == [workstation_tunnel.STATUS_COMMAND_TIMEOUT_SECONDS]


def test_ensure_repairs_when_monitor_is_wedged(monkeypatch) -> None:
    spec = _test_tunnel_spec("WS-A", 2222)
    repair_calls: list[str] = []
    status_calls = 0

    def fake_status(
        spec: workstation_tunnel.WorkstationTunnelSpec,
        install_dir: str = workstation_tunnel.DEFAULT_INSTALL_DIR,
    ) -> dict:
        nonlocal status_calls
        status_calls += 1
        if status_calls == 1:
            return {
                "id": spec.id,
                "ok": False,
                "status": "not_ready",
                "status_payload": {
                    "task_exists": True,
                    "task_state": "Running",
                    "monitor_processes": 1,
                    "ssh_processes": 0,
                    "remote_tunnel_ok": False,
                    "monitor_wedged": True,
                },
            }
        return {"id": spec.id, "ok": True, "status": "ok"}

    def fake_repair(
        spec: workstation_tunnel.WorkstationTunnelSpec,
        install_dir: str = workstation_tunnel.DEFAULT_INSTALL_DIR,
    ) -> dict:
        repair_calls.append(spec.id)
        return {"id": spec.id, "ok": True, "status": "installed"}

    monkeypatch.setattr(workstation_tunnel, "status_workstation_tunnel", fake_status)
    monkeypatch.setattr(workstation_tunnel, "repair_workstation_tunnel", fake_repair)

    result = workstation_tunnel.ensure_workstation_tunnel(spec)

    assert result["ok"] is True
    assert result["ensure_action"] == "repaired"
    assert result["initial_status"]["status_payload"]["monitor_wedged"] is True
    assert repair_calls == ["WS-A"]


def test_ensure_repairs_ready_tunnel_when_owned_script_needs_update(monkeypatch) -> None:
    spec = _test_tunnel_spec("WS-A", 2222)
    repair_calls: list[str] = []
    status_calls = 0

    def fake_status(
        spec: workstation_tunnel.WorkstationTunnelSpec,
        install_dir: str = workstation_tunnel.DEFAULT_INSTALL_DIR,
    ) -> dict:
        nonlocal status_calls
        status_calls += 1
        return {
            "id": spec.id,
            "ok": True,
            "status": "ok",
            "status_payload": {
                "script_needs_update": status_calls == 1,
                "monitor_wedged": False,
            },
        }

    def fake_repair(
        spec: workstation_tunnel.WorkstationTunnelSpec,
        install_dir: str = workstation_tunnel.DEFAULT_INSTALL_DIR,
    ) -> dict:
        repair_calls.append(spec.id)
        return {"id": spec.id, "ok": True, "status": "installed"}

    monkeypatch.setattr(workstation_tunnel, "status_workstation_tunnel", fake_status)
    monkeypatch.setattr(workstation_tunnel, "repair_workstation_tunnel", fake_repair)

    result = workstation_tunnel.ensure_workstation_tunnel(spec)

    assert result["ok"] is True
    assert result["ensure_action"] == "repaired"
    assert result["initial_status"]["status_payload"]["script_needs_update"] is True
    assert repair_calls == ["WS-A"]


def test_ensure_repairs_only_not_ready_workstations(monkeypatch) -> None:
    specs = [_test_tunnel_spec("WS-A", 2222), _test_tunnel_spec("WS-B", 2224)]
    status_calls: list[str] = []
    repair_calls: list[str] = []

    def fake_status(
        spec: workstation_tunnel.WorkstationTunnelSpec,
        install_dir: str = workstation_tunnel.DEFAULT_INSTALL_DIR,
    ) -> dict:
        status_calls.append(spec.id)
        if spec.id == "WS-A":
            return {"id": spec.id, "ok": True, "status": "ok"}
        return {
            "id": spec.id,
            "ok": status_calls.count(spec.id) > 1,
            "status": "ok" if status_calls.count(spec.id) > 1 else "not_ready",
        }

    def fake_repair(
        spec: workstation_tunnel.WorkstationTunnelSpec,
        install_dir: str = workstation_tunnel.DEFAULT_INSTALL_DIR,
    ) -> dict:
        repair_calls.append(spec.id)
        return {"id": spec.id, "ok": True, "status": "installed"}

    monkeypatch.setattr(workstation_tunnel, "status_workstation_tunnel", fake_status)
    monkeypatch.setattr(workstation_tunnel, "repair_workstation_tunnel", fake_repair)

    results = workstation_tunnel.run_for_specs("ensure", specs, jobs=2)

    assert [result["ensure_action"] for result in results] == ["skipped", "repaired"]
    assert repair_calls == ["WS-B"]


def test_ensure_reports_repair_failed_with_initial_status(monkeypatch) -> None:
    spec = _test_tunnel_spec("WS-A", 2222)

    monkeypatch.setattr(
        workstation_tunnel,
        "status_workstation_tunnel",
        lambda spec, install_dir=workstation_tunnel.DEFAULT_INSTALL_DIR: {
            "id": spec.id,
            "ok": False,
            "status": "not_ready",
        },
    )
    monkeypatch.setattr(
        workstation_tunnel,
        "repair_workstation_tunnel",
        lambda spec, install_dir=workstation_tunnel.DEFAULT_INSTALL_DIR: {
            "id": spec.id,
            "ok": False,
            "status": "repair_failed",
        },
    )

    result = workstation_tunnel.ensure_workstation_tunnel(spec)

    assert result["ok"] is False
    assert result["ensure_action"] == "repair_failed"
    assert result["initial_status"]["status"] == "not_ready"


def test_ensure_reports_repair_not_ready_after_final_status_failure(monkeypatch) -> None:
    spec = _test_tunnel_spec("WS-A", 2222)
    status_calls = 0

    def fake_status(
        spec: workstation_tunnel.WorkstationTunnelSpec,
        install_dir: str = workstation_tunnel.DEFAULT_INSTALL_DIR,
    ) -> dict:
        nonlocal status_calls
        status_calls += 1
        return {
            "id": spec.id,
            "ok": False,
            "status": "not_ready" if status_calls == 1 else "status_failed",
        }

    monkeypatch.setattr(workstation_tunnel, "status_workstation_tunnel", fake_status)
    monkeypatch.setattr(
        workstation_tunnel,
        "repair_workstation_tunnel",
        lambda spec, install_dir=workstation_tunnel.DEFAULT_INSTALL_DIR: {
            "id": spec.id,
            "ok": True,
            "status": "installed",
        },
    )

    result = workstation_tunnel.ensure_workstation_tunnel(spec)

    assert result["ok"] is False
    assert result["ensure_action"] == "repair_not_ready"
    assert result["initial_status"]["status"] == "not_ready"
    assert result["repair_result"]["status"] == "installed"


def test_parallel_run_for_specs_preserves_configured_order(monkeypatch) -> None:
    specs = [
        _test_tunnel_spec("WS-A", 2222),
        _test_tunnel_spec("WS-B", 2224),
        _test_tunnel_spec("WS-C", 2225),
        _test_tunnel_spec("WS-D", 2226),
    ]
    delays = {"WS-A": 0.04, "WS-B": 0.03, "WS-C": 0.02, "WS-D": 0.01}

    def fake_status(
        spec: workstation_tunnel.WorkstationTunnelSpec,
        install_dir: str = workstation_tunnel.DEFAULT_INSTALL_DIR,
    ) -> dict:
        time.sleep(delays[spec.id])
        return {"id": spec.id, "ok": True, "status": "ok"}

    monkeypatch.setattr(workstation_tunnel, "status_workstation_tunnel", fake_status)

    results = workstation_tunnel.run_for_specs("status", specs, jobs=4)

    assert [result["id"] for result in results] == ["WS-A", "WS-B", "WS-C", "WS-D"]


def test_parallel_run_for_specs_preserves_results_when_one_worker_raises(monkeypatch) -> None:
    specs = [
        _test_tunnel_spec("WS-A", 2222),
        _test_tunnel_spec("WS-B", 2224),
        _test_tunnel_spec("WS-C", 2225),
    ]

    def fake_status(
        spec: workstation_tunnel.WorkstationTunnelSpec,
        install_dir: str = workstation_tunnel.DEFAULT_INSTALL_DIR,
    ) -> dict:
        if spec.id == "WS-B":
            raise KeyError("ssh state missing")
        return {"id": spec.id, "ok": True, "status": "ok"}

    monkeypatch.setattr(workstation_tunnel, "status_workstation_tunnel", fake_status)

    results = workstation_tunnel.run_for_specs("status", specs, jobs=3)

    assert [result["id"] for result in results] == ["WS-A", "WS-B", "WS-C"]
    assert [result["ok"] for result in results] == [True, False, True]
    assert results[1]["status"] == "exception"
    assert "ssh state missing" in results[1]["detail"]


def test_sequential_run_for_specs_preserves_results_when_one_worker_raises(monkeypatch) -> None:
    specs = [_test_tunnel_spec("WS-A", 2222), _test_tunnel_spec("WS-B", 2224)]

    def fake_status(
        spec: workstation_tunnel.WorkstationTunnelSpec,
        install_dir: str = workstation_tunnel.DEFAULT_INSTALL_DIR,
    ) -> dict:
        if spec.id == "WS-A":
            raise RuntimeError("host offline")
        return {"id": spec.id, "ok": True, "status": "ok"}

    monkeypatch.setattr(workstation_tunnel, "status_workstation_tunnel", fake_status)

    results = workstation_tunnel.run_for_specs("status", specs, jobs=1)

    assert [result["id"] for result in results] == ["WS-A", "WS-B"]
    assert results[0]["ok"] is False
    assert results[0]["status"] == "exception"
    assert results[1]["ok"] is True


def test_progress_jsonl_writer_writes_only_stderr(capsys) -> None:
    writer = workstation_tunnel.jsonl_progress_writer()

    writer({"stage": "status_start", "id": "WS-A"})

    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err) == {"stage": "status_start", "id": "WS-A"}
