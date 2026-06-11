use serde::{Deserialize, Serialize};
use serde_json::Value;

use crate::generate_request_id;

/// IPC 命令常量定义。
///
/// ⚠️ 同步要求：这些常量必须与 Python 侧 `ipc/protocol.py` 中的命令常量保持
/// 完全一致。添加新命令时，请同时更新两处定义。
/// 可通过运行 `python -m pytest tests/test_ipc_protocol.py -v -k "command"` 验证。
pub const CMD_START: &str = "start";
pub const CMD_PAUSE: &str = "pause";
pub const CMD_STOP: &str = "stop";
pub const CMD_CHECK: &str = "check";
pub const CMD_RESET_STEP: &str = "reset_step";
pub const CMD_CLEAN_STEP: &str = "clean_step";
pub const CMD_GET_ALL_STATUS: &str = "get_all_status";
pub const CMD_GET_STATISTICS: &str = "get_statistics";
pub const CMD_GET_ENGINE_STATUS: &str = "get_engine_status";
pub const CMD_GET_LOG_ENTRIES: &str = "get_log_entries";
pub const CMD_GET_DASHBOARD: &str = "get_dashboard";
pub const CMD_RELOAD_CONFIG: &str = "reload_config";
#[allow(dead_code)]
pub const CMD_WORKER_REGISTER: &str = "worker_register";
#[allow(dead_code)]
pub const CMD_WORKER_HEARTBEAT: &str = "worker_heartbeat";
#[allow(dead_code)]
pub const CMD_WORKER_POLL: &str = "worker_poll";
#[allow(dead_code)]
pub const CMD_WORKER_STEP_COMPLETE: &str = "worker_step_complete";
#[allow(dead_code)]
pub const CMD_WORKER_STEP_ERROR: &str = "worker_step_error";

// ---- Worker 生命周期管理命令 ----
pub const CMD_WORKER_START: &str = "worker_start";
pub const CMD_WORKER_STOP: &str = "worker_stop";
pub const CMD_WORKER_RESTART: &str = "worker_restart";

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct IpcRequest {
    pub command: String,
    #[serde(default)]
    pub params: Value,
    pub request_id: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub auth_token: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct IpcResponse {
    pub status: String,
    #[serde(default)]
    pub data: Value,
    #[serde(default)]
    pub message: String,
    pub request_id: String,
}

impl IpcRequest {
    pub fn new(command: &str) -> Self {
        Self {
            command: command.to_string(),
            params: Value::Object(serde_json::Map::new()),
            request_id: generate_request_id(),
            auth_token: auth_token_from_env(),
        }
    }

    pub fn with_params(command: &str, params: Value) -> Self {
        Self {
            command: command.to_string(),
            params,
            request_id: generate_request_id(),
            auth_token: auth_token_from_env(),
        }
    }

    pub fn serialize(&self) -> Vec<u8> {
        match serde_json::to_string(self) {
            Ok(mut json) => {
                json.push('\n');
                json.into_bytes()
            }
            Err(e) => {
                // IpcRequest 结构简单且不含自定义序列化逻辑，正常情况不应失败。
                // 保留 fallback 以防御性处理极端情况（如内存不足）。
                log::warn!("[IPC] 序列化失败: {e}");
                let fallback = format!(
                    r#"{{"command":"error","request_id":"","params":{{"error":"内部序列化错误: {e}"}}}}"#
                );
                let mut bytes = fallback.into_bytes();
                bytes.push(b'\n');
                bytes
            }
        }
    }
}

fn auth_token_from_env() -> Option<String> {
    std::env::var("AUTOFLUID_IPC_AUTH_TOKEN")
        .ok()
        .filter(|token| !token.is_empty())
}

impl IpcResponse {
    pub fn is_ok(&self) -> bool {
        self.status == "ok"
    }

    pub fn deserialize(data: &[u8]) -> Option<Self> {
        let text = String::from_utf8_lossy(data);
        let trimmed = text.trim();
        if trimmed.is_empty() {
            return None;
        }
        match serde_json::from_str(trimmed) {
            Ok(msg) => Some(msg),
            Err(e) => {
                log::warn!("[IPC] 反序列化失败: {e}, 原始数据: {trimmed}");
                None
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::{IpcRequest, CMD_GET_DASHBOARD};
    use std::sync::Mutex;

    static ENV_LOCK: Mutex<()> = Mutex::new(());

    #[test]
    fn request_omits_empty_auth_token() {
        let _guard = ENV_LOCK.lock().expect("env lock poisoned");
        std::env::remove_var("AUTOFLUID_IPC_AUTH_TOKEN");

        let request = IpcRequest::new("ping");
        let json = String::from_utf8(request.serialize()).expect("request json");

        assert!(request.auth_token.is_none());
        assert!(!json.contains("auth_token"));
    }

    #[test]
    fn request_includes_auth_token_from_env() {
        let _guard = ENV_LOCK.lock().expect("env lock poisoned");
        std::env::set_var("AUTOFLUID_IPC_AUTH_TOKEN", "secret-token");

        let request = IpcRequest::new("ping");
        let json = String::from_utf8(request.serialize()).expect("request json");

        assert_eq!(request.auth_token.as_deref(), Some("secret-token"));
        assert!(json.contains(r#""auth_token":"secret-token""#));

        std::env::remove_var("AUTOFLUID_IPC_AUTH_TOKEN");
    }

    #[test]
    fn dashboard_command_constant_matches_python_protocol() {
        assert_eq!(CMD_GET_DASHBOARD, "get_dashboard");
    }
}
