use std::time::Duration;
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::net::TcpStream;

use super::protocol::{IpcRequest, IpcResponse};

const DEFAULT_HOST: &str = "127.0.0.1";
const DEFAULT_PORT: u16 = 9527;
const DEFAULT_TIMEOUT: Duration = Duration::from_secs(5);

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
        let stream = match self.stream.take() {
            Some(s) => s,
            None => return Err("未连接".to_string()),
        };

        let mut stream = stream;
        let data = request.serialize();
        if let Err(e) = stream.write_all(&data).await {
            // Stream may be broken; discard and let caller re-connect
            return Err(format!("发送失败: {}", e));
        }

        let mut reader = BufReader::new(stream);
        let mut buffer = Vec::new();

        match tokio::time::timeout(DEFAULT_TIMEOUT, reader.read_until(b'\n', &mut buffer)).await {
            Ok(Ok(0)) => Err("连接已断开".to_string()),
            Ok(Ok(_)) => {
                let stream = reader.into_inner();
                self.stream = Some(stream);
                match IpcResponse::deserialize(&buffer) {
                    Some(resp) => Ok(resp),
                    None => Err("无效响应格式".to_string()),
                }
            }
            Ok(Err(e)) => {
                Err(format!("读取失败: {}", e))
            }
            Err(_) => {
                // Timeout: stream is consumed by BufReader and dropped.
                // Clear self.stream so the caller can detect disconnection and re-connect.
                Err("请求超时".to_string())
            }
        }
    }

    pub async fn start_pipeline(&mut self) -> Result<IpcResponse, String> {
        self.send_request(&IpcRequest::new(super::protocol::CMD_START)).await
    }

    pub async fn pause_pipeline(&mut self) -> Result<IpcResponse, String> {
        self.send_request(&IpcRequest::new(super::protocol::CMD_PAUSE)).await
    }

    pub async fn full_quit(&mut self) -> Result<IpcResponse, String> {
        self.send_request(&IpcRequest::new(super::protocol::CMD_STOP)).await
    }

    pub async fn check_system(&mut self) -> Result<IpcResponse, String> {
        self.send_request(&IpcRequest::new(super::protocol::CMD_CHECK)).await
    }

    pub async fn get_all_status(&mut self) -> Result<IpcResponse, String> {
        self.send_request(&IpcRequest::new(super::protocol::CMD_GET_ALL_STATUS)).await
    }

    pub async fn get_statistics(&mut self) -> Result<IpcResponse, String> {
        self.send_request(&IpcRequest::new(super::protocol::CMD_GET_STATISTICS)).await
    }

    pub async fn get_engine_status(&mut self) -> Result<IpcResponse, String> {
        self.send_request(&IpcRequest::new(super::protocol::CMD_GET_ENGINE_STATUS)).await
    }

    pub async fn reset_step(&mut self, config_name: serde_json::Value, step_name: Option<&str>) -> Result<IpcResponse, String> {
        let mut params = serde_json::Map::new();
        params.insert("config_name".to_string(), config_name);
        if let Some(sn) = step_name {
            params.insert("step_name".to_string(), serde_json::Value::String(sn.to_string()));
        }
        self.send_request(&IpcRequest::with_params(
            super::protocol::CMD_RESET_STEP,
            serde_json::Value::Object(params),
        )).await
    }

    pub async fn clean_step(&mut self, step_name: &str, config_name: Option<serde_json::Value>) -> Result<IpcResponse, String> {
        let mut params = serde_json::Map::new();
        params.insert("step_name".to_string(), serde_json::Value::String(step_name.to_string()));
        if let Some(cn) = config_name {
            params.insert("config_name".to_string(), cn);
        }
        self.send_request(&IpcRequest::with_params(
            super::protocol::CMD_CLEAN_STEP,
            serde_json::Value::Object(params),
        )).await
    }

    pub async fn get_log_entries(
        &mut self,
        since_id: u64,
        limit: u64,
        level_filter: Option<&str>,
        source_filter: Option<&str>,
    ) -> Result<IpcResponse, String> {
        let mut params = serde_json::Map::new();
        params.insert("since_id".to_string(), serde_json::Value::Number(since_id.into()));
        params.insert("limit".to_string(), serde_json::Value::Number(limit.into()));
        if let Some(lf) = level_filter {
            params.insert("level_filter".to_string(), serde_json::Value::String(lf.to_string()));
        }
        if let Some(sf) = source_filter {
            params.insert("source_filter".to_string(), serde_json::Value::String(sf.to_string()));
        }
        self.send_request(&IpcRequest::with_params(
            super::protocol::CMD_GET_LOG_ENTRIES,
            serde_json::Value::Object(params),
        )).await
    }
}
