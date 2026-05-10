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
use ui::command_bar::BUTTON_DEFS;
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
        if let Some(ct) = state.dialog_click_time {
            if state.clicked_dialog_button.is_some() && ct.elapsed() > Duration::from_millis(120) {
                state.clicked_dialog_button = None;
                state.dialog_click_time = None;
                state.needs_redraw = true;
            }
        }
        if let Some(ct) = state.detail_click_time {
            if state.clicked_detail_row.is_some() && ct.elapsed() > Duration::from_millis(120) {
                state.clicked_detail_row = None;
                state.detail_click_time = None;
                state.needs_redraw = true;
            }
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

        let first_poll_timeout = Duration::from_millis(50);
        if crossterm_event::poll(first_poll_timeout).map_err(|e| e.to_string())? {
            let event = crossterm_event::read().map_err(|e| e.to_string())?;
            process_event(event, &mut state, &mut log_buffer, &mut ipc, &mut daemon, &rt);

            while crossterm_event::poll(Duration::from_millis(0)).map_err(|e| e.to_string())? {
                let event = crossterm_event::read().map_err(|e| e.to_string())?;
                process_event(event, &mut state, &mut log_buffer, &mut ipc, &mut daemon, &rt);
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
    let _ = daemon.stop();

    Ok(())
}

fn process_event(
    event: CrosstermEvent,
    state: &mut AppState,
    log_buffer: &mut LogBuffer,
    ipc: &mut IpcClient,
    daemon: &mut daemon_mgr::DaemonManager,
    rt: &tokio::runtime::Runtime,
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
                        let result = rt.block_on(command::execute_confirm_action(&callback, ipc, log_buffer));
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
            handle_mouse(mouse, state, log_buffer, ipc, rt);
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

        if state.detail_log_auto_scroll {
            if detail_visual_count > detail_content_height {
                state.detail_log_scroll = (detail_visual_count - detail_content_height) as u16;
            }
        }
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

        ui::header::render_header(frame, layout.header, &state);
        ui::header::render_info_bar(frame, layout.info_bar, &state);
        ui::table::render_table(frame, layout.status_table, &state);
        ui::logs::render_info_panel(frame, layout.info_panel, &log_buffer, state.info_log_scroll, state.focus_zone, state.info_log_hscroll);
        ui::logs::render_detail_panel(
            frame,
            layout.detail_panel,
            &log_buffer,
            &state.log_filter_level,
            &state.log_filter_source,
            state.detail_log_scroll,
            state.detail_log_auto_scroll,
            state.focus_zone,
            state.hovered_detail_row,
            state.clicked_detail_row,
            state.detail_log_hscroll,
        );
        ui::command_bar::render_command_bar(frame, layout.cmd_input, layout.quick_buttons, &state);

        match state.ui_mode {
            UiMode::ConfirmDialog => {
                if let Some(ref msg) = state.confirm_message {
                    ui::dialogs::render_confirm_dialog(frame, area, msg, state.hovered_dialog_button, state.clicked_dialog_button);
                }
            }
            UiMode::CheckResult => {
                if let Some(ref data) = state.check_data {
                    ui::dialogs::render_check_result(frame, area, data, state.hovered_dialog_button, state.clicked_dialog_button);
                }
            }
            UiMode::Normal => {}
        }
    }).map_err(|e| e.to_string())?;

    Ok(())
}

fn point_in_rect(col: u16, row: u16, rect: ratatui::layout::Rect) -> bool {
    col >= rect.x && col < rect.x + rect.width && row >= rect.y && row < rect.y + rect.height
}

fn handle_mouse(mouse: MouseEvent, state: &mut AppState, log_buffer: &mut LogBuffer, ipc: &mut IpcClient, rt: &tokio::runtime::Runtime) {
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
    let in_cmd_input = point_in_rect(col, row, layout.cmd_input);

    match mouse.kind {
        MouseEventKind::Moved => {
            let prev_hover_row = state.hovered_table_row;
            let prev_hover_btn = state.hovered_button;
            let prev_hover_detail = state.hovered_detail_row;
            let prev_hover_dialog_btn = state.hovered_dialog_button;

            if in_table {
                let inner_y = row.saturating_sub(layout.status_table.y + 1);
                if inner_y > 0 {
                    let data_row = state.table_scroll_offset + inner_y - 1;
                    if (data_row as usize) < state.configs.len() {
                        state.hovered_table_row = Some(data_row);
                    } else {
                        state.hovered_table_row = None;
                    }
                } else {
                    state.hovered_table_row = None;
                }
            } else {
                state.hovered_table_row = None;
            }

            if in_detail {
                let inner_top = layout.detail_panel.y + 1;
                let inner_bottom = layout.detail_panel.y + layout.detail_panel.height.saturating_sub(1);
                if row >= inner_top && row < inner_bottom {
                    let inner_y = row - inner_top;
                    let visual_line = state.detail_log_scroll + inner_y;
                    state.hovered_detail_row = Some(visual_line);
                } else {
                    state.hovered_detail_row = None;
                }
            } else {
                state.hovered_detail_row = None;
            }

            if in_buttons {
                state.hovered_button = detect_button(col, row, &layout);
            } else {
                state.hovered_button = None;
            }

            if state.ui_mode == UiMode::ConfirmDialog || state.ui_mode == UiMode::CheckResult {
                state.hovered_dialog_button = detect_dialog_button(col, row, area, &state);
            } else {
                state.hovered_dialog_button = None;
            }

            if state.hovered_table_row != prev_hover_row
                || state.hovered_button != prev_hover_btn
                || state.hovered_detail_row != prev_hover_detail
                || state.hovered_dialog_button != prev_hover_dialog_btn
            {
                state.needs_redraw = true;
            }
        }
        MouseEventKind::ScrollUp => {
            if mouse.modifiers.contains(crossterm::event::KeyModifiers::CONTROL) {
                if in_info {
                    state.info_log_hscroll = state.info_log_hscroll.saturating_sub(5);
                    state.needs_redraw = true;
                } else if in_detail {
                    state.detail_log_hscroll = state.detail_log_hscroll.saturating_sub(5);
                    state.needs_redraw = true;
                }
            } else {
                if in_table {
                    if state.table_scroll_offset > 0 {
                        state.table_scroll_offset -= 1;
                        state.focus_zone = FocusZone::Table;
                        state.needs_redraw = true;
                    }
                } else if in_info {
                    if state.info_log_scroll > 0 {
                        state.info_log_scroll = state.info_log_scroll.saturating_sub(3);
                        state.focus_zone = FocusZone::InfoLog;
                        state.needs_redraw = true;
                    }
                } else if in_detail {
                    if state.detail_log_scroll > 0 {
                        state.detail_log_scroll = state.detail_log_scroll.saturating_sub(3);
                        state.detail_log_auto_scroll = false;
                        state.focus_zone = FocusZone::DetailLog;
                        state.needs_redraw = true;
                    }
                }
            }
        }
        MouseEventKind::ScrollDown => {
            if mouse.modifiers.contains(crossterm::event::KeyModifiers::CONTROL) {
                if in_info {
                    state.info_log_hscroll = state.info_log_hscroll.saturating_add(5);
                    state.needs_redraw = true;
                } else if in_detail {
                    state.detail_log_hscroll = state.detail_log_hscroll.saturating_add(5);
                    state.needs_redraw = true;
                }
            } else {
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
        }
        MouseEventKind::Down(_button) => {
            if state.ui_mode == UiMode::ConfirmDialog || state.ui_mode == UiMode::CheckResult {
                if let Some(btn_idx) = detect_dialog_button(col, row, area, state) {
                    state.clicked_dialog_button = Some(btn_idx);
                    state.dialog_click_time = Some(std::time::Instant::now());
                    state.needs_redraw = true;
                    return;
                }
            }
            if in_buttons {
                if let Some(btn_idx) = detect_button(col, row, &layout) {
                    state.clicked_button = Some(btn_idx);
                    state.click_time = Some(std::time::Instant::now());
                    state.needs_redraw = true;
                }
            } else if in_table {
                state.focus_zone = FocusZone::Table;
                state.needs_redraw = true;
            } else if in_info {
                state.focus_zone = FocusZone::InfoLog;
                state.needs_redraw = true;
            } else if in_detail {
                state.focus_zone = FocusZone::DetailLog;
                let now = std::time::Instant::now();
                let is_double_click = state.last_detail_click_row == Some(row)
                    && state.last_detail_click_time.map_or(false, |t| now.duration_since(t).as_millis() < 400);
                if is_double_click {
                    let inner_top = layout.detail_panel.y + 1;
                    let inner_y = row - inner_top;
                    let visual_line = state.detail_log_scroll as usize + inner_y as usize;
                    let max_width = layout.detail_panel.width.saturating_sub(2) as usize;
                    if let Some(msg) = ui::logs::get_raw_message_at_visual_line(
                        log_buffer,
                        &state.log_filter_level,
                        &state.log_filter_source,
                        max_width,
                        visual_line,
                    ) {
                        if let Ok(mut clipboard) = arboard::Clipboard::new() {
                            if clipboard.set_text(&msg).is_ok() {
                                log_buffer.push_info(format!("已复制到剪贴板: {}", if msg.chars().count() > 60 { let s: String = msg.chars().take(60).collect(); format!("{}...", s) } else { msg.clone() }));
                            }
                        }
                    }
                    state.clicked_detail_row = Some(state.detail_log_scroll + inner_y);
                    state.detail_click_time = Some(now);
                    state.last_detail_click_time = None;
                    state.last_detail_click_row = None;
                } else {
                    state.last_detail_click_time = Some(now);
                    state.last_detail_click_row = Some(row);
                }
                state.needs_redraw = true;
            } else if in_cmd_input {
                state.focus_zone = FocusZone::CommandInput;
                state.needs_redraw = true;
            }
        }
        MouseEventKind::Up(_button) => {
            if let Some(btn_idx) = state.clicked_dialog_button {
                if let Some(hover_idx) = detect_dialog_button(col, row, area, state) {
                    if hover_idx == btn_idx {
                        handle_dialog_button_click(btn_idx, state, log_buffer, ipc, rt);
                    }
                }
                state.clicked_dialog_button = None;
                state.needs_redraw = true;
                return;
            }
            if let Some(btn_idx) = state.clicked_button {
                if in_buttons {
                    if let Some(hover_idx) = detect_button(col, row, &layout) {
                        if hover_idx == btn_idx {
                            let cmd = BUTTON_DEFS[btn_idx as usize].1;
                            state.pending_command = Some(cmd.to_string());
                            state.focus_zone = FocusZone::CommandInput;
                        }
                    }
                }
                state.clicked_button = None;
                state.needs_redraw = true;
            }
        }
        _ => {}
    }
}

fn detect_button(col: u16, row: u16, layout: &AppLayout) -> Option<u8> {
    let buttons_area = layout.quick_buttons;
    if !point_in_rect(col, row, buttons_area) {
        return None;
    }

    let rel_row = row.saturating_sub(buttons_area.y);
    if rel_row != 1 {
        return None;
    }

    let rel_col = col.saturating_sub(buttons_area.x);
    let total_width: u16 = BUTTON_DEFS.iter().map(|(l, _)| ui::command_bar::button_total_width(l) + 1).sum();
    let padding = buttons_area.width.saturating_sub(total_width) / 2;

    let mut offset = padding;
    for (i, (label, _cmd)) in BUTTON_DEFS.iter().enumerate() {
        let bw = ui::command_bar::button_total_width(label) + 1;
        if rel_col >= offset && rel_col < offset + bw {
            return Some(i as u8);
        }
        offset += bw;
    }
    None
}

fn dialog_centered_rect(percent_x: u16, percent_y: u16, r: ratatui::layout::Rect) -> ratatui::layout::Rect {
    let popup_layout = ratatui::layout::Layout::default()
        .direction(ratatui::layout::Direction::Vertical)
        .constraints([
            ratatui::layout::Constraint::Percentage((100 - percent_y) / 2),
            ratatui::layout::Constraint::Percentage(percent_y),
            ratatui::layout::Constraint::Percentage((100 - percent_y) / 2),
        ])
        .split(r);

    ratatui::layout::Layout::default()
        .direction(ratatui::layout::Direction::Horizontal)
        .constraints([
            ratatui::layout::Constraint::Percentage((100 - percent_x) / 2),
            ratatui::layout::Constraint::Percentage(percent_x),
            ratatui::layout::Constraint::Percentage((100 - percent_x) / 2),
        ])
        .split(popup_layout[1])[1]
}

fn detect_dialog_button(col: u16, row: u16, area: ratatui::layout::Rect, state: &AppState) -> Option<u8> {
    let dialog_area = match state.ui_mode {
        UiMode::ConfirmDialog => dialog_centered_rect(80, 30, area),
        UiMode::CheckResult => dialog_centered_rect(80, 50, area),
        UiMode::Normal => return None,
    };

    if !point_in_rect(col, row, dialog_area) {
        return None;
    }

    let inner = ratatui::layout::Rect {
        x: dialog_area.x + 1,
        y: dialog_area.y + 1,
        width: dialog_area.width.saturating_sub(2),
        height: dialog_area.height.saturating_sub(2),
    };

    match state.ui_mode {
        UiMode::ConfirmDialog => {
            let msg_line_count = state.confirm_message
                .as_ref()
                .map(|m| m.lines().count())
                .unwrap_or(0);
            let btn_row_count: usize = 2;
            let available_height = inner.height as usize;
            let top_pad = if available_height > msg_line_count + btn_row_count {
                (available_height - msg_line_count - btn_row_count) / 2
            } else {
                0
            };
            let btn_y = inner.y + (top_pad + msg_line_count + 2) as u16;
            if row != btn_y {
                return None;
            }

            let confirm_label = " 确认 [Y] ";
            let cancel_label = " 取消 [N] ";
            let confirm_w = unicode_width::UnicodeWidthStr::width(confirm_label) as u16;
            let cancel_w = unicode_width::UnicodeWidthStr::width(cancel_label) as u16;
            let gap: u16 = 3;
            let total_w = confirm_w + cancel_w + gap;
            let start_x = inner.x + (inner.width.saturating_sub(total_w)) / 2;

            let confirm_start = start_x;
            let confirm_end = confirm_start + confirm_w;
            let cancel_start = confirm_end + gap;
            let cancel_end = cancel_start + cancel_w;

            if col >= confirm_start && col < confirm_end {
                return Some(0);
            }
            if col >= cancel_start && col < cancel_end {
                return Some(1);
            }
            None
        }
        UiMode::CheckResult => {
            let content_lines = if let Some(data) = &state.check_data {
                ui::dialogs::count_check_result_lines(data)
            } else {
                0
            };

            let close_label = " 关闭 [Q] ";
            let close_w = unicode_width::UnicodeWidthStr::width(close_label) as u16;
            let btn_y = inner.y + content_lines.min(inner.height as usize).saturating_sub(1) as u16;
            if row != btn_y {
                return None;
            }
            let start_x = inner.x + (inner.width.saturating_sub(close_w)) / 2;

            if col >= start_x && col < start_x + close_w {
                return Some(0);
            }
            None
        }
        UiMode::Normal => None,
    }
}

fn handle_dialog_button_click(
    btn_idx: u8,
    state: &mut AppState,
    log_buffer: &mut LogBuffer,
    ipc: &mut IpcClient,
    rt: &tokio::runtime::Runtime,
) {
    match state.ui_mode {
        UiMode::ConfirmDialog => {
            match btn_idx {
                0 => {
                    if let Some(callback) = state.confirm_callback.take() {
                        let result = rt.block_on(command::execute_confirm_action(&callback, ipc, log_buffer));
                        match result {
                            command::CommandResult::FullQuit => {
                                state.should_quit = true;
                            }
                            command::CommandResult::StopDaemon => {
                                if ipc.is_connected() {
                                    let _ = rt.block_on(ipc.full_quit());
                                    rt.block_on(ipc.disconnect());
                                }
                                state.connected = false;
                                log_buffer.push_info("✅ 后台引擎已停止".to_string());
                            }
                            _ => {}
                        }
                    }
                    state.ui_mode = UiMode::Normal;
                    state.confirm_message = None;
                }
                1 => {
                    state.ui_mode = UiMode::Normal;
                    state.confirm_message = None;
                    state.confirm_callback = None;
                }
                _ => {}
            }
        }
        UiMode::CheckResult => {
            match btn_idx {
                0 => {
                    state.ui_mode = UiMode::Normal;
                    state.check_data = None;
                }
                _ => {}
            }
        }
        UiMode::Normal => {}
    }
}
