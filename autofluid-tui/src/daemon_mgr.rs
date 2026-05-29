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
        let daemon_script = PathBuf::from(project_dir).join("start_daemon.py");
        let python = std::env::var("PYTHON").unwrap_or_else(|_| "python".to_string());

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
                self.process = Some(child);
                Ok(pid)
            }
            Err(e) => Err(format!("启动后台引擎失败: {}", e)),
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
        let _ = fs::remove_file(pid_file);
    }

    pub fn stop(&mut self, project_dir: &str) -> Result<(), String> {
        // full_quit IPC 命令已在主循环中发送，daemon 的 shutdown() 正在执行。
        // 仅等待进程自行退出，不做额外干预——与 Ctrl+C 行为一致。
        if let Some(mut child) = self.process.take() {
            Self::wait_for_exit(&mut child);
        } else if let Some(_pid) = Self::read_pid_file(project_dir) {
            Self::wait_for_pid_exit(project_dir);
        }
        Self::remove_pid_file(project_dir);
        Ok(())
    }

    /// 等待子进程自行退出。
    fn wait_for_exit(child: &mut Child) {
        let deadline = Instant::now() + Duration::from_secs(DAEMON_SHUTDOWN_TIMEOUT_SECS);
        while Instant::now() < deadline {
            match child.try_wait() {
                Ok(Some(_)) => return,
                Ok(None) => std::thread::sleep(Duration::from_millis(200)),
                Err(_) => return,
            }
        }
        // 超时仅记录，不强杀——daemon 可能仍在清理中
        eprintln!("[TUI] 等待后台引擎退出超时 ({DAEMON_SHUTDOWN_TIMEOUT_SECS}s)，TUI 退出");
    }

    /// 等待外部 daemon 进程自行退出（通过 PID 文件检测）。
    fn wait_for_pid_exit(project_dir: &str) {
        let pid_file = Self::pid_file_path(project_dir);
        let deadline = Instant::now() + Duration::from_secs(DAEMON_SHUTDOWN_TIMEOUT_SECS);
        while Instant::now() < deadline {
            // PID 文件被删除说明 daemon shutdown 已完成
            if !pid_file.exists() {
                return;
            }
            std::thread::sleep(Duration::from_millis(200));
        }
        eprintln!("[TUI] 等待后台引擎退出超时 ({DAEMON_SHUTDOWN_TIMEOUT_SECS}s)，TUI 退出");
    }

    // ------------------------------------------------------------------
    // IPC 生命周期集成方法
    // ------------------------------------------------------------------

    /// 后台引擎启动后重连 IPC，阻塞等待至超时。
    pub fn reconnect_ipc_after_launch(
        rt: &tokio::runtime::Runtime,
        ipc: &mut IpcClient,
        state: &mut AppState,
        log_buffer: &mut LogBuffer,
    ) {
        let timeout = Duration::from_secs(10);
        let deadline = std::time::Instant::now() + timeout;
        while std::time::Instant::now() < deadline {
            if ipc.is_connected() {
                state.connected = true;
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
                    return;
                }
                Err(_) => {
                    std::thread::sleep(Duration::from_millis(500));
                }
            }
        }
        state.connected = false;
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
            let _ = rt.block_on(ipc.full_quit());
            rt.block_on(ipc.disconnect());
        }
        let _ = self.stop(project_dir);
        state.connected = false;
        log_buffer.push_info("✅ 后台引擎已停止".to_string());
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
        self.stop_with_ipc(ipc, rt, state, log_buffer, project_dir);
        match self.launch(project_dir) {
            Ok(pid) => {
                log_buffer.push_info(format!(
                    "⚠️ 后台引擎正在重启 (PID: {})，等待 IPC 就绪...",
                    pid
                ));
                Self::reconnect_ipc_after_launch(rt, ipc, state, log_buffer);
            }
            Err(e) => {
                log_buffer.push_info(format!("❌ 重启后台引擎失败: {}", e));
            }
        }
    }
}
