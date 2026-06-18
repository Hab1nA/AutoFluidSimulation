use std::fs;

use crate::ipc::client::IpcClient;
use crate::state::app_state::{
    AppState, ConfirmAction, UiMode, SETTINGS_LOCKED_MESSAGE, STEP_NAMES,
};
use crate::state::filter;
use crate::state::log_buffer::LogBuffer;
use crate::utils::format_local_time;

pub enum CommandResult {
    None,
    Quit,
    FullQuit,
    StartCheck,
    StartDaemon,
    StopDaemon,
    RestartDaemon,
    StartWorkers,
    StopWorkers,
    RestartWorkers,
}

pub async fn dispatch_command(
    cmd: &str,
    ipc: &mut IpcClient,
    state: &mut AppState,
    log_buffer: &mut LogBuffer,
) -> CommandResult {
    let parts: Vec<&str> = cmd.split_whitespace().collect();
    if parts.is_empty() {
        return CommandResult::None;
    }

    let command = parts[0].to_lowercase();

    match command.as_str() {
        "help" => {
            show_help(log_buffer);
            CommandResult::None
        }
        "start" => cmd_start(ipc, log_buffer).await,
        "pause" => cmd_pause(ipc, log_buffer).await,
        "check" => cmd_check(ipc, state, log_buffer),
        "status" => cmd_status(ipc, log_buffer).await,
        "reset" => cmd_reset(&parts, state, log_buffer),
        "clean" => cmd_clean(&parts, state, log_buffer),
        "quit" => cmd_quit(&parts, state, log_buffer),
        "daemon" => cmd_daemon(&parts, state, log_buffer),
        "worker" => cmd_worker(&parts, ipc, state, log_buffer).await,
        "settings" => {
            if state.settings_locked() {
                log_buffer.push_info(SETTINGS_LOCKED_MESSAGE.to_string());
            } else {
                state.open_settings();
            }
            CommandResult::None
        }
        "filter" => cmd_filter(&parts, state, log_buffer),
        "export" => cmd_export(&parts, state, log_buffer),
        _ => {
            log_buffer.push_info(format!("❌ 未知命令: {}，输入 help 查看帮助", command));
            CommandResult::None
        }
    }
}

// ====================================================================
// 各命令处理函数
// ====================================================================

async fn cmd_start(ipc: &mut IpcClient, log_buffer: &mut LogBuffer) -> CommandResult {
    if !ipc.is_connected() {
        log_buffer.push_info("❌ 未连接到后台引擎".to_string());
        return CommandResult::None;
    }
    match ipc.start_pipeline().await {
        Ok(resp) if resp.is_ok() => {
            log_buffer.push_info(format!("✅ {}", resp.message));
        }
        Ok(resp) => {
            log_buffer.push_info(format!("❌ {}", resp.message));
        }
        Err(e) => {
            log_buffer.push_info(format!("❌ 通信失败: {}", e));
        }
    }
    CommandResult::None
}

async fn cmd_pause(ipc: &mut IpcClient, log_buffer: &mut LogBuffer) -> CommandResult {
    if !ipc.is_connected() {
        log_buffer.push_info("❌ 未连接到后台引擎".to_string());
        return CommandResult::None;
    }
    match ipc.pause_pipeline().await {
        Ok(resp) if resp.is_ok() => {
            log_buffer.push_info(format!("⏸️ {}", resp.message));
        }
        Ok(resp) => {
            log_buffer.push_info(format!("❌ {}", resp.message));
        }
        Err(e) => {
            log_buffer.push_info(format!("❌ 通信失败: {}", e));
        }
    }
    CommandResult::None
}

fn cmd_check(
    ipc: &mut IpcClient,
    _state: &mut AppState,
    log_buffer: &mut LogBuffer,
) -> CommandResult {
    if !ipc.is_connected() {
        log_buffer.push_info("❌ 未连接到后台引擎".to_string());
        return CommandResult::None;
    }
    CommandResult::StartCheck
}

async fn cmd_status(ipc: &mut IpcClient, log_buffer: &mut LogBuffer) -> CommandResult {
    if !ipc.is_connected() {
        log_buffer.push_info("❌ 未连接到后台引擎".to_string());
        return CommandResult::None;
    }
    match ipc.get_statistics().await {
        Ok(resp) if resp.is_ok() => {
            if let Some(obj) = resp.data.as_object() {
                let engine_status = obj
                    .get("engine_status")
                    .and_then(|v| v.as_str())
                    .unwrap_or("?");
                let total = obj
                    .get("total_configs")
                    .and_then(|v| v.as_u64())
                    .unwrap_or(0);
                log_buffer.push_info(format!("引擎状态: {}", engine_status));
                log_buffer.push_info(format!("总构型数: {}", total));
                if let Some(steps) = obj.get("steps").and_then(|v| v.as_object()) {
                    for (step, counts) in steps {
                        log_buffer.push_info(format!("  {}: {:?}", step, counts));
                    }
                }
            }
        }
        Ok(resp) => {
            log_buffer.push_info(format!("❌ {}", resp.message));
        }
        Err(e) => {
            log_buffer.push_info(format!("❌ 通信失败: {}", e));
        }
    }
    CommandResult::None
}

fn cmd_reset(parts: &[&str], state: &mut AppState, log_buffer: &mut LogBuffer) -> CommandResult {
    if parts.len() < 3 {
        log_buffer.push_info("用法: reset <构型名|all> <步骤名|all>".to_string());
        return CommandResult::None;
    }
    let config_arg = parts[1];
    let step_arg = parts[2];

    if !config_arg.eq_ignore_ascii_case("all") && config_arg.parse::<u64>().is_err() {
        log_buffer.push_info("❌ 构型名称必须是整数或 \"all\"".to_string());
        return CommandResult::None;
    }

    let step_name = if step_arg.eq_ignore_ascii_case("all") {
        None
    } else if STEP_NAMES.contains(&step_arg) {
        Some(step_arg.to_string())
    } else {
        log_buffer.push_info(format!("❌ 无效步骤名: {}", step_arg));
        return CommandResult::None;
    };

    let cfg_desc = if config_arg.eq_ignore_ascii_case("all") {
        "所有构型"
    } else {
        config_arg
    };
    let step_desc = match &step_name {
        None => "所有步骤".to_string(),
        Some(s) => format!("{} 及后续步骤", s),
    };

    state.confirm_message = Some(format!(
        "确定要重置{}的{}吗？此操作不可逆！",
        cfg_desc, step_desc
    ));
    state.confirm_callback = Some(ConfirmAction::ResetStep {
        config_name: config_arg.to_string(),
        step_name,
    });
    state.dialog_scroll = 0;
    state.ui_mode = UiMode::ConfirmDialog;
    CommandResult::None
}

fn cmd_clean(parts: &[&str], state: &mut AppState, log_buffer: &mut LogBuffer) -> CommandResult {
    if parts.len() < 3 {
        log_buffer
            .push_info("用法: clean <构型名|all> <步骤名|all> 或 clean all cache".to_string());
        return CommandResult::None;
    }
    let config_arg = parts[1];
    let step_arg = parts[2];

    if config_arg.eq_ignore_ascii_case("all") && step_arg.eq_ignore_ascii_case("cache") {
        state.confirm_message = Some(
            "确定要清理远程工作站上的全部临时缓存文件吗？\n将清空仿真工作目录、仿真标志目录以及动画临时目录内容，此操作不可逆！"
                .to_string(),
        );
        state.confirm_callback = Some(ConfirmAction::CleanStep {
            step_name: "cache".to_string(),
            config_name: None,
        });
        state.dialog_scroll = 0;
        state.ui_mode = UiMode::ConfirmDialog;
        return CommandResult::None;
    }

    if step_arg.eq_ignore_ascii_case("cache") {
        log_buffer.push_info("❌ cache 清理仅支持用法: clean all cache".to_string());
        return CommandResult::None;
    }

    let step_name = if step_arg.eq_ignore_ascii_case("all") {
        "all".to_string()
    } else if STEP_NAMES.contains(&step_arg) {
        step_arg.to_string()
    } else {
        log_buffer.push_info(format!("❌ 无效步骤名: {}", step_arg));
        return CommandResult::None;
    };

    let config_value = if config_arg.eq_ignore_ascii_case("all") {
        None
    } else {
        match config_arg.parse::<u64>() {
            Ok(n) => Some(serde_json::Value::Number(n.into())),
            Err(_) => {
                log_buffer.push_info("❌ 构型名称必须是整数或 \"all\"".to_string());
                return CommandResult::None;
            }
        }
    };

    let cfg_desc = if config_arg.eq_ignore_ascii_case("all") {
        "所有构型"
    } else {
        config_arg
    };
    let step_desc = if step_arg.eq_ignore_ascii_case("all") {
        "所有步骤"
    } else {
        step_arg
    };

    state.confirm_message = Some(format!(
        "确定要清理{}的{}产生的文件吗？此操作不可逆！",
        cfg_desc, step_desc
    ));
    state.confirm_callback = Some(ConfirmAction::CleanStep {
        step_name,
        config_name: config_value,
    });
    state.dialog_scroll = 0;
    state.ui_mode = UiMode::ConfirmDialog;
    CommandResult::None
}

fn cmd_quit(parts: &[&str], state: &mut AppState, log_buffer: &mut LogBuffer) -> CommandResult {
    if parts.len() > 1 && parts[1].eq_ignore_ascii_case("full") {
        state.confirm_message =
            Some("确定要【完全退出】后台引擎和界面吗？\n所有正在运行的任务将被中止！".to_string());
        state.confirm_callback = Some(ConfirmAction::FullQuit);
        state.dialog_scroll = 0;
        state.ui_mode = UiMode::ConfirmDialog;
        CommandResult::None
    } else {
        log_buffer.push_info("⚠️ 界面已退出，后台引擎仍在运行".to_string());
        log_buffer.push_info("  使用 start_client.py 可重新连接界面".to_string());
        CommandResult::Quit
    }
}

fn cmd_daemon(parts: &[&str], state: &mut AppState, log_buffer: &mut LogBuffer) -> CommandResult {
    if parts.len() < 2 {
        log_buffer.push_info("用法: daemon start | daemon stop | daemon restart".to_string());
        return CommandResult::None;
    }
    match parts[1].to_lowercase().as_str() {
        "start" => CommandResult::StartDaemon,
        "stop" => {
            state.confirm_message = Some("确定要【停止后台引擎】吗？\n所有正在运行的任务将被中止！\n（TUI 界面将保持运行，可随时重新启动 daemon）".to_string());
            state.confirm_callback = Some(ConfirmAction::StopDaemon);
            state.dialog_scroll = 0;
            state.ui_mode = UiMode::ConfirmDialog;
            CommandResult::None
        }
        "restart" => CommandResult::RestartDaemon,
        _ => {
            log_buffer.push_info("用法: daemon start | daemon stop | daemon restart".to_string());
            CommandResult::None
        }
    }
}

async fn cmd_worker(
    parts: &[&str],
    ipc: &mut IpcClient,
    state: &mut AppState,
    log_buffer: &mut LogBuffer,
) -> CommandResult {
    if parts.len() < 2 {
        log_buffer.push_info("用法: worker start | worker stop | worker restart".to_string());
        return CommandResult::None;
    }
    match parts[1].to_lowercase().as_str() {
        "start" => cmd_worker_start(ipc, log_buffer).await,
        "stop" => {
            state.confirm_message = Some(
                "确定要【停止所有 Worker】吗？\n本地 Worker 进程和工作站 SSH 隧道将被关闭！"
                    .to_string(),
            );
            state.confirm_callback = Some(ConfirmAction::StopWorkers);
            state.dialog_scroll = 0;
            state.ui_mode = UiMode::ConfirmDialog;
            CommandResult::None
        }
        "restart" => {
            state.confirm_message = Some(
                "确定要【重启所有 Worker】吗？\n将先停止再启动本地 Worker 和工作站连接。"
                    .to_string(),
            );
            state.confirm_callback = Some(ConfirmAction::RestartWorkers);
            state.dialog_scroll = 0;
            state.ui_mode = UiMode::ConfirmDialog;
            CommandResult::None
        }
        _ => {
            log_buffer.push_info("用法: worker start | worker stop | worker restart".to_string());
            CommandResult::None
        }
    }
}

async fn cmd_worker_start(ipc: &mut IpcClient, log_buffer: &mut LogBuffer) -> CommandResult {
    if !ipc.is_connected() {
        log_buffer.push_info("❌ 未连接到后台引擎".to_string());
        return CommandResult::None;
    }
    log_buffer.push_info(
        "🔧 正在启动 Worker（建立 SSH 隧道、验证连通性、启动本地 Worker）...".to_string(),
    );
    CommandResult::StartWorkers
}

fn cmd_filter(parts: &[&str], state: &mut AppState, log_buffer: &mut LogBuffer) -> CommandResult {
    if parts.len() < 2 {
        log_buffer.push_info("用法: filter <error|warning|info|debug|remote|local|com|scheduler|system|clear|status>".to_string());
        return CommandResult::None;
    }
    let sub = parts[1].to_lowercase();
    match sub.as_str() {
        "clear" => {
            state.log_filter_level = None;
            state.log_filter_source = None;
            log_buffer.push_info("✅ 日志过滤已清除，显示全部日志".to_string());
        }
        "status" => {
            let level_str = state.log_filter_level.as_deref().unwrap_or("全部");
            let source_str = state.log_filter_source.as_deref().unwrap_or("全部");
            log_buffer.push_info(format!("当前过滤: 级别={}, 来源={}", level_str, source_str));
        }
        _ => match filter::parse_filter_arg(&sub) {
            Some(filter::FilterType::Level(level)) => {
                log_buffer.push_info(format!("日志过滤: 显示 {} 及以上级别", level));
                state.log_filter_level = Some(level);
                state.log_filter_source = None;
            }
            Some(filter::FilterType::Source(source)) => {
                log_buffer.push_info(format!("日志过滤: 仅显示 {} 来源", source));
                state.log_filter_source = Some(source);
                state.log_filter_level = None;
            }
            None => {
                log_buffer.push_info(format!("❌ 未知过滤条件: {}", sub));
            }
        },
    }
    CommandResult::None
}

fn cmd_export(parts: &[&str], state: &mut AppState, log_buffer: &mut LogBuffer) -> CommandResult {
    let (scope, filename_arg) = match parts.get(1).map(|s| s.to_lowercase()) {
        Some(scope) if matches!(scope.as_str(), "all" | "info" | "detail") => {
            (scope, parts.get(2).copied())
        }
        _ => ("all".to_string(), parts.get(1).copied()),
    };

    let filename = if let Some(name) = filename_arg {
        let mut f = name.to_string();
        if !f.ends_with(".log") {
            f.push_str(".log");
        }
        f
    } else {
        let time_str = format_local_time("%Y%m%d_%H%M%S");
        format!("export_{}.log", time_str)
    };

    let log_dir = match std::env::current_dir() {
        Ok(dir) => dir.join("logs").join("local").join("exports"),
        Err(e) => {
            log_buffer.push_info(format!("❌ 获取当前目录失败: {}", e));
            return CommandResult::None;
        }
    };
    let _ = fs::create_dir_all(&log_dir);
    let filepath = log_dir.join(&filename);

    let lines = match scope.as_str() {
        "info" => log_buffer.export_info_lines(),
        "detail" => log_buffer.export_lines(&state.log_filter_level, &state.log_filter_source),
        _ => log_buffer.export_all_lines(&state.log_filter_level, &state.log_filter_source),
    };
    if lines.is_empty() {
        log_buffer.push_info("当前无日志可导出".to_string());
    } else {
        match fs::write(&filepath, lines.join("\n")) {
            Ok(_) => {
                log::info!(
                    "日志导出完成: scope={}, path={}, lines={}",
                    scope,
                    filepath.display(),
                    lines.len()
                );
                log_buffer.push_info(format!(
                    "✅ 日志已导出: {} ({} 条, scope={})",
                    filepath.display(),
                    lines.len(),
                    scope
                ));
            }
            Err(e) => {
                log::error!("日志导出失败: path={}, error={}", filepath.display(), e);
                log_buffer.push_info(format!("❌ 日志导出失败: {}", e));
            }
        }
    }
    CommandResult::None
}

pub async fn execute_confirm_action(
    action: &ConfirmAction,
    ipc: &mut IpcClient,
    log_buffer: &mut LogBuffer,
) -> CommandResult {
    match action {
        ConfirmAction::ResetStep {
            config_name,
            step_name,
        } => {
            log::info!(
                "确认重置步骤: config={}, step={}",
                config_name,
                step_name.as_deref().unwrap_or("all")
            );
            let config_value = if config_name.eq_ignore_ascii_case("all") {
                serde_json::Value::String("all".to_string())
            } else {
                config_name
                    .parse::<u64>()
                    .map(|n| serde_json::Value::Number(n.into()))
                    .unwrap_or(serde_json::Value::String(config_name.clone()))
            };
            match ipc.reset_step(config_value, step_name.as_deref()).await {
                Ok(resp) if resp.is_ok() => {
                    log_buffer.push_info(format!("✅ {}", resp.message));
                }
                Ok(resp) => {
                    log_buffer.push_info(format!("❌ {}", resp.message));
                }
                Err(e) => {
                    log_buffer.push_info(format!("❌ 通信失败: {}", e));
                }
            }
            CommandResult::None
        }
        ConfirmAction::CleanStep {
            step_name,
            config_name,
        } => {
            log::info!(
                "确认清理步骤文件: step={}, config={}",
                step_name,
                config_name
                    .as_ref()
                    .map_or_else(|| "all".to_string(), serde_json::Value::to_string)
            );
            match ipc.clean_step(step_name, config_name.clone()).await {
                Ok(resp) if resp.is_ok() => {
                    log_buffer.push_info(format!("✅ {}", resp.message));
                }
                Ok(resp) => {
                    log_buffer.push_info(format!("❌ {}", resp.message));
                }
                Err(e) => {
                    log_buffer.push_info(format!("❌ 通信失败: {}", e));
                }
            }
            CommandResult::None
        }
        ConfirmAction::FullQuit => {
            log::info!("确认完全退出后台引擎和界面");
            CommandResult::FullQuit
        }
        ConfirmAction::StopDaemon => {
            log::info!("确认停止后台引擎");
            CommandResult::StopDaemon
        }
        ConfirmAction::StopWorkers => {
            log::info!("确认停止所有 Worker");
            CommandResult::StopWorkers
        }
        ConfirmAction::RestartWorkers => {
            log::info!("确认重启所有 Worker");
            CommandResult::RestartWorkers
        }
    }
}

const HELP_LINES: &[&str] = &[
    "可用命令:",
    "  help                       - 显示此帮助",
    "  start                      - 启动或继续流水线",
    "  pause                      - 暂停流水线",
    "  settings                   - 打开程序设置页面",
    "  check                      - 系统自检",
    "  status                     - 显示状态摘要",
    "  reset <XX|all> <step|all>  - 重置构型步骤状态",
    "  clean <XX|all> <step|all>  - 清理构型步骤文件",
    "  clean all cache            - 清理远程临时缓存文件",
    "  daemon start               - 按当前模式启动本地/服务器后台引擎并自动连接",
    "  daemon stop                - 按当前模式停止本地/服务器后台引擎（TUI 继续运行）",
    "  daemon restart             - 按当前模式重启后台引擎（等同于 stop + start）",
    "  worker start               - 启动所有 Worker（建立 SSH 隧道、启动本地 Worker）",
    "  worker stop                - 停止所有 Worker（关闭进程和 SSH 隧道）",
    "  worker restart             - 重启所有 Worker（等同于 stop + start）",
    "  quit                       - 退出界面（引擎继续运行）",
    "  quit full                  - 完全退出（停止引擎 + 关闭 TUI）",
    "",
    "日志命令:",
    "  filter debug               - 显示 DEBUG 及以上级别日志",
    "  filter info                - 显示 INFO 及以上级别日志",
    "  filter warning             - 显示 WARNING 及以上级别日志",
    "  filter error               - 显示 ERROR 及以上级别日志",
    "  filter critical            - 显示 CRITICAL 及以上级别日志",
    "  filter remote              - 仅显示远程命令日志",
    "  filter local               - 仅显示本地命令日志",
    "  filter com                 - 仅显示 COM 自动化日志",
    "  filter scheduler           - 仅显示调度器日志",
    "  filter system              - 仅显示系统日志",
    "  filter ipc                 - 仅显示 IPC 通信日志",
    "  filter clear               - 清除过滤，显示全部",
    "  filter status              - 查看当前过滤状态",
    "  export                     - 导出当前日志到文件",
    "  export <filename>          - 导出日志为指定文件名",
    "  export all|info|detail     - 导出全部/高级信息/详细日志",
];

fn show_help(log_buffer: &mut LogBuffer) {
    for line in HELP_LINES {
        log_buffer.push_info(line.to_string());
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn test_daemon_restart_dispatch() {
        let mut ipc = IpcClient::new(None, None);
        let mut state = AppState::new();
        let mut log_buffer = LogBuffer::new();

        let result =
            dispatch_command("daemon restart", &mut ipc, &mut state, &mut log_buffer).await;
        assert!(matches!(result, CommandResult::RestartDaemon));
    }

    #[tokio::test]
    async fn test_daemon_help_mentions_restart() {
        let mut ipc = IpcClient::new(None, None);
        let mut state = AppState::new();
        let mut log_buffer = LogBuffer::new();

        let result = dispatch_command("help", &mut ipc, &mut state, &mut log_buffer).await;
        assert!(matches!(result, CommandResult::None));

        assert!(log_buffer
            .info_messages
            .iter()
            .any(|line| line.contains("daemon restart")));
    }

    #[tokio::test]
    async fn settings_command_blocks_while_pipeline_started() {
        let mut ipc = IpcClient::new(None, None);
        let mut state = AppState::new();
        state.engine_info.pipeline_started = true;
        let mut log_buffer = LogBuffer::new();

        let result = dispatch_command("settings", &mut ipc, &mut state, &mut log_buffer).await;

        assert!(matches!(result, CommandResult::None));
        assert_ne!(state.ui_mode, UiMode::Settings);
        assert!(state.settings_state.is_none());
        assert!(log_buffer
            .info_messages
            .iter()
            .any(|line| line == SETTINGS_LOCKED_MESSAGE));
    }

    #[tokio::test]
    async fn settings_command_opens_after_daemon_state_is_cleared() {
        let mut ipc = IpcClient::new(None, None);
        let mut state = AppState::new();
        state.engine_info.pipeline_started = true;
        state.mark_daemon_stopped();
        let mut log_buffer = LogBuffer::new();

        let result = dispatch_command("settings", &mut ipc, &mut state, &mut log_buffer).await;

        assert!(matches!(result, CommandResult::None));
        assert_eq!(state.ui_mode, UiMode::Settings);
        assert!(state.settings_state.is_some());
    }

    #[tokio::test]
    async fn export_command_writes_to_structured_export_dir() {
        let project_dir = std::env::temp_dir().join(format!(
            "autofluid-tui-export-test-{}",
            crate::generate_request_id()
        ));
        std::fs::create_dir_all(&project_dir).expect("create temp project dir");
        let previous_dir = std::env::current_dir().expect("current dir");
        std::env::set_current_dir(&project_dir).expect("set temp cwd");
        let mut ipc = IpcClient::new(None, None);
        let mut state = AppState::new();
        let mut log_buffer = LogBuffer::new();
        log_buffer.push_info("export me".to_string());

        let result = dispatch_command(
            "export all structured-export",
            &mut ipc,
            &mut state,
            &mut log_buffer,
        )
        .await;

        std::env::set_current_dir(previous_dir).expect("restore cwd");
        assert!(matches!(result, CommandResult::None));
        assert!(project_dir
            .join("logs")
            .join("local")
            .join("exports")
            .join("structured-export.log")
            .is_file());
        std::fs::remove_dir_all(project_dir).ok();
    }
}
