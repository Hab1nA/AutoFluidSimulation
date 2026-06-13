use ratatui::layout::Alignment;
use ratatui::style::{Modifier, Style};
use ratatui::widgets::{Block, Borders, Paragraph};
use ratatui::Frame;

use crate::state::app_state::AppState;
use crate::utils::{format_local_time, truncate_for_display};

pub fn render_header(frame: &mut Frame, area: ratatui::layout::Rect, state: &AppState) {
    let theme = &state.theme;
    let time_str = format_local_time("%H:%M:%S");

    let block = Block::default()
        .borders(Borders::ALL)
        .border_style(Style::default().fg(theme.secondary))
        .style(Style::default().bg(theme.bg));
    let inner = block.inner(area);
    frame.render_widget(block, area);

    let title = Paragraph::new("🚀 液氧甲烷火箭发动机仿真总控程序")
        .style(
            Style::default()
                .fg(theme.accent)
                .add_modifier(Modifier::BOLD),
        )
        .alignment(Alignment::Center);
    frame.render_widget(title, inner);

    let daemon_text = state.daemon_runtime_text();
    if inner.width > 30 && !daemon_text.is_empty() {
        let max_daemon_width = (inner.width / 3).max(12) as usize;
        let daemon_area = ratatui::layout::Rect {
            x: inner.x + 1,
            y: inner.y,
            width: (max_daemon_width as u16).min(inner.width),
            height: inner.height,
        };
        let daemon = Paragraph::new(truncate_for_display(&daemon_text, max_daemon_width))
            .style(
                Style::default()
                    .fg(theme.gray_5)
                    .bg(theme.bg)
                    .add_modifier(Modifier::BOLD),
            )
            .alignment(Alignment::Left);
        frame.render_widget(daemon, daemon_area);
    }

    let time_area = ratatui::layout::Rect {
        x: inner.x + inner.width.saturating_sub(9),
        y: inner.y,
        width: 8.min(inner.width),
        height: inner.height,
    };

    let time = Paragraph::new(time_str)
        .style(
            Style::default()
                .fg(theme.gray_5)
                .bg(theme.bg)
                .add_modifier(Modifier::BOLD),
        )
        .alignment(Alignment::Right);
    frame.render_widget(time, time_area);
}

pub fn render_info_bar(frame: &mut Frame, area: ratatui::layout::Rect, state: &AppState) {
    let theme = &state.theme;
    let paragraph = Paragraph::new(state.info_bar_line())
        .style(Style::default().bg(theme.secondary))
        .alignment(Alignment::Center);
    frame.render_widget(paragraph, area);
}
