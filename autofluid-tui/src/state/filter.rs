#[derive(Debug, Clone)]
pub enum FilterType {
    Level(String),
    Source(String),
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
