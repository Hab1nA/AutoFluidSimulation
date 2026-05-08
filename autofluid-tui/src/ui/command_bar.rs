use ratatui::Frame;
use ratatui::style::{Color, Modifier, Style};
use ratatui::widgets::{Block, Borders, Paragraph};

use crate::state::app_state::{AppState, FocusZone};

pub fn render_command_bar(frame: &mut Frame, input_area: ratatui::layout::Rect, buttons_area: ratatui::layout::Rect, state: &AppState) {
    let input_style = if state.focus_zone == FocusZone::CommandInput {
        Style::default().fg(Color::Rgb(0, 255, 136)).bg(Color::Rgb(13, 13, 13))
    } else {
        Style::default().fg(Color::Rgb(100, 160, 100)).bg(Color::Rgb(13, 13, 13))
    };

    let cursor_indicator = if state.focus_zone == FocusZone::CommandInput { "▎" } else { " " };
    let input_text = format!("> {}{}", state.command_input, cursor_indicator);
    let paragraph = Paragraph::new(input_text)
        .style(input_style)
        .block(
            Block::default()
                .borders(Borders::ALL)
                .border_style(Style::default().fg(Color::Rgb(15, 52, 96))),
        );
    frame.render_widget(paragraph, input_area);

    let buttons_text = " [▶ Start] [⏸ Pause] [🔧 Check] [📊 Status] [🔧 Daemon] [⏹ Stop] [🚪 Quit] [⏹ FullQuit] ";
    let buttons = Paragraph::new(buttons_text)
        .style(Style::default().fg(Color::Rgb(224, 224, 224)).bg(Color::Rgb(15, 52, 96)));
    frame.render_widget(buttons, buttons_area);

    let focus_hint = match state.focus_zone {
        FocusZone::CommandInput => "命令输入",
        FocusZone::Table => "状态表格 (↑↓滚动)",
        FocusZone::InfoLog => "信息面板 (↑↓滚动)",
        FocusZone::DetailLog => "详细日志 (↑↓滚动, End=自动)",
    };
    let hint_style = Style::default().fg(Color::Rgb(100, 100, 100)).add_modifier(Modifier::ITALIC);
    let hint = Paragraph::new(format!(" Tab切换焦点: {} ", focus_hint))
        .style(hint_style);
    let hint_area = ratatui::layout::Rect {
        x: input_area.width.saturating_sub(30),
        y: input_area.y,
        width: 30.min(input_area.width),
        height: 1,
    };
    frame.render_widget(hint, hint_area);
}
