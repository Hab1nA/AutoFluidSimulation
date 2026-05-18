pub mod config_io;
pub mod settings_ui;
pub mod validation;

use serde::{Deserialize, Serialize};

use crate::settings::validation::{validate_config, ValidationError};
use crate::utils::char_to_byte_index;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct LocalPaths {
    pub sw_exe: String,
    pub sw_model: String,
    pub excel: String,
    pub step_dir: String,
    pub sc_exe: String,
    pub sc_script: String,
    pub sc_bridge: String,
    pub scdoc_dir: String,
    pub log_dir: String,
    pub data_dir: String,
}

impl Default for LocalPaths {
    fn default() -> Self {
        Self {
            sw_exe: r"C:\Program Files\SOLIDWORKS Corp\SOLIDWORKS\SLDWORKS.exe".to_string(),
            sw_model: r"C:\Users\XKZ\Documents\000ansys_data\Graduation_Project(RE0.)\solidworks_models\model_gen4.SLDPRT".to_string(),
            excel: r"C:\Users\XKZ\Documents\000ansys_data\Graduation_Project(RE0.)\solidworks_models\model_gen4.xlsx".to_string(),
            step_dir: r"C:\Users\XKZ\Documents\000ansys_data\Graduation_Project(RE0.)\solidworks_models\step".to_string(),
            sc_exe: r"C:\Program Files\ANSYS Inc\v231\SCDM\SpaceClaim.exe".to_string(),
            sc_script: String::new(), // computed at runtime relative to project dir
            sc_bridge: String::new(),  // computed at runtime relative to project dir
            scdoc_dir: r"C:\Users\XKZ\Documents\000ansys_data\Graduation_Project(RE0.)\solidworks_models\scdoc".to_string(),
            log_dir: String::new(),   // computed at runtime
            data_dir: String::new(),  // computed at runtime
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct RemoteConfig {
    pub host: String,
    pub port: u16,
    pub username: String,
    pub password: String,
    pub root_dir: String,
    pub scdoc_dir: String,
    pub msh_dir: String,
    pub result_dir: String,
    pub conda_env: String,
    pub conda_exe: String,
    pub meshing_script: String,
    pub solver_script: String,
    pub flag_dir: String,
}

impl Default for RemoteConfig {
    fn default() -> Self {
        Self {
            host: "172.17.135.240".to_string(),
            port: 22,
            username: "ps".to_string(),
            password: String::new(),
            root_dir: r"D:\xkz_1020".to_string(),
            scdoc_dir: r"D:\xkz_1020\scdoc".to_string(),
            msh_dir: r"D:\xkz_1020\msh".to_string(),
            result_dir: r"D:\xkz_1020\case".to_string(),
            conda_env: "pyfluent".to_string(),
            conda_exe: r"C:\ProgramData\anaconda3\Scripts\conda.exe".to_string(),
            meshing_script: r"D:\xkz_1020\batch_meshing_gen4.py".to_string(),
            solver_script: r"D:\xkz_1020\batch_solver_gen4.py".to_string(),
            flag_dir: r"D:\xkz_1020\flags".to_string(),
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
}

impl Default for StepFilePatterns {
    fn default() -> Self {
        Self {
            sw: "model_gen4.SLDPRT_{config}.step".to_string(),
            sc: "model_gen4_{config}.scdoc".to_string(),
            transfer: None,
            meshing: "model_gen4_{config}.msh.h5".to_string(),
            solver: "model_gen4_{config}.cas.h5".to_string(),
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct EngineConfig {
    pub watchdog_interval: f64,
    pub sw_macro_timeout: u64,
    pub sw_close_doc_on_finish: bool,
    pub sw_exit_on_finish: bool,
    pub sw_visible: bool,
    pub sc_timeout: u64,
    pub transfer_timeout: u64,
    pub meshing_timeout: u64,
    pub solver_timeout: u64,
    pub max_retries: u32,
    pub state_refresh_interval: f64,
}

impl Default for EngineConfig {
    fn default() -> Self {
        Self {
            watchdog_interval: 1.0,
            sw_macro_timeout: 3600,
            sw_close_doc_on_finish: true,
            sw_exit_on_finish: true,
            sw_visible: true,
            sc_timeout: 300,
            transfer_timeout: 120,
            meshing_timeout: 600,
            solver_timeout: 7200,
            max_retries: 3,
            state_refresh_interval: 0.5,
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct OperationTimeouts {
    pub sw_startup: u64,
    pub sw_dispatch_startup_delay: u64,
    pub sw_exit_wait_seconds: u64,
    pub sc_poll_interval: f64,
    pub ssh_connection: u64,
    pub dir_recursion_limit: u32,
    pub ssh_upload_max_retries: u32,
}

impl Default for OperationTimeouts {
    fn default() -> Self {
        Self {
            sw_startup: 60,
            sw_dispatch_startup_delay: 8,
            sw_exit_wait_seconds: 15,
            sc_poll_interval: 2.0,
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
    pub engine_config: EngineConfig,
    #[serde(default)]
    pub operation_timeouts: OperationTimeouts,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SettingCategory {
    LocalPaths,
    RemoteConnection,
    RemoteDirs,
    StepPatterns,
    EngineConfig,
    OperationTimeouts,
}

impl SettingCategory {
    pub const ALL: [SettingCategory; 6] = [
        SettingCategory::LocalPaths,
        SettingCategory::RemoteConnection,
        SettingCategory::RemoteDirs,
        SettingCategory::StepPatterns,
        SettingCategory::EngineConfig,
        SettingCategory::OperationTimeouts,
    ];

    pub fn display_name(self) -> &'static str {
        match self {
            SettingCategory::LocalPaths => "本地文件路径",
            SettingCategory::RemoteConnection => "远程工作站连接",
            SettingCategory::RemoteDirs => "远程执行目录",
            SettingCategory::StepPatterns => "步骤文件模板",
            SettingCategory::EngineConfig => "引擎配置",
            SettingCategory::OperationTimeouts => "操作超时参数",
        }
    }

    pub fn field_count(self) -> usize {
        match self {
            SettingCategory::LocalPaths => 10,
            SettingCategory::RemoteConnection => 4,
            SettingCategory::RemoteDirs => 9,
            SettingCategory::StepPatterns => 4,
            SettingCategory::EngineConfig => 12,
            SettingCategory::OperationTimeouts => 7,
        }
    }

    pub fn field_name(self, idx: usize) -> &'static str {
        match self {
            SettingCategory::LocalPaths => match idx {
                0 => "sw_exe", 1 => "sw_model", 2 => "excel", 3 => "step_dir",
                4 => "sc_exe", 5 => "sc_script", 6 => "sc_bridge", 7 => "scdoc_dir", 8 => "log_dir", 9 => "data_dir",
                _ => "",
            },
            SettingCategory::RemoteConnection => match idx {
                0 => "host", 1 => "port", 2 => "username", 3 => "password",
                _ => "",
            },
            SettingCategory::RemoteDirs => match idx {
                0 => "root_dir", 1 => "scdoc_dir", 2 => "msh_dir", 3 => "result_dir",
                4 => "conda_env", 5 => "conda_exe", 6 => "meshing_script", 7 => "solver_script", 8 => "flag_dir",
                _ => "",
            },
            SettingCategory::StepPatterns => match idx {
                0 => "SW", 1 => "SC", 2 => "Meshing", 3 => "Solver",
                _ => "",
            },
            SettingCategory::EngineConfig => match idx {
                0 => "watchdog_interval", 1 => "sw_macro_timeout", 2 => "sw_close_doc_on_finish",
                3 => "sw_exit_on_finish", 4 => "sw_visible",
                5 => "sc_timeout", 6 => "transfer_timeout", 7 => "meshing_timeout",
                8 => "solver_timeout", 9 => "max_retries", 10 => "state_refresh_interval",
                _ => "",
            },
            SettingCategory::OperationTimeouts => match idx {
                0 => "sw_startup", 1 => "sw_dispatch_startup_delay", 2 => "sw_exit_wait_seconds",
                3 => "sc_poll_interval", 4 => "ssh_connection", 5 => "dir_recursion_limit", 6 => "ssh_upload_max_retries",
                _ => "",
            },
        }
    }

    pub fn display_label(self, idx: usize) -> &'static str {
        match self {
            SettingCategory::LocalPaths => match idx {
                0 => "SW可执行文件", 1 => "SW模型文件", 2 => "Excel参数表", 3 => "STEP输出目录",
                4 => "SC可执行文件", 5 => "SC脚本文件", 6 => "SC桥接程序", 7 => "SCDOC输出目录", 8 => "日志目录", 9 => "数据目录",
                _ => "",
            },
            SettingCategory::RemoteConnection => match idx {
                0 => "主机地址", 1 => "SSH端口", 2 => "用户名", 3 => "密码",
                _ => "",
            },
            SettingCategory::RemoteDirs => match idx {
                0 => "工程根目录", 1 => "SCDOC接收目录", 2 => "网格输出目录", 3 => "结果输出目录",
                4 => "Conda环境名", 5 => "Conda路径", 6 => "网格脚本", 7 => "求解脚本", 8 => "标志文件目录",
                _ => "",
            },
            SettingCategory::StepPatterns => match idx {
                0 => "SW步骤模板", 1 => "SC步骤模板", 2 => "Meshing模板", 3 => "Solver模板",
                _ => "",
            },
            SettingCategory::EngineConfig => match idx {
                0 => "看门狗间隔(秒)", 1 => "SW宏超时(秒)", 2 => "SW关闭文档", 3 => "SW退出",
                4 => "SW显示窗口", 5 => "SC超时(秒)", 6 => "传输超时(秒)",
                7 => "网格超时(秒)", 8 => "求解超时(秒)", 9 => "最大重试", 10 => "状态刷新间隔(秒)",
                _ => "",
            },
            SettingCategory::OperationTimeouts => match idx {
                0 => "SW启动超时(秒)", 1 => "SW调度启动延迟(秒)", 2 => "SW退出等待(秒)",
                3 => "SC轮询间隔(秒)", 4 => "SSH连接超时(秒)", 5 => "目录递归深度限制", 6 => "SSH上传最大重试",
                _ => "",
            },
        }
    }

    pub fn is_bool_field(self, idx: usize) -> bool {
        matches!(self, SettingCategory::EngineConfig) && matches!(idx, 2..=4)
    }

    pub fn is_password_field(self, idx: usize) -> bool {
        matches!(self, SettingCategory::RemoteConnection) && idx == 3
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
    pub edit_buffer: String,
    pub edit_cursor: usize,
    /// Selection anchor for Shift+arrow / Ctrl+A. When Some, selection spans
    /// from `selection_anchor` (inclusive) to `edit_cursor` (exclusive if
    /// cursor > anchor, inclusive otherwise). The two may be in either order.
    pub selection_anchor: Option<usize>,
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
        // 从 .env 文件加载密码（TOML 中密码可能为空）
        if config.remote_config.password.is_empty() {
            let env_pwd = config_io::read_env_password();
            if !env_pwd.is_empty() {
                config.remote_config.password = env_pwd;
            }
        }
        // 路径在运行时计算相对于项目根目录的默认值
        let project_dir = std::env::current_dir().unwrap_or_default();
        if config.local_paths.sc_script.is_empty() {
            config.local_paths.sc_script = project_dir
                .join("executor")
                .join("spaceclaim_transit.py")
                .to_string_lossy()
                .to_string();
        }
        if config.local_paths.sc_bridge.is_empty() {
            config.local_paths.sc_bridge = project_dir
                .join("bridge")
                .join("SpaceClaimBridge.exe")
                .to_string_lossy()
                .to_string();
        }
        if config.local_paths.log_dir.is_empty() {
            config.local_paths.log_dir = project_dir
                .join("logs")
                .to_string_lossy()
                .to_string();
        }
        if config.local_paths.data_dir.is_empty() {
            config.local_paths.data_dir = project_dir
                .join("data")
                .to_string_lossy()
                .to_string();
        }
        Self {
            config,
            focus: SettingsFocus::default(),
            scroll: 0,
            dirty: false,
            validation_errors: Vec::new(),
            edit_buffer: String::new(),
            edit_cursor: 0,
            selection_anchor: None,
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
                5 => self.config.local_paths.sc_script.clone(),
                6 => self.config.local_paths.sc_bridge.clone(),
                7 => self.config.local_paths.scdoc_dir.clone(),
                8 => self.config.local_paths.log_dir.clone(),
                9 => self.config.local_paths.data_dir.clone(),
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
                0 => self.config.remote_config.root_dir.clone(),
                1 => self.config.remote_config.scdoc_dir.clone(),
                2 => self.config.remote_config.msh_dir.clone(),
                3 => self.config.remote_config.result_dir.clone(),
                4 => self.config.remote_config.conda_env.clone(),
                5 => self.config.remote_config.conda_exe.clone(),
                6 => self.config.remote_config.meshing_script.clone(),
                7 => self.config.remote_config.solver_script.clone(),
                8 => self.config.remote_config.flag_dir.clone(),
                _ => String::new(),
            },
            SettingCategory::StepPatterns => match idx {
                0 => self.config.step_file_patterns.sw.clone(),
                1 => self.config.step_file_patterns.sc.clone(),
                2 => self.config.step_file_patterns.meshing.clone(),
                3 => self.config.step_file_patterns.solver.clone(),
                _ => String::new(),
            },
            SettingCategory::EngineConfig => match idx {
                0 => self.config.engine_config.watchdog_interval.to_string(),
                1 => self.config.engine_config.sw_macro_timeout.to_string(),
                2 => self.config.engine_config.sw_close_doc_on_finish.to_string(),
                3 => self.config.engine_config.sw_exit_on_finish.to_string(),
                4 => self.config.engine_config.sw_visible.to_string(),
                5 => self.config.engine_config.sc_timeout.to_string(),
                6 => self.config.engine_config.transfer_timeout.to_string(),
                7 => self.config.engine_config.meshing_timeout.to_string(),
                8 => self.config.engine_config.solver_timeout.to_string(),
                9 => self.config.engine_config.max_retries.to_string(),
                10 => self.config.engine_config.state_refresh_interval.to_string(),
                _ => String::new(),
            },
            SettingCategory::OperationTimeouts => match idx {
                0 => self.config.operation_timeouts.sw_startup.to_string(),
                1 => self.config.operation_timeouts.sw_dispatch_startup_delay.to_string(),
                2 => self.config.operation_timeouts.sw_exit_wait_seconds.to_string(),
                3 => self.config.operation_timeouts.sc_poll_interval.to_string(),
                4 => self.config.operation_timeouts.ssh_connection.to_string(),
                5 => self.config.operation_timeouts.dir_recursion_limit.to_string(),
                6 => self.config.operation_timeouts.ssh_upload_max_retries.to_string(),
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
                5 => self.config.local_paths.sc_script = value.to_string(),
                6 => self.config.local_paths.sc_bridge = value.to_string(),
                7 => self.config.local_paths.scdoc_dir = value.to_string(),
                8 => self.config.local_paths.log_dir = value.to_string(),
                9 => self.config.local_paths.data_dir = value.to_string(),
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
                0 => self.config.remote_config.root_dir = value.to_string(),
                1 => self.config.remote_config.scdoc_dir = value.to_string(),
                2 => self.config.remote_config.msh_dir = value.to_string(),
                3 => self.config.remote_config.result_dir = value.to_string(),
                4 => self.config.remote_config.conda_env = value.to_string(),
                5 => self.config.remote_config.conda_exe = value.to_string(),
                6 => self.config.remote_config.meshing_script = value.to_string(),
                7 => self.config.remote_config.solver_script = value.to_string(),
                8 => self.config.remote_config.flag_dir = value.to_string(),
                _ => {}
            },
            SettingCategory::StepPatterns => match idx {
                0 => self.config.step_file_patterns.sw = value.to_string(),
                1 => self.config.step_file_patterns.sc = value.to_string(),
                2 => self.config.step_file_patterns.meshing = value.to_string(),
                3 => self.config.step_file_patterns.solver = value.to_string(),
                _ => {}
            },
            SettingCategory::EngineConfig => match idx {
                0 => if let Ok(v) = value.parse::<f64>() { self.config.engine_config.watchdog_interval = v; }
                1 => if let Ok(v) = value.parse::<u64>() { self.config.engine_config.sw_macro_timeout = v; }
                2 => self.config.engine_config.sw_close_doc_on_finish = value == "true" || value == "是",
                3 => self.config.engine_config.sw_exit_on_finish = value == "true" || value == "是",
                4 => self.config.engine_config.sw_visible = value == "true" || value == "是",
                5 => if let Ok(v) = value.parse::<u64>() { self.config.engine_config.sc_timeout = v; }
                6 => if let Ok(v) = value.parse::<u64>() { self.config.engine_config.transfer_timeout = v; }
                7 => if let Ok(v) = value.parse::<u64>() { self.config.engine_config.meshing_timeout = v; }
                8 => if let Ok(v) = value.parse::<u64>() { self.config.engine_config.solver_timeout = v; }
                9 => if let Ok(v) = value.parse::<u32>() { self.config.engine_config.max_retries = v; }
                10 => if let Ok(v) = value.parse::<f64>() { self.config.engine_config.state_refresh_interval = v; }
                _ => {}
            },
            SettingCategory::OperationTimeouts => match idx {
                0 => if let Ok(v) = value.parse::<u64>() { self.config.operation_timeouts.sw_startup = v; }
                1 => if let Ok(v) = value.parse::<u64>() { self.config.operation_timeouts.sw_dispatch_startup_delay = v; }
                2 => if let Ok(v) = value.parse::<u64>() { self.config.operation_timeouts.sw_exit_wait_seconds = v; }
                3 => if let Ok(v) = value.parse::<f64>() { self.config.operation_timeouts.sc_poll_interval = v; }
                4 => if let Ok(v) = value.parse::<u64>() { self.config.operation_timeouts.ssh_connection = v; }
                5 => if let Ok(v) = value.parse::<u32>() { self.config.operation_timeouts.dir_recursion_limit = v; }
                6 => if let Ok(v) = value.parse::<u32>() { self.config.operation_timeouts.ssh_upload_max_retries = v; }
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
        self.edit_buffer = self.get_field_value(self.current_category(), self.focus.field_index);
        let char_count = self.edit_buffer.chars().count();
        self.edit_cursor = char_count;
        self.selection_anchor = None;
        self.focus.editing = true;
    }

    pub fn cancel_edit_current_field(&mut self) {
        self.edit_buffer.clear();
        self.edit_cursor = 0;
        self.selection_anchor = None;
        self.focus.editing = false;
    }

    pub fn commit_edit_current_field(&mut self) {
        let cat = self.current_category();
        let idx = self.focus.field_index;
        let old_value = self.get_field_value(cat, idx);
        let new_value = self.edit_buffer.clone();

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

            // 路径字段提交后立即检查文件是否存在
            if let SettingCategory::LocalPaths = cat {
                let value = self.get_field_value(cat, idx);
                let field_name = format!("local_paths.{}", cat.field_name(idx));
                if !value.is_empty() {
                    self.path_status.insert(field_name, std::path::Path::new(&value).exists());
                }
            }
        }

        self.edit_buffer.clear();
        self.edit_cursor = 0;
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
        self.delete_selection(); // replaces selection if any
        let byte_pos = char_to_byte_index(&self.edit_buffer, self.edit_cursor);
        self.edit_buffer.insert(byte_pos, c);
        self.edit_cursor += 1;
        self.selection_anchor = None;
    }

    pub fn input_backspace(&mut self) {
        if self.delete_selection().is_some() {
            return;
        }
        if self.edit_cursor > 0 {
            self.edit_cursor -= 1;
            let byte_pos = char_to_byte_index(&self.edit_buffer, self.edit_cursor);
            self.edit_buffer.remove(byte_pos);
        }
    }

    pub fn input_delete(&mut self) {
        if self.delete_selection().is_some() {
            return;
        }
        if self.edit_cursor < self.edit_buffer.chars().count() {
            let byte_pos = char_to_byte_index(&self.edit_buffer, self.edit_cursor);
            self.edit_buffer.remove(byte_pos);
        }
    }

    pub fn move_cursor_left(&mut self) {
        self.selection_anchor = None;
        if self.edit_cursor > 0 {
            self.edit_cursor -= 1;
        }
    }

    pub fn move_cursor_right(&mut self) {
        self.selection_anchor = None;
        if self.edit_cursor < self.edit_buffer.chars().count() {
            self.edit_cursor += 1;
        }
    }

    pub fn move_cursor_home(&mut self) {
        self.selection_anchor = None;
        self.edit_cursor = 0;
    }

    pub fn move_cursor_end(&mut self) {
        self.selection_anchor = None;
        self.edit_cursor = self.edit_buffer.chars().count();
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

    // ── Selection helpers ──────────────────────────────────────────────

    /// Returns (start, end) char indices of the current selection, where
    /// start <= end.  Returns None when there is no active selection.
    pub fn selection_range(&self) -> Option<(usize, usize)> {
        let anchor = self.selection_anchor?;
        let cursor = self.edit_cursor;
        if anchor == cursor {
            return None;
        }
        let (s, e) = if anchor < cursor { (anchor, cursor) } else { (cursor, anchor) };
        Some((s, e))
    }

    /// Select all text in the edit buffer.
    pub fn select_all(&mut self) {
        let len = self.edit_buffer.chars().count();
        if len == 0 {
            self.selection_anchor = None;
            return;
        }
        self.selection_anchor = Some(0);
        self.edit_cursor = len;
    }

    /// Delete the currently selected text (if any) and return it.
    pub fn delete_selection(&mut self) -> Option<String> {
        let (start, end) = self.selection_range()?;
        let selected: String = self.edit_buffer.chars().skip(start).take(end - start).collect();
        let byte_start = char_to_byte_index(&self.edit_buffer, start);
        let byte_end = char_to_byte_index(&self.edit_buffer, end);
        self.edit_buffer.drain(byte_start..byte_end);
        self.edit_cursor = start;
        self.selection_anchor = None;
        Some(selected)
    }

    /// Copy the selected text to the system clipboard.  Returns true on
    /// success.
    pub fn copy_selection(&self) -> bool {
        let Some((start, end)) = self.selection_range() else {
            return false;
        };
        let text: String = self.edit_buffer.chars().skip(start).take(end - start).collect();
        if text.is_empty() {
            return false;
        }
        clipboard_win::set_clipboard_string(&text).is_ok()
    }

    /// Cut: copy to clipboard then delete selection.
    pub fn cut_selection(&mut self) -> bool {
        if self.selection_range().is_none() {
            return false;
        }
        // Copy first
        let copied = self.copy_selection();
        if copied {
            self.delete_selection();
        }
        copied
    }

    /// Paste from system clipboard, replacing any current selection.
    pub fn paste_from_clipboard(&mut self) -> bool {
        let text = match clipboard_win::get_clipboard_string() {
            Ok(s) => s,
            Err(_) => return false,
        };
        if text.is_empty() {
            return false;
        }
        // Remove newlines – settings fields are single-line
        let cleaned: String = text.chars().filter(|c| *c != '\n' && *c != '\r').collect();
        if cleaned.is_empty() {
            return false;
        }
        // Delete any existing selection first
        self.delete_selection();
        let byte_pos = char_to_byte_index(&self.edit_buffer, self.edit_cursor);
        self.edit_buffer.insert_str(byte_pos, &cleaned);
        self.edit_cursor += cleaned.chars().count();
        true
    }
}
