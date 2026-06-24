mod daemon_mgr;
mod event_handler;
mod ipc;
mod settings;
mod state;
mod text_buffer;
mod theme;
mod ui;
mod utils;
mod worker_mgr;

use std::io;
use std::time::{Duration, Instant};

use crossterm::event::{
    self as crossterm_event, DisableMouseCapture, EnableMouseCapture, Event as CrosstermEvent,
};
use crossterm::execute;
use crossterm::terminal::{EnterAlternateScreen, LeaveAlternateScreen};
use ratatui::backend::CrosstermBackend;
use ratatui::layout::{Alignment, Rect};
use ratatui::style::{Modifier, Style};
use ratatui::text::{Line, Span};
use ratatui::widgets::{Block, Borders, Paragraph};
use ratatui::Terminal;
use std::sync::mpsc::{self, Receiver};
use std::thread;

use event_handler::command;
use event_handler::key_handler;
use ipc::client::IpcClient;
use ipc::protocol::IpcResponse;
use state::app_state::{ScrollbarInfo, UiMode};
use state::app_state::{STATUS_COMPLETED, STEP_DISPLAY};
use state::log_buffer::LogEntry;
use state::{AppState, LogBuffer};
use ui::layout::AppLayout;

pub use utils::format_local_time;

const MIN_TERMINAL_WIDTH: u16 = 80;
const MIN_TERMINAL_HEIGHT: u16 = 16;
const WORKER_HEALTH_WATCHDOG_INTERVAL: Duration = Duration::from_secs(10);

fn format_log_record(
    buf: &mut env_logger::fmt::Formatter,
    record: &log::Record<'_>,
) -> std::io::Result<()> {
    use std::io::Write;

    writeln!(
        buf,
        "[{}] [{}] [{}] {}",
        unix_epoch_millis(),
        record.level(),
        record.target(),
        record.args()
    )
}

fn unix_epoch_millis() -> u128 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|duration| duration.as_millis())
        .unwrap_or(0)
}

/// 初始化文件日志。
/// 优先使用 AUTOFLUID_SESSION_LOG_DIR 环境变量（由 Python 启动器设置）；
/// 若未设置，尝试查找结构化 client 会话目录，再兼容旧的 logs/client/；
/// 若均不可用，回退到临时文件，避免 stderr 污染 TUI alternate screen。
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
            match OpenOptions::new().create(true).append(true).open(&log_path) {
                Ok(file) => {
                    env_logger::Builder::new()
                        .filter_level(LevelFilter::Info)
                        .target(env_logger::Target::Pipe(Box::new(file)))
                        .format(format_log_record)
                        .try_init()
                        .ok();
                    log::info!(
                        "AutoFluid TUI v{} 启动，日志文件: {:?}",
                        env!("CARGO_PKG_VERSION"),
                        log_path
                    );
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
    let fallback_path = std::env::temp_dir().join("autofluid-tui.log");
    match std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(&fallback_path)
    {
        Ok(file) => {
            env_logger::Builder::new()
                .filter_level(log::LevelFilter::Info)
                .target(env_logger::Target::Pipe(Box::new(file)))
                .format(format_log_record)
                .try_init()
                .ok();
            log::info!(
                "AutoFluid TUI v{} 启动，fallback 日志文件: {:?}",
                env!("CARGO_PKG_VERSION"),
                fallback_path
            );
        }
        Err(_) => {
            env_logger::Builder::new()
                .filter_level(log::LevelFilter::Off)
                .try_init()
                .ok();
        }
    }
}

/// 查找最新的 client 会话目录，优先使用结构化日志布局。
fn find_latest_client_session_dir() -> Option<std::path::PathBuf> {
    let cwd = std::env::current_dir().ok()?;
    latest_session_dir(
        &cwd.join("logs")
            .join("local")
            .join("sessions")
            .join("client"),
    )
    .or_else(|| latest_session_dir(&cwd.join("logs").join("client")))
}

fn latest_session_dir(client_dir: &std::path::Path) -> Option<std::path::PathBuf> {
    if !client_dir.is_dir() {
        return None;
    }
    let mut entries: Vec<_> = std::fs::read_dir(client_dir)
        .ok()?
        .filter_map(|e| e.ok())
        .filter(|e| e.path().is_dir())
        .collect();
    entries.sort_by_key(|b| std::cmp::Reverse(b.file_name()));
    entries.first().map(|e| e.path())
}

#[cfg(test)]
pub(crate) static TEST_ENV_LOCK: std::sync::Mutex<()> = std::sync::Mutex::new(());

use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::OnceLock;

/// 全局请求 ID 计数器。
/// 注意：此代码仅在 current_thread tokio runtime 下安全。
/// 若切换到 multi_thread runtime，需改用 Ordering::SeqCst。
static REQUEST_COUNTER: AtomicU64 = AtomicU64::new(0);
static REQUEST_PREFIX: OnceLock<u64> = OnceLock::new();

/// 生成唯一的请求 ID。
///
/// 使用时间戳前缀 + 原子计数器确保唯一性。
/// 仅在 current_thread tokio runtime 下安全使用。
pub fn generate_request_id() -> String {
    use std::time::SystemTime;
    let prefix = *REQUEST_PREFIX.get_or_init(|| {
        SystemTime::now()
            .duration_since(SystemTime::UNIX_EPOCH)
            .unwrap_or_default()
            .as_nanos() as u64
    });
    let count = REQUEST_COUNTER.fetch_add(1, Ordering::Relaxed);
    format!("{:016x}", prefix.wrapping_add(count))
}

pub(crate) fn apply_auto_scroll(
    auto_scroll: &mut bool,
    scroll: &mut u16,
    visual_count: usize,
    content_height: usize,
) {
    if visual_count <= content_height {
        *scroll = 0;
        *auto_scroll = true;
        return;
    }

    let max_scroll = (visual_count - content_height) as u16;
    if *auto_scroll {
        *scroll = max_scroll;
    } else if *scroll >= max_scroll {
        *auto_scroll = true;
    }
}

pub(crate) fn should_poll_ipc(last_poll: Instant, interval: Duration) -> bool {
    last_poll.elapsed() >= interval
}

pub(crate) fn initial_ipc_poll_timestamp() -> Instant {
    Instant::now()
}

pub(crate) fn apply_log_entries_response(
    data_obj: &serde_json::Map<String, serde_json::Value>,
    state: &mut AppState,
    log_buffer: &mut LogBuffer,
) {
    if data_obj
        .get("reset")
        .and_then(|v| v.as_bool())
        .unwrap_or(false)
    {
        state.last_log_id = 0;
        log_buffer.clear_detail();
    }
    if data_obj
        .get("has_gap")
        .and_then(|v| v.as_bool())
        .unwrap_or(false)
    {
        log_buffer.push_info("⚠ 详细日志存在缺口，部分较早 daemon 日志已被丢弃".to_string());
    }
    if let Some(entries) = data_obj.get("entries").and_then(|v| v.as_array()) {
        for entry_val in entries {
            if let Some(entry) = LogEntry::from_dict(entry_val) {
                state.last_log_id = state.last_log_id.max(entry.id);
                log_buffer.push_detail(entry);
            }
        }
    }
    if let Some(latest) = data_obj.get("latest_id").and_then(|v| v.as_u64()) {
        state.last_log_id = state.last_log_id.max(latest);
    }
}

pub(crate) fn apply_dashboard_response(
    data_obj: &serde_json::Map<String, serde_json::Value>,
    state: &mut AppState,
    log_buffer: &mut LogBuffer,
) {
    if let Some(statuses) = data_obj.get("statuses") {
        let previous_statuses = state.status_data.clone();
        state.update_status_data(statuses);
        announce_completed_status_transitions(statuses, &previous_statuses, log_buffer);
    }
    if let Some(config_workstations) = data_obj.get("config_workstations") {
        state.update_config_workstations(config_workstations);
    }
    if let Some(engine) = data_obj.get("engine") {
        state.update_engine_info(engine);
    }
    if let Some(health) = data_obj.get("health") {
        state.update_health_info(health);
    }
    if let Some(logs) = data_obj.get("logs").and_then(|v| v.as_object()) {
        apply_log_entries_response(logs, state, log_buffer);
    }
}

fn announce_completed_status_transitions(
    statuses: &serde_json::Value,
    previous_statuses: &std::collections::HashMap<
        String,
        std::collections::HashMap<String, String>,
    >,
    log_buffer: &mut LogBuffer,
) {
    let Some(configs) = statuses.as_object() else {
        return;
    };

    for (config_name, steps_val) in configs {
        let Some(previous_steps) = previous_statuses.get(config_name) else {
            continue;
        };
        let Some(steps) = steps_val.as_object() else {
            continue;
        };

        for (step_name, status_val) in steps {
            if status_val.as_str() != Some(STATUS_COMPLETED) {
                continue;
            }
            if previous_steps.get(step_name).map(String::as_str) == Some(STATUS_COMPLETED) {
                continue;
            }
            if !previous_steps.contains_key(step_name) {
                continue;
            }

            let display_name = STEP_DISPLAY
                .iter()
                .find(|(name, _)| *name == step_name)
                .map(|(_, display)| *display)
                .unwrap_or(step_name.as_str());
            log_buffer.push_info(format!("✅ 构型{config_name} {display_name} 已完成"));
        }
    }
}

fn refresh_dashboard_once(
    rt: &tokio::runtime::Runtime,
    ipc: &mut IpcClient,
    state: &mut AppState,
    log_buffer: &mut LogBuffer,
) -> bool {
    if !ipc.is_connected() {
        return false;
    }
    match rt.block_on(ipc.get_dashboard(state.last_log_id, 50)) {
        Ok(resp) if resp.is_ok() => {
            if let Some(data_obj) = resp.data.as_object() {
                apply_dashboard_response(data_obj, state, log_buffer);
                true
            } else {
                false
            }
        }
        _ => false,
    }
}

fn handle_startup_connect_failure(
    daemon: &mut daemon_mgr::DaemonManager,
    state: &mut AppState,
    log_buffer: &mut LogBuffer,
    project_dir: &str,
    ipc_host: &str,
) {
    state.update_local_ipc_tunnel(ipc_host, false);
    daemon.begin_server_ipc_tunnel_for_reconnect(project_dir, log_buffer);
    daemon.begin_ipc_reconnect_wait(state);
    log_buffer.push_info(
        "❌ 暂未连接到后台引擎，正在后台自动重连；请检查远端 daemon 和 IPC 隧道".to_string(),
    );
    log_buffer.push_info("提示: 界面将在无后台连接的情况下运行，连接恢复后会自动刷新".to_string());
}

fn handle_startup_connect_success(
    state: &mut AppState,
    log_buffer: &mut LogBuffer,
    ipc_host: &str,
) {
    state.connected = true;
    state.update_local_ipc_tunnel(ipc_host, true);
    log_buffer.push_info("✅ 已连接到后台引擎".to_string());
}

fn handle_dashboard_poll_connection_state(
    daemon: &mut daemon_mgr::DaemonManager,
    state: &mut AppState,
    log_buffer: &mut LogBuffer,
    project_dir: &str,
    ipc_host: &str,
    was_connected: bool,
    is_connected: bool,
) {
    state.connected = is_connected;
    state.update_local_ipc_tunnel(ipc_host, is_connected);
    if was_connected && !is_connected {
        daemon.begin_server_ipc_tunnel_for_reconnect(project_dir, log_buffer);
        daemon.begin_ipc_reconnect_wait(state);
        log_buffer.push_info("⚠️ 连接中断，正在后台自动重连...".to_string());
    }
}

/// 非阻塞地尝试刷新一次 Worker 健康信息。
///
/// 启动 Worker 后立即调用，将最新状态拉取到 UI。
/// 若健康信息尚未就绪，正常的 1 秒 IPC 轮询会自动捕获后续更新，
/// 无需阻塞事件循环等待。
fn try_refresh_worker_health_once(
    rt: &tokio::runtime::Runtime,
    ipc: &mut IpcClient,
    state: &mut AppState,
    log_buffer: &mut LogBuffer,
) {
    refresh_dashboard_once(rt, ipc, state, log_buffer);
}

fn poll_worker_health_watchdog(
    worker: &mut worker_mgr::WorkerManager,
    state: &AppState,
    log_buffer: &mut LogBuffer,
) {
    let managed_process_down = worker.has_exited_managed_process();
    let worker_offline = state.health_info.local_worker_online == Some(false);
    let local_tunnel_down = matches!(
        state.health_info.server_to_local_ssh.as_deref(),
        Some("disconnected") | Some("error")
    );
    let workstation_tunnel_down = matches!(
        state.health_info.server_to_workstation_ssh.as_deref(),
        Some("disconnected") | Some("error")
    );
    if !(managed_process_down || worker_offline || local_tunnel_down || workstation_tunnel_down) {
        return;
    }
    let warning = "⚠️ Worker 或 SSH 隧道异常，请确认工作站状态；需要恢复时执行 worker restart";
    if !log_buffer
        .info_messages
        .iter()
        .any(|message| message == warning)
    {
        log_buffer.push_info(warning.to_string());
    }
}

#[cfg(test)]
fn worker_health_is_visible(state: &AppState) -> bool {
    state.health_info.local_worker_online == Some(true)
        && matches!(
            state.health_info.server_to_local_ssh.as_deref(),
            Some("ok" | "disconnected")
        )
}

// ====================================================================
// IPC 后台轮询线程
pub fn run_tui() -> Result<(), Box<dyn std::error::Error>> {
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

/// 事件处理上下文，聚合 process_event 所需的全部可变引用。
pub(crate) struct EventContext<'a> {
    state: &'a mut AppState,
    log_buffer: &'a mut LogBuffer,
    ipc: &'a mut IpcClient,
    check_task: Option<&'a mut Option<CheckTask>>,
    daemon_task: Option<&'a mut Option<DaemonLifecycleTask>>,
    worker_task: Option<&'a mut Option<WorkerLifecycleTask>>,
    daemon: &'a mut daemon_mgr::DaemonManager,
    worker: &'a mut worker_mgr::WorkerManager,
    rt: &'a tokio::runtime::Runtime,
    project_dir: &'a str,
    full_quit: &'a mut bool,
    full_quit_stop_sent: &'a mut bool,
}

struct CheckTask {
    receiver: Receiver<Result<IpcResponse, String>>,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) enum DaemonLifecycleAction {
    Stop,
    Restart,
}

impl DaemonLifecycleAction {
    fn running_message(self) -> &'static str {
        match self {
            Self::Stop => "🛑 Daemon 停止已转入后台，界面可继续操作...",
            Self::Restart => "🔄 Daemon 重启已转入后台，界面可继续操作...",
        }
    }

    fn duplicate_message(self) -> &'static str {
        match self {
            Self::Stop => "ℹ Daemon 生命周期任务正在运行，请等待当前停止完成",
            Self::Restart => "ℹ Daemon 生命周期任务正在运行，请等待当前重启完成",
        }
    }

    fn failure_message(self) -> &'static str {
        match self {
            Self::Stop => "❌ Daemon 后台停止失败，详情见上方日志",
            Self::Restart => "❌ Daemon 后台重启失败，详情见上方日志",
        }
    }
}

struct DaemonLifecycleResult {
    action: DaemonLifecycleAction,
    success: bool,
    logs: Vec<String>,
}

pub(crate) struct DaemonLifecycleTask {
    receiver: Receiver<DaemonLifecycleResult>,
    handle: Option<thread::JoinHandle<()>>,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) enum WorkerLifecycleAction {
    Start,
    Stop,
    Restart,
}

impl WorkerLifecycleAction {
    fn running_message(self) -> &'static str {
        match self {
            Self::Start => "🔧 Worker 启动已转入后台，界面可继续操作...",
            Self::Stop => "🛑 Worker 停止已转入后台，界面可继续操作...",
            Self::Restart => "🔄 Worker 重启已转入后台，界面可继续操作...",
        }
    }

    fn duplicate_message(self) -> &'static str {
        match self {
            Self::Start => "ℹ Worker 生命周期任务正在运行，请等待当前启动完成",
            Self::Stop => "ℹ Worker 生命周期任务正在运行，请等待当前停止完成",
            Self::Restart => "ℹ Worker 生命周期任务正在运行，请等待当前重启完成",
        }
    }

    fn failure_message(self) -> &'static str {
        match self {
            Self::Start => "❌ Worker 后台启动失败，详情见上方日志",
            Self::Stop => "❌ Worker 后台停止失败，详情见上方日志",
            Self::Restart => "❌ Worker 后台重启失败，详情见上方日志",
        }
    }

    fn refresh_after_completion(self) -> bool {
        matches!(self, Self::Start | Self::Stop | Self::Restart)
    }
}

struct WorkerLifecycleResult {
    action: WorkerLifecycleAction,
    success: bool,
    logs: Vec<String>,
}

pub(crate) struct WorkerLifecycleTask {
    receiver: Receiver<WorkerLifecycleResult>,
    handle: Option<thread::JoinHandle<()>>,
}

pub(crate) fn apply_check_response(
    result: Result<IpcResponse, String>,
    state: &mut AppState,
    log_buffer: &mut LogBuffer,
) {
    match result {
        Ok(resp) if resp.is_ok() => {
            log_buffer.push_info("✅ 系统自检完成".to_string());
            state.check_data = Some(resp.data);
            state.dialog_scroll = 0;
            state.ui_mode = UiMode::CheckResult;
        }
        Ok(resp) => {
            log_buffer.push_info(format!("❌ 系统自检失败: {}", resp.message));
        }
        Err(e) => {
            log_buffer.push_info(format!("❌ 系统自检通信失败: {}", e));
        }
    }
    state.needs_redraw = true;
}

fn spawn_check_task(host: &str, port: u16) -> CheckTask {
    let (sender, receiver) = mpsc::channel();
    let host = host.to_string();
    thread::spawn(move || {
        let result = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .map_err(|e| e.to_string())
            .and_then(|rt| {
                rt.block_on(async {
                    let mut ipc = IpcClient::new(Some(&host), Some(port));
                    ipc.connect().await?;
                    let result = ipc.check_system().await;
                    ipc.disconnect().await;
                    result
                })
            });
        let _ = sender.send(result);
    });
    CheckTask { receiver }
}

fn poll_check_task(
    check_task: &mut Option<CheckTask>,
    state: &mut AppState,
    log_buffer: &mut LogBuffer,
) {
    let Some(task) = check_task.as_ref() else {
        return;
    };
    match task.receiver.try_recv() {
        Ok(result) => {
            *check_task = None;
            apply_check_response(result, state, log_buffer);
        }
        Err(mpsc::TryRecvError::Empty) => {}
        Err(mpsc::TryRecvError::Disconnected) => {
            *check_task = None;
            log_buffer.push_info("❌ 系统自检后台任务异常结束".to_string());
            state.needs_redraw = true;
        }
    }
}

pub(crate) fn start_daemon_lifecycle_task(
    daemon_task: Option<&mut Option<DaemonLifecycleTask>>,
    action: DaemonLifecycleAction,
    project_dir: &str,
    ipc_host: &str,
    ipc_port: u16,
    log_buffer: &mut LogBuffer,
    state: &mut AppState,
) {
    let Some(daemon_task) = daemon_task else {
        log_buffer.push_info("❌ Daemon 后台任务通道不可用".to_string());
        state.needs_redraw = true;
        return;
    };
    if daemon_task.is_some() {
        log_buffer.push_info(action.duplicate_message().to_string());
        state.needs_redraw = true;
        return;
    }

    log_buffer.push_info(action.running_message().to_string());
    *daemon_task = Some(spawn_daemon_lifecycle_task(
        action,
        project_dir.to_string(),
        ipc_host.to_string(),
        ipc_port,
    ));
    state.needs_redraw = true;
}

fn spawn_daemon_lifecycle_task(
    action: DaemonLifecycleAction,
    project_dir: String,
    ipc_host: String,
    ipc_port: u16,
) -> DaemonLifecycleTask {
    let (sender, receiver) = mpsc::channel();
    let handle = thread::spawn(move || {
        let mut log_buffer = LogBuffer::new();
        let success =
            run_daemon_lifecycle_action(action, &project_dir, &ipc_host, ipc_port, &mut log_buffer);
        let logs = log_buffer.info_messages.iter().cloned().collect();
        let _ = sender.send(DaemonLifecycleResult {
            action,
            success,
            logs,
        });
    });
    DaemonLifecycleTask {
        receiver,
        handle: Some(handle),
    }
}

fn run_daemon_lifecycle_action(
    action: DaemonLifecycleAction,
    project_dir: &str,
    ipc_host: &str,
    ipc_port: u16,
    log_buffer: &mut LogBuffer,
) -> bool {
    let Ok(rt) = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
    else {
        log_buffer.push_info("❌ Daemon 后台任务创建运行时失败".to_string());
        return false;
    };
    let mut ipc = IpcClient::new(Some(ipc_host), Some(ipc_port));
    let _ = rt.block_on(ipc.connect());
    let mut state = AppState::new();
    let mut daemon = daemon_mgr::DaemonManager::new();
    match action {
        DaemonLifecycleAction::Stop => {
            daemon.stop_with_ipc(&mut ipc, &rt, &mut state, log_buffer, project_dir)
        }
        DaemonLifecycleAction::Restart => {
            daemon.restart_with_ipc(&mut ipc, &rt, &mut state, log_buffer, project_dir)
        }
    }
}

fn finish_daemon_lifecycle_result(
    result: DaemonLifecycleResult,
    daemon: &mut daemon_mgr::DaemonManager,
    state: &mut AppState,
    log_buffer: &mut LogBuffer,
) {
    for message in result.logs {
        log_buffer.push_info(message);
    }
    if !result.success {
        log_buffer.push_info(result.action.failure_message().to_string());
    }
    match result.action {
        DaemonLifecycleAction::Stop if result.success => state.mark_daemon_stopped(),
        DaemonLifecycleAction::Restart if result.success => daemon.begin_ipc_reconnect_wait(state),
        _ => {}
    }
    state.needs_redraw = true;
}

fn poll_daemon_lifecycle_task(
    daemon_task: &mut Option<DaemonLifecycleTask>,
    daemon: &mut daemon_mgr::DaemonManager,
    state: &mut AppState,
    log_buffer: &mut LogBuffer,
) {
    let Some(task) = daemon_task.as_ref() else {
        return;
    };
    match task.receiver.try_recv() {
        Ok(result) => {
            if let Some(mut task) = daemon_task.take() {
                if let Some(handle) = task.handle.take() {
                    let _ = handle.join();
                }
            }
            finish_daemon_lifecycle_result(result, daemon, state, log_buffer);
        }
        Err(mpsc::TryRecvError::Empty) => {}
        Err(mpsc::TryRecvError::Disconnected) => {
            if let Some(mut task) = daemon_task.take() {
                if let Some(handle) = task.handle.take() {
                    let _ = handle.join();
                }
            }
            log_buffer.push_info("❌ Daemon 后台任务异常结束".to_string());
            state.needs_redraw = true;
        }
    }
}

pub(crate) fn start_worker_lifecycle_task(
    worker_task: Option<&mut Option<WorkerLifecycleTask>>,
    action: WorkerLifecycleAction,
    project_dir: &str,
    ipc_host: &str,
    ipc_port: u16,
    log_buffer: &mut LogBuffer,
    state: &mut AppState,
) {
    let Some(worker_task) = worker_task else {
        log_buffer.push_info("❌ Worker 后台任务通道不可用".to_string());
        state.needs_redraw = true;
        return;
    };
    if worker_task.is_some() {
        log_buffer.push_info(action.duplicate_message().to_string());
        state.needs_redraw = true;
        return;
    }

    log_buffer.push_info(action.running_message().to_string());
    *worker_task = Some(spawn_worker_lifecycle_task(
        action,
        project_dir.to_string(),
        ipc_host.to_string(),
        ipc_port,
    ));
    state.needs_redraw = true;
}

fn spawn_worker_lifecycle_task(
    action: WorkerLifecycleAction,
    project_dir: String,
    ipc_host: String,
    ipc_port: u16,
) -> WorkerLifecycleTask {
    let (sender, receiver) = mpsc::channel();
    let handle = thread::spawn(move || {
        let mut worker = worker_mgr::WorkerManager::new();
        let mut log_buffer = LogBuffer::new();
        let success = run_worker_lifecycle_action(
            action,
            &project_dir,
            &ipc_host,
            ipc_port,
            &mut worker,
            &mut log_buffer,
        );
        let logs = log_buffer.info_messages.iter().cloned().collect();
        let _ = sender.send(WorkerLifecycleResult {
            action,
            success,
            logs,
        });
    });
    WorkerLifecycleTask {
        receiver,
        handle: Some(handle),
    }
}

fn run_worker_lifecycle_action(
    action: WorkerLifecycleAction,
    project_dir: &str,
    ipc_host: &str,
    ipc_port: u16,
    worker: &mut worker_mgr::WorkerManager,
    log_buffer: &mut LogBuffer,
) -> bool {
    match action {
        WorkerLifecycleAction::Start => {
            start_workers_in_background(project_dir, ipc_host, ipc_port, worker, log_buffer)
        }
        WorkerLifecycleAction::Stop => {
            stop_remote_workers_in_background(ipc_host, ipc_port, log_buffer);
            worker.stop_workers_for_project(Some(project_dir), log_buffer)
        }
        WorkerLifecycleAction::Restart => {
            stop_remote_workers_in_background(ipc_host, ipc_port, log_buffer);
            let stopped = worker.stop_workers_for_project(Some(project_dir), log_buffer);
            let started =
                start_workers_in_background(project_dir, ipc_host, ipc_port, worker, log_buffer);
            stopped && started
        }
    }
}

fn start_workers_in_background(
    project_dir: &str,
    ipc_host: &str,
    ipc_port: u16,
    worker: &mut worker_mgr::WorkerManager,
    log_buffer: &mut LogBuffer,
) -> bool {
    worker.start_workers_with_prepare(project_dir, log_buffer, |buffer| {
        prepare_remote_workers_in_background(ipc_host, ipc_port, buffer)
    })
}

fn prepare_remote_workers_in_background(
    ipc_host: &str,
    ipc_port: u16,
    log_buffer: &mut LogBuffer,
) -> bool {
    match tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
    {
        Ok(rt) => {
            let mut ipc = IpcClient::new(Some(ipc_host), Some(ipc_port));
            match rt.block_on(ipc.connect()) {
                Ok(()) => {
                    let ok = worker_mgr::prepare_remote_workers(&mut ipc, &rt, log_buffer);
                    rt.block_on(ipc.disconnect());
                    ok
                }
                Err(e) => {
                    log_buffer.push_info(format!("❌ Worker 后台任务连接后台引擎失败: {}", e));
                    false
                }
            }
        }
        Err(e) => {
            log_buffer.push_info(format!("❌ Worker 后台任务创建运行时失败: {}", e));
            false
        }
    }
}

fn stop_remote_workers_in_background(ipc_host: &str, ipc_port: u16, log_buffer: &mut LogBuffer) {
    let Ok(rt) = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
    else {
        log_buffer
            .push_info("⚠️ Worker 后台任务创建运行时失败，跳过 daemon worker_stop".to_string());
        return;
    };
    let mut ipc = IpcClient::new(Some(ipc_host), Some(ipc_port));
    rt.block_on(async {
        match ipc.connect().await {
            Ok(()) => {
                match ipc.worker_stop().await {
                    Ok(resp) if resp.is_ok() => {
                        log_buffer.push_info(format!("✅ {}", resp.message))
                    }
                    Ok(resp) => log_buffer.push_info(format!("❌ {}", resp.message)),
                    Err(e) => log_buffer.push_info(format!("❌ 通信失败: {}", e)),
                }
                ipc.disconnect().await;
            }
            Err(e) => {
                log_buffer.push_info(format!(
                    "⚠️ Worker 后台任务连接后台引擎失败，跳过 daemon worker_stop: {}",
                    e
                ));
            }
        }
    });
}

fn finish_worker_lifecycle_result(
    result: WorkerLifecycleResult,
    rt: &tokio::runtime::Runtime,
    ipc: &mut IpcClient,
    state: &mut AppState,
    log_buffer: &mut LogBuffer,
) {
    for message in result.logs {
        log_buffer.push_info(message);
    }
    if !result.success {
        log_buffer.push_info(result.action.failure_message().to_string());
    }
    if result.action.refresh_after_completion() {
        try_refresh_worker_health_once(rt, ipc, state, log_buffer);
    }
    state.needs_redraw = true;
}

fn poll_worker_lifecycle_task(
    worker_task: &mut Option<WorkerLifecycleTask>,
    rt: &tokio::runtime::Runtime,
    ipc: &mut IpcClient,
    state: &mut AppState,
    log_buffer: &mut LogBuffer,
) {
    let Some(task) = worker_task.as_ref() else {
        return;
    };
    match task.receiver.try_recv() {
        Ok(result) => {
            if let Some(mut task) = worker_task.take() {
                if let Some(handle) = task.handle.take() {
                    let _ = handle.join();
                }
            }
            finish_worker_lifecycle_result(result, rt, ipc, state, log_buffer);
        }
        Err(mpsc::TryRecvError::Empty) => {}
        Err(mpsc::TryRecvError::Disconnected) => {
            if let Some(mut task) = worker_task.take() {
                if let Some(handle) = task.handle.take() {
                    let _ = handle.join();
                }
            }
            log_buffer.push_info("❌ Worker 后台任务异常结束".to_string());
            state.needs_redraw = true;
        }
    }
}

fn finish_pending_worker_lifecycle_task(
    worker_task: &mut Option<WorkerLifecycleTask>,
    rt: &tokio::runtime::Runtime,
    ipc: &mut IpcClient,
    state: &mut AppState,
    log_buffer: &mut LogBuffer,
) {
    let Some(mut task) = worker_task.take() else {
        return;
    };
    log_buffer.push_info("ℹ 等待正在运行的 Worker 生命周期任务收尾...".to_string());
    if let Some(handle) = task.handle.take() {
        let _ = handle.join();
    }
    match task.receiver.try_recv() {
        Ok(result) => finish_worker_lifecycle_result(result, rt, ipc, state, log_buffer),
        Err(_) => {
            log_buffer.push_info("❌ Worker 后台任务未返回结果".to_string());
            state.needs_redraw = true;
        }
    }
}

fn run_app(terminal: &mut Terminal<CrosstermBackend<io::Stdout>>) -> Result<(), String> {
    let rt = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .map_err(|e| e.to_string())?;
    let project_dir = utils::resolve_project_dir().to_string_lossy().to_string();
    if let Err(e) = utils::import_project_env(&project_dir) {
        log::warn!("导入项目环境失败: {}", e);
    }

    let mut ipc = IpcClient::new(None, None);
    let mut state = AppState::new();
    let mut log_buffer = LogBuffer::new();
    let mut daemon = daemon_mgr::DaemonManager::new();
    let mut worker = worker_mgr::WorkerManager::new();
    let mut check_task: Option<CheckTask> = None;
    let mut daemon_task: Option<DaemonLifecycleTask> = None;
    let mut worker_task: Option<WorkerLifecycleTask> = None;

    log_buffer.push_info("欢迎使用液氧甲烷火箭发动机仿真总控程序！".to_string());
    log_buffer.push_info("正在连接后台引擎...".to_string());
    log_buffer
        .push_info("Tab 切换焦点 | ↑↓ 滚动 | PageUp/PageDown 翻页 | Home/End 跳转".to_string());

    if let Ok(size) = terminal.size() {
        state.update_terminal_size(size.width, size.height);
    }

    // 主线程直接连接 IPC（单连接架构，轮询在主循环中进行）
    match rt.block_on(ipc.connect()) {
        Ok(()) => {
            handle_startup_connect_success(&mut state, &mut log_buffer, ipc.host());
        }
        Err(_) => {
            handle_startup_connect_failure(
                &mut daemon,
                &mut state,
                &mut log_buffer,
                &project_dir,
                ipc.host(),
            );
        }
    }

    let mut full_quit = false;
    let mut full_quit_stop_sent = false;
    let ipc_poll_interval = Duration::from_secs(1);
    let clock_interval = Duration::from_millis(500);
    let mut last_ipc_poll = initial_ipc_poll_timestamp();
    let mut last_clock_refresh = Instant::now();
    let mut last_worker_health_watchdog = Instant::now();

    let mut ctx = EventContext {
        state: &mut state,
        log_buffer: &mut log_buffer,
        ipc: &mut ipc,
        check_task: Some(&mut check_task),
        daemon_task: Some(&mut daemon_task),
        worker_task: Some(&mut worker_task),
        daemon: &mut daemon,
        worker: &mut worker,
        rt: &rt,
        project_dir: &project_dir,
        full_quit: &mut full_quit,
        full_quit_stop_sent: &mut full_quit_stop_sent,
    };

    loop {
        if ctx.state.should_quit {
            break;
        }

        ctx.state.tick();
        if let Some(check_task) = ctx.check_task.as_deref_mut() {
            poll_check_task(check_task, ctx.state, ctx.log_buffer);
        }
        if let Some(daemon_task) = ctx.daemon_task.as_deref_mut() {
            poll_daemon_lifecycle_task(daemon_task, ctx.daemon, ctx.state, ctx.log_buffer);
        }
        if let Some(worker_task) = ctx.worker_task.as_deref_mut() {
            poll_worker_lifecycle_task(worker_task, ctx.rt, ctx.ipc, ctx.state, ctx.log_buffer);
        }

        if let Some(cmd) = ctx.state.pending_command.take() {
            let source = ctx.state.pending_command_source.take().unwrap_or("program");
            let result = submit_command(&cmd, source, ctx.rt, ctx.ipc, ctx.state, ctx.log_buffer);
            handle_command_result(result, &mut ctx);
        }

        let first_poll_timeout = Duration::from_millis(50);
        if crossterm_event::poll(first_poll_timeout).map_err(|e| e.to_string())? {
            let event = crossterm_event::read().map_err(|e| e.to_string())?;
            process_event(event, &mut ctx);

            while crossterm_event::poll(Duration::from_millis(0)).map_err(|e| e.to_string())? {
                let event = crossterm_event::read().map_err(|e| e.to_string())?;
                process_event(event, &mut ctx);
            }
        }

        if ctx.state.needs_redraw {
            let _ = do_redraw(terminal, ctx.state, ctx.log_buffer);
            ctx.state.needs_redraw = false;
        }

        ctx.daemon
            .poll_server_ipc_tunnel_reconnect(ctx.state, ctx.log_buffer);

        ctx.daemon
            .poll_ipc_reconnect(ctx.rt, ctx.ipc, ctx.state, ctx.log_buffer);

        if last_worker_health_watchdog.elapsed() >= WORKER_HEALTH_WATCHDOG_INTERVAL {
            poll_worker_health_watchdog(ctx.worker, ctx.state, ctx.log_buffer);
            last_worker_health_watchdog = Instant::now();
        }

        // ★ 主线程直接轮询 IPC 状态（1s 间隔，非阻塞）
        if should_poll_ipc(last_ipc_poll, ipc_poll_interval) {
            // 批量拉取表格状态、引擎状态和日志增量，避免多条轮询命令刷屏。
            let was_connected = ctx.ipc.is_connected();
            refresh_dashboard_once(ctx.rt, ctx.ipc, ctx.state, ctx.log_buffer);
            let is_connected = ctx.ipc.is_connected();
            handle_dashboard_poll_connection_state(
                ctx.daemon,
                ctx.state,
                ctx.log_buffer,
                ctx.project_dir,
                ctx.ipc.host(),
                was_connected,
                is_connected,
            );
            last_ipc_poll = Instant::now();
            ctx.state.needs_redraw = true;
        }

        if last_clock_refresh.elapsed() >= clock_interval {
            ctx.state.needs_redraw = true;
            last_clock_refresh = std::time::Instant::now();
        }
    }

    ctx.rt.block_on(ctx.ipc.disconnect());

    if *ctx.full_quit {
        if let Some(worker_task) = ctx.worker_task.as_deref_mut() {
            finish_pending_worker_lifecycle_task(
                worker_task,
                ctx.rt,
                ctx.ipc,
                ctx.state,
                ctx.log_buffer,
            );
        }
        ctx.worker
            .stop_workers_for_project(Some(ctx.project_dir), ctx.log_buffer);
        let result = if *ctx.full_quit_stop_sent {
            ctx.daemon.finish_after_successful_ipc_stop(ctx.project_dir)
        } else {
            ctx.daemon.stop(ctx.project_dir)
        };
        if let Err(e) = result {
            log::warn!("完全退出清理后台引擎失败: {}", e);
        }
    } else if let Err(e) = daemon_mgr::DaemonManager::stop_server_ipc_tunnel(ctx.project_dir) {
        log::warn!("普通退出清理服务器 IPC 隧道失败: {}", e);
    }

    Ok(())
}

fn submit_command(
    cmd: &str,
    source: &str,
    rt: &tokio::runtime::Runtime,
    ipc: &mut IpcClient,
    state: &mut AppState,
    log_buffer: &mut LogBuffer,
) -> command::CommandResult {
    log::info!("用户命令: source={source}, command={cmd:?}");
    log_buffer.push_info(format!("> {}", cmd));
    state.needs_redraw = true;
    rt.block_on(command::dispatch_command(cmd, ipc, state, log_buffer))
}

fn handle_command_result(result: command::CommandResult, ctx: &mut EventContext) {
    match result {
        command::CommandResult::Quit => {
            ctx.state.should_quit = true;
        }
        command::CommandResult::FullQuit => {
            log::info!("收到完全退出请求，准备停止后台引擎并关闭界面");
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
        command::CommandResult::StartCheck => {
            if let Some(check_task) = ctx.check_task.as_deref_mut() {
                if check_task.is_some() {
                    ctx.log_buffer
                        .push_info("ℹ 系统自检正在运行，请等待当前检查完成".to_string());
                } else {
                    ctx.log_buffer
                        .push_info("🔍 正在系统自检（含远程 SSH 检测，请耐心等待）...".to_string());
                    ctx.state.check_data = None;
                    ctx.state.dialog_scroll = 0;
                    if matches!(ctx.state.ui_mode, UiMode::CheckResult) {
                        ctx.state.ui_mode = UiMode::Normal;
                    }
                    *check_task = Some(spawn_check_task(ctx.ipc.host(), ctx.ipc.port()));
                }
            }
            ctx.state.needs_redraw = true;
        }
        command::CommandResult::StartDaemon => {
            ctx.daemon
                .start_with_ipc(ctx.ipc, ctx.rt, ctx.state, ctx.log_buffer, ctx.project_dir);
        }
        command::CommandResult::RestartDaemon => {
            let host = ctx.ipc.host().to_string();
            let port = ctx.ipc.port();
            start_daemon_lifecycle_task(
                ctx.daemon_task.as_deref_mut(),
                DaemonLifecycleAction::Restart,
                ctx.project_dir,
                &host,
                port,
                ctx.log_buffer,
                ctx.state,
            );
        }
        command::CommandResult::StopDaemon => {
            let host = ctx.ipc.host().to_string();
            let port = ctx.ipc.port();
            start_daemon_lifecycle_task(
                ctx.daemon_task.as_deref_mut(),
                DaemonLifecycleAction::Stop,
                ctx.project_dir,
                &host,
                port,
                ctx.log_buffer,
                ctx.state,
            );
        }
        command::CommandResult::StartWorkers => {
            let host = ctx.ipc.host().to_string();
            let port = ctx.ipc.port();
            start_worker_lifecycle_task(
                ctx.worker_task.as_deref_mut(),
                WorkerLifecycleAction::Start,
                ctx.project_dir,
                &host,
                port,
                ctx.log_buffer,
                ctx.state,
            );
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
        command::CommandResult::None => {}
    }
}

fn process_event(event: CrosstermEvent, ctx: &mut EventContext) {
    match event {
        CrosstermEvent::Key(key) if key.kind == crossterm::event::KeyEventKind::Press => {
            let action = key_handler::handle_key(key, ctx.state);
            match action {
                key_handler::AppAction::Quit => {
                    ctx.state.should_quit = true;
                }
                key_handler::AppAction::SubmitCommand(cmd) => {
                    let result = submit_command(
                        &cmd,
                        "keyboard",
                        ctx.rt,
                        ctx.ipc,
                        ctx.state,
                        ctx.log_buffer,
                    );
                    handle_command_result(result, ctx);
                }
                key_handler::AppAction::Confirm => {
                    if let Some(callback) = ctx.state.confirm_callback.take() {
                        let result = ctx.rt.block_on(command::execute_confirm_action(
                            &callback,
                            ctx.ipc,
                            ctx.log_buffer,
                        ));
                        event_handler::actions::handle_confirm_result(result, ctx);
                    }
                }
                key_handler::AppAction::Cancel | key_handler::AppAction::DismissDialog => {}
                key_handler::AppAction::DiscardSettings => {
                    ctx.state.close_settings();
                }
                key_handler::AppAction::SaveSettings => {
                    event_handler::actions::save_settings(
                        ctx.state,
                        ctx.ipc,
                        ctx.rt,
                        ctx.log_buffer,
                    );
                }
                key_handler::AppAction::None => {}
            }
        }
        CrosstermEvent::Mouse(mouse) => {
            event_handler::mouse::handle_mouse(
                mouse,
                ctx.state,
                ctx.log_buffer,
                event_handler::mouse::MouseRuntime {
                    ipc: ctx.ipc,
                    rt: ctx.rt,
                    daemon: ctx.daemon,
                    worker: ctx.worker,
                    daemon_task: ctx.daemon_task.as_deref_mut(),
                    worker_task: ctx.worker_task.as_deref_mut(),
                    project_dir: ctx.project_dir,
                    full_quit: ctx.full_quit,
                    full_quit_stop_sent: ctx.full_quit_stop_sent,
                },
            );
        }
        CrosstermEvent::Resize(w, h) => {
            ctx.state.update_terminal_size(w, h);
            ctx.state.needs_redraw = true;
        }
        _ => {}
    }
}

fn terminal_too_small(area: Rect) -> bool {
    area.width < MIN_TERMINAL_WIDTH || area.height < MIN_TERMINAL_HEIGHT
}

fn render_terminal_too_small(frame: &mut ratatui::Frame, area: Rect) {
    let lines = vec![
        Line::from(Span::styled(
            "终端窗口过小",
            Style::default().add_modifier(Modifier::BOLD),
        )),
        Line::from(format!("当前 {}x{}", area.width, area.height)),
        Line::from(format!(
            "至少需要 {}x{}",
            MIN_TERMINAL_WIDTH, MIN_TERMINAL_HEIGHT
        )),
    ];
    let widget = Paragraph::new(lines)
        .alignment(Alignment::Center)
        .block(Block::default().borders(Borders::ALL));
    frame.render_widget(widget, area);
}

fn do_redraw(
    terminal: &mut Terminal<CrosstermBackend<io::Stdout>>,
    state: &mut AppState,
    log_buffer: &LogBuffer,
) -> Result<(), String> {
    terminal
        .draw(|frame| {
            let area = frame.area();
            if terminal_too_small(area) {
                render_terminal_too_small(frame, area);
                return;
            }

            let layout = AppLayout::new(area);

            state.clamp_table_scroll(layout.status_table.height.saturating_sub(3));

            let info_lines = ui::logs::compute_info_lines_no_wrap(log_buffer, &state.theme);
            let info_visual_count = info_lines.0.len();
            let info_max_width = info_lines.1;
            let detail_lines = ui::logs::compute_detail_lines_no_wrap(
                log_buffer,
                &state.log_filter_level,
                &state.log_filter_source,
            );
            let detail_visual_count = detail_lines.0.len();
            let detail_max_width = detail_lines.1;

            let info_inner_height = layout.info_panel.height.saturating_sub(2) as usize;
            let detail_inner_height = layout.detail_panel.height.saturating_sub(2) as usize;
            let info_has_hscroll =
                info_max_width > layout.info_panel.width.saturating_sub(2) as usize;
            let detail_has_hscroll =
                detail_max_width > layout.detail_panel.width.saturating_sub(2) as usize;
            let info_content_height = if info_has_hscroll {
                info_inner_height.saturating_sub(1)
            } else {
                info_inner_height
            };
            let detail_content_height = if detail_has_hscroll {
                detail_inner_height.saturating_sub(1)
            } else {
                detail_inner_height
            };

            let new_logs_arrived = log_buffer.log_generation != state.last_log_generation;
            if new_logs_arrived {
                state.last_log_generation = log_buffer.log_generation;
            }

            apply_auto_scroll(
                &mut state.info_log_auto_scroll,
                &mut state.info_log_scroll,
                info_visual_count,
                info_content_height,
            );
            apply_auto_scroll(
                &mut state.detail_log_auto_scroll,
                &mut state.detail_log_scroll,
                detail_visual_count,
                detail_content_height,
            );
            state.clamp_detail_scroll(detail_visual_count as u16, detail_content_height as u16);
            state.clamp_info_scroll(info_visual_count as u16, info_content_height as u16);

            let info_inner_width = layout.info_panel.width.saturating_sub(2) as usize;
            let info_has_vscroll = info_visual_count > info_content_height;
            let info_content_width = if info_has_vscroll {
                info_inner_width.saturating_sub(1)
            } else {
                info_inner_width
            };
            state.clamp_info_hscroll(info_max_width, info_content_width);

            let detail_inner_width = layout.detail_panel.width.saturating_sub(2) as usize;
            let detail_has_vscroll = detail_visual_count > detail_content_height;
            let detail_content_width = if detail_has_vscroll {
                detail_inner_width.saturating_sub(1)
            } else {
                detail_inner_width
            };
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
                    Some(ScrollbarInfo::new(
                        sb_area,
                        state.configs.len(),
                        visible_data_rows,
                        state.table_scroll_offset as usize,
                    ))
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
                Some(ScrollbarInfo::new(
                    sb_area,
                    info_visual_count,
                    info_content_height,
                    state.info_log_scroll as usize,
                ))
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
                Some(ScrollbarInfo::new(
                    sb_area,
                    info_max_width,
                    info_content_width,
                    state.info_log_hscroll as usize,
                ))
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
                Some(ScrollbarInfo::new(
                    sb_area,
                    detail_visual_count,
                    detail_content_height,
                    state.detail_log_scroll as usize,
                ))
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
                Some(ScrollbarInfo::new(
                    sb_area,
                    detail_max_width,
                    detail_content_width,
                    state.detail_log_hscroll as usize,
                ))
            } else {
                None
            };

            ui::header::render_header(frame, layout.header, state);
            ui::header::render_info_bar(frame, layout.info_bar, state);
            ui::table::render_table(frame, layout.status_table, state);
            ui::logs::render_info_panel(
                frame,
                layout.info_panel,
                log_buffer,
                state.info_log_scroll,
                state.focus_zone,
                state.info_log_hscroll,
                state.info_log_auto_scroll,
                &state.theme,
            );
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
                    theme: &state.theme,
                },
            );
            ui::command_bar::render_command_bar(
                frame,
                layout.cmd_input,
                layout.quick_buttons,
                state,
            );

            match state.ui_mode {
                UiMode::ConfirmDialog => {
                    if let Some(ref msg) = state.confirm_message {
                        let info = ui::dialogs::render_confirm_dialog(
                            frame,
                            area,
                            msg,
                            state.dialog_scroll,
                            state.hovered_dialog_button,
                            state.clicked_dialog_button,
                            &state.theme,
                        );
                        state.scrollbar_info.dialog_v =
                            if info.content_total_lines > info.content_visible_lines {
                                Some(ScrollbarInfo::new(
                                    info.scrollbar_area,
                                    info.content_total_lines,
                                    info.content_visible_lines,
                                    state.dialog_scroll as usize,
                                ))
                            } else {
                                None
                            };
                        state.dialog_button_bar_y = Some(info.button_bar_y);
                    }
                }
                UiMode::CheckResult => {
                    if let Some(ref data) = state.check_data {
                        let info = ui::dialogs::render_check_result(
                            frame,
                            area,
                            data,
                            state.dialog_scroll,
                            state.hovered_dialog_button,
                            state.clicked_dialog_button,
                            &state.theme,
                        );
                        state.scrollbar_info.dialog_v =
                            if info.content_total_lines > info.content_visible_lines {
                                Some(ScrollbarInfo::new(
                                    info.scrollbar_area,
                                    info.content_total_lines,
                                    info.content_visible_lines,
                                    state.dialog_scroll as usize,
                                ))
                            } else {
                                None
                            };
                        state.dialog_button_bar_y = Some(info.button_bar_y);
                        state.clamp_dialog_scroll(
                            info.content_total_lines,
                            info.content_visible_lines,
                        );
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
                            &state.theme,
                        );
                        ss.field_positions = info.field_positions;
                        state.scrollbar_info.dialog_v =
                            if info.content_total_lines > info.content_visible_lines {
                                Some(ScrollbarInfo::new(
                                    info.scrollbar_area,
                                    info.content_total_lines,
                                    info.content_visible_lines,
                                    ss.scroll as usize,
                                ))
                            } else {
                                None
                            };
                        state.dialog_button_bar_y = Some(info.button_bar_y);
                        // Clamp settings scroll
                        let max_scroll = info
                            .content_total_lines
                            .saturating_sub(info.content_visible_lines)
                            as u16;
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
        })
        .map_err(|e| e.to_string())?;

    Ok(())
}

pub(crate) fn point_in_rect(col: u16, row: u16, rect: ratatui::layout::Rect) -> bool {
    col >= rect.x && col < rect.x + rect.width && row >= rect.y && row < rect.y + rect.height
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::Value;
    use std::io::{BufRead, BufReader, Write};
    use std::net::TcpListener;

    fn write_ipc_response(stream: &mut std::net::TcpStream, request: &str, data: &str) {
        let request: Value = serde_json::from_str(request.trim()).expect("request json");
        let request_id = request
            .get("request_id")
            .and_then(Value::as_str)
            .expect("request id");
        let response =
            format!(r#"{{"status":"ok","data":{data},"message":"","request_id":"{request_id}"}}"#);
        stream
            .write_all(format!("{response}\n").as_bytes())
            .expect("write ipc response");
    }

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
        assert_eq!(
            result.len(),
            15,
            "日期时间格式应为 YYYYMMDD_HHMMSS (15字符)"
        );
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
    fn terminal_size_guard_matches_layout_minimum() {
        assert!(terminal_too_small(Rect::new(0, 0, 79, 24)));
        assert!(terminal_too_small(Rect::new(0, 0, 120, 15)));
        assert!(!terminal_too_small(Rect::new(0, 0, 80, 16)));
        assert!(!terminal_too_small(Rect::new(0, 0, 120, 30)));
    }

    #[test]
    fn find_latest_client_session_prefers_structured_log_layout() {
        let _guard = TEST_ENV_LOCK.lock().expect("env lock poisoned");
        let project_dir =
            std::env::temp_dir().join(format!("autofluid-tui-log-test-{}", generate_request_id()));
        let structured = project_dir
            .join("logs")
            .join("local")
            .join("sessions")
            .join("client")
            .join("2026-06-18_12-30-00");
        let legacy = project_dir
            .join("logs")
            .join("client")
            .join("2099-01-01_00-00-00");
        std::fs::create_dir_all(&structured).expect("create structured log dir");
        std::fs::create_dir_all(&legacy).expect("create legacy log dir");
        let previous_dir = std::env::current_dir().expect("current dir");
        std::env::set_current_dir(&project_dir).expect("set temp cwd");

        let result = find_latest_client_session_dir();

        std::env::set_current_dir(previous_dir).expect("restore cwd");
        std::fs::remove_dir_all(project_dir).ok();
        assert_eq!(result.as_deref(), Some(structured.as_path()));
    }

    #[test]
    fn find_latest_client_session_falls_back_to_legacy_log_layout() {
        let _guard = TEST_ENV_LOCK.lock().expect("env lock poisoned");
        let project_dir = std::env::temp_dir().join(format!(
            "autofluid-tui-log-legacy-test-{}",
            generate_request_id()
        ));
        let legacy = project_dir
            .join("logs")
            .join("client")
            .join("2026-06-18_12-30-00");
        std::fs::create_dir_all(&legacy).expect("create legacy log dir");
        let previous_dir = std::env::current_dir().expect("current dir");
        std::env::set_current_dir(&project_dir).expect("set temp cwd");

        let result = find_latest_client_session_dir();

        std::env::set_current_dir(previous_dir).expect("restore cwd");
        std::fs::remove_dir_all(project_dir).ok();
        assert_eq!(result.as_deref(), Some(legacy.as_path()));
    }

    #[test]
    fn test_generate_request_id() {
        let id1 = generate_request_id();
        let id2 = generate_request_id();
        assert_eq!(id1.len(), 16, "请求ID应为16字符");
        assert_eq!(id2.len(), 16, "请求ID应为16字符");
        assert!(
            id1.chars().all(|c| c.is_ascii_hexdigit()),
            "请求ID应为十六进制"
        );
        assert!(
            id2.chars().all(|c| c.is_ascii_hexdigit()),
            "请求ID应为十六进制"
        );
        assert_ne!(id1, id2, "连续请求ID不应重复");
    }

    #[test]
    fn test_apply_auto_scroll_reenables_when_manually_scrolled_to_bottom() {
        let mut auto_scroll = false;
        let mut scroll = 90;

        apply_auto_scroll(&mut auto_scroll, &mut scroll, 100, 10);

        assert!(auto_scroll, "手动滚动到底部时应恢复自动滚动");
        assert_eq!(scroll, 90);
    }

    #[test]
    fn test_apply_auto_scroll_stays_disabled_when_not_at_bottom() {
        let mut auto_scroll = false;
        let mut scroll = 89;

        apply_auto_scroll(&mut auto_scroll, &mut scroll, 100, 10);

        assert!(!auto_scroll, "未滚动到底部时不应恢复自动滚动");
        assert_eq!(scroll, 89);
    }

    #[test]
    fn test_initial_ipc_poll_timestamp_waits_one_interval() {
        let interval = Duration::from_secs(60);
        let last_poll = initial_ipc_poll_timestamp();

        assert!(!should_poll_ipc(last_poll, interval));
    }

    #[test]
    fn test_startup_connection_failure_begins_reconnect_wait() {
        let mut daemon = daemon_mgr::DaemonManager::new();
        let mut state = AppState::new();
        let mut log_buffer = LogBuffer::new();

        handle_startup_connect_failure(&mut daemon, &mut state, &mut log_buffer, "", "127.0.0.1");

        assert!(!state.connected);
        assert!(state.needs_redraw);
        assert!(daemon.has_pending_ipc_reconnect());
        assert!(log_buffer
            .info_messages
            .iter()
            .any(|message| message.contains("自动重连")));
    }

    #[test]
    fn test_dashboard_poll_connection_loss_begins_reconnect_wait() {
        let mut daemon = daemon_mgr::DaemonManager::new();
        let mut state = AppState {
            connected: false,
            ..Default::default()
        };
        let mut log_buffer = LogBuffer::new();

        handle_dashboard_poll_connection_state(
            &mut daemon,
            &mut state,
            &mut log_buffer,
            "",
            "127.0.0.1",
            true,
            false,
        );

        assert!(!state.connected);
        assert!(state.needs_redraw);
        assert!(daemon.has_pending_ipc_reconnect());
        assert!(log_buffer
            .info_messages
            .iter()
            .any(|message| message.contains("后台自动重连")));
    }

    #[test]
    fn start_worker_lifecycle_task_rejects_duplicate_operation() {
        let (_sender, receiver) = mpsc::channel();
        let mut worker_task = Some(WorkerLifecycleTask {
            receiver,
            handle: None,
        });
        let mut state = AppState::new();
        let mut log_buffer = LogBuffer::new();

        start_worker_lifecycle_task(
            Some(&mut worker_task),
            WorkerLifecycleAction::Start,
            "C:\\tmp",
            "127.0.0.1",
            9527,
            &mut log_buffer,
            &mut state,
        );

        assert!(worker_task.is_some());
        assert!(state.needs_redraw);
        assert!(log_buffer
            .info_messages
            .iter()
            .any(|message| message.contains("生命周期任务正在运行")));
    }

    #[test]
    fn worker_health_watchdog_warns_without_requesting_restart() {
        let mut worker = worker_mgr::WorkerManager::new();
        let mut state = AppState::default();
        state.health_info.local_worker_online = Some(false);
        state.health_info.server_to_local_ssh = Some("disconnected".to_string());
        let mut log_buffer = LogBuffer::new();

        poll_worker_health_watchdog(&mut worker, &state, &mut log_buffer);

        assert!(log_buffer
            .info_messages
            .iter()
            .any(|message| message.contains("worker restart")));
        assert!(state.pending_command.is_none());
    }

    #[test]
    fn worker_health_watchdog_deduplicates_repeated_warning() {
        let mut worker = worker_mgr::WorkerManager::new();
        let mut state = AppState::default();
        state.health_info.local_worker_online = Some(false);
        state.health_info.server_to_local_ssh = Some("disconnected".to_string());
        let mut log_buffer = LogBuffer::new();

        poll_worker_health_watchdog(&mut worker, &state, &mut log_buffer);
        poll_worker_health_watchdog(&mut worker, &state, &mut log_buffer);

        let warning_count = log_buffer
            .info_messages
            .iter()
            .filter(|message| message.contains("Worker 或 SSH 隧道异常"))
            .count();
        assert_eq!(warning_count, 1);
    }

    #[test]
    fn worker_health_watchdog_does_not_warn_on_unknown_health() {
        let mut worker = worker_mgr::WorkerManager::new();
        let mut state = AppState::default();
        state.health_info.local_worker_online = None;
        state.health_info.server_to_local_ssh = Some("unknown".to_string());
        state.health_info.server_to_workstation_ssh = Some("unknown".to_string());
        let mut log_buffer = LogBuffer::new();

        poll_worker_health_watchdog(&mut worker, &state, &mut log_buffer);

        assert!(log_buffer.info_messages.is_empty());
    }

    #[test]
    fn worker_lifecycle_commands_do_not_start_workers_inline() {
        let source = include_str!("lib.rs");
        let start_arm = source
            .split("command::CommandResult::StartWorkers => {")
            .nth(1)
            .expect("start workers arm should exist")
            .split("command::CommandResult::StopWorkers => {")
            .next()
            .expect("start workers arm should end before stop workers arm");
        assert!(
            start_arm.contains("start_worker_lifecycle_task"),
            "worker start should hand off to a background lifecycle task"
        );
        assert!(
            !start_arm.contains("start_workers_with_prepare"),
            "worker start must not run the blocking startup chain on the TUI event loop"
        );

        let restart_arm = source
            .split("command::CommandResult::RestartWorkers => {")
            .nth(1)
            .expect("restart workers arm should exist")
            .split("command::CommandResult::None => {}")
            .next()
            .expect("restart workers arm should end before none arm");
        assert!(
            restart_arm.contains("start_worker_lifecycle_task"),
            "worker restart should hand off to a background lifecycle task"
        );
        assert!(
            !restart_arm.contains("start_workers_with_prepare"),
            "worker restart must not run the blocking startup chain on the TUI event loop"
        );
    }

    #[test]
    fn daemon_lifecycle_commands_do_not_stop_or_restart_inline() {
        let source = include_str!("lib.rs");
        let restart_arm = source
            .split("command::CommandResult::RestartDaemon => {")
            .nth(1)
            .expect("restart daemon arm should exist")
            .split("command::CommandResult::StopDaemon => {")
            .next()
            .expect("restart daemon arm should end before stop daemon arm");
        assert!(
            restart_arm.contains("start_daemon_lifecycle_task"),
            "daemon restart should hand off to a background lifecycle task"
        );
        assert!(
            !restart_arm.contains("restart_with_ipc"),
            "daemon restart must not run the blocking restart chain on the TUI event loop"
        );

        let stop_arm = source
            .split("command::CommandResult::StopDaemon => {")
            .nth(1)
            .expect("stop daemon arm should exist")
            .split("command::CommandResult::StartWorkers => {")
            .next()
            .expect("stop daemon arm should end before start workers arm");
        assert!(
            stop_arm.contains("start_daemon_lifecycle_task"),
            "daemon stop should hand off to a background lifecycle task"
        );
        assert!(
            !stop_arm.contains("stop_with_ipc"),
            "daemon stop must not run the blocking stop chain on the TUI event loop"
        );
    }
    #[test]
    fn test_dashboard_poll_connection_loss_reconnects_to_finished_daemon() {
        let listener = TcpListener::bind("127.0.0.1:0").expect("bind test listener");
        let port = listener.local_addr().expect("listener address").port();
        let server = std::thread::spawn(move || {
            let (mut first_stream, _) = listener.accept().expect("accept first client");
            let mut first_reader = BufReader::new(first_stream.try_clone().expect("clone stream"));

            let mut handshake = String::new();
            first_reader
                .read_line(&mut handshake)
                .expect("read first handshake");
            write_ipc_response(
                &mut first_stream,
                &handshake,
                r#"{"engine_status":"stopped","pipeline_started":true}"#,
            );

            let mut dashboard = String::new();
            first_reader
                .read_line(&mut dashboard)
                .expect("read dashboard");
            let dashboard_request: Value =
                serde_json::from_str(dashboard.trim()).expect("dashboard json");
            assert_eq!(
                dashboard_request.get("command").and_then(Value::as_str),
                Some(ipc::protocol::CMD_GET_DASHBOARD)
            );
            drop(first_stream);

            let (mut reconnect_stream, _) = listener.accept().expect("accept reconnect client");
            let mut reconnect_reader =
                BufReader::new(reconnect_stream.try_clone().expect("clone reconnect"));
            let mut reconnect_handshake = String::new();
            reconnect_reader
                .read_line(&mut reconnect_handshake)
                .expect("read reconnect handshake");
            write_ipc_response(
                &mut reconnect_stream,
                &reconnect_handshake,
                r#"{"engine_status":"stopped","pipeline_started":true}"#,
            );
        });
        let rt = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .expect("runtime");
        let mut ipc = IpcClient::new(Some("127.0.0.1"), Some(port));
        let mut daemon = daemon_mgr::DaemonManager::new();
        let mut state = AppState::default();
        let mut log_buffer = LogBuffer::new();

        rt.block_on(ipc.connect()).expect("initial connect");
        state.connected = true;
        let was_connected = ipc.is_connected();
        assert!(!refresh_dashboard_once(
            &rt,
            &mut ipc,
            &mut state,
            &mut log_buffer
        ));
        let after_dashboard = ipc.is_connected();
        handle_dashboard_poll_connection_state(
            &mut daemon,
            &mut state,
            &mut log_buffer,
            "",
            ipc.host(),
            was_connected,
            after_dashboard,
        );
        assert!(daemon.has_pending_ipc_reconnect());

        daemon.poll_ipc_reconnect(&rt, &mut ipc, &mut state, &mut log_buffer);

        assert!(state.connected);
        assert!(ipc.is_connected());
        assert!(!daemon.has_pending_ipc_reconnect());
        assert!(log_buffer
            .info_messages
            .iter()
            .any(|message| message.contains("已连接到后台引擎")));
        rt.block_on(ipc.disconnect());
        server.join().expect("server thread");
    }

    #[test]
    fn test_should_poll_ipc_respects_interval() {
        let last_poll = Instant::now();

        assert!(!should_poll_ipc(last_poll, Duration::from_secs(1)));
    }

    #[test]
    fn test_submit_command_marks_redraw_for_immediate_repaint() {
        let rt = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .expect("runtime");
        let mut ipc = IpcClient::new(Some("127.0.0.1"), Some(9));
        let mut state = AppState {
            needs_redraw: false,
            ..Default::default()
        };
        let mut log_buffer = LogBuffer::new();

        let result = submit_command("help", "test", &rt, &mut ipc, &mut state, &mut log_buffer);

        assert!(matches!(result, command::CommandResult::None));
        assert!(
            state.needs_redraw,
            "submitting a command must repaint log/input changes without waiting for resize"
        );
    }

    #[test]
    fn test_full_quit_confirm_defers_worker_cleanup_to_shutdown_phase() {
        let project_dir = std::env::temp_dir().join(format!(
            "autofluid-tui-full-quit-defers-cleanup-{}",
            generate_request_id()
        ));
        std::fs::create_dir_all(project_dir.join("data")).expect("create data dir");
        let worker_pid_file = project_dir.join("data").join("local_worker.pid");
        std::fs::write(&worker_pid_file, "999999").expect("write worker pid");

        let rt = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .expect("runtime");
        let mut ipc = IpcClient::new(Some("127.0.0.1"), Some(9));
        let mut state = AppState::new();
        let mut log_buffer = LogBuffer::new();
        let mut daemon = daemon_mgr::DaemonManager::new();
        let mut worker = worker_mgr::WorkerManager::new();
        let mut full_quit = false;
        let mut full_quit_stop_sent = false;
        let project_dir_string = project_dir.to_string_lossy().to_string();

        let mut ctx = EventContext {
            state: &mut state,
            log_buffer: &mut log_buffer,
            ipc: &mut ipc,
            check_task: None,
            daemon_task: None,
            worker_task: None,
            daemon: &mut daemon,
            worker: &mut worker,
            rt: &rt,
            project_dir: &project_dir_string,
            full_quit: &mut full_quit,
            full_quit_stop_sent: &mut full_quit_stop_sent,
        };

        event_handler::actions::handle_confirm_result(command::CommandResult::FullQuit, &mut ctx);

        assert!(state.should_quit);
        assert!(full_quit);
        assert!(
            worker_pid_file.exists(),
            "FullQuit handling should not perform worker PID cleanup before the shutdown phase"
        );

        std::fs::remove_dir_all(project_dir).ok();
    }

    #[test]
    fn test_apply_check_response_opens_result_dialog() {
        let mut state = AppState::default();
        let mut log_buffer = LogBuffer::new();
        let response = IpcResponse {
            status: "ok".to_string(),
            data: serde_json::json!({
                "health": {
                    "local_worker_online": true,
                    "server_to_local_ssh": "ok"
                }
            }),
            message: "系统自检完成".to_string(),
            request_id: "test".to_string(),
        };

        apply_check_response(Ok(response), &mut state, &mut log_buffer);

        assert!(matches!(state.ui_mode, UiMode::CheckResult));
        assert_eq!(
            state
                .check_data
                .as_ref()
                .and_then(|data| data.get("health"))
                .and_then(|health| health.get("local_worker_online"))
                .and_then(|value| value.as_bool()),
            Some(true)
        );
        assert!(state.needs_redraw);
        assert!(log_buffer
            .info_messages
            .iter()
            .any(|message| message.contains("系统自检完成")));
    }

    #[test]
    fn test_apply_log_entries_response_handles_reset_and_gap() {
        let mut state = AppState {
            last_log_id: 99,
            ..Default::default()
        };
        let mut log_buffer = LogBuffer::new();
        log_buffer.push_detail(LogEntry {
            id: 90,
            timestamp: "2026-06-10 12:00:00".to_string(),
            level: "INFO".to_string(),
            source: "system".to_string(),
            logger_name: "old".to_string(),
            message: "old".to_string(),
            raw_message: "old".to_string(),
            category: "general".to_string(),
            config_name: None,
            step_name: None,
            worker_id: None,
            is_polling: false,
        });
        let data = serde_json::json!({
            "reset": true,
            "has_gap": true,
            "latest_id": 5,
            "entries": [{
                "id": 5,
                "timestamp": "2026-06-10 12:00:01",
                "level": "ERROR",
                "source": "scheduler",
                "logger_name": "engine.scheduler.main",
                "message": "new",
                "raw_message": "new",
                "category": "step"
            }]
        });
        let obj = data.as_object().expect("object response");

        apply_log_entries_response(obj, &mut state, &mut log_buffer);

        assert_eq!(state.last_log_id, 5);
        assert_eq!(log_buffer.detail_buffer.len(), 1);
        assert_eq!(log_buffer.detail_buffer[0].id, 5);
        assert!(log_buffer
            .info_messages
            .iter()
            .any(|message| message.contains("详细日志存在缺口")));
    }

    #[test]
    fn test_apply_dashboard_response_updates_state_engine_and_logs() {
        let mut state = AppState::default();
        let mut log_buffer = LogBuffer::new();
        let data = serde_json::json!({
            "statuses": {
                "2": {"sw": "Running"}
            },
            "config_workstations": {
                "2": "WS-B"
            },
            "engine": {
                "engine_status": "running",
                "sw_macro_started": true,
                "barrier_passed": false,
                "pipeline_started": true,
                "daemon_started_at": 1718000000.0,
                "daemon_started_at_display": "2026-06-13 14:03:21",
                "daemon_uptime_seconds": 65,
                "solver_progress": {
                    "config_name": 2,
                    "current_iter": 3,
                    "total_iter": 10,
                    "remaining_sec": 125.0,
                    "updated_at": 1718000065.0
                }
            },
            "health": {
                "local_worker_online": true,
                "server_to_local_ssh": "ok",
                "server_to_workstation_ssh": "disconnected",
                "workstation_ssh_details": {
                    "WS-A": "ok",
                    "WS-B": "disconnected"
                }
            },
            "logs": {
                "entries": [{
                    "id": 8,
                    "timestamp": "2026-06-10 12:00:01",
                    "level": "INFO",
                    "source": "scheduler",
                    "logger_name": "engine.scheduler.main",
                    "message": "new",
                    "raw_message": "new",
                    "category": "step"
                }],
                "latest_id": 8,
                "has_gap": false,
                "reset": false
            }
        });
        let obj = data.as_object().expect("object response");

        apply_dashboard_response(obj, &mut state, &mut log_buffer);

        assert_eq!(state.get_step_status("2", "sw"), "Running");
        assert_eq!(state.config_cell_text("2"), "2");
        assert_eq!(state.work_location_cell_text("2"), "本地");
        assert_eq!(state.engine_info.engine_status, "running");
        assert!(state.engine_info.sw_macro_started);
        assert_eq!(state.engine_info.daemon_started_at, Some(1718000000.0));
        assert_eq!(
            state.engine_info.daemon_started_at_display.as_deref(),
            Some("2026-06-13 14:03:21")
        );
        assert_eq!(state.engine_info.daemon_uptime_seconds, Some(65));
        let progress = state
            .engine_info
            .solver_progress
            .as_ref()
            .expect("solver progress");
        assert_eq!(progress.config_name, 2);
        assert_eq!(progress.remaining_sec, 125.0);
        assert_eq!(state.health_info.local_worker_online, Some(true));
        assert_eq!(state.health_info.server_to_local_ssh.as_deref(), Some("ok"));
        assert_eq!(
            state.health_info.server_to_workstation_ssh.as_deref(),
            Some("disconnected")
        );
        assert_eq!(state.last_log_id, 8);
        assert_eq!(log_buffer.detail_buffer.len(), 1);
    }

    #[test]
    fn test_apply_dashboard_response_announces_step_completion_transition() {
        let mut state = AppState::default();
        let mut log_buffer = LogBuffer::new();

        let running = serde_json::json!({
            "statuses": {
                "3": {"transfer": "Running"}
            }
        });
        apply_dashboard_response(
            running.as_object().expect("object response"),
            &mut state,
            &mut log_buffer,
        );

        let completed = serde_json::json!({
            "statuses": {
                "3": {"transfer": "Completed"}
            }
        });
        apply_dashboard_response(
            completed.as_object().expect("object response"),
            &mut state,
            &mut log_buffer,
        );

        assert!(log_buffer
            .info_messages
            .iter()
            .any(|message| message.contains("构型3 文件传输 已完成")));
    }

    #[test]
    fn test_apply_dashboard_response_does_not_announce_initial_completed_state() {
        let mut state = AppState::default();
        let mut log_buffer = LogBuffer::new();
        let data = serde_json::json!({
            "statuses": {
                "3": {"transfer": "Completed"}
            }
        });

        apply_dashboard_response(
            data.as_object().expect("object response"),
            &mut state,
            &mut log_buffer,
        );

        assert!(log_buffer.info_messages.is_empty());
    }

    #[test]
    fn test_worker_health_visible_requires_registered_local_worker_reachability() {
        let mut state = AppState::default();
        state.health_info.server_to_workstation_ssh = Some("ok".to_string());

        assert!(!worker_health_is_visible(&state));

        state.health_info.local_worker_online = Some(true);
        state.health_info.server_to_local_ssh = Some("disconnected".to_string());

        assert!(worker_health_is_visible(&state));
        assert!(state.info_bar_text().contains("S→L:断"));

        state.health_info.server_to_local_ssh = Some("ok".to_string());

        assert!(worker_health_is_visible(&state));
        assert!(state.info_bar_text().contains("LW:OK"));
        assert!(state.info_bar_text().contains("S→L:OK"));
        assert!(state.info_bar_text().contains("S→W:OK"));
    }

    #[test]
    fn test_format_local_time_consistency() {
        let r1 = format_local_time("%H:%M:%S");
        let r2 = format_local_time("%H:%M:%S");
        assert_eq!(r1, r2, "同一秒内两次调用结果应相同");
    }
}
