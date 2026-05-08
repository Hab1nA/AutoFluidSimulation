mod ipc;
mod state;
mod ui;
mod event_handler;
mod daemon_mgr;

use std::io;
use std::time::Duration;

use crossterm::event::{self as crossterm_event, Event as CrosstermEvent, MouseEvent, MouseEventKind, EnableMouseCapture, DisableMouseCapture};
use crossterm::terminal::{EnterAlternateScreen, LeaveAlternateScreen};
use crossterm::execute;
use ratatui::backend::CrosstermBackend;
use ratatui::Terminal;

use ipc::client::IpcClient;
use state::{AppState, LogBuffer};
use state::app_state::{FocusZone, UiMode};
use ui::layout::AppLayout;
use event_handler::key_handler;
use event_handler::command;

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
    let rt = tokio::runtime::Runtime::new().map_err(|e| e.to_string())?;

    let mut ipc = IpcClient::new(None, None);
    let mut state = AppState::new();
    let mut log_buffer = LogBuffer::new();
    let mut daemon = daemon_mgr::DaemonManager::new();

    log_buffer.push_info("欢迎使用仿真流水线总控程序！".to_string());
    log_buffer.push_info("正在连接后台引擎...".to_string());
    log_buffer.push_info("Tab 切换焦点区域 | ↑↓ 滚动 | PageUp/PageDown 翻页 | Home/End 跳转".to_string());

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

    let ipc_poll_interval = Duration::from_secs(1);
    let clock_interval = Duration::from_secs(1);
    let mut last_ipc_poll = std::time::Instant::now();
    let mut last_clock_refresh = std::time::Instant::now();
    let mut poll_counter: u64 = 0;

    loop {
        if state.should_quit {
            break;
        }

        if let Some(cmd) = state.pending_command.take() {
            let result = rt.block_on(command::dispatch_command(&cmd, &mut ipc, &mut state, &mut log_buffer));
            match result {
                command::CommandResult::Quit => {
                    state.should_quit = true;
                }
                command::CommandResult::FullQuit => {
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
                        let project_dir = std::env::current_dir()
                            .unwrap_or_default()
                            .to_string_lossy()
                            .to_string();
                        match daemon.launch(&project_dir) {
                            Ok(pid) => {
                                log_buffer.push_info(format!("⚠️ 后台引擎正在启动 (PID: {})，等待 IPC 就绪...", pid));
                            }
                            Err(e) => {
                                log_buffer.push_info(format!("❌ 启动后台引擎失败: {}", e));
                            }
                        }
                    }
                }
                command::CommandResult::StopDaemon => {
                    if ipc.is_connected() {
                        let _ = rt.block_on(ipc.full_quit());
                        rt.block_on(ipc.disconnect());
                    }
                    let _ = daemon.stop();
                    state.connected = false;
                    log_buffer.push_info("✅ 后台引擎已停止".to_string());
                }
                _ => {}
            }
        }

        if crossterm_event::poll(Duration::from_millis(50)).map_err(|e| e.to_string())? {
            match crossterm_event::read().map_err(|e| e.to_string())? {
                CrosstermEvent::Key(key) if key.kind == crossterm::event::KeyEventKind::Press => {
                    let action = key_handler::handle_key(key, &mut state);
                    match action {
                            key_handler::AppAction::Quit => {
                                state.should_quit = true;
                            }
                            key_handler::AppAction::SubmitCommand(cmd) => {
                                log_buffer.push_info(format!("> {}", cmd));
                                let result = rt.block_on(command::dispatch_command(&cmd, &mut ipc, &mut state, &mut log_buffer));
                                match result {
                                    command::CommandResult::Quit => {
                                        state.should_quit = true;
                                    }
                                    command::CommandResult::FullQuit => {
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
                                            let project_dir = std::env::current_dir()
                                                .unwrap_or_default()
                                                .to_string_lossy()
                                                .to_string();
                                            match daemon.launch(&project_dir) {
                                                Ok(pid) => {
                                                    log_buffer.push_info(format!("⚠️ 后台引擎正在启动 (PID: {})，等待 IPC 就绪...", pid));
                                                }
                                                Err(e) => {
                                                    log_buffer.push_info(format!("❌ 启动后台引擎失败: {}", e));
                                                }
                                            }
                                        }
                                    }
                                    command::CommandResult::StopDaemon => {
                                        if ipc.is_connected() {
                                            let _ = rt.block_on(ipc.full_quit());
                                            rt.block_on(ipc.disconnect());
                                        }
                                        let _ = daemon.stop();
                                        state.connected = false;
                                        log_buffer.push_info("✅ 后台引擎已停止".to_string());
                                    }
                                    _ => {}
                                }
                            }
                            key_handler::AppAction::Confirm => {
                                if let Some(callback) = state.confirm_callback.take() {
                                    let result = rt.block_on(command::execute_confirm_action(&callback, &mut ipc, &mut log_buffer));
                                    match result {
                                        command::CommandResult::FullQuit => {
                                            state.should_quit = true;
                                        }
                                        command::CommandResult::StopDaemon => {
                                            if ipc.is_connected() {
                                                let _ = rt.block_on(ipc.full_quit());
                                                rt.block_on(ipc.disconnect());
                                            }
                                            let _ = daemon.stop();
                                            state.connected = false;
                                            log_buffer.push_info("✅ 后台引擎已停止".to_string());
                                        }
                                        _ => {}
                                    }
                                }
                            }
                            key_handler::AppAction::Cancel | key_handler::AppAction::DismissDialog => {}
                            key_handler::AppAction::None => {}
                        }
                }
                CrosstermEvent::Mouse(mouse) => {
                    handle_mouse(mouse, &mut state, &log_buffer);
                }
                CrosstermEvent::Resize(w, h) => {
                    state.update_terminal_size(w, h);
                    state.needs_redraw = true;
                }
                _ => {}
            }
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

        if state.needs_redraw {
            terminal.draw(|frame| {
                let area = frame.area();
                let layout = AppLayout::new(area);

                state.clamp_table_scroll(layout.status_table.height.saturating_sub(2));

                let detail_count = log_buffer.filtered_entries_count(&state.log_filter_level, &state.log_filter_source);
                if state.detail_log_auto_scroll {
                    let visible = layout.detail_panel_body.height;
                    if detail_count > visible as usize {
                        state.detail_log_scroll = (detail_count - visible as usize) as u16;
                    }
                }
                state.clamp_detail_scroll(detail_count as u16, layout.detail_panel_body.height);
                state.clamp_info_scroll(log_buffer.info_messages.len() as u16, layout.info_panel_body.height.saturating_sub(2));

                ui::header::render_header(frame, layout.header, &state);
                ui::header::render_info_bar(frame, layout.info_bar, &state);
                ui::table::render_table(frame, layout.status_table, &state);
                ui::logs::render_info_panel(frame, layout.info_panel, &log_buffer, state.info_log_scroll, state.focus_zone);
                ui::logs::render_detail_panel(
                    frame,
                    layout.detail_panel_title,
                    layout.detail_panel_body,
                    &log_buffer,
                    &state.log_filter_level,
                    &state.log_filter_source,
                    state.detail_log_scroll,
                    state.detail_log_auto_scroll,
                    state.focus_zone,
                );
                ui::command_bar::render_command_bar(frame, layout.cmd_input, layout.quick_buttons, &state);

                match state.ui_mode {
                    UiMode::ConfirmDialog => {
                        if let Some(ref msg) = state.confirm_message {
                            ui::dialogs::render_confirm_dialog(frame, area, msg);
                        }
                    }
                    UiMode::CheckResult => {
                        if let Some(ref data) = state.check_data {
                            ui::dialogs::render_check_result(frame, area, data);
                        }
                    }
                    UiMode::Normal => {}
                }
            }).map_err(|e| e.to_string())?;

            state.needs_redraw = false;
        }
    }

    rt.block_on(ipc.disconnect());
    let _ = daemon.stop();

    Ok(())
}

fn handle_mouse(mouse: MouseEvent, state: &mut AppState, _log_buffer: &LogBuffer) {
    let area = state.terminal_size;
    if area.width == 0 || area.height == 0 {
        return;
    }

    let layout = AppLayout::new(ratatui::layout::Rect::new(0, 0, area.width, area.height));
    let col = mouse.column;
    let row = mouse.row;

    let in_table = point_in_rect(col, row, layout.status_table);
    let in_info = point_in_rect(col, row, layout.info_panel);
    let in_detail = point_in_rect(col, row, layout.detail_panel);
    let in_buttons = point_in_rect(col, row, layout.quick_buttons);

    match mouse.kind {
        MouseEventKind::ScrollUp => {
            if in_table {
                if state.table_scroll_offset > 0 {
                    state.table_scroll_offset -= 1;
                    state.focus_zone = FocusZone::Table;
                    state.needs_redraw = true;
                }
            } else if in_info {
                if state.info_log_scroll > 0 {
                    state.info_log_scroll -= 3;
                    state.focus_zone = FocusZone::InfoLog;
                    state.needs_redraw = true;
                }
            } else if in_detail && state.detail_log_scroll > 0 {
                state.detail_log_scroll = state.detail_log_scroll.saturating_sub(3);
                state.detail_log_auto_scroll = false;
                state.focus_zone = FocusZone::DetailLog;
                state.needs_redraw = true;
            }
        }
        MouseEventKind::ScrollDown => {
            if in_table {
                state.table_scroll_offset = state.table_scroll_offset.saturating_add(1);
                state.focus_zone = FocusZone::Table;
                state.needs_redraw = true;
            } else if in_info {
                state.info_log_scroll = state.info_log_scroll.saturating_add(3);
                state.focus_zone = FocusZone::InfoLog;
                state.needs_redraw = true;
            } else if in_detail {
                state.detail_log_scroll = state.detail_log_scroll.saturating_add(3);
                state.focus_zone = FocusZone::DetailLog;
                state.needs_redraw = true;
            }
        }
        MouseEventKind::Down(_button) => {
            if in_buttons {
                handle_button_click(col, row, &layout.quick_buttons, state);
            } else if in_table {
                state.focus_zone = FocusZone::Table;
                state.needs_redraw = true;
            } else if in_info {
                state.focus_zone = FocusZone::InfoLog;
                state.needs_redraw = true;
            } else if in_detail {
                state.focus_zone = FocusZone::DetailLog;
                state.needs_redraw = true;
            }
        }
        _ => {}
    }
}

fn point_in_rect(col: u16, row: u16, rect: ratatui::layout::Rect) -> bool {
    col >= rect.x && col < rect.x + rect.width && row >= rect.y && row < rect.y + rect.height
}

fn handle_button_click(col: u16, row: u16, buttons_area: &ratatui::layout::Rect, state: &mut AppState) {
    if !point_in_rect(col, row, *buttons_area) {
        return;
    }

    let rel_col = col - buttons_area.x;
    let button_specs: [(u16, u16, &str); 8] = [
        (1, 10, "start"),
        (12, 11, "pause"),
        (24, 11, "check"),
        (36, 11, "status"),
        (48, 13, "daemon start"),
        (62, 10, "stop"),
        (73, 10, "quit"),
        (84, 13, "quit full"),
    ];

    for (start, width, cmd) in button_specs {
        if rel_col >= start && rel_col < start + width {
            state.pending_command = Some(cmd.to_string());
            state.focus_zone = FocusZone::CommandInput;
            state.needs_redraw = true;
            return;
        }
    }
}
