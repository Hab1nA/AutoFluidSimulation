use std::time::Duration;
use tokio::io::{AsyncReadExt, AsyncWriteExt, BufReader};
use tokio::net::TcpStream;

use super::protocol::{IpcRequest, IpcResponse};

const DEFAULT_HOST: &str = "127.0.0.1";
const DEFAULT_PORT: u16 = 9527;
const DEFAULT_TIMEOUT: Duration = Duration::from_secs(5);
const MAX_RESPONSE_BYTES: usize = 1024 * 1024; // 1MB 行长度上限

pub struct IpcClient {
    host: String,
    port: u16,
    stream: Option<TcpStream>,
}

impl IpcClient {
    pub fn new(host: Option<&str>, port: Option<u16>) -> Self {
        Self {
            host: host.unwrap_or(DEFAULT_HOST).to_string(),
            port: port.unwrap_or(DEFAULT_PORT),
            stream: None,
        }
    }

    pub fn is_connected(&self) -> bool {
        self.stream.is_some()
    }

    pub async fn connect(&mut self) -> Result<(), String> {
        let addr = format!("{}:{}", self.host, self.port);
        match tokio::time::timeout(DEFAULT_TIMEOUT, TcpStream::connect(&addr)).await {
            Ok(Ok(stream)) => {
                self.stream = Some(stream);
                Ok(())
            }
            Ok(Err(e)) => Err(format!("连接失败: {}", e)),
            Err(_) => Err("连接超时".to_string()),
        }
    }

    pub async fn disconnect(&mut self) {
        if let Some(mut stream) = self.stream.take() {
            let _ = stream.shutdown().await;
        }
    }

    pub async fn send_request(&mut self, request: &IpcRequest) -> Result<IpcResponse, String> {
        let mut stream = match self.stream.take() {
            Some(s) => s,
            None => return Err("未连接".to_string()),
        };

        let data = request.serialize();
        if let Err(e) = stream.write_all(&data).await {
            // 发送失败：stream 可能已损坏，丢弃连接
            return Err(format!("发送失败: {}", e));
        }

        let mut reader = BufReader::new(stream);
        let mut buffer = Vec::new();

        // 带长度限制的行读取：防止异常长响应导致内存溢出
        let read_result = tokio::time::timeout(DEFAULT_TIMEOUT, async {
            loop {
                let mut byte = [0u8; 1];
                match reader.read(&mut byte).await {
                    Ok(0) => return Ok(false), // EOF
                    Ok(_) => {
                        buffer.push(byte[0]);
                        if byte[0] == b'\n' {
                            return Ok(true); // 行结束
                        }
                        if buffer.len() > MAX_RESPONSE_BYTES {
                            return Err(std::io::Error::new(
                                std::io::ErrorKind::InvalidData,
                                format!("IPC 响应超过 {} 字节上限", MAX_RESPONSE_BYTES),
                            ));
                        }
                    }
                    Err(e) => return Err(e),
                }
            }
        })
        .await;

        match read_result {
            Ok(Ok(true)) => {
                // 正常读取成功：从 reader 取回 stream 归还
                let stream = reader.into_inner();
                self.stream = Some(stream);
                match IpcResponse::deserialize(&buffer) {
                    Some(resp) => Ok(resp),
                    None => Err("无效响应格式".to_string()),
                }
            }
            Ok(Ok(false)) => {
                // 对端关闭连接 → stream 已不可用，丢弃
                Err("连接已断开".to_string())
            }
            Ok(Err(e)) => {
                // 读取 I/O 错误（含超长响应拒绝）
                let stream = reader.into_inner();
                self.stream = Some(stream);
                Err(format!("读取失败: {}", e))
            }
            Err(_) => {
                // 读取超时：stream 仍然完好，归还以便下次重试
                let stream = reader.into_inner();
                self.stream = Some(stream);
                Err("请求超时".to_string())
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
        self.send_request(&IpcRequest::new(super::protocol::CMD_CHECK))
            .await
    }

    pub async fn get_all_status(&mut self) -> Result<IpcResponse, String> {
        self.send_request(&IpcRequest::new(super::protocol::CMD_GET_ALL_STATUS))
            .await
    }

    pub async fn get_statistics(&mut self) -> Result<IpcResponse, String> {
        self.send_request(&IpcRequest::new(super::protocol::CMD_GET_STATISTICS))
            .await
    }

    pub async fn get_engine_status(&mut self) -> Result<IpcResponse, String> {
        self.send_request(&IpcRequest::new(super::protocol::CMD_GET_ENGINE_STATUS))
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

    pub async fn get_log_entries(
        &mut self,
        since_id: u64,
        limit: u64,
        level_filter: Option<&str>,
        source_filter: Option<&str>,
    ) -> Result<IpcResponse, String> {
        let mut params = serde_json::Map::new();
        params.insert(
            "since_id".to_string(),
            serde_json::Value::Number(since_id.into()),
        );
        params.insert("limit".to_string(), serde_json::Value::Number(limit.into()));
        if let Some(lf) = level_filter {
            params.insert(
                "level_filter".to_string(),
                serde_json::Value::String(lf.to_string()),
            );
        }
        if let Some(sf) = source_filter {
            params.insert(
                "source_filter".to_string(),
                serde_json::Value::String(sf.to_string()),
            );
        }
        self.send_request(&IpcRequest::with_params(
            super::protocol::CMD_GET_LOG_ENTRIES,
            serde_json::Value::Object(params),
        ))
        .await
    }
}
