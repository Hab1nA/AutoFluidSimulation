use std::fs;
use std::fs::File;
use std::io::Write;
use std::path::PathBuf;
use std::process::{Child, Command, ExitStatus, Stdio};
use std::sync::mpsc;
use std::time::{Duration, Instant};

use crate::ipc::client::IpcClient;
use crate::state::{AppState, LogBuffer};
use crate::utils::{is_pid_alive, kill_process_tree, run_command_with_timeout};

/// 等待 daemon 进程自行退出的超时时间（秒）。
/// daemon 收到 full_quit 后执行 shutdown() 清理 SC 进程池等资源，完成后自然退出。
const DAEMON_SHUTDOWN_TIMEOUT_SECS: u64 = 60;
const IPC_RECONNECT_TIMEOUT_SECS: u64 = 60;
const IPC_RECONNECT_INTERVAL: Duration = Duration::from_millis(500);
const IPC_RECONNECT_ATTEMPT_TIMEOUT: Duration = Duration::from_millis(200);
const SERVER_DAEMON_START_TIMEOUT: Duration = Duration::from_secs(150);
const SERVER_DAEMON_STOP_TIMEOUT: Duration = Duration::from_secs(10);
const SERVER_DAEMON_DEFAULT_PROJECT_DIR: &str = "$HOME/AutoFluidSimulation";

pub struct DaemonManager {
    process: Option<Child>,
    pending_ipc_reconnect: Option<PendingIpcReconnect>,
    pending_server_start: Option<PendingServerStart>,
}

struct PendingIpcReconnect {
    deadline: Instant,
    next_attempt: Instant,
}

struct PendingServerStart {
    receiver: mpsc::Receiver<ServerStartEvent>,
    deadline: Instant,
}

enum ServerStartEvent {
    Stage(String),
    Done(Result<u32, String>),
}

impl DaemonManager {
    pub fn new() -> Self {
        Self {
            process: None,
            pending_ipc_reconnect: None,
            pending_server_start: None,
        }
    }

    pub fn launch(&mut self, project_dir: &str) -> Result<u32, String> {
        if is_server_mode() {
            return self.launch_server_daemon(project_dir);
        }

        let daemon_script = PathBuf::from(project_dir).join("start_daemon.py");
        let python = local_daemon_python(project_dir);
        log::info!(
            "准备启动后台引擎: python={}, script={}",
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
                log::info!("后台引擎进程已启动: pid={}", pid);
                self.process = Some(child);
                Ok(pid)
            }
            Err(e) => {
                log::error!("启动后台引擎失败: {}", e);
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
            Ok(()) => log::info!("已清理 PID 文件: {}", pid_file.display()),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
                log::debug!("PID 文件不存在，无需清理: {}", pid_file.display());
            }
            Err(e) => log::warn!("清理 PID 文件失败: {}, error={}", pid_file.display(), e),
        }
    }

    pub fn stop(&mut self, project_dir: &str) -> Result<(), String> {
        if is_server_mode() {
            self.process = None;
            log::info!("server 模式下通过 SSH 兜底停止远端 daemon");
            self.stop_server_daemon()?;
            Self::stop_server_ipc_tunnel(project_dir)?;
            return Ok(());
        }

        // full_quit IPC 成功时优先等待 daemon 自行退出；IPC 已断开但 PID 仍存活时，
        // 通过 PID 文件兜底终止，避免 TUI 退出后后台 daemon 残留。
        log::info!("等待后台引擎退出");
        let stopped = if let Some(mut child) = self.process.take() {
            Self::wait_for_exit(&mut child)
        } else if let Some(pid) = Self::read_pid_file(project_dir) {
            log::info!("通过 PID 文件停止后台引擎: pid={}", pid);
            Self::stop_pid_file_daemon(project_dir, pid)
        } else {
            log::info!("未发现需要等待的后台引擎进程");
            true
        };
        Self::cleanup_pid_file_after_wait(project_dir, stopped)
    }

    pub fn finish_after_successful_ipc_stop(&mut self, project_dir: &str) -> Result<(), String> {
        if is_server_mode() {
            self.process = None;
            log::info!("server 模式下已通过 IPC 请求停止远端 daemon，继续执行 SSH 停止兜底");
            self.stop_server_daemon()?;
            Self::stop_server_ipc_tunnel(project_dir)
        } else {
            self.stop(project_dir)
        }
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

    fn server_ipc_tunnel_pid_file(project_dir: &str) -> PathBuf {
        PathBuf::from(project_dir)
            .join("data")
            .join("server_ipc_tunnel.pid")
    }

    pub fn stop_server_ipc_tunnel(project_dir: &str) -> Result<(), String> {
        let pid_file = Self::server_ipc_tunnel_pid_file(project_dir);
        let raw_pid = match fs::read_to_string(&pid_file) {
            Ok(content) => content,
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
                log::debug!("服务器 IPC 隧道 PID 文件不存在，无需清理");
                return Ok(());
            }
            Err(e) => {
                return Err(format!(
                    "读取服务器 IPC 隧道 PID 文件失败: {}, error={}",
                    pid_file.display(),
                    e
                ));
            }
        };

        let pid = raw_pid.trim().parse::<u32>().map_err(|e| {
            format!(
                "服务器 IPC 隧道 PID 文件内容无效: {}, error={}",
                pid_file.display(),
                e
            )
        })?;

        if pid == 0 {
            let _ = fs::remove_file(&pid_file);
            return Ok(());
        }

        let killed = kill_process_tree(pid);
        let stopped = !is_pid_alive(pid) || (killed && Self::wait_for_pid_dead(pid, 2));
        if stopped {
            match fs::remove_file(&pid_file) {
                Ok(()) => {
                    log::info!("已清理服务器 IPC 隧道 PID 文件: {}", pid_file.display());
                }
                Err(e) if e.kind() == std::io::ErrorKind::NotFound => {}
                Err(e) => {
                    return Err(format!(
                        "清理服务器 IPC 隧道 PID 文件失败: {}, error={}",
                        pid_file.display(),
                        e
                    ));
                }
            }
            Ok(())
        } else {
            Err(format!("停止服务器 IPC 隧道超时或失败: pid={pid}"))
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
                    log::info!("后台引擎已退出: status={}", status);
                    return true;
                }
                Ok(None) => std::thread::sleep(Duration::from_millis(200)),
                Err(e) => {
                    log::warn!("检查后台引擎退出状态失败: {}", e);
                    return false;
                }
            }
        }
        // 超时仅记录，不强杀——daemon 可能仍在清理中
        log::warn!("等待后台引擎退出超时 ({DAEMON_SHUTDOWN_TIMEOUT_SECS}s)");
        false
    }

    /// 等待外部 daemon 进程自行退出（通过 PID 文件检测）。
    ///
    /// 注意：此方法在同步上下文中调用，不在 tokio 异步上下文中，
    /// 因此使用 `std::thread::sleep` 是安全的。
    fn wait_for_pid_exit(project_dir: &str) -> bool {
        let pid_file = Self::pid_file_path(project_dir);
        let Some(pid) = Self::read_pid_file(project_dir) else {
            return true;
        };
        let deadline = Instant::now() + Duration::from_secs(DAEMON_SHUTDOWN_TIMEOUT_SECS);
        while Instant::now() < deadline {
            if !pid_file.exists() {
                log::info!("后台引擎已完成退出清理");
                return true;
            }
            if !is_pid_alive(pid) {
                log::info!("后台引擎 PID 已退出: pid={pid}");
                return true;
            }
            std::thread::sleep(Duration::from_millis(200));
        }
        log::warn!("等待后台引擎 PID 退出超时 ({DAEMON_SHUTDOWN_TIMEOUT_SECS}s)");
        false
    }

    fn stop_pid_file_daemon(project_dir: &str, pid: u32) -> bool {
        if !is_pid_alive(pid) || !Self::pid_file_path(project_dir).exists() {
            return Self::wait_for_pid_exit(project_dir);
        }
        let killed = kill_process_tree(pid);
        if !killed {
            log::warn!("通过 PID 文件终止后台引擎失败: pid={pid}");
        }
        !is_pid_alive(pid) || Self::wait_for_pid_dead(pid, DAEMON_SHUTDOWN_TIMEOUT_SECS)
    }

    fn wait_for_pid_dead(pid: u32, timeout_secs: u64) -> bool {
        let deadline = Instant::now() + Duration::from_secs(timeout_secs);
        while Instant::now() < deadline {
            if !is_pid_alive(pid) {
                return true;
            }
            std::thread::sleep(Duration::from_millis(100));
        }
        !is_pid_alive(pid)
    }

    // ------------------------------------------------------------------
    // IPC 生命周期集成方法
    // ------------------------------------------------------------------

    pub(crate) fn begin_ipc_reconnect_wait(&mut self, state: &mut AppState) {
        let now = Instant::now();
        self.pending_ipc_reconnect = Some(PendingIpcReconnect {
            deadline: now + Duration::from_secs(IPC_RECONNECT_TIMEOUT_SECS),
            next_attempt: now,
        });
        state.connected = false;
        state.needs_redraw = true;
        log::info!(
            "后台引擎启动后进入分步 IPC 重连: timeout_ms={}",
            Duration::from_secs(IPC_RECONNECT_TIMEOUT_SECS).as_millis()
        );
    }

    fn begin_server_start(&mut self, project_dir: &str, log_buffer: &mut LogBuffer) {
        if self.pending_server_start.is_some() {
            log_buffer.push_info("⚠️ 服务器 daemon 正在启动，请勿重复操作".to_string());
            return;
        }

        let project_dir = project_dir.to_string();
        let (sender, receiver) = mpsc::channel();
        self.pending_server_start = Some(PendingServerStart {
            receiver,
            deadline: Instant::now() + SERVER_DAEMON_START_TIMEOUT,
        });
        log_buffer.push_info("⚠️ 正在启动服务器 daemon，界面保持响应...".to_string());
        std::thread::spawn(move || {
            let result = Self::launch_server_daemon_sync(&project_dir, Some(&sender));
            let _ = sender.send(ServerStartEvent::Done(result.map(|()| 0)));
        });
    }

    fn poll_server_start(&mut self, state: &mut AppState, log_buffer: &mut LogBuffer) -> bool {
        let Some(wait) = self.pending_server_start.as_ref() else {
            return false;
        };

        if Instant::now() >= wait.deadline {
            self.pending_server_start = None;
            log_buffer.push_info("❌ 启动后台引擎失败: 服务器启动任务超时".to_string());
            state.connected = false;
            state.needs_redraw = true;
            return true;
        }

        let mut changed = false;
        loop {
            let Some(wait) = self.pending_server_start.as_ref() else {
                return changed;
            };
            match wait.receiver.try_recv() {
                Ok(ServerStartEvent::Stage(message)) => {
                    log_buffer.push_info(format!("⚠️ {}", message));
                    state.needs_redraw = true;
                    changed = true;
                }
                Ok(ServerStartEvent::Done(Ok(0))) => {
                    self.pending_server_start = None;
                    log_buffer.push_info(
                        "⚠️ 已向服务器发送 daemon 启动命令，等待 IPC 就绪...".to_string(),
                    );
                    self.begin_ipc_reconnect_wait(state);
                    return true;
                }
                Ok(ServerStartEvent::Done(Ok(pid))) => {
                    self.pending_server_start = None;
                    log_buffer.push_info(format!(
                        "⚠️ 后台引擎正在启动 (PID: {})，等待 IPC 就绪...",
                        pid
                    ));
                    self.begin_ipc_reconnect_wait(state);
                    return true;
                }
                Ok(ServerStartEvent::Done(Err(e))) => {
                    self.pending_server_start = None;
                    log_buffer.push_info(format!("❌ 启动后台引擎失败: {}", e));
                    state.connected = false;
                    state.needs_redraw = true;
                    return true;
                }
                Err(mpsc::TryRecvError::Empty) => return changed,
                Err(mpsc::TryRecvError::Disconnected) => {
                    self.pending_server_start = None;
                    log_buffer.push_info("❌ 启动后台引擎失败: 后台启动任务异常退出".to_string());
                    state.connected = false;
                    state.needs_redraw = true;
                    return true;
                }
            }
        }
    }

    pub fn poll_ipc_reconnect(
        &mut self,
        rt: &tokio::runtime::Runtime,
        ipc: &mut IpcClient,
        state: &mut AppState,
        log_buffer: &mut LogBuffer,
    ) {
        self.poll_server_start(state, log_buffer);
        let Some(wait) = self.pending_ipc_reconnect.as_mut() else {
            return;
        };
        if ipc.is_connected() {
            state.connected = true;
            self.pending_ipc_reconnect = None;
            log::info!("IPC 已处于连接状态");
            return;
        }
        let now = Instant::now();
        if now >= wait.deadline {
            self.pending_ipc_reconnect = None;
            state.connected = false;
            state.needs_redraw = true;
            log::warn!("后台引擎已启动，但 IPC 暂未就绪");
            log_buffer.push_info("⚠️ 后台引擎已启动，但 IPC 暂未就绪".to_string());
            return;
        }
        if now < wait.next_attempt {
            return;
        }
        wait.next_attempt = now + IPC_RECONNECT_INTERVAL;
        if rt
            .block_on(ipc.connect_with_timeout(IPC_RECONNECT_ATTEMPT_TIMEOUT))
            .is_ok()
        {
            self.pending_ipc_reconnect = None;
            state.connected = true;
            state.last_log_id = 0;
            state.needs_redraw = true;
            log_buffer.clear_detail();
            log_buffer.push_info("✅ 已连接到后台引擎".to_string());
            log::info!("后台引擎 IPC 已就绪");
        }
    }

    #[cfg(test)]
    pub(crate) fn has_pending_ipc_reconnect(&self) -> bool {
        self.pending_ipc_reconnect.is_some()
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
        if server_mode {
            if ipc.is_connected() {
                rt.block_on(ipc.disconnect());
            }
            match self.stop(project_dir) {
                Ok(()) => {
                    state.mark_daemon_stopped();
                    log_buffer.push_info("✅ 已向服务器发送 daemon 停止命令".to_string());
                    log_buffer.push_info("✅ 服务器后台引擎已停止或正在停止".to_string());
                    return true;
                }
                Err(e) => {
                    log_buffer.push_info(format!("❌ 停止服务器 daemon 失败: {}", e));
                    state.connected = ipc.is_connected();
                    return false;
                }
            }
        }
        if ipc.is_connected() {
            log::info!("发送后台引擎停止请求");
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
                state.mark_daemon_stopped();
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
            log::info!("server 模式下重启服务器 daemon");
            if !self.stop_with_ipc(ipc, rt, state, log_buffer, project_dir) {
                return;
            }
            std::thread::sleep(Duration::from_secs(1));
            match self.launch(project_dir) {
                Ok(_) => {
                    log_buffer.push_info(
                        "⚠️ 已向服务器发送 daemon 启动命令，等待 IPC 就绪...".to_string(),
                    );
                    self.begin_ipc_reconnect_wait(state);
                }
                Err(e) => {
                    log_buffer.push_info(format!("❌ 重启服务器 daemon 失败: {}", e));
                }
            }
            return;
        }
        log::info!("开始重启后台引擎");
        self.stop_with_ipc(ipc, rt, state, log_buffer, project_dir);
        match self.launch(project_dir) {
            Ok(pid) => {
                log_buffer.push_info(format!(
                    "⚠️ 后台引擎正在重启 (PID: {})，等待 IPC 就绪...",
                    pid
                ));
                self.begin_ipc_reconnect_wait(state);
            }
            Err(e) => {
                log_buffer.push_info(format!("❌ 重启后台引擎失败: {}", e));
            }
        }
    }

    pub fn start_with_ipc(
        &mut self,
        ipc: &mut IpcClient,
        _rt: &tokio::runtime::Runtime,
        state: &mut AppState,
        log_buffer: &mut LogBuffer,
        project_dir: &str,
    ) {
        if ipc.is_connected() {
            log_buffer.push_info("⚠️ 已连接到后台引擎，无需重复启动".to_string());
            state.connected = true;
            return;
        }

        if is_server_mode() {
            self.begin_server_start(project_dir, log_buffer);
            state.connected = false;
            state.needs_redraw = true;
            return;
        }

        match self.launch(project_dir) {
            Ok(0) => {
                log_buffer
                    .push_info("⚠️ 已向服务器发送 daemon 启动命令，等待 IPC 就绪...".to_string());
                self.begin_ipc_reconnect_wait(state);
            }
            Ok(pid) => {
                log_buffer.push_info(format!(
                    "⚠️ 后台引擎正在启动 (PID: {})，等待 IPC 就绪...",
                    pid
                ));
                self.begin_ipc_reconnect_wait(state);
            }
            Err(e) => {
                log_buffer.push_info(format!("❌ 启动后台引擎失败: {}", e));
            }
        }
    }

    fn launch_server_daemon(&mut self, project_dir: &str) -> Result<u32, String> {
        Self::launch_server_daemon_sync(project_dir, None)?;
        Ok(0)
    }

    fn launch_server_daemon_sync(
        project_dir: &str,
        progress: Option<&mpsc::Sender<ServerStartEvent>>,
    ) -> Result<(), String> {
        send_server_start_stage(progress, "正在检查服务器 IPC 隧道...");
        Self::run_server_ipc_tunnel_script(project_dir)?;
        log::info!("服务器 IPC 隧道阶段完成，准备启动远端 daemon");
        Self::sync_server_config(project_dir, progress)?;
        send_server_start_stage(progress, "服务器 IPC 隧道已就绪，正在启动远端 daemon...");
        Self::run_server_daemon_command(ServerDaemonAction::Start, progress)
    }

    fn sync_server_config(
        project_dir: &str,
        progress: Option<&mpsc::Sender<ServerStartEvent>>,
    ) -> Result<(), String> {
        let local_config = PathBuf::from(project_dir).join("autofluid_config.toml");
        if !local_config.exists() {
            log::warn!(
                "本地配置文件不存在，跳过服务器配置同步: {}",
                local_config.display()
            );
            return Ok(());
        }

        let config_bytes = fs::read(&local_config).map_err(|e| {
            format!(
                "读取本地配置文件失败: {}, error={}",
                local_config.display(),
                e
            )
        })?;
        let command = ServerDaemonSshCommand::from_env(ServerDaemonAction::Start)?;
        let remote_project_dir = default_server_project_dir();
        let remote_command = format!(
            "tmp=$(mktemp {remote_project_dir}/.autofluid_config.toml.XXXXXX) && \
             cat > \"$tmp\" && mv \"$tmp\" {remote_project_dir}/autofluid_config.toml"
        );

        send_server_start_stage(progress, "正在同步本地配置到服务器...");
        run_server_config_sync_command(
            &command.ssh_exe,
            &command.target,
            &remote_command,
            &config_bytes,
        )?;
        log::info!(
            "已同步本地配置到服务器: {} -> {}/autofluid_config.toml",
            local_config.display(),
            remote_project_dir
        );
        send_server_start_stage(progress, "本地配置已同步到服务器");
        Ok(())
    }

    fn stop_server_daemon(&mut self) -> Result<(), String> {
        Self::run_server_daemon_command(ServerDaemonAction::Stop, None)
    }

    fn run_server_daemon_command(
        action: ServerDaemonAction,
        progress: Option<&mpsc::Sender<ServerStartEvent>>,
    ) -> Result<(), String> {
        let command = ServerDaemonSshCommand::from_env(action)?;
        let args = command.args();
        log::info!(
            "通过 SSH 控制服务器 daemon: action={}, target={}",
            action.label(),
            command.target
        );
        if matches!(action, ServerDaemonAction::Start) {
            send_server_start_stage(
                progress,
                format!("远端 daemon 启动命令已发送: target={}", command.target),
            );
        }
        let mut cmd = Command::new(&command.ssh_exe);
        cmd.args(&args);
        let timeout = match action {
            ServerDaemonAction::Start => SERVER_DAEMON_START_TIMEOUT,
            ServerDaemonAction::Stop => SERVER_DAEMON_STOP_TIMEOUT,
        };
        let output = run_command_with_timeout(&mut cmd, timeout)?;
        let stdout = String::from_utf8_lossy(&output.stdout).trim().to_string();
        let stderr = String::from_utf8_lossy(&output.stderr).trim().to_string();
        if !stdout.is_empty() {
            log::info!(
                "服务器 daemon SSH 输出: action={}, stdout={}",
                action.label(),
                stdout
            );
        }
        if !stderr.is_empty() {
            log::warn!(
                "服务器 daemon SSH 错误输出: action={}, stderr={}",
                action.label(),
                stderr
            );
        }

        if output.status.success() {
            if matches!(action, ServerDaemonAction::Start) {
                let ready_message = if stdout.is_empty() {
                    "远端 daemon 启动命令已完成，等待本地 IPC 重连...".to_string()
                } else {
                    format!("远端 daemon 启动完成: {}", stdout)
                };
                send_server_start_stage(progress, ready_message);
            }
            return Ok(());
        }

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
            "-OwnerPid".to_string(),
            std::process::id().to_string(),
        ];

        let mut last_error = String::new();
        for powershell in powershell_candidates() {
            let mut cmd = Command::new(&powershell);
            cmd.args(&args).current_dir(project_dir);
            match run_tunnel_script_command_with_timeout(&mut cmd, Duration::from_secs(60)) {
                Ok((status, stdout, stderr)) if status.success() => {
                    if !stdout.is_empty() {
                        log::info!(
                            "服务器 IPC 隧道脚本输出: powershell={}, stdout={}",
                            powershell,
                            stdout
                        );
                    }
                    if !stderr.is_empty() {
                        log::warn!(
                            "服务器 IPC 隧道脚本错误输出: powershell={}, stderr={}",
                            powershell,
                            stderr
                        );
                    }
                    log::info!(
                        "服务器 IPC 隧道脚本执行成功: powershell={}, script={}",
                        powershell,
                        script.display()
                    );
                    return Ok(());
                }
                Ok((status, stdout, stderr)) => {
                    let detail = if stderr.is_empty() { stdout } else { stderr };
                    last_error = if detail.is_empty() {
                        format!("{} 退出状态: {}", powershell, status)
                    } else {
                        format!("{} 退出状态: {}, {}", powershell, status, detail)
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

fn send_server_start_stage<S>(progress: Option<&mpsc::Sender<ServerStartEvent>>, message: S)
where
    S: Into<String>,
{
    if let Some(sender) = progress {
        let _ = sender.send(ServerStartEvent::Stage(message.into()));
    }
}

fn run_tunnel_script_command_with_timeout(
    command: &mut Command,
    timeout: Duration,
) -> Result<(ExitStatus, String, String), String> {
    let request_id = crate::generate_request_id();
    let stdout_path = std::env::temp_dir().join(format!("autofluid-tui-tunnel-{request_id}.out"));
    let stderr_path = std::env::temp_dir().join(format!("autofluid-tui-tunnel-{request_id}.err"));
    let stdout_file = File::create(&stdout_path)
        .map_err(|e| format!("创建隧道脚本 stdout 临时文件失败: {}", e))?;
    let stderr_file = File::create(&stderr_path)
        .map_err(|e| format!("创建隧道脚本 stderr 临时文件失败: {}", e))?;
    command
        .stdout(Stdio::from(stdout_file))
        .stderr(Stdio::from(stderr_file));
    let mut child = command
        .spawn()
        .map_err(|e| format!("启动命令失败: {}", e))?;
    let deadline = Instant::now() + timeout;
    let status = loop {
        match child.try_wait() {
            Ok(Some(status)) => break status,
            Ok(None) if Instant::now() >= deadline => {
                let _ = child.kill();
                let _ = child.wait();
                let stdout = read_and_remove_temp_output(&stdout_path);
                let stderr = read_and_remove_temp_output(&stderr_path);
                let detail = if stderr.is_empty() { stdout } else { stderr };
                return if detail.is_empty() {
                    Err(format!("命令执行超时 ({}s)", timeout.as_secs()))
                } else {
                    Err(format!("命令执行超时 ({}s): {}", timeout.as_secs(), detail))
                };
            }
            Ok(None) => std::thread::sleep(Duration::from_millis(100)),
            Err(e) => {
                let _ = child.kill();
                let _ = child.wait();
                let _ = fs::remove_file(&stdout_path);
                let _ = fs::remove_file(&stderr_path);
                return Err(format!("检查命令状态失败: {}", e));
            }
        }
    };

    let stdout = read_and_remove_temp_output(&stdout_path);
    let stderr = read_and_remove_temp_output(&stderr_path);
    Ok((status, stdout, stderr))
}

fn run_server_config_sync_command(
    ssh_exe: &str,
    target: &str,
    remote_command: &str,
    config_bytes: &[u8],
) -> Result<(), String> {
    let mut command = Command::new(ssh_exe);
    command
        .args([
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=10",
            target,
            remote_command,
        ])
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());

    let mut child = command
        .spawn()
        .map_err(|e| format!("启动配置同步命令失败: {}", e))?;

    if let Some(mut stdin) = child.stdin.take() {
        if let Err(e) = stdin.write_all(config_bytes) {
            let _ = child.kill();
            let _ = child.wait_with_output();
            return Err(format!("写入服务器配置同步数据失败: {}", e));
        }
    }

    let deadline = Instant::now() + Duration::from_secs(30);
    loop {
        match child.try_wait() {
            Ok(Some(_)) => {
                let output = child
                    .wait_with_output()
                    .map_err(|e| format!("读取配置同步命令输出失败: {}", e))?;
                if output.status.success() {
                    return Ok(());
                }
                let stdout = String::from_utf8_lossy(&output.stdout).trim().to_string();
                let stderr = String::from_utf8_lossy(&output.stderr).trim().to_string();
                let detail = if stderr.is_empty() { stdout } else { stderr };
                return if detail.is_empty() {
                    Err(format!("配置同步命令退出状态: {}", output.status))
                } else {
                    Err(format!(
                        "配置同步命令退出状态: {}, {}",
                        output.status, detail
                    ))
                };
            }
            Ok(None) if Instant::now() >= deadline => {
                let _ = child.kill();
                let _ = child.wait_with_output();
                return Err("配置同步命令执行超时 (30s)".to_string());
            }
            Ok(None) => std::thread::sleep(Duration::from_millis(100)),
            Err(e) => {
                let _ = child.kill();
                let _ = child.wait_with_output();
                return Err(format!("检查配置同步命令状态失败: {}", e));
            }
        }
    }
}

fn read_and_remove_temp_output(path: &std::path::Path) -> String {
    let content = fs::read_to_string(path)
        .unwrap_or_default()
        .trim()
        .to_string();
    let _ = fs::remove_file(path);
    content
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
            None => default_server_stop_command(),
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
    let project_dir = default_server_project_dir();
    let service_name = daemon_service_name();
    let service = shell_single_quote(&service_name);
    let service_unit = shell_single_quote(&daemon_service_unit_name(&service_name));
    let remote_ipc_port = first_env_non_empty(&[
        "AUTOFLUID_SERVER_DAEMON_IPC_PORT",
        "AUTOFLUID_SERVER_TUNNEL_REMOTE_PORT",
    ])
    .unwrap_or_else(|| "9527".to_string());
    format!(
        "cd {project_dir} && mkdir -p logs/server/services/daemon-bootstrap && \
         if command -v systemctl >/dev/null 2>&1 && systemctl list-unit-files {service_unit} >/dev/null 2>&1; then \
             systemctl start {service}; \
             systemctl is-active --quiet {service}; \
             daemon_pid=$(systemctl show -p MainPID --value {service} 2>/dev/null || echo 0); \
         else \
             {{ env AUTOFLUID_SERVER_MODE=server nohup .venv/bin/python start_daemon.py > logs/server/services/daemon-bootstrap/autofluid-daemon.out 2>&1 < /dev/null & \
             daemon_pid=$!; }}; \
         fi; \
         ready_count=0; \
         for i in $(seq 1 60); do \
             if .venv/bin/python -c \"import json,socket; s=socket.create_connection(('127.0.0.1', {remote_ipc_port}), 1); s.settimeout(2); s.sendall((json.dumps(dict(command='get_engine_status', params=dict(), request_id='daemon-start-probe'))+'\\n').encode()); data=s.recv(4096); s.close(); resp=json.loads(data.decode().strip()); raise SystemExit(0 if resp.get('status') == 'ok' else 1)\" >/dev/null 2>&1; then \
                 ready_count=$((ready_count + 1)); \
                 if [ \"$ready_count\" -ge 3 ]; then \
                     echo \"AutoFluid daemon IPC ready (pid=$daemon_pid)\"; \
                     exit 0; \
                 fi; \
             else \
                 ready_count=0; \
             fi; \
             if ! kill -0 \"$daemon_pid\" 2>/dev/null && [ \"$ready_count\" -eq 0 ]; then \
                 echo 'AutoFluid daemon exited before IPC became ready' >&2; \
                  tail -n 80 logs/server/services/daemon-bootstrap/autofluid-daemon.out >&2 2>/dev/null || true; \
                 exit 1; \
             fi; \
             sleep 1; \
         done; \
         echo 'AutoFluid daemon IPC readiness timeout' >&2; \
         tail -n 80 logs/server/services/daemon-bootstrap/autofluid-daemon.out >&2 2>/dev/null || true; \
         exit 1"
    )
}

fn default_server_stop_command() -> String {
    let project_dir = default_server_project_dir();
    let service_name = daemon_service_name();
    let service = shell_single_quote(&service_name);
    let service_unit = shell_single_quote(&daemon_service_unit_name(&service_name));
    format!(
        "cd {project_dir} && \
         if command -v systemctl >/dev/null 2>&1 && systemctl list-unit-files {service_unit} >/dev/null 2>&1; then \
             systemctl stop {service}; \
         else \
             .venv/bin/python main.py --stop; \
         fi"
    )
}

fn daemon_service_name() -> String {
    env_non_empty("AUTOFLUID_DAEMON_SERVICE").unwrap_or_else(|| "autofluid-daemon".to_string())
}

fn daemon_service_unit_name(service_name: &str) -> String {
    if service_name.ends_with(".service") {
        service_name.to_string()
    } else {
        format!("{service_name}.service")
    }
}

fn default_server_project_dir() -> String {
    env_non_empty("AUTOFLUID_SERVER_DAEMON_PROJECT_DIR")
        .or_else(|| env_non_empty("AUTOFLUID_SERVER_PROJECT_DIR"))
        .map(|path| shell_single_quote(&path))
        .unwrap_or_else(|| SERVER_DAEMON_DEFAULT_PROJECT_DIR.to_string())
}

fn shell_single_quote(value: &str) -> String {
    format!("'{}'", value.replace('\'', "'\\''"))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::ipc::client::IpcClient;
    use std::io::{Read, Write};
    use std::net::TcpListener;

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
    fn wait_for_pid_exit_treats_dead_pid_as_stopped() {
        let project_dir = unique_temp_project_dir();
        let pid_file = project_dir.join("data").join("daemon.pid");
        fs::write(&pid_file, "999999").expect("write stale pid file");

        assert!(DaemonManager::wait_for_pid_exit(
            project_dir.to_str().expect("utf8 temp path")
        ));
        assert!(pid_file.exists(), "caller owns PID-file cleanup after wait");

        let _ = fs::remove_dir_all(project_dir);
    }

    #[test]
    fn local_stop_without_ipc_terminates_live_pid_file_daemon() {
        let _guard = crate::TEST_ENV_LOCK.lock().expect("env lock poisoned");
        std::env::remove_var("AUTOFLUID_SERVER_MODE");
        let project_dir = unique_temp_project_dir();
        let pid_file = project_dir.join("data").join("daemon.pid");
        let mut child = spawn_live_pid_process(&project_dir);
        fs::write(&pid_file, child.id().to_string()).expect("write daemon pid");

        let mut daemon = DaemonManager::new();
        daemon
            .stop(project_dir.to_str().expect("utf8 temp path"))
            .expect("local stop should terminate PID-file daemon");

        let exited = wait_for_child_exit(&mut child, 2);
        if !exited {
            let _ = child.kill();
            let _ = child.wait();
        }
        assert!(
            exited,
            "local stop fallback should terminate live daemon PID"
        );
        assert!(!pid_file.exists(), "stopped daemon PID file removed");

        let _ = fs::remove_dir_all(project_dir);
    }
    #[test]
    fn local_daemon_python_prefers_project_venv() {
        let _guard = crate::TEST_ENV_LOCK.lock().expect("env lock poisoned");
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
    fn start_with_ipc_returns_without_waiting_for_reconnect_timeout() {
        let _guard = crate::TEST_ENV_LOCK.lock().expect("env lock poisoned");
        std::env::remove_var("AUTOFLUID_SERVER_MODE");
        let project_dir = unique_temp_project_dir();
        fs::write(project_dir.join("start_daemon.py"), "").expect("write daemon script");
        let python_exe = fake_success_exe(&project_dir, "fake_python");
        std::env::set_var("PYTHON", &python_exe);
        let rt = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .expect("runtime");
        let mut ipc = IpcClient::new(Some("127.0.0.1"), Some(9));
        let mut state = AppState::new();
        let mut log_buffer = LogBuffer::new();
        let mut daemon = DaemonManager::new();

        let started = Instant::now();
        daemon.start_with_ipc(
            &mut ipc,
            &rt,
            &mut state,
            &mut log_buffer,
            project_dir.to_str().expect("utf8 temp path"),
        );

        assert!(
            started.elapsed() < Duration::from_secs(2),
            "daemon start must not freeze the TUI while waiting for IPC reconnect"
        );
        assert!(log_buffer
            .info_messages
            .iter()
            .any(|message| message.contains("等待 IPC 就绪")));

        std::env::remove_var("PYTHON");
        let _ = daemon.stop(project_dir.to_str().expect("utf8 temp path"));
        let _ = fs::remove_dir_all(project_dir);
    }

    #[test]
    fn server_start_with_ipc_returns_while_remote_start_runs() {
        let _guard = crate::TEST_ENV_LOCK.lock().expect("env lock poisoned");
        std::env::set_var("AUTOFLUID_SERVER_MODE", "server");
        let project_dir = unique_temp_project_dir();
        write_fake_server_ipc_tunnel_script(&project_dir);
        let powershell_exe = fake_success_exe(&project_dir, "fake_pwsh");
        let ssh_exe = fake_sleep_exe(&project_dir, "fake_ssh", 3);
        std::env::set_var("AUTOFLUID_POWERSHELL_EXE", &powershell_exe);
        std::env::set_var("AUTOFLUID_SSH_EXE", &ssh_exe);
        std::env::set_var("AUTOFLUID_SERVER_DAEMON_SSH_TARGET", "ocar-prod");
        std::env::set_var("AUTOFLUID_SERVER_DAEMON_START_CMD", "exit 0");
        let rt = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .expect("runtime");
        let mut ipc = IpcClient::new(Some("127.0.0.1"), Some(9));
        let mut state = AppState::new();
        let mut log_buffer = LogBuffer::new();
        let mut daemon = DaemonManager::new();

        let started = Instant::now();
        daemon.start_with_ipc(
            &mut ipc,
            &rt,
            &mut state,
            &mut log_buffer,
            project_dir.to_str().expect("utf8 temp path"),
        );

        assert!(
            started.elapsed() < Duration::from_secs(1),
            "server daemon start must not block the TUI on SSH startup"
        );
        assert!(log_buffer
            .info_messages
            .iter()
            .any(|message| message.contains("正在启动服务器 daemon")));
        let deadline = Instant::now() + Duration::from_secs(2);
        while Instant::now() < deadline
            && !log_buffer
                .info_messages
                .iter()
                .any(|message| message.contains("正在检查服务器 IPC 隧道"))
        {
            daemon.poll_server_start(&mut state, &mut log_buffer);
            std::thread::sleep(Duration::from_millis(20));
        }
        assert!(log_buffer
            .info_messages
            .iter()
            .any(|message| message.contains("正在检查服务器 IPC 隧道")));

        let deadline = Instant::now() + Duration::from_secs(5);
        while Instant::now() < deadline && daemon.pending_server_start.is_some() {
            daemon.poll_server_start(&mut state, &mut log_buffer);
            std::thread::sleep(Duration::from_millis(20));
        }
        assert!(
            daemon.pending_server_start.is_none(),
            "server start background task must finish before test releases env"
        );

        std::env::remove_var("AUTOFLUID_SERVER_DAEMON_START_CMD");
        std::env::remove_var("AUTOFLUID_SERVER_DAEMON_SSH_TARGET");
        std::env::remove_var("AUTOFLUID_SSH_EXE");
        std::env::remove_var("AUTOFLUID_POWERSHELL_EXE");
        std::env::remove_var("AUTOFLUID_SERVER_MODE");
        let _ = fs::remove_dir_all(project_dir);
    }

    #[test]
    fn tunnel_script_runner_returns_when_child_keeps_output_handles_open() {
        let project_dir = unique_temp_project_dir();
        let helper = fake_background_handle_holder_exe(&project_dir);
        let mut cmd = Command::new(&helper);

        let started = Instant::now();
        let result = run_tunnel_script_command_with_timeout(&mut cmd, Duration::from_secs(3));

        assert!(result.is_ok());
        assert!(
            started.elapsed() < Duration::from_secs(2),
            "tunnel script runner must not wait for a background child that inherited output handles"
        );
        let _ = fs::remove_dir_all(project_dir);
    }

    #[test]
    fn server_daemon_start_command_uses_configured_ssh_target() {
        let _guard = crate::TEST_ENV_LOCK.lock().expect("env lock poisoned");
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
    fn default_server_start_command_waits_for_remote_daemon_readiness() {
        let _guard = crate::TEST_ENV_LOCK.lock().expect("env lock poisoned");
        std::env::remove_var("AUTOFLUID_SERVER_DAEMON_PROJECT_DIR");
        std::env::remove_var("AUTOFLUID_SERVER_PROJECT_DIR");
        std::env::remove_var("AUTOFLUID_SERVER_DAEMON_IPC_PORT");
        std::env::remove_var("AUTOFLUID_SERVER_TUNNEL_REMOTE_PORT");
        std::env::remove_var("AUTOFLUID_IPC_PORT");
        std::env::remove_var("AUTOFLUID_DAEMON_SERVICE");

        let command = default_server_start_command();

        assert!(command.contains("systemctl start 'autofluid-daemon'"));
        assert!(command.contains("is-active --quiet 'autofluid-daemon'"));
        assert!(command.contains("nohup .venv/bin/python start_daemon.py"));
        assert!(command.contains("daemon_pid=$!"));
        assert!(command.contains("kill -0 \"$daemon_pid\""));
        assert!(command.contains("socket.create_connection(('127.0.0.1', 9527)"));
        assert!(command.contains("command='get_engine_status'"));
        assert!(command.contains("ready_count=$((ready_count + 1))"));
        assert!(command
            .contains("tail -n 80 logs/server/services/daemon-bootstrap/autofluid-daemon.out"));
        assert!(!command.contains("setsid -f"));
        assert!(!command.contains("&& env AUTOFLUID_SERVER_MODE=server nohup"));
    }

    #[test]
    fn default_server_start_command_uses_configured_remote_ipc_port_for_readiness() {
        let _guard = crate::TEST_ENV_LOCK.lock().expect("env lock poisoned");
        std::env::remove_var("AUTOFLUID_SERVER_DAEMON_PROJECT_DIR");
        std::env::remove_var("AUTOFLUID_SERVER_PROJECT_DIR");
        std::env::remove_var("AUTOFLUID_SERVER_DAEMON_IPC_PORT");
        std::env::set_var("AUTOFLUID_SERVER_TUNNEL_REMOTE_PORT", "19527");
        std::env::set_var("AUTOFLUID_IPC_PORT", "18000");
        std::env::remove_var("AUTOFLUID_DAEMON_SERVICE");

        let command = default_server_start_command();

        assert!(command.contains("socket.create_connection(('127.0.0.1', 19527)"));
        assert!(!command.contains("socket.create_connection(('127.0.0.1', 18000)"));

        std::env::remove_var("AUTOFLUID_SERVER_TUNNEL_REMOTE_PORT");
        std::env::remove_var("AUTOFLUID_IPC_PORT");
    }

    #[test]
    fn default_server_start_command_uses_custom_systemd_service_name() {
        let _guard = crate::TEST_ENV_LOCK.lock().expect("env lock poisoned");
        std::env::set_var("AUTOFLUID_DAEMON_SERVICE", "autofluid-daemon-prod");

        let command = default_server_start_command();

        assert!(command.contains("systemctl start 'autofluid-daemon-prod'"));
        assert!(command.contains("autofluid-daemon-prod.service"));
        assert!(command.contains("nohup .venv/bin/python start_daemon.py"));

        std::env::remove_var("AUTOFLUID_DAEMON_SERVICE");
    }

    #[test]
    fn default_server_stop_command_prefers_systemd_and_keeps_process_fallback() {
        let _guard = crate::TEST_ENV_LOCK.lock().expect("env lock poisoned");
        std::env::remove_var("AUTOFLUID_SERVER_DAEMON_PROJECT_DIR");
        std::env::remove_var("AUTOFLUID_SERVER_PROJECT_DIR");
        std::env::remove_var("AUTOFLUID_DAEMON_SERVICE");

        let command = default_server_stop_command();

        assert!(command.contains("systemctl stop 'autofluid-daemon'"));
        assert!(command.contains("autofluid-daemon.service"));
        assert!(command.contains(".venv/bin/python main.py --stop"));
    }

    #[test]
    fn default_server_stop_command_uses_custom_systemd_service_name() {
        let _guard = crate::TEST_ENV_LOCK.lock().expect("env lock poisoned");
        std::env::set_var("AUTOFLUID_DAEMON_SERVICE", "autofluid-daemon-prod");

        let command = default_server_stop_command();

        assert!(command.contains("systemctl stop 'autofluid-daemon-prod'"));
        assert!(command.contains("autofluid-daemon-prod.service"));
        assert!(command.contains(".venv/bin/python main.py --stop"));

        std::env::remove_var("AUTOFLUID_DAEMON_SERVICE");
    }

    #[test]
    fn server_daemon_command_timeout_covers_remote_readiness_probe() {
        assert!(
            SERVER_DAEMON_START_TIMEOUT >= Duration::from_secs(60),
            "start SSH timeout must not expire before the remote readiness probe"
        );
    }

    #[test]
    fn server_daemon_stop_timeout_is_shorter_than_start_readiness_timeout() {
        assert!(
            SERVER_DAEMON_STOP_TIMEOUT < SERVER_DAEMON_START_TIMEOUT,
            "server daemon stop should not reuse the long start readiness timeout"
        );
    }

    #[test]
    fn launch_uses_server_daemon_start_command_in_server_mode() {
        let _guard = crate::TEST_ENV_LOCK.lock().expect("env lock poisoned");
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
    fn server_ipc_tunnel_script_receives_client_owner_pid() {
        let _guard = crate::TEST_ENV_LOCK.lock().expect("env lock poisoned");
        let project_dir = unique_temp_project_dir();
        write_fake_server_ipc_tunnel_script(&project_dir);
        let marker = project_dir.join("server-ipc-args.log");
        let powershell_exe = fake_argument_marker_exe(&project_dir, "fake_pwsh_args", &marker);
        std::env::set_var("AUTOFLUID_POWERSHELL_EXE", &powershell_exe);

        DaemonManager::run_server_ipc_tunnel_script(project_dir.to_str().expect("utf8 temp path"))
            .expect("server ipc tunnel script should run");

        let args = fs::read_to_string(&marker).expect("read marker");
        assert!(args.contains("-OwnerPid"));
        assert!(args.contains(&std::process::id().to_string()));

        std::env::remove_var("AUTOFLUID_POWERSHELL_EXE");
        let _ = fs::remove_dir_all(project_dir);
    }
    #[test]
    fn launch_starts_server_ipc_tunnel_before_server_daemon() {
        let _guard = crate::TEST_ENV_LOCK.lock().expect("env lock poisoned");
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
    fn launch_syncs_local_config_before_server_daemon_start() {
        let _guard = crate::TEST_ENV_LOCK.lock().expect("env lock poisoned");
        std::env::set_var("AUTOFLUID_SERVER_MODE", "server");
        let project_dir = unique_temp_project_dir();
        write_fake_server_ipc_tunnel_script(&project_dir);
        let config_text = "[solver]\nsolver_iteration_count = 1000\n";
        fs::write(project_dir.join("autofluid_config.toml"), config_text)
            .expect("write local config");
        let marker = project_dir.join("launch-config-order.log");
        let captured_config = project_dir.join("captured-autofluid-config.toml");
        let powershell_exe = fake_marker_exe(&project_dir, "fake_pwsh", &marker, "tunnel");
        let ssh_exe = fake_config_sync_ssh_exe(&project_dir, &marker, &captured_config);
        std::env::set_var("AUTOFLUID_POWERSHELL_EXE", &powershell_exe);
        std::env::set_var("AUTOFLUID_SSH_EXE", &ssh_exe);
        std::env::set_var("AUTOFLUID_SERVER_DAEMON_SSH_TARGET", "ocar-prod");
        std::env::set_var("AUTOFLUID_SERVER_DAEMON_START_CMD", "exit 0");
        let mut daemon = DaemonManager::new();

        let result = daemon.launch(project_dir.to_str().expect("utf8 temp path"));

        assert_eq!(result, Ok(0));
        let order = fs::read_to_string(&marker).expect("read marker file");
        let lines: Vec<&str> = order.lines().collect();
        assert_eq!(lines, vec!["tunnel", "sync", "daemon"]);
        assert_eq!(
            normalize_line_endings(
                &fs::read_to_string(&captured_config).expect("read captured config")
            ),
            normalize_line_endings(config_text)
        );

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

        let _guard = crate::TEST_ENV_LOCK.lock().expect("env lock poisoned");
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
        assert_eq!(
            bytes, 0,
            "restart must use server stop command instead of IPC full_quit"
        );
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
    fn restart_uses_default_server_stop_command_when_stop_env_missing() {
        let _guard = crate::TEST_ENV_LOCK.lock().expect("env lock poisoned");
        std::env::set_var("AUTOFLUID_SERVER_MODE", "server");
        let project_dir = unique_temp_project_dir();
        let marker = project_dir.join("restart-ssh-marker.txt");
        let ssh_exe = fake_marker_exe(&project_dir, "fake_ssh", &marker, "ssh");
        let powershell_exe = fake_success_exe(&project_dir, "fake_pwsh");
        write_fake_server_ipc_tunnel_script(&project_dir);
        std::env::set_var("AUTOFLUID_POWERSHELL_EXE", &powershell_exe);
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

        let marker_text = fs::read_to_string(&marker).expect("read marker");
        assert_eq!(marker_text.lines().count(), 2);
        assert!(log_buffer
            .info_messages
            .iter()
            .any(|message| message.contains("已向服务器发送 daemon 启动命令")));

        std::env::remove_var("AUTOFLUID_SERVER_DAEMON_START_CMD");
        std::env::remove_var("AUTOFLUID_SERVER_DAEMON_SSH_TARGET");
        std::env::remove_var("AUTOFLUID_SSH_EXE");
        std::env::remove_var("AUTOFLUID_POWERSHELL_EXE");
        std::env::remove_var("AUTOFLUID_SERVER_MODE");
        let _ = fs::remove_dir_all(project_dir);
    }

    #[test]
    fn stop_sends_server_daemon_stop_command_in_server_mode() {
        let _guard = crate::TEST_ENV_LOCK.lock().expect("env lock poisoned");
        std::env::set_var("AUTOFLUID_SERVER_MODE", "server");
        let project_dir = unique_temp_project_dir();
        let marker = project_dir.join("ssh-stop-marker.txt");
        let ssh_exe = fake_marker_exe(&project_dir, "fake_ssh", &marker, "stop");
        std::env::set_var("AUTOFLUID_SSH_EXE", &ssh_exe);
        std::env::set_var("AUTOFLUID_SERVER_DAEMON_SSH_TARGET", "ocar-prod");
        std::env::set_var("AUTOFLUID_SERVER_DAEMON_STOP_CMD", "echo stop");

        let mut daemon = DaemonManager::new();
        daemon
            .stop(project_dir.to_str().expect("utf8 temp path"))
            .expect("server stop should succeed");

        let marker_text = fs::read_to_string(&marker).expect("read marker");
        let marker_lines: Vec<&str> = marker_text.lines().collect();
        assert!(!marker_lines.is_empty());
        assert!(marker_lines.iter().all(|line| *line == "stop"));

        std::env::remove_var("AUTOFLUID_SERVER_DAEMON_STOP_CMD");
        std::env::remove_var("AUTOFLUID_SERVER_DAEMON_SSH_TARGET");
        std::env::remove_var("AUTOFLUID_SSH_EXE");
        std::env::remove_var("AUTOFLUID_SERVER_MODE");
        let _ = fs::remove_dir_all(project_dir);
    }

    #[test]
    fn stop_cleans_server_ipc_tunnel_pid_in_server_mode() {
        let _guard = crate::TEST_ENV_LOCK.lock().expect("env lock poisoned");
        std::env::set_var("AUTOFLUID_SERVER_MODE", "server");
        let project_dir = unique_temp_project_dir();
        let marker = project_dir.join("ssh-stop-with-tunnel-marker.txt");
        let ssh_exe = fake_marker_exe(&project_dir, "fake_ssh", &marker, "stop");
        std::env::set_var("AUTOFLUID_SSH_EXE", &ssh_exe);
        std::env::set_var("AUTOFLUID_SERVER_DAEMON_SSH_TARGET", "ocar-prod");
        std::env::set_var("AUTOFLUID_SERVER_DAEMON_STOP_CMD", "echo stop");

        let pid_file = project_dir.join("data").join("server_ipc_tunnel.pid");
        fs::create_dir_all(pid_file.parent().expect("pid parent")).expect("create data dir");
        let mut child = spawn_live_pid_process(&project_dir);
        fs::write(&pid_file, child.id().to_string()).expect("write pid");

        let mut daemon = DaemonManager::new();
        daemon
            .stop(project_dir.to_str().expect("utf8 temp path"))
            .expect("server stop should succeed");

        let exited = wait_for_child_exit(&mut child, 2);
        if !exited {
            let _ = child.kill();
            let _ = child.wait();
        }
        assert!(
            exited,
            "server stop should terminate the server IPC tunnel PID"
        );
        assert!(!pid_file.exists(), "server IPC tunnel PID file removed");

        std::env::remove_var("AUTOFLUID_SERVER_DAEMON_STOP_CMD");
        std::env::remove_var("AUTOFLUID_SERVER_DAEMON_SSH_TARGET");
        std::env::remove_var("AUTOFLUID_SSH_EXE");
        std::env::remove_var("AUTOFLUID_SERVER_MODE");
        let _ = fs::remove_dir_all(project_dir);
    }

    #[test]
    fn stop_with_ipc_prefers_server_stop_command_even_when_ipc_connected() {
        let _guard = crate::TEST_ENV_LOCK.lock().expect("env lock poisoned");
        std::env::set_var("AUTOFLUID_SERVER_MODE", "server");
        let project_dir = unique_temp_project_dir();
        let marker = project_dir.join("ssh-stop-after-ipc-marker.txt");
        let full_quit_marker = project_dir.join("full-quit-seen.txt");
        let ssh_exe = fake_marker_exe(&project_dir, "fake_ssh", &marker, "stop");
        std::env::set_var("AUTOFLUID_SSH_EXE", &ssh_exe);
        std::env::set_var("AUTOFLUID_SERVER_DAEMON_SSH_TARGET", "ocar-prod");
        std::env::set_var("AUTOFLUID_SERVER_DAEMON_STOP_CMD", "echo stop");
        let pid_file = project_dir.join("data").join("server_ipc_tunnel.pid");
        fs::create_dir_all(pid_file.parent().expect("pid parent")).expect("create data dir");
        let mut child = spawn_live_pid_process(&project_dir);
        fs::write(&pid_file, child.id().to_string()).expect("write pid");

        let listener = TcpListener::bind("127.0.0.1:0").expect("bind test ipc");
        let port = listener.local_addr().expect("listener addr").port();
        let full_quit_marker_for_server = full_quit_marker.clone();
        let server = std::thread::spawn(move || {
            let (mut stream, _) = listener.accept().expect("accept ipc client");
            stream
                .set_read_timeout(Some(Duration::from_secs(1)))
                .expect("set read timeout");
            let mut buf = [0_u8; 1024];
            let handshake_bytes = stream.read(&mut buf).expect("read handshake");
            assert!(handshake_bytes > 0, "connect should send handshake");
            let handshake_text = String::from_utf8_lossy(&buf[..handshake_bytes]);
            let handshake_id = extract_request_id(&handshake_text);
            let handshake_response = format!(
                r#"{{"status":"ok","data":{{"engine_status":"stopped"}},"message":"","request_id":"{handshake_id}"}}"#
            );
            let _ = stream.write_all(handshake_response.as_bytes());
            let _ = stream.write_all(b"\n");
            if let Ok(bytes) = stream.read(&mut buf) {
                if bytes > 0 {
                    fs::write(&full_quit_marker_for_server, &buf[..bytes])
                        .expect("write full quit marker");
                }
            }
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

        let stopped = daemon.stop_with_ipc(
            &mut ipc,
            &rt,
            &mut state,
            &mut log_buffer,
            project_dir.to_str().expect("utf8 temp path"),
        );

        assert!(stopped);
        server.join().expect("server thread");
        assert!(marker.exists(), "server stop must issue SSH/systemd stop");
        assert!(
            !full_quit_marker.exists(),
            "server stop must not block on IPC full_quit"
        );
        let exited = wait_for_child_exit(&mut child, 2);
        if !exited {
            let _ = child.kill();
            let _ = child.wait();
        }
        assert!(exited, "server stop path should terminate the tunnel PID");
        assert!(!pid_file.exists(), "server IPC tunnel PID file removed");

        std::env::remove_var("AUTOFLUID_SERVER_DAEMON_STOP_CMD");
        std::env::remove_var("AUTOFLUID_SERVER_DAEMON_SSH_TARGET");
        std::env::remove_var("AUTOFLUID_SSH_EXE");
        std::env::remove_var("AUTOFLUID_SERVER_MODE");
        let _ = fs::remove_dir_all(project_dir);
    }

    #[test]
    fn finish_after_successful_ipc_stop_still_runs_server_stop_fallback() {
        let _guard = crate::TEST_ENV_LOCK.lock().expect("env lock poisoned");
        std::env::set_var("AUTOFLUID_SERVER_MODE", "server");
        let project_dir = unique_temp_project_dir();
        let marker = project_dir.join("finish-after-ipc-stop-marker.txt");
        let ssh_exe = fake_marker_exe(&project_dir, "fake_ssh", &marker, "stop");
        std::env::set_var("AUTOFLUID_SSH_EXE", &ssh_exe);
        std::env::set_var("AUTOFLUID_SERVER_DAEMON_SSH_TARGET", "ocar-prod");
        std::env::set_var("AUTOFLUID_SERVER_DAEMON_STOP_CMD", "echo stop");

        let mut daemon = DaemonManager::new();
        daemon
            .finish_after_successful_ipc_stop(project_dir.to_str().expect("utf8 temp path"))
            .expect("server finish-after-ipc stop should use SSH fallback");

        assert!(
            marker.exists(),
            "IPC success path must still issue server stop"
        );

        std::env::remove_var("AUTOFLUID_SERVER_DAEMON_STOP_CMD");
        std::env::remove_var("AUTOFLUID_SERVER_DAEMON_SSH_TARGET");
        std::env::remove_var("AUTOFLUID_SSH_EXE");
        std::env::remove_var("AUTOFLUID_SERVER_MODE");
        let _ = fs::remove_dir_all(project_dir);
    }
    fn wait_for_child_exit(child: &mut Child, timeout_secs: u64) -> bool {
        let deadline = Instant::now() + Duration::from_secs(timeout_secs);
        while Instant::now() < deadline {
            if child.try_wait().expect("query child status").is_some() {
                return true;
            }
            std::thread::sleep(Duration::from_millis(100));
        }
        child.try_wait().expect("query child status").is_some()
    }

    fn extract_request_id(text: &str) -> String {
        let marker = "\"request_id\":\"";
        let start = text.find(marker).expect("request_id marker") + marker.len();
        let end = text[start..]
            .find('"')
            .map(|offset| start + offset)
            .expect("request_id end");
        text[start..end].to_string()
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

    fn fake_sleep_exe(project_dir: &std::path::Path, name: &str, seconds: u64) -> PathBuf {
        #[cfg(windows)]
        {
            let path = project_dir.join(format!("{name}.cmd"));
            fs::write(
                &path,
                format!(
                    "@echo off\r\nping 127.0.0.1 -n {} >nul\r\nexit /b 0\r\n",
                    seconds + 1
                ),
            )
            .expect("write fake sleep cmd");
            path
        }
        #[cfg(not(windows))]
        {
            let path = project_dir.join(format!("{name}.sh"));
            fs::write(&path, format!("#!/bin/sh\nsleep {seconds}\nexit 0\n"))
                .expect("write fake sleep sh");
            use std::os::unix::fs::PermissionsExt;
            let mut permissions = fs::metadata(&path)
                .expect("fake sleep metadata")
                .permissions();
            permissions.set_mode(0o755);
            fs::set_permissions(&path, permissions).expect("chmod fake sleep");
            path
        }
    }

    fn fake_background_handle_holder_exe(project_dir: &std::path::Path) -> PathBuf {
        #[cfg(windows)]
        {
            let path = project_dir.join("fake_bg_handle_holder.cmd");
            fs::write(
                &path,
                "@echo off\r\nstart \"\" /b cmd /c \"ping 127.0.0.1 -n 4 >nul\"\r\nexit /b 0\r\n",
            )
            .expect("write fake background holder cmd");
            path
        }
        #[cfg(not(windows))]
        {
            let path = project_dir.join("fake_bg_handle_holder.sh");
            fs::write(&path, "#!/bin/sh\n(sleep 3) &\nexit 0\n")
                .expect("write fake background holder sh");
            use std::os::unix::fs::PermissionsExt;
            let mut permissions = fs::metadata(&path)
                .expect("fake background holder metadata")
                .permissions();
            permissions.set_mode(0o755);
            fs::set_permissions(&path, permissions).expect("chmod fake background holder");
            path
        }
    }

    fn spawn_live_pid_process(project_dir: &std::path::Path) -> Child {
        #[cfg(windows)]
        {
            let path = project_dir.join("fake_live_pid.cmd");
            fs::write(&path, "@echo off\r\nping 127.0.0.1 -n 30 >nul\r\n")
                .expect("write fake live pid cmd");
            Command::new("cmd")
                .arg("/C")
                .arg(&path)
                .stdout(Stdio::null())
                .stderr(Stdio::null())
                .spawn()
                .expect("spawn live pid process")
        }
        #[cfg(not(windows))]
        {
            let path = project_dir.join("fake_live_pid.sh");
            fs::write(&path, "#!/bin/sh\nsleep 30\n").expect("write fake live pid sh");
            use std::os::unix::fs::PermissionsExt;
            let mut permissions = fs::metadata(&path)
                .expect("fake live metadata")
                .permissions();
            permissions.set_mode(0o755);
            fs::set_permissions(&path, permissions).expect("chmod fake live");
            Command::new(&path)
                .stdout(Stdio::null())
                .stderr(Stdio::null())
                .spawn()
                .expect("spawn live pid process")
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

    fn fake_argument_marker_exe(
        project_dir: &std::path::Path,
        name: &str,
        marker: &std::path::Path,
    ) -> PathBuf {
        #[cfg(windows)]
        {
            let path = project_dir.join(format!("{name}.cmd"));
            fs::write(
                &path,
                format!(
                    "@echo off\r\necho %*>>\"{}\"\r\nexit /b 0\r\n",
                    marker.display()
                ),
            )
            .expect("write fake argument marker cmd");
            path
        }
        #[cfg(not(windows))]
        {
            let path = project_dir.join(format!("{name}.sh"));
            fs::write(
                &path,
                format!(
                    "#!/bin/sh\nprintf '%s\\n' \"$*\" >> '{}'\nexit 0\n",
                    marker.display()
                ),
            )
            .expect("write fake argument marker shell");
            use std::os::unix::fs::PermissionsExt;
            let mut permissions = fs::metadata(&path)
                .expect("fake argument marker metadata")
                .permissions();
            permissions.set_mode(0o755);
            fs::set_permissions(&path, permissions).expect("chmod fake argument marker");
            path
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

    fn fake_config_sync_ssh_exe(
        project_dir: &std::path::Path,
        marker: &std::path::Path,
        captured_config: &std::path::Path,
    ) -> PathBuf {
        #[cfg(windows)]
        {
            let path = project_dir.join("fake_config_sync_ssh.cmd");
            fs::write(
                &path,
                format!(
                    "@echo off\r\n\
                     if not exist \"{}\" (\r\n\
                     echo sync>>\"{}\"\r\n\
                     more > \"{}\"\r\n\
                     exit /b 0\r\n\
                     )\r\n\
                     echo daemon>>\"{}\"\r\n\
                     exit /b 0\r\n",
                    captured_config.display(),
                    marker.display(),
                    captured_config.display(),
                    marker.display()
                ),
            )
            .expect("write fake config sync ssh cmd");
            path
        }
        #[cfg(not(windows))]
        {
            let path = project_dir.join("fake_config_sync_ssh.sh");
            fs::write(
                &path,
                format!(
                    "#!/bin/sh\n\
                     case \"$*\" in\n\
                     *autofluid_config.toml*) echo sync >> '{}'; cat > '{}'; exit 0 ;;\n\
                     *) echo daemon >> '{}'; exit 0 ;;\n\
                     esac\n",
                    marker.display(),
                    captured_config.display(),
                    marker.display()
                ),
            )
            .expect("write fake config sync ssh shell");
            use std::os::unix::fs::PermissionsExt;
            let mut permissions = fs::metadata(&path)
                .expect("fake config sync metadata")
                .permissions();
            permissions.set_mode(0o755);
            fs::set_permissions(&path, permissions).expect("chmod fake config sync ssh");
            path
        }
    }

    fn normalize_line_endings(value: &str) -> String {
        value.replace("\r\n", "\n").trim_end().to_string()
    }
}
