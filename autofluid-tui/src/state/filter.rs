#[derive(Debug, Clone)]
pub enum FilterType {
    Level(String),
    Source(String),
}

/// 返回日志级别的严重程度排序值，数值越大越严重。
/// 用于阈值过滤：`filter warning` 显示 WARNING(2) 及以上级别。
pub fn severity_rank(level: &str) -> u8 {
    match level {
        "DEBUG" => 0,
        "INFO" => 1,
        "WARNING" => 2,
        "ERROR" => 3,
        "CRITICAL" => 4,
        _ => 1, // 未知级别按 INFO 处理
    }
}

pub fn parse_filter_arg(arg: &str) -> Option<FilterType> {
    let level_map: [(&str, &str); 5] = [
        ("debug", "DEBUG"),
        ("info", "INFO"),
        ("warning", "WARNING"),
        ("error", "ERROR"),
        ("critical", "CRITICAL"),
    ];
    let source_map: [(&str, &str); 6] = [
        ("remote", "remote_ps"),
        ("local", "local_ps"),
        ("com", "com"),
        ("scheduler", "scheduler"),
        ("system", "system"),
        ("ipc", "ipc"),
    ];

    let lower = arg.to_lowercase();

    for (key, value) in &level_map {
        if lower == *key {
            return Some(FilterType::Level(value.to_string()));
        }
    }

    for (key, value) in &source_map {
        if lower == *key {
            return Some(FilterType::Source(value.to_string()));
        }
    }

    None
}
