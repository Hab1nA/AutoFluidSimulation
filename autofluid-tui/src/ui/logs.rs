use ratatui::layout::Rect;
use ratatui::style::{Color, Modifier, Style};
use ratatui::text::{Line, Span};
use ratatui::widgets::{Block, Borders, Clear, Paragraph};
use ratatui::Frame;

use crate::state::app_state::FocusZone;
use crate::state::log_buffer::LogBuffer;
use crate::ui::scrollbar;

#[derive(Debug, Clone)]
pub struct DetailPanelParams<'a> {
    pub log_buffer: &'a LogBuffer,
    pub level_filter: &'a Option<String>,
    pub source_filter: &'a Option<String>,
    pub scroll_offset: u16,
    pub auto_scroll: bool,
    pub focus_zone: FocusZone,
    pub hovered_detail_row: Option<u16>,
    pub clicked_detail_row: Option<u16>,
    pub hscroll: u16,
    pub theme: &'a crate::theme::AppTheme,
}

fn info_message_color(msg: &str, theme: &crate::theme::AppTheme) -> Color {
    if msg.contains('\u{2705}') {
        theme.success
    } else if msg.contains('\u{274C}') {
        theme.error
    } else if msg.contains('\u{26A0}') {
        theme.warning
    } else if msg.contains('\u{1F4A1}') || msg.contains('\u{1F4CC}') {
        theme.info
    } else if msg.contains('\u{23F3}') || msg.contains('\u{23F8}') {
        theme.warning
    } else {
        theme.success
    }
}

pub fn compute_info_lines_no_wrap(
    log_buffer: &LogBuffer,
    theme: &crate::theme::AppTheme,
) -> (Vec<Line<'static>>, usize) {
    let mut lines: Vec<Line> = Vec::new();
    let mut max_width: usize = 0;
    for msg in &log_buffer.info_messages {
        let color = info_message_color(msg, theme);
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

fn compute_layout(
    inner: Rect,
    total: usize,
    max_content_width: usize,
) -> (usize, usize, bool, bool) {
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

#[allow(clippy::too_many_arguments)]
pub fn render_info_panel(
    frame: &mut Frame,
    area: Rect,
    log_buffer: &LogBuffer,
    scroll_offset: u16,
    focus_zone: FocusZone,
    hscroll: u16,
    auto_scroll: bool,
    theme: &crate::theme::AppTheme,
) {
    let border_style = theme.border_style_for(focus_zone == FocusZone::InfoLog);

    let mut title_text = " 📋 信息提示 ".to_string();
    if auto_scroll {
        title_text.push_str("[自动▼] ");
    }

    let block = Block::default()
        .borders(Borders::ALL)
        .border_style(border_style)
        .title(title_text)
        .title_style(theme.title_style())
        .style(Style::default().bg(theme.bg));
    frame.render_widget(Clear, area);
    let inner = block.inner(area);
    frame.render_widget(&block, area);

    let (lines, max_content_width) = compute_info_lines_no_wrap(log_buffer, theme);
    let total = lines.len();
    let (content_height, content_width, has_vscroll, has_hscroll) =
        compute_layout(inner, total, max_content_width);

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
        .style(Style::default().bg(theme.panel_bg))
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
                track_color: Some(theme.scrollbar_track),
                thumb_color: Some(theme.scrollbar_thumb),
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
                track_color: Some(theme.scrollbar_track),
                thumb_color: Some(theme.scrollbar_thumb),
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
    let entries: Vec<_> = log_buffer
        .filtered_entries(level_filter, source_filter)
        .collect();
    if visual_line < entries.len() {
        Some(entries[visual_line].raw_message.clone())
    } else {
        None
    }
}

pub fn render_detail_panel(frame: &mut Frame, area: Rect, params: &DetailPanelParams) {
    let DetailPanelParams {
        log_buffer,
        ref level_filter,
        ref source_filter,
        ref scroll_offset,
        ref auto_scroll,
        ref focus_zone,
        ref hovered_detail_row,
        ref clicked_detail_row,
        ref hscroll,
        theme,
    } = params;

    let border_style = if *focus_zone == FocusZone::DetailLog {
        Style::default().fg(theme.accent)
    } else {
        Style::default().fg(theme.border_inactive)
    };

    let mut title_text = " 📝 详细日志 ".to_string();
    if let Some(lf) = level_filter {
        title_text.push_str(&format!("[{}] ", lf));
    }
    if let Some(sf) = source_filter {
        title_text.push_str(&format!("[{}] ", sf));
    }
    if *auto_scroll {
        title_text.push_str("[自动▼] ");
    }

    let block = Block::default()
        .borders(Borders::ALL)
        .border_style(border_style)
        .title(title_text)
        .title_style(theme.detail_title_style())
        .style(Style::default().bg(theme.bg));
    frame.render_widget(Clear, area);
    let inner = block.inner(area);
    frame.render_widget(&block, area);

    let (lines, max_content_width) =
        compute_detail_lines_no_wrap(log_buffer, level_filter, source_filter);
    let total = lines.len();
    let (content_height, content_width, has_vscroll, has_hscroll) =
        compute_layout(inner, total, max_content_width);

    let scroll = *scroll_offset as usize;
    let start = scroll.min(total);

    let visible_lines: Vec<Line> = lines
        .into_iter()
        .enumerate()
        .skip(start)
        .take(content_height)
        .map(|(idx, mut line)| {
            let is_clicked = *clicked_detail_row == Some(idx as u16);
            let is_hovered = !is_clicked && *hovered_detail_row == Some(idx as u16);
            let highlight_style = if is_clicked {
                Style::default()
                    .fg(theme.click_fg)
                    .bg(theme.click_bg)
                    .add_modifier(Modifier::BOLD)
            } else if is_hovered {
                theme.hover_style()
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
        .style(Style::default().bg(theme.panel_bg))
        .scroll((0, *hscroll));
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
                track_color: Some(theme.scrollbar_track),
                thumb_color: Some(theme.scrollbar_thumb),
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
                scroll: (*hscroll) as usize,
                track_color: Some(theme.scrollbar_track),
                thumb_color: Some(theme.scrollbar_thumb),
            },
            hscrollbar_area,
        );
    }
}
