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
    frame.render_widget(Clear, dialog_area);

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

fn build_check_content_lines(data: &serde_json::Value) -> Vec<Line<'static>> {
    let left_pad = "   ";

    let local_sections: &[(&str, &[&str])] = &[
        ("[SW 阶段]", &["SW模型", "Excel参数表", "STEP目录"]),
        ("[SC 阶段]", &["SC程序", "SC脚本", "SCDOC目录"]),
        ("[系统]", &["日志目录"]),
    ];

    let mut lines: Vec<Line> = vec![
        Line::from(Span::styled(
            format!("{}系统自检结果", left_pad),
            Style::default().fg(Color::White).add_modifier(Modifier::BOLD),
        )),
        Line::from(Span::styled(
            format!("{}─── 本地检查 ───", left_pad),
            Style::default().fg(Color::White).add_modifier(Modifier::BOLD),
        )),
    ];

    if let Some(local) = data.get("local_checks").and_then(|v| v.as_object()) {
        for (section_label, section_keys) in local_sections {
            lines.push(Line::from(Span::styled(
                format!("{}  {}", left_pad, section_label),
                Style::default()
                    .fg(Color::Rgb(128, 128, 128))
                    .add_modifier(Modifier::BOLD),
            )));
            for name in *section_keys {
                if let Some(info) = local.get(*name) {
                    let exists = info.get("exists").and_then(|v| v.as_bool()).unwrap_or(false);
                    let path = info.get("path").and_then(|v| v.as_str()).unwrap_or("");
                    let icon = if exists { "✅" } else { "❌" };
                    let color = if exists { Color::Green } else { Color::Red };
                    lines.push(Line::from(vec![
                        Span::styled(
                            format!("{}    {} ", left_pad, icon),
                            Style::default().fg(color),
                        ),
                        Span::styled(
                            format!("{}: {}", name, path),
                            Style::default().fg(color),
                        ),
                    ]));
                }
            }
        }
    }

    lines.push(Line::from(""));
    lines.push(Line::from(Span::styled(
        format!("{}─── 远程检查 ───", left_pad),
        Style::default().fg(Color::White).add_modifier(Modifier::BOLD),
    )));

    if let Some(remote) = data.get("remote_checks").and_then(|v| v.as_object()) {
        if let Some(ssh_status) = remote.get("ssh").and_then(|v| v.as_str()) {
            let (icon, color) = if ssh_status.contains("成功") {
                ("✅", Color::Green)
            } else {
                ("❌", Color::Red)
            };
            lines.push(Line::from(vec![
                Span::styled(
                    format!("{}    {} ", left_pad, icon),
                    Style::default().fg(color),
                ),
                Span::styled(
                    format!("SSH连接: {}", ssh_status),
                    Style::default().fg(Color::White),
                ),
            ]));
        }

        if let Some(conda) = remote.get("conda_available").and_then(|v| v.as_bool()) {
            let (icon, status) = if conda { ("✅", "可用") } else { ("❌", "不可用") };
            let conda_color = if conda { Color::Green } else { Color::Red };
            lines.push(Line::from(vec![
                Span::styled(
                    format!("{}    {} ", left_pad, icon),
                    Style::default().fg(conda_color),
                ),
                Span::styled(
                    format!("Conda: {}", status),
                    Style::default().fg(Color::White),
                ),
            ]));
        }

        if let Some(py_ver) = remote.get("python_version").and_then(|v| v.as_str()) {
            let display = if py_ver.is_empty() { "未安装或无法检测" } else { py_ver };
            let (icon, color) = if py_ver.is_empty() { ("❌", Color::Red) } else { ("✅", Color::Green) };
            lines.push(Line::from(vec![
                Span::styled(
                    format!("{}    {} ", left_pad, icon),
                    Style::default().fg(color),
                ),
                Span::styled(
                    format!("Python版本: {}", display),
                    Style::default().fg(Color::White),
                ),
            ]));
        }

        if let Some(disk) = remote.get("disk_space").and_then(|v| v.as_str()) {
            lines.push(Line::from(vec![
                Span::styled(
                    format!("{}    💾 ", left_pad),
                    Style::default().fg(Color::Cyan),
                ),
                Span::styled(
                    format!("磁盘空间: {}", disk),
                    Style::default().fg(Color::White),
                ),
            ]));
        }

        if let Some(procs) = remote.get("background_processes").and_then(|v| v.as_array()) {
            let proc_list: Vec<&str> = procs.iter().map(|v| v.as_str().unwrap_or("?")).collect();
            let display = if proc_list.is_empty() { "无".to_string() } else { proc_list.join(", ") };
            lines.push(Line::from(vec![
                Span::styled(
                    format!("{}    ⚙️  ", left_pad),
                    Style::default().fg(Color::Cyan),
                ),
                Span::styled(
                    format!("后台进程: {}", display),
                    Style::default().fg(Color::White),
                ),
            ]));
        }
    }

    lines
}

pub fn render_check_result(
    frame: &mut Frame,
    area: Rect,
    data: &serde_json::Value,
    scroll: u16,
    hovered_dialog_button: Option<u8>,
    clicked_dialog_button: Option<u8>,
) -> DialogRenderInfo {
    let dialog_area = centered_rect(80, 70, area);
    frame.render_widget(Clear, dialog_area);

    let block = Block::default()
        .borders(Borders::ALL)
        .border_style(Style::default().fg(Color::Rgb(15, 52, 96)))
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

    let content_lines = build_check_content_lines(data);
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

    let separator_y = inner.y + content_height;
    let separator: String = "─".repeat(inner.width as usize);
    frame.render_widget(
        Paragraph::new(Span::styled(separator, Style::default().fg(Color::Rgb(60, 60, 60)))),
        Rect { x: inner.x, y: separator_y, width: inner.width, height: 1 },
    );

    let btn_y = inner.y + content_height + 1;
    let close_label = " 关闭 [Q] ";
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

fn centered_rect(percent_x: u16, percent_y: u16, r: Rect) -> Rect {
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
