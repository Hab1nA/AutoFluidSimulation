mod ipc;
mod settings;
mod state;
mod text_buffer;
mod ui;
mod event_handler;
mod daemon_mgr;
mod utils;

use std::io;
use std::sync::mpsc;
use std::time::Duration;

use serde_json::Value as JsonValue;

use crossterm::event::{self as crossterm_event, Event as CrosstermEvent, EnableMouseCapture, DisableMouseCapture};
use crossterm::terminal::{EnterAlternateScreen, LeaveAlternateScreen};
use crossterm::execute;
use ratatui::backend::CrosstermBackend;
use ratatui::Terminal;

use ipc::client::IpcClient;
use state::{AppState, LogBuffer};
use state::app_state::UiMode;
use state::log_buffer::LogEntry;
use ui::layout::AppLayout;
use event_handler::key_handler;
use event_handler::command;

pub use utils::format_local_time;

/// 初始化文件日志。
/// 优先使用 AUTOFLUID_SESSION_LOG_DIR 环境变量（由 Python 启动器设置）；
/// 若未设置，尝试查找 logs/client/ 下最新的时间戳子目录；
/// 若均不可用，回退到 stderr-only 模式。
fn init_file_logger() {
    use log::LevelFilter;
    use std::fs::OpenOptions;
    use std::path::PathBuf;

    let log_dir: Option<PathBuf> = std::env::var("AUTOFLUID_SESSION_LOG_DIR")
        .ok()
        .map(PathBuf::from)
        .filter(|p| p.is_dir())
        .or_else(find_latest_client_session_dir);

    match log_dir {
        Some(dir) => {
            let log_path = dir.join("autofluid-tui.log");
            match OpenOptions::new()
                .create(true)
                .append(true)
                .open(&log_path)
            {
                Ok(file) => {
                    env_logger::Builder::new()
                        .filter_level(LevelFilter::Info)
                        .target(env_logger::Target::Pipe(Box::new(file)))
                        .format_timestamp_millis()
                        .init();
                    log::info!("AutoFluid TUI v{} 启动，日志文件: {:?}", env!("CARGO_PKG_VERSION"), log_path);
                }
                Err(e) => {
                    eprintln!("警告: 无法创建日志文件 {:?}: {}", log_path, e);
                    init_stderr_logger();
                }
            }
        }
        None => init_stderr_logger(),
    }
}

fn init_stderr_logger() {
    env_logger::Builder::new()
        .filter_level(log::LevelFilter::Info)
        .target(env_logger::Target::Stderr)
        .format_timestamp_millis()
        .init();
    log::info!("AutoFluid TUI v{} 启动 (stderr-only 日志)", env!("CARGO_PKG_VERSION"));
}

/// 查找 logs/client/ 下最新的时间戳子目录
fn find_latest_client_session_dir() -> Option<std::path::PathBuf> {
    let client_dir = std::env::current_dir()
        .ok()?
        .join("logs")
        .join("client");
    if !client_dir.is_dir() {
        return None;
    }
    let mut entries: Vec<_> = std::fs::read_dir(&client_dir)
        .ok()?
        .filter_map(|e| e.ok())
        .filter(|e| e.path().is_dir())
        .collect();
    entries.sort_by_key(|b| std::cmp::Reverse(b.file_name()));
    entries.first().map(|e| e.path())
}

use std::sync::atomic::{AtomicU64, Ordering};

static REQUEST_COUNTER: AtomicU64 = AtomicU64::new(0);

pub fn generate_request_id() -> String {
    use std::time::SystemTime;
    let count = REQUEST_COUNTER.fetch_add(1, Ordering::Relaxed);
    let t = SystemTime::now()
        .duration_since(SystemTime::UNIX_EPOCH)
        .unwrap_or_default()
        .as_nanos();
    // 结合时间戳低位和原子计数器，确保唯一性
    let time_part = ((t as u64) ^ ((t >> 32) as u64)) & 0xFFFF_FFFF;
    let combined = time_part.wrapping_add(count);
    format!("{:08x}", combined)
}

// ====================================================================
// IPC 后台轮询线程
// ====================================================================

/// IPC 轮询线程发送给主循环的状态更新。
enum IpcPollUpdate {
    /// 周期性状态拉取结果
    Poll {
        status_data: Option<JsonValue>,
        engine_data: Option<JsonValue>,
        log_entries: Vec<LogEntry>,
        latest_id: u64,
    },
    /// IPC 连接状态变化（由轮询线程自动检测）
    ConnectionChanged { connected: bool },
}

/// 启动 IPC 后台轮询线程，返回接收端。
///
/// 轮询线程拥有独立的 tokio 运行时和 IpcClient，通过 mpsc 通道
/// 将状态更新发送给主循环。主线程不再因 IPC 网络阻塞而卡顿。
fn spawn_ipc_poll_thread() -> mpsc::Receiver<IpcPollUpdate> {
    let (tx, rx) = mpsc::channel();

    std::thread::Builder::new()
        .name("IPC-Poll".into())
        .spawn(move || {
            let rt = match tokio::runtime::Builder::new_current_thread()
                .enable_all()
                .build()
            {
                Ok(rt) => rt,
                Err(e) => {
                    log::error!("IPC 轮询线程 tokio 运行时创建失败: {e}");
                    return;
                }
            };

            let mut ipc = IpcClient::new(None, None);
            let mut connected = false;
            let mut poll_counter: u64 = 0;
            let mut last_log_id: u64 = 0;

            // 初始连接
            if rt.block_on(ipc.connect()).is_ok() {
                connected = true;
                let _ = tx.send(IpcPollUpdate::ConnectionChanged { connected: true });
            }

            loop {
                // 1s 轮询周期（与原主循环保持一致）
                std::thread::sleep(Duration::from_secs(1));

                // 尝试连接（如果断开）
                if !connected {
                    match rt.block_on(ipc.connect()) {
                        Ok(()) => {
                            connected = true;
                            let _ = tx.send(IpcPollUpdate::ConnectionChanged { connected: true });
                        }
                        Err(_) => continue,
                    }
                }

                // 拉取状态
                let status_data = match rt.block_on(ipc.get_all_status()) {
                    Ok(resp) if resp.is_ok() => Some(resp.data),
                    Ok(_) => None,
                    Err(_) => {
                        connected = false;
                        let _ = tx.send(IpcPollUpdate::ConnectionChanged { connected: false });
                        continue;
                    }
                };

                poll_counter += 1;
                let engine_data = if poll_counter >= 5 {
                    poll_counter = 0;
                    match rt.block_on(ipc.get_engine_status()) {
                        Ok(resp) if resp.is_ok() => Some(resp.data),
                        _ => None,
                    }
                } else {
                    None
                };

                let mut log_entries = Vec::new();
                match rt.block_on(ipc.get_log_entries(last_log_id, 50, None, None)) {
                    Ok(resp) if resp.is_ok() => {
                        if let Some(obj) = resp.data.as_object() {
                            if let Some(entries) = obj.get("entries").and_then(|v| v.as_array()) {
                                for val in entries {
                                    if let Some(entry) = LogEntry::from_dict(val) {
                                        last_log_id = last_log_id.max(entry.id);
                                        log_entries.push(entry);
                                    }
                                }
                            }
                            if let Some(lid) = obj.get("latest_id").and_then(|v| v.as_u64()) {
                                last_log_id = last_log_id.max(lid);
                            }
                        }
                    }
                    Err(_) => {
                        connected = false;
                        let _ = tx.send(IpcPollUpdate::ConnectionChanged { connected: false });
                        continue;
                    }
                    _ => {}
                }

                if tx.send(IpcPollUpdate::Poll {
                    status_data,
                    engine_data,
                    log_entries,
                    latest_id: last_log_id,
                }).is_err() {
                    break; // 主线程已关闭接收端
                }
            }
        })
        .expect("无法启动 IPC 轮询线程");

    rx
}

fn main() -> Result<(), Box<dyn std::error::Error>> {
    init_file_logger();
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

    // ★ 启动 IPC 后台轮询线程（非阻塞，独立 tokio 运行时）
    let ipc_poll_rx = spawn_ipc_poll_thread();

    let mut full_quit = false;
    let clock_interval = Duration::from_millis(500);
    let mut last_clock_refresh = std::time::Instant::now();

    loop {
        if state.should_quit {
            break;
        }

        state.tick();

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
                    if state.connected {
                        log_buffer.push_info("⚠️ 已连接到后台引擎，无需重复启动".to_string());
                    } else {
                        match daemon.launch(&project_dir) {
                            Ok(pid) => {
                                log_buffer.push_info(format!("⚠️ 后台引擎正在启动 (PID: {})，IPC 将自动连接...", pid));
                            }
                            Err(e) => {
                                log_buffer.push_info(format!("❌ 启动后台引擎失败: {}", e));
                            }
                        }
                    }
                }
                command::CommandResult::RestartDaemon => {
                    if ipc.is_connected() {
                        let _ = rt.block_on(ipc.full_quit());
                        rt.block_on(ipc.disconnect());
                    }
                    let _ = daemon.stop(&project_dir);
                    match daemon.launch(&project_dir) {
                        Ok(pid) => {
                            log_buffer.push_info(format!("⚠️ 后台引擎正在重启 (PID: {})，IPC 将自动连接...", pid));
                        }
                        Err(e) => {
                            log_buffer.push_info(format!("❌ 重启后台引擎失败: {}", e));
                        }
                    }
                    state.connected = false;
                }
                command::CommandResult::StopDaemon => {
                    if ipc.is_connected() {
                        let _ = rt.block_on(ipc.full_quit());
                        rt.block_on(ipc.disconnect());
                    }
                    let _ = daemon.stop(&project_dir);
                    state.connected = false;
                    log_buffer.push_info("✅ 后台引擎已停止".to_string());
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

        // ★ 非阻塞接收 IPC 轮询线程的状态更新
        while let Ok(update) = ipc_poll_rx.try_recv() {
            match update {
                IpcPollUpdate::Poll { status_data, engine_data, log_entries, latest_id } => {
                    if let Some(data) = status_data {
                        state.update_status_data(&data);
                    }
                    if let Some(data) = engine_data {
                        state.update_engine_info(&data);
                    }
                    for entry in log_entries {
                        log_buffer.push_detail(entry);
                    }
                    state.last_log_id = state.last_log_id.max(latest_id);
                    state.needs_redraw = true;
                }
                IpcPollUpdate::ConnectionChanged { connected } => {
                    if connected && !state.connected {
                        log_buffer.push_info("✅ 已连接到后台引擎".to_string());
                    } else if !connected && state.connected {
                        log_buffer.push_info("⚠️ 与后台引擎的连接已断开，正在重试...".to_string());
                    }
                    state.connected = connected;
                    state.needs_redraw = true;
                }
            }
        }

        if last_clock_refresh.elapsed() >= clock_interval {
            state.needs_redraw = true;
            last_clock_refresh = std::time::Instant::now();
        }
    }

    // IPC 轮询线程会在 rx 被 drop 时自动退出
    drop(ipc_poll_rx);

    if full_quit {
        let _ = daemon.stop(&project_dir);
    }

    Ok(())
}

#[allow(clippy::too_many_arguments)] // 事件处理函数需要访问多个上下文对象
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
                            if state.connected {
                                log_buffer.push_info("⚠️ 已连接到后台引擎，无需重复启动".to_string());
                            } else {
                                match daemon.launch(project_dir) {
                                    Ok(pid) => {
                                        log_buffer.push_info(format!("⚠️ 后台引擎正在启动 (PID: {})，IPC 将自动连接...", pid));
                                    }
                                    Err(e) => {
                                        log_buffer.push_info(format!("❌ 启动后台引擎失败: {}", e));
                                    }
                                }
                            }
                        }
                        command::CommandResult::RestartDaemon => {
                            if ipc.is_connected() {
                                let _ = rt.block_on(ipc.full_quit());
                                rt.block_on(ipc.disconnect());
                            }
                            let _ = daemon.stop(project_dir);
                            match daemon.launch(project_dir) {
                                Ok(pid) => {
                                    log_buffer.push_info(format!("⚠️ 后台引擎正在重启 (PID: {})，IPC 将自动连接...", pid));
                                }
                                Err(e) => {
                                    log_buffer.push_info(format!("❌ 重启后台引擎失败: {}", e));
                                }
                            }
                            state.connected = false;
                        }
                        command::CommandResult::StopDaemon => {
                            if ipc.is_connected() {
                                let _ = rt.block_on(ipc.full_quit());
                                rt.block_on(ipc.disconnect());
                            }
                            let _ = daemon.stop(project_dir);
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
                                *full_quit = true;
                                if ipc.is_connected() {
                                    let _ = rt.block_on(ipc.full_quit());
                                }
                                rt.block_on(ipc.disconnect());
                                state.should_quit = true;
                            }
                            command::CommandResult::StopDaemon => {
                                if ipc.is_connected() {
                                    let _ = rt.block_on(ipc.full_quit());
                                    rt.block_on(ipc.disconnect());
                                }
                                let _ = daemon.stop(project_dir);
                                state.connected = false;
                                log_buffer.push_info("✅ 后台引擎已停止".to_string());
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
