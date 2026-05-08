use ratatui::Frame;
use ratatui::style::{Color, Modifier, Style};
use ratatui::widgets::{Block, Borders, Paragraph, Wrap, Scrollbar, ScrollbarOrientation, ScrollbarState};

use crate::state::app_state::FocusZone;
use crate::state::log_buffer::LogBuffer;

pub fn render_info_panel(frame: &mut Frame, area: ratatui::layout::Rect, log_buffer: &LogBuffer, scroll_offset: u16, focus_zone: FocusZone) {
    let border_style = if focus_zone == FocusZone::InfoLog {
        Style::default().fg(Color::Rgb(233, 69, 96))
    } else {
        Style::default().fg(Color::Rgb(51, 51, 51))
    };

    let title = Paragraph::new("📋 信息提示")
        .style(Style::default().fg(Color::Rgb(233, 69, 96)).add_modifier(Modifier::BOLD))
        .block(Block::default().borders(Borders::ALL).border_style(border_style).style(Style::default().bg(Color::Rgb(22, 33, 62))));
    frame.render_widget(title, area);

    let inner_area = Rect {
        x: area.x + 1,
        y: area.y + 1,
        width: area.width.saturating_sub(2),
        height: area.height.saturating_sub(2),
    };

    let messages: String = log_buffer.info_messages.iter().cloned().collect::<Vec<_>>().join("\n");
    let line_count = log_buffer.info_messages.len() as u16;
    let visible_height = inner_area.height;

    let paragraph = Paragraph::new(messages)
        .style(Style::default().fg(Color::Rgb(0, 255, 136)).bg(Color::Rgb(13, 13, 13)))
        .wrap(Wrap { trim: false })
        .scroll((scroll_offset, 0))
        .block(Block::default().style(Style::default().bg(Color::Rgb(13, 13, 13))));
    frame.render_widget(paragraph, inner_area);

    if line_count > visible_height {
        let mut scrollbar_state = ScrollbarState::new(line_count as usize)
            .position(scroll_offset as usize);
        let scrollbar = Scrollbar::new(ScrollbarOrientation::VerticalRight)
            .style(Style::default().fg(Color::Rgb(100, 100, 100)));
        frame.render_stateful_widget(scrollbar, inner_area, &mut scrollbar_state);
    }
}

pub fn render_detail_panel(
    frame: &mut Frame,
    title_area: ratatui::layout::Rect,
    body_area: ratatui::layout::Rect,
    log_buffer: &LogBuffer,
    level_filter: &Option<String>,
    source_filter: &Option<String>,
    scroll_offset: u16,
    auto_scroll: bool,
    focus_zone: FocusZone,
) {
    let border_style = if focus_zone == FocusZone::DetailLog {
        Style::default().fg(Color::Rgb(233, 69, 96))
    } else {
        Style::default().fg(Color::Rgb(51, 51, 51))
    };

    let mut title_text = "📝 详细日志".to_string();
    if let Some(lf) = level_filter {
        title_text.push_str(&format!(" [{}]", lf));
    }
    if let Some(sf) = source_filter {
        title_text.push_str(&format!(" [{}]", sf));
    }
    if auto_scroll {
        title_text.push_str(" [自动]");
    }

    let title = Paragraph::new(title_text)
        .style(Style::default().fg(Color::Rgb(11, 188, 240)).add_modifier(Modifier::BOLD))
        .block(Block::default().borders(Borders::ALL).border_style(border_style).style(Style::default().bg(Color::Rgb(22, 33, 62))));
    frame.render_widget(title, title_area);

    let inner_body = Rect {
        x: body_area.x,
        y: body_area.y,
        width: body_area.width,
        height: body_area.height,
    };

    let mut lines: Vec<ratatui::text::Line> = Vec::new();
    for entry in log_buffer.filtered_entries(level_filter, source_filter) {
        let icon = entry.source_icon();
        let color = entry.level_color();
        let level = &entry.level;
        let msg = &entry.raw_message;
        lines.push(ratatui::text::Line::from(vec![
            ratatui::text::Span::styled(
                format!("{} ", icon),
                Style::default(),
            ),
            ratatui::text::Span::styled(
                format!("[{}]", level),
                Style::default().fg(color),
            ),
            ratatui::text::Span::raw(format!(" {}", msg)),
        ]));
    }

    let line_count = lines.len() as u16;
    let visible_height = inner_body.height;

    let paragraph = Paragraph::new(lines)
        .style(Style::default().fg(Color::Rgb(224, 224, 224)).bg(Color::Rgb(13, 13, 13)))
        .wrap(Wrap { trim: false })
        .scroll((scroll_offset, 0))
        .block(Block::default().style(Style::default().bg(Color::Rgb(13, 13, 13))));
    frame.render_widget(paragraph, inner_body);

    if line_count > visible_height {
        let mut scrollbar_state = ScrollbarState::new(line_count as usize)
            .position(scroll_offset as usize);
        let scrollbar = Scrollbar::new(ScrollbarOrientation::VerticalRight)
            .style(Style::default().fg(Color::Rgb(100, 100, 100)));
        frame.render_stateful_widget(scrollbar, inner_body, &mut scrollbar_state);
    }
}

use ratatui::layout::Rect;
