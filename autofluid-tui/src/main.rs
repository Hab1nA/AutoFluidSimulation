mod ipc;
mod settings;
mod state;
mod ui;
mod event_handler;
mod daemon_mgr;
mod utils;

use std::io;
use std::time::Duration;

use crossterm::event::{self as crossterm_event, Event as CrosstermEvent, EnableMouseCapture, DisableMouseCapture};
use crossterm::terminal::{EnterAlternateScreen, LeaveAlternateScreen};
use crossterm::execute;
use ratatui::backend::CrosstermBackend;
use ratatui::Terminal;

use daemon_mgr::DaemonManager;
use ipc::client::IpcClient;
use state::{AppState, LogBuffer};
use state::app_state::UiMode;
use ui::layout::AppLayout;
use event_handler::key_handler;
use event_handler::command;

pub use utils::format_local_time;

// Daemon 生命周期函数已迁入 daemon_mgr::DaemonManager:
//   DaemonManager::reconnect_ipc_after_launch()
//   daemon.stop_with_ipc()
//   daemon.restart_with_ipc()

pub fn generate_request_id() -> String {
    use std::time::SystemTime;
    let t = SystemTime::now()
        .duration_since(SystemTime::UNIX_EPOCH)
        .unwrap_or_default()
        .as_nanos();
    let mut s = String::with_capacity(8);
    for i in 0..8 {
        let nibble = ((t >> (i * 4)) ^ (t >> ((i + 8) * 4))) as u8 & 0x0f;
        s.push(std::char::from_digit(nibble as u32, 16).unwrap_or('0'));
    }
    s
}

fn main() -> Result<(), Box<dyn std::error::Error>> {
    crossterm::terminal::enable_raw_mode()?;

    let mut stdout = io::stdout();
    execute!(stdout, EnterAlternateScreen, EnableMouseCapture)?;

    let backend = CrosstermBackend::new(stdout);
    let mut terminal = Terminal::new(backend)?;
    terminal.clear()?;

    let result = run_app(&mut terminal);

    execute!(io::stdout(), DisableMouseCapture, LeaveAlternateScreen)?;
    crossterm::terminal::disable_raw_mode()?;

    if let Err(e) = result {
        eprintln!("应用错误: {}", e);
    }

    Ok(())
}

fn run_app(terminal: &mut Terminal<CrosstermBackend<io::Stdout>>) -> Result<(), String> {
    let rt = tokio::runtime::Builder::new_current_thread().enable_all().build().map_err(|e| e.to_string())?;
    let project_dir = std::env::current_dir()
        .unwrap_or_default()
        .to_string_lossy()
        .to_string();

    let mut ipc = IpcClient::new(None, None);
    let mut state = AppState::new();
    let mut log_buffer = LogBuffer::new();
    let mut daemon = daemon_mgr::DaemonManager::new();

    log_buffer.push_info("欢迎使用液氧甲烷火箭发动机仿真总控程序！".to_string());
    log_buffer.push_info("正在连接后台引擎...".to_string());
    log_buffer.push_info("Tab 切换焦点 | ↑↓ 滚动 | PageUp/PageDown 翻页 | Home/End 跳转".to_string());

    if let Ok(size) = terminal.size() {
        state.update_terminal_size(size.width, size.height);
    }

    match rt.block_on(ipc.connect()) {
        Ok(()) => {
            state.connected = true;
            log_buffer.push_info("✅ 已连接到后台引擎".to_string());
        }
        Err(_) => {
            state.connected = false;
            log_buffer.push_info("❌ 无法连接到后台引擎，请先启动 start_daemon.py".to_string());
            log_buffer.push_info("提示: 界面将在无后台连接的情况下运行，部分功能不可用".to_string());
        }
    }

    let mut full_quit = false;
    let ipc_poll_interval = Duration::from_secs(1);
    let clock_interval = Duration::from_millis(500);
    let mut last_ipc_poll = std::time::Instant::now();
    let mut last_clock_refresh = std::time::Instant::now();
    let mut poll_counter: u64 = 0;

    loop {
        if state.should_quit {
            break;
        }

        if let Some(ct) = state.click_time {
            if state.clicked_button.is_some() && ct.elapsed() > Duration::from_millis(120) {
                state.clicked_button = None;
                state.click_time = None;
                state.needs_redraw = true;
            }
        }
        if let Some(ct) = state.daemon_menu_click_time {
            if state.clicked_daemon_menu_item.is_some() && ct.elapsed() > Duration::from_millis(120) {
                state.clicked_daemon_menu_item = None;
                state.daemon_menu_click_time = None;
                state.needs_redraw = true;
            }
        }
        if let Some(ct) = state.dialog_click_time {
            if state.clicked_dialog_button.is_some() && ct.elapsed() > Duration::from_millis(120) {
                state.clicked_dialog_button = None;
                state.dialog_click_time = None;
                state.needs_redraw = true;
            }
        }
        if let Some(ct) = state.detail_click_time {
            if state.clicked_detail_row.is_some() && ct.elapsed() > Duration::from_millis(20) {
                state.clicked_detail_row = None;
                state.detail_click_time = None;
                state.needs_redraw = true;
            }
        }
        // 设置页面字段点击动画超时
        if let Some(ref mut ss) = state.settings_state {
            if let Some(ct) = ss.field_click_time {
                if ss.clicked_field.is_some() && ct.elapsed() > Duration::from_millis(20) {
                    ss.clicked_field = None;
                    ss.field_click_time = None;
                    state.needs_redraw = true;
                }
            }
        }

        if let Some(cmd) = state.pending_command.take() {
            let result = rt.block_on(command::dispatch_command(&cmd, &mut ipc, &mut state, &mut log_buffer));
            match result {
                command::CommandResult::Quit => {
                    state.should_quit = true;
                }
                command::CommandResult::FullQuit => {
                    full_quit = true;
                    if ipc.is_connected() {
                        let _ = rt.block_on(ipc.full_quit());
                    }
                    rt.block_on(ipc.disconnect());
                    state.should_quit = true;
                }
                command::CommandResult::StartDaemon => {
                    if ipc.is_connected() {
                        log_buffer.push_info("⚠️ 已连接到后台引擎，无需重复启动".to_string());
                    } else {
                        match daemon.launch(&project_dir) {
                            Ok(pid) => {
                                log_buffer.push_info(format!("⚠️ 后台引擎正在启动 (PID: {})，等待 IPC 就绪...", pid));
                                DaemonManager::reconnect_ipc_after_launch(&rt, &mut ipc, &mut state, &mut log_buffer);
                            }
                            Err(e) => {
                                log_buffer.push_info(format!("❌ 启动后台引擎失败: {}", e));
                            }
                        }
                    }
                }
                command::CommandResult::RestartDaemon => {
                    daemon.restart_with_ipc(&mut ipc, &rt, &mut state, &mut log_buffer, &project_dir);
                }
                command::CommandResult::StopDaemon => {
                    daemon.stop_with_ipc(&mut ipc, &rt, &mut state, &mut log_buffer, &project_dir);
                }
                _ => {}
            }
        }

        let first_poll_timeout = Duration::from_millis(50);
        if crossterm_event::poll(first_poll_timeout).map_err(|e| e.to_string())? {
            let event = crossterm_event::read().map_err(|e| e.to_string())?;
            process_event(event, &mut state, &mut log_buffer, &mut ipc, &mut daemon, &rt, &project_dir, &mut full_quit);

            while crossterm_event::poll(Duration::from_millis(0)).map_err(|e| e.to_string())? {
                let event = crossterm_event::read().map_err(|e| e.to_string())?;
                process_event(event, &mut state, &mut log_buffer, &mut ipc, &mut daemon, &rt, &project_dir, &mut full_quit);
            }
        }

        if state.needs_redraw {
            let _ = do_redraw(terminal, &mut state, &log_buffer);
            state.needs_redraw = false;
        }

        if ipc.is_connected() && last_ipc_poll.elapsed() >= ipc_poll_interval {
            if let Ok(resp) = rt.block_on(ipc.get_all_status()) {
                if resp.is_ok() {
                    state.update_status_data(&resp.data);
                }
            }

            poll_counter += 1;
            if poll_counter >= 5 {
                if let Ok(resp) = rt.block_on(ipc.get_engine_status()) {
                    if resp.is_ok() {
                        state.update_engine_info(&resp.data);
                    }
                }
                poll_counter = 0;
            }

            if let Ok(resp) = rt.block_on(ipc.get_log_entries(state.last_log_id, 50, state.log_filter_level.as_deref(), state.log_filter_source.as_deref())) {
                if resp.is_ok() {
                    if let Some(data_obj) = resp.data.as_object() {
                        if let Some(entries) = data_obj.get("entries").and_then(|v| v.as_array()) {
                            for entry_val in entries {
                                if let Some(entry) = crate::state::log_buffer::LogEntry::from_dict(entry_val) {
                                    state.last_log_id = state.last_log_id.max(entry.id);
                                    log_buffer.push_detail(entry);
                                }
                            }
                        }
                        if let Some(latest) = data_obj.get("latest_id").and_then(|v| v.as_u64()) {
                            state.last_log_id = state.last_log_id.max(latest);
                        }
                    }
                }
            }

            last_ipc_poll = std::time::Instant::now();
            state.needs_redraw = true;
        }

        if last_clock_refresh.elapsed() >= clock_interval {
            state.needs_redraw = true;
            last_clock_refresh = std::time::Instant::now();
        }
    }

    rt.block_on(ipc.disconnect());
    if full_quit {
        let _ = daemon.stop(&project_dir);
    }

    Ok(())
}

fn process_event(
    event: CrosstermEvent,
    state: &mut AppState,
    log_buffer: &mut LogBuffer,
    ipc: &mut IpcClient,
    daemon: &mut daemon_mgr::DaemonManager,
    rt: &tokio::runtime::Runtime,
    project_dir: &str,
    full_quit: &mut bool,
) {
    match event {
        CrosstermEvent::Key(key) if key.kind == crossterm::event::KeyEventKind::Press => {
            let action = key_handler::handle_key(key, state);
            match action {
                key_handler::AppAction::Quit => {
                    state.should_quit = true;
                }
                key_handler::AppAction::SubmitCommand(cmd) => {
                    log_buffer.push_info(format!("> {}", cmd));
                    let result = rt.block_on(command::dispatch_command(&cmd, ipc, state, log_buffer));
                    match result {
                        command::CommandResult::Quit => {
                            state.should_quit = true;
                        }
                        command::CommandResult::FullQuit => {
                            *full_quit = true;
                            if ipc.is_connected() {
                                let _ = rt.block_on(ipc.full_quit());
                            }
                            rt.block_on(ipc.disconnect());
                            state.should_quit = true;
                        }
                        command::CommandResult::StartDaemon => {
                            if ipc.is_connected() {
                                log_buffer.push_info("⚠️ 已连接到后台引擎，无需重复启动".to_string());
                            } else {
                                match daemon.launch(project_dir) {
                                    Ok(pid) => {
                                        log_buffer.push_info(format!("⚠️ 后台引擎正在启动 (PID: {})，等待 IPC 就绪...", pid));
                                        DaemonManager::reconnect_ipc_after_launch(&rt, ipc, state, log_buffer);
                                    }
                                    Err(e) => {
                                        log_buffer.push_info(format!("❌ 启动后台引擎失败: {}", e));
                                    }
                                }
                            }
                        }
                        command::CommandResult::RestartDaemon => {
                            daemon.restart_with_ipc(ipc, &rt, state, log_buffer, project_dir);
                        }
                        command::CommandResult::StopDaemon => {
                            daemon.stop_with_ipc(ipc, rt, state, log_buffer, project_dir);
                        }
                        _ => {}
                    }
                }
                key_handler::AppAction::Confirm => {
                    if let Some(callback) = state.confirm_callback.take() {
                        let result = rt.block_on(command::execute_confirm_action(&callback, ipc, log_buffer));
                        match result {
                            command::CommandResult::FullQuit => {
                                *full_quit = true;
                                state.should_quit = true;
                            }
                            command::CommandResult::StopDaemon => {
                                daemon.stop_with_ipc(ipc, rt, state, log_buffer, project_dir);
                            }
                            _ => {}
                        }
                    }
                }
                key_handler::AppAction::Cancel | key_handler::AppAction::DismissDialog => {}
                key_handler::AppAction::DiscardSettings => {
                    state.close_settings();
                }
                key_handler::AppAction::SaveSettings => {
                    if let Some(ref mut ss) = state.settings_state {
                        ss.validation_errors.clear();
                        ss.save_error = None;
                        match ss.save() {
                            Ok(()) => {
                                ss.saved = true;
                                // Notify daemon via IPC
                                if ipc.is_connected() {
                                    let _ = rt.block_on(ipc.reload_config());
                                }
                                log_buffer.push_info(" 设置已保存到 autofluid_config.toml".to_string());
                                log_buffer.push_info(" 后台引擎配置已重新加载".to_string());
                            }
                            Err(errors) => {
                                ss.validation_errors = errors;
                                ss.save_error = Some("保存失败，请修正错误后重试".to_string());
                                state.needs_redraw = true;
                            }
                        }
                    }
                }
                key_handler::AppAction::None => {}
            }
        }
        CrosstermEvent::Mouse(mouse) => {
            event_handler::mouse::handle_mouse(mouse, state, log_buffer, ipc, rt, full_quit);
        }
        CrosstermEvent::Resize(w, h) => {
            state.update_terminal_size(w, h);
            state.needs_redraw = true;
        }
        _ => {}
    }
}

fn do_redraw(
    terminal: &mut Terminal<CrosstermBackend<io::Stdout>>,
    state: &mut AppState,
    log_buffer: &LogBuffer,
) -> Result<(), String> {
    terminal.draw(|frame| {
        let area = frame.area();
        let layout = AppLayout::new(area);

        state.clamp_table_scroll(layout.status_table.height.saturating_sub(3));

        let info_lines = ui::logs::compute_info_lines_no_wrap(log_buffer);
        let info_visual_count = info_lines.0.len();
        let info_max_width = info_lines.1;
        let detail_lines = ui::logs::compute_detail_lines_no_wrap(log_buffer, &state.log_filter_level, &state.log_filter_source);
        let detail_visual_count = detail_lines.0.len();
        let detail_max_width = detail_lines.1;

        let info_inner_height = layout.info_panel.height.saturating_sub(2) as usize;
        let detail_inner_height = layout.detail_panel.height.saturating_sub(2) as usize;
        let info_has_hscroll = info_max_width > layout.info_panel.width.saturating_sub(2) as usize;
        let detail_has_hscroll = detail_max_width > layout.detail_panel.width.saturating_sub(2) as usize;
        let info_content_height = if info_has_hscroll { info_inner_height.saturating_sub(1) } else { info_inner_height };
        let detail_content_height = if detail_has_hscroll { detail_inner_height.saturating_sub(1) } else { detail_inner_height };

        fn apply_auto_scroll(auto_scroll: &mut bool, scroll: &mut u16, visual_count: usize, content_height: usize) {
            if *auto_scroll && visual_count > content_height {
                *scroll = (visual_count - content_height) as u16;
            }
            if visual_count > content_height {
                let max_scroll = (visual_count - content_height) as u16;
                if *scroll >= max_scroll {
                    *auto_scroll = true;
                }
            }
        }

        if log_buffer.log_generation != state.last_log_generation {
            state.info_log_auto_scroll = true;
            state.last_log_generation = log_buffer.log_generation;
        }

        apply_auto_scroll(&mut state.info_log_auto_scroll, &mut state.info_log_scroll, info_visual_count, info_content_height);
        apply_auto_scroll(&mut state.detail_log_auto_scroll, &mut state.detail_log_scroll, detail_visual_count, detail_content_height);
        state.clamp_detail_scroll(detail_visual_count as u16, detail_content_height as u16);
        state.clamp_info_scroll(info_visual_count as u16, info_content_height as u16);

        let info_inner_width = layout.info_panel.width.saturating_sub(2) as usize;
        let info_has_vscroll = info_visual_count > info_content_height;
        let info_content_width = if info_has_vscroll { info_inner_width.saturating_sub(1) } else { info_inner_width };
        state.clamp_info_hscroll(info_max_width, info_content_width);

        let detail_inner_width = layout.detail_panel.width.saturating_sub(2) as usize;
        let detail_has_vscroll = detail_visual_count > detail_content_height;
        let detail_content_width = if detail_has_vscroll { detail_inner_width.saturating_sub(1) } else { detail_inner_width };
        state.clamp_detail_hscroll(detail_max_width, detail_content_width);

        state.scrollbar_info.table_v = {
            let visible_data_rows = layout.status_table.height.saturating_sub(3) as usize;
            if state.configs.len() > visible_data_rows {
                let table_inner = ratatui::layout::Rect {
                    x: layout.status_table.x + 1,
                    y: layout.status_table.y + 1,
                    width: layout.status_table.width.saturating_sub(2),
                    height: layout.status_table.height.saturating_sub(2),
                };
                let sb_area = ratatui::layout::Rect {
                    x: table_inner.x + table_inner.width.saturating_sub(1),
                    y: table_inner.y + 1,
                    width: 1,
                    height: table_inner.height.saturating_sub(1),
                };
                Some((sb_area, state.configs.len(), visible_data_rows, state.table_scroll_offset as usize))
            } else {
                None
            }
        };
        state.scrollbar_info.info_v = if info_has_vscroll {
            let info_inner = ratatui::layout::Rect {
                x: layout.info_panel.x + 1,
                y: layout.info_panel.y + 1,
                width: layout.info_panel.width.saturating_sub(2),
                height: layout.info_panel.height.saturating_sub(2),
            };
            let sb_area = ratatui::layout::Rect {
                x: info_inner.x + info_inner.width.saturating_sub(1),
                y: info_inner.y,
                width: 1,
                height: info_content_height as u16,
            };
            Some((sb_area, info_visual_count, info_content_height, state.info_log_scroll as usize))
        } else {
            None
        };
        state.scrollbar_info.info_h = if info_has_hscroll {
            let info_inner = ratatui::layout::Rect {
                x: layout.info_panel.x + 1,
                y: layout.info_panel.y + 1,
                width: layout.info_panel.width.saturating_sub(2),
                height: layout.info_panel.height.saturating_sub(2),
            };
            let sb_area = ratatui::layout::Rect {
                x: info_inner.x,
                y: info_inner.y + info_content_height as u16,
                width: info_content_width as u16,
                height: 1,
            };
            Some((sb_area, info_max_width, info_content_width, state.info_log_hscroll as usize))
        } else {
            None
        };
        state.scrollbar_info.detail_v = if detail_has_vscroll {
            let detail_inner = ratatui::layout::Rect {
                x: layout.detail_panel.x + 1,
                y: layout.detail_panel.y + 1,
                width: layout.detail_panel.width.saturating_sub(2),
                height: layout.detail_panel.height.saturating_sub(2),
            };
            let sb_area = ratatui::layout::Rect {
                x: detail_inner.x + detail_inner.width.saturating_sub(1),
                y: detail_inner.y,
                width: 1,
                height: detail_content_height as u16,
            };
            Some((sb_area, detail_visual_count, detail_content_height, state.detail_log_scroll as usize))
        } else {
            None
        };
        state.scrollbar_info.detail_h = if detail_has_hscroll {
            let detail_inner = ratatui::layout::Rect {
                x: layout.detail_panel.x + 1,
                y: layout.detail_panel.y + 1,
                width: layout.detail_panel.width.saturating_sub(2),
                height: layout.detail_panel.height.saturating_sub(2),
            };
            let sb_area = ratatui::layout::Rect {
                x: detail_inner.x,
                y: detail_inner.y + detail_content_height as u16,
                width: detail_content_width as u16,
                height: 1,
            };
            Some((sb_area, detail_max_width, detail_content_width, state.detail_log_hscroll as usize))
        } else {
            None
        };

        ui::header::render_header(frame, layout.header, state);
        ui::header::render_info_bar(frame, layout.info_bar, state);
        ui::table::render_table(frame, layout.status_table, state);
        ui::logs::render_info_panel(frame, layout.info_panel, log_buffer, state.info_log_scroll, state.focus_zone, state.info_log_hscroll, state.info_log_auto_scroll);
        ui::logs::render_detail_panel(
        frame,
        layout.detail_panel,
        &ui::logs::DetailPanelParams {
            log_buffer,
            level_filter: &state.log_filter_level,
            source_filter: &state.log_filter_source,
            scroll_offset: state.detail_log_scroll,
            auto_scroll: state.detail_log_auto_scroll,
            focus_zone: state.focus_zone,
            hovered_detail_row: state.hovered_detail_row,
            clicked_detail_row: state.clicked_detail_row,
            hscroll: state.detail_log_hscroll,
        },
    );
        ui::command_bar::render_command_bar(frame, layout.cmd_input, layout.quick_buttons, state);

        match state.ui_mode {
            UiMode::ConfirmDialog => {
                if let Some(ref msg) = state.confirm_message {
                    let info = ui::dialogs::render_confirm_dialog(frame, area, msg, state.dialog_scroll, state.hovered_dialog_button, state.clicked_dialog_button);
                    state.scrollbar_info.dialog_v = if info.content_total_lines > info.content_visible_lines {
                        Some((info.scrollbar_area, info.content_total_lines, info.content_visible_lines, state.dialog_scroll as usize))
                    } else {
                        None
                    };
                    state.dialog_button_bar_y = Some(info.button_bar_y);
                }
            }
            UiMode::CheckResult => {
                if let Some(ref data) = state.check_data {
                    let info = ui::dialogs::render_check_result(frame, area, data, state.dialog_scroll, state.hovered_dialog_button, state.clicked_dialog_button);
                    state.scrollbar_info.dialog_v = if info.content_total_lines > info.content_visible_lines {
                        Some((info.scrollbar_area, info.content_total_lines, info.content_visible_lines, state.dialog_scroll as usize))
                    } else {
                        None
                    };
                    state.dialog_button_bar_y = Some(info.button_bar_y);
                    state.clamp_dialog_scroll(info.content_total_lines, info.content_visible_lines);
                }
            }
            UiMode::Settings => {
                if let Some(ref mut ss) = state.settings_state {
                    let info = settings::settings_ui::render_settings_dialog(
                        frame,
                        area,
                        ss,
                        state.hovered_dialog_button,
                        state.clicked_dialog_button,
                    );
                    ss.field_positions = info.field_positions;
                    state.scrollbar_info.dialog_v = if info.content_total_lines > info.content_visible_lines {
                        Some((info.scrollbar_area, info.content_total_lines, info.content_visible_lines, ss.scroll as usize))
                    } else {
                        None
                    };
                    state.dialog_button_bar_y = Some(info.button_bar_y);
                    // Clamp settings scroll
                    let max_scroll = info.content_total_lines.saturating_sub(info.content_visible_lines) as u16;
                    if ss.scroll > max_scroll {
                        ss.scroll = max_scroll;
                    }
                }
            }
            UiMode::Normal => {
                state.scrollbar_info.dialog_v = None;
                state.dialog_button_bar_y = None;
            }
        }
    }).map_err(|e| e.to_string())?;

    Ok(())
}

pub(crate) fn point_in_rect(col: u16, row: u16, rect: ratatui::layout::Rect) -> bool {
    col >= rect.x && col < rect.x + rect.width && row >= rect.y && row < rect.y + rect.height
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_format_local_time_time_format() {
        let result = format_local_time("%H:%M:%S");
        assert_eq!(result.len(), 8, "时间格式应为 HH:MM:SS (8字符)");
        let parts: Vec<&str> = result.split(':').collect();
        assert_eq!(parts.len(), 3, "应包含3个部分");
        let h: u32 = parts[0].parse().expect("小时应为数字");
        let m: u32 = parts[1].parse().expect("分钟应为数字");
        let s: u32 = parts[2].parse().expect("秒应为数字");
        assert!(h < 24, "小时应在0-23之间");
        assert!(m < 60, "分钟应在0-59之间");
        assert!(s < 60, "秒应在0-59之间");
    }

    #[test]
    fn test_format_local_time_datetime_format() {
        let result = format_local_time("%Y%m%d_%H%M%S");
        assert_eq!(result.len(), 15, "日期时间格式应为 YYYYMMDD_HHMMSS (15字符)");
        let parts: Vec<&str> = result.split('_').collect();
        assert_eq!(parts.len(), 2, "应包含日期和时间两部分");
        assert_eq!(parts[0].len(), 8, "日期部分应为8字符");
        assert_eq!(parts[1].len(), 6, "时间部分应为6字符");
        let year: u32 = parts[0][0..4].parse().expect("年份应为数字");
        assert!(year >= 2024 && year <= 2100, "年份应在合理范围内");
        let month: u32 = parts[0][4..6].parse().expect("月份应为数字");
        assert!(month >= 1 && month <= 12, "月份应在1-12之间");
        let day: u32 = parts[0][6..8].parse().expect("日期应为数字");
        assert!(day >= 1 && day <= 31, "日期应在1-31之间");
    }

    #[test]
    fn test_generate_request_id() {
        let id1 = generate_request_id();
        let id2 = generate_request_id();
        assert_eq!(id1.len(), 8, "请求ID应为8字符");
        assert_eq!(id2.len(), 8, "请求ID应为8字符");
        assert!(id1.chars().all(|c| c.is_ascii_hexdigit()), "请求ID应为十六进制");
        assert!(id2.chars().all(|c| c.is_ascii_hexdigit()), "请求ID应为十六进制");
    }

    #[test]
    fn test_format_local_time_consistency() {
        let r1 = format_local_time("%H:%M:%S");
        let r2 = format_local_time("%H:%M:%S");
        assert_eq!(r1, r2, "同一秒内两次调用结果应相同");
    }
}
