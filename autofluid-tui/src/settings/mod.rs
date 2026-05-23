pub mod config_io;
pub mod settings_ui;
pub mod validation;

use serde::{Deserialize, Serialize};

use crate::settings::validation::{validate_config, ValidationError};
use crate::text_buffer::TextBuffer;

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct LocalPaths {
    pub sw_exe: String,
    pub sw_model: String,
    pub excel: String,
    pub step_dir: String,
    pub sc_exe: String,
    pub scdoc_dir: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct RemoteConfig {
    pub host: String,
    pub port: u16,
    pub username: String,
    #[serde(skip_serializing, default)]
    pub password: String,
    pub working_dir: String,
    pub scripts_dir: String,
    pub ref_files_dir: String,
    pub scdoc_dir: String,
    pub msh_dir: String,
    pub result_dir: String,
    pub flag_dir: String,
    pub conda_env: String,
    pub conda_exe: String,
    pub mpi_bin_dir: String,
}

impl Default for RemoteConfig {
    fn default() -> Self {
        Self {
            host: "172.17.135.240".to_string(),
            port: 22,
            username: "ps".to_string(),
            password: String::new(),
            working_dir: String::new(),
            scripts_dir: String::new(),
            ref_files_dir: String::new(),
            scdoc_dir: String::new(),
            msh_dir: String::new(),
            result_dir: String::new(),
            flag_dir: String::new(),
            conda_env: String::new(),
            conda_exe: String::new(),
            mpi_bin_dir: String::new(),
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct StepFilePatterns {
    #[serde(rename = "SW")]
    pub sw: String,
    #[serde(rename = "SC")]
    pub sc: String,
    #[serde(rename = "Transfer")]
    pub transfer: Option<String>,
    #[serde(rename = "Meshing")]
    pub meshing: String,
    #[serde(rename = "Solver")]
    pub solver: String,
    #[serde(rename = "SolverData")]
    pub solver_dat: String,
}

impl Default for StepFilePatterns {
    fn default() -> Self {
        Self {
            sw: "model_gen4.SLDPRT_{config}.step".to_string(),
            sc: "model_gen4_{config}.scdoc".to_string(),
            transfer: None,
            meshing: "model_gen4_{config}.msh.h5".to_string(),
            solver: "model_gen4_{config}.cas.h5".to_string(),
            solver_dat: "model_gen4_{config}.dat.h5".to_string(),
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SolidWorksConfig {
    pub sw_macro_timeout: u64,
    pub sw_close_doc_on_finish: bool,
    pub sw_exit_on_finish: bool,
    pub sw_visible: bool,
    pub sw_startup: u64,
    pub sw_dispatch_startup_delay: u64,
    pub sw_exit_wait_seconds: u64,
}

impl Default for SolidWorksConfig {
    fn default() -> Self {
        Self {
            sw_macro_timeout: 3600,
            sw_close_doc_on_finish: true,
            sw_exit_on_finish: true,
            sw_visible: true,
            sw_startup: 60,
            sw_dispatch_startup_delay: 8,
            sw_exit_wait_seconds: 15,
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SpaceClaimConfig {
    pub sc_timeout: u64,
    pub sc_poll_interval: f64,
    pub sc_process_appear_timeout: u64,
    pub sc_gui_ready_timeout: u64,
    pub sc_gui_stable_delay: u64,
}

impl Default for SpaceClaimConfig {
    fn default() -> Self {
        Self {
            sc_timeout: 300,
            sc_poll_interval: 2.0,
            sc_process_appear_timeout: 120,
            sc_gui_ready_timeout: 30,
            sc_gui_stable_delay: 15,
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct GlobalSettings {
    pub watchdog_interval: f64,
    pub transfer_timeout: u64,
    pub meshing_timeout: u64,
    pub solver_timeout: u64,
    pub max_retries: u32,
    pub state_refresh_interval: f64,
    pub ssh_connection: u64,
    pub dir_recursion_limit: u32,
    pub ssh_upload_max_retries: u32,
}

impl Default for GlobalSettings {
    fn default() -> Self {
        Self {
            watchdog_interval: 1.0,
            transfer_timeout: 120,
            meshing_timeout: 600,
            solver_timeout: 7200,
            max_retries: 3,
            state_refresh_interval: 0.5,
            ssh_connection: 10,
            dir_recursion_limit: 32,
            ssh_upload_max_retries: 3,
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct SettingsConfig {
    #[serde(default)]
    pub local_paths: LocalPaths,
    #[serde(default)]
    pub remote_config: RemoteConfig,
    #[serde(default)]
    pub step_file_patterns: StepFilePatterns,
    #[serde(default)]
    pub solidworks: SolidWorksConfig,
    #[serde(default)]
    pub spaceclaim: SpaceClaimConfig,
    #[serde(default)]
    pub global_settings: GlobalSettings,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SettingCategory {
    LocalPaths,
    RemoteConnection,
    RemoteDirs,
    StepPatterns,
    SolidWorks,
    SpaceClaim,
    GlobalSettings,
}

impl SettingCategory {
    pub const ALL: [SettingCategory; 7] = [
        SettingCategory::LocalPaths,
        SettingCategory::RemoteConnection,
        SettingCategory::RemoteDirs,
        SettingCategory::StepPatterns,
        SettingCategory::SolidWorks,
        SettingCategory::SpaceClaim,
        SettingCategory::GlobalSettings,
    ];

    pub fn display_name(self) -> &'static str {
        match self {
            SettingCategory::LocalPaths => "本地文件路径",
            SettingCategory::RemoteConnection => "远程工作站连接",
            SettingCategory::RemoteDirs => "远程执行目录",
            SettingCategory::StepPatterns => "步骤文件模板",
            SettingCategory::SolidWorks => "SolidWorks",
            SettingCategory::SpaceClaim => "SpaceClaim",
            SettingCategory::GlobalSettings => "全局设置",
        }
    }

    pub fn field_count(self) -> usize {
        match self {
            SettingCategory::LocalPaths => 6,
            SettingCategory::RemoteConnection => 4,
            SettingCategory::RemoteDirs => 10,
            SettingCategory::StepPatterns => 5,
            SettingCategory::SolidWorks => 7,
            SettingCategory::SpaceClaim => 5,
            SettingCategory::GlobalSettings => 9,
        }
    }

    pub fn field_name(self, idx: usize) -> &'static str {
        match self {
            SettingCategory::LocalPaths => match idx {
                0 => "sw_exe", 1 => "sw_model", 2 => "excel", 3 => "step_dir",
                4 => "sc_exe", 5 => "scdoc_dir",
                _ => "",
            },
            SettingCategory::RemoteConnection => match idx {
                0 => "host", 1 => "port", 2 => "username", 3 => "password",
                _ => "",
            },
            SettingCategory::RemoteDirs => match idx {
                0 => "working_dir", 1 => "scripts_dir", 2 => "ref_files_dir", 3 => "scdoc_dir",
                4 => "msh_dir", 5 => "result_dir", 6 => "flag_dir",
                7 => "conda_env", 8 => "conda_exe", 9 => "mpi_bin_dir",
                _ => "",
            },
            SettingCategory::StepPatterns => match idx {
                0 => "SW", 1 => "SC", 2 => "Meshing", 3 => "Solver", 4 => "SolverData",
                _ => "",
            },
            SettingCategory::SolidWorks => match idx {
                0 => "sw_macro_timeout", 1 => "sw_close_doc_on_finish", 2 => "sw_exit_on_finish",
                3 => "sw_visible", 4 => "sw_startup", 5 => "sw_dispatch_startup_delay",
                6 => "sw_exit_wait_seconds",
                _ => "",
            },
            SettingCategory::SpaceClaim => match idx {
                0 => "sc_timeout", 1 => "sc_poll_interval", 2 => "sc_process_appear_timeout",
                3 => "sc_gui_ready_timeout", 4 => "sc_gui_stable_delay",
                _ => "",
            },
            SettingCategory::GlobalSettings => match idx {
                0 => "watchdog_interval", 1 => "transfer_timeout", 2 => "meshing_timeout",
                3 => "solver_timeout", 4 => "max_retries", 5 => "state_refresh_interval",
                6 => "ssh_connection", 7 => "dir_recursion_limit", 8 => "ssh_upload_max_retries",
                _ => "",
            },
        }
    }

    pub fn display_label(self, idx: usize) -> &'static str {
        match self {
            SettingCategory::LocalPaths => match idx {
                0 => "SW可执行文件", 1 => "SW模型文件", 2 => "Excel参数表", 3 => "STEP输出目录",
                4 => "SC可执行文件", 5 => "SCDOC输出目录",
                _ => "",
            },
            SettingCategory::RemoteConnection => match idx {
                0 => "主机地址", 1 => "SSH端口", 2 => "用户名", 3 => "密码",
                _ => "",
            },
            SettingCategory::RemoteDirs => match idx {
                0 => "仿真工作目录", 1 => "脚本部署目录", 2 => "引用文件目录", 3 => "SCDOC接收目录",
                4 => "网格输出目录", 5 => "仿真输出目录", 6 => "仿真标志目录",
                7 => "Conda环境名", 8 => "Conda可执行文件", 9 => "MPI安装目录",
                _ => "",
            },
            SettingCategory::StepPatterns => match idx {
                0 => "SW步骤模板", 1 => "SC步骤模板", 2 => "Meshing模板", 3 => "Solver模板", 4 => "Solver数据模板",
                _ => "",
            },
            SettingCategory::SolidWorks => match idx {
                0 => "宏超时(秒)", 1 => "完成后关闭文档", 2 => "完成后退出SW",
                3 => "显示窗口", 4 => "启动超时(秒)", 5 => "调度启动延迟(秒)",
                6 => "退出等待(秒)",
                _ => "",
            },
            SettingCategory::SpaceClaim => match idx {
                0 => "脚本超时(秒)", 1 => "轮询间隔(秒)", 2 => "进程出现等待(秒)",
                3 => "窗口就绪超时(秒)", 4 => "窗口稳定等待(秒)",
                _ => "",
            },
            SettingCategory::GlobalSettings => match idx {
                0 => "看门狗间隔(秒)", 1 => "传输超时(秒)", 2 => "网格超时(秒)",
                3 => "求解超时(秒)", 4 => "最大重试", 5 => "状态刷新间隔(秒)",
                6 => "SSH连接超时(秒)", 7 => "目录递归深度限制", 8 => "SSH上传最大重试",
                _ => "",
            },
        }
    }

    pub fn is_bool_field(self, idx: usize) -> bool {
        matches!(self, SettingCategory::SolidWorks) && matches!(idx, 1..=3)
    }

    pub fn is_password_field(self, idx: usize) -> bool {
        matches!(self, SettingCategory::RemoteConnection) && idx == 3
    }

    /// 返回字段的完整配置路径名（如 "local_paths.sw_exe"）
    pub fn field_full_name(self, idx: usize) -> String {
        match self {
            SettingCategory::LocalPaths => format!("local_paths.{}", self.field_name(idx)),
            SettingCategory::RemoteConnection | SettingCategory::RemoteDirs => {
                format!("remote_config.{}", self.field_name(idx))
            }
            SettingCategory::StepPatterns => format!("step_file_patterns.{}", self.field_name(idx)),
            SettingCategory::SolidWorks => format!("solidworks.{}", self.field_name(idx)),
            SettingCategory::SpaceClaim => format!("spaceclaim.{}", self.field_name(idx)),
            SettingCategory::GlobalSettings => format!("global_settings.{}", self.field_name(idx)),
        }
    }

    /// 判断字段是否为本地文件/目录路径（需要存在性检查）
    pub fn is_path_field(self, _idx: usize) -> bool {
        match self {
            SettingCategory::LocalPaths => true,
            // RemoteDirs 中的所有路径都指向远程工作站，不做本地存在性检查
            _ => false,
        }
    }
}

#[derive(Debug, Clone)]
pub struct UndoEntry {
    pub category: SettingCategory,
    pub field_index: usize,
    pub old_value: String,
}

#[derive(Debug, Clone, Default)]
pub struct SettingsFocus {
    pub category_index: usize,
    pub field_index: usize,
    pub editing: bool,
}

#[derive(Debug, Clone)]
pub struct SettingsState {
    pub config: SettingsConfig,
    pub focus: SettingsFocus,
    pub scroll: u16,
    pub dirty: bool,
    pub validation_errors: Vec<ValidationError>,
    /// 通用文本编辑缓冲区（光标、选区、剪贴板）。
    pub buffer: TextBuffer,
    pub undo_stack: Vec<UndoEntry>,
    pub saved: bool,
    pub save_error: Option<String>,
    /// Mouse hover tracking: (category_index, field_index)
    pub hovered_field: Option<(usize, usize)>,
    /// Click animation highlight (short-lived, ~20ms)
    pub clicked_field: Option<(usize, usize)>,
    pub field_click_time: Option<std::time::Instant>,
    /// Double-click tracking (longer window, ~400ms)
    pub last_clicked_field: Option<(usize, usize)>,
    pub last_click_time: Option<std::time::Instant>,
    /// Field positions computed during rendering: (cat_idx, fi, y)
    pub field_positions: Vec<(usize, usize, u16)>,
    /// Path existence cache: field_name -> exists
    pub path_status: std::collections::HashMap<String, bool>,
}

impl SettingsState {
    pub fn new() -> Self {
        let mut config = config_io::load_config().unwrap_or_default();
        // 密码始终从 .env 文件读取（TOML 中不存储密码）
        let env_pwd = config_io::read_env_password();
        if !env_pwd.is_empty() {
            config.remote_config.password = env_pwd;
        }
        Self {
            config,
            focus: SettingsFocus::default(),
            scroll: 0,
            dirty: false,
            validation_errors: Vec::new(),
            buffer: TextBuffer::new(),
            undo_stack: Vec::new(),
            saved: false,
            save_error: None,
            hovered_field: None,
            clicked_field: None,
            field_click_time: None,
            last_clicked_field: None,
            last_click_time: None,
            field_positions: Vec::new(),
            path_status: std::collections::HashMap::new(),
        }
    }

    pub fn current_category(&self) -> SettingCategory {
        SettingCategory::ALL.get(self.focus.category_index).copied().unwrap_or(SettingCategory::LocalPaths)
    }

    pub fn is_editing_field(&self) -> bool {
        self.focus.editing
    }

    pub fn get_field_value(&self, category: SettingCategory, idx: usize) -> String {
        match category {
            SettingCategory::LocalPaths => match idx {
                0 => self.config.local_paths.sw_exe.clone(),
                1 => self.config.local_paths.sw_model.clone(),
                2 => self.config.local_paths.excel.clone(),
                3 => self.config.local_paths.step_dir.clone(),
                4 => self.config.local_paths.sc_exe.clone(),
                5 => self.config.local_paths.scdoc_dir.clone(),
                _ => String::new(),
            },
            SettingCategory::RemoteConnection => match idx {
                0 => self.config.remote_config.host.clone(),
                1 => self.config.remote_config.port.to_string(),
                2 => self.config.remote_config.username.clone(),
                3 => self.config.remote_config.password.clone(),
                _ => String::new(),
            },
            SettingCategory::RemoteDirs => match idx {
                0 => self.config.remote_config.working_dir.clone(),
                1 => self.config.remote_config.scripts_dir.clone(),
                2 => self.config.remote_config.ref_files_dir.clone(),
                3 => self.config.remote_config.scdoc_dir.clone(),
                4 => self.config.remote_config.msh_dir.clone(),
                5 => self.config.remote_config.result_dir.clone(),
                6 => self.config.remote_config.flag_dir.clone(),
                7 => self.config.remote_config.conda_env.clone(),
                8 => self.config.remote_config.conda_exe.clone(),
                9 => self.config.remote_config.mpi_bin_dir.clone(),
                _ => String::new(),
            },
            SettingCategory::StepPatterns => match idx {
                0 => self.config.step_file_patterns.sw.clone(),
                1 => self.config.step_file_patterns.sc.clone(),
                2 => self.config.step_file_patterns.meshing.clone(),
                3 => self.config.step_file_patterns.solver.clone(),
                4 => self.config.step_file_patterns.solver_dat.clone(),
                _ => String::new(),
            },
            SettingCategory::SolidWorks => match idx {
                0 => self.config.solidworks.sw_macro_timeout.to_string(),
                1 => self.config.solidworks.sw_close_doc_on_finish.to_string(),
                2 => self.config.solidworks.sw_exit_on_finish.to_string(),
                3 => self.config.solidworks.sw_visible.to_string(),
                4 => self.config.solidworks.sw_startup.to_string(),
                5 => self.config.solidworks.sw_dispatch_startup_delay.to_string(),
                6 => self.config.solidworks.sw_exit_wait_seconds.to_string(),
                _ => String::new(),
            },
            SettingCategory::SpaceClaim => match idx {
                0 => self.config.spaceclaim.sc_timeout.to_string(),
                1 => self.config.spaceclaim.sc_poll_interval.to_string(),
                2 => self.config.spaceclaim.sc_process_appear_timeout.to_string(),
                3 => self.config.spaceclaim.sc_gui_ready_timeout.to_string(),
                4 => self.config.spaceclaim.sc_gui_stable_delay.to_string(),
                _ => String::new(),
            },
            SettingCategory::GlobalSettings => match idx {
                0 => self.config.global_settings.watchdog_interval.to_string(),
                1 => self.config.global_settings.transfer_timeout.to_string(),
                2 => self.config.global_settings.meshing_timeout.to_string(),
                3 => self.config.global_settings.solver_timeout.to_string(),
                4 => self.config.global_settings.max_retries.to_string(),
                5 => self.config.global_settings.state_refresh_interval.to_string(),
                6 => self.config.global_settings.ssh_connection.to_string(),
                7 => self.config.global_settings.dir_recursion_limit.to_string(),
                8 => self.config.global_settings.ssh_upload_max_retries.to_string(),
                _ => String::new(),
            },
        }
    }

    pub fn set_field_value(&mut self, category: SettingCategory, idx: usize, value: &str) {
        match category {
            SettingCategory::LocalPaths => match idx {
                0 => self.config.local_paths.sw_exe = value.to_string(),
                1 => self.config.local_paths.sw_model = value.to_string(),
                2 => self.config.local_paths.excel = value.to_string(),
                3 => self.config.local_paths.step_dir = value.to_string(),
                4 => self.config.local_paths.sc_exe = value.to_string(),
                5 => self.config.local_paths.scdoc_dir = value.to_string(),
                _ => {}
            },
            SettingCategory::RemoteConnection => match idx {
                0 => self.config.remote_config.host = value.to_string(),
                1 => {
                    if let Ok(p) = value.parse::<u16>() {
                        self.config.remote_config.port = p;
                    }
                }
                2 => self.config.remote_config.username = value.to_string(),
                3 => self.config.remote_config.password = value.to_string(),
                _ => {}
            },
            SettingCategory::RemoteDirs => match idx {
                0 => self.config.remote_config.working_dir = value.to_string(),
                1 => self.config.remote_config.scripts_dir = value.to_string(),
                2 => self.config.remote_config.ref_files_dir = value.to_string(),
                3 => self.config.remote_config.scdoc_dir = value.to_string(),
                4 => self.config.remote_config.msh_dir = value.to_string(),
                5 => self.config.remote_config.result_dir = value.to_string(),
                6 => self.config.remote_config.flag_dir = value.to_string(),
                7 => self.config.remote_config.conda_env = value.to_string(),
                8 => self.config.remote_config.conda_exe = value.to_string(),
                9 => self.config.remote_config.mpi_bin_dir = value.to_string(),
                _ => {}
            },
            SettingCategory::StepPatterns => match idx {
                0 => self.config.step_file_patterns.sw = value.to_string(),
                1 => self.config.step_file_patterns.sc = value.to_string(),
                2 => self.config.step_file_patterns.meshing = value.to_string(),
                3 => self.config.step_file_patterns.solver = value.to_string(),
                4 => self.config.step_file_patterns.solver_dat = value.to_string(),
                _ => {}
            },
            SettingCategory::SolidWorks => match idx {
                0 => if let Ok(v) = value.parse::<u64>() { self.config.solidworks.sw_macro_timeout = v; }
                1 => self.config.solidworks.sw_close_doc_on_finish = value == "true" || value == "是",
                2 => self.config.solidworks.sw_exit_on_finish = value == "true" || value == "是",
                3 => self.config.solidworks.sw_visible = value == "true" || value == "是",
                4 => if let Ok(v) = value.parse::<u64>() { self.config.solidworks.sw_startup = v; }
                5 => if let Ok(v) = value.parse::<u64>() { self.config.solidworks.sw_dispatch_startup_delay = v; }
                6 => if let Ok(v) = value.parse::<u64>() { self.config.solidworks.sw_exit_wait_seconds = v; }
                _ => {}
            },
            SettingCategory::SpaceClaim => match idx {
                0 => if let Ok(v) = value.parse::<u64>() { self.config.spaceclaim.sc_timeout = v; }
                1 => if let Ok(v) = value.parse::<f64>() { self.config.spaceclaim.sc_poll_interval = v; }
                2 => if let Ok(v) = value.parse::<u64>() { self.config.spaceclaim.sc_process_appear_timeout = v; }
                3 => if let Ok(v) = value.parse::<u64>() { self.config.spaceclaim.sc_gui_ready_timeout = v; }
                4 => if let Ok(v) = value.parse::<u64>() { self.config.spaceclaim.sc_gui_stable_delay = v; }
                _ => {}
            },
            SettingCategory::GlobalSettings => match idx {
                0 => if let Ok(v) = value.parse::<f64>() { self.config.global_settings.watchdog_interval = v; }
                1 => if let Ok(v) = value.parse::<u64>() { self.config.global_settings.transfer_timeout = v; }
                2 => if let Ok(v) = value.parse::<u64>() { self.config.global_settings.meshing_timeout = v; }
                3 => if let Ok(v) = value.parse::<u64>() { self.config.global_settings.solver_timeout = v; }
                4 => if let Ok(v) = value.parse::<u32>() { self.config.global_settings.max_retries = v; }
                5 => if let Ok(v) = value.parse::<f64>() { self.config.global_settings.state_refresh_interval = v; }
                6 => if let Ok(v) = value.parse::<u64>() { self.config.global_settings.ssh_connection = v; }
                7 => if let Ok(v) = value.parse::<u32>() { self.config.global_settings.dir_recursion_limit = v; }
                8 => if let Ok(v) = value.parse::<u32>() { self.config.global_settings.ssh_upload_max_retries = v; }
                _ => {}
            },
        }
        self.dirty = true;
    }

    pub fn move_focus_up(&mut self) {
        if self.focus.field_index > 0 {
            self.focus.field_index -= 1;
        } else if self.focus.category_index > 0 {
            self.focus.category_index -= 1;
            self.focus.field_index = self.current_category().field_count().saturating_sub(1);
        }
    }

    pub fn move_focus_down(&mut self) {
        let cat = self.current_category();
        if self.focus.field_index + 1 < cat.field_count() {
            self.focus.field_index += 1;
        } else if self.focus.category_index + 1 < SettingCategory::ALL.len() {
            self.focus.category_index += 1;
            self.focus.field_index = 0;
        }
    }

    pub fn move_focus_next(&mut self) {
        let cat = self.current_category();
        if self.focus.field_index + 1 < cat.field_count() {
            self.focus.field_index += 1;
        } else if self.focus.category_index + 1 < SettingCategory::ALL.len() {
            self.focus.category_index += 1;
            self.focus.field_index = 0;
        } else {
            self.focus.category_index = 0;
            self.focus.field_index = 0;
        }
    }

    pub fn move_focus_prev(&mut self) {
        if self.focus.field_index > 0 {
            self.focus.field_index -= 1;
        } else if self.focus.category_index > 0 {
            self.focus.category_index -= 1;
            self.focus.field_index = self.current_category().field_count().saturating_sub(1);
        } else {
            let last_idx = SettingCategory::ALL.len() - 1;
            self.focus.category_index = last_idx;
            self.focus.field_index = SettingCategory::ALL[last_idx].field_count().saturating_sub(1);
        }
    }

    pub fn begin_edit_current_field(&mut self) {
        let value = self.get_field_value(self.current_category(), self.focus.field_index);
        self.buffer = TextBuffer::with_text(value);
        self.focus.editing = true;
    }

    pub fn cancel_edit_current_field(&mut self) {
        self.buffer = TextBuffer::new();
        self.focus.editing = false;
    }

    pub fn commit_edit_current_field(&mut self) {
        let cat = self.current_category();
        let idx = self.focus.field_index;
        let old_value = self.get_field_value(cat, idx);
        let new_value = self.buffer.text.clone();

        if old_value != new_value {
            self.undo_stack.push(UndoEntry {
                category: cat,
                field_index: idx,
                old_value,
            });
            if self.undo_stack.len() > 200 {
                self.undo_stack.remove(0);
            }
            self.set_field_value(cat, idx, &new_value);
        }

        // 路径字段提交后立即检查文件/目录是否存在
        if cat.is_path_field(idx) {
            let value = self.get_field_value(cat, idx);
            let field_name = cat.field_full_name(idx);
            if !value.is_empty() {
                self.path_status.insert(field_name, std::path::Path::new(&value).exists());
            } else {
                self.path_status.remove(&field_name);
            }
        }

        // 重新运行校验，刷新 validation_errors（修正路径后错误自动消失）
        self.validation_errors = validate_config(&self.config);

        self.buffer = TextBuffer::new();
        self.focus.editing = false;
    }

    pub fn undo(&mut self) {
        if let Some(entry) = self.undo_stack.pop() {
            let current = self.get_field_value(entry.category, entry.field_index);
            self.set_field_value(entry.category, entry.field_index, &entry.old_value);
            // Push current value back for redo... but simple undo is enough
            let _ = current;
            // Re-push the entry pointing to the current value (for redo)
            // Actually just pop and don't re-push - simple linear undo
        }
    }

    pub fn input_char(&mut self, c: char) {
        self.buffer.input_char(c);
    }

    pub fn input_backspace(&mut self) {
        self.buffer.input_backspace();
    }

    pub fn input_delete(&mut self) {
        self.buffer.input_delete();
    }

    pub fn move_cursor_left(&mut self) {
        self.buffer.move_cursor_left();
    }

    pub fn move_cursor_right(&mut self) {
        self.buffer.move_cursor_right();
    }

    pub fn move_cursor_home(&mut self) {
        self.buffer.move_cursor_home();
    }

    pub fn move_cursor_end(&mut self) {
        self.buffer.move_cursor_end();
    }

    pub fn toggle_boolean(&mut self) {
        let cat = self.current_category();
        let idx = self.focus.field_index;
        let old = self.get_field_value(cat, idx);
        let new = if old == "true" || old == "是" { "false" } else { "true" };
        self.undo_stack.push(UndoEntry { category: cat, field_index: idx, old_value: old });
        self.set_field_value(cat, idx, new);
    }

    pub fn save(&self) -> Result<(), Vec<ValidationError>> {
        let errors = validate_config(&self.config);
        if errors.iter().any(|e| matches!(e.severity, crate::settings::validation::Severity::Error)) {
            return Err(errors);
        }
        config_io::save_config(&self.config).map_err(|e| {
            vec![ValidationError {
                field_name: "save".to_string(),
                message: e,
                severity: crate::settings::validation::Severity::Error,
            }]
        })?;
        // Write password to .env (always write to allow clearing)
        let _ = config_io::write_env_password(&self.config.remote_config.password);
        Ok(())
    }

    // ── Selection helpers (delegated to TextBuffer) ─────────────────

    pub fn select_all(&mut self) {
        self.buffer.select_all();
    }

    pub fn copy_selection(&self) -> bool {
        self.buffer.copy_selection()
    }

    pub fn cut_selection(&mut self) -> bool {
        self.buffer.cut_selection()
    }

    pub fn paste_from_clipboard(&mut self) -> bool {
        self.buffer.paste_from_clipboard()
    }
}
