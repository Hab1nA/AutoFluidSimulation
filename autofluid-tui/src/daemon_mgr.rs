use std::fs;
use std::path::PathBuf;
use std::process::{Child, Command};
use std::time::Duration;

use crate::ipc::client::IpcClient;
use crate::state::{AppState, LogBuffer};


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
        let mut stopped = false;

        if let Some(mut child) = self.process.take() {
            stopped = true;

            #[cfg(target_os = "windows")]
            {
                let pid = child.id();
                let _ = Command::new("taskkill")
                    .args(["/pid", &pid.to_string(), "/f"])
                    .stdout(std::process::Stdio::null())
                    .stderr(std::process::Stdio::null())
                    .status();
            }

            #[cfg(not(target_os = "windows"))]
            {
                let _ = child.kill();
            }

            let _ = child.wait();
        }

        if !stopped {
            if let Some(pid) = Self::read_pid_file(project_dir) {
                #[cfg(target_os = "windows")]
                {
                    let _ = Command::new("taskkill")
                        .args(["/pid", &pid.to_string(), "/f"])
                        .stdout(std::process::Stdio::null())
                        .stderr(std::process::Stdio::null())
                        .status();
                }

                #[cfg(not(target_os = "windows"))]
                {
                    // 使用标准 SIGTERM 信号（15），而非不规范的自由格式
                    let _ = Command::new("kill").args(["-s", "TERM", &pid.to_string()]).status();
                }
            }
        }

        Self::remove_pid_file(project_dir);
        Ok(())
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
                log_buffer.push_info(format!("⚠️ 后台引擎正在重启 (PID: {})，等待 IPC 就绪...", pid));
                Self::reconnect_ipc_after_launch(rt, ipc, state, log_buffer);
            }
            Err(e) => {
                log_buffer.push_info(format!("❌ 重启后台引擎失败: {}", e));
            }
        }
    }
}
