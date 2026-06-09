"""Local Windows worker registration client for split-daemon deployments."""
from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import os
import socket
import time
from typing import Any
from urllib.error import URLError
from urllib.request import urlopen

from ipc.protocol import (
    CMD_WORKER_HEARTBEAT,
    CMD_WORKER_REGISTER,
    create_request,
    deserialize,
    serialize,
)


DEFAULT_PUBLIC_IP_URL = "https://api.ipify.org"


@dataclass(frozen=True)
class LocalWorkerConfig:
    """Runtime configuration for a LocalWorker process."""

    worker_id: str
    server_host: str
    server_port: int
    auth_token: str = ""
    capabilities: dict[str, Any] = field(default_factory=dict)
    network: dict[str, Any] = field(default_factory=dict)
    heartbeat_interval: float = 30.0
    request_timeout: float = 10.0


class LocalWorker:
    """Minimal LocalWorker that registers with the remote daemon and heartbeats."""

    def __init__(self, config: LocalWorkerConfig) -> None:
        self.config = config

    @classmethod
    def from_env(cls) -> "LocalWorker":
        """Build a LocalWorker from environment variables."""
        worker_id = os.environ.get("AUTOFLUID_WORKER_ID") or socket.gethostname()
        server_host = (
            os.environ.get("AUTOFLUID_SERVER_HOST")
            or os.environ.get("AUTOFLUID_IPC_HOST")
            or "127.0.0.1"
        )
        server_port = int(os.environ.get("AUTOFLUID_IPC_PORT", "9527"))
        auth_token = os.environ.get("AUTOFLUID_IPC_AUTH_TOKEN", "")
        capabilities = {
            "sw": True,
            "sc": True,
            "sc_slots": int(os.environ.get("AUTOFLUID_WORKER_SC_SLOTS", "3")),
        }
        return cls(
            LocalWorkerConfig(
                worker_id=worker_id,
                server_host=server_host,
                server_port=server_port,
                auth_token=auth_token,
                capabilities=capabilities,
                network=build_worker_network(),
            )
        )

    def build_register_request(self) -> dict[str, Any]:
        """Return the IPC request used to register this LocalWorker."""
        return create_request(
            CMD_WORKER_REGISTER,
            {
                "worker_id": self.config.worker_id,
                "capabilities": dict(self.config.capabilities),
                "network": dict(self.config.network),
            },
            auth_token=self.config.auth_token,
        )

    def build_heartbeat_request(self) -> dict[str, Any]:
        """Return the IPC request used to refresh this LocalWorker heartbeat."""
        return create_request(
            CMD_WORKER_HEARTBEAT,
            {"worker_id": self.config.worker_id},
            auth_token=self.config.auth_token,
        )

    def register_once(self) -> dict[str, Any]:
        """Register this worker with the daemon once."""
        return self._send_request(self.build_register_request())

    def heartbeat_once(self) -> dict[str, Any]:
        """Send one heartbeat to the daemon."""
        return self._send_request(self.build_heartbeat_request())

    def run_forever(self) -> None:
        """Register once, then keep sending heartbeats until interrupted."""
        self.register_once()
        while True:
            time.sleep(self.config.heartbeat_interval)
            self.heartbeat_once()

    def _send_request(self, request: dict[str, Any]) -> dict[str, Any]:
        with socket.create_connection(
            (self.config.server_host, self.config.server_port),
            timeout=self.config.request_timeout,
        ) as sock:
            sock.sendall(serialize(request))
            response = sock.recv(1024 * 1024)
        decoded = deserialize(response)
        if decoded is None:
            raise RuntimeError("LocalWorker 收到无法解析的 IPC 响应")
        return decoded


def build_worker_network() -> dict[str, Any]:
    """Collect local network metadata for daemon-side diagnostics."""
    network: dict[str, Any] = {}
    reachable_host = os.environ.get("AUTOFLUID_WORKER_REACHABLE_HOST", "").strip()
    connectivity_mode = os.environ.get("AUTOFLUID_WORKER_CONNECTIVITY_MODE", "").strip()
    ssh_port = int(os.environ.get("AUTOFLUID_WORKER_SSH_PORT", "22"))
    candidate_hosts = _candidate_hosts_from_env() + detect_candidate_hosts()
    candidate_hosts = list(dict.fromkeys(host for host in candidate_hosts if host))

    public_ip = os.environ.get("AUTOFLUID_WORKER_PUBLIC_IP", "").strip()
    if not public_ip and os.environ.get("AUTOFLUID_DISCOVER_PUBLIC_IP", "1") != "0":
        public_ip = discover_public_ip()

    if public_ip:
        network["public_ip"] = public_ip
    if candidate_hosts:
        network["candidate_hosts"] = candidate_hosts
    if reachable_host:
        network["reachable_host"] = reachable_host
    if connectivity_mode:
        network["connectivity_mode"] = connectivity_mode
    network["ssh_port"] = ssh_port
    return network


def detect_candidate_hosts() -> list[str]:
    """Return local interface addresses useful for operator diagnostics."""
    hosts: list[str] = []
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            hosts.append(sock.getsockname()[0])
    except OSError:
        pass
    try:
        _hostname, _aliases, addresses = socket.gethostbyname_ex(socket.gethostname())
        hosts.extend(addresses)
    except OSError:
        pass
    return list(dict.fromkeys(host for host in hosts if not host.startswith("127.")))


def discover_public_ip(timeout: float = 3.0) -> str:
    """Best-effort public IP discovery for diagnostics."""
    url = os.environ.get("AUTOFLUID_PUBLIC_IP_URL", DEFAULT_PUBLIC_IP_URL)
    try:
        with urlopen(url, timeout=timeout) as response:
            public_ip: str = response.read(128).decode("utf-8").strip()
            return public_ip
    except (OSError, URLError):
        return ""


def _candidate_hosts_from_env() -> list[str]:
    raw = os.environ.get("AUTOFLUID_WORKER_CANDIDATE_HOSTS", "")
    return [item.strip() for item in raw.split(",") if item.strip()]


def main() -> int:
    """Run the LocalWorker registration client."""
    parser = argparse.ArgumentParser(description="Run AutoFluid LocalWorker heartbeat client")
    parser.add_argument("--once", action="store_true", help="register once and send one heartbeat")
    args = parser.parse_args()
    worker = LocalWorker.from_env()
    worker.register_once()
    if args.once:
        worker.heartbeat_once()
        return 0
    worker.run_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
