use crate::daemon_mgr::DaemonManager;
use crate::event_handler::command;
use crate::ipc::client::IpcClient;
use crate::state::{AppState, LogBuffer};
use crate::worker_mgr::{prepare_remote_workers, WorkerManager};

#[allow(clippy::too_many_arguments)]
pub fn handle_confirm_result(
    result: command::CommandResult,
    rt: &tokio::runtime::Runtime,
    ipc: &mut IpcClient,
    state: &mut AppState,
    log_buffer: &mut LogBuffer,
    daemon: &mut DaemonManager,
    worker: &mut WorkerManager,
    project_dir: &str,
    full_quit: &mut bool,
) {
    match result {
        command::CommandResult::FullQuit => {
            *full_quit = true;
            if ipc.is_connected() {
                match rt.block_on(ipc.full_quit()) {
                    Ok(resp) if resp.is_ok() => {
                        log_buffer.push_info(format!("✅ {}", resp.message));
                    }
                    Ok(resp) => {
                        log_buffer.push_info(format!("❌ {}", resp.message));
                    }
                    Err(e) => {
                        log_buffer.push_info(format!("❌ 停止后台引擎通信失败: {}", e));
                    }
                }
            }
            rt.block_on(ipc.disconnect());
            state.should_quit = true;
        }
        command::CommandResult::StopDaemon => {
            daemon.stop_with_ipc(ipc, rt, state, log_buffer, project_dir);
        }
        command::CommandResult::StopWorkers => {
            if ipc.is_connected() {
                match rt.block_on(ipc.worker_stop()) {
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
            }
            worker.stop_workers(log_buffer);
        }
        command::CommandResult::RestartWorkers => {
            if ipc.is_connected() {
                match rt.block_on(ipc.worker_stop()) {
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
            }
            worker.stop_workers(log_buffer);
            worker.start_workers_with_prepare(project_dir, log_buffer, |buffer| {
                prepare_remote_workers(ipc, rt, buffer)
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
