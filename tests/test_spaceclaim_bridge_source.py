from __future__ import annotations

import re
from pathlib import Path


BRIDGE_SOURCE = (
    Path(__file__).resolve().parents[1]
    / "bridge"
    / "SpaceClaimBridge"
    / "Program.cs"
)
SC_PROCESS_POOL_SOURCE = (
    Path(__file__).resolve().parents[1]
    / "engine"
    / "sc_process_pool.py"
)
SPACECLAIM_TRANSIT_SOURCE = (
    Path(__file__).resolve().parents[1]
    / "executor"
    / "spaceclaim_transit.py"
)
DEPLOY_SCRIPT_SOURCE = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "deploy_linux_server.sh"
)


def _source() -> str:
    return BRIDGE_SOURCE.read_text(encoding="utf-8")


def test_persistent_bridge_uses_shared_ready_timeout_env() -> None:
    source = _source()
    pool_source = SC_PROCESS_POOL_SOURCE.read_text(encoding="utf-8")

    assert "PersistentReadyTimeoutSeconds = 120" not in source
    assert '"AUTOFLUID_SC_PERSISTENT_READY_TIMEOUT"' in source
    assert "DefaultPersistentReadyTimeoutSeconds = 180" in source
    assert 'sc_env["AUTOFLUID_SC_PERSISTENT_READY_TIMEOUT"]' in pool_source
    assert 'ENGINE_CONFIG.get("sc_persistent_ready_timeout", 180)' in pool_source


def test_persistent_bridge_passes_session_log_dir_to_transit() -> None:
    pool_source = SC_PROCESS_POOL_SOURCE.read_text(encoding="utf-8")

    assert 'sc_env["AUTOFLUID_SC_LOG_DIR"]' in pool_source
    assert "_build_bridge_log_dir" in pool_source


def test_spaceclaim_transit_uses_env_log_dir_and_slot_filename() -> None:
    source = SPACECLAIM_TRANSIT_SOURCE.read_text(encoding="utf-8")

    assert 'os.environ.get("AUTOFLUID_SC_LOG_DIR"' in source
    assert 'os.environ.get("AUTOFLUID_SC_SLOT_ID"' in source
    assert '"local", "services", "spaceclaim"' in source
    assert "spaceclaim_transit_slot{}_{}.log" in source
    assert "spaceclaim_transit_{}.log" in source


def test_linux_daemon_unit_declares_lifecycle_boundaries() -> None:
    source = DEPLOY_SCRIPT_SOURCE.read_text(encoding="utf-8")

    assert "KillMode=control-group" in source
    assert "TimeoutStopSec=30" in source
    assert "Restart=on-failure" in source
    assert "RestartSec=5" in source


def test_daemon_service_logs_use_structured_service_paths() -> None:
    source = (Path(__file__).resolve().parents[1] / "engine" / "daemon.py").read_text(
        encoding="utf-8"
    )

    assert "service_log_file" in source
    assert 'service_log_file("alert-watcher", "alert_watcher.log")' in source
    assert 'service_log_file("local-worker", "local_worker_autostart.log")' in source


def test_spaceclaim_transit_uses_passed_scdoc_name() -> None:
    source = SPACECLAIM_TRANSIT_SOURCE.read_text(encoding="utf-8")

    assert "scdoc_name" in source
    assert "cmd_data.get(\"scdocname\"" in source
    assert "os.path.basename(scdoc_name)" in source
    assert "out_filename" not in source
    assert "model_gen4_{}.scdoc" not in source


def test_bridge_treats_pre_gui_exit_with_scdoc_as_success() -> None:
    source = _source()
    gui_failure_block_start = source.index("catch (InvalidOperationException ex)")
    gui_failure_block_end = source.index("finally", gui_failure_block_start)
    gui_failure_block = source[gui_failure_block_start:gui_failure_block_end]

    assert "TryReturnSuccessIfScdocExists" in gui_failure_block
    assert "SpaceClaim exited before GUI ready but SCDOC exists" in source


def test_bridge_normalizes_duplicate_path_environment_before_start() -> None:
    source = _source()

    assert "NormalizePathEnvironmentVariables" in source
    assert source.count("PrepareSpaceClaimEnvironment(psi);") >= 2
    normalize_start = source.index("private static void NormalizePathEnvironmentVariables")
    normalize_block = source[normalize_start:source.index("private static int? TryReturnSuccessIfScdocExists", normalize_start)]
    current_env_call = normalize_block.index("NormalizeCurrentProcessPathEnvironment();")
    child_env_enum = normalize_block.index("psi.EnvironmentVariables.Keys")
    assert current_env_call < child_env_enum
    assert 'Environment.SetEnvironmentVariable("PATH", null)' in source
    assert 'Environment.SetEnvironmentVariable("Path", pathValue)' in source
    assert 'pathKeys.Add(key);' in source
    assert "pathKeySet" not in source
    assert "foreach (string key in pathKeys)" in source


def test_persistent_quit_exits_spaceclaim() -> None:
    source = SPACECLAIM_TRANSIT_SOURCE.read_text(encoding="utf-8")
    quit_block_start = source.index('cmd_data.get("command") == "quit"')
    quit_block_end = source.index("config_name = str", quit_block_start)
    quit_block = source[quit_block_start:quit_block_end]

    assert 'Command.Execute("Exit")' in quit_block
    assert "常驻模式: 正在退出 SpaceClaim" in quit_block


def test_persistent_loop_reports_abnormal_exit_codes_and_statuses() -> None:
    source = _source()

    assert '"running"' in source
    assert '"spaceclaim_exited"' in source
    assert '"process_handle_released"' in source
    assert '"process_check_failed"' in source
    assert "GetProcessIdOrDefault" in source
    assert "exitCode = (int)ExitCode.ScriptFailed;" in source
    assert "exitCode = (int)ExitCode.LaunchFailed;" in source


def test_persistent_loop_checks_process_before_quit_command() -> None:
    source = _source()
    loop_start = source.index("while (true)")
    process_check = source.index("检测 SpaceClaim 进程是否已退出", loop_start)
    quit_check = source.index("检测 quit 命令文件", loop_start)

    assert process_check < quit_check


def test_bridge_source_has_defensive_file_and_json_helpers() -> None:
    source = _source()

    assert "ReadCommandFile" in source
    assert "FileShare.ReadWrite | FileShare.Delete" in source
    assert "EscapeJsonString" in source
    assert '\\"status\\":\\"{EscapeJsonString(status)}\\"' in source


def test_argument_parser_reports_missing_values_per_option() -> None:
    source = _source()

    for option in (
        "--script",
        "--config",
        "--stepdir",
        "--scdocdir",
        "--scdocname",
        "--timeout",
        "--sc-exe",
        "--cmddir",
        "--slotid",
    ):
        assert f'ReadRequiredArgumentValue(args, ref i, "{option}")' in source
    assert "参数 {optionName} 缺少值" in source


def test_bridge_scdoc_filename_is_argument_driven() -> None:
    source = _source()

    assert "ScdocFileNamePattern" not in source
    assert "ScdocFileName" in source
    assert re.search(
        r"Path\.Combine\(\s*opts\.ScdocDir!?,\s*opts\.ScdocFileName!?\s*\)",
        source,
        re.DOTALL,
    )


def test_bridge_rejects_scdoc_path_as_invalid_args() -> None:
    source = _source()

    parse_start = source.index("private static BridgeOptions? ParseArguments")
    parse_end = source.index("private static int Execute", parse_start)
    parse_block = source[parse_start:parse_end]

    assert "Path.GetFileName(options.ScdocFileName) != options.ScdocFileName" in parse_block
    assert "return null;" in parse_block


def test_process_scan_disposes_processes_on_failed_metadata_access() -> None:
    source = _source()

    assert "finally" in source[source.index("WaitForProcessAppear") :]
    assert "selectedProcess = p;" in source
    assert "DisposeUnmatchedProcesses(procs, selectedProcess)" in source
    assert "DisposeProcesses(procs)" in source
