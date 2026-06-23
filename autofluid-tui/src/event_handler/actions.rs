use crate::event_handler::command;
use crate::ipc::client::IpcClient;
use crate::state::app_state::SETTINGS_LOCKED_MESSAGE;
use crate::state::{AppState, LogBuffer};
use crate::{start_worker_lifecycle_task, EventContext, WorkerLifecycleAction};

pub fn handle_confirm_result(result: command::CommandResult, ctx: &mut EventContext) {
    match result {
        command::CommandResult::FullQuit => {
            *ctx.full_quit = true;
            if ctx.ipc.is_connected() {
                match ctx.rt.block_on(ctx.ipc.full_quit()) {
                    Ok(resp) if resp.is_ok() => {
                        *ctx.full_quit_stop_sent = true;
                        ctx.log_buffer.push_info(format!("✅ {}", resp.message));
                    }
                    Ok(resp) => {
                        ctx.log_buffer.push_info(format!("❌ {}", resp.message));
                    }
                    Err(e) => {
                        ctx.log_buffer
                            .push_info(format!("❌ 停止后台引擎通信失败: {}", e));
                    }
                }
            }
            ctx.state.should_quit = true;
        }
        command::CommandResult::StopDaemon => {
            ctx.daemon
                .stop_with_ipc(ctx.ipc, ctx.rt, ctx.state, ctx.log_buffer, ctx.project_dir);
        }
        command::CommandResult::StopWorkers => {
            let host = ctx.ipc.host().to_string();
            let port = ctx.ipc.port();
            start_worker_lifecycle_task(
                ctx.worker_task.as_deref_mut(),
                WorkerLifecycleAction::Stop,
                ctx.project_dir,
                &host,
                port,
                ctx.log_buffer,
                ctx.state,
            );
        }
        command::CommandResult::RestartWorkers => {
            let host = ctx.ipc.host().to_string();
            let port = ctx.ipc.port();
            start_worker_lifecycle_task(
                ctx.worker_task.as_deref_mut(),
                WorkerLifecycleAction::Restart,
                ctx.project_dir,
                &host,
                port,
                ctx.log_buffer,
                ctx.state,
            );
        }
        _ => {}
    }
}

pub fn save_settings(
    state: &mut AppState,
    ipc: &mut IpcClient,
    rt: &tokio::runtime::Runtime,
    log_buffer: &mut LogBuffer,
) {
    if state.settings_locked() {
        log_buffer.push_info(SETTINGS_LOCKED_MESSAGE.to_string());
        if let Some(ref mut ss) = state.settings_state {
            ss.save_error = Some(SETTINGS_LOCKED_MESSAGE.to_string());
        }
        state.needs_redraw = true;
        return;
    }

    if let Some(ref mut ss) = state.settings_state {
        if ss.is_editing_field() {
            ss.commit_edit_current_field();
        }
        ss.validation_errors.clear();
        ss.save_error = None;
        match ss.save() {
            Ok(()) => {
                ss.saved = true;
                if ipc.is_connected() {
                    match rt.block_on(ipc.reload_config()) {
                        Ok(resp) if resp.is_ok() => {
                            log_buffer.push_info("✅ 后台引擎配置已重新加载".to_string());
                        }
                        Ok(resp) => {
                            log_buffer
                                .push_info(format!("⚠️ 后台引擎配置重载失败: {}", resp.message));
                        }
                        Err(e) => {
                            log_buffer.push_info(format!("⚠️ 后台引擎配置重载通信失败: {}", e));
                        }
                    }
                } else {
                    log_buffer.push_info("⚠️ 后台引擎未连接，配置将在下次启动时生效".to_string());
                }
                log_buffer.push_info("✅ 设置已保存到 autofluid_config.toml".to_string());
            }
            Err(errors) => {
                log_buffer.push_info(format!(
                    "❌ 设置保存失败，请修正错误后重试 ({} 项)",
                    errors.len()
                ));
                ss.validation_errors = errors;
                ss.save_error = Some("保存失败，请修正错误后重试".to_string());
                state.needs_redraw = true;
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::settings::SettingsState;

    #[test]
    fn save_settings_refuses_when_pipeline_started_after_dialog_opened() {
        let rt = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .expect("runtime");
        let mut ipc = IpcClient::new(None, None);
        let mut state = AppState::new();
        state.settings_state = Some(SettingsState::new());
        state.engine_info.pipeline_started = true;
        let mut log_buffer = LogBuffer::new();

        save_settings(&mut state, &mut ipc, &rt, &mut log_buffer);

        assert!(state.settings_state.is_some());
        assert_eq!(
            state
                .settings_state
                .as_ref()
                .and_then(|settings| settings.save_error.as_deref()),
            Some(SETTINGS_LOCKED_MESSAGE)
        );
        assert!(log_buffer
            .info_messages
            .iter()
            .any(|line| line == SETTINGS_LOCKED_MESSAGE));
        assert!(state.needs_redraw);
    }
}
