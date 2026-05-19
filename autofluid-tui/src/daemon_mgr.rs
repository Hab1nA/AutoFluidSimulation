use std::fs;
use std::path::PathBuf;
use std::process::{Child, Command};


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
}
