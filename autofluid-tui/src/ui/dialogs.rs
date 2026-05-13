use ratatui::Frame;
use ratatui::layout::{Alignment, Constraint, Direction, Layout, Rect};
use ratatui::style::{Color, Modifier, Style};
use ratatui::text::{Line, Span};
use ratatui::widgets::{Block, Borders, Clear, Paragraph};

use crate::ui::scrollbar::VerticalScrollbar;

const BTN_NORMAL: Style = Style::new()
    .fg(Color::Rgb(224, 224, 224))
    .bg(Color::Rgb(15, 52, 96));
const BTN_HOVER: Style = Style::new()
    .fg(Color::Rgb(255, 255, 255))
    .bg(Color::Rgb(233, 69, 96))
    .add_modifier(Modifier::BOLD);
const BTN_CLICKED: Style = Style::new()
    .fg(Color::Rgb(0, 0, 0))
    .bg(Color::Rgb(255, 255, 255))
    .add_modifier(Modifier::BOLD);

const DIALOG_BG: Color = Color::Rgb(22, 33, 62);
const ACCENT_RED: Color = Color::Rgb(233, 69, 96);
const GREEN: Color = Color::Rgb(0, 255, 136);
const FIELD_LABEL: Color = Color::Rgb(200, 200, 200);
const FIELD_VALUE: Color = Color::Rgb(180, 180, 180);
const SECTION_HEADER: Color = Color::Rgb(0, 255, 136);
const HINT_COLOR: Color = Color::Rgb(80, 80, 80);
const ERROR_COLOR: Color = Color::Rgb(255, 69, 58);

/// 清除对话框背景并处理 forbidden render margin：
/// 主体区域精确清除，左侧 1 格禁止宽字符渲染，防止其右半身侵入边框
pub fn clear_dialog_background(frame: &mut Frame, dialog_area: Rect) {
    // 主体区域精确清除（与弹窗区域一致，无扩展）
    frame.render_widget(Clear, dialog_area);

    // Forbidden render margin：弹窗左侧 1 格中的宽字符（中文/emoji）
    // 起始位置在此列时会跨越侵入弹窗边框，需禁止渲染
    if dialog_area.x > 0 {
        let fx = dialog_area.x - 1;
        let buffer = frame.buffer_mut();
        let max_y = (dialog_area.y + dialog_area.height).min(buffer.area().height);
        for y in dialog_area.y..max_y {
            if let Some(cell) = buffer.cell_mut((fx, y)) {
                let ch = cell.symbol().chars().next().unwrap_or(' ');
                if unicode_width::UnicodeWidthChar::width(ch).unwrap_or(0) > 1 {
                    cell.set_char(' '); // 仅清除文字，保留原面板背景色
                }
            }
        }
    }
}

fn button_style(idx: u8, hovered: Option<u8>, clicked: Option<u8>) -> Style {
    if clicked == Some(idx) {
        BTN_CLICKED
    } else if hovered == Some(idx) {
        BTN_HOVER
    } else {
        BTN_NORMAL
    }
}

/// 对话框渲染信息
///
/// - content_total_lines: 内容总行数
/// - content_visible_lines: 可见行数
/// - scrollbar_area: 滚动条区域
/// - button_bar_y: 按钮栏Y坐标
pub struct DialogRenderInfo {
    pub content_total_lines: usize,
    pub content_visible_lines: usize,
    pub scrollbar_area: Rect,
    pub button_bar_y: u16,
}

pub fn render_confirm_dialog(
    frame: &mut Frame,
    area: Rect,
    message: &str,
    scroll: u16,
    hovered_dialog_button: Option<u8>,
    clicked_dialog_button: Option<u8>,
) -> DialogRenderInfo {
    let dialog_area = centered_rect(80, 40, area);
    clear_dialog_background(frame, dialog_area);

    let block = Block::default()
        .borders(Borders::ALL)
        .border_style(Style::default().fg(Color::Rgb(233, 69, 96)))
        .style(Style::default().bg(DIALOG_BG));
    frame.render_widget(block, dialog_area);

    let inner = Rect {
        x: dialog_area.x + 1,
        y: dialog_area.y + 1,
        width: dialog_area.width.saturating_sub(2),
        height: dialog_area.height.saturating_sub(2),
    };

    let btn_bar_height: u16 = 2;
    let content_height = inner.height.saturating_sub(btn_bar_height);

    let content_area = Rect {
        x: inner.x,
        y: inner.y,
        width: inner.width.saturating_sub(1),
        height: content_height,
    };

    let scrollbar_area = Rect {
        x: inner.x + inner.width.saturating_sub(1),
        y: inner.y,
        width: 1,
        height: content_height,
    };

    let msg_lines: Vec<Line> = message
        .lines()
        .map(|line| {
            Line::from(Span::styled(
                line.to_string(),
                Style::default().fg(Color::Rgb(255, 170, 0)).add_modifier(Modifier::BOLD),
            ))
        })
        .collect();

    let msg_line_count = msg_lines.len();
    let visible = content_height as usize;

    let (content_lines, total_lines, scroll_offset) = if msg_line_count <= visible {
        let top_pad = visible.saturating_sub(msg_line_count) / 2;
        let mut lines: Vec<Line> = Vec::new();
        for _ in 0..top_pad {
            lines.push(Line::from(""));
        }
        lines.extend(msg_lines);
        let total = lines.len();
        (lines, total, 0usize)
    } else {
        let max_scroll = msg_line_count - visible;
        let so = (scroll as usize).min(max_scroll);
        (msg_lines, msg_line_count, so)
    };

    let paragraph = Paragraph::new(content_lines)
        .alignment(Alignment::Center)
        .scroll((scroll_offset as u16, 0));
    frame.render_widget(paragraph, content_area);

    if total_lines > visible {
        let sb = VerticalScrollbar {
            total: total_lines,
            visible,
            scroll: scroll_offset,
        };
        frame.render_widget(sb, scrollbar_area);
    }

    let separator_y = inner.y + content_height;
    let separator: String = "─".repeat(inner.width as usize);
    frame.render_widget(
        Paragraph::new(Span::styled(separator, Style::default().fg(Color::Rgb(60, 60, 60)))),
        Rect { x: inner.x, y: separator_y, width: inner.width, height: 1 },
    );

    let btn_y = inner.y + content_height + 1;
    let confirm_label = " 确认 [Y] ";
    let cancel_label = " 取消 [N] ";
    let gap: u16 = 3;
    let confirm_style = button_style(0, hovered_dialog_button, clicked_dialog_button);
    let cancel_style = button_style(1, hovered_dialog_button, clicked_dialog_button);

    let btn_line = Line::from(vec![
        Span::styled(confirm_label.to_string(), confirm_style),
        Span::styled(" ".repeat(gap as usize), Style::default().bg(DIALOG_BG)),
        Span::styled(cancel_label.to_string(), cancel_style),
    ]);

    frame.render_widget(
        Paragraph::new(btn_line).alignment(Alignment::Center),
        Rect { x: inner.x, y: btn_y, width: inner.width, height: 1 },
    );

    DialogRenderInfo {
        content_total_lines: total_lines,
        content_visible_lines: visible,
        scrollbar_area,
        button_bar_y: btn_y,
    }
}

fn build_check_content_lines(data: &serde_json::Value, _content_width: usize) -> Vec<Line<'static>> {
    let label_width: u16 = 20;

    struct CheckItem {
        label: &'static str,
        value: String,
        exists: Option<bool>,
    }

    let mut raw_lines: Vec<Line> = Vec::new();

    // ---- 本地环境检查 ----
    let local_header = "─── 本地环境检查 ──";
    let remote_header = "─── 远程工作站检查 ──";
    let target_header_w = {
        let w1 = unicode_width::UnicodeWidthStr::width(local_header);
        let w2 = unicode_width::UnicodeWidthStr::width(remote_header);
        w1.max(w2) + 2
    };

    let local_items: Vec<CheckItem> = if let Some(local) = data.get("local_checks").and_then(|v| v.as_object()) {
        let keys_in_order = [
            "SW可执行文件", "SW模型文件", "Excel参数表", "STEP输出目录",
            "SC可执行文件", "SC脚本文件", "SCDOC输出目录", "日志目录", "数据目录",
        ];
        keys_in_order.iter().filter_map(|name| {
            local.get(*name).map(|info| {
                let exists = info.get("exists").and_then(|v| v.as_bool());
                let path = info.get("path").and_then(|v| v.as_str()).unwrap_or("").to_string();
                let value = if path.is_empty() { "(未设置)".to_string() } else { path.clone() };
                CheckItem {
                    label: name,
                    value,
                    exists,
                }
            })
        }).collect()
    } else {
        Vec::new()
    };

    // ---- 远程工作站检查 ----
    let mut remote_items: Vec<CheckItem> = Vec::new();
    if let Some(remote) = data.get("remote_checks").and_then(|v| v.as_object()) {
        if let Some(ssh_status) = remote.get("ssh").and_then(|v| v.as_str()) {
            let ok = ssh_status.contains("成功");
            remote_items.push(CheckItem {
                label: "SSH连接",
                value: ssh_status.to_string(),
                exists: Some(ok),
            });
        }
        if let Some(conda) = remote.get("conda_available").and_then(|v| v.as_bool()) {
            remote_items.push(CheckItem {
                label: "Conda环境",
                value: if conda { "可用".to_string() } else { "不可用".to_string() },
                exists: Some(conda),
            });
        }
        if let Some(py_ver) = remote.get("python_version").and_then(|v| v.as_str()) {
            let ok = !py_ver.is_empty();
            remote_items.push(CheckItem {
                label: "Python版本",
                value: if ok { py_ver.to_string() } else { "未安装或无法检测".to_string() },
                exists: Some(ok),
            });
        }
        if let Some(disk) = remote.get("disk_space").and_then(|v| v.as_str()) {
            remote_items.push(CheckItem {
                label: "磁盘空间",
                value: disk.to_string(),
                exists: None,
            });
        }
        if let Some(procs) = remote.get("background_processes").and_then(|v| v.as_array()) {
            let proc_list: Vec<&str> = procs.iter().map(|v| v.as_str().unwrap_or("?")).collect();
            let display = if proc_list.is_empty() { "无".to_string() } else { proc_list.join(", ") };
            remote_items.push(CheckItem {
                label: "后台进程",
                value: display,
                exists: None,
            });
        }
    }

    fn render_section(raw_lines: &mut Vec<Line>, header: &str, target_header_w: usize, items: &[CheckItem], label_width: u16) {
        let header_dw = unicode_width::UnicodeWidthStr::width(header);
        let header_pad = if header_dw < target_header_w {
            "─".repeat(target_header_w - header_dw)
        } else {
            String::new()
        };

        raw_lines.push(Line::from(""));
        raw_lines.push(Line::from(Span::styled(
            format!("  {}{}", header, header_pad),
            Style::default().fg(SECTION_HEADER).add_modifier(Modifier::BOLD),
        )));
        raw_lines.push(Line::from(""));

        for item in items {
            let icon = match item.exists {
                Some(true) => (" ✅", GREEN),
                Some(false) => (" ❌", ERROR_COLOR),
                None => ("", DIALOG_BG),
            };

            let mut spans = vec![
                Span::styled("  ", Style::default().bg(DIALOG_BG)),
                Span::styled(
                    pad_label_by_display_width(item.label, label_width),
                    Style::default().fg(FIELD_LABEL).bg(DIALOG_BG),
                ),
                Span::styled(
                    truncate_for_display(&item.value, 50),
                    Style::default().fg(FIELD_VALUE).bg(DIALOG_BG),
                ),
            ];

            if !icon.0.is_empty() {
                spans.push(Span::styled(icon.0, Style::default().fg(icon.1).bg(DIALOG_BG)));
            }

            raw_lines.push(Line::from(spans));
        }
    }

    if !local_items.is_empty() {
        render_section(&mut raw_lines, local_header, target_header_w, &local_items, label_width);
    }
    if !remote_items.is_empty() {
        render_section(&mut raw_lines, remote_header, target_header_w, &remote_items, label_width);
    }

    raw_lines
}

fn pad_label_by_display_width(label: &str, target_width: u16) -> String {
    let dw = unicode_width::UnicodeWidthStr::width(label);
    let pad = (target_width as usize).saturating_sub(dw);
    format!("{}{}", label, " ".repeat(pad))
}

fn truncate_for_display(s: &str, max_width: usize) -> String {
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

pub fn render_check_result(
    frame: &mut Frame,
    area: Rect,
    data: &serde_json::Value,
    scroll: u16,
    hovered_dialog_button: Option<u8>,
    clicked_dialog_button: Option<u8>,
) -> DialogRenderInfo {
    let dialog_area = centered_rect(90, 90, area);
    clear_dialog_background(frame, dialog_area);

    let block = Block::default()
        .borders(Borders::ALL)
        .border_style(Style::default().fg(ACCENT_RED))
        .style(Style::default().bg(DIALOG_BG));
    frame.render_widget(block, dialog_area);

    let inner = Rect {
        x: dialog_area.x + 1,
        y: dialog_area.y + 1,
        width: dialog_area.width.saturating_sub(2),
        height: dialog_area.height.saturating_sub(2),
    };

    // Title bar: title left-aligned, hint right-aligned
    let title_text = "系统自检结果 (System Check)";
    let title_w = unicode_width::UnicodeWidthStr::width(title_text) as u16 + 2;

    let title_row = Rect { x: inner.x, y: inner.y, width: inner.width, height: 1 };
    let title_chunks = Layout::default()
        .direction(Direction::Horizontal)
        .constraints([
            Constraint::Length(title_w.min(inner.width)),
            Constraint::Min(1),
        ])
        .split(title_row);

    frame.render_widget(
        Paragraph::new(Line::from(Span::styled(
            format!(" {}", title_text),
            Style::default().fg(Color::White).add_modifier(Modifier::BOLD),
        ))),
        title_chunks[0],
    );

    frame.render_widget(
        Paragraph::new(Line::from(Span::styled(
            "↑↓滚轮 Esc关闭",
            Style::default().fg(HINT_COLOR),
        )))
        .alignment(Alignment::Right),
        title_chunks[1],
    );

    // Separator after title
    let sep_style = Style::default().fg(Color::Rgb(60, 60, 60));
    let sep = "─".repeat(inner.width as usize);
    let sep_y = inner.y + 1;
    frame.render_widget(
        Paragraph::new(Span::styled(&sep, sep_style)),
        Rect { x: inner.x, y: sep_y, width: inner.width, height: 1 },
    );

    // Layout: top(2 rows) + content + bottom(2 rows = separator + button)
    let top_height: u16 = 2;
    let bottom_height: u16 = 2;
    let content_top = inner.y + top_height;
    let content_height = inner.height.saturating_sub(top_height).saturating_sub(bottom_height);

    let content_area = Rect {
        x: inner.x,
        y: content_top,
        width: inner.width.saturating_sub(1),
        height: content_height,
    };

    let scrollbar_area = Rect {
        x: inner.x + inner.width.saturating_sub(1),
        y: content_top,
        width: 1,
        height: content_height,
    };

    let content_lines = build_check_content_lines(data, content_area.width as usize);
    let total_lines = content_lines.len();
    let visible = content_height as usize;
    let max_scroll = total_lines.saturating_sub(visible);
    let scroll_offset = (scroll as usize).min(max_scroll);

    let paragraph = Paragraph::new(content_lines)
        .alignment(Alignment::Left)
        .scroll((scroll_offset as u16, 0));
    frame.render_widget(paragraph, content_area);

    if total_lines > visible {
        let sb = VerticalScrollbar {
            total: total_lines,
            visible,
            scroll: scroll_offset,
        };
        frame.render_widget(sb, scrollbar_area);
    }

    // Bottom separator
    let sep2_y = content_top + content_height;
    frame.render_widget(
        Paragraph::new(Span::styled(&sep, sep_style)),
        Rect { x: inner.x, y: sep2_y, width: inner.width, height: 1 },
    );

    // Button bar — btn_y = inner.y + inner.height - 1
    let btn_y = inner.y + inner.height - 1;
    let close_label = " 关闭 (Esc) ";
    let close_style = button_style(0, hovered_dialog_button, clicked_dialog_button);

    let btn_line = Line::from(vec![Span::styled(close_label.to_string(), close_style)]);

    frame.render_widget(
        Paragraph::new(btn_line).alignment(Alignment::Center),
        Rect { x: inner.x, y: btn_y, width: inner.width, height: 1 },
    );

    DialogRenderInfo {
        content_total_lines: total_lines,
        content_visible_lines: visible,
        scrollbar_area,
        button_bar_y: btn_y,
    }
}

pub fn centered_rect(percent_x: u16, percent_y: u16, r: Rect) -> Rect {
    let popup_layout = Layout::default()
        .direction(Direction::Vertical)
        .constraints([
            Constraint::Percentage((100 - percent_y) / 2),
            Constraint::Percentage(percent_y),
            Constraint::Percentage((100 - percent_y) / 2),
        ])
        .split(r);

    Layout::default()
        .direction(Direction::Horizontal)
        .constraints([
            Constraint::Percentage((100 - percent_x) / 2),
            Constraint::Percentage(percent_x),
            Constraint::Percentage((100 - percent_x) / 2),
        ])
        .split(popup_layout[1])[1]
}
