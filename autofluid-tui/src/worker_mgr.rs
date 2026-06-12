//! Worker 进程管理器。
//!
//! 管理本地 LocalWorker 子进程和工作站 SSH 反向隧道进程的生命周期。
//! 与 `DaemonManager` 类似，通过子进程方式启动/停止 worker 相关进程。

use std::path::PathBuf;
use std::process::{Child, Command};
use std::time::Duration;

use crate::state::LogBuffer;

/// WorkerManager 管理本地 LocalWorker 进程和 SSH 隧道进程。
pub struct WorkerManager {
    /// 本地 LocalWorker 子进程
    worker_process: Option<Child>,
    /// 工作站 SSH 反向隧道子进程
    tunnel_process: Option<Child>,
}

impl WorkerManager {
    pub fn new() -> Self {
        Self {
            worker_process: None,
            tunnel_process: None,
        }
    }

    /// 检查进程是否仍在运行。
    ///
    /// `None` → 没有进程 → 返回 `false`。
    /// `Some(child)` 且 `try_wait()` 返回 `Ok(None)` → 仍在运行 → `true`。
    fn is_process_running(proc: &mut Option<Child>) -> bool {
        proc.as_mut()
            .is_some_and(|p| p.try_wait().ok().flatten().is_none())
    }

    /// 启动所有 worker：建立 SSH 隧道 + 启动本地 LocalWorker。
    pub fn start_workers(&mut self, project_dir: &str, log_buffer: &mut LogBuffer) -> bool {
        log::info!("[Worker] 开始启动 Worker 进程");

        // 1. 启动工作站 SSH 反向隧道
        if !self.start_tunnel(project_dir, log_buffer) {
            log_buffer.push_info("⚠️ SSH 隧道启动失败，继续尝试启动本地 Worker".to_string());
        }

        // 2. 启动本地 LocalWorker 进程
        if !self.start_local_worker(project_dir, log_buffer) {
            log_buffer.push_info("❌ 本地 Worker 启动失败".to_string());
            return false;
        }

        log_buffer.push_info("✅ Worker 启动流程完成".to_string());
        true
    }

    /// 停止所有 worker：终止本地 Worker 进程 + 关闭 SSH 隧道。
    pub fn stop_workers(&mut self, log_buffer: &mut LogBuffer) -> bool {
        log::info!("[Worker] 开始停止 Worker 进程");
        let mut success = true;

        // 1. 终止本地 LocalWorker 进程
        if !self.stop_local_worker(log_buffer) {
            success = false;
        }

        // 2. 终止 SSH 隧道进程
        if !self.stop_tunnel(log_buffer) {
            success = false;
        }

        if success {
            log_buffer.push_info("✅ 所有 Worker 已停止".to_string());
        }
        success
    }

    /// 重启所有 worker：先停止再启动。
    pub fn restart_workers(&mut self, project_dir: &str, log_buffer: &mut LogBuffer) -> bool {
        log::info!("[Worker] 开始重启 Worker 进程");
        self.stop_workers(log_buffer);
        std::thread::sleep(Duration::from_secs(1));
        self.start_workers(project_dir, log_buffer)
    }

    /// 本地 LocalWorker 是否正在运行。
    pub fn is_worker_running(&mut self) -> bool {
        Self::is_process_running(&mut self.worker_process)
    }

    /// SSH 隧道是否正在运行。
    pub fn is_tunnel_running(&mut self) -> bool {
        Self::is_process_running(&mut self.tunnel_process)
    }

    // ------------------------------------------------------------------
    // 内部方法
    // ------------------------------------------------------------------

    /// 启动本地 LocalWorker 子进程。
    fn start_local_worker(&mut self, project_dir: &str, log_buffer: &mut LogBuffer) -> bool {
        // 如果已有进程在运行，先停止
        if self.is_worker_running() {
            log_buffer.push_info("⚠️ 本地 Worker 已在运行中".to_string());
            return true;
        }
        self.worker_process = None;

        let worker_script = PathBuf::from(project_dir).join("main.py");
        if !worker_script.exists() {
            log_buffer.push_info(format!(
                "❌ Worker 启动脚本不存在: {}",
                worker_script.display()
            ));
            return false;
        }

        let python = std::env::var("PYTHON").unwrap_or_else(|_| "python".to_string());
        log::info!(
            "[Worker] 启动本地 LocalWorker: python={}, script={}",
            python,
            worker_script.display()
        );

        let mut cmd = Command::new(&python);
        cmd.arg(&worker_script)
            .arg("--worker")
            .current_dir(project_dir);

        #[cfg(target_os = "windows")]
        {
            use std::os::windows::process::CommandExt;
            use windows_sys::Win32::System::Threading::CREATE_NO_WINDOW;
            cmd.creation_flags(CREATE_NO_WINDOW);
        }

        match cmd.spawn() {
            Ok(child) => {
                let pid = child.id();
                log::info!("[Worker] 本地 LocalWorker 已启动: pid={}", pid);
                log_buffer.push_info(format!("✅ 本地 Worker 已启动 (PID: {})", pid));
                self.worker_process = Some(child);
                true
            }
            Err(e) => {
                log::error!("[Worker] 启动本地 LocalWorker 失败: {}", e);
                log_buffer.push_info(format!("❌ 启动本地 Worker 失败: {}", e));
                false
            }
        }
    }

    /// 终止本地 LocalWorker 子进程。
    fn stop_local_worker(&mut self, log_buffer: &mut LogBuffer) -> bool {
        if let Some(ref mut proc) = self.worker_process {
            match proc.try_wait() {
                Ok(Some(status)) => {
                    log::info!("[Worker] 本地 Worker 已自行退出: {}", status);
                    self.worker_process = None;
                    return true;
                }
                Ok(None) => {
                    // 进程仍在运行，尝试终止
                    log::info!("[Worker] 正在终止本地 Worker 进程...");
                    if let Err(e) = proc.kill() {
                        log::warn!("[Worker] 终止本地 Worker 进程失败: {}", e);
                        log_buffer.push_info(format!("⚠️ 终止本地 Worker 失败: {}", e));
                        return false;
                    }
                    // 使用 try_wait 轮询替代 wait()，避免阻塞主循环
                    let deadline = std::time::Instant::now() + std::time::Duration::from_secs(5);
                    let mut terminated = false;
                    while std::time::Instant::now() < deadline {
                        match proc.try_wait() {
                            Ok(Some(status)) => {
                                log::info!("[Worker] 本地 Worker 已终止: {}", status);
                                log_buffer.push_info("✅ 本地 Worker 已终止".to_string());
                                terminated = true;
                                break;
                            }
                            Ok(None) => std::thread::sleep(std::time::Duration::from_millis(100)),
                            Err(e) => {
                                log::warn!("[Worker] 检查本地 Worker 退出状态失败: {}", e);
                                break;
                            }
                        }
                    }
                    if !terminated {
                        log::warn!("[Worker] 等待本地 Worker 退出超时 (5s)");
                        log_buffer.push_info("⚠️ 等待本地 Worker 退出超时".to_string());
                    }
                    self.worker_process = None;
                    return true;
                }
                Err(e) => {
                    log::warn!("[Worker] 检查本地 Worker 状态失败: {}", e);
                    self.worker_process = None;
                    return false;
                }
            }
        }
        // 没有进程在运行，视为成功
        true
    }

    /// 启动工作站 SSH 反向隧道。
    fn start_tunnel(&mut self, project_dir: &str, log_buffer: &mut LogBuffer) -> bool {
        // 如果已有隧道在运行，先停止
        if self.is_tunnel_running() {
            log_buffer.push_info("⚠️ SSH 隧道已在运行中".to_string());
            return true;
        }
        self.tunnel_process = None;

        let tunnel_script = PathBuf::from(project_dir)
            .join("scripts")
            .join("start_workstation_reverse_tunnel.ps1");
        if !tunnel_script.exists() {
            log_buffer.push_info(format!(
                "⚠️ SSH 隧道脚本不存在: {}（跳过隧道建立）",
                tunnel_script.display()
            ));
            return false;
        }

        log::info!(
            "[Worker] 启动 SSH 反向隧道: script={}",
            tunnel_script.display()
        );

        let mut cmd = Command::new("powershell");
        cmd.args([
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            &tunnel_script.to_string_lossy(),
        ])
        .current_dir(project_dir);

        #[cfg(target_os = "windows")]
        {
            use std::os::windows::process::CommandExt;
            use windows_sys::Win32::System::Threading::CREATE_NO_WINDOW;
            cmd.creation_flags(CREATE_NO_WINDOW);
        }

        match cmd.spawn() {
            Ok(child) => {
                let pid = child.id();
                log::info!("[Worker] SSH 反向隧道已启动: pid={}", pid);
                log_buffer.push_info(format!("✅ SSH 反向隧道已启动 (PID: {})", pid));
                self.tunnel_process = Some(child);
                true
            }
            Err(e) => {
                log::error!("[Worker] 启动 SSH 反向隧道失败: {}", e);
                log_buffer.push_info(format!("⚠️ 启动 SSH 隧道失败: {}", e));
                false
            }
        }
    }

    /// 终止 SSH 反向隧道进程。
    fn stop_tunnel(&mut self, log_buffer: &mut LogBuffer) -> bool {
        if let Some(ref mut proc) = self.tunnel_process {
            match proc.try_wait() {
                Ok(Some(status)) => {
                    log::info!("[Worker] SSH 隧道已自行退出: {}", status);
                    self.tunnel_process = None;
                    return true;
                }
                Ok(None) => {
                    log::info!("[Worker] 正在终止 SSH 隧道进程...");
                    if let Err(e) = proc.kill() {
                        log::warn!("[Worker] 终止 SSH 隧道进程失败: {}", e);
                        log_buffer.push_info(format!("⚠️ 终止 SSH 隧道失败: {}", e));
                        return false;
                    }
                    // 使用 try_wait 轮询替代 wait()，避免阻塞主循环
                    let deadline = std::time::Instant::now() + std::time::Duration::from_secs(5);
                    let mut terminated = false;
                    while std::time::Instant::now() < deadline {
                        match proc.try_wait() {
                            Ok(Some(status)) => {
                                log::info!("[Worker] SSH 隧道已终止: {}", status);
                                log_buffer.push_info("✅ SSH 隧道已终止".to_string());
                                terminated = true;
                                break;
                            }
                            Ok(None) => std::thread::sleep(std::time::Duration::from_millis(100)),
                            Err(e) => {
                                log::warn!("[Worker] 检查 SSH 隧道退出状态失败: {}", e);
                                break;
                            }
                        }
                    }
                    if !terminated {
                        log::warn!("[Worker] 等待 SSH 隧道退出超时 (5s)");
                        log_buffer.push_info("⚠️ 等待 SSH 隧道退出超时".to_string());
                    }
                    self.tunnel_process = None;
                    return true;
                }
                Err(e) => {
                    log::warn!("[Worker] 检查 SSH 隧道状态失败: {}", e);
                    self.tunnel_process = None;
                    return false;
                }
            }
        }
        true
    }
}

impl Drop for WorkerManager {
    fn drop(&mut self) {
        // 确保子进程在 WorkerManager 被丢弃时被清理
        if let Some(ref mut proc) = self.worker_process {
            let _ = proc.kill();
            let _ = proc.wait();
        }
        if let Some(ref mut proc) = self.tunnel_process {
            let _ = proc.kill();
            let _ = proc.wait();
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn is_process_running_returns_false_for_none() {
        let mut proc: Option<Child> = None;
        assert!(!WorkerManager::is_process_running(&mut proc));
    }

    #[test]
    fn is_process_running_returns_true_for_running_process() {
        // Spawn a long-running process
        #[cfg(target_os = "windows")]
        let child = Command::new("cmd")
            .args(["/C", "timeout /t 30 /nobreak >nul"])
            .stdout(std::process::Stdio::null())
            .stderr(std::process::Stdio::null())
            .spawn()
            .expect("spawn test process");

        #[cfg(not(target_os = "windows"))]
        let mut child = Command::new("sleep")
            .arg("30")
            .stdout(std::process::Stdio::null())
            .stderr(std::process::Stdio::null())
            .spawn()
            .expect("spawn test process");

        let mut proc: Option<Child> = Some(child);
        assert!(WorkerManager::is_process_running(&mut proc));

        // Cleanup
        if let Some(ref mut p) = proc {
            let _ = p.kill();
            let _ = p.wait();
        }
    }

    #[test]
    fn is_process_running_returns_false_for_exited_process() {
        // Spawn a process that exits immediately
        #[cfg(target_os = "windows")]
        let child = Command::new("cmd")
            .args(["/C", "echo done"])
            .stdout(std::process::Stdio::null())
            .stderr(std::process::Stdio::null())
            .spawn()
            .expect("spawn test process");

        #[cfg(not(target_os = "windows"))]
        let child = Command::new("true")
            .stdout(std::process::Stdio::null())
            .stderr(std::process::Stdio::null())
            .spawn()
            .expect("spawn test process");

        let mut proc: Option<Child> = Some(child);
        // Wait a bit for the process to exit
        std::thread::sleep(std::time::Duration::from_millis(100));
        assert!(!WorkerManager::is_process_running(&mut proc));
    }

    #[test]
    fn worker_manager_initial_state_has_no_processes() {
        let mut wm = WorkerManager::new();
        assert!(!wm.is_worker_running());
        assert!(!wm.is_tunnel_running());
    }
}
