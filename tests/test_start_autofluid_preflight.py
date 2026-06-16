from __future__ import annotations

from pathlib import Path


def test_preflight_default_server_daemon_command_waits_for_ipc_readiness() -> None:
    content = Path("scripts/start_autofluid_preflight.ps1").read_text(encoding="utf-8")
    marker = "function Get-AutoFluidServerDaemonStartCommand"
    function_body = content[content.index(marker): content.index("function Start-AutoFluidServerDaemon")]

    assert "setsid -f" not in function_body
    assert "nohup .venv/bin/python start_daemon.py" in function_body
    assert "&& { env AUTOFLUID_SERVER_MODE=server nohup" in function_body
    assert "daemon_pid=`$!" in function_body
    assert "Get-AutoFluidServerDaemonIpcPort" in content
    assert "AUTOFLUID_SERVER_TUNNEL_REMOTE_PORT" in content
    assert "Get-AutoFluidServerPort" not in function_body
    assert "get_engine_status" in function_body
    assert "ready_count" in function_body
    assert "AutoFluid daemon IPC ready" in function_body
    assert "tail -n 80 logs/autofluid-daemon.out" in function_body


def test_server_ipc_tunnel_reuse_requires_protocol_probe() -> None:
    content = Path("scripts/start_server_ipc_tunnel.ps1").read_text(encoding="utf-8")

    assert "if (Test-AutoFluidIpcProtocolEndpoint)" in content
    assert "AutoFluid server IPC protocol endpoint is already reachable" in content
    assert "if (Test-AutoFluidEndpoint)" not in content


def test_server_ipc_tunnel_persists_pid_for_cleanup() -> None:
    content = Path("scripts/start_server_ipc_tunnel.ps1").read_text(encoding="utf-8")

    assert "server_ipc_tunnel.pid" in content
    assert "Set-Content -LiteralPath $pidFile" in content


def test_server_ipc_tunnel_reuse_refreshes_pid_for_cleanup() -> None:
    content = Path("scripts/start_server_ipc_tunnel.ps1").read_text(encoding="utf-8")
    reuse_block = content[
        content.index("if (Test-AutoFluidIpcProtocolEndpoint)"):
        content.index("$process = Start-ServerTunnel")
    ]

    assert "Update-ServerTunnelPidFile" in reuse_block
    assert "Get-NetTCPConnection" in content
    assert "OwningProcess" in content
    assert "expectedForward" in content
    assert "CommandLine -like \"*-L $expectedForward*\"" in content
    assert "CommandLine -like \"* $TunnelTarget*\"" in content
    assert "matchingPids[0]" not in content
