use ratatui::Frame;
use ratatui::layout::{Alignment, Rect};
use ratatui::style::{Color, Modifier, Style};
use ratatui::text::{Line, Span};
use ratatui::widgets::{Block, Borders, Clear, Paragraph};

use super::{SettingCategory, SettingsState};
use crate::ui::dialogs::centered_rect;
use crate::ui::scrollbar::VerticalScrollbar;

const DIALOG_BG: Color = Color::Rgb(22, 33, 62);
const ACCENT_RED: Color = Color::Rgb(233, 69, 96);
const GREEN: Color = Color::Rgb(0, 255, 136);
const FIELD_LABEL: Color = Color::Rgb(200, 200, 200);
const FIELD_VALUE: Color = Color::Rgb(180, 180, 180);
const SECTION_HEADER: Color = Color::Rgb(0, 255, 136);
const HINT_COLOR: Color = Color::Rgb(80, 80, 80);
const ERROR_COLOR: Color = Color::Rgb(255, 69, 58);
const WARN_COLOR: Color = Color::Rgb(255, 170, 0);

const BTN_NORMAL: Style = Style::new()
    .fg(Color::Rgb(224, 224, 224))
    .bg(Color::Rgb(15, 52, 96));
const BTN_HOVER: Style = Style::new()
    .fg(Color::Rgb(255, 255, 255))
    .bg(Color::Rgb(233, 69, 96))
    .add_modifier(Modifier::BOLD);
const BTN_CLICKED: Style = Style::new()
    .fg(Color::Rgb(0, 0, 0))
    .bg(Color::Rgb(255, 255, 255))
    .add_modifier(Modifier::BOLD);

pub struct SettingsRenderInfo {
    pub content_total_lines: usize,
    pub content_visible_lines: usize,
    pub scrollbar_area: Rect,
    pub button_bar_y: u16,
}

fn btn_style(idx: u8, hovered: Option<u8>, clicked: Option<u8>) -> Style {
    if clicked == Some(idx) {
        BTN_CLICKED
    } else if hovered == Some(idx) {
        BTN_HOVER
    } else {
        BTN_NORMAL
    }
}

pub fn render_settings_dialog(
    frame: &mut Frame,
    area: Rect,
    ss: &SettingsState,
    hovered_dialog_button: Option<u8>,
    clicked_dialog_button: Option<u8>,
) -> SettingsRenderInfo {
    let dialog_area = centered_rect(90, 90, area);
    frame.render_widget(Clear, dialog_area);

    let block = Block::default()
        .borders(Borders::ALL)
        .border_style(Style::default().fg(ACCENT_RED))
        .style(Style::default().bg(DIALOG_BG));
    frame.render_widget(block, dialog_area);

    let inner = Rect {
        x: dialog_area.x + 1,
        y: dialog_area.y + 1,
        width: dialog_area.width.saturating_sub(2),
        height: dialog_area.height.saturating_sub(2),
    };

    // Title bar
    let title_style = Style::default().fg(Color::White).add_modifier(Modifier::BOLD);
    let title = Line::from(vec![
        Span::styled(" 程序设置 (Settings)", title_style),
    ]);
    frame.render_widget(
        Paragraph::new(title),
        Rect { x: inner.x, y: inner.y, width: inner.width, height: 1 },
    );

    // Separator after title
    let sep_style = Style::default().fg(Color::Rgb(60, 60, 60));
    let sep = "─".repeat(inner.width as usize);
    let sep_y = inner.y + 1;
    frame.render_widget(
        Paragraph::new(Span::styled(&sep, sep_style)),
        Rect { x: inner.x, y: sep_y, width: inner.width, height: 1 },
    );

    // Hint bar at bottom
    let hint_bar_height: u16 = 2;
    let btn_bar_height: u16 = 2;
    let bottom_height = hint_bar_height + btn_bar_height;
    let content_top = sep_y + 1;
    let content_height = inner.height.saturating_sub(2).saturating_sub(bottom_height);
    let content_y = content_top;

    let content_area = Rect {
        x: inner.x,
        y: content_y,
        width: inner.width.saturating_sub(1),
        height: content_height,
    };

    let scrollbar_area = Rect {
        x: inner.x + inner.width.saturating_sub(1),
        y: content_y,
        width: 1,
        height: content_height,
    };

    // Build content lines with padding
    let label_width: u16 = 18;
    let field_content_width = content_area.width.saturating_sub(label_width).saturating_sub(2) as usize;

    let mut raw_lines: Vec<Line> = Vec::new();

    for (cat_idx, cat) in SettingCategory::ALL.iter().enumerate() {
        // Section header
        raw_lines.push(Line::from(""));
        let header_str = format!("─── {} ──", cat.display_name());
        let header_pad = "─".repeat((content_area.width as usize).saturating_sub(header_str.len()).min(20));
        raw_lines.push(Line::from(Span::styled(
            format!("  {}{}", header_str, header_pad),
            Style::default().fg(SECTION_HEADER).add_modifier(Modifier::BOLD),
        )));
        raw_lines.push(Line::from(""));

        for fi in 0..cat.field_count() {
            let label = cat.display_label(fi);
            let value = ss.get_field_value(*cat, fi);

            let is_focused = ss.focus.category_index == cat_idx && ss.focus.field_index == fi;
            let is_current_field = is_focused && ss.focus.editing;

            let display_value = if cat.is_password_field(fi) && !is_current_field {
                if value.is_empty() {
                    "(未设置)".to_string()
                } else {
                    "*".repeat(value.len().min(12))
                }
            } else if is_current_field {
                build_edit_display(&ss.edit_buffer, ss.edit_cursor, field_content_width)
            } else if cat.is_bool_field(fi) {
                if value == "true" { "是".to_string() } else { "否".to_string() }
            } else {
                truncate_for_display(&value, field_content_width.saturating_sub(3))
            };

            let label_style = if is_focused {
                Style::default().fg(GREEN).add_modifier(Modifier::BOLD)
            } else {
                Style::default().fg(FIELD_LABEL)
            };

            let value_style = if is_current_field {
                Style::default().fg(GREEN)
            } else if is_focused {
                Style::default().fg(Color::White).bg(Color::Rgb(15, 52, 96))
            } else {
                Style::default().fg(FIELD_VALUE)
            };

            // Check for validation errors on this field
            let field_name = match cat {
                SettingCategory::LocalPaths => format!("local_paths.{}", cat.field_name(fi)),
                SettingCategory::RemoteConnection | SettingCategory::RemoteDirs => {
                    format!("remote_config.{}", cat.field_name(fi))
                }
                SettingCategory::StepPatterns => format!("step_file_patterns.{}", cat.field_name(fi)),
                SettingCategory::EngineConfig => format!("engine_config.{}", cat.field_name(fi)),
            };

            let mut spans = vec![
                Span::raw("  "),
                Span::styled(
                    format!("{:<width$}", label, width = label_width as usize),
                    label_style,
                ),
                Span::styled(display_value, value_style),
            ];

            if let Some(err) = ss.validation_errors.iter().find(|e| e.field_name == field_name) {
                let color = if matches!(err.severity, super::validation::Severity::Error) {
                    ERROR_COLOR
                } else {
                    WARN_COLOR
                };
                spans.push(Span::styled(format!("  ⚠ {}", err.message), Style::default().fg(color)));
            }

            // Show edit indicator for focused non-editing field
            if is_focused && !ss.focus.editing {
                spans.push(Span::styled(
                    "  [Enter 编辑]",
                    Style::default().fg(HINT_COLOR),
                ));
            }

            raw_lines.push(Line::from(spans));
        }
    }

    let total_lines = raw_lines.len();
    let visible = content_height as usize;
    let max_scroll = total_lines.saturating_sub(visible);
    let scroll_offset = (ss.scroll as usize).min(max_scroll);

    let paragraph = Paragraph::new(raw_lines)
        .alignment(Alignment::Left)
        .scroll((scroll_offset as u16, 0));
    frame.render_widget(paragraph, content_area);

    if total_lines > visible {
        let sb = VerticalScrollbar {
            total: total_lines,
            visible,
            scroll: scroll_offset,
        };
        frame.render_widget(sb, scrollbar_area);
    }

    // Separator above hint bar
    let sep2_y = content_y + content_height;
    frame.render_widget(
        Paragraph::new(Span::styled(&sep, sep_style)),
        Rect { x: inner.x, y: sep2_y, width: inner.width, height: 1 },
    );

    // Hint bar
    let hint_y = sep2_y + 1;
    let hint_text = if ss.focus.editing {
        " Enter提交 Esc取消 ←→移动光标 Home/End跳转 "
    } else {
        " ↑↓导航 Tab跳转分类 Enter编辑 Ctrl+Z撤销 Esc退出 Ctrl+S保存 "
    };

    let hint_style = Style::default().fg(HINT_COLOR);
    frame.render_widget(
        Paragraph::new(Span::styled(hint_text, hint_style)).alignment(Alignment::Center),
        Rect { x: inner.x, y: hint_y, width: inner.width, height: 1 },
    );

    // Button bar
    let btn_y = hint_y + 1;
    let save_label = " 保存更改 (Ctrl+S) ";
    let cancel_label = " 取消 (Esc) ";
    let gap: u16 = 4;

    let save_style = btn_style(0, hovered_dialog_button, clicked_dialog_button);
    let cancel_style = btn_style(1, hovered_dialog_button, clicked_dialog_button);

    let mut btn_spans = vec![
        Span::styled(save_label.to_string(), save_style),
        Span::styled(" ".repeat(gap as usize), Style::default().bg(DIALOG_BG)),
        Span::styled(cancel_label.to_string(), cancel_style),
    ];

    // Show save status
    if let Some(ref err) = ss.save_error {
        btn_spans.push(Span::raw("  "));
        btn_spans.push(Span::styled(err, Style::default().fg(ERROR_COLOR)));
    } else if ss.saved {
        btn_spans.push(Span::raw("  "));
        btn_spans.push(Span::styled("✅ 已保存", Style::default().fg(GREEN)));
    }

    frame.render_widget(
        Paragraph::new(Line::from(btn_spans)).alignment(Alignment::Center),
        Rect { x: inner.x, y: btn_y, width: inner.width, height: 1 },
    );

    SettingsRenderInfo {
        content_total_lines: total_lines,
        content_visible_lines: visible,
        scrollbar_area,
        button_bar_y: btn_y,
    }
}

fn build_edit_display(buffer: &str, cursor: usize, max_width: usize) -> String {
    let display = if buffer.is_empty() {
        "▎".to_string()
    } else {
        let before: String = buffer.chars().take(cursor).collect();
        let cursor_char = buffer.chars().nth(cursor).unwrap_or(' ');
        let after: String = buffer.chars().skip(cursor + 1).collect();
        format!("{}│{}", before, format!("{}{}", cursor_char, after))
    };
    truncate_for_display(&display, max_width)
}

fn truncate_for_display(s: &str, max_width: usize) -> String {
    if max_width == 0 {
        return String::new();
    }
    let width = unicode_width::UnicodeWidthStr::width(s);
    if width <= max_width {
        return s.to_string();
    }
    let mut result = String::new();
    let mut current_width = 0;
    for c in s.chars() {
        let cw = unicode_width::UnicodeWidthChar::width(c).unwrap_or(0);
        if current_width + cw + 3 > max_width {
            result.push_str("...");
            break;
        }
        result.push(c);
        current_width += cw;
    }
    if result.is_empty() {
        result = s.chars().take(3).collect();
        result.push_str("...");
    }
    result
}
