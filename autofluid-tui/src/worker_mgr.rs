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
        if let Some(ref mut proc) = self.worker_process {
            proc.try_wait().ok().flatten().is_none()
        } else {
            false
        }
    }

    /// SSH 隧道是否正在运行。
    pub fn is_tunnel_running(&mut self) -> bool {
        if let Some(ref mut proc) = self.tunnel_process {
            proc.try_wait().ok().flatten().is_none()
        } else {
            false
        }
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
                    // 等待进程退出
                    match proc.wait() {
                        Ok(status) => {
                            log::info!("[Worker] 本地 Worker 已终止: {}", status);
                            log_buffer.push_info("✅ 本地 Worker 已终止".to_string());
                        }
                        Err(e) => {
                            log::warn!("[Worker] 等待本地 Worker 退出失败: {}", e);
                        }
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
                    match proc.wait() {
                        Ok(status) => {
                            log::info!("[Worker] SSH 隧道已终止: {}", status);
                            log_buffer.push_info("✅ SSH 隧道已终止".to_string());
                        }
                        Err(e) => {
                            log::warn!("[Worker] 等待 SSH 隧道退出失败: {}", e);
                        }
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
