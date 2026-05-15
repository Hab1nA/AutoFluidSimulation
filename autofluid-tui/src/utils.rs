/// 将字符索引转换为字节索引（UTF-8 安全）。
///
/// 用于在 String 中按字符位置插入/删除字符时，
/// 将人类可读的字符偏移量转换为 Rust String 所需的字节偏移量。
/// 若 char_idx 超出范围，返回字符串的字节长度。
pub fn char_to_byte_index(s: &str, char_idx: usize) -> usize {
    s.char_indices()
        .nth(char_idx)
        .map(|(i, _)| i)
        .unwrap_or(s.len())
}

/// 按显示宽度截断字符串，超出部分以 "..." 替代。
///
/// 使用 unicode-width 计算实际显示宽度（中文占 2 列，ASCII 占 1 列）。
pub fn truncate_for_display(s: &str, max_width: usize) -> String {
    if max_width == 0 {
        return String::new();
    }
    let width = unicode_width::UnicodeWidthStr::width(s);
    if width <= max_width {
        return s.to_string();
    }
    let mut result = String::new();
    let mut current_width = 0;
    for c in s.chars() {
        let cw = unicode_width::UnicodeWidthChar::width(c).unwrap_or(0);
        if current_width + cw + 3 > max_width {
            result.push_str("...");
            break;
        }
        result.push(c);
        current_width += cw;
    }
    if result.is_empty() {
        result = s.chars().take(3).collect();
        result.push_str("...");
    }
    result
}

/// 按显示宽度用空格填充标签至目标宽度。
pub fn pad_label_by_display_width(label: &str, target_width: u16) -> String {
    let dw = unicode_width::UnicodeWidthStr::width(label);
    let pad = (target_width as usize).saturating_sub(dw);
    format!("{}{}", label, " ".repeat(pad))
}

/// 格式化本地时间（通过 Windows API `GetLocalTime`）。
///
/// 支持的占位符：%Y（年）、%m（月）、%d（日）、%H（时）、%M（分）、%S（秒）。
/// 替代 chrono crate 以减少依赖。
pub fn format_local_time(fmt: &str) -> String {
    let mut st: windows_sys::Win32::Foundation::SYSTEMTIME = unsafe { std::mem::zeroed() };
    unsafe { windows_sys::Win32::System::SystemInformation::GetLocalTime(&mut st) };
    fmt.replace("%Y", &format!("{:04}", st.wYear))
        .replace("%m", &format!("{:02}", st.wMonth))
        .replace("%d", &format!("{:02}", st.wDay))
        .replace("%H", &format!("{:02}", st.wHour))
        .replace("%M", &format!("{:02}", st.wMinute))
        .replace("%S", &format!("{:02}", st.wSecond))
}
