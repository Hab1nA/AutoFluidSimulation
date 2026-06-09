use std::fs;
use std::path::PathBuf;
use std::process::{Child, Command};
use std::time::{Duration, Instant};

use crate::ipc::client::IpcClient;
use crate::state::{AppState, LogBuffer};

/// 等待 daemon 进程自行退出的超时时间（秒）。
/// daemon 收到 full_quit 后执行 shutdown() 清理 SC 进程池等资源，完成后自然退出。
const DAEMON_SHUTDOWN_TIMEOUT_SECS: u64 = 60;

pub struct DaemonManager {
    process: Option<Child>,
}

impl DaemonManager {
    pub fn new() -> Self {
        Self { process: None }
    }

    pub fn launch(&mut self, project_dir: &str) -> Result<u32, String> {
        if is_server_mode() {
            return Err(
                "server 模式下不会启动本地 Daemon；请确认 ocar 后端已运行并连接远程 IPC"
                    .to_string(),
            );
        }

        let daemon_script = PathBuf::from(project_dir).join("start_daemon.py");
        let python = std::env::var("PYTHON").unwrap_or_else(|_| "python".to_string());
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
    ) {
        if ipc.is_connected() {
            log::info!("[Daemon] 发送后台引擎停止请求");
            match rt.block_on(ipc.full_quit()) {
                Ok(resp) if resp.is_ok() => {
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
        match self.stop(project_dir) {
            Ok(()) => {
                state.connected = false;
                log_buffer.push_info("✅ 后台引擎已停止".to_string());
            }
            Err(e) => {
                log_buffer.push_info(format!("⚠️ {}", e));
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
            log::warn!("[Daemon] server 模式下拒绝从 TUI 重启远程 ocar 后端");
            log_buffer.push_info(
                "server 模式下不会从 TUI 重启 ocar 后端；请在服务器上重启 daemon 后重新连接"
                    .to_string(),
            );
            state.connected = ipc.is_connected();
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
}

fn is_server_mode() -> bool {
    std::env::var("AUTOFLUID_SERVER_MODE")
        .map(|mode| mode.eq_ignore_ascii_case("server"))
        .unwrap_or(false)
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
    fn launch_rejects_local_daemon_start_in_server_mode() {
        let _guard = ENV_LOCK.lock().expect("env lock poisoned");
        std::env::set_var("AUTOFLUID_SERVER_MODE", "server");
        let project_dir = unique_temp_project_dir();
        let mut daemon = DaemonManager::new();

        let result = daemon.launch(project_dir.to_str().expect("utf8 temp path"));

        assert!(result.is_err());
        assert!(result.expect_err("launch should fail").contains("ocar"));

        std::env::remove_var("AUTOFLUID_SERVER_MODE");
        let _ = fs::remove_dir_all(project_dir);
    }

    #[test]
    fn restart_rejects_before_sending_stop_in_server_mode() {
        use std::io::{Read, Write};
        use std::net::TcpListener;
        use std::sync::mpsc;
        use std::time::Duration;

        let _guard = ENV_LOCK.lock().expect("env lock poisoned");
        std::env::set_var("AUTOFLUID_SERVER_MODE", "server");

        let listener = TcpListener::bind("127.0.0.1:0").expect("bind test ipc");
        let port = listener.local_addr().expect("listener addr").port();
        let (tx, rx) = mpsc::channel();
        let server = std::thread::spawn(move || {
            let (mut stream, _) = listener.accept().expect("accept ipc client");
            stream
                .set_read_timeout(Some(Duration::from_millis(300)))
                .expect("set read timeout");
            let mut buf = [0_u8; 1024];
            let bytes = match stream.read(&mut buf) {
                Ok(n) => n,
                Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => 0,
                Err(e) if e.kind() == std::io::ErrorKind::TimedOut => 0,
                Err(e) => panic!("read ipc request: {e}"),
            };
            if bytes > 0 {
                let _ = stream.write_all(
                    br#"{"status":"ok","data":null,"message":"stopping","request_id":"test"}"#,
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
        let project_dir = unique_temp_project_dir();
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
        assert_eq!(
            bytes, 0,
            "restart must not send stop/full_quit in server mode"
        );
        assert!(state.connected);
        assert!(log_buffer
            .info_messages
            .iter()
            .any(|message| message.contains("不会从 TUI 重启 ocar 后端")));

        rt.block_on(ipc.disconnect());
        server.join().expect("server thread");
        std::env::remove_var("AUTOFLUID_SERVER_MODE");
        let _ = fs::remove_dir_all(project_dir);
    }
}
