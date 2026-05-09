use ratatui::Frame;
use ratatui::layout::Alignment;
use ratatui::style::{Color, Modifier, Style};
use ratatui::text::{Line, Span};
use ratatui::widgets::{Block, Borders, Paragraph};

use crate::state::app_state::{AppState, FocusZone};

pub const BUTTON_DEFS: [(&str, &str); 8] = [
    ("▶ Start", "start"),
    ("⏸ Pause", "pause"),
    ("🔧 Check", "check"),
    ("📊 Status", "status"),
    ("▶ Daemon Start", "daemon start"),
    ("⏹ Daemon Stop", "daemon stop"),
    ("🚪 Quit", "quit"),
    ("⏹ Quit Full", "quit full"),
];

pub fn button_display_width(label: &str) -> u16 {
    unicode_width::UnicodeWidthStr::width(label) as u16
}

pub fn button_total_width(label: &str) -> u16 {
    button_display_width(label) + 2
}

pub fn render_command_bar(frame: &mut Frame, input_area: ratatui::layout::Rect, buttons_area: ratatui::layout::Rect, state: &AppState) {
    let input_style = if state.focus_zone == FocusZone::CommandInput {
        Style::default().fg(Color::Rgb(0, 255, 136)).bg(Color::Rgb(13, 13, 13))
    } else {
        Style::default().fg(Color::Rgb(100, 160, 100)).bg(Color::Rgb(13, 13, 13))
    };

    let before_cursor: String = state.command_input.chars().take(state.command_cursor).collect();
    let cursor_char = state.command_input.chars().nth(state.command_cursor);
    let after_cursor: String = state.command_input.chars().skip(state.command_cursor + 1).collect();

    let prefix = "> ";
    let before_text = format!("{}{}", prefix, before_cursor);
    let before_width = unicode_width::UnicodeWidthStr::width(before_text.as_str()) as usize;
    let cursor_display_w = cursor_char
        .and_then(|c| unicode_width::UnicodeWidthChar::width(c))
        .unwrap_or(0);
    let after_width = unicode_width::UnicodeWidthStr::width(after_cursor.as_str()) as usize;
    let total_width = before_width + cursor_display_w + after_width;

    let input_display_width = input_area.width.saturating_sub(2) as usize;

    let scroll_x = if total_width > input_display_width {
        if before_width + cursor_display_w > input_display_width {
            (before_width + cursor_display_w - input_display_width + 3).min(total_width - input_display_width) as u16
        } else {
            0u16
        }
    } else {
        0u16
    };

    let cursor_highlight = Style::default()
        .fg(Color::Rgb(0, 0, 0))
        .bg(Color::Rgb(0, 255, 136))
        .add_modifier(Modifier::BOLD);

    let input_line = if state.focus_zone == FocusZone::CommandInput {
        let mut spans = vec![
            Span::styled(prefix, input_style),
            Span::styled(before_cursor, input_style),
        ];
        if let Some(ch) = cursor_char {
            spans.push(Span::styled(ch.to_string(), cursor_highlight));
            spans.push(Span::styled(after_cursor, input_style));
        } else {
            spans.push(Span::styled("▎".to_string(), Style::default().fg(Color::Rgb(0, 255, 136))));
        }
        Line::from(spans)
    } else {
        let full_text = format!("{}{}{}", prefix, state.command_input, " ");
        Line::from(Span::styled(full_text, input_style))
    };

    let cmd_border_style = if state.focus_zone == FocusZone::CommandInput {
        Style::default().fg(Color::Rgb(233, 69, 96))
    } else {
        Style::default().fg(Color::Rgb(15, 52, 96))
    };

    let paragraph = Paragraph::new(vec![input_line])
        .style(Style::default().bg(Color::Rgb(13, 13, 13)))
        .alignment(Alignment::Left)
        .scroll((0, scroll_x))
        .block(
            Block::default()
                .borders(Borders::ALL)
                .border_style(cmd_border_style),
        );
    frame.render_widget(paragraph, input_area);

    let mut spans: Vec<Span> = Vec::new();

    for (i, (label, _cmd)) in BUTTON_DEFS.iter().enumerate() {
        let is_hovered = state.hovered_button == Some(i as u8);
        let is_clicked = state.clicked_button == Some(i as u8);

        let btn_style = if is_clicked {
            Style::default().fg(Color::Rgb(0, 0, 0)).bg(Color::Rgb(255, 255, 255)).add_modifier(Modifier::BOLD)
        } else if is_hovered {
            Style::default().fg(Color::Rgb(255, 255, 255)).bg(Color::Rgb(233, 69, 96)).add_modifier(Modifier::BOLD)
        } else {
            Style::default().fg(Color::Rgb(224, 224, 224)).bg(Color::Rgb(15, 52, 96))
        };

        spans.push(Span::styled(format!(" {} ", label), btn_style));
        spans.push(Span::styled(" ", Style::default().bg(Color::Rgb(15, 52, 96))));
    }

    let total_btn_width: u16 = BUTTON_DEFS.iter().map(|(l, _)| button_total_width(l) + 1).sum();
    let padding = buttons_area.width.saturating_sub(total_btn_width) / 2;

    let mut padded_spans = vec![Span::styled(
        " ".repeat(padding as usize),
        Style::default().bg(Color::Rgb(15, 52, 96))
    )];
    padded_spans.extend(spans);

    let buttons_line = Line::from(padded_spans);
    let buttons = Paragraph::new(vec![Line::from(""), buttons_line, Line::from("")])
        .style(Style::default().bg(Color::Rgb(15, 52, 96)));
    frame.render_widget(buttons, buttons_area);

    let focus_hint = match state.focus_zone {
        FocusZone::CommandInput => "命令输入",
        FocusZone::Table => "表格 ↑↓滚动",
        FocusZone::InfoLog => "信息 ↑↓滚动",
        FocusZone::DetailLog => "日志 ↑↓ End=自动",
    };
    let hint_style = Style::default().fg(Color::Rgb(80, 80, 80)).add_modifier(Modifier::ITALIC);
    let hint = Paragraph::new(format!(" Tab:{}", focus_hint))
        .style(hint_style)
        .alignment(Alignment::Right);
    let hint_area = ratatui::layout::Rect {
        x: input_area.x + input_area.width.saturating_sub(46),
        y: input_area.y + 1,
        width: 44.min(input_area.width.saturating_sub(4)),
        height: 1,
    };
    frame.render_widget(hint, hint_area);
}
