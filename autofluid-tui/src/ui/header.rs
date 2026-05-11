use ratatui::Frame;
use ratatui::layout::Alignment;
use ratatui::style::{Color, Modifier, Style};
use ratatui::widgets::{Block, Borders, Paragraph};

use crate::state::app_state::AppState;

pub fn render_header(frame: &mut Frame, area: ratatui::layout::Rect, _state: &AppState) {
    let now = chrono::Local::now();
    let time_str = now.format("%H:%M:%S").to_string();

    let block = Block::default()
        .borders(Borders::ALL)
        .border_style(Style::default().fg(Color::Rgb(15, 52, 96)))
        .style(Style::default().bg(Color::Rgb(22, 33, 62)));
    let inner = block.inner(area);
    frame.render_widget(block, area);

    let title = Paragraph::new("🚀 液氧甲烷火箭发动机仿真总控程序 v2.3.0")
        .style(Style::default().fg(Color::Rgb(233, 69, 96)).add_modifier(Modifier::BOLD))
        .alignment(Alignment::Center);
    frame.render_widget(title, inner);

    let time_area = ratatui::layout::Rect {
        x: inner.x + inner.width.saturating_sub(11),
        y: inner.y,
        width: 8.min(inner.width),
        height: inner.height,
    };

    let time = Paragraph::new(time_str)
        .style(Style::default().fg(Color::Rgb(200, 200, 200)).bg(Color::Rgb(22, 33, 62)).add_modifier(Modifier::BOLD))
        .alignment(Alignment::Right);
    frame.render_widget(time, time_area);
}

pub fn render_info_bar(frame: &mut Frame, area: ratatui::layout::Rect, state: &AppState) {
    let text = state.info_bar_text();
    let paragraph = Paragraph::new(text)
        .style(Style::default().fg(Color::Rgb(200, 200, 200)).bg(Color::Rgb(15, 52, 96)))
        .alignment(Alignment::Center);
    frame.render_widget(paragraph, area);
}
