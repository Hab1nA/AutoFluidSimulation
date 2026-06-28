from __future__ import annotations

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
