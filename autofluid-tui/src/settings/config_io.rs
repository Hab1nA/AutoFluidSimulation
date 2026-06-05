use std::fs;
use std::io::Write;
use std::path::{Path, PathBuf};

use super::SettingsConfig;

pub fn config_file_path() -> PathBuf {
    std::env::current_dir()
        .unwrap_or_default()
        .join("autofluid_config.toml")
}

pub fn load_config() -> Result<SettingsConfig, String> {
    let path = config_file_path();
    if !path.exists() {
        return Ok(SettingsConfig::default());
    }
    let contents = fs::read_to_string(&path).map_err(|e| format!("读取配置文件失败: {}", e))?;
    toml::from_str(&contents).map_err(|e| format!("解析配置文件失败: {}", e))
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
    std::env::current_dir().unwrap_or_default().join(".env")
}

pub fn read_env_password() -> String {
    let path = env_file_path();
    if !path.exists() {
        return String::new();
    }
    match fs::read_to_string(&path) {
        Ok(contents) => {
            for line in contents.lines() {
                let trimmed = line.trim();
                if let Some(value) = trimmed.strip_prefix("AUTOFLUID_SSH_PASSWORD=") {
                    return value.to_string();
                }
            }
            String::new()
        }
        Err(_) => String::new(),
    }
}

pub fn write_env_password(password: &str) -> Result<(), String> {
    let path = env_file_path();
    let mut contents = if path.exists() {
        fs::read_to_string(&path).unwrap_or_default()
    } else {
        String::new()
    };

    let key = "AUTOFLUID_SSH_PASSWORD=";
    let new_line = format!("{}{}", key, password);

    if let Some(line_start) = contents.find(key) {
        let line_end = contents[line_start..]
            .find('\n')
            .map(|i| line_start + i)
            .unwrap_or(contents.len());
        contents.replace_range(line_start..line_end, &new_line);
    } else {
        if !contents.is_empty() && !contents.ends_with('\n') {
            contents.push('\n');
        }
        contents.push_str(&new_line);
        contents.push('\n');
    }

    let mut file = fs::File::create(&path).map_err(|e| format!("写入 .env 文件失败: {}", e))?;
    file.write_all(contents.as_bytes())
        .map_err(|e| format!("写入 .env 文件失败: {}", e))?;
    Ok(())
}
