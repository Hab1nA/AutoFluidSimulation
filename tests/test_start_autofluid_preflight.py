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
    assert "logs/server/services/daemon-bootstrap/autofluid-daemon.out" in function_body
    assert "tail -n 80 logs/server/services/daemon-bootstrap/autofluid-daemon.out" in function_body


def test_server_ipc_tunnel_reuse_requires_protocol_probe() -> None:
    content = Path("scripts/start_server_ipc_tunnel.ps1").read_text(encoding="utf-8")

    assert "if (Test-AutoFluidIpcProtocolEndpoint)" in content
    assert "AutoFluid server IPC protocol endpoint is already reachable" in content
    assert "if (Test-AutoFluidEndpoint)" not in content


def test_server_ipc_tunnel_persists_pid_for_cleanup() -> None:
    content = Path("scripts/start_server_ipc_tunnel.ps1").read_text(encoding="utf-8")

    assert "server_ipc_tunnel.pid" in content
    assert "Set-Content -LiteralPath $pidFile" in content


def test_server_ipc_tunnel_prefers_structured_log_dir_with_temp_fallback() -> None:
    content = Path("scripts/start_server_ipc_tunnel.ps1").read_text(encoding="utf-8")

    assert "logs/local/tunnels/server-ipc" in content
    assert "[System.IO.Path]::GetTempPath()" in content


def test_server_ipc_tunnel_reuse_refreshes_pid_for_cleanup() -> None:
    content = Path("scripts/start_server_ipc_tunnel.ps1").read_text(encoding="utf-8")
    reuse_block = content[
        content.rindex("if (Test-AutoFluidIpcProtocolEndpoint)"):
        content.rindex("$process = Start-ServerTunnel")
    ]

    assert "Update-ServerTunnelPidFile" in reuse_block
    assert "Get-NetTCPConnection" in content
    assert "OwningProcess" in content
    assert "expectedForward" in content
    assert "CommandLine -like \"*-L $expectedForward*\"" in content
    assert "CommandLine -like \"* $TunnelTarget*\"" in content
    assert "matchingPids[0]" not in content

def test_server_ipc_tunnel_runs_monitor_and_records_monitor_pid() -> None:
    content = Path("scripts/start_server_ipc_tunnel.ps1").read_text(encoding="utf-8")

    assert "[switch]$Monitor" in content
    assert "function Start-ServerTunnelMonitor" in content
    assert "Start-ServerTunnelMonitor" in content
    assert "-Monitor" in content
    assert "Update-ServerTunnelPidFile -TunnelPid $PID" in content
    monitor_fn = content[content.index("function Start-ServerTunnelMonitorProcess"): content.index("function Get-ServerTunnelListeningPid")]
    assert "Update-ServerTunnelPidFile -TunnelPid $process.Id" in monitor_fn
    assert "if (-not $SkipPidFile)" not in monitor_fn
    start_fn = content[content.index("function Start-ServerTunnel {"): content.index("function Update-ServerTunnelPidFile")]
    assert "[switch]$SkipPidFile" in start_fn
    assert "if (-not $SkipPidFile)" in start_fn
    assert "-SkipPidFile" in content
    assert "Server IPC tunnel endpoint dropped; restarting" in content

def test_server_ipc_tunnel_reuse_path_still_starts_monitor_by_default() -> None:
    content = Path("scripts/start_server_ipc_tunnel.ps1").read_text(encoding="utf-8")
    reuse_start = content.rindex("if (Test-AutoFluidIpcProtocolEndpoint)")
    default_monitor_start = content.index("if (-not $NoMonitor)", reuse_start)
    one_shot_start = content.index("$process = Start-ServerTunnel", default_monitor_start)
    reuse_block = content[reuse_start:default_monitor_start]
    default_monitor_block = content[default_monitor_start:one_shot_start]

    assert "if ($NoMonitor)" in reuse_block
    assert "exit 0" in reuse_block
    assert "if (-not $NoMonitor)" not in reuse_block
    assert "Start-ServerTunnelMonitorProcess" in default_monitor_block
    assert "Get-ServerTunnelMonitorPid" in default_monitor_block
    assert default_monitor_block.index("Get-ServerTunnelMonitorPid") < default_monitor_block.index(
        "Start-ServerTunnelMonitorProcess"
    )
    assert "exit 0" in default_monitor_block
