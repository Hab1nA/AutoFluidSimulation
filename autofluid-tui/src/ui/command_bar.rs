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
    ("🔧 Daemon", "daemon start"),
    ("⏹ Stop", "stop"),
    ("🚪 Quit", "quit"),
    ("⏹ FullQuit", "quit full"),
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

    let cursor_indicator = if state.focus_zone == FocusZone::CommandInput { "▎" } else { " " };
    let input_text = format!("> {}{}", state.command_input, cursor_indicator);

    let input_display_width = input_area.width.saturating_sub(2) as usize;
    let input_text_width = unicode_width::UnicodeWidthStr::width(input_text.as_str()) as usize;

    let scroll_x = if input_text_width > input_display_width {
        (input_text_width - input_display_width) as u16
    } else {
        0
    };

    let paragraph = Paragraph::new(input_text)
        .style(input_style)
        .alignment(Alignment::Left)
        .scroll((0, scroll_x))
        .block(
            Block::default()
                .borders(Borders::ALL)
                .border_style(Style::default().fg(Color::Rgb(15, 52, 96))),
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

    let total_width: u16 = BUTTON_DEFS.iter().map(|(l, _)| button_total_width(l) + 1).sum();
    let padding = buttons_area.width.saturating_sub(total_width) / 2;

    let mut padded_spans = vec![Span::styled(
        " ".repeat(padding as usize),
        Style::default().bg(Color::Rgb(15, 52, 96))
    )];
    padded_spans.extend(spans);

    let buttons_line = Line::from(padded_spans);
    let buttons = Paragraph::new(vec![buttons_line])
        .style(Style::default().bg(Color::Rgb(15, 52, 96)))
        .alignment(Alignment::Center);
    frame.render_widget(buttons, buttons_area);

    let focus_hint = match state.focus_zone {
        FocusZone::CommandInput => "命令输入",
        FocusZone::Table => "状态表格 (↑↓滚动)",
        FocusZone::InfoLog => "信息面板 (↑↓滚动)",
        FocusZone::DetailLog => "详细日志 (↑↓滚动, End=自动)",
    };
    let hint_style = Style::default().fg(Color::Rgb(80, 80, 80)).add_modifier(Modifier::ITALIC);
    let hint = Paragraph::new(format!(" Tab:{}", focus_hint))
        .style(hint_style)
        .alignment(Alignment::Right);
    let hint_area = ratatui::layout::Rect {
        x: input_area.x + input_area.width.saturating_sub(28),
        y: input_area.y + 1,
        width: 26.min(input_area.width.saturating_sub(4)),
        height: 1,
    };
    frame.render_widget(hint, hint_area);
}
