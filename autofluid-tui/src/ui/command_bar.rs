use ratatui::Frame;
use ratatui::layout::{Alignment, Rect};
use ratatui::style::{Color, Modifier, Style};
use ratatui::text::{Line, Span};
use ratatui::widgets::{Block, Borders, Clear, Paragraph};

use crate::state::app_state::{AppState, FocusZone};

pub const BUTTON_DEFS: [(&str, &str); 8] = [
    ("▶ Start", "start"),
    ("⏸ Pause", "pause"),
    ("⚙ Settings", "settings"),
    ("🔧 Check", "check"),
    ("📊 Status", "status"),
    ("😈 Daemon", "daemon"),
    ("🚪 Quit", "quit"),
    ("⏹ Quit Full", "quit full"),
];

pub const DAEMON_MENU_ITEMS: [(&str, &str); 3] = [
    ("start", "daemon start"),
    ("stop", "daemon stop"),
    ("restart", "daemon restart"),
];

pub fn button_display_width(label: &str) -> u16 {
    unicode_width::UnicodeWidthStr::width(label) as u16
}

pub fn button_total_width(label: &str) -> u16 {
    button_display_width(label) + 2
}

fn point_in_rect(col: u16, row: u16, rect: Rect) -> bool {
    col >= rect.x
        && col < rect.x + rect.width
        && row >= rect.y
        && row < rect.y + rect.height
}

pub fn button_bounds(buttons_area: Rect, index: usize) -> Option<Rect> {
    let total_btn_width: u16 = BUTTON_DEFS.iter().map(|(l, _)| button_total_width(l) + 1).sum();
    let padding = buttons_area.width.saturating_sub(total_btn_width) / 2;

    let mut offset = padding;
    for (i, (label, _)) in BUTTON_DEFS.iter().enumerate() {
        let bw = button_total_width(label) + 1;
        if i == index {
            return Some(Rect {
                x: buttons_area.x + offset,
                y: buttons_area.y + 1,
                width: bw,
                height: 1,
            });
        }
        offset += bw;
    }
    None
}

pub fn daemon_button_index() -> Option<usize> {
    BUTTON_DEFS.iter().position(|(_, cmd)| *cmd == "daemon")
}

pub fn daemon_button_bounds(buttons_area: Rect) -> Option<Rect> {
    daemon_button_index().and_then(|idx| button_bounds(buttons_area, idx))
}

pub fn daemon_menu_bounds(buttons_area: Rect) -> Option<Rect> {
    let button = daemon_button_bounds(buttons_area)?;
    let menu_width = DAEMON_MENU_ITEMS
        .iter()
        .map(|(label, _)| unicode_width::UnicodeWidthStr::width(*label) as u16)
        .max()
        .unwrap_or(6)
        + 4;
    let menu_height = DAEMON_MENU_ITEMS.len() as u16 + 2;
    let x = button
        .x
        .saturating_add(button.width.saturating_div(2))
        .saturating_sub(menu_width.saturating_div(2));
    // 按钮文本从 button.x+1 开始（format " {} " 的前导空格），菜单文本从 menu.x+2 开始（边框+空格）
    // 为使二者对齐，菜单整体左移 1 格
    let menu_x = x.saturating_sub(1).min(buttons_area.x + buttons_area.width.saturating_sub(menu_width));
    let menu_y = buttons_area.y.saturating_sub(menu_height).saturating_add(1);  // 下移一行紧贴按钮
    Some(Rect {
        x: menu_x,
        y: menu_y,
        width: menu_width,
        height: menu_height,
    })
}

pub fn daemon_menu_item_bounds(buttons_area: Rect, index: usize) -> Option<Rect> {
    let menu = daemon_menu_bounds(buttons_area)?;
    if index >= DAEMON_MENU_ITEMS.len() {
        return None;
    }
    Some(Rect {
        x: menu.x + 1,
        y: menu.y + 1 + index as u16,
        width: menu.width.saturating_sub(2),
        height: 1,
    })
}

pub fn detect_daemon_menu_item(col: u16, row: u16, buttons_area: Rect) -> Option<u8> {
    daemon_menu_bounds(buttons_area)?;
    for idx in 0..DAEMON_MENU_ITEMS.len() {
        if let Some(item_rect) = daemon_menu_item_bounds(buttons_area, idx) {
            if point_in_rect(col, row, item_rect) {
                return Some(idx as u8);
            }
        }
    }
    None
}

pub fn daemon_menu_command(index: u8) -> Option<&'static str> {
    DAEMON_MENU_ITEMS.get(index as usize).map(|(_, cmd)| *cmd)
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
    let before_width = unicode_width::UnicodeWidthStr::width(before_text.as_str());
    let cursor_display_w = cursor_char
        .and_then(unicode_width::UnicodeWidthChar::width)
        .unwrap_or(0);
    let after_width = unicode_width::UnicodeWidthStr::width(after_cursor.as_str());
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

    if state.daemon_menu_open {
        if let Some(menu_area) = daemon_menu_bounds(buttons_area) {
            frame.render_widget(Clear, menu_area);
            let block = Block::default()
                .borders(Borders::ALL)
                .border_style(Style::default().fg(Color::Rgb(233, 69, 96)))
                .style(Style::default().bg(Color::Rgb(22, 33, 62)));
            frame.render_widget(block, menu_area);

            let mut lines = Vec::new();
            for (idx, (label, _)) in DAEMON_MENU_ITEMS.iter().enumerate() {
                let hovered = state.hovered_daemon_menu_item == Some(idx as u8);
                let clicked = state.clicked_daemon_menu_item == Some(idx as u8);
                let style = if clicked {
                    Style::default().fg(Color::Rgb(0, 0, 0)).bg(Color::Rgb(255, 255, 255)).add_modifier(Modifier::BOLD)
                } else if hovered {
                    Style::default().fg(Color::Rgb(255, 255, 255)).bg(Color::Rgb(233, 69, 96)).add_modifier(Modifier::BOLD)
                } else {
                    Style::default().fg(Color::Rgb(224, 224, 224)).bg(Color::Rgb(22, 33, 62))
                };
                lines.push(Line::from(Span::styled(format!(" {:<8} ", label), style)));
            }
            frame.render_widget(
                Paragraph::new(lines).alignment(Alignment::Left),
                Rect {
                    x: menu_area.x + 1,
                    y: menu_area.y + 1,
                    width: menu_area.width.saturating_sub(2),
                    height: menu_area.height.saturating_sub(2),
                },
            );
        }
    }

}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_daemon_button_and_menu_mapping() {
        assert_eq!(daemon_button_index(), Some(5));
        assert_eq!(daemon_menu_command(0), Some("daemon start"));
        assert_eq!(daemon_menu_command(1), Some("daemon stop"));
        assert_eq!(daemon_menu_command(2), Some("daemon restart"));
        assert_eq!(DAEMON_MENU_ITEMS[0].0, "start");
        assert_eq!(DAEMON_MENU_ITEMS[1].0, "stop");
        assert_eq!(DAEMON_MENU_ITEMS[2].0, "restart");
    }
}
