from __future__ import annotations

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
    assert "spaceclaim_transit_slot{}_{}.log" in source
    assert "spaceclaim_transit_{}.log" in source


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
        "--timeout",
        "--sc-exe",
        "--cmddir",
        "--slotid",
    ):
        assert f'ReadRequiredArgumentValue(args, ref i, "{option}")' in source
    assert "参数 {optionName} 缺少值" in source


def test_process_scan_disposes_processes_on_failed_metadata_access() -> None:
    source = _source()

    assert "finally" in source[source.index("WaitForProcessAppear") :]
    assert "selectedProcess = p;" in source
    assert "DisposeUnmatchedProcesses(procs, selectedProcess)" in source
    assert "DisposeProcesses(procs)" in source
