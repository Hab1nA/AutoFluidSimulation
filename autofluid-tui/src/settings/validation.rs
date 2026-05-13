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
    validate_engine_config(config, &mut errors);
    errors
}

fn validate_local_paths(config: &SettingsConfig, errors: &mut Vec<ValidationError>) {
    let required_executables = [
        ("local_paths.sw_exe", &config.local_paths.sw_exe, false),
        ("local_paths.sw_model", &config.local_paths.sw_model, true),
        ("local_paths.excel", &config.local_paths.excel, true),
        ("local_paths.sc_exe", &config.local_paths.sc_exe, false),
        ("local_paths.sc_script", &config.local_paths.sc_script, false),
    ];

    for (name, path, is_required) in &required_executables {
        if path.is_empty() {
            if *is_required {
                errors.push(ValidationError {
                    field_name: name.to_string(),
                    message: "路径不能为空".to_string(),
                    severity: Severity::Error,
                });
            }
        } else if !Path::new(path).exists() {
            let sev = if *is_required { Severity::Error } else { Severity::Warning };
            errors.push(ValidationError {
                field_name: name.to_string(),
                message: "文件不存在".to_string(),
                severity: sev,
            });
        }
    }

    let dirs = [
        ("local_paths.step_dir", &config.local_paths.step_dir),
        ("local_paths.scdoc_dir", &config.local_paths.scdoc_dir),
        ("local_paths.log_dir", &config.local_paths.log_dir),
        ("local_paths.data_dir", &config.local_paths.data_dir),
    ];
    for (name, path) in &dirs {
        if path.is_empty() {
            errors.push(ValidationError {
                field_name: name.to_string(),
                message: "目录路径为空 (将在首次使用时创建)".to_string(),
                severity: Severity::Warning,
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

    if config.remote_config.password.is_empty() {
        errors.push(ValidationError {
            field_name: "remote_config.password".to_string(),
            message: "SSH密码未设置".to_string(),
            severity: Severity::Warning,
        });
    }
}

fn validate_remote_dirs(config: &SettingsConfig, errors: &mut Vec<ValidationError>) {
    let remote_dirs = [
        ("remote_config.root_dir", &config.remote_config.root_dir),
        ("remote_config.scdoc_dir", &config.remote_config.scdoc_dir),
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
    let patterns = [
        ("step_file_patterns.SW", &config.step_file_patterns.sw, true),
        ("step_file_patterns.SC", &config.step_file_patterns.sc, true),
        ("step_file_patterns.Transfer", &config.step_file_patterns.transfer.clone().unwrap_or_default(), false),
        ("step_file_patterns.Meshing", &config.step_file_patterns.meshing, true),
        ("step_file_patterns.Solver", &config.step_file_patterns.solver, true),
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

fn validate_engine_config(config: &SettingsConfig, errors: &mut Vec<ValidationError>) {
    if config.engine_config.watchdog_interval <= 0.0 {
        errors.push(ValidationError {
            field_name: "engine_config.watchdog_interval".to_string(),
            message: "必须大于 0".to_string(),
            severity: Severity::Error,
        });
    }

    let timeouts = [
        ("engine_config.sw_macro_timeout", config.engine_config.sw_macro_timeout),
        ("engine_config.sc_timeout", config.engine_config.sc_timeout),
        ("engine_config.transfer_timeout", config.engine_config.transfer_timeout),
        ("engine_config.meshing_timeout", config.engine_config.meshing_timeout),
        ("engine_config.solver_timeout", config.engine_config.solver_timeout),
    ];
    for (name, val) in &timeouts {
        if *val == 0 {
            errors.push(ValidationError {
                field_name: name.to_string(),
                message: "超时值必须大于 0".to_string(),
                severity: Severity::Error,
            });
        }
    }

    if config.engine_config.max_retries == 0 {
        errors.push(ValidationError {
            field_name: "engine_config.max_retries".to_string(),
            message: "重试次数至少为 1".to_string(),
            severity: Severity::Error,
        });
    }

    if config.engine_config.state_refresh_interval <= 0.0 {
        errors.push(ValidationError {
            field_name: "engine_config.state_refresh_interval".to_string(),
            message: "必须大于 0".to_string(),
            severity: Severity::Error,
        });
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_valid_config_passes() {
        let config = SettingsConfig::default();
        let errors = validate_config(&config);
        let critical: Vec<_> = errors.iter().filter(|e| matches!(e.severity, Severity::Error)).collect();
        assert!(critical.is_empty(), "默认配置不应有严重错误: {:?}", critical);
    }

    #[test]
    fn test_invalid_port_reports_error() {
        let mut config = SettingsConfig::default();
        config.remote_config.port = 0;
        let errors = validate_config(&config);
        assert!(errors.iter().any(|e| e.field_name == "remote_config.port" && matches!(e.severity, Severity::Error)));
    }

    #[test]
    fn test_missing_placeholder_in_template() {
        let mut config = SettingsConfig::default();
        config.step_file_patterns.sw = "bad_pattern.step".to_string();
        let errors = validate_config(&config);
        assert!(errors.iter().any(|e| e.field_name == "step_file_patterns.SW" && matches!(e.severity, Severity::Error)));
    }

    #[test]
    fn test_zero_timeout_reports_error() {
        let mut config = SettingsConfig::default();
        config.engine_config.solver_timeout = 0;
        let errors = validate_config(&config);
        assert!(errors.iter().any(|e| e.field_name == "engine_config.solver_timeout" && matches!(e.severity, Severity::Error)));
    }

    #[test]
    fn test_empty_host_reports_error() {
        let mut config = SettingsConfig::default();
        config.remote_config.host = String::new();
        let errors = validate_config(&config);
        assert!(errors.iter().any(|e| e.field_name == "remote_config.host" && matches!(e.severity, Severity::Error)));
    }

    #[test]
    fn test_empty_username_reports_error() {
        let mut config = SettingsConfig::default();
        config.remote_config.username = String::new();
        let errors = validate_config(&config);
        assert!(errors.iter().any(|e| e.field_name == "remote_config.username" && matches!(e.severity, Severity::Error)));
    }
}
