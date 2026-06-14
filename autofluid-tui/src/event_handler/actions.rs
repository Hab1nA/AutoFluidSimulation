use crate::event_handler::command;
use crate::ipc::client::IpcClient;
use crate::state::{AppState, LogBuffer};
use crate::worker_mgr::prepare_remote_workers;
use crate::EventContext;

pub fn handle_confirm_result(result: command::CommandResult, ctx: &mut EventContext) {
    match result {
        command::CommandResult::FullQuit => {
            *ctx.full_quit = true;
            if ctx.ipc.is_connected() {
                match ctx.rt.block_on(ctx.ipc.full_quit()) {
                    Ok(resp) if resp.is_ok() => {
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
            ctx.rt.block_on(ctx.ipc.disconnect());
            ctx.state.should_quit = true;
        }
        command::CommandResult::StopDaemon => {
            ctx.daemon
                .stop_with_ipc(ctx.ipc, ctx.rt, ctx.state, ctx.log_buffer, ctx.project_dir);
        }
        command::CommandResult::StopWorkers => {
            if ctx.ipc.is_connected() {
                match ctx.rt.block_on(ctx.ipc.worker_stop()) {
                    Ok(resp) if resp.is_ok() => {
                        ctx.log_buffer.push_info(format!("✅ {}", resp.message));
                    }
                    Ok(resp) => {
                        ctx.log_buffer.push_info(format!("❌ {}", resp.message));
                    }
                    Err(e) => {
                        ctx.log_buffer.push_info(format!("❌ 通信失败: {}", e));
                    }
                }
            }
            ctx.worker.stop_workers(ctx.log_buffer);
        }
        command::CommandResult::RestartWorkers => {
            if ctx.ipc.is_connected() {
                match ctx.rt.block_on(ctx.ipc.worker_stop()) {
                    Ok(resp) if resp.is_ok() => {
                        ctx.log_buffer.push_info(format!("✅ {}", resp.message));
                    }
                    Ok(resp) => {
                        ctx.log_buffer.push_info(format!("❌ {}", resp.message));
                    }
                    Err(e) => {
                        ctx.log_buffer.push_info(format!("❌ 通信失败: {}", e));
                    }
                }
            }
            ctx.worker.stop_workers(ctx.log_buffer);
            ctx.worker
                .start_workers_with_prepare(ctx.project_dir, ctx.log_buffer, |buffer| {
                    prepare_remote_workers(ctx.ipc, ctx.rt, buffer)
                });
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
