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

    if let Some((_, value)) = find_unique_prefix_match(&lower, &level_map) {
        return Some(FilterType::Level((*value).to_string()));
    }

    if let Some((_, value)) = find_unique_prefix_match(&lower, &source_map) {
        return Some(FilterType::Source((*value).to_string()));
    }

    None
}

fn find_unique_prefix_match<'a>(
    input: &str,
    candidates: &'a [(&str, &str)],
) -> Option<&'a (&'a str, &'a str)> {
    let mut matches = candidates.iter().filter(|(key, _)| key.starts_with(input));
    let first = matches.next()?;
    if matches.next().is_none() {
        Some(first)
    } else {
        None
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parse_filter_arg_accepts_level_prefixes() {
        match parse_filter_arg("warn") {
            Some(FilterType::Level(level)) => assert_eq!(level, "WARNING"),
            other => panic!("expected warning level, got {other:?}"),
        }

        match parse_filter_arg("err") {
            Some(FilterType::Level(level)) => assert_eq!(level, "ERROR"),
            other => panic!("expected error level, got {other:?}"),
        }
    }

    #[test]
    fn parse_filter_arg_accepts_source_prefixes() {
        match parse_filter_arg("sched") {
            Some(FilterType::Source(source)) => assert_eq!(source, "scheduler"),
            other => panic!("expected scheduler source, got {other:?}"),
        }
    }
}
