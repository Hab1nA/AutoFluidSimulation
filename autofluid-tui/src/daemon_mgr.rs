use std::fs;
use std::path::PathBuf;
use std::process::{Child, Command};
use std::time::{Duration, Instant};

use crate::ipc::client::IpcClient;
use crate::state::{AppState, LogBuffer};
use crate::utils::run_command_with_timeout;

/// 等待 daemon 进程自行退出的超时时间（秒）。
/// daemon 收到 full_quit 后执行 shutdown() 清理 SC 进程池等资源，完成后自然退出。
const DAEMON_SHUTDOWN_TIMEOUT_SECS: u64 = 60;
const SERVER_DAEMON_DEFAULT_PROJECT_DIR: &str = "$HOME/AutoFluidSimulation";

pub struct DaemonManager {
    process: Option<Child>,
}

impl DaemonManager {
    pub fn new() -> Self {
        Self { process: None }
    }

    pub fn launch(&mut self, project_dir: &str) -> Result<u32, String> {
        if is_server_mode() {
            return self.launch_server_daemon(project_dir);
        }

        let daemon_script = PathBuf::from(project_dir).join("start_daemon.py");
        let python = local_daemon_python(project_dir);
        log::info!(
            "[Daemon] 准备启动后台引擎: python={}, script={}",
            python,
            daemon_script.display()
        );

        let mut cmd = Command::new(&python);
        cmd.arg(&daemon_script)
            .current_dir(project_dir)
            .stdout(std::process::Stdio::null())
            .stderr(std::process::Stdio::null());

        #[cfg(target_os = "windows")]
        {
            use std::os::windows::process::CommandExt;
            use windows_sys::Win32::System::Threading::CREATE_NO_WINDOW;
            cmd.creation_flags(CREATE_NO_WINDOW);
        }

        match cmd.spawn() {
            Ok(child) => {
                let pid = child.id();
                log::info!("[Daemon] 后台引擎进程已启动: pid={}", pid);
                self.process = Some(child);
                Ok(pid)
            }
            Err(e) => {
                log::error!("[Daemon] 启动后台引擎失败: {}", e);
                Err(format!("启动后台引擎失败: {}", e))
            }
        }
    }

    // ------------------------------------------------------------------
    // 内部辅助方法
    // ------------------------------------------------------------------

    fn pid_file_path(project_dir: &str) -> PathBuf {
        PathBuf::from(project_dir).join("data").join("daemon.pid")
    }

    fn read_pid_file(project_dir: &str) -> Option<u32> {
        let pid_file = Self::pid_file_path(project_dir);
        let content = fs::read_to_string(pid_file).ok()?;
        content.trim().parse::<u32>().ok()
    }

    fn remove_pid_file(project_dir: &str) {
        let pid_file = Self::pid_file_path(project_dir);
        match fs::remove_file(&pid_file) {
            Ok(()) => log::info!("[Daemon] 已清理 PID 文件: {}", pid_file.display()),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
                log::debug!("[Daemon] PID 文件不存在，无需清理: {}", pid_file.display());
            }
            Err(e) => log::warn!(
                "[Daemon] 清理 PID 文件失败: {}, error={}",
                pid_file.display(),
                e
            ),
        }
    }

    pub fn stop(&mut self, project_dir: &str) -> Result<(), String> {
        if is_server_mode() {
            self.process = None;
            log::info!("[Daemon] server 模式下跳过本地 daemon 进程等待");
            return Ok(());
        }

        // full_quit IPC 命令已在主循环中发送，daemon 的 shutdown() 正在执行。
        // 仅等待进程自行退出，不做额外干预——与 Ctrl+C 行为一致。
        log::info!("[Daemon] 等待后台引擎退出");
        let stopped = if let Some(mut child) = self.process.take() {
            Self::wait_for_exit(&mut child)
        } else if let Some(pid) = Self::read_pid_file(project_dir) {
            log::info!("[Daemon] 通过 PID 文件等待后台引擎退出: pid={}", pid);
            Self::wait_for_pid_exit(project_dir)
        } else {
            log::info!("[Daemon] 未发现需要等待的后台引擎进程");
            true
        };
        Self::cleanup_pid_file_after_wait(project_dir, stopped)
    }

    fn cleanup_pid_file_after_wait(project_dir: &str, stopped: bool) -> Result<(), String> {
        if stopped {
            Self::remove_pid_file(project_dir);
            Ok(())
        } else {
            Err(format!(
                "等待后台引擎退出超时 ({DAEMON_SHUTDOWN_TIMEOUT_SECS}s)"
            ))
        }
    }

    /// 等待子进程自行退出。
    ///
    /// 注意：此方法在同步上下文中调用（`run_app` 主循环退出阶段），
    /// 不在 tokio 异步上下文中，因此使用 `std::thread::sleep` 是安全的。
    fn wait_for_exit(child: &mut Child) -> bool {
        let deadline = Instant::now() + Duration::from_secs(DAEMON_SHUTDOWN_TIMEOUT_SECS);
        while Instant::now() < deadline {
            match child.try_wait() {
                Ok(Some(status)) => {
                    log::info!("[Daemon] 后台引擎已退出: status={}", status);
                    return true;
                }
                Ok(None) => std::thread::sleep(Duration::from_millis(200)),
                Err(e) => {
                    log::warn!("[Daemon] 检查后台引擎退出状态失败: {}", e);
                    return false;
                }
            }
        }
        // 超时仅记录，不强杀——daemon 可能仍在清理中
        log::warn!("[Daemon] 等待后台引擎退出超时 ({DAEMON_SHUTDOWN_TIMEOUT_SECS}s)");
        eprintln!("[TUI] 等待后台引擎退出超时 ({DAEMON_SHUTDOWN_TIMEOUT_SECS}s)，TUI 退出");
        false
    }

    /// 等待外部 daemon 进程自行退出（通过 PID 文件检测）。
    ///
    /// 注意：此方法在同步上下文中调用，不在 tokio 异步上下文中，
    /// 因此使用 `std::thread::sleep` 是安全的。
    fn wait_for_pid_exit(project_dir: &str) -> bool {
        let pid_file = Self::pid_file_path(project_dir);
        Self::wait_for_pid_file_removed(&pid_file, DAEMON_SHUTDOWN_TIMEOUT_SECS)
    }

    fn wait_for_pid_file_removed(pid_file: &std::path::Path, timeout_secs: u64) -> bool {
        let deadline = Instant::now() + Duration::from_secs(timeout_secs);
        while Instant::now() < deadline {
            // PID 文件被删除说明 daemon shutdown 已完成
            if !pid_file.exists() {
                log::info!("[Daemon] 后台引擎已完成退出清理");
                return true;
            }
            std::thread::sleep(Duration::from_millis(200));
        }
        log::warn!("[Daemon] 等待后台引擎 PID 文件清理超时 ({timeout_secs}s)");
        eprintln!("[TUI] 等待后台引擎退出超时 ({timeout_secs}s)，TUI 退出");
        false
    }

    // ------------------------------------------------------------------
    // IPC 生命周期集成方法
    // ------------------------------------------------------------------

    /// 后台引擎启动后重连 IPC，阻塞等待至超时。
    pub fn reconnect_ipc_after_launch_sync(
        rt: &tokio::runtime::Runtime,
        ipc: &mut IpcClient,
        state: &mut AppState,
        log_buffer: &mut LogBuffer,
    ) {
        let timeout = Duration::from_secs(10);
        let deadline = std::time::Instant::now() + timeout;
        log::info!(
            "[Daemon] 等待后台引擎 IPC 就绪: timeout_ms={}",
            timeout.as_millis()
        );
        while std::time::Instant::now() < deadline {
            if ipc.is_connected() {
                state.connected = true;
                log::info!("[Daemon] IPC 已处于连接状态");
                return;
            }
            match rt.block_on(ipc.connect()) {
                Ok(()) => {
                    state.connected = true;
                    // ★ 重置日志状态：新 daemon 的日志 ID 从 1 重新开始，
                    // 必须清零 last_log_id 否则增量轮询会因 since_id 过高而收不到任何条目
                    state.last_log_id = 0;
                    log_buffer.clear_detail();
                    log_buffer.push_info("✅ 已连接到后台引擎".to_string());
                    log::info!("[Daemon] 后台引擎 IPC 已就绪");
                    return;
                }
                Err(_) => {
                    std::thread::sleep(Duration::from_millis(500));
                }
            }
        }
        state.connected = false;
        log::warn!("[Daemon] 后台引擎已启动，但 IPC 暂未就绪");
        log_buffer.push_info("⚠️ 后台引擎已启动，但 IPC 暂未就绪".to_string());
    }

    /// 停止后台引擎并通过 IPC 通知对端退出，然后断开 IPC。
    pub fn stop_with_ipc(
        &mut self,
        ipc: &mut IpcClient,
        rt: &tokio::runtime::Runtime,
        state: &mut AppState,
        log_buffer: &mut LogBuffer,
        project_dir: &str,
    ) -> bool {
        let server_mode = is_server_mode();
        let mut stop_sent_over_ipc = false;
        if ipc.is_connected() {
            log::info!("[Daemon] 发送后台引擎停止请求");
            match rt.block_on(ipc.full_quit()) {
                Ok(resp) if resp.is_ok() => {
                    stop_sent_over_ipc = true;
                    log_buffer.push_info(format!("✅ {}", resp.message));
                }
                Ok(resp) => {
                    log_buffer.push_info(format!("❌ {}", resp.message));
                }
                Err(e) => {
                    log_buffer.push_info(format!("❌ 停止后台引擎通信失败: {}", e));
                }
            }
            rt.block_on(ipc.disconnect());
        }

        if server_mode {
            if !stop_sent_over_ipc {
                match self.stop_server_daemon() {
                    Ok(()) => {
                        log_buffer.push_info("✅ 已向服务器发送 daemon 停止命令".to_string());
                    }
                    Err(e) => {
                        log_buffer.push_info(format!("❌ 停止服务器 daemon 失败: {}", e));
                        state.connected = ipc.is_connected();
                        return false;
                    }
                }
            }
            state.connected = false;
            log_buffer.push_info("✅ 服务器后台引擎已停止或正在停止".to_string());
            return true;
        }

        match self.stop(project_dir) {
            Ok(()) => {
                state.connected = false;
                log_buffer.push_info("✅ 后台引擎已停止".to_string());
                true
            }
            Err(e) => {
                log_buffer.push_info(format!("⚠️ {}", e));
                false
            }
        }
    }

    /// 重启后台引擎：停止 → 启动 → 等待 IPC 就绪。
    pub fn restart_with_ipc(
        &mut self,
        ipc: &mut IpcClient,
        rt: &tokio::runtime::Runtime,
        state: &mut AppState,
        log_buffer: &mut LogBuffer,
        project_dir: &str,
    ) {
        if is_server_mode() {
            log::info!("[Daemon] server 模式下重启服务器 daemon");
            if !self.stop_with_ipc(ipc, rt, state, log_buffer, project_dir) {
                return;
            }
            std::thread::sleep(Duration::from_secs(1));
            match self.launch(project_dir) {
                Ok(_) => {
                    log_buffer.push_info(
                        "⚠️ 已向服务器发送 daemon 启动命令，等待 IPC 就绪...".to_string(),
                    );
                    Self::reconnect_ipc_after_launch_sync(rt, ipc, state, log_buffer);
                }
                Err(e) => {
                    log_buffer.push_info(format!("❌ 重启服务器 daemon 失败: {}", e));
                }
            }
            return;
        }
        log::info!("[Daemon] 开始重启后台引擎");
        self.stop_with_ipc(ipc, rt, state, log_buffer, project_dir);
        match self.launch(project_dir) {
            Ok(pid) => {
                log_buffer.push_info(format!(
                    "⚠️ 后台引擎正在重启 (PID: {})，等待 IPC 就绪...",
                    pid
                ));
                Self::reconnect_ipc_after_launch_sync(rt, ipc, state, log_buffer);
            }
            Err(e) => {
                log_buffer.push_info(format!("❌ 重启后台引擎失败: {}", e));
            }
        }
    }

    pub fn start_with_ipc(
        &mut self,
        ipc: &mut IpcClient,
        rt: &tokio::runtime::Runtime,
        state: &mut AppState,
        log_buffer: &mut LogBuffer,
        project_dir: &str,
    ) {
        if ipc.is_connected() {
            log_buffer.push_info("⚠️ 已连接到后台引擎，无需重复启动".to_string());
            state.connected = true;
            return;
        }

        match self.launch(project_dir) {
            Ok(0) => {
                log_buffer
                    .push_info("⚠️ 已向服务器发送 daemon 启动命令，等待 IPC 就绪...".to_string());
                Self::reconnect_ipc_after_launch_sync(rt, ipc, state, log_buffer);
            }
            Ok(pid) => {
                log_buffer.push_info(format!(
                    "⚠️ 后台引擎正在启动 (PID: {})，等待 IPC 就绪...",
                    pid
                ));
                Self::reconnect_ipc_after_launch_sync(rt, ipc, state, log_buffer);
            }
            Err(e) => {
                log_buffer.push_info(format!("❌ 启动后台引擎失败: {}", e));
            }
        }
    }

    fn launch_server_daemon(&mut self, project_dir: &str) -> Result<u32, String> {
        Self::run_server_ipc_tunnel_script(project_dir)?;
        Self::run_server_daemon_command(ServerDaemonAction::Start)?;
        Ok(0)
    }

    fn stop_server_daemon(&mut self) -> Result<(), String> {
        Self::run_server_daemon_command(ServerDaemonAction::Stop)
    }

    fn run_server_daemon_command(action: ServerDaemonAction) -> Result<(), String> {
        let command = ServerDaemonSshCommand::from_env(action)?;
        let args = command.args();
        log::info!(
            "[Daemon] 通过 SSH 控制服务器 daemon: action={}, target={}",
            action.label(),
            command.target
        );
        let output = Command::new(&command.ssh_exe)
            .args(&args)
            .output()
            .map_err(|e| format!("执行 ssh 失败: {}", e))?;

        if output.status.success() {
            return Ok(());
        }

        let stderr = String::from_utf8_lossy(&output.stderr).trim().to_string();
        let stdout = String::from_utf8_lossy(&output.stdout).trim().to_string();
        let detail = if stderr.is_empty() { stdout } else { stderr };
        if detail.is_empty() {
            Err(format!("ssh 退出状态: {}", output.status))
        } else {
            Err(format!("ssh 退出状态: {}, {}", output.status, detail))
        }
    }

    fn run_server_ipc_tunnel_script(project_dir: &str) -> Result<(), String> {
        let script = PathBuf::from(project_dir)
            .join("scripts")
            .join("start_server_ipc_tunnel.ps1");
        if !script.exists() {
            return Err(format!("服务器 IPC 隧道脚本不存在: {}", script.display()));
        }

        let args = vec![
            "-NoLogo".to_string(),
            "-NoProfile".to_string(),
            "-ExecutionPolicy".to_string(),
            "Bypass".to_string(),
            "-File".to_string(),
            script.to_string_lossy().to_string(),
        ];

        let mut last_error = String::new();
        for powershell in powershell_candidates() {
            let mut cmd = Command::new(&powershell);
            cmd.args(&args).current_dir(project_dir);
            match run_command_with_timeout(&mut cmd, Duration::from_secs(60)) {
                Ok(output) if output.status.success() => {
                    log::info!(
                        "[Daemon] 服务器 IPC 隧道脚本执行成功: powershell={}, script={}",
                        powershell,
                        script.display()
                    );
                    return Ok(());
                }
                Ok(output) => {
                    let stderr = String::from_utf8_lossy(&output.stderr).trim().to_string();
                    let stdout = String::from_utf8_lossy(&output.stdout).trim().to_string();
                    let detail = if stderr.is_empty() { stdout } else { stderr };
                    last_error = if detail.is_empty() {
                        format!("{} 退出状态: {}", powershell, output.status)
                    } else {
                        format!("{} 退出状态: {}, {}", powershell, output.status, detail)
                    };
                }
                Err(e) => {
                    last_error = format!("执行 {} 失败: {}", powershell, e);
                }
            }
        }

        Err(format!("启动服务器 IPC 隧道失败: {}", last_error))
    }
}

fn is_server_mode() -> bool {
    std::env::var("AUTOFLUID_SERVER_MODE")
        .map(|mode| mode.eq_ignore_ascii_case("server"))
        .unwrap_or(false)
}

#[derive(Clone, Copy)]
enum ServerDaemonAction {
    Start,
    Stop,
}

impl ServerDaemonAction {
    fn label(self) -> &'static str {
        match self {
            ServerDaemonAction::Start => "start",
            ServerDaemonAction::Stop => "stop",
        }
    }

    fn command_env(self) -> &'static str {
        match self {
            ServerDaemonAction::Start => "AUTOFLUID_SERVER_DAEMON_START_CMD",
            ServerDaemonAction::Stop => "AUTOFLUID_SERVER_DAEMON_STOP_CMD",
        }
    }
}

struct ServerDaemonSshCommand {
    ssh_exe: String,
    target: String,
    remote_command: String,
}

impl ServerDaemonSshCommand {
    fn from_env(action: ServerDaemonAction) -> Result<Self, String> {
        let ssh_exe = env_non_empty("AUTOFLUID_SSH_EXE").unwrap_or_else(|| "ssh".to_string());
        let target = first_env_non_empty(&[
            "AUTOFLUID_SERVER_DAEMON_SSH_TARGET",
            "AUTOFLUID_SERVER_TUNNEL_HOST",
            "AUTOFLUID_SERVER_HOST",
            "AUTOFLUID_IPC_HOST",
        ])
        .unwrap_or_else(|| "ocar".to_string());
        let remote_command = match env_non_empty(action.command_env()) {
            Some(command) => command,
            None if matches!(action, ServerDaemonAction::Start) => default_server_start_command(),
            None => {
                return Err(format!(
                    "{} 未配置，且当前 IPC 未连接，无法停止服务器 daemon",
                    action.command_env()
                ));
            }
        };

        Ok(Self {
            ssh_exe,
            target,
            remote_command,
        })
    }

    fn args(&self) -> Vec<String> {
        vec![
            "-o".to_string(),
            "BatchMode=yes".to_string(),
            "-o".to_string(),
            "ConnectTimeout=10".to_string(),
            self.target.clone(),
            self.remote_command.clone(),
        ]
    }
}

fn first_env_non_empty(keys: &[&str]) -> Option<String> {
    keys.iter().find_map(|key| env_non_empty(key))
}

fn env_non_empty(key: &str) -> Option<String> {
    std::env::var(key).ok().and_then(|value| {
        let trimmed = value.trim();
        if trimmed.is_empty() {
            None
        } else {
            Some(trimmed.to_string())
        }
    })
}

fn powershell_candidates() -> Vec<String> {
    if let Some(value) = env_non_empty("AUTOFLUID_POWERSHELL_EXE") {
        return vec![value];
    }
    vec!["pwsh.exe".to_string(), "powershell.exe".to_string()]
}

fn local_daemon_python(project_dir: &str) -> String {
    let venv_python = PathBuf::from(project_dir)
        .join(".venv")
        .join("Scripts")
        .join("python.exe");
    if venv_python.is_file() {
        return venv_python.to_string_lossy().to_string();
    }
    if let Some(value) = env_non_empty("PYTHON") {
        return value;
    }
    "python".to_string()
}

fn default_server_start_command() -> String {
    let project_dir = env_non_empty("AUTOFLUID_SERVER_DAEMON_PROJECT_DIR")
        .or_else(|| env_non_empty("AUTOFLUID_SERVER_PROJECT_DIR"))
        .map(|path| shell_single_quote(&path))
        .unwrap_or_else(|| SERVER_DAEMON_DEFAULT_PROJECT_DIR.to_string());
    format!(
        "cd {project_dir} && mkdir -p logs && env AUTOFLUID_SERVER_MODE=server setsid -f .venv/bin/python start_daemon.py > logs/autofluid-daemon.out 2>&1 < /dev/null"
    )
}

fn shell_single_quote(value: &str) -> String {
    format!("'{}'", value.replace('\'', "'\\''"))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::ipc::client::IpcClient;
    use std::sync::Mutex;

    static ENV_LOCK: Mutex<()> = Mutex::new(());

    fn unique_temp_project_dir() -> PathBuf {
        let dir = std::env::temp_dir().join(format!(
            "autofluid-tui-daemon-test-{}",
            crate::generate_request_id()
        ));
        fs::create_dir_all(dir.join("data")).expect("create temp project data dir");
        dir
    }

    #[test]
    fn cleanup_pid_file_after_wait_removes_pid_when_stopped() {
        let project_dir = unique_temp_project_dir();
        let pid_file = project_dir.join("data").join("daemon.pid");
        fs::write(&pid_file, "12345").expect("write pid file");

        let result = DaemonManager::cleanup_pid_file_after_wait(
            project_dir.to_str().expect("utf8 temp path"),
            true,
        );

        assert!(result.is_ok());
        assert!(!pid_file.exists());
        let _ = fs::remove_dir_all(project_dir);
    }

    #[test]
    fn cleanup_pid_file_after_wait_keeps_pid_when_timeout() {
        let project_dir = unique_temp_project_dir();
        let pid_file = project_dir.join("data").join("daemon.pid");
        fs::write(&pid_file, "12345").expect("write pid file");

        let result = DaemonManager::cleanup_pid_file_after_wait(
            project_dir.to_str().expect("utf8 temp path"),
            false,
        );

        assert!(result.is_err());
        assert!(pid_file.exists());
        let _ = fs::remove_dir_all(project_dir);
    }

    #[test]
    fn local_daemon_python_prefers_project_venv() {
        let _guard = ENV_LOCK.lock().expect("env lock poisoned");
        std::env::set_var("PYTHON", "C:\\wrong-python\\python.exe");
        let project_dir = unique_temp_project_dir();
        let python_dir = project_dir.join(".venv").join("Scripts");
        fs::create_dir_all(&python_dir).expect("create venv scripts dir");
        let python_exe = python_dir.join("python.exe");
        fs::write(&python_exe, "").expect("write python marker");

        let resolved = local_daemon_python(project_dir.to_str().expect("utf8 temp path"));

        assert_eq!(resolved, python_exe.to_string_lossy());

        std::env::remove_var("PYTHON");
        let _ = fs::remove_dir_all(project_dir);
    }

    #[test]
    fn server_daemon_start_command_uses_configured_ssh_target() {
        let _guard = ENV_LOCK.lock().expect("env lock poisoned");
        std::env::set_var("AUTOFLUID_SERVER_MODE", "server");
        std::env::set_var("AUTOFLUID_SSH_EXE", "ssh-test");
        std::env::set_var("AUTOFLUID_SERVER_DAEMON_SSH_TARGET", "ocar-prod");
        std::env::set_var(
            "AUTOFLUID_SERVER_DAEMON_START_CMD",
            "systemctl --user start autofluid-daemon",
        );

        let command = ServerDaemonSshCommand::from_env(ServerDaemonAction::Start)
            .expect("server daemon command");
        let args = command.args();

        assert_eq!(command.ssh_exe, "ssh-test");
        assert_eq!(
            args,
            vec![
                "-o".to_string(),
                "BatchMode=yes".to_string(),
                "-o".to_string(),
                "ConnectTimeout=10".to_string(),
                "ocar-prod".to_string(),
                "systemctl --user start autofluid-daemon".to_string(),
            ]
        );

        std::env::remove_var("AUTOFLUID_SERVER_DAEMON_START_CMD");
        std::env::remove_var("AUTOFLUID_SERVER_DAEMON_SSH_TARGET");
        std::env::remove_var("AUTOFLUID_SSH_EXE");
        std::env::remove_var("AUTOFLUID_SERVER_MODE");
    }

    #[test]
    fn launch_uses_server_daemon_start_command_in_server_mode() {
        let _guard = ENV_LOCK.lock().expect("env lock poisoned");
        std::env::set_var("AUTOFLUID_SERVER_MODE", "server");
        let project_dir = unique_temp_project_dir();
        write_fake_server_ipc_tunnel_script(&project_dir);
        let powershell_exe = fake_success_exe(&project_dir, "fake_pwsh");
        let ssh_exe = fake_success_ssh_exe(&project_dir);
        std::env::set_var("AUTOFLUID_POWERSHELL_EXE", &powershell_exe);
        std::env::set_var("AUTOFLUID_SSH_EXE", &ssh_exe);
        std::env::set_var("AUTOFLUID_SERVER_DAEMON_SSH_TARGET", "ocar-prod");
        std::env::set_var("AUTOFLUID_SERVER_DAEMON_START_CMD", "exit 0");
        let mut daemon = DaemonManager::new();

        let result = daemon.launch(project_dir.to_str().expect("utf8 temp path"));

        assert_eq!(result, Ok(0));

        std::env::remove_var("AUTOFLUID_SERVER_DAEMON_START_CMD");
        std::env::remove_var("AUTOFLUID_SERVER_DAEMON_SSH_TARGET");
        std::env::remove_var("AUTOFLUID_SSH_EXE");
        std::env::remove_var("AUTOFLUID_POWERSHELL_EXE");
        std::env::remove_var("AUTOFLUID_SERVER_MODE");
        let _ = fs::remove_dir_all(project_dir);
    }

    #[test]
    fn launch_starts_server_ipc_tunnel_before_server_daemon() {
        let _guard = ENV_LOCK.lock().expect("env lock poisoned");
        std::env::set_var("AUTOFLUID_SERVER_MODE", "server");
        let project_dir = unique_temp_project_dir();
        fs::create_dir_all(project_dir.join("scripts")).expect("create scripts dir");
        fs::write(
            project_dir
                .join("scripts")
                .join("start_server_ipc_tunnel.ps1"),
            "Write-Host tunnel",
        )
        .expect("write tunnel script");
        let marker = project_dir.join("launch-order.log");
        let powershell_exe = fake_marker_exe(&project_dir, "fake_pwsh", &marker, "tunnel");
        let ssh_exe = fake_marker_exe(&project_dir, "fake_ssh", &marker, "daemon");
        std::env::set_var("AUTOFLUID_POWERSHELL_EXE", &powershell_exe);
        std::env::set_var("AUTOFLUID_SSH_EXE", &ssh_exe);
        std::env::set_var("AUTOFLUID_SERVER_DAEMON_SSH_TARGET", "ocar-prod");
        std::env::set_var("AUTOFLUID_SERVER_DAEMON_START_CMD", "exit 0");
        let mut daemon = DaemonManager::new();

        let result = daemon.launch(project_dir.to_str().expect("utf8 temp path"));

        assert_eq!(result, Ok(0));
        let order = fs::read_to_string(&marker).expect("read marker file");
        let lines: Vec<&str> = order.lines().collect();
        assert_eq!(lines, vec!["tunnel", "daemon"]);

        std::env::remove_var("AUTOFLUID_SERVER_DAEMON_START_CMD");
        std::env::remove_var("AUTOFLUID_SERVER_DAEMON_SSH_TARGET");
        std::env::remove_var("AUTOFLUID_SSH_EXE");
        std::env::remove_var("AUTOFLUID_POWERSHELL_EXE");
        std::env::remove_var("AUTOFLUID_SERVER_MODE");
        let _ = fs::remove_dir_all(project_dir);
    }

    #[test]
    fn restart_stops_remote_daemon_over_ipc_then_starts_server_daemon() {
        use std::io::{Read, Write};
        use std::net::TcpListener;
        use std::sync::mpsc;
        use std::time::Duration;

        let _guard = ENV_LOCK.lock().expect("env lock poisoned");
        std::env::set_var("AUTOFLUID_SERVER_MODE", "server");

        let project_dir = unique_temp_project_dir();
        write_fake_server_ipc_tunnel_script(&project_dir);
        let powershell_exe = fake_success_exe(&project_dir, "fake_pwsh");
        let ssh_exe = fake_success_ssh_exe(&project_dir);
        std::env::set_var("AUTOFLUID_POWERSHELL_EXE", &powershell_exe);
        std::env::set_var("AUTOFLUID_SSH_EXE", &ssh_exe);
        std::env::set_var("AUTOFLUID_SERVER_DAEMON_SSH_TARGET", "ocar-prod");
        std::env::set_var("AUTOFLUID_SERVER_DAEMON_START_CMD", "exit 0");

        let listener = TcpListener::bind("127.0.0.1:0").expect("bind test ipc");
        let port = listener.local_addr().expect("listener addr").port();
        let (tx, rx) = mpsc::channel();
        let server = std::thread::spawn(move || {
            let (mut stream, _) = listener.accept().expect("accept ipc client");
            stream
                .set_read_timeout(Some(Duration::from_millis(300)))
                .expect("set read timeout");
            let mut buf = [0_u8; 1024];
            let handshake_bytes = stream.read(&mut buf).expect("read handshake");
            assert!(handshake_bytes > 0, "connect should send handshake");
            let _ = stream.write_all(
                br#"{"status":"ok","data":{"engine_status":"stopped"},"message":"","request_id":"test"}"#,
            );
            let _ = stream.write_all(b"\n");
            let bytes = match stream.read(&mut buf) {
                Ok(n) => n,
                Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => 0,
                Err(e) if e.kind() == std::io::ErrorKind::TimedOut => 0,
                Err(e) => panic!("read ipc request: {e}"),
            };
            if bytes > 0 {
                let _ = stream.write_all(
                    br#"{"status":"ok","data":{},"message":"daemon stopping","request_id":"test"}"#,
                );
                let _ = stream.write_all(b"\n");
            }
            tx.send(bytes).expect("send byte count");
        });

        let rt = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .expect("runtime");
        let mut ipc = IpcClient::new(Some("127.0.0.1"), Some(port));
        rt.block_on(ipc.connect()).expect("connect test ipc");
        let mut state = AppState::new();
        state.connected = true;
        let mut log_buffer = LogBuffer::new();
        let mut daemon = DaemonManager::new();

        daemon.restart_with_ipc(
            &mut ipc,
            &rt,
            &mut state,
            &mut log_buffer,
            project_dir.to_str().expect("utf8 temp path"),
        );

        let bytes = rx
            .recv_timeout(Duration::from_secs(2))
            .expect("server byte count");
        assert!(bytes > 0, "restart must send full_quit in server mode");
        assert!(log_buffer
            .info_messages
            .iter()
            .any(|message| message.contains("已向服务器发送 daemon 启动命令")));

        rt.block_on(ipc.disconnect());
        server.join().expect("server thread");
        std::env::remove_var("AUTOFLUID_SERVER_DAEMON_START_CMD");
        std::env::remove_var("AUTOFLUID_SERVER_DAEMON_SSH_TARGET");
        std::env::remove_var("AUTOFLUID_SSH_EXE");
        std::env::remove_var("AUTOFLUID_POWERSHELL_EXE");
        std::env::remove_var("AUTOFLUID_SERVER_MODE");
        let _ = fs::remove_dir_all(project_dir);
    }

    #[test]
    fn restart_does_not_start_server_daemon_when_stop_fails() {
        let _guard = ENV_LOCK.lock().expect("env lock poisoned");
        std::env::set_var("AUTOFLUID_SERVER_MODE", "server");
        let project_dir = unique_temp_project_dir();
        let ssh_exe = fake_success_ssh_exe(&project_dir);
        std::env::set_var("AUTOFLUID_SSH_EXE", &ssh_exe);
        std::env::set_var("AUTOFLUID_SERVER_DAEMON_SSH_TARGET", "ocar-prod");
        std::env::set_var("AUTOFLUID_SERVER_DAEMON_START_CMD", "exit 0");
        std::env::remove_var("AUTOFLUID_SERVER_DAEMON_STOP_CMD");

        let rt = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .expect("runtime");
        let mut ipc = IpcClient::new(Some("127.0.0.1"), Some(9));
        let mut state = AppState::new();
        let mut log_buffer = LogBuffer::new();
        let mut daemon = DaemonManager::new();

        daemon.restart_with_ipc(
            &mut ipc,
            &rt,
            &mut state,
            &mut log_buffer,
            project_dir.to_str().expect("utf8 temp path"),
        );

        assert!(log_buffer
            .info_messages
            .iter()
            .any(|message| message.contains("停止服务器 daemon 失败")));
        assert!(!log_buffer
            .info_messages
            .iter()
            .any(|message| message.contains("已向服务器发送 daemon 启动命令")));

        std::env::remove_var("AUTOFLUID_SERVER_DAEMON_START_CMD");
        std::env::remove_var("AUTOFLUID_SERVER_DAEMON_SSH_TARGET");
        std::env::remove_var("AUTOFLUID_SSH_EXE");
        std::env::remove_var("AUTOFLUID_SERVER_MODE");
        let _ = fs::remove_dir_all(project_dir);
    }

    fn fake_success_ssh_exe(project_dir: &std::path::Path) -> PathBuf {
        fake_success_exe(project_dir, "fake_ssh")
    }

    fn fake_success_exe(project_dir: &std::path::Path, name: &str) -> PathBuf {
        #[cfg(windows)]
        {
            let path = project_dir.join(format!("{name}.cmd"));
            fs::write(&path, "@echo off\r\nexit /b 0\r\n").expect("write fake success cmd");
            path
        }
        #[cfg(not(windows))]
        {
            let path = project_dir.join(format!("{name}.sh"));
            fs::write(&path, "#!/bin/sh\nexit 0\n").expect("write fake success sh");
            use std::os::unix::fs::PermissionsExt;
            let mut permissions = fs::metadata(&path)
                .expect("fake success metadata")
                .permissions();
            permissions.set_mode(0o755);
            fs::set_permissions(&path, permissions).expect("chmod fake success");
            path
        }
    }

    fn write_fake_server_ipc_tunnel_script(project_dir: &std::path::Path) {
        fs::create_dir_all(project_dir.join("scripts")).expect("create scripts dir");
        fs::write(
            project_dir
                .join("scripts")
                .join("start_server_ipc_tunnel.ps1"),
            "Write-Host tunnel",
        )
        .expect("write tunnel script");
    }

    fn fake_marker_exe(
        project_dir: &std::path::Path,
        name: &str,
        marker: &std::path::Path,
        label: &str,
    ) -> PathBuf {
        #[cfg(windows)]
        {
            let path = project_dir.join(format!("{name}.cmd"));
            fs::write(
                &path,
                format!(
                    "@echo off\r\necho {label}>>\"{}\"\r\nexit /b 0\r\n",
                    marker.display()
                ),
            )
            .expect("write fake marker cmd");
            path
        }
        #[cfg(not(windows))]
        {
            let path = project_dir.join(format!("{name}.sh"));
            fs::write(
                &path,
                format!(
                    "#!/bin/sh\necho {label} >> '{}'\nexit 0\n",
                    marker.display()
                ),
            )
            .expect("write fake marker shell");
            use std::os::unix::fs::PermissionsExt;
            let mut permissions = fs::metadata(&path)
                .expect("fake marker metadata")
                .permissions();
            permissions.set_mode(0o755);
            fs::set_permissions(&path, permissions).expect("chmod fake marker");
            path
        }
    }
}
