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


def test_spaceclaim_transit_uses_only_2024r1_api() -> None:
    source = SPACECLAIM_TRANSIT_SOURCE.read_text(encoding="utf-8")

    assert "_SPACECLAIM_API_VERSION" in source
    assert '"V241"' in source
    assert "from SpaceClaim.Api.V241 import *" in source
    assert '"V23"' not in source
    assert "_SPACECLAIM_API_VERSIONS" not in source
    assert "for _api_version" not in source
    assert "SpaceClaim.Api.V23 import *" not in source
    assert "__import__" not in source
    assert "dir(_sc_api)" not in source


def test_spaceclaim_transit_fails_closed_before_saving_invalid_scdoc() -> None:
    source = SPACECLAIM_TRANSIT_SOURCE.read_text(encoding="utf-8")
    processing_failure_start = source.index("def _fail_closed_processing")
    save_start = source.index("doc.SaveAs(temp_path)", processing_failure_start)
    named_selection_block = source[processing_failure_start:save_start]

    assert "remaining = _close_all_documents()" in named_selection_block
    assert "os.remove(temp_path)" in named_selection_block
    assert "return False" in named_selection_block
    assert "result is None or not result.Success" in named_selection_block
    assert "result.CreatedNamedSelection is None" in named_selection_block
    assert "return result.CreatedNamedSelection" in named_selection_block
    assert "merge_result is None or not merge_result.Success" in named_selection_block
    assert "rename_result is None or not rename_result.Success" in named_selection_block
    assert "组{} 创建失败" in named_selection_block
    assert "合并 组4+组5 命令未成功" in named_selection_block
    assert "选择集创建失败" in named_selection_block
    assert "重命名 {} → {} 命令未成功" in named_selection_block
    assert "将以默认名称保存" not in source
    assert "将跳过合并" not in source


def test_spaceclaim_transit_uses_fresh_body_selection_for_each_group() -> None:
    source = SPACECLAIM_TRANSIT_SOURCE.read_text(encoding="utf-8")
    helper_start = source.index("def _create_named_selection")
    helper_end = source.index("selection_specs = [", helper_start)
    helper = source[helper_start:helper_end]

    assert "bodies = list(part.Bodies)" in helper
    assert "if not bodies:" in helper
    assert "fresh_body_selection = Selection.Create(bodies[0])" in helper
    assert "if fresh_body_selection is None:" in helper
    assert "PowerSelectOptions(False, fresh_body_selection)" in helper
    assert "PowerSelectOptions(False)" not in helper
    assert "body_selection" not in source.replace("fresh_body_selection", "")


def test_spaceclaim_transit_removes_stale_output_before_opening_step() -> None:
    source = SPACECLAIM_TRANSIT_SOURCE.read_text(encoding="utf-8")

    step_exists_check = source.index("if not os.path.exists(step_path)")
    out_path_build = source.index(
        "out_path = os.path.join(scdoc_dir, scdoc_name)", step_exists_check
    )
    stale_output_remove = source.index("os.remove(stale_path)", out_path_build)
    document_open = source.index("Document.Open(step_path, None)", stale_output_remove)
    document_save = source.index("doc.SaveAs(temp_path)", document_open)

    assert step_exists_check < out_path_build < stale_output_remove
    assert stale_output_remove < document_open < document_save
    assert source.count("out_path = os.path.join(scdoc_dir, scdoc_name)") == 1
    stale_output_block = source[out_path_build:document_open]
    assert "except (OSError, IOError) as e:" in stale_output_block
    assert "return False" in stale_output_block


def test_spaceclaim_transit_atomically_publishes_only_after_close() -> None:
    source = SPACECLAIM_TRANSIT_SOURCE.read_text(encoding="utf-8")

    temp_save = source.index("doc.SaveAs(temp_path)")
    temp_nonempty_check = source.index("os.path.getsize(temp_path) <= 0", temp_save)
    close_documents = source.index("remaining = _close_all_documents()", temp_save)
    atomic_publish = source.index("os.rename(temp_path, out_path)", close_documents)
    final_nonempty_check = source.index("os.path.getsize(out_path) <= 0", atomic_publish)

    assert temp_save < temp_nonempty_check < close_documents
    assert close_documents < atomic_publish < final_nonempty_check
    assert 'out_basename + ".autofluid_tmp.scdoc"' in source
    assert "doc.SaveAs(out_path)" not in source


def test_bridge_sets_versioned_ansys_environment_from_spaceclaim_exe() -> None:
    source = _source()

    assert "private static string ResolveAwpRoot(string? spaceClaimExePath)" in source
    assert "private static string ResolveAnsysVersionToken(string? awpRoot)" in source
    assert "SetVersionedEnvironmentIfMissing" in source
    assert '"AWP_ROOT{versionToken}"' in source
    assert '"ANSYS{versionToken}_DIR"' in source
    assert '"CADOE_LIBDIR{versionToken}"' in source
    assert '@"C:\\Program Files\\ANSYS Inc\\v241"' in source
    assert 'Environment.GetEnvironmentVariable("AWP_ROOT231")' not in source


def test_bridge_auto_detects_only_spaceclaim_2024r1() -> None:
    source = _source()
    paths_start = source.index("private static readonly string[] SpaceClaimExePaths")
    paths_end = source.index("};", paths_start)
    paths_block = source[paths_start:paths_end]

    assert r"C:\Program Files\ANSYS Inc\v241\SCDM\SpaceClaim.exe" in paths_block
    assert r"C:\Program Files\ANSYS Inc\v232\SCDM\SpaceClaim.exe" not in paths_block
    assert r"C:\Program Files\ANSYS Inc\v231\SCDM\SpaceClaim.exe" not in paths_block


def test_bridge_normalizes_filesystem_arguments_to_absolute_paths() -> None:
    source = _source()

    assert "private static void NormalizePathOptions(BridgeOptions options)" in source
    assert "NormalizePathOptions(options);" in source
    assert "options.ScriptPath = Path.GetFullPath(options.ScriptPath);" in source
    assert "options.StepDir = Path.GetFullPath(options.StepDir);" in source
    assert "options.ScdocDir = Path.GetFullPath(options.ScdocDir);" in source
    assert "options.CmdDir = Path.GetFullPath(options.CmdDir);" in source


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


def test_bridge_only_accepts_fresh_nonempty_scdoc_outputs() -> None:
    source = _source()

    assert "private static bool IsFreshScdocFile" in source
    assert "LastWriteTimeUtc" in source
    assert "launchBaseline" in source
    assert "fi.Length <= 0" in source

    execute_start = source.index("private static int Execute")
    execute_end = source.index("private static void PrepareSpaceClaimEnvironment", execute_start)
    execute_block = source[execute_start:execute_end]

    assert "if (IsFreshScdocFile(scdocFile, launchBaseline))" in execute_block
    assert "if (File.Exists(scdocFile))" not in execute_block
    gui_failure_start = execute_block.index("catch (InvalidOperationException ex)")
    gui_failure_block = execute_block[gui_failure_start:]
    assert "TryReturnSuccessIfScdocExists(" in gui_failure_block
    assert "launchBaseline" in gui_failure_block


def test_bridge_kills_oneshot_spaceclaim_on_failure_before_dispose() -> None:
    source = _source()
    execute_start = source.index("private static int Execute")
    execute_end = source.index("private static void PrepareSpaceClaimEnvironment", execute_start)
    execute_block = source[execute_start:execute_end]

    assert "private static void TryKillWorkingProcess" in source
    assert 'TryKillWorkingProcess(workingProcess, "one-shot timed out")' in execute_block
    assert (
        'TryKillWorkingProcess(workingProcess, "SpaceClaim exited without fresh SCDOC")'
        in execute_block
    )
    assert (
        'TryKillWorkingProcess(workingProcess, "SpaceClaim GUI ready detection failed")'
        in execute_block
    )
    assert execute_block.index(
        'TryKillWorkingProcess(workingProcess, "one-shot timed out")'
    ) < execute_block.index("return (int)ExitCode.Timeout;")
    assert "process.Kill();" in source


def test_resolve_started_spaceclaim_disposes_unusable_started_handle() -> None:
    source = _source()
    resolve_start = source.index("private static Process? ResolveStartedSpaceClaimProcess")
    resolve_end = source.index("private static string GetStepFilePath", resolve_start)
    resolve_block = source[resolve_start:resolve_end]

    assert "startedProcess.Dispose();" in resolve_block
    assert resolve_block.index("startedProcess.Dispose();") < resolve_block.index(
        "return WaitForProcessAppear"
    )

def test_bridge_normalizes_duplicate_path_environment_before_start() -> None:
    source = _source()

    assert "NormalizePathEnvironmentVariables" in source
    assert "private static Process? LaunchAndResolve" in source
    assert source.count("LaunchAndResolve(") >= 3
    launch_start = source.index("private static Process? LaunchAndResolve")
    launch_end = source.index("private static void PrepareSpaceClaimEnvironment", launch_start)
    launch_block = source[launch_start:launch_end]
    assert "PrepareSpaceClaimEnvironment(psi, scExe);" in launch_block
    assert "configureEnvironment(psi);" in launch_block
    assert launch_block.index("PrepareSpaceClaimEnvironment(psi, scExe);") < launch_block.index(
        "configureEnvironment(psi);"
    )
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


def test_persistent_bridge_quit_has_deadline() -> None:
    source = _source()

    assert "PersistentQuitTimeoutSeconds" in source
    assert "Stopwatch? quitTimer = null;" in source
    assert "quitTimer = Stopwatch.StartNew();" in source
    assert re.search(r"quitTimer\.Elapsed\.TotalSeconds\s*>=\s*PersistentQuitTimeoutSeconds", source)
    assert 'TryKillWorkingProcess(workingProcess, "persistent quit timeout")' in source
    assert "exitCode = (int)ExitCode.Timeout;" in source


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
