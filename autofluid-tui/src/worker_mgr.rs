//! Worker 进程管理器。
//!
//! 管理本地 LocalWorker 子进程和工作站 SSH 反向隧道进程的生命周期。
//! 与 `DaemonManager` 类似，通过子进程方式启动/停止 worker 相关进程。

use std::path::PathBuf;
use std::process::{Child, Command};
use std::time::Duration;

use crate::ipc::client::IpcClient;
use crate::settings::{SettingsConfig, WorkstationConfig};
use crate::state::LogBuffer;
use crate::utils::{is_pid_alive, kill_process_tree, run_command_with_timeout, wait_for_pid_dead};

/// 进程终止结果
enum StopResult {
    /// 没有进程需要终止
    NoProcess,
    /// 进程已自行退出
    AlreadyExited,
    /// 成功终止
    Terminated,
    /// 终止过程中出错
    Error(String),
}

#[derive(Clone, Copy)]
enum WorkerPidKind {
    LocalWorker,
    TunnelWorkstation,
    TunnelLocalWorker,
}

#[derive(Clone, Debug, PartialEq, Eq)]
struct WorkstationTunnelSpec {
    id: String,
    remote_host: String,
    remote_port: u16,
    target_host: String,
    target_port: u16,
}

impl WorkerPidKind {
    fn filename(self) -> &'static str {
        match self {
            Self::LocalWorker => "local_worker.pid",
            Self::TunnelWorkstation => "tunnel_workstation.pid",
            Self::TunnelLocalWorker => "tunnel_localworker.pid",
        }
    }

    fn label(self) -> &'static str {
        match self {
            Self::LocalWorker => "本地 Worker",
            Self::TunnelWorkstation => "工作站 SSH 隧道",
            Self::TunnelLocalWorker => "本机 SSH 隧道",
        }
    }
}

/// WorkerManager 管理本地 LocalWorker 进程和 SSH 隧道进程。
pub struct WorkerManager {
    /// 本地 LocalWorker 子进程
    worker_process: Option<Child>,
    /// 本地 LocalWorker 是常驻进程，TUI 退出时不应隐式杀掉；显式 stop/restart 时再清理。
    worker_started_detached: bool,
    /// 工作站 SSH 反向隧道子进程
    workstation_tunnel_process: Option<Child>,
    /// 服务器到本机 LocalWorker 的 SSH 反向隧道子进程
    local_worker_tunnel_process: Option<Child>,
    /// 最近一次启动 worker 时使用的项目目录，用于跨会话 PID 文件清理。
    project_dir: PathBuf,
}

impl WorkerManager {
    pub fn new() -> Self {
        Self {
            worker_process: None,
            worker_started_detached: false,
            workstation_tunnel_process: None,
            local_worker_tunnel_process: None,
            project_dir: std::env::current_dir().unwrap_or_else(|_| PathBuf::from(".")),
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
        log::info!("开始启动 Worker 进程");
        self.project_dir = PathBuf::from(project_dir);

        // 1. 启动所有工作站 SSH 反向隧道
        if !self.start_workstation_tunnels(project_dir, log_buffer) {
            log_buffer.push_info("❌ 工作站 SSH 隧道启动失败，已停止 Worker 启动流程".to_string());
            return false;
        }

        // 2. 启动服务器到本机的 SSH 反向隧道
        if !self.start_tunnel(project_dir, log_buffer, "LocalWorker") {
            log_buffer.push_info("❌ 本机 SSH 隧道启动失败，已停止 Worker 启动流程".to_string());
            return false;
        }

        // 3. 通知 daemon 端刷新配置并验证工作站 SSH 连通性
        if !prepare_remote_workers(log_buffer) {
            log_buffer
                .push_info("❌ daemon 端 Worker 准备失败，已停止本地 Worker 启动".to_string());
            return false;
        }

        // 4. 启动本地 LocalWorker 进程
        if !self.start_local_worker(project_dir, log_buffer) {
            log_buffer.push_info("❌ 本地 Worker 启动失败".to_string());
            return false;
        }

        log_buffer.push_info("✅ Worker 启动流程完成".to_string());
        true
    }

    /// 停止所有 worker：终止本地 Worker 进程 + 关闭 SSH 隧道。
    pub fn stop_workers(&mut self, log_buffer: &mut LogBuffer) -> bool {
        self.stop_workers_for_project(None, log_buffer)
    }

    /// 停止所有 worker，并用项目目录中的 PID 文件清理跨会话残留进程。
    pub fn stop_workers_for_project(
        &mut self,
        project_dir: Option<&str>,
        log_buffer: &mut LogBuffer,
    ) -> bool {
        if let Some(project_dir) = project_dir {
            self.project_dir = PathBuf::from(project_dir);
        }
        log::info!("开始停止 Worker 进程");
        let mut success = true;

        // 1. 终止本地 LocalWorker 进程
        if !self.stop_local_worker(log_buffer) {
            success = false;
        }

        // 2. 终止 SSH 隧道进程
        if !self.stop_tunnel(log_buffer) {
            success = false;
        }

        for kind in [WorkerPidKind::LocalWorker, WorkerPidKind::TunnelLocalWorker] {
            if !Self::cleanup_pid_file_for_path(&self.project_dir, kind) {
                log_buffer.push_info(format!("⚠️ {} PID 文件清理失败", kind.label()));
                success = false;
            }
        }
        if !Self::cleanup_workstation_tunnel_pid_files_for_path(&self.project_dir) {
            log_buffer.push_info("⚠️ 工作站 SSH 隧道 PID 文件清理失败".to_string());
            success = false;
        }

        Self::uninstall_tunnel_watchdogs_for_path(&self.project_dir, log_buffer);

        if success {
            log_buffer.push_info("✅ 所有 Worker 已停止".to_string());
        }
        success
    }

    /// 重启所有 worker：先停止再启动。
    #[allow(dead_code)]
    pub fn restart_workers(&mut self, project_dir: &str, log_buffer: &mut LogBuffer) -> bool {
        log::info!("开始重启 Worker 进程");
        self.stop_workers(log_buffer);
        std::thread::sleep(Duration::from_secs(1));
        self.start_workers(project_dir, log_buffer)
    }

    /// 本地 LocalWorker 是否正在运行。
    pub fn is_worker_running(&mut self) -> bool {
        Self::is_process_running(&mut self.worker_process)
    }

    /// SSH 隧道是否正在运行。
    #[allow(dead_code)]
    pub fn is_tunnel_running(&mut self) -> bool {
        Self::is_process_running(&mut self.workstation_tunnel_process)
            || Self::is_process_running(&mut self.local_worker_tunnel_process)
    }

    // ------------------------------------------------------------------
    // 内部方法
    // ------------------------------------------------------------------

    fn start_workstation_tunnels(&mut self, project_dir: &str, log_buffer: &mut LogBuffer) -> bool {
        let specs = workstation_tunnel_specs_for_project(project_dir);
        let mut success = true;
        for spec in specs {
            if !self.start_workstation_tunnel(project_dir, log_buffer, &spec) {
                success = false;
            }
        }
        success
    }

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
            "启动本地 LocalWorker: python={}, script={}",
            python,
            worker_script.display()
        );

        let mut cmd = Command::new(&python);
        cmd.arg(&worker_script)
            .arg("--worker")
            .current_dir(project_dir)
            .env(
                "AUTOFLUID_WORKER_REACHABLE_HOST",
                env_or_default("AUTOFLUID_WORKER_REACHABLE_HOST", "127.0.0.1"),
            )
            .env(
                "AUTOFLUID_WORKER_SSH_PORT",
                env_or_default("AUTOFLUID_WORKER_SSH_PORT", "2223"),
            )
            .env(
                "AUTOFLUID_WORKER_CONNECTIVITY_MODE",
                env_or_default("AUTOFLUID_WORKER_CONNECTIVITY_MODE", "reverse_tunnel"),
            )
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
                let _ = Self::write_pid_file(project_dir, WorkerPidKind::LocalWorker, pid);
                log::info!("本地 LocalWorker 已启动: pid={}", pid);
                log_buffer.push_info(format!("✅ 本地 Worker 已启动 (PID: {})", pid));
                self.worker_process = Some(child);
                self.worker_started_detached = true;
                true
            }
            Err(e) => {
                log::error!("启动本地 LocalWorker 失败: {}", e);
                log_buffer.push_info(format!("❌ 启动本地 Worker 失败: {}", e));
                false
            }
        }
    }

    /// 终止本地 LocalWorker 子进程。
    fn stop_local_worker(&mut self, log_buffer: &mut LogBuffer) -> bool {
        let result = Self::stop_child_process(&mut self.worker_process, "本地 Worker");
        match result {
            StopResult::NoProcess => true,
            StopResult::AlreadyExited => {
                self.worker_started_detached = false;
                log_buffer.push_info("✅ 本地 Worker 已终止".to_string());
                true
            }
            StopResult::Terminated => {
                self.worker_started_detached = false;
                log_buffer.push_info("✅ 本地 Worker 已终止".to_string());
                true
            }
            StopResult::Error(e) => {
                self.worker_started_detached = false;
                log_buffer.push_info(format!("⚠️ 终止本地 Worker 失败: {}", e));
                false
            }
        }
    }

    /// 启动指定方向的 SSH 反向隧道。
    fn start_tunnel(
        &mut self,
        project_dir: &str,
        log_buffer: &mut LogBuffer,
        tunnel_kind: &str,
    ) -> bool {
        let tunnel_process = match tunnel_kind {
            "LocalWorker" => &mut self.local_worker_tunnel_process,
            _ => &mut self.workstation_tunnel_process,
        };

        if Self::is_process_running(tunnel_process) {
            log_buffer.push_info("⚠️ SSH 隧道已在运行中".to_string());
            return true;
        }
        *tunnel_process = None;

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

        log::info!("启动 SSH 反向隧道: script={}", tunnel_script.display());

        let powershell = resolve_powershell_exe();
        let mut cmd = Command::new(&powershell);
        cmd.args([
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            &tunnel_script.to_string_lossy(),
            "-TunnelKind",
            tunnel_kind,
        ])
        .env(
            "AUTOFLUID_TUNNEL_PID_FILE",
            Self::pid_file_for(
                project_dir,
                if tunnel_kind == "LocalWorker" {
                    WorkerPidKind::TunnelLocalWorker
                } else {
                    WorkerPidKind::TunnelWorkstation
                },
            ),
        )
        .current_dir(project_dir);
        if tunnel_kind == "LocalWorker" {
            cmd.arg("-NoWatchdog");
        }

        #[cfg(target_os = "windows")]
        {
            use std::os::windows::process::CommandExt;
            use windows_sys::Win32::System::Threading::CREATE_NO_WINDOW;
            cmd.creation_flags(CREATE_NO_WINDOW);
        }

        match run_command_with_timeout(&mut cmd, Duration::from_secs(60)) {
            Ok(output) if output.status.success() => {
                log::info!(
                    "{} SSH 反向隧道脚本已确认可达: powershell={}",
                    tunnel_kind,
                    powershell
                );
                log_buffer.push_info(format!(
                    "✅ {} SSH 反向隧道已建立并通过连通性检查",
                    tunnel_kind
                ));
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
                log::error!("启动 SSH 反向隧道失败: {}", message);
                log_buffer.push_info(format!("⚠️ 启动 SSH 隧道失败: {}", message));
                false
            }
            Err(e) => {
                log::error!("启动 SSH 反向隧道失败: {}", e);
                log_buffer.push_info(format!("⚠️ 启动 SSH 隧道失败: {}", e));
                false
            }
        }
    }

    fn start_workstation_tunnel(
        &mut self,
        project_dir: &str,
        log_buffer: &mut LogBuffer,
        spec: &WorkstationTunnelSpec,
    ) -> bool {
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
            "启动工作站 SSH 反向隧道: workstation={}, remote={}:{}, target={}:{}",
            spec.id,
            spec.remote_host,
            spec.remote_port,
            spec.target_host,
            spec.target_port
        );

        let powershell = resolve_powershell_exe();
        let mut cmd = Command::new(&powershell);
        cmd.args([
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            &tunnel_script.to_string_lossy(),
            "-TunnelKind",
            "Workstation",
        ])
        .env("AUTOFLUID_SSH_REACHABLE_HOST", &spec.remote_host)
        .env("AUTOFLUID_SSH_REACHABLE_PORT", spec.remote_port.to_string())
        .env(
            "AUTOFLUID_WORKSTATION_TUNNEL_TARGET_HOST",
            &spec.target_host,
        )
        .env(
            "AUTOFLUID_WORKSTATION_TUNNEL_TARGET_PORT",
            spec.target_port.to_string(),
        )
        .env(
            "AUTOFLUID_TUNNEL_PID_FILE",
            workstation_tunnel_pid_file(project_dir, spec.remote_port),
        )
        .current_dir(project_dir);

        #[cfg(target_os = "windows")]
        {
            use std::os::windows::process::CommandExt;
            use windows_sys::Win32::System::Threading::CREATE_NO_WINDOW;
            cmd.creation_flags(CREATE_NO_WINDOW);
        }

        match run_command_with_timeout(&mut cmd, Duration::from_secs(60)) {
            Ok(output) if output.status.success() => {
                log_buffer.push_info(format!(
                    "✅ 工作站 {} SSH 反向隧道已建立: {}:{} -> {}:{}",
                    spec.id, spec.remote_host, spec.remote_port, spec.target_host, spec.target_port
                ));
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
                log::error!("启动工作站 {} SSH 反向隧道失败: {}", spec.id, message);
                log_buffer.push_info(format!(
                    "⚠️ 工作站 {} SSH 隧道启动失败: {}",
                    spec.id, message
                ));
                false
            }
            Err(e) => {
                log::error!("启动工作站 {} SSH 反向隧道失败: {}", spec.id, e);
                log_buffer.push_info(format!("⚠️ 工作站 {} SSH 隧道启动失败: {}", spec.id, e));
                false
            }
        }
    }

    /// 终止 SSH 反向隧道进程。
    fn stop_tunnel(&mut self, log_buffer: &mut LogBuffer) -> bool {
        let workstation_ok =
            Self::stop_tunnel_process(&mut self.workstation_tunnel_process, log_buffer);
        let local_worker_ok =
            Self::stop_tunnel_process(&mut self.local_worker_tunnel_process, log_buffer);
        workstation_ok && local_worker_ok
    }

    fn stop_tunnel_process(proc_slot: &mut Option<Child>, log_buffer: &mut LogBuffer) -> bool {
        let result = Self::stop_child_process(proc_slot, "SSH 隧道");
        match result {
            StopResult::NoProcess => true,
            StopResult::AlreadyExited => {
                log_buffer.push_info("✅ SSH 隧道已终止".to_string());
                true
            }
            StopResult::Terminated => {
                log_buffer.push_info("✅ SSH 隧道已终止".to_string());
                true
            }
            StopResult::Error(e) => {
                log_buffer.push_info(format!("⚠️ 终止 SSH 隧道失败: {}", e));
                false
            }
        }
    }

    /// 通用进程终止辅助方法。
    ///
    /// 先检查进程是否已退出，若仍在运行则发送 kill 信号并立即释放管理状态。
    fn stop_child_process(proc_slot: &mut Option<Child>, name: &str) -> StopResult {
        let Some(ref mut proc) = proc_slot else {
            return StopResult::NoProcess;
        };
        match proc.try_wait() {
            Ok(Some(status)) => {
                log::info!("{} 已自行退出: {}", name, status);
                *proc_slot = None;
                StopResult::AlreadyExited
            }
            Ok(None) => {
                log::info!("正在终止 {}...", name);
                if let Err(e) = proc.kill() {
                    log::warn!("终止 {} 失败: {}", name, e);
                    *proc_slot = None;
                    return StopResult::Error(e.to_string());
                }
                match proc.try_wait() {
                    Ok(Some(status)) => log::info!("{} 已终止: {}", name, status),
                    Ok(None) => log::info!("{} 已发送终止信号", name),
                    Err(e) => log::warn!("检查 {} 退出状态失败: {}", name, e),
                }
                *proc_slot = None;
                StopResult::Terminated
            }
            Err(e) => {
                log::warn!("检查 {} 状态失败: {}", name, e);
                *proc_slot = None;
                StopResult::Error(e.to_string())
            }
        }
    }

    fn pid_file_for(project_dir: &str, kind: WorkerPidKind) -> PathBuf {
        PathBuf::from(project_dir)
            .join("data")
            .join(kind.filename())
    }

    fn write_pid_file(project_dir: &str, kind: WorkerPidKind, pid: u32) -> std::io::Result<()> {
        let pid_file = Self::pid_file_for(project_dir, kind);
        if let Some(parent) = pid_file.parent() {
            std::fs::create_dir_all(parent)?;
        }
        std::fs::write(pid_file, pid.to_string())
    }

    fn cleanup_pid_file_for_path(project_dir: &std::path::Path, kind: WorkerPidKind) -> bool {
        let pid_file = project_dir.join("data").join(kind.filename());
        Self::cleanup_pid_file(&pid_file)
    }

    fn cleanup_pid_file(pid_file: &std::path::Path) -> bool {
        let Ok(raw_pid) = std::fs::read_to_string(pid_file) else {
            return true;
        };
        let pid = raw_pid.trim().parse::<u32>().ok();
        let mut success = true;
        if let Some(pid) = pid {
            let killed = kill_process_tree(pid);
            success =
                !is_pid_alive(pid) || (killed && wait_for_pid_dead(pid, Duration::from_secs(2)));
        }
        if success {
            let _ = std::fs::remove_file(pid_file);
        }
        success
    }

    fn cleanup_workstation_tunnel_pid_files_for_path(project_dir: &std::path::Path) -> bool {
        let data_dir = project_dir.join("data");
        let mut pid_files = vec![data_dir.join(WorkerPidKind::TunnelWorkstation.filename())];
        if let Ok(entries) = std::fs::read_dir(&data_dir) {
            for entry in entries.flatten() {
                let path = entry.path();
                let Some(name) = path.file_name().and_then(|value| value.to_str()) else {
                    continue;
                };
                if name.starts_with("tunnel_workstation_") && name.ends_with(".pid") {
                    pid_files.push(path);
                }
            }
        }
        pid_files.sort();
        pid_files.dedup();
        pid_files
            .iter()
            .all(|pid_file| Self::cleanup_pid_file(pid_file))
    }

    fn uninstall_tunnel_watchdogs_for_path(
        project_dir: &std::path::Path,
        log_buffer: &mut LogBuffer,
    ) {
        let tunnel_script = project_dir
            .join("scripts")
            .join("start_workstation_reverse_tunnel.ps1");
        if !tunnel_script.exists() {
            return;
        }

        for spec in workstation_tunnel_specs_for_path(project_dir) {
            let mut cmd = watchdog_uninstall_command(&tunnel_script, "Workstation");
            cmd.env("AUTOFLUID_SSH_REACHABLE_HOST", &spec.remote_host)
                .env("AUTOFLUID_SSH_REACHABLE_PORT", spec.remote_port.to_string());
            cmd.current_dir(project_dir);
            match run_command_with_timeout(&mut cmd, Duration::from_secs(10)) {
                Ok(output) if output.status.success() => {}
                Ok(output) => {
                    let stderr = String::from_utf8_lossy(&output.stderr).trim().to_string();
                    let stdout = String::from_utf8_lossy(&output.stdout).trim().to_string();
                    let detail = if stderr.is_empty() { stdout } else { stderr };
                    log::warn!(
                        "Workstation {} watchdog 卸载失败: status={}, detail={}",
                        spec.id,
                        output.status,
                        detail
                    );
                    log_buffer.push_info(format!("⚠️ 工作站 {} watchdog 卸载失败", spec.id));
                }
                Err(err) => {
                    log::warn!("Workstation {} watchdog 卸载失败: {}", spec.id, err);
                    log_buffer.push_info(format!("⚠️ 工作站 {} watchdog 卸载失败", spec.id));
                }
            }
        }

        {
            let tunnel_kind = "LocalWorker";
            let mut cmd = watchdog_uninstall_command(&tunnel_script, "LocalWorker");
            cmd.current_dir(project_dir);
            match run_command_with_timeout(&mut cmd, Duration::from_secs(10)) {
                Ok(output) if output.status.success() => {}
                Ok(output) => {
                    let stderr = String::from_utf8_lossy(&output.stderr).trim().to_string();
                    let stdout = String::from_utf8_lossy(&output.stdout).trim().to_string();
                    let detail = if stderr.is_empty() { stdout } else { stderr };
                    log::warn!(
                        "{} watchdog 卸载失败: status={}, detail={}",
                        tunnel_kind,
                        output.status,
                        detail
                    );
                    log_buffer.push_info(format!("⚠️ {} watchdog 卸载失败", tunnel_kind));
                }
                Err(err) => {
                    log::warn!("{} watchdog 卸载失败: {}", tunnel_kind, err);
                    log_buffer.push_info(format!("⚠️ {} watchdog 卸载失败", tunnel_kind));
                }
            }
        }
    }
}

fn workstation_tunnel_pid_file(project_dir: &str, remote_port: u16) -> PathBuf {
    PathBuf::from(project_dir)
        .join("data")
        .join(format!("tunnel_workstation_{remote_port}.pid"))
}

fn workstation_tunnel_specs_for_project(project_dir: &str) -> Vec<WorkstationTunnelSpec> {
    workstation_tunnel_specs_for_path(&PathBuf::from(project_dir))
}

fn workstation_tunnel_specs_for_path(project_dir: &std::path::Path) -> Vec<WorkstationTunnelSpec> {
    let config = std::fs::read_to_string(project_dir.join("autofluid_config.toml"))
        .ok()
        .and_then(|contents| toml::from_str::<SettingsConfig>(&contents).ok())
        .unwrap_or_default();
    let workstations = if config.workstations.is_empty() {
        vec![default_workstation_from_remote_config(&config)]
    } else {
        config.workstations
    };
    workstations
        .iter()
        .enumerate()
        .filter_map(|(idx, workstation)| workstation_tunnel_spec(workstation, idx))
        .collect()
}

fn workstation_tunnel_spec(
    workstation: &WorkstationConfig,
    index: usize,
) -> Option<WorkstationTunnelSpec> {
    let id = if workstation.id.trim().is_empty() {
        format!("WS-{}", index + 1)
    } else {
        workstation.id.trim().to_string()
    };
    let token = workstation_env_token(&id);
    let remote_host = first_non_empty(&[
        Some(workstation.reachable_host.as_str()),
        env_str(&format!("AUTOFLUID_{token}_SSH_REACHABLE_HOST")).as_deref(),
        env_str("AUTOFLUID_SSH_REACHABLE_HOST").as_deref(),
        Some("127.0.0.1"),
    ])?;
    let remote_port = workstation
        .reachable_port
        .or_else(|| env_u16(&format!("AUTOFLUID_{token}_SSH_REACHABLE_PORT")))
        .or_else(|| {
            if index == 0 {
                env_u16("AUTOFLUID_SSH_REACHABLE_PORT")
            } else {
                None
            }
        })
        .unwrap_or_else(|| default_workstation_tunnel_port(index));
    Some(WorkstationTunnelSpec {
        id,
        remote_host,
        remote_port,
        target_host: workstation.host.clone(),
        target_port: workstation.port,
    })
}

fn default_workstation_from_remote_config(config: &SettingsConfig) -> WorkstationConfig {
    WorkstationConfig {
        id: "WS-A".to_string(),
        host: config.remote_config.host.clone(),
        port: config.remote_config.port,
        username: config.remote_config.username.clone(),
        working_dir: config.remote_config.working_dir.clone(),
        scripts_dir: config.remote_config.scripts_dir.clone(),
        ref_files_dir: config.remote_config.ref_files_dir.clone(),
        scdoc_dir: config.remote_config.scdoc_dir.clone(),
        msh_dir: config.remote_config.msh_dir.clone(),
        result_dir: config.remote_config.result_dir.clone(),
        animation_dir: config.remote_config.animation_dir.clone(),
        flag_dir: config.remote_config.flag_dir.clone(),
        conda_env: config.remote_config.conda_env.clone(),
        conda_exe: config.remote_config.conda_exe.clone(),
        mpi_bin_dir: config.remote_config.mpi_bin_dir.clone(),
        ..Default::default()
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

fn default_workstation_tunnel_port(index: usize) -> u16 {
    if index == 0 {
        2222
    } else {
        2224u16.saturating_add(index.saturating_sub(1) as u16)
    }
}

fn env_str(name: &str) -> Option<String> {
    std::env::var(name)
        .ok()
        .filter(|value| !value.trim().is_empty())
}

fn env_u16(name: &str) -> Option<u16> {
    env_str(name).and_then(|value| value.parse::<u16>().ok())
}

fn first_non_empty(values: &[Option<&str>]) -> Option<String> {
    values
        .iter()
        .flatten()
        .map(|value| value.trim())
        .find(|value| !value.is_empty())
        .map(ToString::to_string)
}

fn watchdog_uninstall_command(tunnel_script: &std::path::Path, tunnel_kind: &str) -> Command {
    #[cfg(target_os = "windows")]
    let mut cmd = {
        let powershell = resolve_powershell_exe();
        let mut cmd = Command::new(&powershell);
        cmd.args([
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            &tunnel_script.to_string_lossy(),
        ]);
        cmd
    };

    #[cfg(not(target_os = "windows"))]
    let mut cmd = Command::new(tunnel_script);

    cmd.args(["-TunnelKind", tunnel_kind, "-UninstallWatchdog"]);

    #[cfg(target_os = "windows")]
    {
        use std::os::windows::process::CommandExt;
        use windows_sys::Win32::System::Threading::CREATE_NO_WINDOW;
        cmd.creation_flags(CREATE_NO_WINDOW);
    }

    cmd
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
    let targets = data
        .get("workstation_ssh_targets")
        .and_then(|value| value.as_object());
    for (workstation_id, status) in checks {
        let status_text = status.as_str().unwrap_or("unknown");
        let target_text = targets
            .and_then(|items| items.get(workstation_id))
            .and_then(format_worker_ssh_target)
            .map(|target| format!(" ({target})"))
            .unwrap_or_default();
        if status_text == "ok" {
            log_buffer.push_info(format!("✅ 工作站 {workstation_id}: SSH ok{target_text}"));
        } else {
            log_buffer.push_info(format!(
                "❌ 工作站 {workstation_id}: SSH {status_text}{target_text}"
            ));
        }
    }
}

fn format_worker_ssh_target(value: &serde_json::Value) -> Option<String> {
    let obj = value.as_object()?;
    let host = obj.get("host").and_then(|v| v.as_str()).unwrap_or("");
    let port = obj.get("port").and_then(|v| v.as_i64()).unwrap_or(22);
    let mode = obj
        .get("connectivity_mode")
        .and_then(|v| v.as_str())
        .unwrap_or("direct");
    if host.is_empty() {
        return Some(format!("target=:{} mode={}", port, mode));
    }
    Some(format!("target={host}:{port} mode={mode}"))
}

fn resolve_powershell_exe() -> String {
    std::env::var("AUTOFLUID_POWERSHELL_EXE")
        .ok()
        .filter(|value| !value.trim().is_empty())
        .unwrap_or_else(|| "powershell".to_string())
}

fn env_or_default(name: &str, default: &str) -> String {
    std::env::var(name)
        .ok()
        .filter(|value| !value.trim().is_empty())
        .unwrap_or_else(|| default.to_string())
}

impl Drop for WorkerManager {
    fn drop(&mut self) {
        // 随 TUI 启动的 LocalWorker 需要跨客户端重连继续运行；显式 worker stop 会清理。
        if !self.worker_started_detached {
            if let Some(ref mut proc) = self.worker_process {
                let _ = proc.kill();
                let _ = proc.wait();
            }
        }
        if let Some(ref mut proc) = self.workstation_tunnel_process {
            let _ = proc.kill();
            let _ = proc.wait();
        }
        if let Some(ref mut proc) = self.local_worker_tunnel_process {
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
    fn worker_pid_file_path_uses_project_data_dir() {
        let project_dir = std::env::temp_dir().join(format!(
            "autofluid-tui-worker-pid-path-{}",
            crate::generate_request_id()
        ));

        assert_eq!(
            WorkerManager::pid_file_for(
                project_dir.to_str().expect("utf8 temp path"),
                WorkerPidKind::LocalWorker
            ),
            project_dir.join("data").join("local_worker.pid")
        );
        assert_eq!(
            WorkerManager::pid_file_for(
                project_dir.to_str().expect("utf8 temp path"),
                WorkerPidKind::TunnelWorkstation
            ),
            project_dir.join("data").join("tunnel_workstation.pid")
        );
        assert_eq!(
            WorkerManager::pid_file_for(
                project_dir.to_str().expect("utf8 temp path"),
                WorkerPidKind::TunnelLocalWorker
            ),
            project_dir.join("data").join("tunnel_localworker.pid")
        );
    }

    #[test]
    fn cleanup_pid_file_removes_stale_pid_file_without_process_handle() {
        let project_dir = std::env::temp_dir().join(format!(
            "autofluid-tui-worker-stale-pid-{}",
            crate::generate_request_id()
        ));
        std::fs::create_dir_all(project_dir.join("data")).expect("create data dir");
        let pid_file = project_dir.join("data").join("local_worker.pid");
        std::fs::write(&pid_file, "999999").expect("write pid");

        let result =
            WorkerManager::cleanup_pid_file_for_path(&project_dir, WorkerPidKind::LocalWorker);

        assert!(result);
        assert!(!pid_file.exists());
        let _ = std::fs::remove_dir_all(project_dir);
    }

    #[test]
    fn cleanup_pid_file_terminates_live_process_without_process_handle() {
        let project_dir = std::env::temp_dir().join(format!(
            "autofluid-tui-worker-live-pid-{}",
            crate::generate_request_id()
        ));
        std::fs::create_dir_all(project_dir.join("data")).expect("create data dir");
        let pid_file = project_dir.join("data").join("tunnel_localworker.pid");

        #[cfg(target_os = "windows")]
        let mut child = Command::new("cmd")
            .args(["/C", "ping -n 30 127.0.0.1 >nul"])
            .stdout(std::process::Stdio::null())
            .stderr(std::process::Stdio::null())
            .spawn()
            .expect("spawn live pid process");

        #[cfg(not(target_os = "windows"))]
        let mut child = Command::new("sleep")
            .arg("30")
            .stdout(std::process::Stdio::null())
            .stderr(std::process::Stdio::null())
            .spawn()
            .expect("spawn live pid process");

        std::fs::write(&pid_file, child.id().to_string()).expect("write pid");

        let result = WorkerManager::cleanup_pid_file_for_path(
            &project_dir,
            WorkerPidKind::TunnelLocalWorker,
        );

        assert!(result);
        assert!(!pid_file.exists());
        let exited = wait_for_pid_dead(child.id(), Duration::from_secs(2));
        if !exited {
            let _ = child.kill();
            let _ = child.wait();
        }
        assert!(
            exited,
            "cleanup should terminate the live PID from the file"
        );
        let _ = std::fs::remove_dir_all(project_dir);
    }

    #[test]
    fn stop_workers_for_project_preserves_server_ipc_tunnel_pid_file() {
        let project_dir = std::env::temp_dir().join(format!(
            "autofluid-tui-server-ipc-pid-{}",
            crate::generate_request_id()
        ));
        std::fs::create_dir_all(project_dir.join("data")).expect("create data dir");
        let pid_file = project_dir.join("data").join("server_ipc_tunnel.pid");
        std::fs::write(&pid_file, "999999").expect("write pid");

        let mut log_buffer = LogBuffer::new();
        let mut worker = WorkerManager::new();
        let result = worker.stop_workers_for_project(
            Some(project_dir.to_str().expect("utf8 temp path")),
            &mut log_buffer,
        );

        assert!(result);
        assert!(pid_file.exists());
        let _ = std::fs::remove_dir_all(project_dir);
    }

    #[test]
    fn stop_workers_for_project_uninstalls_tunnel_watchdogs() {
        let _guard = crate::TEST_ENV_LOCK.lock().expect("env lock poisoned");
        let project_dir = std::env::temp_dir().join(format!(
            "autofluid-tui-watchdog-cleanup-{}",
            crate::generate_request_id()
        ));
        let scripts_dir = project_dir.join("scripts");
        std::fs::create_dir_all(&scripts_dir).expect("create scripts dir");
        let marker = project_dir.join("watchdog_calls.txt");

        #[cfg(target_os = "windows")]
        let fake_powershell = {
            let script_path = project_dir.join("fake-powershell.cmd");
            let script_body = format!(
                "@echo off\r\necho %*>> \"{}\"\r\nexit /b 0\r\n",
                marker.display()
            );
            std::fs::write(&script_path, script_body).expect("write fake powershell");
            script_path
        };

        #[cfg(target_os = "windows")]
        std::env::set_var("AUTOFLUID_POWERSHELL_EXE", &fake_powershell);

        #[cfg(not(target_os = "windows"))]
        let fake_powershell = {
            let script_path = project_dir.join("fake-powershell.sh");
            let script_body = format!(
                "#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"{}\"\n",
                marker.display()
            );
            std::fs::write(&script_path, script_body).expect("write fake powershell");
            use std::os::unix::fs::PermissionsExt;
            let mut perms = std::fs::metadata(&script_path)
                .expect("metadata")
                .permissions();
            perms.set_mode(0o755);
            std::fs::set_permissions(&script_path, perms).expect("chmod fake powershell");
            script_path
        };

        #[cfg(not(target_os = "windows"))]
        std::env::set_var("AUTOFLUID_POWERSHELL_EXE", &fake_powershell);

        let script_path = scripts_dir.join("start_workstation_reverse_tunnel.ps1");
        std::fs::write(&script_path, "").expect("write fake tunnel script");

        #[cfg(not(target_os = "windows"))]
        {
            use std::os::unix::fs::PermissionsExt;
            let mut perms = std::fs::metadata(&script_path)
                .expect("metadata")
                .permissions();
            perms.set_mode(0o755);
            std::fs::set_permissions(&script_path, perms).expect("chmod fake script");
        }

        let mut log_buffer = LogBuffer::new();
        let mut worker = WorkerManager::new();
        let result = worker.stop_workers_for_project(
            Some(project_dir.to_str().expect("utf8 temp path")),
            &mut log_buffer,
        );

        assert!(result);
        let calls = std::fs::read_to_string(&marker).expect("read marker");
        assert!(calls.contains("-TunnelKind Workstation -UninstallWatchdog"));
        assert!(calls.contains("-TunnelKind LocalWorker -UninstallWatchdog"));
        std::env::remove_var("AUTOFLUID_POWERSHELL_EXE");
        let _ = std::fs::remove_dir_all(project_dir);
    }

    #[test]
    fn workstation_tunnel_specs_use_three_workstation_defaults() {
        let _guard = crate::TEST_ENV_LOCK.lock().expect("env lock poisoned");
        let project_dir = std::env::temp_dir().join(format!(
            "autofluid-tui-worker-specs-{}",
            crate::generate_request_id()
        ));
        std::fs::create_dir_all(&project_dir).expect("create project dir");
        std::fs::write(
            project_dir.join("autofluid_config.toml"),
            three_workstation_toml(),
        )
        .expect("write config");
        std::env::remove_var("AUTOFLUID_SSH_REACHABLE_HOST");
        std::env::remove_var("AUTOFLUID_SSH_REACHABLE_PORT");
        std::env::remove_var("AUTOFLUID_WS_A_SSH_REACHABLE_PORT");
        std::env::remove_var("AUTOFLUID_WS_B_SSH_REACHABLE_PORT");
        std::env::remove_var("AUTOFLUID_WS_C_SSH_REACHABLE_PORT");

        let specs = workstation_tunnel_specs_for_path(&project_dir);

        assert_eq!(
            specs,
            vec![
                WorkstationTunnelSpec {
                    id: "WS-A".to_string(),
                    remote_host: "127.0.0.1".to_string(),
                    remote_port: 2222,
                    target_host: "172.17.135.240".to_string(),
                    target_port: 22,
                },
                WorkstationTunnelSpec {
                    id: "WS-B".to_string(),
                    remote_host: "127.0.0.1".to_string(),
                    remote_port: 2224,
                    target_host: "172.17.135.89".to_string(),
                    target_port: 22,
                },
                WorkstationTunnelSpec {
                    id: "WS-C".to_string(),
                    remote_host: "127.0.0.1".to_string(),
                    remote_port: 2225,
                    target_host: "172.17.135.115".to_string(),
                    target_port: 22,
                },
            ]
        );
        let _ = std::fs::remove_dir_all(project_dir);
    }

    #[test]
    fn worker_ssh_check_log_includes_effective_target() {
        let mut log_buffer = LogBuffer::new();
        let data = serde_json::json!({
            "ssh_checks": {
                "WS-A": "error: timed out"
            },
            "workstation_ssh_targets": {
                "WS-A": {
                    "host": "127.0.0.1",
                    "port": 2222,
                    "connectivity_mode": "reverse_tunnel"
                }
            }
        });

        log_worker_ssh_checks(&data, &mut log_buffer);

        assert!(log_buffer.info_messages.iter().any(|message| {
            message.contains("WS-A")
                && message.contains("127.0.0.1:2222")
                && message.contains("reverse_tunnel")
                && message.contains("timed out")
        }));
    }

    #[test]
    fn stop_child_process_has_no_blocking_wait_loop() {
        let source = include_str!("worker_mgr.rs");
        let fn_start = source
            .find("fn stop_child_process(")
            .expect("stop_child_process should exist");
        let rest = &source[fn_start..];
        let fn_end = rest
            .find("\n    fn pid_file_for")
            .expect("next helper should mark function end");
        let function_body = &rest[..fn_end];

        assert!(!function_body.contains("while "));
        assert!(!function_body.contains("std::thread::sleep"));
    }

    #[test]
    fn drop_preserves_tui_started_local_worker() {
        #[cfg(target_os = "windows")]
        let child = Command::new("cmd")
            .args(["/C", "ping -n 30 127.0.0.1 >nul"])
            .stdout(std::process::Stdio::null())
            .stderr(std::process::Stdio::null())
            .spawn()
            .expect("spawn live worker process");

        #[cfg(not(target_os = "windows"))]
        let child = Command::new("sleep")
            .arg("30")
            .stdout(std::process::Stdio::null())
            .stderr(std::process::Stdio::null())
            .spawn()
            .expect("spawn live worker process");

        let pid = child.id();
        let mut manager = WorkerManager::new();
        manager.worker_process = Some(child);
        manager.worker_started_detached = true;

        drop(manager);

        assert!(crate::utils::is_pid_alive(pid));
        let killed = crate::utils::kill_process_tree(pid);
        assert!(
            !crate::utils::is_pid_alive(pid)
                || (killed && crate::utils::wait_for_pid_dead(pid, Duration::from_secs(2)))
        );
    }

    #[test]
    fn start_workers_with_prepare_runs_both_tunnels_then_daemon_prepare_then_local_worker() {
        let _guard = crate::TEST_ENV_LOCK.lock().expect("env lock poisoned");
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
        std::fs::write(
            project_dir.join("autofluid_config.toml"),
            three_workstation_toml(),
        )
        .expect("write config");
        let marker = project_dir.join("worker-order.log");
        let powershell_exe = fake_argument_marker_exe(&project_dir, "fake_pwsh", &marker);
        let python_exe = fake_worker_env_marker_exe(&project_dir, "fake_python", &marker);
        std::env::set_var("AUTOFLUID_POWERSHELL_EXE", &powershell_exe);
        std::env::set_var("PYTHON", &python_exe);
        std::env::set_var("AUTOFLUID_WORKER_REACHABLE_HOST", "127.0.0.1");
        std::env::set_var("AUTOFLUID_WORKER_SSH_PORT", "2223");
        std::env::set_var("AUTOFLUID_WORKER_CONNECTIVITY_MODE", "reverse_tunnel");

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
        let order = wait_for_marker_lines(&marker, 6);
        let lines: Vec<&str> = order.lines().collect();
        let workstation_tunnel_lines: Vec<&&str> = lines
            .iter()
            .filter(|line| line.contains("-TunnelKind Workstation"))
            .collect();
        assert_eq!(workstation_tunnel_lines.len(), 3);
        assert!(workstation_tunnel_lines
            .iter()
            .any(|line| line.contains("AUTOFLUID_SSH_REACHABLE_PORT=2222")));
        assert!(workstation_tunnel_lines
            .iter()
            .any(|line| line.contains("AUTOFLUID_SSH_REACHABLE_PORT=2224")));
        assert!(workstation_tunnel_lines
            .iter()
            .any(|line| line.contains("AUTOFLUID_SSH_REACHABLE_PORT=2225")));
        let local_worker_tunnel_line = lines
            .iter()
            .find(|line| line.contains("-TunnelKind LocalWorker"))
            .expect("local worker tunnel command");
        assert!(workstation_tunnel_lines
            .iter()
            .all(|line| !line.contains("-NoWatchdog")));
        assert!(local_worker_tunnel_line.contains("-NoWatchdog"));
        let daemon_idx = lines
            .iter()
            .position(|line| *line == "daemon")
            .expect("daemon prepare marker");
        let worker_idx = lines
            .iter()
            .position(|line| *line == "worker 127.0.0.1 2223 reverse_tunnel")
            .expect("worker marker");
        assert!(daemon_idx < worker_idx);
        assert!(
            log_buffer
                .info_messages
                .iter()
                .any(|message| message.contains("本地 Worker 已启动")),
            "local worker should start even when stdio is silenced"
        );

        let _ = wm.stop_workers(&mut log_buffer);
        std::env::remove_var("AUTOFLUID_POWERSHELL_EXE");
        std::env::remove_var("PYTHON");
        std::env::remove_var("AUTOFLUID_WORKER_REACHABLE_HOST");
        std::env::remove_var("AUTOFLUID_WORKER_SSH_PORT");
        std::env::remove_var("AUTOFLUID_WORKER_CONNECTIVITY_MODE");
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

    fn fake_argument_marker_exe(
        project_dir: &std::path::Path,
        name: &str,
        marker: &std::path::Path,
    ) -> PathBuf {
        #[cfg(windows)]
        {
            let path = project_dir.join(format!("{name}.cmd"));
            std::fs::write(
                &path,
                format!(
                    "@echo off\r\necho %* AUTOFLUID_SSH_REACHABLE_PORT=%AUTOFLUID_SSH_REACHABLE_PORT% AUTOFLUID_WORKSTATION_TUNNEL_TARGET_HOST=%AUTOFLUID_WORKSTATION_TUNNEL_TARGET_HOST%>>\"{}\"\r\nexit /b 0\r\n",
                    marker.display()
                ),
            )
            .expect("write fake argument marker exe");
            path
        }

        #[cfg(not(windows))]
        {
            let path = project_dir.join(name);
            std::fs::write(
                &path,
                format!(
                    "#!/bin/sh\nprintf '%s AUTOFLUID_SSH_REACHABLE_PORT=%s AUTOFLUID_WORKSTATION_TUNNEL_TARGET_HOST=%s\\n' \"$*\" \"$AUTOFLUID_SSH_REACHABLE_PORT\" \"$AUTOFLUID_WORKSTATION_TUNNEL_TARGET_HOST\" >> '{}'\n",
                    marker.display()
                ),
            )
            .expect("write fake argument marker exe");
            make_executable(&path);
            path
        }
    }

    fn three_workstation_toml() -> &'static str {
        r#"
[[workstations]]
id = "WS-A"
host = "172.17.135.240"
port = 22
username = "ps"
working_dir = "/tmp/a"
scripts_dir = "/tmp/a/scripts"
ref_files_dir = "/tmp/a/ref"
scdoc_dir = "/tmp/a/scdoc"
msh_dir = "/tmp/a/msh"
result_dir = "/tmp/a/result"
flag_dir = "/tmp/a/flags"
conda_env = "base"
conda_exe = "conda"
mpi_bin_dir = "/tmp/mpi"

[[workstations]]
id = "WS-B"
host = "172.17.135.89"
port = 22
username = "ps"
working_dir = "/tmp/b"
scripts_dir = "/tmp/b/scripts"
ref_files_dir = "/tmp/b/ref"
scdoc_dir = "/tmp/b/scdoc"
msh_dir = "/tmp/b/msh"
result_dir = "/tmp/b/result"
flag_dir = "/tmp/b/flags"
conda_env = "base"
conda_exe = "conda"
mpi_bin_dir = "/tmp/mpi"

[[workstations]]
id = "WS-C"
host = "172.17.135.115"
port = 22
username = "bh"
working_dir = "/tmp/c"
scripts_dir = "/tmp/c/scripts"
ref_files_dir = "/tmp/c/ref"
scdoc_dir = "/tmp/c/scdoc"
msh_dir = "/tmp/c/msh"
result_dir = "/tmp/c/result"
flag_dir = "/tmp/c/flags"
conda_env = "base"
conda_exe = "conda"
mpi_bin_dir = "/tmp/mpi"
"#
    }

    fn fake_worker_env_marker_exe(
        project_dir: &std::path::Path,
        name: &str,
        marker: &std::path::Path,
    ) -> PathBuf {
        #[cfg(windows)]
        {
            let path = project_dir.join(format!("{name}.cmd"));
            std::fs::write(
                &path,
                format!(
                    "@echo off\r\necho worker %AUTOFLUID_WORKER_REACHABLE_HOST% %AUTOFLUID_WORKER_SSH_PORT% %AUTOFLUID_WORKER_CONNECTIVITY_MODE%>>\"{}\"\r\nexit /b 0\r\n",
                    marker.display()
                ),
            )
            .expect("write fake worker env marker exe");
            path
        }

        #[cfg(not(windows))]
        {
            let path = project_dir.join(name);
            std::fs::write(
                &path,
                format!(
                    "#!/bin/sh\nprintf 'worker %s %s %s\\n' \"$AUTOFLUID_WORKER_REACHABLE_HOST\" \"$AUTOFLUID_WORKER_SSH_PORT\" \"$AUTOFLUID_WORKER_CONNECTIVITY_MODE\" >> '{}'\n",
                    marker.display()
                ),
            )
            .expect("write fake worker env marker exe");
            make_executable(&path);
            path
        }
    }
}
