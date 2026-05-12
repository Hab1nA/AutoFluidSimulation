use serde::{Deserialize, Serialize};
use serde_json::Value;

use crate::generate_request_id;

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

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct IpcRequest {
    pub command: String,
    #[serde(default)]
    pub params: Value,
    pub request_id: String,
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
        }
    }

    pub fn with_params(command: &str, params: Value) -> Self {
        Self {
            command: command.to_string(),
            params,
            request_id: generate_request_id(),
        }
    }

    pub fn serialize(&self) -> Vec<u8> {
        let mut json = serde_json::to_string(self).unwrap_or_default();
        json.push('\n');
        json.into_bytes()
    }
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
        serde_json::from_str(trimmed).ok()
    }
}
