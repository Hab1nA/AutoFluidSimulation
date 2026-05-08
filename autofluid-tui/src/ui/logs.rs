use ratatui::Frame;
use ratatui::layout::Rect;
use ratatui::style::{Color, Modifier, Style};
use ratatui::text::{Line, Span};
use ratatui::widgets::{Block, Borders, Clear, Paragraph, Scrollbar, ScrollbarOrientation, ScrollbarState};

use crate::state::app_state::FocusZone;
use crate::state::log_buffer::LogBuffer;

fn wrap_text_to_width(text: &str, max_width: usize) -> Vec<String> {
    if max_width == 0 {
        return vec![text.to_string()];
    }
    let mut result = Vec::new();
    let mut current = String::new();
    let mut current_w = 0usize;

    for ch in text.chars() {
        let cw = unicode_width::UnicodeWidthChar::width(ch).unwrap_or(0);
        if current_w + cw > max_width && !current.is_empty() {
            result.push(std::mem::take(&mut current));
            current_w = 0;
        }
        current.push(ch);
        current_w += cw;
    }
    if !current.is_empty() {
        result.push(current);
    }
    if result.is_empty() {
        result.push(String::new());
    }
    result
}

fn info_message_color(msg: &str) -> Color {
    if msg.contains('✅') {
        Color::Rgb(0, 204, 102)
    } else if msg.contains('❌') {
        Color::Rgb(255, 68, 68)
    } else if msg.contains('⚠') {
        Color::Rgb(255, 204, 0)
    } else if msg.contains('💡') || msg.contains('📌') {
        Color::Rgb(0, 188, 240)
    } else if msg.contains('⏳') || msg.contains('⏸') {
        Color::Rgb(255, 136, 0)
    } else {
        Color::Rgb(0, 255, 136)
    }
}

pub fn compute_info_visual_lines(log_buffer: &LogBuffer, max_width: usize) -> Vec<Line<'static>> {
    let mut visual_lines: Vec<Line> = Vec::new();
    for msg in &log_buffer.info_messages {
        let color = info_message_color(msg);
        for chunk in wrap_text_to_width(msg, max_width) {
            visual_lines.push(Line::from(Span::styled(
                chunk,
                Style::default().fg(color),
            )));
        }
    }
    visual_lines
}

pub fn compute_detail_visual_lines(
    log_buffer: &LogBuffer,
    level_filter: &Option<String>,
    source_filter: &Option<String>,
    max_width: usize,
) -> Vec<Line<'static>> {
    let mut visual_lines: Vec<Line> = Vec::new();
    for entry in log_buffer.filtered_entries(level_filter, source_filter) {
        let icon = entry.source_icon();
        let color = entry.level_color();
        let level = entry.level.clone();
        let msg = entry.raw_message.clone();
        let full = format!("{} [{}] {}", icon, level, msg);

        let wrapped = wrap_text_to_width(&full, max_width);
        for (i, chunk) in wrapped.into_iter().enumerate() {
            if i == 0 {
                let prefix = format!("{} [{}] ", icon, level);
                let prefix_display_w = unicode_width::UnicodeWidthStr::width(prefix.as_str());
                let chunk_display_w = unicode_width::UnicodeWidthStr::width(chunk.as_str());

                if chunk_display_w > prefix_display_w {
                    let msg_text = chunk.chars().skip(prefix.chars().count()).collect::<String>();
                    visual_lines.push(Line::from(vec![
                        Span::styled(prefix, Style::default().fg(color)),
                        Span::raw(msg_text),
                    ]));
                } else {
                    visual_lines.push(Line::from(Span::styled(
                        chunk,
                        Style::default().fg(color),
                    )));
                }
            } else {
                visual_lines.push(Line::from(Span::styled(
                    format!("  {}", chunk),
                    Style::default().fg(Color::Rgb(180, 180, 180)),
                )));
            }
        }
    }
    visual_lines
}

pub fn render_info_panel(frame: &mut Frame, area: Rect, log_buffer: &LogBuffer, scroll_offset: u16, focus_zone: FocusZone, scrollbar_state: &mut ScrollbarState) {
    let border_style = if focus_zone == FocusZone::InfoLog {
        Style::default().fg(Color::Rgb(233, 69, 96))
    } else {
        Style::default().fg(Color::Rgb(51, 51, 51))
    };

    let block = Block::default()
        .borders(Borders::ALL)
        .border_style(border_style)
        .title(" 📋 信息提示 ")
        .title_style(Style::default().fg(Color::Rgb(233, 69, 96)).add_modifier(Modifier::BOLD))
        .style(Style::default().bg(Color::Rgb(22, 33, 62)));
    frame.render_widget(Clear, area);
    let inner = block.inner(area);
    frame.render_widget(&block, area);

    let content_width = inner.width.saturating_sub(1) as usize;
    let visual_lines = compute_info_visual_lines(log_buffer, content_width);
    let total = visual_lines.len();
    let visible = inner.height as usize;
    let scroll = scroll_offset as usize;
    let start = scroll.min(total);
    let visible_lines: Vec<Line> = visual_lines.into_iter().skip(start).take(visible).collect();

    let text_area = Rect {
        x: inner.x,
        y: inner.y,
        width: inner.width.saturating_sub(1),
        height: inner.height,
    };
    let scrollbar_area = Rect {
        x: inner.x + inner.width.saturating_sub(1),
        y: inner.y,
        width: 1,
        height: inner.height,
    };

    let paragraph = Paragraph::new(visible_lines)
        .style(Style::default().bg(Color::Rgb(13, 13, 13)));
    frame.render_widget(paragraph, text_area);

    if total > visible {
        *scrollbar_state = ScrollbarState::new(total)
            .viewport_content_length(visible)
            .position(scroll);
        let scrollbar = Scrollbar::new(ScrollbarOrientation::VerticalRight)
            .style(Style::default().fg(Color::Rgb(100, 100, 100)));
        frame.render_stateful_widget(scrollbar, scrollbar_area, scrollbar_state);
    }
}

pub fn render_detail_panel(
    frame: &mut Frame,
    area: Rect,
    log_buffer: &LogBuffer,
    level_filter: &Option<String>,
    source_filter: &Option<String>,
    scroll_offset: u16,
    auto_scroll: bool,
    focus_zone: FocusZone,
    scrollbar_state: &mut ScrollbarState,
) {
    let border_style = if focus_zone == FocusZone::DetailLog {
        Style::default().fg(Color::Rgb(233, 69, 96))
    } else {
        Style::default().fg(Color::Rgb(51, 51, 51))
    };

    let mut title_text = " 📝 详细日志 ".to_string();
    if let Some(lf) = level_filter {
        title_text.push_str(&format!("[{}] ", lf));
    }
    if let Some(sf) = source_filter {
        title_text.push_str(&format!("[{}] ", sf));
    }
    if auto_scroll {
        title_text.push_str("[自动▼] ");
    }

    let block = Block::default()
        .borders(Borders::ALL)
        .border_style(border_style)
        .title(title_text)
        .title_style(Style::default().fg(Color::Rgb(11, 188, 240)).add_modifier(Modifier::BOLD))
        .style(Style::default().bg(Color::Rgb(22, 33, 62)));
    frame.render_widget(Clear, area);
    let inner = block.inner(area);
    frame.render_widget(&block, area);

    let content_width = inner.width.saturating_sub(1) as usize;
    let visual_lines = compute_detail_visual_lines(log_buffer, level_filter, source_filter, content_width);
    let total = visual_lines.len();
    let visible = inner.height as usize;
    let scroll = scroll_offset as usize;
    let start = scroll.min(total);
    let visible_lines: Vec<Line> = visual_lines.into_iter().skip(start).take(visible).collect();

    let text_area = Rect {
        x: inner.x,
        y: inner.y,
        width: inner.width.saturating_sub(1),
        height: inner.height,
    };
    let scrollbar_area = Rect {
        x: inner.x + inner.width.saturating_sub(1),
        y: inner.y,
        width: 1,
        height: inner.height,
    };

    let paragraph = Paragraph::new(visible_lines)
        .style(Style::default().bg(Color::Rgb(13, 13, 13)));
    frame.render_widget(paragraph, text_area);

    if total > visible {
        *scrollbar_state = ScrollbarState::new(total)
            .viewport_content_length(visible)
            .position(scroll);
        let scrollbar = Scrollbar::new(ScrollbarOrientation::VerticalRight)
            .style(Style::default().fg(Color::Rgb(100, 100, 100)));
        frame.render_stateful_widget(scrollbar, scrollbar_area, scrollbar_state);
    }
}
