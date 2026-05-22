use ratatui::Frame;
use ratatui::layout::{Alignment, Constraint, Direction, Layout, Rect};
use ratatui::style::{Modifier, Style};
use ratatui::text::{Line, Span};
use ratatui::widgets::{Block, Borders, Paragraph};

use super::{SettingCategory, SettingsState};
use crate::ui::dialogs::{centered_rect, clear_dialog_background};
use crate::ui::scrollbar::VerticalScrollbar;
use crate::utils::{truncate_for_display, pad_label_by_display_width};
use crate::theme::AppTheme;

pub struct SettingsRenderInfo {
    pub content_total_lines: usize,
    pub content_visible_lines: usize,
    pub scrollbar_area: Rect,
    pub button_bar_y: u16,
    /// Map from (category_index, field_index) to y offset in content_area
    pub field_positions: Vec<(usize, usize, u16)>,
}

pub fn render_settings_dialog(
    frame: &mut Frame,
    area: Rect,
    ss: &SettingsState,
    hovered_dialog_button: Option<u8>,
    clicked_dialog_button: Option<u8>,
    theme: &AppTheme,
) -> SettingsRenderInfo {
    let dialog_area = centered_rect(90, 90, area);
    clear_dialog_background(frame, dialog_area);

    let block = Block::default()
        .borders(Borders::ALL)
        .border_style(Style::default().fg(theme.accent))
        .style(Style::default().bg(theme.bg));
    frame.render_widget(block, dialog_area);

    let inner = Rect {
        x: dialog_area.x + 1,
        y: dialog_area.y + 1,
        width: dialog_area.width.saturating_sub(2),
        height: dialog_area.height.saturating_sub(2),
    };

    // Title bar: title left-aligned, hint centered in remaining space
    let hint_text = if ss.focus.editing {
        "Enter 提交 | Esc 取消 | ←→ 移动光标 | Home/End 跳转 | Ctrl+A/X/C/V"
    } else {
        "↑↓ 滚动 | Tab 切换分类 | Enter 编辑 | Ctrl+Z 撤销 | Esc 关闭 | Ctrl+S 保存"
    };

    let title_text = "程序设置 (Settings)";
    let title_w = unicode_width::UnicodeWidthStr::width(title_text) as u16 + 2;

    let title_row = Rect { x: inner.x, y: inner.y, width: inner.width, height: 1 };
    let title_chunks = Layout::default()
        .direction(Direction::Horizontal)
        .constraints([
            Constraint::Length(title_w.min(inner.width)),
            Constraint::Min(1),
        ])
        .split(title_row);

    frame.render_widget(
        Paragraph::new(Line::from(Span::styled(
            format!(" {}", title_text),
            Style::default().fg(theme.fg).add_modifier(Modifier::BOLD),
        ))),
        title_chunks[0],
    );

    frame.render_widget(
        Paragraph::new(Line::from(Span::styled(
            hint_text,
            Style::default().fg(theme.muted),
        )))
        .alignment(Alignment::Right),
        title_chunks[1],
    );

    // Separator after title
    let sep_style = Style::default().fg(theme.gray_1);
    let sep = "─".repeat(inner.width as usize);
    let sep_y = inner.y + 1;
    frame.render_widget(
        Paragraph::new(Span::styled(&sep, sep_style)),
        Rect { x: inner.x, y: sep_y, width: inner.width, height: 1 },
    );

    // Layout: top(2 rows) + content + bottom(2 rows = separator + button)
    let top_height: u16 = 2;
    let bottom_height: u16 = 2;
    let content_top = inner.y + top_height;
    let content_height = inner.height.saturating_sub(top_height).saturating_sub(bottom_height);
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

    let label_width: u16 = 20;
    let field_content_width = content_area.width.saturating_sub(label_width).saturating_sub(2) as usize;

    let mut raw_lines: Vec<Line> = Vec::new();
    let mut field_positions: Vec<(usize, usize, u16)> = Vec::new();

    // 预计算子标题最大显示宽度，确保所有标题长度一致
    let target_header_w: usize = SettingCategory::ALL.iter().map(|c| {
        let hdr = format!("─── {} ──", c.display_name());
        unicode_width::UnicodeWidthStr::width(hdr.as_str())
    }).max().unwrap_or(20) + 2;

    for (cat_idx, cat) in SettingCategory::ALL.iter().enumerate() {
        raw_lines.push(Line::from(""));
        let header_str = format!("─── {} ──", cat.display_name());
        let header_dw = unicode_width::UnicodeWidthStr::width(header_str.as_str());
        let header_pad = if header_dw < target_header_w {
            "─".repeat(target_header_w - header_dw)
        } else {
            String::new()
        };
        raw_lines.push(Line::from(Span::styled(
            format!("  {}{}", header_str, header_pad),
            Style::default().fg(theme.success).add_modifier(Modifier::BOLD),
        )));
        raw_lines.push(Line::from(""));

        for fi in 0..cat.field_count() {
            let field_y = raw_lines.len() as u16;
            field_positions.push((cat_idx, fi, field_y));

            let label = cat.display_label(fi);
            let value = ss.get_field_value(*cat, fi);
            let field_name = make_field_name(*cat, fi);

            let is_focused = ss.focus.category_index == cat_idx && ss.focus.field_index == fi;
            let is_hovered = ss.hovered_field == Some((cat_idx, fi));
            let is_clicked = ss.clicked_field == Some((cat_idx, fi));
            let is_current_field = is_focused && ss.focus.editing;

            // Pre-compute row background
            let row_bg = if is_clicked {
                theme.secondary
            } else if is_hovered {
                theme.selection
            } else {
                theme.bg
            };

            let label_style = if is_focused {
                Style::default().fg(theme.success).add_modifier(Modifier::BOLD).bg(row_bg)
            } else {
                Style::default().fg(theme.gray_4).bg(row_bg)
            };

            let value_style = if is_current_field {
                Style::default().fg(theme.success).bg(row_bg)
            } else if is_focused {
                Style::default().fg(theme.fg).bg(theme.secondary)
            } else if is_hovered {
                Style::default().fg(theme.fg).bg(row_bg)
            } else {
                Style::default().fg(theme.gray_3).bg(row_bg)
            };

            // Selection highlight style (inverted: bright bg, dark fg)
            let sel_style = Style::default()
                .fg(theme.bg)
                .bg(theme.success);

            let mut spans: Vec<Span<'_>> = vec![
                Span::styled("  ", Style::default().bg(row_bg)),
                Span::styled(
                    pad_label_by_display_width(label, label_width),
                    label_style,
                ),
            ];

            if is_current_field {
                // Editing mode – use styled spans with selection highlight
                let edit_spans = build_edit_spans(
                    &ss.buffer.text,
                    ss.buffer.cursor,
                    ss.buffer.selection_anchor,
                    field_content_width,
                    value_style,
                    sel_style,
                    Style::default().fg(theme.cursor_fg).bg(theme.success).add_modifier(Modifier::BOLD),
                    Style::default().fg(theme.success),
                );
                spans.extend(edit_spans);
            } else {
                // Non-editing: plain string
                let display_value = if cat.is_password_field(fi) {
                    if value.is_empty() {
                        "(未设置)".to_string()
                    } else {
                        "*".repeat(12)
                    }
                } else if cat.is_bool_field(fi) {
                    if value == "true" { "是".to_string() } else { "否".to_string() }
                } else {
                    truncate_for_display(&value, field_content_width.saturating_sub(3))
                };
                spans.push(Span::styled(display_value, value_style));
            }

            // Path status check
            if let Some(exists) = ss.path_status.get(&field_name) {
                let (icon, color) = if *exists {
                    (" ✅", theme.success)
                } else {
                    (" ❌", theme.error)
                };
                spans.push(Span::styled(icon, Style::default().fg(color).bg(row_bg)));
            }

            // Validation error
            if let Some(err) = ss.validation_errors.iter().find(|e| e.field_name == field_name) {
                let color = if matches!(err.severity, super::validation::Severity::Error) {
                    theme.error
                } else {
                    theme.warning
                };
                spans.push(Span::styled(format!("  ⚠ {}", err.message), Style::default().fg(color).bg(row_bg)));
            }

            // Edit indicator
            if is_focused && !ss.focus.editing {
                let hint = if cat.is_bool_field(fi) {
                    "  [Enter 切换]"
                } else {
                    "  [Enter 编辑]"
                };
                spans.push(Span::styled(
                    hint,
                    Style::default().fg(theme.muted).bg(row_bg),
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
            track_color: Some(theme.scrollbar_track),
            thumb_color: Some(theme.scrollbar_thumb),
        };
        frame.render_widget(sb, scrollbar_area);
    }

    // Bottom separator
    let sep2_y = content_y + content_height;
    frame.render_widget(
        Paragraph::new(Span::styled(&sep, sep_style)),
        Rect { x: inner.x, y: sep2_y, width: inner.width, height: 1 },
    );

    // Button bar — btn_y = inner.y + inner.height - 1 (matches detect_dialog_button formula)
    let btn_y = inner.y + inner.height - 1;
    let save_label = " 保存更改 (Ctrl+S) ";
    let cancel_label = " 取消 (Esc) ";
    let gap: u16 = 4;

    let save_style = theme.dialog_btn_style(0, hovered_dialog_button, clicked_dialog_button);
    let cancel_style = theme.dialog_btn_style(1, hovered_dialog_button, clicked_dialog_button);

    let mut btn_spans = vec![
        Span::styled(save_label.to_string(), save_style),
        Span::styled(" ".repeat(gap as usize), Style::default().bg(theme.bg)),
        Span::styled(cancel_label.to_string(), cancel_style),
    ];

    if let Some(ref err) = ss.save_error {
        btn_spans.push(Span::raw("  "));
        btn_spans.push(Span::styled(err, Style::default().fg(theme.error)));
    } else if ss.saved {
        btn_spans.push(Span::raw("  "));
        btn_spans.push(Span::styled("✅ 已保存", Style::default().fg(theme.success)));
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
        field_positions,
    }
}

fn make_field_name(cat: SettingCategory, fi: usize) -> String {
    match cat {
        SettingCategory::LocalPaths => format!("local_paths.{}", cat.field_name(fi)),
        SettingCategory::RemoteConnection | SettingCategory::RemoteDirs => {
            format!("remote_config.{}", cat.field_name(fi))
        }
        SettingCategory::StepPatterns => format!("step_file_patterns.{}", cat.field_name(fi)),
        SettingCategory::SolidWorks => format!("solidworks.{}", cat.field_name(fi)),
        SettingCategory::SpaceClaim => format!("spaceclaim.{}", cat.field_name(fi)),
        SettingCategory::GlobalSettings => format!("global_settings.{}", cat.field_name(fi)),
    }
}

/// Build styled spans for the edit field display.
///
/// When a selection is active (`selection_anchor` is Some and differs from
/// `cursor`), the selected region is rendered with `sel_style`.  The
/// character at the cursor position is highlighted with `cursor_style`
/// (inverted colors); if the cursor is past the last character, a `▎`
/// indicator is appended.
#[allow(clippy::too_many_arguments)]
fn build_edit_spans(
    buffer: &str,
    cursor: usize,
    selection_anchor: Option<usize>,
    max_width: usize,
    base_style: Style,
    sel_style: Style,
    cursor_style: Style,
    cursor_bar_style: Style,
) -> Vec<Span<'static>> {
    if buffer.is_empty() {
        return vec![Span::styled("▎".to_string(), cursor_bar_style)];
    }

    // Determine selection range (start <= end)
    let sel: Option<(usize, usize)> = selection_anchor.and_then(|anchor| {
        if anchor == cursor {
            None
        } else if anchor < cursor {
            Some((anchor, cursor))
        } else {
            Some((cursor, anchor))
        }
    });

    let chars: Vec<char> = buffer.chars().collect();
    let len = chars.len();
    let mut spans: Vec<Span<'static>> = Vec::new();

    #[allow(clippy::needless_range_loop)] // i 用于 cursor/selection 比较，迭代器不更清晰
    for i in 0..len {
        let ch = chars[i];
        let style = if i == cursor {
            cursor_style
        } else if let Some((s, e)) = sel {
            if i >= s && i < e { sel_style } else { base_style }
        } else {
            base_style
        };
        spans.push(Span::styled(ch.to_string(), style));
    }
    // Cursor past last character → show bar indicator (plain fg, not inverted)
    if cursor >= len {
        spans.push(Span::styled("▎".to_string(), cursor_bar_style));
    }

    // Truncate to max_width by measuring display width of spans
    let mut total_w: usize = 0;
    let mut keep: usize = 0;
    for (idx, span) in spans.iter().enumerate() {
        let w = unicode_width::UnicodeWidthStr::width(span.content.as_ref() as &str);
        if total_w + w > max_width {
            break;
        }
        total_w += w;
        keep = idx + 1;
    }
    spans.truncate(keep);
    spans
}
