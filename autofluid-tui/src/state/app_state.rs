use std::collections::{BTreeMap, HashMap};
use std::time::Duration;

use ratatui::style::Style;
use ratatui::text::{Line, Span};

use crate::settings::SettingsState;
use crate::text_buffer::TextBuffer;
use crate::theme::AppTheme;

pub const STATUS_WAITING: &str = "Waiting";
pub const STATUS_RUNNING: &str = "Running";
pub const STATUS_PAUSED: &str = "Paused";
pub const STATUS_RETRYING: &str = "Retrying";
pub const STATUS_COMPLETED: &str = "Completed";
pub const STATUS_ERROR: &str = "Error";
pub const SETTINGS_LOCKED_MESSAGE: &str =
    "⚠ 流水线已启动过，配置已锁定。请重启 Daemon 后再修改设置";

pub const STEP_NAMES: [&str; 6] = ["sw", "sc", "transfer", "meshing", "solver", "postprocess"];

pub const STEP_DISPLAY: [(&str, &str); 6] = [
    ("sw", "SolidWorks导出"),
    ("sc", "SpaceClaim转换"),
    ("transfer", "文件传输"),
    ("meshing", "网格划分"),
    ("solver", "仿真求解"),
    ("postprocess", "后处理"),
];

pub fn step_display_name(step: &str) -> &str {
    STEP_DISPLAY
        .iter()
        .find(|(s, _)| *s == step)
        .map(|(_, d)| *d)
        .unwrap_or(step)
}

pub fn status_icon(status: &str) -> &str {
    match status {
        STATUS_WAITING => "🕐",
        STATUS_RUNNING => "⏳",
        STATUS_PAUSED => "⏸️",
        STATUS_RETRYING => "🔄",
        STATUS_COMPLETED => "✅",
        STATUS_ERROR => "❌",
        _ => "?",
    }
}

pub fn status_color(status: &str) -> ratatui::style::Color {
    match status {
        STATUS_WAITING => ratatui::style::Color::DarkGray,
        STATUS_RUNNING => ratatui::style::Color::Yellow,
        STATUS_PAUSED => ratatui::style::Color::Gray,
        STATUS_RETRYING => ratatui::style::Color::Rgb(255, 136, 0),
        STATUS_COMPLETED => ratatui::style::Color::Green,
        STATUS_ERROR => ratatui::style::Color::Red,
        _ => ratatui::style::Color::White,
    }
}

pub fn engine_status_display(status: &str) -> &str {
    match status {
        "stopped" => "已停止",
        "running" => "运行中",
        "paused" => "已暂停",
        _ => "未知",
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum FocusZone {
    #[default]
    CommandInput,
    Table,
    InfoLog,
    DetailLog,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ScrollbarDragZone {
    TableVertical,
    InfoVertical,
    InfoHorizontal,
    DetailVertical,
    DetailHorizontal,
    DialogVertical,
}

impl FocusZone {
    pub fn cycle_next(self) -> Self {
        match self {
            FocusZone::CommandInput => FocusZone::Table,
            FocusZone::Table => FocusZone::InfoLog,
            FocusZone::InfoLog => FocusZone::DetailLog,
            FocusZone::DetailLog => FocusZone::CommandInput,
        }
    }

    pub fn cycle_prev(self) -> Self {
        match self {
            FocusZone::CommandInput => FocusZone::DetailLog,
            FocusZone::Table => FocusZone::CommandInput,
            FocusZone::InfoLog => FocusZone::Table,
            FocusZone::DetailLog => FocusZone::InfoLog,
        }
    }
}

#[derive(Debug, Clone, Default)]
pub struct EngineInfo {
    pub engine_status: String,
    pub sw_macro_started: bool,
    pub barrier_passed: bool,
    pub workstation_barriers: BTreeMap<String, bool>,
    pub pipeline_started: bool,
    pub daemon_started_at: Option<f64>,
    pub daemon_started_at_display: Option<String>,
    pub daemon_uptime_seconds: Option<u64>,
    pub solver_progress: Option<SolverProgress>,
}

#[derive(Debug, Clone, Default)]
pub struct SolverProgress {
    pub config_name: u64,
    pub current_iter: Option<u64>,
    pub total_iter: u64,
    pub remaining_sec: f64,
    pub updated_at: Option<f64>,
}

#[derive(Debug, Clone, Default)]
pub struct HealthInfo {
    pub local_worker_online: Option<bool>,
    pub local_ipc_tunnel_ok: Option<bool>,
    pub local_ipc_tunnel_direct: bool,
    pub server_to_local_ssh: Option<String>,
    pub server_to_workstation_ssh: Option<String>,
    pub workstation_ssh_details: HashMap<String, String>,
}

#[derive(Debug, Clone, Copy, Default)]
pub struct ScrollbarInfo {
    pub area: ratatui::layout::Rect,
    pub total: usize,
    pub visible: usize,
    pub scroll: usize,
}

impl ScrollbarInfo {
    pub fn new(area: ratatui::layout::Rect, total: usize, visible: usize, scroll: usize) -> Self {
        Self {
            area,
            total,
            visible,
            scroll,
        }
    }
}

#[derive(Debug, Clone, Copy, Default)]
pub struct ScrollbarRenderedInfo {
    pub table_v: Option<ScrollbarInfo>,
    pub info_v: Option<ScrollbarInfo>,
    pub info_h: Option<ScrollbarInfo>,
    pub detail_v: Option<ScrollbarInfo>,
    pub detail_h: Option<ScrollbarInfo>,
    pub dialog_v: Option<ScrollbarInfo>,
}

#[derive(Debug, Clone, Default)]
pub struct AppState {
    pub connected: bool,
    pub status_data: HashMap<String, HashMap<String, String>>,
    pub config_workstations: HashMap<String, String>,
    pub configs: Vec<u64>,
    pub engine_info: EngineInfo,
    pub health_info: HealthInfo,
    pub last_log_id: u64,
    pub log_filter_level: Option<String>,
    pub log_filter_source: Option<String>,
    pub ui_mode: UiMode,
    pub should_quit: bool,
    pub needs_redraw: bool,
    pub command_buffer: TextBuffer,
    pub confirm_message: Option<String>,
    pub confirm_callback: Option<ConfirmAction>,
    pub check_data: Option<serde_json::Value>,
    pub focus_zone: FocusZone,
    pub table_scroll_offset: u16,
    pub info_log_scroll: u16,
    pub info_log_hscroll: u16,
    pub detail_log_scroll: u16,
    pub detail_log_hscroll: u16,
    pub detail_log_auto_scroll: bool,
    pub info_log_auto_scroll: bool,
    pub last_log_generation: u64,
    pub terminal_size: ratatui::layout::Rect,
    pub pending_command: Option<String>,
    pub pending_command_source: Option<&'static str>,
    pub hovered_table_row: Option<u16>,
    pub hovered_detail_row: Option<u16>,
    pub hovered_button: Option<u8>,
    pub hovered_dialog_button: Option<u8>,
    pub clicked_button: Option<u8>,
    pub daemon_menu_open: bool,
    pub hovered_daemon_menu_item: Option<u8>,
    pub clicked_daemon_menu_item: Option<u8>,
    pub daemon_menu_click_time: Option<std::time::Instant>,
    pub worker_menu_open: bool,
    pub hovered_worker_menu_item: Option<u8>,
    pub clicked_worker_menu_item: Option<u8>,
    pub worker_menu_click_time: Option<std::time::Instant>,
    pub clicked_dialog_button: Option<u8>,
    pub click_time: Option<std::time::Instant>,
    pub dialog_click_time: Option<std::time::Instant>,
    pub last_detail_click_time: Option<std::time::Instant>,
    pub last_detail_click_row: Option<u16>,
    pub clicked_detail_row: Option<u16>,
    pub detail_click_time: Option<std::time::Instant>,
    pub scrollbar_drag: Option<(ScrollbarDragZone, u16, u16)>,
    pub scrollbar_info: ScrollbarRenderedInfo,
    pub dialog_scroll: u16,
    pub dialog_button_bar_y: Option<u16>,
    pub settings_state: Option<SettingsState>,
    pub theme: AppTheme,
}

#[derive(Debug, Clone, PartialEq, Default)]
pub enum UiMode {
    #[default]
    Normal,
    ConfirmDialog,
    CheckResult,
    Settings,
}

#[derive(Debug, Clone)]
pub enum ConfirmAction {
    ResetStep {
        config_name: String,
        step_name: Option<String>,
    },
    CleanStep {
        step_name: String,
        config_name: Option<serde_json::Value>,
    },
    FullQuit,
    StopDaemon,
    StopWorkers,
    RestartWorkers,
}

impl AppState {
    pub fn new() -> Self {
        Self {
            detail_log_auto_scroll: true,
            info_log_auto_scroll: true,
            last_log_generation: 0,
            focus_zone: FocusZone::CommandInput,
            ..Default::default()
        }
    }

    pub fn update_terminal_size(&mut self, width: u16, height: u16) {
        self.terminal_size = ratatui::layout::Rect::new(0, 0, width, height);
    }

    /// 每帧调用一次：清理过期的点击动画状态。
    pub fn tick(&mut self) {
        let btn_timeout = Duration::from_millis(120);
        let detail_timeout = Duration::from_millis(20);

        self.needs_redraw |=
            expire_click(&mut self.click_time, &mut self.clicked_button, btn_timeout);
        self.needs_redraw |= expire_click(
            &mut self.daemon_menu_click_time,
            &mut self.clicked_daemon_menu_item,
            btn_timeout,
        );
        self.needs_redraw |= expire_click(
            &mut self.dialog_click_time,
            &mut self.clicked_dialog_button,
            btn_timeout,
        );
        self.needs_redraw |= expire_click(
            &mut self.detail_click_time,
            &mut self.clicked_detail_row,
            detail_timeout,
        );
        if let Some(ref mut ss) = self.settings_state {
            self.needs_redraw |= expire_click(
                &mut ss.field_click_time,
                &mut ss.clicked_field,
                detail_timeout,
            );
        }
    }

    pub fn update_status_data(&mut self, data: &serde_json::Value) {
        self.status_data.clear();
        self.configs.clear();

        if let Some(obj) = data.as_object() {
            let mut config_list: Vec<u64> = Vec::new();
            for (key, value) in obj {
                if let Ok(cn) = key.parse::<u64>() {
                    config_list.push(cn);
                    if let Some(steps) = value.as_object() {
                        let mut step_map: HashMap<String, String> = HashMap::new();
                        for (step_name, step_val) in steps {
                            if let Some(status) = step_val.as_str() {
                                step_map.insert(step_name.clone(), status.to_string());
                            }
                        }
                        self.status_data.insert(key.clone(), step_map);
                    }
                }
            }
            config_list.sort();
            self.configs = config_list;
        }
        self.needs_redraw = true;
    }

    pub fn update_config_workstations(&mut self, data: &serde_json::Value) {
        self.config_workstations.clear();
        if let Some(obj) = data.as_object() {
            for (config_name, workstation) in obj {
                if let Some(workstation_id) = workstation.as_str() {
                    self.config_workstations
                        .insert(config_name.clone(), workstation_id.to_string());
                }
            }
        }
        self.needs_redraw = true;
    }

    pub fn config_cell_text(&self, config: &str) -> String {
        config.to_string()
    }

    pub fn work_location_cell_text(&self, config: &str) -> String {
        let Some(steps) = self.status_data.get(config) else {
            return String::new();
        };

        for step in STEP_NAMES {
            if steps.get(step).map(String::as_str) != Some(STATUS_RUNNING) {
                continue;
            }
            return match step {
                "sw" | "sc" => "本地".to_string(),
                "transfer" | "meshing" | "solver" | "postprocess" => self
                    .config_workstations
                    .get(config)
                    .map(String::as_str)
                    .filter(|workstation_id| {
                        !workstation_id.is_empty() && *workstation_id != "default"
                    })
                    .unwrap_or("")
                    .to_string(),
                _ => String::new(),
            };
        }

        String::new()
    }

    pub fn update_engine_info(&mut self, data: &serde_json::Value) {
        if let Some(obj) = data.as_object() {
            self.engine_info.engine_status = obj
                .get("engine_status")
                .and_then(|v| v.as_str())
                .unwrap_or("stopped")
                .to_string();
            self.engine_info.sw_macro_started = obj
                .get("sw_macro_started")
                .and_then(|v| v.as_bool())
                .unwrap_or(false);
            self.engine_info.barrier_passed = obj
                .get("barrier_passed")
                .and_then(|v| v.as_bool())
                .unwrap_or(false);
            self.engine_info.workstation_barriers.clear();
            if let Some(workstation_barriers) =
                obj.get("workstation_barriers").and_then(|v| v.as_object())
            {
                for (workstation_id, passed) in workstation_barriers {
                    if let Some(passed) = passed.as_bool() {
                        self.engine_info
                            .workstation_barriers
                            .insert(workstation_id.clone(), passed);
                    }
                }
            }
            self.engine_info.pipeline_started = obj
                .get("pipeline_started")
                .and_then(|v| v.as_bool())
                .unwrap_or(false);
            self.engine_info.daemon_started_at =
                obj.get("daemon_started_at").and_then(|v| v.as_f64());
            self.engine_info.daemon_started_at_display = obj
                .get("daemon_started_at_display")
                .and_then(|v| v.as_str())
                .map(str::to_string);
            self.engine_info.daemon_uptime_seconds =
                obj.get("daemon_uptime_seconds").and_then(|v| v.as_u64());
            self.engine_info.solver_progress =
                obj.get("solver_progress").and_then(parse_solver_progress);
        }
        self.needs_redraw = true;
    }

    pub fn update_health_info(&mut self, data: &serde_json::Value) {
        if let Some(obj) = data.as_object() {
            self.health_info.local_worker_online =
                obj.get("local_worker_online").and_then(|v| v.as_bool());
            self.health_info.server_to_local_ssh = obj
                .get("server_to_local_ssh")
                .and_then(|v| v.as_str())
                .map(str::to_string);
            self.health_info.server_to_workstation_ssh = obj
                .get("server_to_workstation_ssh")
                .and_then(|v| v.as_str())
                .map(str::to_string);
            self.health_info.workstation_ssh_details.clear();
            if let Some(details) = obj
                .get("workstation_ssh_details")
                .and_then(|v| v.as_object())
            {
                for (key, value) in details {
                    if let Some(status) = value.as_str() {
                        self.health_info
                            .workstation_ssh_details
                            .insert(key.clone(), status.to_string());
                    }
                }
            }
        }
        self.needs_redraw = true;
    }

    pub fn update_local_ipc_tunnel(&mut self, host: &str, connected: bool) {
        self.health_info.local_ipc_tunnel_direct = !is_local_endpoint(host);
        self.health_info.local_ipc_tunnel_ok = Some(connected);
        self.needs_redraw = true;
    }

    pub fn get_step_status(&self, config: &str, step: &str) -> &str {
        self.status_data
            .get(config)
            .and_then(|steps| steps.get(step))
            .map(|s| s.as_str())
            .unwrap_or(STATUS_WAITING)
    }

    pub fn step_cell_text(&self, config: &str, step: &str) -> String {
        let status = self.get_step_status(config, step);
        if self.should_show_solver_progress(config, step, status) {
            if let Some(progress) = self.engine_info.solver_progress.as_ref() {
                return format!(
                    "{} {}",
                    status_icon(status),
                    format_remaining_time(progress.remaining_sec)
                );
            }
        }
        format!("{} {}", status_icon(status), status)
    }

    pub fn step_cell_color(&self, config: &str, step: &str) -> ratatui::style::Color {
        let status = self.get_step_status(config, step);
        if self.should_show_solver_progress(config, step, status) {
            return self.theme.success;
        }
        status_color(status)
    }

    fn should_show_solver_progress(&self, config: &str, step: &str, status: &str) -> bool {
        if step != "solver" || status != STATUS_RUNNING {
            return false;
        }
        let Some(progress) = self.engine_info.solver_progress.as_ref() else {
            return false;
        };
        let iteration_is_valid = progress
            .current_iter
            .is_none_or(|current| current <= progress.total_iter);
        let timestamp_is_valid = progress.updated_at.is_none_or(f64::is_finite);
        progress.remaining_sec.is_finite()
            && progress.remaining_sec >= 0.0
            && iteration_is_valid
            && timestamp_is_valid
            && config.parse::<u64>().ok() == Some(progress.config_name)
    }

    #[cfg(test)]
    pub fn info_bar_text(&self) -> String {
        self.info_bar_parts()
            .into_iter()
            .map(|part| part.text)
            .collect()
    }

    pub fn info_bar_line(&self) -> Line<'static> {
        Line::from(
            self.info_bar_parts()
                .into_iter()
                .map(|part| {
                    let color = match part.color {
                        Some(InfoBarColor::Success) => self.theme.success,
                        Some(InfoBarColor::Error) => self.theme.error,
                        None => self.theme.gray_5,
                    };
                    let style = Style::default().fg(color);
                    Span::styled(part.text, style)
                })
                .collect::<Vec<_>>(),
        )
    }

    pub fn daemon_runtime_text(&self) -> String {
        let Some(started_at) = self.engine_info.daemon_started_at_display.as_deref() else {
            return String::new();
        };
        let uptime = self
            .engine_info
            .daemon_uptime_seconds
            .map(format_uptime)
            .unwrap_or_else(|| "--".to_string());
        format!("启动 {}  运行 {}", started_at, uptime)
    }

    pub fn settings_locked(&self) -> bool {
        self.engine_info.pipeline_started
    }

    pub fn mark_daemon_stopped(&mut self) {
        self.connected = false;
        self.engine_info = EngineInfo {
            engine_status: "stopped".to_string(),
            ..Default::default()
        };
        self.needs_redraw = true;
    }

    fn info_bar_parts(&self) -> Vec<InfoBarPart> {
        let ipc = ok_label(Some(self.connected));
        let local_worker = ok_label(self.health_info.local_worker_online);
        let local_server = if self.health_info.local_ipc_tunnel_direct {
            "直连"
        } else {
            ok_label(self.health_info.local_ipc_tunnel_ok)
        };
        let server_local = status_label(self.health_info.server_to_local_ssh.as_deref());
        let server_workstation =
            status_label(self.health_info.server_to_workstation_ssh.as_deref());

        let mut parts = vec![InfoBarPart::plain("  IPC:")];
        push_status_part(&mut parts, ipc);
        parts.push(InfoBarPart::plain(" │ LW:"));
        push_status_part(&mut parts, local_worker);
        parts.push(InfoBarPart::plain(" │ L→S:"));
        push_status_part(&mut parts, local_server);
        parts.push(InfoBarPart::plain(" │ S→L:"));
        push_status_part(&mut parts, server_local);
        parts.push(InfoBarPart::plain(" │ S→W:"));
        push_status_part(&mut parts, server_workstation);
        parts.push(InfoBarPart::plain(" │ 引擎:"));

        if !self.connected {
            parts.push(InfoBarPart::plain("未连接"));
            return parts;
        }

        let engine_status = engine_status_display(&self.engine_info.engine_status);
        parts.push(InfoBarPart::status(engine_status));
        parts.push(InfoBarPart::plain(format!(
            " │ 构型:{}",
            self.configs.len()
        )));
        parts.push(InfoBarPart::plain(" │ 屏障:"));
        if self.engine_info.workstation_barriers.is_empty() {
            let barrier = if self.engine_info.barrier_passed {
                "已通过"
            } else {
                "未通过"
            };
            parts.push(InfoBarPart::status(barrier));
        } else {
            for (idx, (workstation_id, passed)) in
                self.engine_info.workstation_barriers.iter().enumerate()
            {
                if idx > 0 {
                    parts.push(InfoBarPart::plain(" | "));
                }
                parts.push(InfoBarPart::workstation_barrier(
                    workstation_barrier_label(workstation_id),
                    *passed,
                ));
            }
        }
        parts
    }

    pub fn clamp_table_scroll(&mut self, visible_height: u16) {
        let total = self.configs.len() as u16;
        if total <= visible_height {
            self.table_scroll_offset = 0;
        } else {
            let max_scroll = total - visible_height;
            if self.table_scroll_offset > max_scroll {
                self.table_scroll_offset = max_scroll;
            }
        }
    }

    pub fn clamp_detail_scroll(&mut self, total_lines: u16, visible_height: u16) {
        if total_lines <= visible_height {
            self.detail_log_scroll = 0;
        } else {
            let max_scroll = total_lines.saturating_sub(visible_height);
            if self.detail_log_scroll > max_scroll {
                self.detail_log_scroll = max_scroll;
            }
        }
    }

    pub fn clamp_info_scroll(&mut self, total_lines: u16, visible_height: u16) {
        if total_lines <= visible_height {
            self.info_log_scroll = 0;
        } else {
            let max_scroll = total_lines.saturating_sub(visible_height);
            if self.info_log_scroll > max_scroll {
                self.info_log_scroll = max_scroll;
            }
        }
    }

    pub fn clamp_info_hscroll(&mut self, max_content_width: usize, visible_width: usize) {
        if max_content_width <= visible_width {
            self.info_log_hscroll = 0;
        } else {
            let max_scroll = (max_content_width - visible_width) as u16;
            if self.info_log_hscroll > max_scroll {
                self.info_log_hscroll = max_scroll;
            }
        }
    }

    pub fn clamp_detail_hscroll(&mut self, max_content_width: usize, visible_width: usize) {
        if max_content_width <= visible_width {
            self.detail_log_hscroll = 0;
        } else {
            let max_scroll = (max_content_width - visible_width) as u16;
            if self.detail_log_hscroll > max_scroll {
                self.detail_log_hscroll = max_scroll;
            }
        }
    }

    pub fn clamp_dialog_scroll(&mut self, total_lines: usize, visible_lines: usize) {
        if total_lines <= visible_lines {
            self.dialog_scroll = 0;
        } else {
            let max_scroll = (total_lines - visible_lines) as u16;
            if self.dialog_scroll > max_scroll {
                self.dialog_scroll = max_scroll;
            }
        }
    }

    pub fn open_settings(&mut self) {
        self.settings_state = Some(SettingsState::new());
        self.ui_mode = UiMode::Settings;
        self.needs_redraw = true;
    }

    pub fn close_settings(&mut self) {
        self.settings_state = None;
        self.ui_mode = UiMode::Normal;
        self.needs_redraw = true;
    }
}

struct InfoBarPart {
    text: String,
    color: Option<InfoBarColor>,
}

#[derive(Clone, Copy)]
enum InfoBarColor {
    Success,
    Error,
}

impl InfoBarPart {
    fn plain(text: impl Into<String>) -> Self {
        Self {
            text: text.into(),
            color: None,
        }
    }

    fn status(text: impl Into<String>) -> Self {
        let text = text.into();
        let color = match text.as_str() {
            "OK" | "运行中" | "已通过" => Some(InfoBarColor::Success),
            "断" | "已停止" | "未通过" => Some(InfoBarColor::Error),
            _ => None,
        };
        Self { text, color }
    }

    fn workstation_barrier(text: impl Into<String>, passed: bool) -> Self {
        Self {
            text: text.into(),
            color: Some(if passed {
                InfoBarColor::Success
            } else {
                InfoBarColor::Error
            }),
        }
    }
}

fn workstation_barrier_label(workstation_id: &str) -> String {
    workstation_id
        .strip_prefix("WS-")
        .unwrap_or(workstation_id)
        .to_string()
}

fn push_status_part(parts: &mut Vec<InfoBarPart>, label: &'static str) {
    parts.push(InfoBarPart::status(label));
}

fn ok_label(value: Option<bool>) -> &'static str {
    match value {
        Some(true) => "OK",
        Some(false) => "断",
        None => "未知",
    }
}

fn status_label(value: Option<&str>) -> &'static str {
    match value {
        Some("ok") => "OK",
        Some("disconnected") => "断",
        Some(status) if status.starts_with("error:") => "断",
        Some("unknown") | None => "未知",
        Some(_) => "未知",
    }
}

fn is_local_endpoint(host: &str) -> bool {
    matches!(host, "127.0.0.1" | "localhost" | "::1")
}

pub fn format_uptime(seconds: u64) -> String {
    if seconds < 60 {
        return format!("{seconds}s");
    }
    let hours = seconds / 3600;
    let minutes = (seconds % 3600) / 60;
    let secs = seconds % 60;
    if hours > 0 {
        format!("{hours}h{minutes:02}m{secs:02}s")
    } else {
        format!("{minutes}m{secs:02}s")
    }
}

pub fn format_remaining_time(seconds: f64) -> String {
    let safe_seconds = if seconds.is_finite() && seconds >= 0.0 {
        seconds.floor() as u64
    } else {
        0
    };
    let hours = safe_seconds / 3600;
    let minutes = (safe_seconds % 3600) / 60;
    let secs = safe_seconds % 60;
    format!("{hours:02}:{minutes:02}:{secs:02}")
}

fn parse_solver_progress(value: &serde_json::Value) -> Option<SolverProgress> {
    let obj = value.as_object()?;
    Some(SolverProgress {
        config_name: obj.get("config_name")?.as_u64()?,
        current_iter: obj.get("current_iter").and_then(|v| v.as_u64()),
        total_iter: obj.get("total_iter").and_then(|v| v.as_u64()).unwrap_or(0),
        remaining_sec: obj.get("remaining_sec")?.as_f64()?,
        updated_at: obj.get("updated_at").and_then(|v| v.as_f64()),
    })
}

fn expire_click<T>(
    click_time: &mut Option<std::time::Instant>,
    clicked: &mut Option<T>,
    timeout: Duration,
) -> bool {
    if let Some(ct) = *click_time {
        if clicked.is_some() && ct.elapsed() > timeout {
            *clicked = None;
            *click_time = None;
            return true;
        }
    }
    false
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn info_bar_text_includes_compact_health_statuses() {
        let mut state = AppState::default();
        state.connected = true;
        state.engine_info.engine_status = "running".to_string();
        state.engine_info.barrier_passed = true;
        state.configs = vec![1, 2, 3];
        state.health_info.local_worker_online = Some(true);
        state.health_info.local_ipc_tunnel_ok = Some(true);
        state.health_info.local_ipc_tunnel_direct = false;
        state.health_info.server_to_local_ssh = Some("ok".to_string());
        state.health_info.server_to_workstation_ssh = Some("disconnected".to_string());

        let text = state.info_bar_text();

        assert!(text.contains("IPC:OK"));
        assert!(text.contains("LW:OK"));
        assert!(text.contains("L→S:OK"));
        assert!(text.contains("S→L:OK"));
        assert!(text.contains("S→W:断"));
        assert!(text.contains("引擎:运行中"));
        assert!(text.contains("构型:3"));
        assert!(text.contains("屏障:已通过"));
    }

    #[test]
    fn info_bar_line_colors_status_values_only() {
        let mut state = AppState::default();
        state.connected = true;
        state.engine_info.engine_status = "running".to_string();
        state.engine_info.barrier_passed = false;
        state.health_info.local_worker_online = Some(true);
        state.health_info.local_ipc_tunnel_ok = Some(false);
        state.health_info.server_to_local_ssh = Some("unknown".to_string());
        state.health_info.server_to_workstation_ssh = Some("disconnected".to_string());

        let line = state.info_bar_line();

        assert_span_color(&line, "OK", Some(state.theme.success));
        assert_span_color(&line, "断", Some(state.theme.error));
        assert_span_color(&line, "运行中", Some(state.theme.success));
        assert_span_color(&line, "未通过", Some(state.theme.error));
        assert_span_color(&line, "未知", Some(state.theme.gray_5));
        assert_span_color(&line, " │ 引擎:", Some(state.theme.gray_5));
    }

    #[test]
    fn info_bar_text_shows_workstation_barrier_letters() {
        let mut state = AppState::default();
        state.connected = true;
        state.engine_info.engine_status = "running".to_string();
        state
            .engine_info
            .workstation_barriers
            .insert("WS-A".to_string(), true);
        state
            .engine_info
            .workstation_barriers
            .insert("WS-B".to_string(), false);
        state
            .engine_info
            .workstation_barriers
            .insert("WS-C".to_string(), false);

        assert!(state.info_bar_text().contains("屏障:A | B | C"));
    }

    #[test]
    fn info_bar_line_colors_workstation_barrier_letters() {
        let mut state = AppState::default();
        state.connected = true;
        state.engine_info.engine_status = "running".to_string();
        state
            .engine_info
            .workstation_barriers
            .insert("WS-A".to_string(), true);
        state
            .engine_info
            .workstation_barriers
            .insert("WS-B".to_string(), false);
        state
            .engine_info
            .workstation_barriers
            .insert("WS-C".to_string(), true);

        let line = state.info_bar_line();

        assert_span_color(&line, "A", Some(state.theme.success));
        assert_span_color(&line, "B", Some(state.theme.error));
        assert_span_color(&line, "C", Some(state.theme.success));
        assert_span_color(&line, " | ", Some(state.theme.gray_5));
    }

    #[test]
    fn info_bar_text_distinguishes_direct_ipc_endpoint() {
        let mut state = AppState::default();
        state.connected = true;
        state.update_local_ipc_tunnel("ocar", true);

        assert!(state.info_bar_text().contains("L→S:直连"));
    }

    #[test]
    fn info_bar_text_treats_local_ipc_endpoint_as_tunnel_status() {
        let mut state = AppState::default();
        state.connected = true;
        state.update_local_ipc_tunnel("127.0.0.1", true);

        assert!(state.info_bar_text().contains("L→S:OK"));
    }

    #[test]
    fn format_uptime_compacts_seconds_minutes_and_hours() {
        assert_eq!(format_uptime(8), "8s");
        assert_eq!(format_uptime(65), "1m05s");
        assert_eq!(format_uptime(3_725), "1h02m05s");
    }

    #[test]
    fn format_remaining_time_uses_hh_mm_ss() {
        assert_eq!(format_remaining_time(0.0), "00:00:00");
        assert_eq!(format_remaining_time(125.0), "00:02:05");
        assert_eq!(format_remaining_time(5_025.0), "01:23:45");
        assert_eq!(format_remaining_time(-1.0), "00:00:00");
        assert_eq!(format_remaining_time(f64::NAN), "00:00:00");
    }

    #[test]
    fn update_engine_info_parses_solver_progress() {
        let mut state = AppState::default();
        let data = serde_json::json!({
            "engine_status": "running",
            "solver_progress": {
                "config_name": 5,
                "current_iter": 350,
                "total_iter": 1000,
                "remaining_sec": 5025.0,
                "updated_at": 1717584000.123
            }
        });

        state.update_engine_info(&data);

        let progress = state
            .engine_info
            .solver_progress
            .as_ref()
            .expect("solver progress");
        assert_eq!(progress.config_name, 5);
        assert_eq!(progress.current_iter, Some(350));
        assert_eq!(progress.total_iter, 1000);
        assert_eq!(progress.remaining_sec, 5025.0);
        assert_eq!(progress.updated_at, Some(1717584000.123));
    }

    #[test]
    fn update_engine_info_parses_workstation_barriers() {
        let mut state = AppState::default();
        state.update_engine_info(&serde_json::json!({
            "workstation_barriers": {
                "WS-A": true,
                "WS-B": false,
                "WS-C": true,
                "ignored": "yes"
            }
        }));

        assert_eq!(
            state.engine_info.workstation_barriers.get("WS-A"),
            Some(&true)
        );
        assert_eq!(
            state.engine_info.workstation_barriers.get("WS-B"),
            Some(&false)
        );
        assert_eq!(
            state.engine_info.workstation_barriers.get("WS-C"),
            Some(&true)
        );
        assert!(!state
            .engine_info
            .workstation_barriers
            .contains_key("ignored"));
    }

    #[test]
    fn solver_running_cell_shows_remaining_time_for_matching_config() {
        let mut state = AppState::default();
        state.update_status_data(&serde_json::json!({
            "5": {"solver": "Running", "meshing": "Running"},
            "6": {"solver": "Running"}
        }));
        state.update_engine_info(&serde_json::json!({
            "solver_progress": {
                "config_name": 5,
                "total_iter": 1000,
                "remaining_sec": 5025.0
            }
        }));

        assert_eq!(state.step_cell_text("5", "solver"), "⏳ 01:23:45");
        assert_eq!(state.step_cell_color("5", "solver"), state.theme.success);
        assert_eq!(state.step_cell_text("5", "meshing"), "⏳ Running");
        assert_eq!(
            state.step_cell_color("5", "meshing"),
            status_color(STATUS_RUNNING)
        );
        assert_eq!(state.step_cell_text("6", "solver"), "⏳ Running");
    }

    #[test]
    fn work_location_cell_text_reflects_only_running_step_location() {
        let mut state = AppState::default();
        state.update_status_data(&serde_json::json!({
            "1": {"sw": STATUS_RUNNING},
            "2": {"sc": STATUS_RUNNING},
            "3": {"meshing": STATUS_RUNNING},
            "4": {"solver": STATUS_PAUSED},
            "5": {"postprocess": STATUS_COMPLETED},
            "6": {"transfer": STATUS_RUNNING},
            "7": {"solver": STATUS_RUNNING}
        }));
        state.update_config_workstations(&serde_json::json!({
            "3": "WS-B",
            "6": "default"
        }));

        assert_eq!(state.config_cell_text("3"), "3");
        assert_eq!(state.work_location_cell_text("1"), "本地");
        assert_eq!(state.work_location_cell_text("2"), "本地");
        assert_eq!(state.work_location_cell_text("3"), "WS-B");
        assert_eq!(state.work_location_cell_text("4"), "");
        assert_eq!(state.work_location_cell_text("5"), "");
        assert_eq!(state.work_location_cell_text("6"), "");
        assert_eq!(state.work_location_cell_text("7"), "");
    }

    #[test]
    fn step_names_include_postprocess() {
        assert_eq!(
            STEP_NAMES,
            ["sw", "sc", "transfer", "meshing", "solver", "postprocess"]
        );
        assert_eq!(step_display_name("postprocess"), "后处理");
    }

    #[test]
    fn daemon_runtime_text_keeps_started_date_and_time() {
        let mut state = AppState::default();
        state.engine_info.daemon_started_at_display = Some("2026-06-13 14:03:21".to_string());
        state.engine_info.daemon_uptime_seconds = Some(65);

        assert_eq!(
            state.daemon_runtime_text(),
            "启动 2026-06-13 14:03:21  运行 1m05s"
        );
    }

    #[test]
    fn mark_daemon_stopped_clears_settings_lock_runtime_fields() {
        let mut state = AppState::default();
        state.connected = true;
        state.engine_info.engine_status = "running".to_string();
        state.engine_info.sw_macro_started = true;
        state.engine_info.barrier_passed = true;
        state.engine_info.pipeline_started = true;
        state.engine_info.daemon_started_at = Some(1718000000.0);
        state.engine_info.daemon_started_at_display = Some("2026-06-13 14:03:21".to_string());
        state.engine_info.daemon_uptime_seconds = Some(65);
        state.engine_info.solver_progress = Some(SolverProgress {
            config_name: 1,
            current_iter: Some(1),
            total_iter: 10,
            remaining_sec: 9.0,
            updated_at: Some(1718000000.0),
        });

        state.mark_daemon_stopped();

        assert!(!state.connected);
        assert!(!state.settings_locked());
        assert_eq!(state.engine_info.engine_status, "stopped");
        assert!(!state.engine_info.sw_macro_started);
        assert!(!state.engine_info.barrier_passed);
        assert!(state.engine_info.daemon_started_at.is_none());
        assert!(state.engine_info.daemon_started_at_display.is_none());
        assert!(state.engine_info.daemon_uptime_seconds.is_none());
        assert!(state.engine_info.solver_progress.is_none());
        assert!(state.needs_redraw);
    }

    fn assert_span_color(line: &Line<'_>, text: &str, expected: Option<ratatui::style::Color>) {
        let span = line
            .spans
            .iter()
            .find(|span| span.content.as_ref() == text)
            .unwrap_or_else(|| panic!("missing span {text:?}"));
        assert_eq!(span.style.fg, expected);
    }
}
