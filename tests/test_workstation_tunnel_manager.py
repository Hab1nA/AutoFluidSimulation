from __future__ import annotations

import subprocess
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


class ProgramDataMissingRemoteWorkstation(FakeRemoteWorkstation):
    def exec_command(self, command: str, timeout: int = 30) -> tuple[str, str, int]:
        self.commands.append(command)
        if "C:\\ProgramData\\AutoFluid\\tunnel" in command:
            return "", "script not found", 1
        return '{"task_exists": true, "remote_tunnel_ok": true}', "", 0


class BothDirsDeniedRemoteWorkstation(FakeRemoteWorkstation):
    def exec_command(self, command: str, timeout: int = 30) -> tuple[str, str, int]:
        self.commands.append(command)
        return "", "拒绝访问。", 1


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
    assert len([command for command in remote.commands if "New-Item" in command]) == 1


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
