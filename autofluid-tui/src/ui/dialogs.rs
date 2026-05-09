use ratatui::Frame;
use ratatui::layout::{Alignment, Constraint, Direction, Layout, Rect};
use ratatui::style::{Color, Modifier, Style};
use ratatui::text::{Line, Span};
use ratatui::widgets::{Block, Borders, Clear, Paragraph};

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

fn button_style(idx: u8, hovered: Option<u8>, clicked: Option<u8>) -> Style {
    if clicked == Some(idx) {
        BTN_CLICKED
    } else if hovered == Some(idx) {
        BTN_HOVER
    } else {
        BTN_NORMAL
    }
}

pub fn render_confirm_dialog(
    frame: &mut Frame,
    area: Rect,
    message: &str,
    hovered_dialog_button: Option<u8>,
    clicked_dialog_button: Option<u8>,
) {
    let dialog_area = centered_rect(80, 30, area);
    frame.render_widget(Clear, dialog_area);

    let inner = Rect {
        x: dialog_area.x + 1,
        y: dialog_area.y + 1,
        width: dialog_area.width.saturating_sub(2),
        height: dialog_area.height.saturating_sub(2),
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
    let btn_row_count: usize = 2;
    let available_height = inner.height as usize;
    let top_pad = if available_height > msg_line_count + btn_row_count {
        (available_height - msg_line_count - btn_row_count) / 2
    } else {
        0
    };

    let mut all_lines: Vec<Line> = Vec::new();
    for _ in 0..top_pad {
        all_lines.push(Line::from(""));
    }
    all_lines.extend(msg_lines);
    all_lines.push(Line::from(""));
    all_lines.push(Line::from(""));

    let confirm_label = " 确认 [Y] ";
    let cancel_label = " 取消 [N] ";
    let gap: u16 = 3;

    let confirm_style = button_style(0, hovered_dialog_button, clicked_dialog_button);
    let cancel_style = button_style(1, hovered_dialog_button, clicked_dialog_button);

    let btn_line = Line::from(vec![
        Span::styled(confirm_label.to_string(), confirm_style),
        Span::styled(" ".repeat(gap as usize), Style::default().bg(Color::Rgb(22, 33, 62))),
        Span::styled(cancel_label.to_string(), cancel_style),
    ]);
    all_lines.push(btn_line);

    let block = Block::default()
        .borders(Borders::ALL)
        .border_style(Style::default().fg(Color::Rgb(233, 69, 96)))
        .style(Style::default().bg(Color::Rgb(22, 33, 62)));

    let paragraph = Paragraph::new(all_lines)
        .alignment(Alignment::Center)
        .block(block);
    frame.render_widget(paragraph, dialog_area);
}

pub fn count_check_result_lines(data: &serde_json::Value) -> usize {
    let mut count: usize = 2;
    if let Some(local) = data.get("local_checks").and_then(|v| v.as_object()) {
        count += local.len();
    }
    count += 1;
    count += 1;
    if let Some(remote) = data.get("remote_checks").and_then(|v| v.as_object()) {
        count += remote.len();
    }
    count += 1;
    count += 1;
    count
}

pub fn render_check_result(
    frame: &mut Frame,
    area: Rect,
    data: &serde_json::Value,
    hovered_dialog_button: Option<u8>,
    clicked_dialog_button: Option<u8>,
) {
    let dialog_area = centered_rect(80, 50, area);
    frame.render_widget(Clear, dialog_area);

    let left_pad = "  ";

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
        for (name, info) in local {
            let exists = info.get("exists").and_then(|v| v.as_bool()).unwrap_or(false);
            let path = info.get("path").and_then(|v| v.as_str()).unwrap_or("");
            let icon = if exists { "✅" } else { "❌" };
            let color = if exists { Color::Green } else { Color::Red };
            lines.push(Line::from(vec![
                Span::styled(format!("{}  {} ", left_pad, icon), Style::default().fg(color)),
                Span::styled(format!("{}: {}", name, path), Style::default().fg(color)),
            ]));
        }
    }

    lines.push(Line::from(""));
    lines.push(Line::from(Span::styled(
        format!("{}─── 远程检查 ───", left_pad),
        Style::default().fg(Color::White).add_modifier(Modifier::BOLD),
    )));

    if let Some(remote) = data.get("remote_checks").and_then(|v| v.as_object()) {
        for (key, val) in remote {
            let display_val = match val {
                serde_json::Value::Array(arr) => arr.iter().map(|v| v.as_str().unwrap_or("?")).collect::<Vec<_>>().join(", "),
                _ => val.as_str().unwrap_or("?").to_string(),
            };
            lines.push(Line::from(format!("{}  {}: {}", left_pad, key, display_val)));
        }
    }

    lines.push(Line::from(""));

    let close_label = " 关闭 [Q] ";
    let close_style = button_style(0, hovered_dialog_button, clicked_dialog_button);

    let btn_line = Line::from(vec![
        Span::styled(close_label.to_string(), close_style),
    ]);
    lines.push(btn_line);

    let paragraph = Paragraph::new(lines)
        .alignment(Alignment::Center)
        .block(
            Block::default()
                .borders(Borders::ALL)
                .border_style(Style::default().fg(Color::Rgb(15, 52, 96)))
                .style(Style::default().bg(Color::Rgb(22, 33, 62))),
        );
    frame.render_widget(paragraph, dialog_area);
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
