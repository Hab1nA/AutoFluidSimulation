use std::io::Read;
use std::path::PathBuf;
use std::process::{Command, Output, Stdio};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};

/// 将字符索引转换为字节索引（UTF-8 安全）。
///
/// 用于在 String 中按字符位置插入/删除字符时，
/// 将人类可读的字符偏移量转换为 Rust String 所需的字节偏移量。
/// 若 char_idx 超出范围，返回字符串的字节长度。
pub fn char_to_byte_index(s: &str, char_idx: usize) -> usize {
    s.char_indices()
        .nth(char_idx)
        .map(|(i, _)| i)
        .unwrap_or(s.len())
}

/// 按显示宽度截断字符串，超出部分以 "..." 替代。
///
/// 使用 unicode-width 计算实际显示宽度（中文占 2 列，ASCII 占 1 列）。
pub fn truncate_for_display(s: &str, max_width: usize) -> String {
    if max_width == 0 {
        return String::new();
    }
    let width = unicode_width::UnicodeWidthStr::width(s);
    if width <= max_width {
        return s.to_string();
    }
    let mut result = String::new();
    let mut current_width = 0;
    for c in s.chars() {
        let cw = unicode_width::UnicodeWidthChar::width(c).unwrap_or(0);
        if current_width + cw + 3 > max_width {
            result.push_str("...");
            break;
        }
        result.push(c);
        current_width += cw;
    }
    if result.is_empty() {
        result = s.chars().take(3).collect();
        result.push_str("...");
    }
    result
}

/// 按显示宽度用空格填充标签至目标宽度。
pub fn pad_label_by_display_width(label: &str, target_width: u16) -> String {
    let dw = unicode_width::UnicodeWidthStr::width(label);
    let pad = (target_width as usize).saturating_sub(dw);
    format!("{}{}", label, " ".repeat(pad))
}

/// 格式化本地时间（通过 Windows API `GetLocalTime`）。
///
/// 支持的占位符：%Y（年）、%m（月）、%d（日）、%H（时）、%M（分）、%S（秒）。
/// 替代 chrono crate 以减少依赖。
pub fn format_local_time(fmt: &str) -> String {
    let mut st = std::mem::MaybeUninit::<windows_sys::Win32::Foundation::SYSTEMTIME>::uninit();
    let st = unsafe {
        windows_sys::Win32::System::SystemInformation::GetLocalTime(st.as_mut_ptr());
        st.assume_init()
    };
    fmt.replace("%Y", &format!("{:04}", st.wYear))
        .replace("%m", &format!("{:02}", st.wMonth))
        .replace("%d", &format!("{:02}", st.wDay))
        .replace("%H", &format!("{:02}", st.wHour))
        .replace("%M", &format!("{:02}", st.wMinute))
        .replace("%S", &format!("{:02}", st.wSecond))
}

pub fn import_project_env(project_dir: &str) -> Result<(), String> {
    let env_path = PathBuf::from(project_dir).join(".env");
    let content = match std::fs::read_to_string(&env_path) {
        Ok(content) => content,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
            ensure_default_server_mode();
            sync_endpoint_env();
            return Ok(());
        }
        Err(e) => return Err(format!("读取 .env 失败: {}", e)),
    };

    for (key, value) in parse_env_content(&content) {
        std::env::set_var(key, value);
    }

    ensure_default_server_mode();
    sync_endpoint_env();
    Ok(())
}

pub fn resolve_project_dir() -> PathBuf {
    if let Ok(current_dir) = std::env::current_dir() {
        if let Some(project_dir) = find_project_dir_from(&current_dir) {
            return project_dir;
        }
    }
    if let Ok(current_exe) = std::env::current_exe() {
        if let Some(project_dir) = find_project_dir_from(&current_exe) {
            return project_dir;
        }
    }
    std::env::current_dir().unwrap_or_default()
}

fn find_project_dir_from(start: &std::path::Path) -> Option<PathBuf> {
    let mut current = if start.is_file() {
        start.parent()?
    } else {
        start
    };
    loop {
        if current.join("start_daemon.py").is_file()
            || (current.join(".env").is_file() && current.join("autofluid-tui").is_dir())
        {
            return Some(current.to_path_buf());
        }
        current = current.parent()?;
    }
}

pub(crate) fn parse_env_content(content: &str) -> Vec<(String, String)> {
    content.lines().filter_map(parse_env_assignment).collect()
}

fn parse_env_assignment(raw_line: &str) -> Option<(String, String)> {
    let mut line = raw_line.trim();
    if line.is_empty() || line.starts_with('#') {
        return None;
    }
    if let Some(rest) = line.strip_prefix("export ") {
        line = rest.trim_start();
    }

    let separator = line.find('=')?;
    let key = line[..separator].trim();
    if !is_valid_env_key(key) {
        return None;
    }

    let mut value = line[separator + 1..].trim().to_string();
    if value.len() >= 2 {
        let bytes = value.as_bytes();
        let first = bytes[0];
        let last = bytes[value.len() - 1];
        if (first == b'"' && last == b'"') || (first == b'\'' && last == b'\'') {
            value = value[1..value.len() - 1].to_string();
        }
    }

    Some((key.to_string(), value))
}

fn is_valid_env_key(key: &str) -> bool {
    let mut chars = key.chars();
    match chars.next() {
        Some(first) if first == '_' || first.is_ascii_alphabetic() => {}
        _ => return false,
    }
    chars.all(|ch| ch == '_' || ch.is_ascii_alphanumeric())
}

fn ensure_default_server_mode() {
    if std::env::var("AUTOFLUID_SERVER_MODE")
        .map(|value| value.trim().is_empty())
        .unwrap_or(true)
    {
        std::env::set_var("AUTOFLUID_SERVER_MODE", "server");
    }
}

fn sync_endpoint_env() {
    let ipc_host = std::env::var("AUTOFLUID_IPC_HOST").unwrap_or_default();
    let server_host = std::env::var("AUTOFLUID_SERVER_HOST").unwrap_or_default();
    if ipc_host.trim().is_empty() && !server_host.trim().is_empty() {
        std::env::set_var("AUTOFLUID_IPC_HOST", server_host.trim());
    } else if server_host.trim().is_empty() && !ipc_host.trim().is_empty() {
        std::env::set_var("AUTOFLUID_SERVER_HOST", ipc_host.trim());
    }
}

fn spawn_output_reader<R>(mut reader: R) -> JoinHandle<Result<Vec<u8>, std::io::Error>>
where
    R: Read + Send + 'static,
{
    thread::spawn(move || {
        let mut bytes = Vec::new();
        reader.read_to_end(&mut bytes).map(|_| bytes)
    })
}

fn collect_output_reader(
    handle: Option<JoinHandle<Result<Vec<u8>, std::io::Error>>>,
    label: &str,
) -> Result<Vec<u8>, String> {
    match handle {
        Some(handle) => match handle.join() {
            Ok(Ok(bytes)) => Ok(bytes),
            Ok(Err(e)) => Err(format!("读取命令{}失败: {}", label, e)),
            Err(_) => Err(format!("读取命令{}线程异常", label)),
        },
        None => Ok(Vec::new()),
    }
}

pub fn run_command_with_timeout(
    command: &mut Command,
    timeout: Duration,
) -> Result<Output, String> {
    command.stdout(Stdio::piped()).stderr(Stdio::piped());
    let mut child = command
        .spawn()
        .map_err(|e| format!("启动命令失败: {}", e))?;
    let mut stdout_reader = child.stdout.take().map(spawn_output_reader);
    let mut stderr_reader = child.stderr.take().map(spawn_output_reader);
    let deadline = Instant::now() + timeout;

    loop {
        match child.try_wait() {
            Ok(Some(status)) => {
                let stdout = collect_output_reader(stdout_reader.take(), "stdout")?;
                let stderr = collect_output_reader(stderr_reader.take(), "stderr")?;
                return Ok(Output {
                    status,
                    stdout,
                    stderr,
                });
            }
            Ok(None) if Instant::now() >= deadline => {
                let _ = child.kill();
                let _ = child.wait();
                let _ = collect_output_reader(stdout_reader.take(), "stdout");
                let _ = collect_output_reader(stderr_reader.take(), "stderr");
                return Err(format!("命令执行超时 ({}s)", timeout.as_secs()));
            }
            Ok(None) => std::thread::sleep(Duration::from_millis(100)),
            Err(e) => {
                let _ = child.kill();
                let _ = child.wait();
                let _ = collect_output_reader(stdout_reader.take(), "stdout");
                let _ = collect_output_reader(stderr_reader.take(), "stderr");
                return Err(format!("检查命令状态失败: {}", e));
            }
        }
    }
}

pub fn is_pid_alive(pid: u32) -> bool {
    if pid == 0 {
        return false;
    }

    #[cfg(target_os = "windows")]
    {
        use windows_sys::Win32::Foundation::{CloseHandle, HANDLE};
        use windows_sys::Win32::System::Threading::{
            GetExitCodeProcess, OpenProcess, PROCESS_QUERY_LIMITED_INFORMATION,
        };
        const STILL_ACTIVE_EXIT_CODE: u32 = 259;
        unsafe {
            let handle: HANDLE = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid);
            if handle.is_null() {
                return false;
            }
            let mut exit_code = 0;
            if GetExitCodeProcess(handle, &mut exit_code) == 0 {
                CloseHandle(handle);
                return false;
            }
            CloseHandle(handle);
            exit_code == STILL_ACTIVE_EXIT_CODE
        }
    }

    #[cfg(not(target_os = "windows"))]
    {
        Command::new("kill")
            .args(["-0", &pid.to_string()])
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .status()
            .map(|status| status.success())
            .unwrap_or(false)
    }
}

pub fn wait_for_pid_dead(pid: u32, timeout: Duration) -> bool {
    let deadline = Instant::now() + timeout;
    while Instant::now() < deadline {
        if !is_pid_alive(pid) {
            return true;
        }
        std::thread::sleep(Duration::from_millis(100));
    }
    !is_pid_alive(pid)
}

pub fn kill_process_tree(pid: u32) -> bool {
    #[cfg(target_os = "windows")]
    {
        let taskkill_ok = Command::new("taskkill")
            .args(["/PID", &pid.to_string(), "/T", "/F"])
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .status()
            .map(|status| status.success())
            .unwrap_or(false);
        if taskkill_ok {
            return true;
        }

        use windows_sys::Win32::Foundation::CloseHandle;
        use windows_sys::Win32::System::Threading::{
            OpenProcess, TerminateProcess, PROCESS_TERMINATE,
        };
        unsafe {
            let handle = OpenProcess(PROCESS_TERMINATE, 0, pid);
            if handle.is_null() {
                return false;
            }
            let terminated = TerminateProcess(handle, 1) != 0;
            CloseHandle(handle);
            terminated
        }
    }

    #[cfg(not(target_os = "windows"))]
    {
        Command::new("kill")
            .args(["-TERM", &pid.to_string()])
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .status()
            .map(|status| status.success())
            .unwrap_or(false)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn run_command_with_timeout_drains_large_stdout() {
        let mut command = large_stdout_command();
        let output = run_command_with_timeout(&mut command, Duration::from_secs(5))
            .expect("large stdout command should finish without pipe deadlock");

        assert!(output.status.success());
        assert!(
            output.stdout.len() > 128 * 1024,
            "expected large stdout, got {} bytes",
            output.stdout.len()
        );
    }

    #[cfg(windows)]
    fn large_stdout_command() -> Command {
        let mut command = Command::new("cmd");
        command.args([
            "/C",
            "for /L %i in (1,1,5000) do @echo 0123456789012345678901234567890123456789",
        ]);
        command
    }

    #[cfg(not(windows))]
    fn large_stdout_command() -> Command {
        let mut command = Command::new("sh");
        command.args([
            "-c",
            "yes 0123456789012345678901234567890123456789 | head -c 200000",
        ]);
        command
    }

    #[test]
    fn parse_env_content_reads_server_ipc_settings() {
        let values = parse_env_content(
            "# comment\n\
             export AUTOFLUID_SERVER_MODE=server\n\
             AUTOFLUID_SERVER_HOST=\"127.0.0.1\"\n\
             AUTOFLUID_IPC_PORT='19527'\n\
             INVALID KEY=ignored\n",
        );

        assert_eq!(
            values,
            vec![
                ("AUTOFLUID_SERVER_MODE".to_string(), "server".to_string()),
                ("AUTOFLUID_SERVER_HOST".to_string(), "127.0.0.1".to_string()),
                ("AUTOFLUID_IPC_PORT".to_string(), "19527".to_string()),
            ]
        );
    }

    #[test]
    fn find_project_dir_from_nested_tui_binary_path() {
        let project_dir = std::env::temp_dir().join(format!(
            "autofluid-tui-project-test-{}",
            crate::generate_request_id()
        ));
        let binary_dir = project_dir
            .join("autofluid-tui")
            .join("target")
            .join("release");
        std::fs::create_dir_all(&binary_dir).expect("create binary dir");
        std::fs::write(project_dir.join(".env"), "AUTOFLUID_SERVER_MODE=server\n")
            .expect("write .env");

        let resolved = find_project_dir_from(&binary_dir).expect("resolve project dir");

        assert_eq!(resolved, project_dir);

        let _ = std::fs::remove_dir_all(project_dir);
    }
}
