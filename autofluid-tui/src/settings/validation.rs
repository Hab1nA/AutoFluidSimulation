use std::path::Path;

use super::SettingsConfig;

#[derive(Debug, Clone)]
pub struct ValidationError {
    pub field_name: String,
    pub message: String,
    pub severity: Severity,
}

#[derive(Debug, Clone, PartialEq)]
pub enum Severity {
    Error,
    Warning,
}

pub fn validate_config(config: &SettingsConfig) -> Vec<ValidationError> {
    let mut errors = Vec::new();
    validate_local_paths(config, &mut errors);
    validate_remote_connection(config, &mut errors);
    validate_remote_dirs(config, &mut errors);
    validate_step_patterns(config, &mut errors);
    validate_solidworks(config, &mut errors);
    validate_spaceclaim(config, &mut errors);
    validate_meshing(config, &mut errors);
    validate_solver(config, &mut errors);
    validate_postprocess(config, &mut errors);
    validate_global_settings(config, &mut errors);
    errors
}

fn validate_local_paths(config: &SettingsConfig, errors: &mut Vec<ValidationError>) {
    let executables = [
        ("local_paths.sw_exe", &config.local_paths.sw_exe, false),
        ("local_paths.sw_model", &config.local_paths.sw_model, true),
        ("local_paths.excel", &config.local_paths.excel, true),
        ("local_paths.sc_exe", &config.local_paths.sc_exe, false),
    ];

    for (name, path, is_required) in &executables {
        if path.is_empty() {
            if *is_required {
                errors.push(ValidationError {
                    field_name: name.to_string(),
                    message: "路径不能为空".to_string(),
                    severity: Severity::Error,
                });
            }
        } else if !Path::new(path).exists() {
            errors.push(ValidationError {
                field_name: name.to_string(),
                message: "文件不存在".to_string(),
                severity: Severity::Error,
            });
        }
    }

    let dirs = [
        ("local_paths.step_dir", &config.local_paths.step_dir),
        ("local_paths.scdoc_dir", &config.local_paths.scdoc_dir),
    ];
    for (name, path) in &dirs {
        if path.is_empty() {
            errors.push(ValidationError {
                field_name: name.to_string(),
                message: "目录路径为空 (将在首次使用时创建)".to_string(),
                severity: Severity::Warning,
            });
        } else if !Path::new(path).is_dir() {
            errors.push(ValidationError {
                field_name: name.to_string(),
                message: "目录不存在".to_string(),
                severity: Severity::Error,
            });
        }
    }
}

fn validate_remote_connection(config: &SettingsConfig, errors: &mut Vec<ValidationError>) {
    if config.remote_config.host.is_empty() {
        errors.push(ValidationError {
            field_name: "remote_config.host".to_string(),
            message: "主机地址不能为空".to_string(),
            severity: Severity::Error,
        });
    }

    if config.remote_config.port == 0 {
        errors.push(ValidationError {
            field_name: "remote_config.port".to_string(),
            message: "端口号必须在 1-65535 之间".to_string(),
            severity: Severity::Error,
        });
    }

    if config.remote_config.username.is_empty() {
        errors.push(ValidationError {
            field_name: "remote_config.username".to_string(),
            message: "用户名不能为空".to_string(),
            severity: Severity::Error,
        });
    }
}

fn validate_remote_dirs(config: &SettingsConfig, errors: &mut Vec<ValidationError>) {
    let remote_dirs = [
        (
            "remote_config.scripts_dir",
            &config.remote_config.scripts_dir,
        ),
        (
            "remote_config.working_dir",
            &config.remote_config.working_dir,
        ),
        ("remote_config.scdoc_dir", &config.remote_config.scdoc_dir),
        (
            "remote_config.ref_files_dir",
            &config.remote_config.ref_files_dir,
        ),
        ("remote_config.msh_dir", &config.remote_config.msh_dir),
        ("remote_config.result_dir", &config.remote_config.result_dir),
    ];
    for (name, path) in &remote_dirs {
        if path.is_empty() {
            errors.push(ValidationError {
                field_name: name.to_string(),
                message: "远程路径为空".to_string(),
                severity: Severity::Warning,
            });
        }
    }

    if config.remote_config.conda_env.is_empty() {
        errors.push(ValidationError {
            field_name: "remote_config.conda_env".to_string(),
            message: "Conda环境名称为空".to_string(),
            severity: Severity::Warning,
        });
    }
}

fn validate_step_patterns(config: &SettingsConfig, errors: &mut Vec<ValidationError>) {
    let patterns: [(&str, &str, bool); 6] = [
        ("step_file_patterns.sw", &config.step_file_patterns.sw, true),
        ("step_file_patterns.sc", &config.step_file_patterns.sc, true),
        (
            "step_file_patterns.meshing",
            &config.step_file_patterns.meshing,
            true,
        ),
        (
            "step_file_patterns.solver",
            &config.step_file_patterns.solver,
            true,
        ),
        (
            "step_file_patterns.solverdata",
            &config.step_file_patterns.solver_dat,
            true,
        ),
        (
            "step_file_patterns.postprocess",
            &config.step_file_patterns.postprocess,
            true,
        ),
    ];
    for (name, pattern, requires_placeholder) in &patterns {
        if *requires_placeholder && !pattern.contains("{config}") {
            errors.push(ValidationError {
                field_name: name.to_string(),
                message: "模板必须包含 {config} 占位符".to_string(),
                severity: Severity::Error,
            });
        }
    }
}

fn validate_solidworks(config: &SettingsConfig, errors: &mut Vec<ValidationError>) {
    if config.solidworks.sw_macro_timeout == 0 {
        errors.push(ValidationError {
            field_name: "solidworks.sw_macro_timeout".to_string(),
            message: "超时值必须大于 0".to_string(),
            severity: Severity::Error,
        });
    }
    if config.solidworks.sw_startup == 0 {
        errors.push(ValidationError {
            field_name: "solidworks.sw_startup".to_string(),
            message: "超时值必须大于 0".to_string(),
            severity: Severity::Error,
        });
    }
    if config.solidworks.sw_dispatch_startup_delay == 0 {
        errors.push(ValidationError {
            field_name: "solidworks.sw_dispatch_startup_delay".to_string(),
            message: "超时值必须大于 0".to_string(),
            severity: Severity::Error,
        });
    }
}

fn validate_spaceclaim(config: &SettingsConfig, errors: &mut Vec<ValidationError>) {
    if config.spaceclaim.sc_timeout == 0 {
        errors.push(ValidationError {
            field_name: "spaceclaim.sc_timeout".to_string(),
            message: "超时值必须大于 0".to_string(),
            severity: Severity::Error,
        });
    }
    if config.spaceclaim.sc_poll_interval <= 0.0 {
        errors.push(ValidationError {
            field_name: "spaceclaim.sc_poll_interval".to_string(),
            message: "轮询间隔必须大于 0".to_string(),
            severity: Severity::Error,
        });
    }
    if config.spaceclaim.sc_process_appear_timeout < 30
        || config.spaceclaim.sc_process_appear_timeout > 600
    {
        errors.push(ValidationError {
            field_name: "spaceclaim.sc_process_appear_timeout".to_string(),
            message: "应在 30-600 秒之间".to_string(),
            severity: Severity::Warning,
        });
    }
    if config.spaceclaim.sc_gui_ready_timeout < 10 || config.spaceclaim.sc_gui_ready_timeout > 120 {
        errors.push(ValidationError {
            field_name: "spaceclaim.sc_gui_ready_timeout".to_string(),
            message: "应在 10-120 秒之间".to_string(),
            severity: Severity::Warning,
        });
    }
    if config.spaceclaim.sc_gui_stable_delay > 60 {
        errors.push(ValidationError {
            field_name: "spaceclaim.sc_gui_stable_delay".to_string(),
            message: "不应超过 60 秒".to_string(),
            severity: Severity::Warning,
        });
    }
    if config.spaceclaim.sc_max_slots == 0 {
        errors.push(ValidationError {
            field_name: "spaceclaim.sc_max_slots".to_string(),
            message: "槽位数必须大于 0".to_string(),
            severity: Severity::Error,
        });
    }
}

fn validate_meshing(config: &SettingsConfig, errors: &mut Vec<ValidationError>) {
    if config.meshing.meshing_timeout == 0 {
        errors.push(ValidationError {
            field_name: "meshing.meshing_timeout".to_string(),
            message: "超时值必须大于 0".to_string(),
            severity: Severity::Error,
        });
    }
    if config.meshing.meshing_processor_count == 0 {
        errors.push(ValidationError {
            field_name: "meshing.meshing_processor_count".to_string(),
            message: "网格核心数至少为 1".to_string(),
            severity: Severity::Error,
        });
    } else if config.meshing.meshing_processor_count > 32 {
        errors.push(ValidationError {
            field_name: "meshing.meshing_processor_count".to_string(),
            message: "Fluent Meshing 高核心数可能导致体网格阶段不稳定".to_string(),
            severity: Severity::Warning,
        });
    }
}

fn validate_solver(config: &SettingsConfig, errors: &mut Vec<ValidationError>) {
    if config.solver.solver_timeout == 0 {
        errors.push(ValidationError {
            field_name: "solver.solver_timeout".to_string(),
            message: "超时值必须大于 0".to_string(),
            severity: Severity::Error,
        });
    }
    if config.solver.solver_processor_count == 0 {
        errors.push(ValidationError {
            field_name: "solver.solver_processor_count".to_string(),
            message: "求解核心数至少为 1".to_string(),
            severity: Severity::Error,
        });
    } else if !(64..=128).contains(&config.solver.solver_processor_count) {
        errors.push(ValidationError {
            field_name: "solver.solver_processor_count".to_string(),
            message: "Solver 建议使用 64-128 核".to_string(),
            severity: Severity::Warning,
        });
    }
    if config.solver.solver_iteration_count == 0 {
        errors.push(ValidationError {
            field_name: "solver.solver_iteration_count".to_string(),
            message: "迭代次数至少为 1".to_string(),
            severity: Severity::Error,
        });
    } else if config.solver.solver_iteration_count > 100_000 {
        errors.push(ValidationError {
            field_name: "solver.solver_iteration_count".to_string(),
            message: "迭代次数异常偏高（> 100000）".to_string(),
            severity: Severity::Warning,
        });
    }
}

fn validate_postprocess(config: &SettingsConfig, errors: &mut Vec<ValidationError>) {
    let remote_dirs = [
        ("postprocess.output_dir", &config.postprocess.output_dir),
        (
            "postprocess.animation_dir",
            &config.postprocess.animation_dir,
        ),
        ("postprocess.metrics_dir", &config.postprocess.metrics_dir),
    ];
    for (name, path) in &remote_dirs {
        if path.is_empty() {
            errors.push(ValidationError {
                field_name: name.to_string(),
                message: "远程路径为空".to_string(),
                severity: Severity::Warning,
            });
        }
    }
    if config.postprocess.postprocess_timeout == 0 {
        errors.push(ValidationError {
            field_name: "postprocess.postprocess_timeout".to_string(),
            message: "超时值必须大于 0".to_string(),
            severity: Severity::Error,
        });
    }
}

fn validate_global_settings(config: &SettingsConfig, errors: &mut Vec<ValidationError>) {
    if config.global_settings.watchdog_interval <= 0.0 {
        errors.push(ValidationError {
            field_name: "global_settings.watchdog_interval".to_string(),
            message: "必须大于 0".to_string(),
            severity: Severity::Error,
        });
    }
    if config.global_settings.transfer_timeout == 0 {
        errors.push(ValidationError {
            field_name: "global_settings.transfer_timeout".to_string(),
            message: "超时值必须大于 0".to_string(),
            severity: Severity::Error,
        });
    }
    if config.global_settings.max_retries == 0 {
        errors.push(ValidationError {
            field_name: "global_settings.max_retries".to_string(),
            message: "重试次数至少为 1".to_string(),
            severity: Severity::Error,
        });
    }
    if config.global_settings.state_refresh_interval <= 0.0 {
        errors.push(ValidationError {
            field_name: "global_settings.state_refresh_interval".to_string(),
            message: "必须大于 0".to_string(),
            severity: Severity::Error,
        });
    }
    if config.global_settings.ssh_connection == 0 {
        errors.push(ValidationError {
            field_name: "global_settings.ssh_connection".to_string(),
            message: "超时值必须大于 0".to_string(),
            severity: Severity::Error,
        });
    }
    if config.global_settings.dir_recursion_limit == 0 {
        errors.push(ValidationError {
            field_name: "global_settings.dir_recursion_limit".to_string(),
            message: "递归深度限制必须大于 0".to_string(),
            severity: Severity::Error,
        });
    }
    if config.global_settings.ssh_upload_max_retries == 0 {
        errors.push(ValidationError {
            field_name: "global_settings.ssh_upload_max_retries".to_string(),
            message: "上传重试次数至少为 1".to_string(),
            severity: Severity::Error,
        });
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_valid_config_passes() {
        let mut config = SettingsConfig::default();
        // 填充所有必需的本地路径字段（用当前可执行文件代替不存在的路径）
        let exe = std::env::current_exe()
            .unwrap()
            .to_string_lossy()
            .to_string();
        config.local_paths.sw_exe = exe.clone();
        config.local_paths.sw_model = exe.clone();
        config.local_paths.excel = exe.clone();
        config.local_paths.sc_exe = exe.clone();
        config.local_paths.step_dir = std::env::temp_dir().to_string_lossy().to_string();
        config.local_paths.scdoc_dir = std::env::temp_dir().to_string_lossy().to_string();
        // 填充远程连接字段
        config.remote_config.host = "127.0.0.1".to_string();
        config.remote_config.username = "test".to_string();
        // 远程目录留空（不校验本地存在性）
        config.remote_config.conda_exe = String::new();
        config.remote_config.mpi_bin_dir = String::new();
        let errors = validate_config(&config);
        let critical: Vec<_> = errors
            .iter()
            .filter(|e| matches!(e.severity, Severity::Error))
            .collect();
        assert!(
            critical.is_empty(),
            "完整配置不应有严重错误: {:?}",
            critical
        );
    }

    #[test]
    fn test_empty_defaults_have_errors() {
        let config = SettingsConfig::default();
        let errors = validate_config(&config);
        let critical: Vec<_> = errors
            .iter()
            .filter(|e| matches!(e.severity, Severity::Error))
            .collect();
        assert!(!critical.is_empty(), "空默认配置应有严重错误（路径未填写）");
    }

    #[test]
    fn test_invalid_port_reports_error() {
        let mut config = SettingsConfig::default();
        config.remote_config.port = 0;
        let errors = validate_config(&config);
        assert!(
            errors
                .iter()
                .any(|e| e.field_name == "remote_config.port"
                    && matches!(e.severity, Severity::Error))
        );
    }

    #[test]
    fn test_missing_placeholder_in_template() {
        let mut config = SettingsConfig::default();
        config.step_file_patterns.sw = "bad_pattern.step".to_string();
        let errors = validate_config(&config);
        assert!(errors
            .iter()
            .any(|e| e.field_name == "step_file_patterns.sw"
                && matches!(e.severity, Severity::Error)));
    }

    #[test]
    fn test_zero_timeout_reports_error() {
        let mut config = SettingsConfig::default();
        config.solver.solver_timeout = 0;
        let errors = validate_config(&config);
        assert!(errors
            .iter()
            .any(|e| e.field_name == "solver.solver_timeout"
                && matches!(e.severity, Severity::Error)));
    }

    #[test]
    fn test_zero_meshing_processor_count_reports_error() {
        let mut config = SettingsConfig::default();
        config.meshing.meshing_processor_count = 0;
        let errors = validate_config(&config);
        assert!(errors
            .iter()
            .any(|e| e.field_name == "meshing.meshing_processor_count"
                && matches!(e.severity, Severity::Error)));
    }

    #[test]
    fn test_zero_solver_processor_count_reports_error() {
        let mut config = SettingsConfig::default();
        config.solver.solver_processor_count = 0;
        let errors = validate_config(&config);
        assert!(errors
            .iter()
            .any(|e| e.field_name == "solver.solver_processor_count"
                && matches!(e.severity, Severity::Error)));
    }

    #[test]
    fn test_low_solver_processor_count_reports_warning() {
        let mut config = SettingsConfig::default();
        config.solver.solver_processor_count = 32;
        let errors = validate_config(&config);
        assert!(errors
            .iter()
            .any(|e| e.field_name == "solver.solver_processor_count"
                && matches!(e.severity, Severity::Warning)));
    }

    #[test]
    fn test_empty_host_reports_error() {
        let mut config = SettingsConfig::default();
        config.remote_config.host = String::new();
        let errors = validate_config(&config);
        assert!(
            errors
                .iter()
                .any(|e| e.field_name == "remote_config.host"
                    && matches!(e.severity, Severity::Error))
        );
    }

    #[test]
    fn test_empty_username_reports_error() {
        let mut config = SettingsConfig::default();
        config.remote_config.username = String::new();
        let errors = validate_config(&config);
        assert!(errors
            .iter()
            .any(|e| e.field_name == "remote_config.username"
                && matches!(e.severity, Severity::Error)));
    }

    #[test]
    fn test_postprocess_visible_empty_paths_warn_and_zero_timeout_errors() {
        let mut config = SettingsConfig::default();
        config.postprocess.postprocess_timeout = 0;
        config.postprocess.output_dir = String::new();
        config.postprocess.animation_dir = String::new();
        config.postprocess.metrics_dir = String::new();
        config.postprocess.exit_to_throat_area_ratio = 0.0;
        config.postprocess.cstar_reference = 0.0;

        let errors = validate_config(&config);

        assert!(errors.iter().any(|e| {
            e.field_name == "postprocess.output_dir" && matches!(e.severity, Severity::Warning)
        }));
        assert!(errors.iter().any(|e| {
            e.field_name == "postprocess.metrics_dir" && matches!(e.severity, Severity::Warning)
        }));
        assert!(errors.iter().any(|e| {
            e.field_name == "postprocess.postprocess_timeout"
                && matches!(e.severity, Severity::Error)
        }));
        assert!(errors.iter().any(|e| {
            e.field_name == "postprocess.animation_dir" && matches!(e.severity, Severity::Warning)
        }));
        assert!(!errors
            .iter()
            .any(|e| e.field_name == "postprocess.exit_to_throat_area_ratio"));
        assert!(!errors
            .iter()
            .any(|e| e.field_name == "postprocess.cstar_reference"));
    }
}
