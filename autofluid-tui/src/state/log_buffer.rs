use std::collections::VecDeque;

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
}

impl LogEntry {
    pub fn from_dict(data: &serde_json::Value) -> Option<Self> {
        let obj = data.as_object()?;
        Some(Self {
            id: obj.get("id")?.as_u64()?,
            timestamp: obj.get("timestamp").and_then(|v| v.as_str()).unwrap_or("").to_string(),
            level: obj.get("level").and_then(|v| v.as_str()).unwrap_or("INFO").to_string(),
            source: obj.get("source").and_then(|v| v.as_str()).unwrap_or("system").to_string(),
            logger_name: obj.get("logger_name").and_then(|v| v.as_str()).unwrap_or("").to_string(),
            message: obj.get("message").and_then(|v| v.as_str()).unwrap_or("").to_string(),
            raw_message: obj.get("raw_message").and_then(|v| v.as_str()).unwrap_or("").to_string(),
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

    pub fn source_icon(&self) -> &str {
        match self.source.as_str() {
            "local_ps" => "💻",
            "remote_ps" => "🌐",
            "com" => "🔧",
            "scheduler" => "⚙️",
            "system" => "📡",
            "ipc" => "🔌",
            _ => "📌",
        }
    }
}

#[derive(Debug, Clone)]
pub struct LogBuffer {
    pub detail_buffer: VecDeque<LogEntry>,
    pub info_messages: VecDeque<String>,
}

impl LogBuffer {
    pub fn new() -> Self {
        Self {
            detail_buffer: VecDeque::with_capacity(MAX_DETAIL_BUFFER),
            info_messages: VecDeque::with_capacity(MAX_INFO_BUFFER),
        }
    }

    pub fn push_detail(&mut self, entry: LogEntry) {
        if self.detail_buffer.len() >= MAX_DETAIL_BUFFER {
            self.detail_buffer.pop_front();
        }
        self.detail_buffer.push_back(entry);
    }

    pub fn push_info(&mut self, message: String) {
        if self.info_messages.len() >= MAX_INFO_BUFFER {
            self.info_messages.pop_front();
        }
        self.info_messages.push_back(message);
    }

    pub fn filtered_entries<'a>(
        &'a self,
        level_filter: &'a Option<String>,
        source_filter: &'a Option<String>,
    ) -> impl Iterator<Item = &'a LogEntry> {
        self.detail_buffer.iter().filter(move |entry| {
            if let Some(lf) = level_filter {
                if entry.level != *lf {
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
            .map(|e| {
                let logger = if e.logger_name.is_empty() {
                    String::new()
                } else {
                    format!(" [{}]", e.logger_name)
                };
                let timestamp = if e.timestamp.is_empty() {
                    String::new()
                } else {
                    format!("[{}] ", e.timestamp)
                };
                format!(
                    "{}[{}][{}]{} {}",
                    timestamp,
                    e.level,
                    e.source,
                    logger,
                    e.message
                )
            })
            .collect()
    }
}
