use std::collections::VecDeque;

use super::filter::severity_rank;

const MAX_DETAIL_BUFFER: usize = 2000;
const MAX_INFO_BUFFER: usize = 200;

#[derive(Debug, Clone)]
pub struct LogEntry {
    pub id: u64,
    pub level: String,
    pub source: String,
    pub message: String,
    pub raw_message: String,
}

impl LogEntry {
    pub fn from_dict(data: &serde_json::Value) -> Option<Self> {
        let obj = data.as_object()?;
        Some(Self {
            id: obj.get("id")?.as_u64()?,
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
            message: obj
                .get("message")
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .to_string(),
            raw_message: obj
                .get("raw_message")
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .to_string(),
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
            .map(|e| e.message.clone())
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
    let trimmed = message.trim_start();
    // ✅ 成功、❌ 错误、⚠ 警告、⏳ 等待中、⏸ 暂停
    if trimmed.starts_with('❌') || trimmed.contains("失败") || trimmed.contains("错误") {
        log::error!("[TUI] 高级信息: {message}");
    } else if trimmed.starts_with('⚠') || trimmed.contains("警告") || trimmed.contains("超时")
    {
        log::warn!("[TUI] 高级信息: {message}");
    } else {
        log::info!("[TUI] 高级信息: {message}");
    }
}
