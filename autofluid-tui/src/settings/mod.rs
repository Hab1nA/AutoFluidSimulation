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
    #[serde(skip, default)]
    pub password: String,
    pub working_dir: String,
    pub scripts_dir: String,
    pub ref_files_dir: String,
    pub scdoc_dir: String,
    pub msh_dir: String,
    pub result_dir: String,
    #[serde(default)]
    pub animation_dir: String,
    pub flag_dir: String,
    pub conda_env: String,
    pub conda_exe: String,
    pub mpi_bin_dir: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct WorkstationConfig {
    pub id: String,
    pub host: String,
    pub port: u16,
    pub username: String,
    #[serde(default, skip_serializing_if = "String::is_empty")]
    pub auth_method: String,
    #[serde(default, skip_serializing_if = "String::is_empty")]
    pub key_filename: String,
    #[serde(skip, default)]
    pub password: String,
    #[serde(default, skip_serializing_if = "String::is_empty")]
    pub reachable_host: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub reachable_port: Option<u16>,
    #[serde(default, skip_serializing_if = "String::is_empty")]
    pub connectivity_mode: String,
    pub working_dir: String,
    pub scripts_dir: String,
    pub ref_files_dir: String,
    pub scdoc_dir: String,
    pub msh_dir: String,
    pub result_dir: String,
    #[serde(default)]
    pub animation_dir: String,
    pub flag_dir: String,
    pub conda_env: String,
    pub conda_exe: String,
    pub mpi_bin_dir: String,
    #[serde(default, skip_serializing_if = "String::is_empty")]
    pub notes: String,
}

impl Default for WorkstationConfig {
    fn default() -> Self {
        let remote = RemoteConfig::default();
        Self {
            id: "WS-A".to_string(),
            host: remote.host,
            port: remote.port,
            username: remote.username,
            auth_method: String::new(),
            key_filename: String::new(),
            password: remote.password,
            reachable_host: String::new(),
            reachable_port: None,
            connectivity_mode: String::new(),
            working_dir: remote.working_dir,
            scripts_dir: remote.scripts_dir,
            ref_files_dir: remote.ref_files_dir,
            scdoc_dir: remote.scdoc_dir,
            msh_dir: remote.msh_dir,
            result_dir: remote.result_dir,
            animation_dir: remote.animation_dir,
            flag_dir: remote.flag_dir,
            conda_env: remote.conda_env,
            conda_exe: remote.conda_exe,
            mpi_bin_dir: remote.mpi_bin_dir,
            notes: String::new(),
        }
    }
}

impl WorkstationConfig {
    fn from_remote_config(id: &str, remote: &RemoteConfig) -> Self {
        Self {
            id: id.to_string(),
            host: remote.host.clone(),
            port: remote.port,
            username: remote.username.clone(),
            auth_method: String::new(),
            key_filename: String::new(),
            password: remote.password.clone(),
            reachable_host: String::new(),
            reachable_port: None,
            connectivity_mode: String::new(),
            working_dir: remote.working_dir.clone(),
            scripts_dir: remote.scripts_dir.clone(),
            ref_files_dir: remote.ref_files_dir.clone(),
            scdoc_dir: remote.scdoc_dir.clone(),
            msh_dir: remote.msh_dir.clone(),
            result_dir: remote.result_dir.clone(),
            animation_dir: remote.animation_dir.clone(),
            flag_dir: remote.flag_dir.clone(),
            conda_env: remote.conda_env.clone(),
            conda_exe: remote.conda_exe.clone(),
            mpi_bin_dir: remote.mpi_bin_dir.clone(),
            notes: String::new(),
        }
    }
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
            animation_dir: String::new(),
            flag_dir: String::new(),
            conda_env: String::new(),
            conda_exe: String::new(),
            mpi_bin_dir: String::new(),
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct StepFilePatterns {
    #[serde(rename = "sw")]
    pub sw: String,
    #[serde(rename = "sc")]
    pub sc: String,
    #[serde(rename = "meshing")]
    pub meshing: String,
    #[serde(rename = "solver")]
    pub solver: String,
    #[serde(rename = "solverdata")]
    pub solver_dat: String,
    #[serde(rename = "postprocess", default)]
    pub postprocess: String,
}

impl Default for StepFilePatterns {
    fn default() -> Self {
        Self {
            sw: "model_gen4.SLDPRT_{config}.step".to_string(),
            sc: "model_gen4_{config}.scdoc".to_string(),
            meshing: "model_gen4_{config}.msh.h5".to_string(),
            solver: "model_gen4_{config}.cas.h5".to_string(),
            solver_dat: "model_gen4_{config}.dat.h5".to_string(),
            postprocess: "postprocess_done_{config}.txt".to_string(),
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SolidWorksConfig {
    pub sw_macro_timeout: u64,
    pub sw_close_doc_on_finish: bool,
    pub sw_visible: bool,
    pub sw_startup: u64,
    pub sw_dispatch_startup_delay: u64,
}

impl Default for SolidWorksConfig {
    fn default() -> Self {
        Self {
            sw_macro_timeout: 3600,
            sw_close_doc_on_finish: true,
            sw_visible: true,
            sw_startup: 60,
            sw_dispatch_startup_delay: 8,
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
    pub sc_max_slots: u64,
    pub sc_persistent_enabled: bool,
    pub sc_oneshot_fallback_enabled: bool,
}

impl Default for SpaceClaimConfig {
    fn default() -> Self {
        Self {
            sc_timeout: 300,
            sc_poll_interval: 2.0,
            sc_process_appear_timeout: 120,
            sc_gui_ready_timeout: 30,
            sc_gui_stable_delay: 5,
            sc_max_slots: 1,
            sc_persistent_enabled: true,
            sc_oneshot_fallback_enabled: true,
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct MeshingConfig {
    pub meshing_timeout: u64,
    pub meshing_processor_count: u32,
}

impl Default for MeshingConfig {
    fn default() -> Self {
        Self {
            meshing_timeout: 600,
            meshing_processor_count: 8,
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SolverConfig {
    pub solver_timeout: u64,
    pub solver_processor_count: u32,
    pub solver_iteration_count: u32,
}

impl Default for SolverConfig {
    fn default() -> Self {
        Self {
            solver_timeout: 7200,
            solver_processor_count: 128,
            solver_iteration_count: 1000,
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PostProcessConfig {
    pub postprocess_timeout: u64,
    pub output_dir: String,
    pub animation_dir: String,
    pub metrics_dir: String,
    pub exit_to_throat_area_ratio: f64,
    pub cstar_reference: f64,
}

impl Default for PostProcessConfig {
    fn default() -> Self {
        Self {
            postprocess_timeout: 3600,
            output_dir: String::new(),
            animation_dir: String::new(),
            metrics_dir: String::new(),
            exit_to_throat_area_ratio: 7.427276607,
            cstar_reference: 1830.4,
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct GlobalSettings {
    pub watchdog_interval: f64,
    pub transfer_timeout: u64,
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
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub workstations: Vec<WorkstationConfig>,
    #[serde(default)]
    pub step_file_patterns: StepFilePatterns,
    #[serde(default)]
    pub solidworks: SolidWorksConfig,
    #[serde(default)]
    pub spaceclaim: SpaceClaimConfig,
    #[serde(default)]
    pub meshing: MeshingConfig,
    #[serde(default)]
    pub solver: SolverConfig,
    #[serde(default)]
    pub postprocess: PostProcessConfig,
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
    Meshing,
    Solver,
    PostProcess,
    GlobalSettings,
}

impl SettingCategory {
    pub const ALL: [SettingCategory; 10] = [
        SettingCategory::LocalPaths,
        SettingCategory::RemoteConnection,
        SettingCategory::RemoteDirs,
        SettingCategory::StepPatterns,
        SettingCategory::SolidWorks,
        SettingCategory::SpaceClaim,
        SettingCategory::Meshing,
        SettingCategory::Solver,
        SettingCategory::PostProcess,
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
            SettingCategory::Meshing => "网格划分",
            SettingCategory::Solver => "仿真求解",
            SettingCategory::PostProcess => "后处理",
            SettingCategory::GlobalSettings => "全局设置",
        }
    }

    pub fn field_count(self) -> usize {
        match self {
            SettingCategory::LocalPaths => 6,
            SettingCategory::RemoteConnection => 4,
            SettingCategory::RemoteDirs => 10,
            SettingCategory::StepPatterns => 6,
            SettingCategory::SolidWorks => 5,
            SettingCategory::SpaceClaim => 8,
            SettingCategory::Meshing => 2,
            SettingCategory::Solver => 3,
            SettingCategory::PostProcess => 4,
            SettingCategory::GlobalSettings => 7,
        }
    }

    pub fn field_name(self, idx: usize) -> &'static str {
        match self {
            SettingCategory::LocalPaths => match idx {
                0 => "sw_exe",
                1 => "sw_model",
                2 => "excel",
                3 => "step_dir",
                4 => "sc_exe",
                5 => "scdoc_dir",
                _ => panic!("LocalPaths: invalid field index {idx}"),
            },
            SettingCategory::RemoteConnection => match idx {
                0 => "host",
                1 => "port",
                2 => "username",
                3 => "password",
                _ => panic!("RemoteConnection: invalid field index {idx}"),
            },
            SettingCategory::RemoteDirs => match idx {
                0 => "working_dir",
                1 => "scripts_dir",
                2 => "ref_files_dir",
                3 => "scdoc_dir",
                4 => "msh_dir",
                5 => "result_dir",
                6 => "flag_dir",
                7 => "conda_env",
                8 => "conda_exe",
                9 => "mpi_bin_dir",
                _ => panic!("RemoteDirs: invalid field index {idx}"),
            },
            SettingCategory::StepPatterns => match idx {
                0 => "sw",
                1 => "sc",
                2 => "meshing",
                3 => "solver",
                4 => "solverdata",
                5 => "postprocess",
                _ => panic!("StepPatterns: invalid field index {idx}"),
            },
            SettingCategory::SolidWorks => match idx {
                0 => "sw_macro_timeout",
                1 => "sw_close_doc_on_finish",
                2 => "sw_visible",
                3 => "sw_startup",
                4 => "sw_dispatch_startup_delay",
                _ => panic!("SolidWorks: invalid field index {idx}"),
            },
            SettingCategory::SpaceClaim => match idx {
                0 => "sc_timeout",
                1 => "sc_poll_interval",
                2 => "sc_process_appear_timeout",
                3 => "sc_gui_ready_timeout",
                4 => "sc_gui_stable_delay",
                5 => "sc_max_slots",
                6 => "sc_persistent_enabled",
                7 => "sc_oneshot_fallback_enabled",
                _ => panic!("SpaceClaim: invalid field index {idx}"),
            },
            SettingCategory::Meshing => match idx {
                0 => "meshing_timeout",
                1 => "meshing_processor_count",
                _ => panic!("Meshing: invalid field index {idx}"),
            },
            SettingCategory::Solver => match idx {
                0 => "solver_timeout",
                1 => "solver_processor_count",
                2 => "solver_iteration_count",
                _ => panic!("Solver: invalid field index {idx}"),
            },
            SettingCategory::PostProcess => match idx {
                0 => "postprocess_timeout",
                1 => "output_dir",
                2 => "animation_dir",
                3 => "metrics_dir",
                _ => panic!("PostProcess: invalid field index {idx}"),
            },
            SettingCategory::GlobalSettings => match idx {
                0 => "watchdog_interval",
                1 => "state_refresh_interval",
                2 => "transfer_timeout",
                3 => "ssh_connection",
                4 => "max_retries",
                5 => "ssh_upload_max_retries",
                6 => "dir_recursion_limit",
                _ => panic!("GlobalSettings: invalid field index {idx}"),
            },
        }
    }

    pub fn display_label(self, idx: usize) -> &'static str {
        match self {
            SettingCategory::LocalPaths => match idx {
                0 => "SW可执行文件",
                1 => "SW模型文件",
                2 => "Excel参数表",
                3 => "STEP输出目录",
                4 => "SC可执行文件",
                5 => "SCDOC输出目录",
                _ => panic!("LocalPaths: invalid field index {idx}"),
            },
            SettingCategory::RemoteConnection => match idx {
                0 => "主机地址",
                1 => "SSH端口",
                2 => "用户名",
                3 => "密码",
                _ => panic!("RemoteConnection: invalid field index {idx}"),
            },
            SettingCategory::RemoteDirs => match idx {
                0 => "仿真工作目录",
                1 => "脚本部署目录",
                2 => "引用文件目录",
                3 => "SCDOC接收目录",
                4 => "网格输出目录",
                5 => "仿真输出目录",
                6 => "仿真标志目录",
                7 => "Conda环境名",
                8 => "Conda可执行文件",
                9 => "MPI安装目录",
                _ => panic!("RemoteDirs: invalid field index {idx}"),
            },
            SettingCategory::StepPatterns => match idx {
                0 => "SW步骤模板",
                1 => "SC步骤模板",
                2 => "Meshing模板",
                3 => "Solver模板",
                4 => "Solver数据模板",
                5 => "PostProcess模板",
                _ => panic!("StepPatterns: invalid field index {idx}"),
            },
            SettingCategory::SolidWorks => match idx {
                0 => "宏超时(秒)",
                1 => "完成后关闭文档",
                2 => "显示窗口",
                3 => "启动超时(秒)",
                4 => "调度启动延迟(秒)",
                _ => panic!("SolidWorks: invalid field index {idx}"),
            },
            SettingCategory::SpaceClaim => match idx {
                0 => "脚本超时(秒)",
                1 => "轮询间隔(秒)",
                2 => "进程出现等待(秒)",
                3 => "窗口就绪超时(秒)",
                4 => "窗口稳定等待(秒)",
                5 => "常驻槽位数",
                6 => "启用常驻Bridge",
                7 => "失败回退一次性Bridge",
                _ => panic!("SpaceClaim: invalid field index {idx}"),
            },
            SettingCategory::Meshing => match idx {
                0 => "网格超时(秒)",
                1 => "网格核心数",
                _ => panic!("Meshing: invalid field index {idx}"),
            },
            SettingCategory::Solver => match idx {
                0 => "求解超时(秒)",
                1 => "求解核心数",
                2 => "求解迭代次数",
                _ => panic!("Solver: invalid field index {idx}"),
            },
            SettingCategory::PostProcess => match idx {
                0 => "后处理超时(秒)",
                1 => "后处理输出目录",
                2 => "动画输出目录",
                3 => "指标输出目录",
                _ => panic!("PostProcess: invalid field index {idx}"),
            },
            SettingCategory::GlobalSettings => match idx {
                0 => "看门狗间隔(秒)",
                1 => "状态刷新间隔(秒)",
                2 => "传输超时(秒)",
                3 => "SSH连接超时(秒)",
                4 => "最大重试",
                5 => "SSH上传最大重试",
                6 => "目录递归深度限制",
                _ => panic!("GlobalSettings: invalid field index {idx}"),
            },
        }
    }

    pub fn is_bool_field(self, idx: usize) -> bool {
        (matches!(self, SettingCategory::SolidWorks) && matches!(idx, 1..=2))
            || (matches!(self, SettingCategory::SpaceClaim) && matches!(idx, 6..=7))
    }

    pub fn is_password_field(self, idx: usize) -> bool {
        // 使用基于字段名的防御性检查，避免因字段顺序调整而失效
        matches!(self, SettingCategory::RemoteConnection) && self.field_name(idx) == "password"
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
            SettingCategory::Meshing => format!("meshing.{}", self.field_name(idx)),
            SettingCategory::Solver => format!("solver.{}", self.field_name(idx)),
            SettingCategory::PostProcess => format!("postprocess.{}", self.field_name(idx)),
            SettingCategory::GlobalSettings => format!("global_settings.{}", self.field_name(idx)),
        }
    }

    /// 判断字段是否为本地文件/目录路径（需要存在性检查）。
    /// 当前仅 `LocalPaths` category 的所有字段为本地路径。
    pub fn is_path_field(self, idx: usize) -> bool {
        matches!(self, SettingCategory::LocalPaths)
            || (matches!(self, SettingCategory::PostProcess) && matches!(idx, 1..=3))
    }
}

#[derive(Debug, Clone)]
pub struct UndoEntry {
    pub category: SettingCategory,
    pub field_index: usize,
    pub workstation_index: Option<usize>,
    pub old_value: String,
}

#[derive(Debug, Clone, Default)]
pub struct SettingsFocus {
    pub category_index: usize,
    pub field_index: usize,
    pub workstation_index: Option<usize>,
    pub editing: bool,
}

impl SettingsFocus {
    pub fn workstation_index_or_default(&self) -> usize {
        self.workstation_index.unwrap_or(0)
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct SettingsFieldHit {
    pub category_index: usize,
    pub field_index: usize,
    pub workstation_index: Option<usize>,
    pub y: u16,
    pub x_start: u16,
    pub x_end: u16,
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
    /// Mouse hover tracking: (category_index, field_index, workstation_index)
    pub hovered_field: Option<(usize, usize, Option<usize>)>,
    /// Click animation highlight (short-lived, ~20ms)
    pub clicked_field: Option<(usize, usize, Option<usize>)>,
    pub field_click_time: Option<std::time::Instant>,
    /// Double-click tracking (longer window, ~400ms)
    pub last_clicked_field: Option<(usize, usize, Option<usize>)>,
    pub last_click_time: Option<std::time::Instant>,
    /// Field positions computed during rendering.
    pub field_positions: Vec<SettingsFieldHit>,
    /// Path existence cache: field_name -> exists
    pub path_status: std::collections::HashMap<String, bool>,
}

impl SettingsState {
    pub fn new() -> Self {
        let mut config = config_io::load_config().unwrap_or_default();
        // 密码始终从 .env 文件读取（TOML 中不存储密码）
        config_io::apply_env_passwords(&mut config);
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

    #[cfg(test)]
    pub fn default_for_tests() -> Self {
        Self {
            config: SettingsConfig::default(),
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
        SettingCategory::ALL
            .get(self.focus.category_index)
            .copied()
            .unwrap_or(SettingCategory::LocalPaths)
    }

    pub fn set_focus(
        &mut self,
        category_index: usize,
        field_index: usize,
        workstation_index: Option<usize>,
    ) {
        self.focus.category_index = category_index;
        self.focus.field_index = field_index;
        self.focus.workstation_index = workstation_index;
    }

    pub fn is_editing_field(&self) -> bool {
        self.focus.editing
    }

    pub fn get_field_value(&self, category: SettingCategory, idx: usize) -> String {
        if matches!(
            category,
            SettingCategory::RemoteConnection | SettingCategory::RemoteDirs
        ) && self.focus.workstation_index.is_some()
        {
            return self.get_workstation_field_value(
                self.focus.workstation_index_or_default(),
                category,
                idx,
            );
        }
        macro_rules! field_val {
            ($config:expr, $field:ident) => {
                $config.$field.to_string()
            };
            ($config:expr, $field:ident, string) => {
                $config.$field.clone()
            };
        }
        match category {
            SettingCategory::LocalPaths => match idx {
                0 => field_val!(self.config.local_paths, sw_exe, string),
                1 => field_val!(self.config.local_paths, sw_model, string),
                2 => field_val!(self.config.local_paths, excel, string),
                3 => field_val!(self.config.local_paths, step_dir, string),
                4 => field_val!(self.config.local_paths, sc_exe, string),
                5 => field_val!(self.config.local_paths, scdoc_dir, string),
                _ => String::new(),
            },
            SettingCategory::RemoteConnection => match idx {
                0 => field_val!(self.config.remote_config, host, string),
                1 => field_val!(self.config.remote_config, port),
                2 => field_val!(self.config.remote_config, username, string),
                3 => field_val!(self.config.remote_config, password, string),
                _ => String::new(),
            },
            SettingCategory::RemoteDirs => match idx {
                0 => field_val!(self.config.remote_config, working_dir, string),
                1 => field_val!(self.config.remote_config, scripts_dir, string),
                2 => field_val!(self.config.remote_config, ref_files_dir, string),
                3 => field_val!(self.config.remote_config, scdoc_dir, string),
                4 => field_val!(self.config.remote_config, msh_dir, string),
                5 => field_val!(self.config.remote_config, result_dir, string),
                6 => field_val!(self.config.remote_config, flag_dir, string),
                7 => field_val!(self.config.remote_config, conda_env, string),
                8 => field_val!(self.config.remote_config, conda_exe, string),
                9 => field_val!(self.config.remote_config, mpi_bin_dir, string),
                _ => String::new(),
            },
            SettingCategory::StepPatterns => match idx {
                0 => field_val!(self.config.step_file_patterns, sw, string),
                1 => field_val!(self.config.step_file_patterns, sc, string),
                2 => field_val!(self.config.step_file_patterns, meshing, string),
                3 => field_val!(self.config.step_file_patterns, solver, string),
                4 => field_val!(self.config.step_file_patterns, solver_dat, string),
                5 => field_val!(self.config.step_file_patterns, postprocess, string),
                _ => String::new(),
            },
            SettingCategory::SolidWorks => match idx {
                0 => field_val!(self.config.solidworks, sw_macro_timeout),
                1 => field_val!(self.config.solidworks, sw_close_doc_on_finish),
                2 => field_val!(self.config.solidworks, sw_visible),
                3 => field_val!(self.config.solidworks, sw_startup),
                4 => field_val!(self.config.solidworks, sw_dispatch_startup_delay),
                _ => String::new(),
            },
            SettingCategory::SpaceClaim => match idx {
                0 => field_val!(self.config.spaceclaim, sc_timeout),
                1 => field_val!(self.config.spaceclaim, sc_poll_interval),
                2 => field_val!(self.config.spaceclaim, sc_process_appear_timeout),
                3 => field_val!(self.config.spaceclaim, sc_gui_ready_timeout),
                4 => field_val!(self.config.spaceclaim, sc_gui_stable_delay),
                5 => field_val!(self.config.spaceclaim, sc_max_slots),
                6 => field_val!(self.config.spaceclaim, sc_persistent_enabled),
                7 => field_val!(self.config.spaceclaim, sc_oneshot_fallback_enabled),
                _ => String::new(),
            },
            SettingCategory::Meshing => match idx {
                0 => field_val!(self.config.meshing, meshing_timeout),
                1 => field_val!(self.config.meshing, meshing_processor_count),
                _ => String::new(),
            },
            SettingCategory::Solver => match idx {
                0 => field_val!(self.config.solver, solver_timeout),
                1 => field_val!(self.config.solver, solver_processor_count),
                2 => field_val!(self.config.solver, solver_iteration_count),
                _ => String::new(),
            },
            SettingCategory::PostProcess => match idx {
                0 => field_val!(self.config.postprocess, postprocess_timeout),
                1 => field_val!(self.config.postprocess, output_dir, string),
                2 => field_val!(self.config.postprocess, animation_dir, string),
                3 => field_val!(self.config.postprocess, metrics_dir, string),
                _ => String::new(),
            },
            SettingCategory::GlobalSettings => match idx {
                0 => field_val!(self.config.global_settings, watchdog_interval),
                1 => field_val!(self.config.global_settings, state_refresh_interval),
                2 => field_val!(self.config.global_settings, transfer_timeout),
                3 => field_val!(self.config.global_settings, ssh_connection),
                4 => field_val!(self.config.global_settings, max_retries),
                5 => field_val!(self.config.global_settings, ssh_upload_max_retries),
                6 => field_val!(self.config.global_settings, dir_recursion_limit),
                _ => String::new(),
            },
        }
    }

    fn workstation_or_legacy(&self, workstation_index: usize) -> WorkstationConfig {
        self.config
            .workstations
            .get(workstation_index)
            .cloned()
            .unwrap_or_else(|| {
                WorkstationConfig::from_remote_config(
                    if workstation_index == 0 { "WS-A" } else { "" },
                    &self.config.remote_config,
                )
            })
    }

    pub fn get_workstation_field_value(
        &self,
        workstation_index: usize,
        category: SettingCategory,
        idx: usize,
    ) -> String {
        let workstation = self.workstation_or_legacy(workstation_index);
        match category {
            SettingCategory::RemoteConnection => match idx {
                0 => workstation.host,
                1 => workstation.port.to_string(),
                2 => workstation.username,
                3 => workstation.password,
                _ => String::new(),
            },
            SettingCategory::RemoteDirs => match idx {
                0 => workstation.working_dir,
                1 => workstation.scripts_dir,
                2 => workstation.ref_files_dir,
                3 => workstation.scdoc_dir,
                4 => workstation.msh_dir,
                5 => workstation.result_dir,
                6 => workstation.flag_dir,
                7 => workstation.conda_env,
                8 => workstation.conda_exe,
                9 => workstation.mpi_bin_dir,
                _ => String::new(),
            },
            _ => self.get_field_value(category, idx),
        }
    }

    pub fn set_field_value(&mut self, category: SettingCategory, idx: usize, value: &str) {
        if matches!(
            category,
            SettingCategory::RemoteConnection | SettingCategory::RemoteDirs
        ) && self.focus.workstation_index.is_some()
        {
            self.set_workstation_field_value(
                self.focus.workstation_index_or_default(),
                category,
                idx,
                value,
            );
            return;
        }
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
                5 => self.config.step_file_patterns.postprocess = value.to_string(),
                _ => {}
            },
            SettingCategory::SolidWorks => match idx {
                0 => {
                    if let Ok(v) = value.parse::<u64>() {
                        self.config.solidworks.sw_macro_timeout = v;
                    }
                }
                1 => {
                    self.config.solidworks.sw_close_doc_on_finish = value == "true" || value == "是"
                }
                2 => self.config.solidworks.sw_visible = value == "true" || value == "是",
                3 => {
                    if let Ok(v) = value.parse::<u64>() {
                        self.config.solidworks.sw_startup = v;
                    }
                }
                4 => {
                    if let Ok(v) = value.parse::<u64>() {
                        self.config.solidworks.sw_dispatch_startup_delay = v;
                    }
                }
                _ => {}
            },
            SettingCategory::SpaceClaim => match idx {
                0 => {
                    if let Ok(v) = value.parse::<u64>() {
                        self.config.spaceclaim.sc_timeout = v;
                    }
                }
                1 => {
                    if let Ok(v) = value.parse::<f64>() {
                        self.config.spaceclaim.sc_poll_interval = v;
                    }
                }
                2 => {
                    if let Ok(v) = value.parse::<u64>() {
                        self.config.spaceclaim.sc_process_appear_timeout = v;
                    }
                }
                3 => {
                    if let Ok(v) = value.parse::<u64>() {
                        self.config.spaceclaim.sc_gui_ready_timeout = v;
                    }
                }
                4 => {
                    if let Ok(v) = value.parse::<u64>() {
                        self.config.spaceclaim.sc_gui_stable_delay = v;
                    }
                }
                5 => {
                    if let Ok(v) = value.parse::<u64>() {
                        self.config.spaceclaim.sc_max_slots = v;
                    }
                }
                6 => {
                    self.config.spaceclaim.sc_persistent_enabled = value == "true" || value == "是"
                }
                7 => {
                    self.config.spaceclaim.sc_oneshot_fallback_enabled =
                        value == "true" || value == "是"
                }
                _ => {}
            },
            SettingCategory::Meshing => match idx {
                0 => {
                    if let Ok(v) = value.parse::<u64>() {
                        self.config.meshing.meshing_timeout = v;
                    }
                }
                1 => {
                    if let Ok(v) = value.parse::<u32>() {
                        self.config.meshing.meshing_processor_count = v;
                    }
                }
                _ => {}
            },
            SettingCategory::Solver => match idx {
                0 => {
                    if let Ok(v) = value.parse::<u64>() {
                        self.config.solver.solver_timeout = v;
                    }
                }
                1 => {
                    if let Ok(v) = value.parse::<u32>() {
                        self.config.solver.solver_processor_count = v;
                    }
                }
                2 => {
                    if let Ok(v) = value.parse::<u32>() {
                        self.config.solver.solver_iteration_count = v;
                    }
                }
                _ => {}
            },
            SettingCategory::PostProcess => match idx {
                0 => {
                    if let Ok(v) = value.parse::<u64>() {
                        self.config.postprocess.postprocess_timeout = v;
                    }
                }
                1 => self.config.postprocess.output_dir = value.to_string(),
                2 => self.config.postprocess.animation_dir = value.to_string(),
                3 => self.config.postprocess.metrics_dir = value.to_string(),
                _ => {}
            },
            SettingCategory::GlobalSettings => match idx {
                0 => {
                    if let Ok(v) = value.parse::<f64>() {
                        self.config.global_settings.watchdog_interval = v;
                    }
                }
                1 => {
                    if let Ok(v) = value.parse::<f64>() {
                        self.config.global_settings.state_refresh_interval = v;
                    }
                }
                2 => {
                    if let Ok(v) = value.parse::<u64>() {
                        self.config.global_settings.transfer_timeout = v;
                    }
                }
                3 => {
                    if let Ok(v) = value.parse::<u64>() {
                        self.config.global_settings.ssh_connection = v;
                    }
                }
                4 => {
                    if let Ok(v) = value.parse::<u32>() {
                        self.config.global_settings.max_retries = v;
                    }
                }
                5 => {
                    if let Ok(v) = value.parse::<u32>() {
                        self.config.global_settings.ssh_upload_max_retries = v;
                    }
                }
                6 => {
                    if let Ok(v) = value.parse::<u32>() {
                        self.config.global_settings.dir_recursion_limit = v;
                    }
                }
                _ => {}
            },
        }
        self.dirty = true;
    }

    fn ensure_workstation_index(&mut self, workstation_index: usize) {
        while self.config.workstations.len() <= workstation_index {
            let id = match self.config.workstations.len() {
                0 => "WS-A",
                1 => "WS-B",
                2 => "WS-C",
                _ => "",
            };
            self.config
                .workstations
                .push(WorkstationConfig::from_remote_config(
                    id,
                    &self.config.remote_config,
                ));
        }
    }

    pub fn set_workstation_field_value(
        &mut self,
        workstation_index: usize,
        category: SettingCategory,
        idx: usize,
        value: &str,
    ) {
        self.ensure_workstation_index(workstation_index);
        let Some(workstation) = self.config.workstations.get_mut(workstation_index) else {
            return;
        };
        match category {
            SettingCategory::RemoteConnection => match idx {
                0 => workstation.host = value.to_string(),
                1 => {
                    if let Ok(port) = value.parse::<u16>() {
                        workstation.port = port;
                    }
                }
                2 => workstation.username = value.to_string(),
                3 => workstation.password = value.to_string(),
                _ => {}
            },
            SettingCategory::RemoteDirs => match idx {
                0 => workstation.working_dir = value.to_string(),
                1 => workstation.scripts_dir = value.to_string(),
                2 => workstation.ref_files_dir = value.to_string(),
                3 => workstation.scdoc_dir = value.to_string(),
                4 => workstation.msh_dir = value.to_string(),
                5 => workstation.result_dir = value.to_string(),
                6 => workstation.flag_dir = value.to_string(),
                7 => workstation.conda_env = value.to_string(),
                8 => workstation.conda_exe = value.to_string(),
                9 => workstation.mpi_bin_dir = value.to_string(),
                _ => {}
            },
            _ => self.set_field_value(category, idx, value),
        }
        self.dirty = true;
    }

    pub fn move_focus_up(&mut self) {
        if self.focus.field_index > 0 {
            self.focus.field_index -= 1;
        } else if self.focus.category_index > 0 {
            self.focus.category_index -= 1;
            self.focus.field_index = self.current_category().field_count().saturating_sub(1);
            self.focus.workstation_index = None;
        }
    }

    pub fn move_focus_down(&mut self) {
        let cat = self.current_category();
        if self.focus.field_index + 1 < cat.field_count() {
            self.focus.field_index += 1;
        } else if self.focus.category_index + 1 < SettingCategory::ALL.len() {
            self.focus.category_index += 1;
            self.focus.field_index = 0;
            self.focus.workstation_index = None;
        }
    }

    pub fn move_focus_next(&mut self) {
        let cat = self.current_category();
        if self.focus.field_index + 1 < cat.field_count() {
            self.focus.field_index += 1;
        } else if self.focus.category_index + 1 < SettingCategory::ALL.len() {
            self.focus.category_index += 1;
            self.focus.field_index = 0;
            self.focus.workstation_index = None;
        } else {
            self.focus.category_index = 0;
            self.focus.field_index = 0;
            self.focus.workstation_index = None;
        }
    }

    pub fn move_focus_prev(&mut self) {
        if self.focus.field_index > 0 {
            self.focus.field_index -= 1;
        } else if self.focus.category_index > 0 {
            self.focus.category_index -= 1;
            self.focus.field_index = self.current_category().field_count().saturating_sub(1);
            self.focus.workstation_index = None;
        } else {
            let last_idx = SettingCategory::ALL.len() - 1;
            self.focus.category_index = last_idx;
            self.focus.field_index = SettingCategory::ALL[last_idx]
                .field_count()
                .saturating_sub(1);
            self.focus.workstation_index = None;
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
                workstation_index: self.focus.workstation_index,
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
            let field_name = self.focus.workstation_index.map_or_else(
                || cat.field_full_name(idx),
                |ws| format!("workstations[{ws}].{}", cat.field_name(idx)),
            );
            if !value.is_empty() {
                self.path_status
                    .insert(field_name, std::path::Path::new(&value).exists());
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
            if let Some(workstation_index) = entry.workstation_index {
                self.set_workstation_field_value(
                    workstation_index,
                    entry.category,
                    entry.field_index,
                    &entry.old_value,
                );
            } else {
                self.set_field_value(entry.category, entry.field_index, &entry.old_value);
            }
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
        let new = if old == "true" || old == "是" {
            "false"
        } else {
            "true"
        };
        self.undo_stack.push(UndoEntry {
            category: cat,
            field_index: idx,
            workstation_index: self.focus.workstation_index,
            old_value: old,
        });
        self.set_field_value(cat, idx, new);
    }

    pub fn save(&self) -> Result<(), Vec<ValidationError>> {
        let errors = validate_config(&self.config);
        if errors
            .iter()
            .any(|e| matches!(e.severity, crate::settings::validation::Severity::Error))
        {
            return Err(errors);
        }
        config_io::save_config(&self.config).map_err(|e| {
            vec![ValidationError {
                field_name: "save".to_string(),
                message: e,
                severity: crate::settings::validation::Severity::Error,
            }]
        })?;
        // Write passwords to .env (always write to allow clearing).
        config_io::write_env_passwords(&self.config).map_err(|e| {
            vec![ValidationError {
                field_name: ".env".to_string(),
                message: e,
                severity: crate::settings::validation::Severity::Error,
            }]
        })?;
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

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::{Mutex, OnceLock};

    fn cwd_lock() -> &'static Mutex<()> {
        static LOCK: OnceLock<Mutex<()>> = OnceLock::new();
        LOCK.get_or_init(|| Mutex::new(()))
    }

    fn unique_temp_project_dir() -> std::path::PathBuf {
        std::env::temp_dir().join(format!(
            "autofluid-tui-settings-{}",
            crate::generate_request_id()
        ))
    }

    #[test]
    fn settings_config_round_trips_three_workstations() {
        let toml_text = r#"
[remote_config]
host = "172.17.135.240"
port = 22
username = "ps"
password = "toml-remote-secret"
working_dir = 'D:\xkz_1020\workingdir'
scripts_dir = 'D:\xkz_1020\scripts'
ref_files_dir = 'D:\xkz_1020\fluent_chemkin_files'
scdoc_dir = 'D:\xkz_1020\scdoc'
msh_dir = 'D:\xkz_1020\msh'
result_dir = 'D:\xkz_1020\case'
flag_dir = 'D:\xkz_1020\flags'
conda_env = "pyfluent"
conda_exe = 'C:\ProgramData\anaconda3\Scripts\conda.exe'
mpi_bin_dir = 'C:\Program Files\ANSYS Inc\v241\fluent\fluent24.1.0\multiport\mpi\win64\intel2021\bin'

[[workstations]]
id = "WS-A"
host = "172.17.135.240"
port = 22
username = "ps"
password = "toml-secret"
reachable_host = "127.0.0.1"
reachable_port = 2222
connectivity_mode = "reverse_tunnel"
working_dir = 'D:\xkz_1020\workingdir'
scripts_dir = 'D:\xkz_1020\scripts'
ref_files_dir = 'D:\xkz_1020\fluent_chemkin_files'
scdoc_dir = 'D:\xkz_1020\scdoc'
msh_dir = 'D:\xkz_1020\msh'
result_dir = 'D:\xkz_1020\case'
flag_dir = 'D:\xkz_1020\flags'
conda_env = "pyfluent"
conda_exe = 'C:\ProgramData\anaconda3\Scripts\conda.exe'
mpi_bin_dir = 'C:\Program Files\ANSYS Inc\v241\fluent\fluent24.1.0\multiport\mpi\win64\intel2021\bin'

[[workstations]]
id = "WS-B"
host = "172.17.135.89"
port = 22
username = "ps"
auth_method = "none"
key_filename = '${USERPROFILE}\.ssh\id_ed25519'
working_dir = 'D:\xkz_1020\workingdir'
scripts_dir = 'D:\xkz_1020\scripts'
ref_files_dir = 'D:\xkz_1020\fluent_chemkin_files'
scdoc_dir = 'D:\xkz_1020\scdoc_b'
msh_dir = 'D:\xkz_1020\msh_b'
result_dir = 'D:\xkz_1020\case_b'
flag_dir = 'D:\xkz_1020\flags_b'
conda_env = "pyfluent"
conda_exe = 'C:\ProgramData\anaconda3\Scripts\conda.exe'
mpi_bin_dir = 'C:\Program Files\ANSYS Inc\v241\fluent\fluent24.1.0\multiport\mpi\win64\intel2021\bin'

[[workstations]]
id = "WS-C"
host = "172.17.135.254"
port = 22
username = "ps"
auth_method = "none"
key_filename = '${USERPROFILE}\.ssh\id_ed25519'
notes = "offline during development"
working_dir = 'D:\xkz_1020\workingdir'
scripts_dir = 'D:\xkz_1020\scripts'
ref_files_dir = 'D:\xkz_1020\fluent_chemkin_files'
scdoc_dir = 'D:\xkz_1020\scdoc_c'
msh_dir = 'D:\xkz_1020\msh_c'
result_dir = 'D:\xkz_1020\case_c'
flag_dir = 'D:\xkz_1020\flags_c'
conda_env = "pyfluent"
conda_exe = 'C:\ProgramData\anaconda3\Scripts\conda.exe'
mpi_bin_dir = 'C:\Program Files\ANSYS Inc\v241\fluent\fluent24.1.0\multiport\mpi\win64\intel2021\bin'
"#;

        let config: SettingsConfig = toml::from_str(toml_text).expect("parse workstations");

        assert_eq!(config.remote_config.password, "");
        assert_eq!(config.workstations.len(), 3);
        assert_eq!(config.workstations[0].id, "WS-A");
        assert_eq!(config.workstations[0].password, "");
        assert_eq!(config.workstations[1].host, "172.17.135.89");
        assert_eq!(config.workstations[1].auth_method, "none");
        assert_eq!(
            config.workstations[1].key_filename,
            r"${USERPROFILE}\.ssh\id_ed25519"
        );
        assert_eq!(config.workstations[1].scdoc_dir, r"D:\xkz_1020\scdoc_b");
        assert_eq!(config.workstations[2].notes, "offline during development");

        let serialized = toml::to_string_pretty(&config).expect("serialize workstations");

        assert!(serialized.contains("[[workstations]]"));
        assert!(serialized.contains("id = \"WS-A\""));
        assert!(serialized.contains("host = \"172.17.135.89\""));
        assert!(serialized.contains("auth_method = \"none\""));
        assert!(serialized.contains("key_filename"));
        assert!(
            serialized.contains("scdoc_dir = 'D:\\xkz_1020\\scdoc_c'")
                || serialized.contains("scdoc_dir = \"D:\\\\xkz_1020\\\\scdoc_c\"")
        );
    }

    #[test]
    fn workstation_field_edits_do_not_cross_columns() {
        let mut state = SettingsState::default_for_tests();
        state.config.workstations = vec![
            WorkstationConfig {
                id: "WS-A".to_string(),
                host: "172.17.135.240".to_string(),
                ..WorkstationConfig::default()
            },
            WorkstationConfig {
                id: "WS-B".to_string(),
                host: "172.17.135.89".to_string(),
                ..WorkstationConfig::default()
            },
            WorkstationConfig {
                id: "WS-C".to_string(),
                host: "172.17.135.254".to_string(),
                ..WorkstationConfig::default()
            },
        ];

        state.set_workstation_field_value(1, SettingCategory::RemoteDirs, 3, r"D:\ws-b\scdoc");

        assert_eq!(
            state.get_workstation_field_value(1, SettingCategory::RemoteDirs, 3),
            r"D:\ws-b\scdoc"
        );
        assert_ne!(
            state.get_workstation_field_value(0, SettingCategory::RemoteDirs, 3),
            r"D:\ws-b\scdoc"
        );
        assert_ne!(
            state.get_workstation_field_value(2, SettingCategory::RemoteDirs, 3),
            r"D:\ws-b\scdoc"
        );
    }

    #[test]
    fn settings_page_round_trips_three_workstation_passwords_through_env_only() {
        let _guard = cwd_lock().lock().expect("lock cwd");
        let project_dir = unique_temp_project_dir();
        std::fs::create_dir_all(&project_dir).expect("create project dir");
        std::fs::write(project_dir.join("start_daemon.py"), "").expect("write project marker");
        let sw_model = project_dir.join("model.SLDPRT");
        let excel = project_dir.join("params.xlsx");
        std::fs::write(&sw_model, "").expect("write model placeholder");
        std::fs::write(&excel, "").expect("write excel placeholder");
        std::fs::write(
            project_dir.join("autofluid_config.toml"),
            r#"
[[workstations]]
id = "WS-A"
host = "172.17.135.240"
port = 22
username = "ps"
password = "toml-a-ignored"
working_dir = 'D:\work-a'
scripts_dir = 'D:\scripts-a'
ref_files_dir = 'D:\refs-a'
scdoc_dir = 'D:\scdoc-a'
msh_dir = 'D:\msh-a'
result_dir = 'D:\case-a'
flag_dir = 'D:\flags-a'
conda_env = "pyfluent"
conda_exe = 'C:\conda.exe'
mpi_bin_dir = 'C:\mpi'

[[workstations]]
id = "WS-B"
host = "172.17.135.89"
port = 22
username = "ps"
password = "toml-b-ignored"
working_dir = 'D:\work-b'
scripts_dir = 'D:\scripts-b'
ref_files_dir = 'D:\refs-b'
scdoc_dir = 'D:\scdoc-b'
msh_dir = 'D:\msh-b'
result_dir = 'D:\case-b'
flag_dir = 'D:\flags-b'
conda_env = "pyfluent"
conda_exe = 'C:\conda.exe'
mpi_bin_dir = 'C:\mpi'

[[workstations]]
id = "WS-C"
host = "172.17.135.254"
port = 22
username = "ps"
password = "toml-c-ignored"
working_dir = 'D:\work-c'
scripts_dir = 'D:\scripts-c'
ref_files_dir = 'D:\refs-c'
scdoc_dir = 'D:\scdoc-c'
msh_dir = 'D:\msh-c'
result_dir = 'D:\case-c'
flag_dir = 'D:\flags-c'
conda_env = "pyfluent"
conda_exe = 'C:\conda.exe'
mpi_bin_dir = 'C:\mpi'
"#,
        )
        .expect("write config");
        std::fs::write(
            project_dir.join(".env"),
            "AUTOFLUID_WS_A_SSH_PASSWORD=old-a\nAUTOFLUID_WS_B_SSH_PASSWORD=old-b\nAUTOFLUID_WS_C_SSH_PASSWORD=old-c\n",
        )
        .expect("write env");

        let previous_dir = std::env::current_dir().expect("current dir");
        std::env::set_current_dir(&project_dir).expect("set cwd");

        let mut state = SettingsState::new();
        state.config.local_paths.sw_model = sw_model.to_string_lossy().into_owned();
        state.config.local_paths.excel = excel.to_string_lossy().into_owned();
        assert_eq!(
            state.get_workstation_field_value(0, SettingCategory::RemoteConnection, 3),
            "old-a"
        );
        assert_eq!(
            state.get_workstation_field_value(1, SettingCategory::RemoteConnection, 3),
            "old-b"
        );
        assert_eq!(
            state.get_workstation_field_value(2, SettingCategory::RemoteConnection, 3),
            "old-c"
        );

        for (workstation_index, password) in ["new-a", "new-b", "new-c"].iter().enumerate() {
            state.set_focus(1, 3, Some(workstation_index));
            state.begin_edit_current_field();
            assert_eq!(
                state.buffer.text,
                format!("old-{}", (b'a' + workstation_index as u8) as char)
            );
            state.buffer = TextBuffer::with_text((*password).to_string());
            state.commit_edit_current_field();
        }
        state.save().expect("save settings");

        let env_contents = std::fs::read_to_string(project_dir.join(".env")).expect("read env");
        assert!(env_contents.contains("AUTOFLUID_WS_A_SSH_PASSWORD=new-a\n"));
        assert!(env_contents.contains("AUTOFLUID_WS_B_SSH_PASSWORD=new-b\n"));
        assert!(env_contents.contains("AUTOFLUID_WS_C_SSH_PASSWORD=new-c\n"));

        let toml_contents =
            std::fs::read_to_string(project_dir.join("autofluid_config.toml")).expect("read toml");
        assert!(!toml_contents.contains("old-a"));
        assert!(!toml_contents.contains("old-b"));
        assert!(!toml_contents.contains("old-c"));
        assert!(!toml_contents.contains("new-a"));
        assert!(!toml_contents.contains("new-b"));
        assert!(!toml_contents.contains("new-c"));
        assert!(!toml_contents.contains("toml-a-ignored"));
        assert!(!toml_contents.contains("toml-b-ignored"));
        assert!(!toml_contents.contains("toml-c-ignored"));

        let reopened = SettingsState::new();
        std::env::set_current_dir(previous_dir).expect("restore cwd");

        assert_eq!(
            reopened.get_workstation_field_value(0, SettingCategory::RemoteConnection, 3),
            "new-a"
        );
        assert_eq!(
            reopened.get_workstation_field_value(1, SettingCategory::RemoteConnection, 3),
            "new-b"
        );
        assert_eq!(
            reopened.get_workstation_field_value(2, SettingCategory::RemoteConnection, 3),
            "new-c"
        );

        let _ = std::fs::remove_dir_all(project_dir);
    }

    #[test]
    fn serializing_config_keeps_workstation_password_out_of_toml() {
        let mut config = SettingsConfig::default();
        config.workstations = vec![WorkstationConfig {
            id: "WS-A".to_string(),
            host: "172.17.135.240".to_string(),
            port: 22,
            username: "ps".to_string(),
            password: "secret".to_string(),
            ..WorkstationConfig::default()
        }];

        let serialized = toml::to_string_pretty(&config).expect("serialize config");

        assert!(!serialized.contains("secret"));
        assert!(!serialized.contains("password"));
    }

    #[test]
    fn step_patterns_do_not_expose_transfer_field() {
        assert_eq!(SettingCategory::StepPatterns.field_count(), 6);
        assert_eq!(SettingCategory::StepPatterns.field_name(2), "meshing");
        assert_eq!(
            SettingCategory::StepPatterns.field_full_name(2),
            "step_file_patterns.meshing"
        );
        assert_eq!(
            SettingCategory::StepPatterns.display_label(2),
            "Meshing模板"
        );

        let mut state = SettingsState::new();
        state.config = SettingsConfig::default();
        assert_eq!(
            state.get_field_value(SettingCategory::StepPatterns, 2),
            "model_gen4_{config}.msh.h5"
        );

        state.set_field_value(SettingCategory::StepPatterns, 2, "mesh_{config}.msh.h5");
        assert_eq!(
            state.config.step_file_patterns.meshing,
            "mesh_{config}.msh.h5"
        );

        assert_eq!(SettingCategory::StepPatterns.field_name(5), "postprocess");
        assert_eq!(
            SettingCategory::StepPatterns.field_full_name(5),
            "step_file_patterns.postprocess"
        );
        assert_eq!(
            state.get_field_value(SettingCategory::StepPatterns, 5),
            "postprocess_done_{config}.txt"
        );
    }

    #[test]
    fn solidworks_settings_do_not_expose_exit_fields() {
        assert_eq!(SettingCategory::SolidWorks.field_count(), 5);

        let field_names = (0..SettingCategory::SolidWorks.field_count())
            .map(|idx| SettingCategory::SolidWorks.field_name(idx))
            .collect::<Vec<_>>();

        assert_eq!(
            field_names,
            vec![
                "sw_macro_timeout",
                "sw_close_doc_on_finish",
                "sw_visible",
                "sw_startup",
                "sw_dispatch_startup_delay",
            ]
        );
    }

    #[test]
    fn spaceclaim_settings_expose_persistent_bridge_fields() {
        assert_eq!(SettingCategory::SpaceClaim.field_count(), 8);

        let field_names = (0..SettingCategory::SpaceClaim.field_count())
            .map(|idx| SettingCategory::SpaceClaim.field_name(idx))
            .collect::<Vec<_>>();

        assert_eq!(
            field_names,
            vec![
                "sc_timeout",
                "sc_poll_interval",
                "sc_process_appear_timeout",
                "sc_gui_ready_timeout",
                "sc_gui_stable_delay",
                "sc_max_slots",
                "sc_persistent_enabled",
                "sc_oneshot_fallback_enabled",
            ]
        );

        assert!(SettingCategory::SpaceClaim.is_bool_field(6));
        assert!(SettingCategory::SpaceClaim.is_bool_field(7));

        let mut state = SettingsState::new();
        state.config = SettingsConfig::default();
        state.set_field_value(SettingCategory::SpaceClaim, 5, "2");
        state.set_field_value(SettingCategory::SpaceClaim, 6, "false");
        state.set_field_value(SettingCategory::SpaceClaim, 7, "false");

        assert_eq!(state.get_field_value(SettingCategory::SpaceClaim, 5), "2");
        assert_eq!(
            state.get_field_value(SettingCategory::SpaceClaim, 6),
            "false"
        );
        assert_eq!(
            state.get_field_value(SettingCategory::SpaceClaim, 7),
            "false"
        );
    }

    #[test]
    fn is_password_field_identifies_remote_connection_password() {
        // RemoteConnection.password is at idx 3
        assert!(SettingCategory::RemoteConnection.is_password_field(3));
        // Other fields in RemoteConnection are not password fields
        assert!(!SettingCategory::RemoteConnection.is_password_field(0)); // host
        assert!(!SettingCategory::RemoteConnection.is_password_field(1)); // port
        assert!(!SettingCategory::RemoteConnection.is_password_field(2)); // username
                                                                          // Other categories are never password fields
        assert!(!SettingCategory::LocalPaths.is_password_field(0));
        assert!(!SettingCategory::GlobalSettings.is_password_field(0));
    }

    #[test]
    fn is_bool_field_identifies_solidworks_boolean_fields() {
        // SolidWorks bool fields: idx 1 (sw_close_doc_on_finish), idx 2 (sw_visible)
        assert!(SettingCategory::SolidWorks.is_bool_field(1));
        assert!(SettingCategory::SolidWorks.is_bool_field(2));
        // Non-bool fields in SolidWorks
        assert!(!SettingCategory::SolidWorks.is_bool_field(0)); // sw_macro_timeout
        assert!(!SettingCategory::SolidWorks.is_bool_field(3)); // sw_startup
                                                                // Other categories are never bool fields
        assert!(!SettingCategory::LocalPaths.is_bool_field(0));
        assert!(!SettingCategory::GlobalSettings.is_bool_field(0));
    }

    #[test]
    fn field_count_matches_all_categories() {
        // Verify field_count is consistent across all categories
        assert_eq!(SettingCategory::LocalPaths.field_count(), 6);
        assert_eq!(SettingCategory::RemoteConnection.field_count(), 4);
        assert_eq!(SettingCategory::RemoteDirs.field_count(), 10);
        assert_eq!(SettingCategory::StepPatterns.field_count(), 6);
        assert_eq!(SettingCategory::SolidWorks.field_count(), 5);
        assert_eq!(SettingCategory::SpaceClaim.field_count(), 8);
        assert_eq!(SettingCategory::Meshing.field_count(), 2);
        assert_eq!(SettingCategory::Solver.field_count(), 3);
        assert_eq!(SettingCategory::PostProcess.field_count(), 4);
        assert_eq!(SettingCategory::GlobalSettings.field_count(), 7);
    }

    #[test]
    fn remote_dirs_do_not_expose_animation_dir() {
        assert_eq!(SettingCategory::RemoteDirs.field_count(), 10);
        let visible_fields = (0..SettingCategory::RemoteDirs.field_count())
            .map(|idx| SettingCategory::RemoteDirs.field_name(idx))
            .collect::<Vec<_>>();
        assert!(!visible_fields.contains(&"animation_dir"));
        assert_eq!(SettingCategory::RemoteDirs.field_name(6), "flag_dir");
        assert_eq!(
            SettingCategory::RemoteDirs.field_full_name(6),
            "remote_config.flag_dir"
        );
    }

    #[test]
    fn postprocess_settings_expose_animation_dir() {
        assert_eq!(SettingCategory::PostProcess.field_count(), 4);
        assert_eq!(SettingCategory::PostProcess.field_name(2), "animation_dir");
        assert_eq!(
            SettingCategory::PostProcess.field_full_name(2),
            "postprocess.animation_dir"
        );
        assert_eq!(
            SettingCategory::PostProcess.display_label(2),
            "动画输出目录"
        );
        assert!(SettingCategory::PostProcess.is_path_field(2));

        let mut state = SettingsState::new();
        state.config = SettingsConfig::default();
        state.set_field_value(SettingCategory::PostProcess, 2, r"D:\animation");
        assert_eq!(
            state.get_field_value(SettingCategory::PostProcess, 2),
            r"D:\animation"
        );
        assert_eq!(state.config.postprocess.animation_dir, r"D:\animation");
    }

    #[test]
    fn postprocess_settings_expose_user_editable_paths_and_metrics() {
        assert_eq!(SettingCategory::ALL.len(), 10);
        assert_eq!(SettingCategory::PostProcess.field_count(), 4);
        assert_eq!(
            SettingCategory::PostProcess.field_name(0),
            "postprocess_timeout"
        );
        assert_eq!(SettingCategory::PostProcess.field_name(1), "output_dir");
        assert_eq!(SettingCategory::PostProcess.field_name(2), "animation_dir");
        assert_eq!(SettingCategory::PostProcess.field_name(3), "metrics_dir");
        assert_eq!(
            SettingCategory::PostProcess.field_full_name(3),
            "postprocess.metrics_dir"
        );
        assert!(SettingCategory::PostProcess.is_path_field(1));
        assert!(SettingCategory::PostProcess.is_path_field(2));
        assert!(SettingCategory::PostProcess.is_path_field(3));

        let visible_fields = (0..SettingCategory::PostProcess.field_count())
            .map(|idx| SettingCategory::PostProcess.field_name(idx))
            .collect::<Vec<_>>();
        assert!(visible_fields.contains(&"animation_dir"));
        assert!(!visible_fields.contains(&"exit_to_throat_area_ratio"));
        assert!(!visible_fields.contains(&"cstar_reference"));

        let mut state = SettingsState::new();
        state.config = SettingsConfig::default();
        state.set_field_value(SettingCategory::PostProcess, 0, "4200");
        state.set_field_value(SettingCategory::PostProcess, 1, r"D:\post\output");
        state.set_field_value(SettingCategory::PostProcess, 2, r"D:\post\animation");
        state.set_field_value(SettingCategory::PostProcess, 3, r"D:\post\metrics");

        assert_eq!(state.config.postprocess.postprocess_timeout, 4200);
        assert_eq!(state.config.postprocess.output_dir, r"D:\post\output");
        assert_eq!(state.config.postprocess.animation_dir, r"D:\post\animation");
        assert_eq!(state.config.postprocess.metrics_dir, r"D:\post\metrics");
        assert_eq!(
            state.config.postprocess.exit_to_throat_area_ratio,
            7.427276607
        );
        assert_eq!(state.config.postprocess.cstar_reference, 1830.4);
    }

    #[test]
    fn display_labels_fit_settings_label_column() {
        const SETTINGS_LABEL_WIDTH: usize = 20;
        for cat in SettingCategory::ALL {
            for idx in 0..cat.field_count() {
                let label = cat.display_label(idx);
                let width = unicode_width::UnicodeWidthStr::width(label);
                assert!(
                    width <= SETTINGS_LABEL_WIDTH,
                    "{:?}.{} label `{}` width {} exceeds {}",
                    cat,
                    idx,
                    label,
                    width,
                    SETTINGS_LABEL_WIDTH
                );
            }
        }
    }

    #[test]
    fn field_name_covers_all_indices_for_each_category() {
        // Verify that field_name returns non-empty strings for all valid indices
        for cat in SettingCategory::ALL {
            for idx in 0..cat.field_count() {
                let name = cat.field_name(idx);
                assert!(
                    !name.is_empty(),
                    "field_name({:?}, {}) returned empty string",
                    cat,
                    idx
                );
            }
        }
    }

    #[test]
    fn display_label_covers_all_indices_for_each_category() {
        // Verify that display_label returns non-empty strings for all valid indices
        for cat in SettingCategory::ALL {
            for idx in 0..cat.field_count() {
                let label = cat.display_label(idx);
                assert!(
                    !label.is_empty(),
                    "display_label({:?}, {}) returned empty string",
                    cat,
                    idx
                );
            }
        }
    }

    #[test]
    fn field_full_name_format_is_correct() {
        // Verify field_full_name format: "section.field_name"
        assert_eq!(
            SettingCategory::LocalPaths.field_full_name(0),
            "local_paths.sw_exe"
        );
        assert_eq!(
            SettingCategory::RemoteConnection.field_full_name(0),
            "remote_config.host"
        );
        assert_eq!(
            SettingCategory::SolidWorks.field_full_name(0),
            "solidworks.sw_macro_timeout"
        );
        assert_eq!(
            SettingCategory::GlobalSettings.field_full_name(0),
            "global_settings.watchdog_interval"
        );
    }

    #[test]
    #[should_panic(expected = "invalid field index")]
    fn field_name_panics_on_out_of_bounds_index() {
        SettingCategory::LocalPaths.field_name(999);
    }

    #[test]
    #[should_panic(expected = "invalid field index")]
    fn display_label_panics_on_out_of_bounds_index() {
        SettingCategory::LocalPaths.display_label(999);
    }
}
