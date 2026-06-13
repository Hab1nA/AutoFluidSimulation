use std::time::{Duration, Instant};
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::net::TcpStream;

use super::protocol::{IpcRequest, IpcResponse};

const DEFAULT_HOST: &str = "127.0.0.1";
const DEFAULT_PORT: u16 = 9527;
const DEFAULT_TIMEOUT: Duration = Duration::from_secs(5);
/// check 命令涉及远程 SSH 自检（含 conda/目录/文件/磁盘/进程检查），需要更长超时。
const CHECK_TIMEOUT: Duration = Duration::from_secs(60);
const MAX_RESPONSE_BYTES: usize = 1024 * 1024; // 1MB 行长度上限
/// 自动重连冷却时间：防止 daemon 不可用时频繁重连。
const RECONNECT_COOLDOWN: Duration = Duration::from_secs(5);

pub struct IpcClient {
    host: String,
    port: u16,
    stream: Option<TcpStream>,
    /// 上次重连尝试时间，用于冷却。
    last_reconnect: Option<Instant>,
    /// 连续重连失败次数，用于分级冷却。
    consecutive_failures: u32,
}

impl IpcClient {
    pub fn new(host: Option<&str>, port: Option<u16>) -> Self {
        Self {
            host: host.map(str::to_string).unwrap_or_else(default_host),
            port: port.unwrap_or_else(default_port),
            stream: None,
            last_reconnect: None,
            consecutive_failures: 0,
        }
    }

    pub fn is_connected(&self) -> bool {
        self.stream.is_some()
    }

    pub fn host(&self) -> &str {
        &self.host
    }

    pub async fn connect(&mut self) -> Result<(), String> {
        // 先关闭已有连接，防止连接泄漏导致服务端出现重复连接
        self.disconnect().await;
        let addr = format!("{}:{}", self.host, self.port);
        log::info!("[IPC] 正在连接后台引擎: {}", addr);
        match tokio::time::timeout(DEFAULT_TIMEOUT, TcpStream::connect(&addr)).await {
            Ok(Ok(stream)) => {
                self.stream = Some(stream);
                match self.verify_connection().await {
                    Ok(()) => {
                        self.last_reconnect = None; // 连接成功，清除冷却
                        self.consecutive_failures = 0; // 连接成功，重置失败计数
                        log::info!("[IPC] 已连接后台引擎: {}", addr);
                        Ok(())
                    }
                    Err(e) => {
                        self.disconnect().await;
                        log::warn!("[IPC] 连接握手失败: {}, error={}", addr, e);
                        Err(format!("连接握手失败: {}", e))
                    }
                }
            }
            Ok(Err(e)) => {
                log::warn!("[IPC] 连接失败: {}, error={}", addr, e);
                Err(format!("连接失败: {}", e))
            }
            Err(_) => {
                log::warn!("[IPC] 连接超时: {}", addr);
                Err("连接超时".to_string())
            }
        }
    }

    pub async fn disconnect(&mut self) {
        if let Some(mut stream) = self.stream.take() {
            let _ = stream.shutdown().await;
            log::info!("[IPC] 已断开后台引擎连接");
        }
    }

    async fn verify_connection(&mut self) -> Result<(), String> {
        let request = IpcRequest::new(super::protocol::CMD_GET_ENGINE_STATUS);
        let mut stream = self.stream.take().ok_or_else(|| "未连接".to_string())?;

        let data = request.serialize();
        stream
            .write_all(&data)
            .await
            .map_err(|e| format!("发送失败: {}", e))?;

        let mut reader = BufReader::new(stream);
        let mut buffer = Vec::new();
        let read_result = tokio::time::timeout(DEFAULT_TIMEOUT, async {
            // 使用 read_until 替代逐字节读取，提高效率
            match reader.read_until(b'\n', &mut buffer).await {
                Ok(0) => Ok(false), // EOF
                Ok(_) => {
                    // 检查响应长度
                    if buffer.len() > MAX_RESPONSE_BYTES {
                        Err(std::io::Error::new(
                            std::io::ErrorKind::InvalidData,
                            format!("IPC 响应超过 {} 字节上限", MAX_RESPONSE_BYTES),
                        ))
                    } else {
                        Ok(true)
                    }
                }
                Err(e) => Err(e),
            }
        })
        .await;

        let response = match read_result {
            Ok(Ok(true)) => {
                IpcResponse::deserialize(&buffer).ok_or_else(|| "无效响应格式".to_string())?
            }
            Ok(Ok(false)) => return Err("连接已断开".to_string()),
            Ok(Err(e)) => return Err(format!("读取失败: {}", e)),
            Err(_) => return Err("请求超时".to_string()),
        };

        self.stream = Some(reader.into_inner());
        if response.is_ok() {
            Ok(())
        } else {
            Err(response.message)
        }
    }

    /// 发送 IPC 请求并等待响应（使用默认超时）。
    pub async fn send_request(&mut self, request: &IpcRequest) -> Result<IpcResponse, String> {
        self.send_request_with_timeout(request, DEFAULT_TIMEOUT)
            .await
    }

    /// 发送 IPC 请求并等待响应（使用自定义超时）。
    ///
    /// 用于已知耗时较长的命令（如 `check` 涉及远程 SSH 自检）。
    /// 任何导致 stream 丢失的错误（超时/读取失败/对端关闭）都会自动尝试重连，
    /// 确保客户端不会永久停留在断连状态。
    pub async fn send_request_with_timeout(
        &mut self,
        request: &IpcRequest,
        timeout: Duration,
    ) -> Result<IpcResponse, String> {
        let started_at = Instant::now();
        log_request_start(request, timeout);
        let mut stream = match self.stream.take() {
            Some(s) => s,
            None => {
                log::warn!(
                    "[IPC] 请求暂未发送: command={}, request_id={}, reason=未连接，尝试重连",
                    request.command,
                    request.request_id
                );
                self.auto_reconnect("请求前未连接").await;
                match self.stream.take() {
                    Some(s) => s,
                    None => return Err("未连接".to_string()),
                }
            }
        };

        let data = request.serialize();
        if let Err(e) = stream.write_all(&data).await {
            // 发送失败：stream 可能已损坏，丢弃连接后自动重连
            self.auto_reconnect("发送失败").await;
            log::warn!(
                "[IPC] 请求发送失败: command={}, request_id={}, elapsed_ms={}, error={}",
                request.command,
                request.request_id,
                started_at.elapsed().as_millis(),
                e
            );
            return Err(format!("发送失败: {}", e));
        }

        let mut reader = BufReader::new(stream);
        let mut buffer = Vec::new();

        // 使用 read_until 替代逐字节读取，提高效率
        let read_result = tokio::time::timeout(timeout, async {
            match reader.read_until(b'\n', &mut buffer).await {
                Ok(0) => Ok(false), // EOF
                Ok(_) => {
                    // 检查响应长度
                    if buffer.len() > MAX_RESPONSE_BYTES {
                        Err(std::io::Error::new(
                            std::io::ErrorKind::InvalidData,
                            format!("IPC 响应超过 {} 字节上限", MAX_RESPONSE_BYTES),
                        ))
                    } else {
                        Ok(true)
                    }
                }
                Err(e) => Err(e),
            }
        })
        .await;

        match read_result {
            Ok(Ok(true)) => {
                match IpcResponse::deserialize(&buffer) {
                    Some(resp) => {
                        // 正常读取成功：从 reader 取回 stream 归还
                        let stream = reader.into_inner();
                        self.stream = Some(stream);
                        log_request_finish(request, &resp, started_at.elapsed());
                        Ok(resp)
                    }
                    None => {
                        // 响应格式异常时丢弃当前连接，避免下一次请求复用已脱序的流。
                        drop(reader);
                        self.auto_reconnect("响应解析失败").await;
                        log::warn!(
                            "[IPC] 响应解析失败: command={}, request_id={}, elapsed_ms={}",
                            request.command,
                            request.request_id,
                            started_at.elapsed().as_millis()
                        );
                        Err("无效响应格式".to_string())
                    }
                }
            }
            Ok(Ok(false)) => {
                // 对端关闭连接 → stream 已不可用，自动重连
                self.auto_reconnect("对端关闭").await;
                log::warn!(
                    "[IPC] 请求失败: command={}, request_id={}, elapsed_ms={}, reason=连接已断开",
                    request.command,
                    request.request_id,
                    started_at.elapsed().as_millis()
                );
                Err("连接已断开".to_string())
            }
            Ok(Err(e)) => {
                // 读取 I/O 错误：丢弃 stream，防止后续通信脱序。
                // BufReader 内部缓冲区中的字节已被 read() 从内核消耗，
                // 但未被应用层消费；into_inner() 后这些字节永久丢失，
                // 而内核缓冲区中可能残留后续字节 → 下次读取脱序。
                // 直接丢弃 reader（含其内部缓冲区和底层 stream）最安全。
                self.auto_reconnect("读取失败").await;
                log::warn!(
                    "[IPC] 请求读取失败: command={}, request_id={}, elapsed_ms={}, error={}",
                    request.command,
                    request.request_id,
                    started_at.elapsed().as_millis(),
                    e
                );
                Err(format!("读取失败: {}", e))
            }
            Err(_) => {
                // 读取超时：同样丢弃 stream，原因同上。
                // 服务端可能仍在处理旧请求（旧连接线程完成后自行清理），
                // 新连接获得干净的字节流，不会与旧请求混淆。
                self.auto_reconnect("请求超时").await;
                log::warn!(
                    "[IPC] 请求超时: command={}, request_id={}, timeout_ms={}, elapsed_ms={}",
                    request.command,
                    request.request_id,
                    timeout.as_millis(),
                    started_at.elapsed().as_millis()
                );
                Err("请求超时".to_string())
            }
        }
    }

    /// 内部方法：连接丢失后自动尝试重连（带冷却时间）。
    ///
    /// 静默执行——成功时恢复 `self.stream`，失败时仅记录日志。
    /// 调用方无需感知重连结果，下次 `is_connected()` 即可反映真实状态。
    /// 分级冷却机制：仅在连续失败时增加冷却间隔，成功重连后立即重置。
    ///
    /// 注意：此方法通常在 `send_request_with_timeout` 内部调用，此时 `self.stream`
    /// 已被 `take()` 取出放入 `BufReader`。重连会创建全新连接，旧连接的清理
    /// 依赖 `BufReader` 的 drop——短暂窗口内服务端可能看到两个活跃连接。
    async fn auto_reconnect(&mut self, reason: &str) {
        // 仅当存在连续失败时才应用冷却
        if self.consecutive_failures > 0 {
            if let Some(last) = self.last_reconnect {
                if last.elapsed() < RECONNECT_COOLDOWN {
                    log::debug!("[IPC] {}，重连冷却中，跳过", reason);
                    return;
                }
            }
        }
        self.last_reconnect = Some(Instant::now());
        log::info!("[IPC] {}，尝试自动重连...", reason);
        // connect() 内部会先 disconnect()，确保 self.stream 中的旧连接被关闭。
        // 注意：send_request_with_timeout 中 take() 取出的 stream 不受此影响，
        // 旧 stream 的关闭由 BufReader 的 drop 保证。
        match self.connect().await {
            Ok(()) => {
                self.consecutive_failures = 0; // 成功重连，重置失败计数
                log::info!("[IPC] 自动重连成功")
            }
            Err(e) => {
                self.consecutive_failures += 1;
                log::warn!("[IPC] 自动重连失败: {}", e)
            }
        }
    }

    pub async fn start_pipeline(&mut self) -> Result<IpcResponse, String> {
        self.send_request(&IpcRequest::new(super::protocol::CMD_START))
            .await
    }

    pub async fn pause_pipeline(&mut self) -> Result<IpcResponse, String> {
        self.send_request(&IpcRequest::new(super::protocol::CMD_PAUSE))
            .await
    }

    pub async fn full_quit(&mut self) -> Result<IpcResponse, String> {
        self.send_request(&IpcRequest::new(super::protocol::CMD_STOP))
            .await
    }

    pub async fn check_system(&mut self) -> Result<IpcResponse, String> {
        self.send_request_with_timeout(&IpcRequest::new(super::protocol::CMD_CHECK), CHECK_TIMEOUT)
            .await
    }

    pub async fn get_statistics(&mut self) -> Result<IpcResponse, String> {
        self.send_request(&IpcRequest::new(super::protocol::CMD_GET_STATISTICS))
            .await
    }

    pub async fn get_dashboard(
        &mut self,
        since_log_id: u64,
        log_limit: u64,
    ) -> Result<IpcResponse, String> {
        let mut params = serde_json::Map::new();
        params.insert(
            "since_log_id".to_string(),
            serde_json::Value::Number(since_log_id.into()),
        );
        params.insert(
            "log_limit".to_string(),
            serde_json::Value::Number(log_limit.into()),
        );
        self.send_request(&IpcRequest::with_params(
            super::protocol::CMD_GET_DASHBOARD,
            serde_json::Value::Object(params),
        ))
        .await
    }

    pub async fn reset_step(
        &mut self,
        config_name: serde_json::Value,
        step_name: Option<&str>,
    ) -> Result<IpcResponse, String> {
        let mut params = serde_json::Map::new();
        params.insert("config_name".to_string(), config_name);
        if let Some(sn) = step_name {
            params.insert(
                "step_name".to_string(),
                serde_json::Value::String(sn.to_string()),
            );
        }
        self.send_request(&IpcRequest::with_params(
            super::protocol::CMD_RESET_STEP,
            serde_json::Value::Object(params),
        ))
        .await
    }

    pub async fn clean_step(
        &mut self,
        step_name: &str,
        config_name: Option<serde_json::Value>,
    ) -> Result<IpcResponse, String> {
        let mut params = serde_json::Map::new();
        params.insert(
            "step_name".to_string(),
            serde_json::Value::String(step_name.to_string()),
        );
        if let Some(cn) = config_name {
            params.insert("config_name".to_string(), cn);
        }
        self.send_request(&IpcRequest::with_params(
            super::protocol::CMD_CLEAN_STEP,
            serde_json::Value::Object(params),
        ))
        .await
    }

    pub async fn reload_config(&mut self) -> Result<IpcResponse, String> {
        self.send_request(&IpcRequest::new(super::protocol::CMD_RELOAD_CONFIG))
            .await
    }

    #[allow(dead_code)]
    pub async fn worker_register(
        &mut self,
        worker_id: &str,
        capabilities: serde_json::Value,
    ) -> Result<IpcResponse, String> {
        let mut params = serde_json::Map::new();
        params.insert(
            "worker_id".to_string(),
            serde_json::Value::String(worker_id.to_string()),
        );
        params.insert("capabilities".to_string(), capabilities);
        self.send_request(&IpcRequest::with_params(
            super::protocol::CMD_WORKER_REGISTER,
            serde_json::Value::Object(params),
        ))
        .await
    }

    #[allow(dead_code)]
    pub async fn worker_heartbeat(&mut self, worker_id: &str) -> Result<IpcResponse, String> {
        let mut params = serde_json::Map::new();
        params.insert(
            "worker_id".to_string(),
            serde_json::Value::String(worker_id.to_string()),
        );
        self.send_request(&IpcRequest::with_params(
            super::protocol::CMD_WORKER_HEARTBEAT,
            serde_json::Value::Object(params),
        ))
        .await
    }

    pub async fn worker_start(&mut self) -> Result<IpcResponse, String> {
        self.send_request(&IpcRequest::new(super::protocol::CMD_WORKER_START))
            .await
    }

    pub async fn worker_stop(&mut self) -> Result<IpcResponse, String> {
        self.send_request(&IpcRequest::new(super::protocol::CMD_WORKER_STOP))
            .await
    }

    #[allow(dead_code)]
    pub async fn worker_restart(&mut self) -> Result<IpcResponse, String> {
        self.send_request(&IpcRequest::new(super::protocol::CMD_WORKER_RESTART))
            .await
    }
}

fn default_host() -> String {
    std::env::var("AUTOFLUID_IPC_HOST")
        .ok()
        .filter(|host| !host.is_empty())
        .unwrap_or_else(|| DEFAULT_HOST.to_string())
}

fn default_port() -> u16 {
    std::env::var("AUTOFLUID_IPC_PORT")
        .ok()
        .and_then(|port| port.parse::<u16>().ok())
        .unwrap_or(DEFAULT_PORT)
}

fn log_request_start(request: &IpcRequest, timeout: Duration) {
    if is_polling_command(&request.command) {
        log::debug!(
            "[IPC] 发送轮询请求: command={}, request_id={}, timeout_ms={}",
            request.command,
            request.request_id,
            timeout.as_millis()
        );
    } else {
        log::info!(
            "[IPC] 发送请求: command={}, request_id={}, timeout_ms={}",
            request.command,
            request.request_id,
            timeout.as_millis()
        );
    }
}

fn log_request_finish(request: &IpcRequest, response: &IpcResponse, elapsed: Duration) {
    if is_polling_command(&request.command) {
        log::debug!(
            "[IPC] 收到轮询响应: command={}, request_id={}, status={}, elapsed_ms={}",
            request.command,
            request.request_id,
            response.status,
            elapsed.as_millis()
        );
    } else if response.is_ok() {
        log::info!(
            "[IPC] 收到响应: command={}, request_id={}, status={}, elapsed_ms={}",
            request.command,
            request.request_id,
            response.status,
            elapsed.as_millis()
        );
    } else {
        log::warn!(
            "[IPC] 收到失败响应: command={}, request_id={}, status={}, elapsed_ms={}, message={}",
            request.command,
            request.request_id,
            response.status,
            elapsed.as_millis(),
            response.message
        );
    }
}

fn is_polling_command(command: &str) -> bool {
    matches!(
        command,
        super::protocol::CMD_GET_ALL_STATUS
            | super::protocol::CMD_GET_ENGINE_STATUS
            | super::protocol::CMD_GET_LOG_ENTRIES
            | super::protocol::CMD_GET_DASHBOARD
    )
}

#[cfg(test)]
mod tests {
    use super::IpcClient;
    use serde_json::Value;
    use std::io::{BufRead, BufReader, Write};
    use std::net::TcpListener;
    use std::sync::Mutex;

    static ENV_LOCK: Mutex<()> = Mutex::new(());

    #[test]
    fn client_uses_env_endpoint_when_args_absent() {
        let _guard = ENV_LOCK.lock().expect("env lock poisoned");
        std::env::set_var("AUTOFLUID_IPC_HOST", "ocar.example");
        std::env::set_var("AUTOFLUID_IPC_PORT", "9650");

        let client = IpcClient::new(None, None);

        assert_eq!(client.host, "ocar.example");
        assert_eq!(client.port, 9650);

        std::env::remove_var("AUTOFLUID_IPC_HOST");
        std::env::remove_var("AUTOFLUID_IPC_PORT");
    }

    #[test]
    fn client_args_override_env_endpoint() {
        let _guard = ENV_LOCK.lock().expect("env lock poisoned");
        std::env::set_var("AUTOFLUID_IPC_HOST", "ocar.example");
        std::env::set_var("AUTOFLUID_IPC_PORT", "9650");

        let client = IpcClient::new(Some("127.0.0.1"), Some(9527));

        assert_eq!(client.host, "127.0.0.1");
        assert_eq!(client.port, 9527);

        std::env::remove_var("AUTOFLUID_IPC_HOST");
        std::env::remove_var("AUTOFLUID_IPC_PORT");
    }

    #[test]
    fn connect_rejects_tcp_accept_without_ipc_response() {
        let listener = TcpListener::bind("127.0.0.1:0").expect("bind test listener");
        let port = listener.local_addr().expect("listener address").port();
        let server = std::thread::spawn(move || {
            let (_stream, _) = listener.accept().expect("accept client");
        });
        let rt = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .expect("runtime");
        let mut client = IpcClient::new(Some("127.0.0.1"), Some(port));

        let result = rt.block_on(client.connect());

        assert!(result.is_err());
        assert!(!client.is_connected());
        server.join().expect("server thread");
    }

    #[test]
    fn connect_succeeds_after_engine_status_handshake() {
        let listener = TcpListener::bind("127.0.0.1:0").expect("bind test listener");
        let port = listener.local_addr().expect("listener address").port();
        let server = std::thread::spawn(move || {
            let (mut stream, _) = listener.accept().expect("accept client");
            let mut line = String::new();
            BufReader::new(stream.try_clone().expect("clone stream"))
                .read_line(&mut line)
                .expect("read handshake");
            let request: Value = serde_json::from_str(line.trim()).expect("handshake json");
            assert_eq!(
                request.get("command").and_then(Value::as_str),
                Some(super::super::protocol::CMD_GET_ENGINE_STATUS)
            );
            let request_id = request
                .get("request_id")
                .and_then(Value::as_str)
                .expect("request id");
            let response = format!(
                r#"{{"status":"ok","data":{{"engine_status":"stopped"}},"message":"","request_id":"{request_id}"}}"#
            );
            stream
                .write_all(format!("{response}\n").as_bytes())
                .expect("write response");
        });
        let rt = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .expect("runtime");
        let mut client = IpcClient::new(Some("127.0.0.1"), Some(port));

        let result = rt.block_on(client.connect());

        assert!(result.is_ok());
        assert!(client.is_connected());
        rt.block_on(client.disconnect());
        server.join().expect("server thread");
    }

    #[test]
    fn get_dashboard_sends_log_cursor_and_limit() {
        let listener = TcpListener::bind("127.0.0.1:0").expect("bind test listener");
        let port = listener.local_addr().expect("listener address").port();
        let server = std::thread::spawn(move || {
            let (mut stream, _) = listener.accept().expect("accept client");
            let mut reader = BufReader::new(stream.try_clone().expect("clone stream"));

            let mut handshake = String::new();
            reader.read_line(&mut handshake).expect("read handshake");
            let handshake_request: Value =
                serde_json::from_str(handshake.trim()).expect("handshake json");
            let handshake_id = handshake_request
                .get("request_id")
                .and_then(Value::as_str)
                .expect("handshake request id");
            let handshake_response = format!(
                r#"{{"status":"ok","data":{{"engine_status":"stopped"}},"message":"","request_id":"{handshake_id}"}}"#
            );
            stream
                .write_all(format!("{handshake_response}\n").as_bytes())
                .expect("write handshake response");

            let mut dashboard = String::new();
            reader.read_line(&mut dashboard).expect("read dashboard");
            let request: Value = serde_json::from_str(dashboard.trim()).expect("dashboard json");
            assert_eq!(
                request.get("command").and_then(Value::as_str),
                Some(super::super::protocol::CMD_GET_DASHBOARD)
            );
            assert_eq!(
                request
                    .get("params")
                    .and_then(|params| params.get("since_log_id"))
                    .and_then(Value::as_u64),
                Some(17)
            );
            assert_eq!(
                request
                    .get("params")
                    .and_then(|params| params.get("log_limit"))
                    .and_then(Value::as_u64),
                Some(75)
            );
            let request_id = request
                .get("request_id")
                .and_then(Value::as_str)
                .expect("request id");
            let response = format!(
                r#"{{"status":"ok","data":{{"statuses":{{}},"engine":{{}},"logs":{{"entries":[],"latest_id":17}}}},"message":"","request_id":"{request_id}"}}"#
            );
            stream
                .write_all(format!("{response}\n").as_bytes())
                .expect("write dashboard response");
        });
        let rt = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .expect("runtime");
        let mut client = IpcClient::new(Some("127.0.0.1"), Some(port));

        rt.block_on(client.connect()).expect("connect");
        let result = rt.block_on(client.get_dashboard(17, 75));

        assert!(result.is_ok());
        rt.block_on(client.disconnect());
        server.join().expect("server thread");
    }

    #[test]
    fn consecutive_failures_resets_on_successful_connect() {
        let listener = TcpListener::bind("127.0.0.1:0").expect("bind test listener");
        let port = listener.local_addr().expect("listener address").port();
        let server = std::thread::spawn(move || {
            let (mut stream, _) = listener.accept().expect("accept client");
            let mut line = String::new();
            BufReader::new(stream.try_clone().expect("clone stream"))
                .read_line(&mut line)
                .expect("read handshake");
            let request: Value = serde_json::from_str(line.trim()).expect("handshake json");
            let request_id = request
                .get("request_id")
                .and_then(Value::as_str)
                .expect("request id");
            let response = format!(
                r#"{{"status":"ok","data":{{"engine_status":"stopped"}},"message":"","request_id":"{request_id}"}}"#
            );
            stream
                .write_all(format!("{response}\n").as_bytes())
                .expect("write response");
        });
        let rt = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .expect("runtime");
        let mut client = IpcClient::new(Some("127.0.0.1"), Some(port));

        // Simulate consecutive failures
        client.consecutive_failures = 3;

        // Successful connect should reset failures
        let result = rt.block_on(client.connect());
        assert!(result.is_ok());
        assert_eq!(client.consecutive_failures, 0);

        rt.block_on(client.disconnect());
        server.join().expect("server thread");
    }

    #[test]
    fn connect_failure_does_not_affect_consecutive_failures() {
        // connect() failure does NOT increment consecutive_failures —
        // only auto_reconnect() does that. Manual connect attempts are
        // independent of the auto-reconnect cooldown tracking.
        let rt = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .expect("runtime");
        let mut client = IpcClient::new(Some("127.0.0.1"), Some(1)); // port 1 should fail

        assert_eq!(client.consecutive_failures, 0);

        let result = rt.block_on(client.connect());
        assert!(result.is_err());
        assert_eq!(client.consecutive_failures, 0); // unchanged by manual connect failure
    }
}
