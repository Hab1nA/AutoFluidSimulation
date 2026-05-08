use ratatui::Frame;
use ratatui::layout::{Constraint, Direction, Layout};
use ratatui::style::{Color, Modifier, Style};
use ratatui::widgets::{Block, Borders, Clear, Paragraph};

pub fn render_confirm_dialog(frame: &mut Frame, area: ratatui::layout::Rect, message: &str) {
    let dialog_area = centered_rect(80, 30, area);

    frame.render_widget(Clear, dialog_area);

    let lines: Vec<ratatui::text::Line> = message
        .lines()
        .map(|line| {
            ratatui::text::Line::from(ratatui::text::Span::styled(
                line.to_string(),
                Style::default().fg(Color::Rgb(255, 170, 0)).add_modifier(Modifier::BOLD),
            ))
        })
        .chain(std::iter::once(ratatui::text::Line::from("")))
        .chain(std::iter::once(ratatui::text::Line::from(
            ratatui::text::Span::styled(
                "按 [[Y]] 确认  |  按 [[N]] 取消",
                Style::default().fg(Color::Rgb(136, 136, 136)),
            ),
        )))
        .collect();

    let paragraph = Paragraph::new(lines)
        .alignment(ratatui::layout::Alignment::Center)
        .block(
            Block::default()
                .borders(Borders::ALL)
                .border_style(Style::default().fg(Color::Rgb(233, 69, 96)))
                .style(Style::default().bg(Color::Rgb(22, 33, 62))),
        );
    frame.render_widget(paragraph, dialog_area);
}

pub fn render_check_result(frame: &mut Frame, area: ratatui::layout::Rect, data: &serde_json::Value) {
    let dialog_area = centered_rect(80, 50, area);

    frame.render_widget(Clear, dialog_area);

    let mut lines: Vec<ratatui::text::Line> = vec![
        ratatui::text::Line::from(ratatui::text::Span::styled(
            "系统自检结果",
            Style::default().fg(Color::White).add_modifier(Modifier::BOLD),
        )),
        ratatui::text::Line::from(ratatui::text::Span::styled(
            "─── 本地检查 ───",
            Style::default().fg(Color::White).add_modifier(Modifier::BOLD),
        )),
    ];

    if let Some(local) = data.get("local_checks").and_then(|v| v.as_object()) {
        for (name, info) in local {
            let exists = info.get("exists").and_then(|v| v.as_bool()).unwrap_or(false);
            let path = info.get("path").and_then(|v| v.as_str()).unwrap_or("");
            let icon = if exists { "✅" } else { "❌" };
            let color = if exists { Color::Green } else { Color::Red };
            lines.push(ratatui::text::Line::from(vec![
                ratatui::text::Span::styled(format!("  {} ", icon), Style::default().fg(color)),
                ratatui::text::Span::styled(format!("{}: {}", name, path), Style::default().fg(color)),
            ]));
        }
    }

    lines.push(ratatui::text::Line::from(ratatui::text::Span::styled(
        "\n─── 远程检查 ───",
        Style::default().fg(Color::White).add_modifier(Modifier::BOLD),
    )));

    if let Some(remote) = data.get("remote_checks").and_then(|v| v.as_object()) {
        for (key, val) in remote {
            let display_val = match val {
                serde_json::Value::Array(arr) => arr.iter().map(|v| v.as_str().unwrap_or("?")).collect::<Vec<_>>().join(", "),
                _ => val.as_str().unwrap_or("?").to_string(),
            };
            lines.push(ratatui::text::Line::from(format!("  {}: {}", key, display_val)));
        }
    }

    lines.push(ratatui::text::Line::from(""));
    lines.push(ratatui::text::Line::from(ratatui::text::Span::styled(
        "按 [[Q]] 或 [[Esc]] 关闭",
        Style::default().fg(Color::Rgb(136, 136, 136)),
    )));

    let paragraph = Paragraph::new(lines)
        .block(
            Block::default()
                .borders(Borders::ALL)
                .border_style(Style::default().fg(Color::Rgb(15, 52, 96)))
                .style(Style::default().bg(Color::Rgb(22, 33, 62))),
        );
    frame.render_widget(paragraph, dialog_area);
}

fn centered_rect(percent_x: u16, percent_y: u16, r: ratatui::layout::Rect) -> ratatui::layout::Rect {
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
