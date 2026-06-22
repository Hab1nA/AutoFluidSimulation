use std::collections::HashMap;
use std::fs;
use std::io::Write;
use std::path::{Path, PathBuf};

use super::{SettingsConfig, WorkstationConfig};

pub fn config_file_path() -> PathBuf {
    crate::utils::resolve_project_dir().join("autofluid_config.toml")
}

pub fn load_config() -> Result<SettingsConfig, String> {
    let path = config_file_path();
    if !path.exists() {
        let mut config = SettingsConfig::default();
        config.apply_derived_defaults();
        return Ok(config);
    }
    let contents = fs::read_to_string(&path).map_err(|e| format!("读取配置文件失败: {}", e))?;
    let mut config: SettingsConfig =
        toml::from_str(&contents).map_err(|e| format!("解析配置文件失败: {}", e))?;
    config.apply_derived_defaults();
    Ok(config)
}

pub fn save_config(config: &SettingsConfig) -> Result<(), String> {
    let path = config_file_path();
    let toml_str = toml::to_string_pretty(config).map_err(|e| format!("序列化配置失败: {}", e))?;

    let tmp_path = path.with_extension("toml.tmp");
    let mut file =
        fs::File::create(&tmp_path).map_err(|e| format!("创建临时配置文件失败: {}", e))?;
    file.write_all(toml_str.as_bytes())
        .map_err(|e| format!("写入配置文件失败: {}", e))?;
    file.sync_all()
        .map_err(|e| format!("同步配置文件失败: {}", e))?;
    drop(file);
    replace_file(&tmp_path, &path)?;
    Ok(())
}

#[cfg(target_os = "windows")]
fn replace_file(tmp_path: &Path, path: &Path) -> Result<(), String> {
    use std::os::windows::ffi::OsStrExt;
    use windows_sys::Win32::Storage::FileSystem::{
        MoveFileExW, MOVEFILE_REPLACE_EXISTING, MOVEFILE_WRITE_THROUGH,
    };

    let tmp_wide: Vec<u16> = tmp_path.as_os_str().encode_wide().chain([0]).collect();
    let path_wide: Vec<u16> = path.as_os_str().encode_wide().chain([0]).collect();
    let result = unsafe {
        MoveFileExW(
            tmp_wide.as_ptr(),
            path_wide.as_ptr(),
            MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH,
        )
    };
    if result == 0 {
        Err(format!(
            "替换配置文件失败: {}",
            std::io::Error::last_os_error()
        ))
    } else {
        Ok(())
    }
}

#[cfg(not(target_os = "windows"))]
fn replace_file(tmp_path: &Path, path: &Path) -> Result<(), String> {
    fs::rename(tmp_path, path).map_err(|e| format!("替换配置文件失败: {}", e))
}

pub fn env_file_path() -> PathBuf {
    crate::utils::resolve_project_dir().join(".env")
}

fn read_env_values() -> HashMap<String, String> {
    let path = env_file_path();
    if !path.exists() {
        return HashMap::new();
    }
    match fs::read_to_string(&path) {
        Ok(contents) => contents
            .lines()
            .filter_map(|line| {
                let trimmed = line.trim();
                let (key, value) = trimmed.split_once('=')?;
                if key.is_empty() || key.starts_with('#') {
                    return None;
                }
                Some((key.to_string(), value.to_string()))
            })
            .collect(),
        Err(_) => HashMap::new(),
    }
}

fn workstation_env_token(workstation_id: &str) -> String {
    let mut token = String::new();
    let mut last_was_separator = false;
    for ch in workstation_id.chars() {
        if ch.is_ascii_alphanumeric() {
            token.push(ch.to_ascii_uppercase());
            last_was_separator = false;
        } else if !last_was_separator && !token.is_empty() {
            token.push('_');
            last_was_separator = true;
        }
    }
    while token.ends_with('_') {
        token.pop();
    }
    if token.is_empty() {
        "DEFAULT".to_string()
    } else {
        token
    }
}

fn workstation_password_env_key(workstation: &WorkstationConfig) -> String {
    let token = workstation_env_token(&workstation.id);
    format!("AUTOFLUID_{token}_SSH_PASSWORD")
}

fn legacy_workstation_password_env_key(workstation: &WorkstationConfig) -> String {
    let token = workstation_env_token(&workstation.id);
    format!("AUTOFLUID_{token}_PASSWORD")
}

pub fn apply_env_passwords(config: &mut SettingsConfig) {
    let env_values = read_env_values();
    if let Some(password) = env_values.get("AUTOFLUID_SSH_PASSWORD") {
        config.remote_config.password = password.clone();
    }
    for workstation in &mut config.workstations {
        let key = workstation_password_env_key(workstation);
        if let Some(password) = env_values.get(&key) {
            workstation.password = password.clone();
            if password.trim().is_empty() && !workstation.auth_method.eq_ignore_ascii_case("key") {
                workstation.auth_method = "none".to_string();
            } else if !password.trim().is_empty() {
                workstation.auth_method = "password".to_string();
            }
        }
    }
}

fn remove_env_line(contents: &mut String, key: &str) {
    let prefix = format!("{key}=");
    let mut rebuilt = String::with_capacity(contents.len());

    for segment in contents.split_inclusive('\n') {
        let line = segment.trim_end_matches(['\r', '\n']);
        let candidate = line.trim_start();
        if candidate.starts_with(&prefix) && !candidate.starts_with('#') {
            continue;
        }
        rebuilt.push_str(segment);
    }

    *contents = rebuilt;
}

fn replace_or_append_env_line(contents: &mut String, key: &str, value: &str) {
    let prefix = format!("{key}=");
    let new_line = format!("{prefix}{value}");
    let mut replaced = false;
    let mut rebuilt = String::with_capacity(contents.len() + new_line.len() + 1);

    for segment in contents.split_inclusive('\n') {
        let line = segment.trim_end_matches(['\r', '\n']);
        let newline = &segment[line.len()..];
        let candidate = line.trim_start();
        if !replaced && candidate.starts_with(&prefix) && !candidate.starts_with('#') {
            rebuilt.push_str(&new_line);
            rebuilt.push_str(newline);
            replaced = true;
        } else {
            rebuilt.push_str(segment);
        }
    }

    if !replaced {
        if !rebuilt.is_empty() && !rebuilt.ends_with('\n') {
            rebuilt.push('\n');
        }
        rebuilt.push_str(&new_line);
        rebuilt.push('\n');
    }

    *contents = rebuilt;
}

pub fn write_env_passwords(config: &SettingsConfig) -> Result<(), String> {
    let path = env_file_path();
    let mut contents = if path.exists() {
        fs::read_to_string(&path).unwrap_or_default()
    } else {
        String::new()
    };

    replace_or_append_env_line(
        &mut contents,
        "AUTOFLUID_SSH_PASSWORD",
        &config.remote_config.password,
    );
    for workstation in &config.workstations {
        let legacy_key = legacy_workstation_password_env_key(workstation);
        remove_env_line(&mut contents, &legacy_key);
        let key = workstation_password_env_key(workstation);
        replace_or_append_env_line(&mut contents, &key, &workstation.password);
    }

    let mut file = fs::File::create(&path).map_err(|e| format!("写入 .env 文件失败: {}", e))?;
    file.write_all(contents.as_bytes())
        .map_err(|e| format!("写入 .env 文件失败: {}", e))?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::Mutex;

    fn cwd_lock() -> &'static Mutex<()> {
        &crate::TEST_ENV_LOCK
    }

    fn unique_temp_project_dir() -> PathBuf {
        std::env::temp_dir().join(format!(
            "autofluid-tui-config-{}",
            crate::generate_request_id()
        ))
    }

    #[test]
    fn workstation_env_token_normalizes_edge_cases() {
        let cases = [
            ("", "DEFAULT"),
            ("  ", "DEFAULT"),
            ("___", "DEFAULT"),
            ("WS-A", "WS_A"),
            ("WS--A", "WS_A"),
            ("-WS-A", "WS_A"),
            ("WS_A_", "WS_A"),
            ("my__ws", "MY_WS"),
            ("WS 01", "WS_01"),
            ("ws.a", "WS_A"),
            ("alpha/beta", "ALPHA_BETA"),
        ];

        for (input, expected) in cases {
            assert_eq!(workstation_env_token(input), expected);
        }
    }

    #[test]
    fn load_config_finds_project_root_when_started_from_tui_subdirectory() {
        let _guard = cwd_lock().lock().expect("lock cwd");
        let project_dir = unique_temp_project_dir();
        let nested_dir = project_dir
            .join("autofluid-tui")
            .join("target")
            .join("release");
        std::fs::create_dir_all(&nested_dir).expect("create nested dir");
        std::fs::write(project_dir.join("start_daemon.py"), "").expect("write project marker");
        std::fs::write(
            project_dir.join("autofluid_config.toml"),
            r#"
[local_paths]
sw_exe = 'C:\AutoFluid\Test\SLDWORKS.exe'
sw_model = ''
excel = ''
step_dir = ''
sc_exe = ''
scdoc_dir = ''
"#,
        )
        .expect("write config");

        let previous_dir = std::env::current_dir().expect("current dir");
        std::env::set_current_dir(&nested_dir).expect("set nested cwd");
        let config = load_config();
        std::env::set_current_dir(previous_dir).expect("restore cwd");
        let config = config.expect("load config from project root");

        assert_eq!(config.local_paths.sw_exe, r"C:\AutoFluid\Test\SLDWORKS.exe");

        let _ = std::fs::remove_dir_all(project_dir);
    }

    #[test]
    fn write_env_passwords_persists_per_workstation_passwords() {
        let _guard = cwd_lock().lock().expect("lock cwd");
        let project_dir = unique_temp_project_dir();
        std::fs::create_dir_all(&project_dir).expect("create project dir");
        std::fs::write(project_dir.join("start_daemon.py"), "").expect("write project marker");
        std::fs::write(
            project_dir.join(".env"),
            "AUTOFLUID_SERVER_MODE=server\nAUTOFLUID_SSH_PASSWORD=old\nAUTOFLUID_WS_A_PASSWORD=legacy-a\n",
        )
        .expect("write env");

        let previous_dir = std::env::current_dir().expect("current dir");
        std::env::set_current_dir(&project_dir).expect("set cwd");

        let mut config = SettingsConfig::default();
        config.remote_config.password = "legacy".to_string();
        config.workstations = vec![
            crate::settings::WorkstationConfig {
                id: "WS-A".to_string(),
                password: "secret-a".to_string(),
                ..Default::default()
            },
            crate::settings::WorkstationConfig {
                id: "WS-B".to_string(),
                password: String::new(),
                ..Default::default()
            },
            crate::settings::WorkstationConfig {
                id: "WS-C".to_string(),
                password: String::new(),
                ..Default::default()
            },
        ];

        let result = write_env_passwords(&config);
        std::env::set_current_dir(previous_dir).expect("restore cwd");
        result.expect("write passwords");

        let contents = std::fs::read_to_string(project_dir.join(".env")).expect("read env");
        assert!(contents.contains("AUTOFLUID_SERVER_MODE=server\n"));
        assert!(contents.contains("AUTOFLUID_SSH_PASSWORD=legacy\n"));
        assert!(contents.contains("AUTOFLUID_WS_A_SSH_PASSWORD=secret-a\n"));
        assert!(contents.contains("AUTOFLUID_WS_B_SSH_PASSWORD=\n"));
        assert!(contents.contains("AUTOFLUID_WS_C_SSH_PASSWORD=\n"));
        assert!(!contents.contains("AUTOFLUID_WS_A_PASSWORD=legacy-a\n"));

        let _ = std::fs::remove_dir_all(project_dir);
    }

    #[test]
    fn write_env_passwords_ignores_commented_matching_keys() {
        let _guard = cwd_lock().lock().expect("lock cwd");
        let project_dir = unique_temp_project_dir();
        std::fs::create_dir_all(&project_dir).expect("create project dir");
        std::fs::write(project_dir.join("start_daemon.py"), "").expect("write project marker");
        std::fs::write(
            project_dir.join(".env"),
            "#AUTOFLUID_WS_A_SSH_PASSWORD=old\n",
        )
        .expect("write env");

        let previous_dir = std::env::current_dir().expect("current dir");
        std::env::set_current_dir(&project_dir).expect("set cwd");

        let mut config = SettingsConfig::default();
        config.workstations = vec![crate::settings::WorkstationConfig {
            id: "WS-A".to_string(),
            password: "secret-a".to_string(),
            ..Default::default()
        }];

        let result = write_env_passwords(&config);
        std::env::set_current_dir(previous_dir).expect("restore cwd");
        result.expect("write passwords");

        let contents = std::fs::read_to_string(project_dir.join(".env")).expect("read env");
        assert!(contents.contains("#AUTOFLUID_WS_A_SSH_PASSWORD=old\n"));
        assert!(contents.contains("AUTOFLUID_WS_A_SSH_PASSWORD=secret-a\n"));

        let _ = std::fs::remove_dir_all(project_dir);
    }

    #[test]
    fn apply_env_passwords_reads_blank_workstation_passwords() {
        let _guard = cwd_lock().lock().expect("lock cwd");
        let project_dir = unique_temp_project_dir();
        std::fs::create_dir_all(&project_dir).expect("create project dir");
        std::fs::write(project_dir.join("start_daemon.py"), "").expect("write project marker");
        std::fs::write(
            project_dir.join(".env"),
            "AUTOFLUID_SSH_PASSWORD=legacy\nAUTOFLUID_WS_A_SSH_PASSWORD=secret-a\nAUTOFLUID_WS_B_SSH_PASSWORD=\n",
        )
        .expect("write env");

        let previous_dir = std::env::current_dir().expect("current dir");
        std::env::set_current_dir(&project_dir).expect("set cwd");

        let mut config = SettingsConfig::default();
        config.workstations = vec![
            crate::settings::WorkstationConfig {
                id: "WS-A".to_string(),
                password: "toml-a".to_string(),
                ..Default::default()
            },
            crate::settings::WorkstationConfig {
                id: "WS-B".to_string(),
                password: "toml-b".to_string(),
                auth_method: "password".to_string(),
                ..Default::default()
            },
        ];

        apply_env_passwords(&mut config);
        std::env::set_current_dir(previous_dir).expect("restore cwd");

        assert_eq!(config.remote_config.password, "legacy");
        assert_eq!(config.workstations[0].password, "secret-a");
        assert_eq!(config.workstations[1].password, "");
        assert_eq!(config.workstations[1].auth_method, "none");

        let _ = std::fs::remove_dir_all(project_dir);
    }

    #[test]
    fn apply_env_passwords_ignores_legacy_workstation_password_key() {
        let _guard = cwd_lock().lock().expect("lock cwd");
        let project_dir = unique_temp_project_dir();
        std::fs::create_dir_all(&project_dir).expect("create project dir");
        std::fs::write(project_dir.join("start_daemon.py"), "").expect("write project marker");
        std::fs::write(
            project_dir.join(".env"),
            "AUTOFLUID_WS_A_PASSWORD=secret-a\n",
        )
        .expect("write env");

        let previous_dir = std::env::current_dir().expect("current dir");
        std::env::set_current_dir(&project_dir).expect("set cwd");

        let mut config = SettingsConfig::default();
        config.workstations = vec![crate::settings::WorkstationConfig {
            id: "WS-A".to_string(),
            password: "toml-a".to_string(),
            ..Default::default()
        }];

        apply_env_passwords(&mut config);
        std::env::set_current_dir(previous_dir).expect("restore cwd");

        assert_eq!(config.workstations[0].password, "toml-a");

        let _ = std::fs::remove_dir_all(project_dir);
    }
}
