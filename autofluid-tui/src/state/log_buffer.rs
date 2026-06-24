use std::collections::VecDeque;

use super::filter::severity_rank;

const MAX_DETAIL_BUFFER: usize = 2000;
const MAX_INFO_BUFFER: usize = 200;

#[derive(Debug, Clone)]
pub struct LogEntry {
    pub id: u64,
    pub timestamp: String,
    pub level: String,
    pub source: String,
    pub logger_name: String,
    pub message: String,
    pub raw_message: String,
    pub category: String,
    pub config_name: Option<String>,
    pub step_name: Option<String>,
    pub worker_id: Option<String>,
    pub is_polling: bool,
}

impl LogEntry {
    pub fn from_dict(data: &serde_json::Value) -> Option<Self> {
        let obj = data.as_object()?;
        Some(Self {
            id: obj.get("id")?.as_u64()?,
            timestamp: obj
                .get("timestamp")
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .to_string(),
            level: obj
                .get("level")
                .and_then(|v| v.as_str())
                .unwrap_or("INFO")
                .to_string(),
            source: obj
                .get("source")
                .and_then(|v| v.as_str())
                .unwrap_or("system")
                .to_string(),
            logger_name: obj
                .get("logger_name")
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .to_string(),
            message: obj
                .get("message")
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .to_string(),
            raw_message: obj
                .get("raw_message")
                .and_then(|v| v.as_str())
                .or_else(|| obj.get("message").and_then(|v| v.as_str()))
                .unwrap_or("")
                .to_string(),
            category: obj
                .get("category")
                .and_then(|v| v.as_str())
                .unwrap_or("general")
                .to_string(),
            config_name: optional_string_field(obj, "config_name"),
            step_name: optional_string_field(obj, "step_name"),
            worker_id: optional_string_field(obj, "worker_id"),
            is_polling: obj
                .get("is_polling")
                .and_then(|v| v.as_bool())
                .unwrap_or(false),
        })
    }

    pub fn level_color(&self) -> ratatui::style::Color {
        match self.level.as_str() {
            "DEBUG" => ratatui::style::Color::DarkGray,
            "INFO" => ratatui::style::Color::Cyan,
            "WARNING" => ratatui::style::Color::Yellow,
            "ERROR" => ratatui::style::Color::Red,
            "CRITICAL" => ratatui::style::Color::Magenta,
            _ => ratatui::style::Color::White,
        }
    }

    pub fn detail_prefix(&self) -> String {
        format!("[{}] ", self.level)
    }

    pub fn display_message(&self) -> String {
        let base = if self.raw_message.is_empty() && !self.logger_name.is_empty() {
            format!("[{}] {}", self.logger_name, self.message)
        } else if self.raw_message.is_empty() {
            self.message.clone()
        } else {
            self.raw_message.clone()
        };
        let mut context = Vec::new();
        if self.category != "general" {
            context.push(format!("category={}", self.category));
        }
        if let Some(config_name) = &self.config_name {
            context.push(format!("config={config_name}"));
        }
        if let Some(step_name) = &self.step_name {
            context.push(format!("step={step_name}"));
        }
        if let Some(worker_id) = &self.worker_id {
            context.push(format!("worker={worker_id}"));
        }
        if self.is_polling {
            context.push("polling=true".to_string());
        }
        if context.is_empty() {
            base
        } else {
            format!("{} ({})", base, context.join(", "))
        }
    }

    pub fn export_message(&self) -> String {
        if !self.message.is_empty() {
            return self.message.clone();
        }

        let message = self.display_message();
        if self.timestamp.is_empty() {
            message
        } else {
            format!("[{}] [{}] {}", self.timestamp, self.level, message)
        }
    }
}

fn optional_string_field(
    obj: &serde_json::Map<String, serde_json::Value>,
    key: &str,
) -> Option<String> {
    obj.get(key)
        .and_then(|v| v.as_str())
        .filter(|value| !value.is_empty())
        .map(str::to_string)
}

#[derive(Debug, Clone)]
pub struct LogBuffer {
    pub detail_buffer: VecDeque<LogEntry>,
    pub info_messages: VecDeque<String>,
    pub log_generation: u64,
}

impl LogBuffer {
    pub fn new() -> Self {
        Self {
            detail_buffer: VecDeque::with_capacity(MAX_DETAIL_BUFFER),
            info_messages: VecDeque::with_capacity(MAX_INFO_BUFFER),
            log_generation: 0,
        }
    }

    pub fn push_detail(&mut self, entry: LogEntry) {
        if self.detail_buffer.len() >= MAX_DETAIL_BUFFER {
            self.detail_buffer.pop_front();
        }
        self.detail_buffer.push_back(entry);
        self.log_generation += 1;
    }

    /// 清空详细日志缓冲区（daemon 重启时调用，避免新旧日志混淆）
    pub fn clear_detail(&mut self) {
        self.detail_buffer.clear();
        self.log_generation += 1;
    }

    pub fn push_info(&mut self, message: String) {
        log_info_message(&message);
        if self.info_messages.len() >= MAX_INFO_BUFFER {
            self.info_messages.pop_front();
        }
        self.info_messages.push_back(message);
        self.log_generation += 1;
    }

    pub fn filtered_entries<'a>(
        &'a self,
        level_filter: &'a Option<String>,
        source_filter: &'a Option<String>,
    ) -> impl Iterator<Item = &'a LogEntry> {
        self.detail_buffer.iter().filter(move |entry| {
            if let Some(lf) = level_filter {
                if severity_rank(&entry.level) < severity_rank(lf.as_str()) {
                    return false;
                }
            }
            if let Some(sf) = source_filter {
                if entry.source != *sf {
                    return false;
                }
            }
            true
        })
    }

    pub fn export_lines(
        &self,
        level_filter: &Option<String>,
        source_filter: &Option<String>,
    ) -> Vec<String> {
        self.filtered_entries(level_filter, source_filter)
            .map(LogEntry::export_message)
            .collect()
    }

    pub fn export_info_lines(&self) -> Vec<String> {
        self.info_messages.iter().cloned().collect()
    }

    pub fn export_all_lines(
        &self,
        level_filter: &Option<String>,
        source_filter: &Option<String>,
    ) -> Vec<String> {
        let mut lines = Vec::new();
        if !self.info_messages.is_empty() {
            lines.push("=== TUI 高级信息 ===".to_string());
            lines.extend(self.export_info_lines());
        }

        let detail_lines = self.export_lines(level_filter, source_filter);
        if !detail_lines.is_empty() {
            if !lines.is_empty() {
                lines.push(String::new());
            }
            lines.push("=== Daemon 详细日志 ===".to_string());
            lines.extend(detail_lines);
        }
        lines
    }
}

fn log_info_message(message: &str) {
    match info_message_level(message) {
        log::Level::Error => log::error!("{message}"),
        log::Level::Warn => log::warn!("{message}"),
        _ => log::info!("{message}"),
    }
}

fn info_message_level(message: &str) -> log::Level {
    let trimmed = message.trim_start();
    if trimmed.starts_with('❌') {
        log::Level::Error
    } else if trimmed.starts_with('⚠') || trimmed.starts_with('⏸') {
        log::Level::Warn
    } else if trimmed.contains("失败") || trimmed.contains("错误") {
        log::Level::Error
    } else if trimmed.contains("警告") || trimmed.contains("超时") {
        log::Level::Warn
    } else {
        log::Level::Info
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn log_entry_parses_structured_optional_fields() {
        let value = json!({
            "id": 42,
            "timestamp": "2026-06-10 12:34:56",
            "level": "WARNING",
            "source": "scheduler",
            "logger_name": "engine.scheduler.main",
            "message": "[2026-06-10 12:34:56] [WARNING] [engine.scheduler.main] message",
            "raw_message": "[engine.scheduler.main] message",
            "category": "step",
            "config_name": "7",
            "step_name": "Meshing",
            "worker_id": "local-pc-01",
            "is_polling": false
        });

        let entry = LogEntry::from_dict(&value).expect("entry should parse");

        assert_eq!(entry.id, 42);
        assert_eq!(entry.timestamp, "2026-06-10 12:34:56");
        assert_eq!(entry.logger_name, "engine.scheduler.main");
        assert_eq!(entry.category, "step");
        assert_eq!(entry.config_name.as_deref(), Some("7"));
        assert_eq!(entry.step_name.as_deref(), Some("Meshing"));
        assert_eq!(entry.worker_id.as_deref(), Some("local-pc-01"));
        assert!(!entry.is_polling);
    }

    #[test]
    fn log_buffer_filters_entries_locally() {
        let mut buffer = LogBuffer::new();
        buffer.push_detail(LogEntry {
            id: 1,
            timestamp: "2026-06-10 12:00:00".to_string(),
            level: "INFO".to_string(),
            source: "remote_ps".to_string(),
            logger_name: "utils.ssh_client".to_string(),
            message: "remote info".to_string(),
            raw_message: "remote info".to_string(),
            category: "general".to_string(),
            config_name: None,
            step_name: None,
            worker_id: None,
            is_polling: false,
        });
        buffer.push_detail(LogEntry {
            id: 2,
            timestamp: "2026-06-10 12:00:01".to_string(),
            level: "ERROR".to_string(),
            source: "scheduler".to_string(),
            logger_name: "engine.scheduler.main".to_string(),
            message: "scheduler error".to_string(),
            raw_message: "scheduler error".to_string(),
            category: "step".to_string(),
            config_name: Some("3".to_string()),
            step_name: Some("Solver".to_string()),
            worker_id: None,
            is_polling: false,
        });

        let level_filter = Some("WARNING".to_string());
        let source_filter = None;
        let filtered: Vec<_> = buffer
            .filtered_entries(&level_filter, &source_filter)
            .map(|entry| entry.id)
            .collect();

        assert_eq!(filtered, vec![2]);
    }

    #[test]
    fn info_message_level_prefers_warning_emoji_over_error_keywords() {
        assert_eq!(
            info_message_level("⚠️ 启动 SSH 隧道失败: timeout"),
            log::Level::Warn
        );
        assert_eq!(
            info_message_level("⚠ 工作站 SSH 隧道启动失败"),
            log::Level::Warn
        );
        assert_eq!(info_message_level("⏸️ 流水线已暂停"), log::Level::Warn);
        assert_eq!(info_message_level("❌ 后台引擎启动失败"), log::Level::Error);
        assert_eq!(info_message_level("后台引擎启动失败"), log::Level::Error);
        assert_eq!(info_message_level("收到超时警告"), log::Level::Warn);
        assert_eq!(info_message_level("✅ 后台引擎已启动"), log::Level::Info);
    }
}
