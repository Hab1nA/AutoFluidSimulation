use std::collections::HashMap;

use crate::settings::SettingsState;

pub const STATUS_WAITING: &str = "Waiting";
pub const STATUS_RUNNING: &str = "Running";
pub const STATUS_PAUSED: &str = "Paused";
pub const STATUS_RETRYING: &str = "Retrying";
pub const STATUS_COMPLETED: &str = "Completed";
pub const STATUS_ERROR: &str = "Error";

pub const STEP_NAMES: [&str; 5] = ["SW", "SC", "Transfer", "Meshing", "Solver"];

pub const STEP_DISPLAY: [(&str, &str); 5] = [
    ("SW", "SolidWorks导出"),
    ("SC", "SpaceClaim转换"),
    ("Transfer", "文件传输"),
    ("Meshing", "网格划分"),
    ("Solver", "仿真求解"),
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
        STATUS_WAITING => "⏸️",
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
}

#[derive(Debug, Clone, Default)]
pub struct ScrollbarRenderedInfo {
    pub table_v: Option<(ratatui::layout::Rect, usize, usize, usize)>,
    pub info_v: Option<(ratatui::layout::Rect, usize, usize, usize)>,
    pub info_h: Option<(ratatui::layout::Rect, usize, usize, usize)>,
    pub detail_v: Option<(ratatui::layout::Rect, usize, usize, usize)>,
    pub detail_h: Option<(ratatui::layout::Rect, usize, usize, usize)>,
    pub dialog_v: Option<(ratatui::layout::Rect, usize, usize, usize)>,
}

#[derive(Debug, Clone, Default)]
pub struct AppState {
    pub connected: bool,
    pub status_data: HashMap<String, HashMap<String, String>>,
    pub configs: Vec<u64>,
    pub engine_info: EngineInfo,
    pub last_log_id: u64,
    pub log_filter_level: Option<String>,
    pub log_filter_source: Option<String>,
    pub ui_mode: UiMode,
    pub should_quit: bool,
    pub needs_redraw: bool,
    pub command_input: String,
    pub command_cursor: usize,
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
    pub last_info_generation: u64,
    pub terminal_size: ratatui::layout::Rect,
    pub pending_command: Option<String>,
    pub hovered_table_row: Option<u16>,
    pub hovered_detail_row: Option<u16>,
    pub hovered_button: Option<u8>,
    pub hovered_dialog_button: Option<u8>,
    pub clicked_button: Option<u8>,
    pub daemon_menu_open: bool,
    pub hovered_daemon_menu_item: Option<u8>,
    pub clicked_daemon_menu_item: Option<u8>,
    pub daemon_menu_click_time: Option<std::time::Instant>,
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
    ResetStep { config_name: String, step_name: Option<String> },
    CleanStep { step_name: String, config_name: Option<serde_json::Value> },
    FullQuit,
    StopDaemon,
}

impl AppState {
    pub fn new() -> Self {
        Self {
            detail_log_auto_scroll: true,
            info_log_auto_scroll: true,
            last_info_generation: 0,
            focus_zone: FocusZone::CommandInput,
            ..Default::default()
        }
    }

    pub fn update_terminal_size(&mut self, width: u16, height: u16) {
        self.terminal_size = ratatui::layout::Rect::new(0, 0, width, height);
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
        }
        self.needs_redraw = true;
    }

    pub fn get_step_status(&self, config: &str, step: &str) -> &str {
        self.status_data
            .get(config)
            .and_then(|steps| steps.get(step))
            .map(|s| s.as_str())
            .unwrap_or(STATUS_WAITING)
    }

    pub fn info_bar_text(&self) -> String {
        if !self.connected {
            return "  引擎: 未连接  │  请先启动 Daemon".to_string();
        }
        let engine_status = engine_status_display(&self.engine_info.engine_status);
        let barrier = if self.engine_info.barrier_passed { "已通过" } else { "未通过" };
        format!(
            "  引擎: {}  │  构型数: {}  │  屏障: {}",
            engine_status,
            self.configs.len(),
            barrier,
        )
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
