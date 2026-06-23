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


def test_preflight_does_not_touch_worker_owned_reverse_tunnels_by_default() -> None:
    content = Path("scripts/start_autofluid_preflight.ps1").read_text(encoding="utf-8")

    execution_block = content[content.index("if (-not (Test-Path -LiteralPath $PythonExe"):]

    assert "& $TunnelScript -Check" in execution_block
    assert "& $WorkstationTunnelScript" not in execution_block
    assert "worker start" in execution_block
    assert "& $TunnelScript`n" not in execution_block


def test_preflight_remote_daemon_start_is_explicit_opt_in() -> None:
    content = Path("scripts/start_autofluid_preflight.ps1").read_text(encoding="utf-8")
    execution_block = content[content.index("if (-not (Test-Path -LiteralPath $PythonExe"):]

    assert "[switch]$StartDaemon" in content
    assert "if ($StartDaemon)" in execution_block
    assert execution_block.index("if ($StartDaemon)") < execution_block.index("Start-AutoFluidServerDaemon")
    assert "if (-not $tcpReady)" not in execution_block

def test_preflight_has_no_dead_local_ipc_wait_loop() -> None:
    content = Path("scripts/start_autofluid_preflight.ps1").read_text(encoding="utf-8")

    assert "Wait-AutoFluidIpcProtocolEndpoint" not in content


def test_preflight_user_facing_docs_describe_check_not_start() -> None:
    main_source = Path("main.py").read_text(encoding="utf-8")
    readme = Path("README.md").read_text(encoding="utf-8")
    daemon_window = Path("scripts/start_daemon_window.ps1").read_text(encoding="utf-8")

    assert "start_autofluid_preflight.ps1  # Windows 预检并启动" not in main_source
    assert "start_autofluid_preflight.ps1 # 预检 + 启动 Daemon" not in readme
    assert "to launch LocalWorker and Client" not in daemon_window
    assert "Windows 启动前预检" in main_source
    assert "启动前预检" in readme


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
    assert "listener-not-ready" in content

def test_server_ipc_tunnel_monitor_process_quotes_script_path() -> None:
    content = Path("scripts/start_server_ipc_tunnel.ps1").read_text(encoding="utf-8")
    monitor_fn = content[
        content.index("function Start-ServerTunnelMonitorProcess"):
        content.index("function Get-ServerTunnelListeningPid")
    ]

    assert "function Quote-ProcessArgument" in content
    assert "Quote-ProcessArgument -Value $PSCommandPath" in monitor_fn

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
    assert "Stop-ServerTunnelListeningProcess" in default_monitor_block
    assert "exit 0" in default_monitor_block
