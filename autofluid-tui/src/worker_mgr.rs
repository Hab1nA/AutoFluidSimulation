//! Worker 进程管理器。
//!
//! 管理本地 LocalWorker 子进程和工作站 SSH 反向隧道进程的生命周期。
//! 与 `DaemonManager` 类似，通过子进程方式启动/停止 worker 相关进程。

use std::path::PathBuf;
use std::process::{Child, Command};
use std::time::Duration;

use crate::ipc::client::IpcClient;
use crate::state::LogBuffer;
use crate::utils::run_command_with_timeout;

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
    #[allow(dead_code)]
    pub fn start_workers(&mut self, project_dir: &str, log_buffer: &mut LogBuffer) -> bool {
        self.start_workers_with_prepare(project_dir, log_buffer, |_| true)
    }

    /// 启动所有 worker：建立 SSH 隧道 + 执行 daemon 端准备 + 启动本地 LocalWorker。
    pub fn start_workers_with_prepare<F>(
        &mut self,
        project_dir: &str,
        log_buffer: &mut LogBuffer,
        prepare_remote_workers: F,
    ) -> bool
    where
        F: FnOnce(&mut LogBuffer) -> bool,
    {
        log::info!("[Worker] 开始启动 Worker 进程");

        // 1. 启动工作站 SSH 反向隧道
        if !self.start_tunnel(project_dir, log_buffer) {
            log_buffer.push_info("❌ SSH 隧道启动失败，已停止 Worker 启动流程".to_string());
            return false;
        }

        // 2. 通知 daemon 端刷新配置并验证工作站 SSH 连通性
        if !prepare_remote_workers(log_buffer) {
            log_buffer
                .push_info("❌ daemon 端 Worker 准备失败，已停止本地 Worker 启动".to_string());
            return false;
        }

        // 3. 启动本地 LocalWorker 进程
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
    #[allow(dead_code)]
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

        let python = resolve_python_exe(project_dir);
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

        let powershell = resolve_powershell_exe();
        let mut cmd = Command::new(&powershell);
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

        match run_command_with_timeout(&mut cmd, Duration::from_secs(60)) {
            Ok(output) if output.status.success() => {
                log::info!(
                    "[Worker] SSH 反向隧道脚本已确认可达: powershell={}",
                    powershell
                );
                log_buffer.push_info("✅ SSH 反向隧道已建立并通过连通性检查".to_string());
                true
            }
            Ok(output) => {
                let stderr = String::from_utf8_lossy(&output.stderr).trim().to_string();
                let stdout = String::from_utf8_lossy(&output.stdout).trim().to_string();
                let detail = if stderr.is_empty() { stdout } else { stderr };
                let message = if detail.is_empty() {
                    format!("PowerShell 退出状态: {}", output.status)
                } else {
                    format!("PowerShell 退出状态: {}, {}", output.status, detail)
                };
                log::error!("[Worker] 启动 SSH 反向隧道失败: {}", message);
                log_buffer.push_info(format!("⚠️ 启动 SSH 隧道失败: {}", message));
                false
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

fn resolve_python_exe(project_dir: &str) -> String {
    let project_python = PathBuf::from(project_dir)
        .join(".venv")
        .join("Scripts")
        .join("python.exe");
    if project_python.exists() {
        return project_python.to_string_lossy().to_string();
    }
    std::env::var("PYTHON").unwrap_or_else(|_| "python".to_string())
}

pub fn prepare_remote_workers(
    ipc: &mut IpcClient,
    rt: &tokio::runtime::Runtime,
    log_buffer: &mut LogBuffer,
) -> bool {
    if !ipc.is_connected() {
        log_buffer.push_info("❌ 未连接到后台引擎，无法准备 daemon 端 Worker".to_string());
        return false;
    }
    log_buffer.push_info("🔧 正在请求 daemon 验证工作站 SSH 连通性...".to_string());
    match rt.block_on(ipc.worker_start()) {
        Ok(resp) if resp.is_ok() => {
            log_buffer.push_info(format!("✅ {}", resp.message));
            log_worker_ssh_checks(&resp.data, log_buffer);
            true
        }
        Ok(resp) => {
            log_buffer.push_info(format!("❌ {}", resp.message));
            log_worker_ssh_checks(&resp.data, log_buffer);
            false
        }
        Err(e) => {
            log_buffer.push_info(format!("❌ 通信失败: {}", e));
            false
        }
    }
}

fn log_worker_ssh_checks(data: &serde_json::Value, log_buffer: &mut LogBuffer) {
    let Some(checks) = data.get("ssh_checks").and_then(|value| value.as_object()) else {
        return;
    };
    for (workstation_id, status) in checks {
        let status_text = status.as_str().unwrap_or("unknown");
        if status_text == "ok" {
            log_buffer.push_info(format!("  ✅ 工作站 {workstation_id}: SSH ok"));
        } else {
            log_buffer.push_info(format!("  ❌ 工作站 {workstation_id}: SSH {status_text}"));
        }
    }
}

fn resolve_powershell_exe() -> String {
    std::env::var("AUTOFLUID_POWERSHELL_EXE")
        .ok()
        .filter(|value| !value.trim().is_empty())
        .unwrap_or_else(|| "powershell".to_string())
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

    #[test]
    fn start_workers_with_prepare_runs_tunnel_then_daemon_prepare_then_local_worker() {
        let project_dir = std::env::temp_dir().join(format!(
            "autofluid-tui-worker-test-{}",
            crate::generate_request_id()
        ));
        std::fs::create_dir_all(project_dir.join("scripts")).expect("create scripts dir");
        std::fs::write(
            project_dir
                .join("scripts")
                .join("start_workstation_reverse_tunnel.ps1"),
            "Write-Host tunnel",
        )
        .expect("write tunnel script");
        std::fs::write(project_dir.join("main.py"), "print('worker')").expect("write main.py");
        let marker = project_dir.join("worker-order.log");
        let powershell_exe = fake_marker_exe(&project_dir, "fake_pwsh", &marker, "tunnel");
        let python_exe = fake_marker_exe(&project_dir, "fake_python", &marker, "worker");
        std::env::set_var("AUTOFLUID_POWERSHELL_EXE", &powershell_exe);
        std::env::set_var("PYTHON", &python_exe);

        let mut wm = WorkerManager::new();
        let mut log_buffer = LogBuffer::new();
        let result = wm.start_workers_with_prepare(
            project_dir.to_str().expect("utf8 temp path"),
            &mut log_buffer,
            |buffer| {
                buffer.push_info("daemon prepare".to_string());
                std::fs::OpenOptions::new()
                    .create(true)
                    .append(true)
                    .open(&marker)
                    .and_then(|mut file| {
                        use std::io::Write;
                        writeln!(file, "daemon")
                    })
                    .expect("write daemon marker");
                true
            },
        );

        assert!(result);
        let order = wait_for_marker_lines(&marker, 3);
        let lines: Vec<&str> = order.lines().collect();
        assert_eq!(lines, vec!["tunnel", "daemon", "worker"]);

        let _ = wm.stop_workers(&mut log_buffer);
        std::env::remove_var("AUTOFLUID_POWERSHELL_EXE");
        std::env::remove_var("PYTHON");
        let _ = std::fs::remove_dir_all(project_dir);
    }

    fn wait_for_marker_lines(marker: &std::path::Path, expected_lines: usize) -> String {
        let deadline = std::time::Instant::now() + std::time::Duration::from_secs(2);
        loop {
            if let Ok(order) = std::fs::read_to_string(marker) {
                if order.lines().count() >= expected_lines {
                    return order;
                }
            }
            if std::time::Instant::now() >= deadline {
                return std::fs::read_to_string(marker).unwrap_or_default();
            }
            std::thread::sleep(std::time::Duration::from_millis(20));
        }
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
            std::fs::write(
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
            std::fs::write(
                &path,
                format!(
                    "#!/bin/sh\necho {label} >> '{}'\nexit 0\n",
                    marker.display()
                ),
            )
            .expect("write fake marker shell");
            use std::os::unix::fs::PermissionsExt;
            let mut permissions = std::fs::metadata(&path)
                .expect("fake marker metadata")
                .permissions();
            permissions.set_mode(0o755);
            std::fs::set_permissions(&path, permissions).expect("chmod fake marker");
            path
        }
    }
}
