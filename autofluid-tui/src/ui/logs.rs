use ratatui::Frame;
use ratatui::layout::Rect;
use ratatui::style::{Color, Modifier, Style};
use ratatui::text::{Line, Span};
use ratatui::widgets::{Block, Borders, Clear, Paragraph};

use crate::state::app_state::FocusZone;
use crate::state::log_buffer::LogBuffer;
use crate::ui::scrollbar;

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

const HOVER_HIGHLIGHT_STYLE: Style = Style::new()
    .bg(Color::Rgb(15, 52, 96))
    .add_modifier(Modifier::BOLD);

const CLICK_HIGHLIGHT_STYLE: Style = Style::new()
    .fg(Color::Rgb(0, 0, 0))
    .bg(Color::Rgb(255, 255, 255))
    .add_modifier(Modifier::BOLD);

pub fn compute_info_lines_no_wrap(log_buffer: &LogBuffer) -> (Vec<Line<'static>>, usize) {
    let mut lines: Vec<Line> = Vec::new();
    let mut max_width: usize = 0;
    for msg in &log_buffer.info_messages {
        let color = info_message_color(msg);
        let w = unicode_width::UnicodeWidthStr::width(msg.as_str());
        if w > max_width {
            max_width = w;
        }
        lines.push(Line::from(Span::styled(
            msg.clone(),
            Style::default().fg(color),
        )));
    }
    (lines, max_width)
}

pub fn compute_detail_lines_no_wrap(
    log_buffer: &LogBuffer,
    level_filter: &Option<String>,
    source_filter: &Option<String>,
) -> (Vec<Line<'static>>, usize) {
    let mut lines: Vec<Line> = Vec::new();
    let mut max_width: usize = 0;
    for entry in log_buffer.filtered_entries(level_filter, source_filter) {
        let color = entry.level_color();
        let level = entry.level.clone();
        let prefix = format!("[{}] ", level);
        let msg = entry.raw_message.clone();
        let full = format!("{}{}", prefix, msg);
        let w = unicode_width::UnicodeWidthStr::width(full.as_str());
        if w > max_width {
            max_width = w;
        }
        lines.push(Line::from(vec![
            Span::styled(prefix, Style::default().fg(color)),
            Span::raw(msg),
        ]));
    }
    (lines, max_width)
}

fn compute_layout(inner: Rect, total: usize, max_content_width: usize) -> (usize, usize, bool, bool) {
    let visible_height = inner.height as usize;
    let has_vscroll = total > visible_height;
    let has_hscroll = max_content_width > inner.width as usize;

    let content_height = if has_hscroll {
        inner.height.saturating_sub(1) as usize
    } else {
        inner.height as usize
    };
    let content_width = if has_vscroll {
        inner.width.saturating_sub(1) as usize
    } else {
        inner.width as usize
    };

    (content_height, content_width, has_vscroll, has_hscroll)
}

pub fn render_info_panel(
    frame: &mut Frame,
    area: Rect,
    log_buffer: &LogBuffer,
    scroll_offset: u16,
    focus_zone: FocusZone,
    hscroll: u16,
) {
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

    let (lines, max_content_width) = compute_info_lines_no_wrap(log_buffer);
    let total = lines.len();
    let (content_height, content_width, has_vscroll, has_hscroll) = compute_layout(inner, total, max_content_width);

    let scroll = scroll_offset as usize;
    let start = scroll.min(total);
    let visible_lines: Vec<Line> = lines.into_iter().skip(start).take(content_height).collect();

    let text_area = Rect {
        x: inner.x,
        y: inner.y,
        width: content_width as u16,
        height: content_height as u16,
    };

    let paragraph = Paragraph::new(visible_lines)
        .style(Style::default().bg(Color::Rgb(13, 13, 13)))
        .scroll((0, hscroll));
    frame.render_widget(paragraph, text_area);

    if has_vscroll {
        let scrollbar_area = Rect {
            x: inner.x + inner.width.saturating_sub(1),
            y: inner.y,
            width: 1,
            height: content_height as u16,
        };
        frame.render_widget(
            scrollbar::VerticalScrollbar {
                total,
                visible: content_height,
                scroll,
            },
            scrollbar_area,
        );
    }

    if has_hscroll {
        let hscrollbar_area = Rect {
            x: inner.x,
            y: inner.y + content_height as u16,
            width: content_width as u16,
            height: 1,
        };
        frame.render_widget(
            scrollbar::HorizontalScrollbar {
                total: max_content_width,
                visible: content_width,
                scroll: hscroll as usize,
            },
            hscrollbar_area,
        );
    }
}

pub fn get_raw_message_at_visual_line(
    log_buffer: &LogBuffer,
    level_filter: &Option<String>,
    source_filter: &Option<String>,
    _max_width: usize,
    visual_line: usize,
) -> Option<String> {
    let entries: Vec<_> = log_buffer.filtered_entries(level_filter, source_filter).collect();
    if visual_line < entries.len() {
        Some(entries[visual_line].raw_message.clone())
    } else {
        None
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
    hovered_detail_row: Option<u16>,
    clicked_detail_row: Option<u16>,
    hscroll: u16,
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

    let (lines, max_content_width) = compute_detail_lines_no_wrap(log_buffer, level_filter, source_filter);
    let total = lines.len();
    let (content_height, content_width, has_vscroll, has_hscroll) = compute_layout(inner, total, max_content_width);

    let scroll = scroll_offset as usize;
    let start = scroll.min(total);

    let visible_lines: Vec<Line> = lines
        .into_iter()
        .enumerate()
        .skip(start)
        .take(content_height)
        .map(|(idx, mut line)| {
            let is_clicked = clicked_detail_row == Some(idx as u16);
            let is_hovered = !is_clicked && hovered_detail_row == Some(idx as u16);
            let highlight_style = if is_clicked {
                CLICK_HIGHLIGHT_STYLE
            } else if is_hovered {
                HOVER_HIGHLIGHT_STYLE
            } else {
                Style::default()
            };
            if is_clicked || is_hovered {
                for span in &mut line.spans {
                    span.style = span.style.patch(highlight_style);
                }
            }
            line
        })
        .collect();

    let text_area = Rect {
        x: inner.x,
        y: inner.y,
        width: content_width as u16,
        height: content_height as u16,
    };

    let paragraph = Paragraph::new(visible_lines)
        .style(Style::default().bg(Color::Rgb(13, 13, 13)))
        .scroll((0, hscroll));
    frame.render_widget(paragraph, text_area);

    if has_vscroll {
        let scrollbar_area = Rect {
            x: inner.x + inner.width.saturating_sub(1),
            y: inner.y,
            width: 1,
            height: content_height as u16,
        };
        frame.render_widget(
            scrollbar::VerticalScrollbar {
                total,
                visible: content_height,
                scroll,
            },
            scrollbar_area,
        );
    }

    if has_hscroll {
        let hscrollbar_area = Rect {
            x: inner.x,
            y: inner.y + content_height as u16,
            width: content_width as u16,
            height: 1,
        };
        frame.render_widget(
            scrollbar::HorizontalScrollbar {
                total: max_content_width,
                visible: content_width,
                scroll: hscroll as usize,
            },
            hscrollbar_area,
        );
    }
}
