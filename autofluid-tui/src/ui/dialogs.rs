use ratatui::layout::{Alignment, Constraint, Direction, Layout, Rect};
use ratatui::style::{Modifier, Style};
use ratatui::text::{Line, Span};
use ratatui::widgets::{Block, Borders, Clear, Paragraph};
use ratatui::Frame;

use crate::theme::AppTheme;
use crate::ui::scrollbar::VerticalScrollbar;
use crate::utils::{pad_label_by_display_width, truncate_for_display};

fn symbol_is_wide(symbol: &str) -> bool {
    unicode_width::UnicodeWidthStr::width(symbol) > 1
}

/// 清除对话框背景并处理 forbidden render margin：
/// 主体区域精确清除，左侧 1 格禁止宽字符渲染，防止其右半身侵入边框
pub fn clear_dialog_background(frame: &mut Frame, dialog_area: Rect) {
    // 主体区域精确清除（与弹窗区域一致，无扩展）
    frame.render_widget(Clear, dialog_area);

    // Forbidden render margin：弹窗左侧 1 格中的宽字符（中文/emoji）
    // 起始位置在此列时会跨越侵入弹窗边框，需禁止渲染
    if dialog_area.x > 0 {
        let fx = dialog_area.x - 1;
        let buffer = frame.buffer_mut();
        let max_y = (dialog_area.y + dialog_area.height).min(buffer.area().height);
        for y in dialog_area.y..max_y {
            if let Some(cell) = buffer.cell_mut((fx, y)) {
                if symbol_is_wide(cell.symbol()) {
                    cell.set_char(' '); // 仅清除文字，保留原面板背景色
                }
            }
        }
    }
}

/// 对话框渲染信息
///
/// - content_total_lines: 内容总行数
/// - content_visible_lines: 可见行数
/// - scrollbar_area: 滚动条区域
/// - button_bar_y: 按钮栏Y坐标
pub struct DialogRenderInfo {
    pub content_total_lines: usize,
    pub content_visible_lines: usize,
    pub scrollbar_area: Rect,
    pub button_bar_y: u16,
}

pub fn render_confirm_dialog(
    frame: &mut Frame,
    area: Rect,
    message: &str,
    scroll: u16,
    hovered_dialog_button: Option<u8>,
    clicked_dialog_button: Option<u8>,
    theme: &AppTheme,
) -> DialogRenderInfo {
    let dialog_area = centered_rect(80, 40, area);
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

    let btn_bar_height: u16 = 2;
    let content_height = inner.height.saturating_sub(btn_bar_height);

    let content_area = Rect {
        x: inner.x,
        y: inner.y,
        width: inner.width.saturating_sub(1),
        height: content_height,
    };

    let scrollbar_area = Rect {
        x: inner.x + inner.width.saturating_sub(1),
        y: inner.y,
        width: 1,
        height: content_height,
    };

    let msg_lines: Vec<Line> = message
        .lines()
        .map(|line| {
            Line::from(Span::styled(
                line.to_string(),
                Style::default()
                    .fg(theme.warning)
                    .add_modifier(Modifier::BOLD),
            ))
        })
        .collect();

    let msg_line_count = msg_lines.len();
    let visible = content_height as usize;

    let (content_lines, total_lines, scroll_offset) = if msg_line_count <= visible {
        let top_pad = visible.saturating_sub(msg_line_count) / 2;
        let mut lines: Vec<Line> = Vec::new();
        for _ in 0..top_pad {
            lines.push(Line::from(""));
        }
        lines.extend(msg_lines);
        let total = lines.len();
        (lines, total, 0usize)
    } else {
        let max_scroll = msg_line_count - visible;
        let so = (scroll as usize).min(max_scroll);
        (msg_lines, msg_line_count, so)
    };

    let paragraph = Paragraph::new(content_lines)
        .alignment(Alignment::Center)
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

    let separator_y = inner.y + content_height;
    let separator: String = "─".repeat(inner.width as usize);
    frame.render_widget(
        Paragraph::new(Span::styled(separator, Style::default().fg(theme.gray_1))),
        Rect {
            x: inner.x,
            y: separator_y,
            width: inner.width,
            height: 1,
        },
    );

    let btn_y = inner.y + content_height + 1;
    let confirm_label = " 确认 [Y] ";
    let cancel_label = " 取消 [N] ";
    let gap: u16 = 3;
    let confirm_style = theme.dialog_btn_style(0, hovered_dialog_button, clicked_dialog_button);
    let cancel_style = theme.dialog_btn_style(1, hovered_dialog_button, clicked_dialog_button);

    let btn_line = Line::from(vec![
        Span::styled(confirm_label.to_string(), confirm_style),
        Span::styled(" ".repeat(gap as usize), Style::default().bg(theme.bg)),
        Span::styled(cancel_label.to_string(), cancel_style),
    ]);

    frame.render_widget(
        Paragraph::new(btn_line).alignment(Alignment::Center),
        Rect {
            x: inner.x,
            y: btn_y,
            width: inner.width,
            height: 1,
        },
    );

    DialogRenderInfo {
        content_total_lines: total_lines,
        content_visible_lines: visible,
        scrollbar_area,
        button_bar_y: btn_y,
    }
}

fn build_check_content_lines(
    data: &serde_json::Value,
    _content_width: usize,
    theme: &AppTheme,
) -> Vec<Line<'static>> {
    let label_width: u16 = 20;

    struct CheckItem {
        label: String,
        value: String,
        exists: Option<bool>,
    }

    let mut raw_lines: Vec<Line> = Vec::new();

    // ---- 本地环境检查 ----
    let daemon_header = "─── Daemon 检查 ──";
    let local_header = "─── 本地环境检查 ──";
    let local_worker_header = "─── LocalWorker 本地环境检查 ──";
    let workstation_header = "─── 工作站配置检查 ──";
    let remote_header = "─── 远程工作站检查 ──";
    let target_header_w = {
        [
            daemon_header,
            local_header,
            local_worker_header,
            workstation_header,
            remote_header,
        ]
        .iter()
        .map(|header| unicode_width::UnicodeWidthStr::width(*header))
        .max()
        .unwrap_or(0)
            + 2
    };

    let local_items: Vec<CheckItem> =
        if let Some(local) = data.get("local_checks").and_then(|v| v.as_object()) {
            let keys_in_order = [
                "SW可执行文件",
                "SW模型文件",
                "Excel参数表",
                "STEP输出目录",
                "SC可执行文件",
                "SCDOC输出目录",
            ];
            keys_in_order
                .iter()
                .filter_map(|name| {
                    local.get(*name).map(|info| {
                        let exists = info.get("exists").and_then(|v| v.as_bool());
                        let path = info
                            .get("path")
                            .and_then(|v| v.as_str())
                            .unwrap_or("")
                            .to_string();
                        let value = if path.is_empty() {
                            "(未设置)".to_string()
                        } else {
                            path.clone()
                        };
                        CheckItem {
                            label: name.to_string(),
                            value,
                            exists,
                        }
                    })
                })
                .collect()
        } else {
            Vec::new()
        };

    // ---- 远程工作站检查 ----
    let mut conn_items: Vec<CheckItem> = Vec::new();
    let mut dir_items: Vec<CheckItem> = Vec::new();
    let mut prog_items: Vec<CheckItem> = Vec::new();
    let mut sys_items: Vec<CheckItem> = Vec::new();
    let mut scripts_info: Option<(usize, usize, Vec<String>)> = None;
    let mut ref_files_info: Option<(usize, usize, Vec<String>)> = None;

    if let Some(remote) = data.get("remote_checks").and_then(|v| v.as_object()) {
        // 连接状态
        if let Some(ssh_status) = remote.get("ssh").and_then(|v| v.as_str()) {
            let ok = ssh_status.contains("成功");
            conn_items.push(CheckItem {
                label: "SSH连接".to_string(),
                value: ssh_status.to_string(),
                exists: Some(ok),
            });
        }

        // 远程目录（从 Python 端传入的结构化数据）
        if let Some(dirs) = remote.get("remote_dirs").and_then(|v| v.as_array()) {
            for d in dirs {
                let label = d
                    .get("label")
                    .and_then(|v| v.as_str())
                    .unwrap_or("")
                    .to_string();
                let path = d
                    .get("path")
                    .and_then(|v| v.as_str())
                    .unwrap_or("")
                    .to_string();
                let exists = d.get("exists").and_then(|v| v.as_bool());
                let value = if path.is_empty() {
                    "(未设置)".to_string()
                } else {
                    path
                };
                dir_items.push(CheckItem {
                    label,
                    value,
                    exists,
                });
            }
        }

        // 远程程序（Conda、MPI 等）
        if let Some(progs) = remote.get("remote_programs").and_then(|v| v.as_array()) {
            for p in progs {
                let label = p
                    .get("label")
                    .and_then(|v| v.as_str())
                    .unwrap_or("")
                    .to_string();
                let path = p
                    .get("path")
                    .and_then(|v| v.as_str())
                    .unwrap_or("")
                    .to_string();
                let exists = p.get("exists").and_then(|v| v.as_bool());
                let value = if path.is_empty() {
                    "(未设置)".to_string()
                } else {
                    path
                };
                prog_items.push(CheckItem {
                    label,
                    value,
                    exists,
                });
            }
        }

        // 脚本部署状态
        if let Some(scripts) = remote.get("scripts_status") {
            let total = scripts.get("total").and_then(|v| v.as_u64()).unwrap_or(0) as usize;
            let deployed = scripts
                .get("deployed")
                .and_then(|v| v.as_u64())
                .unwrap_or(0) as usize;
            let missing: Vec<String> = scripts
                .get("missing")
                .and_then(|v| v.as_array())
                .map(|arr| {
                    arr.iter()
                        .filter_map(|v| v.as_str().map(String::from))
                        .collect()
                })
                .unwrap_or_default();
            scripts_info = Some((total, deployed, missing));
        }

        // 引用文件部署状态
        if let Some(refs) = remote.get("ref_files_status") {
            let total = refs.get("total").and_then(|v| v.as_u64()).unwrap_or(0) as usize;
            let deployed = refs.get("deployed").and_then(|v| v.as_u64()).unwrap_or(0) as usize;
            let missing: Vec<String> = refs
                .get("missing")
                .and_then(|v| v.as_array())
                .map(|arr| {
                    arr.iter()
                        .filter_map(|v| v.as_str().map(String::from))
                        .collect()
                })
                .unwrap_or_default();
            ref_files_info = Some((total, deployed, missing));
        }

        // 系统信息
        if let Some(py_ver) = remote.get("python_version").and_then(|v| v.as_str()) {
            let ok = !py_ver.is_empty();
            sys_items.push(CheckItem {
                label: "Python版本".to_string(),
                value: if ok {
                    py_ver.to_string()
                } else {
                    "未安装或无法检测".to_string()
                },
                exists: Some(ok),
            });
        }
        if let Some(disk) = remote.get("disk_space").and_then(|v| v.as_str()) {
            sys_items.push(CheckItem {
                label: "磁盘空间".to_string(),
                value: disk.to_string(),
                exists: None,
            });
        }
        if let Some(procs) = remote
            .get("background_processes")
            .and_then(|v| v.as_array())
        {
            let proc_list: Vec<&str> = procs.iter().map(|v| v.as_str().unwrap_or("?")).collect();
            let display = if proc_list.is_empty() {
                "无".to_string()
            } else {
                proc_list.join(", ")
            };
            sys_items.push(CheckItem {
                label: "后台进程".to_string(),
                value: display,
                exists: None,
            });
        }
    }

    fn json_value_to_display(value: &serde_json::Value) -> String {
        if let Some(text) = value.as_str() {
            text.to_string()
        } else if let Some(flag) = value.as_bool() {
            if flag {
                "是".to_string()
            } else {
                "否".to_string()
            }
        } else if value.is_number() {
            value.to_string()
        } else if value.is_null() {
            "未设置".to_string()
        } else {
            value.to_string()
        }
    }

    fn object_entry_to_check_item(label: &str, info: &serde_json::Value) -> CheckItem {
        let exists = info
            .get("exists")
            .and_then(|v| v.as_bool())
            .or_else(|| info.get("ok").and_then(|v| v.as_bool()));
        let value = info
            .get("path")
            .and_then(|v| v.as_str())
            .filter(|path| !path.is_empty())
            .map(str::to_string)
            .or_else(|| {
                info.get("message")
                    .and_then(|v| v.as_str())
                    .filter(|message| !message.is_empty())
                    .map(str::to_string)
            })
            .or_else(|| info.get("value").map(json_value_to_display))
            .unwrap_or_else(|| match exists {
                Some(true) => "正常".to_string(),
                Some(false) => "异常".to_string(),
                None => json_value_to_display(info),
            });
        CheckItem {
            label: label.to_string(),
            value,
            exists,
        }
    }

    fn object_to_check_items(
        object: &serde_json::Map<String, serde_json::Value>,
    ) -> Vec<CheckItem> {
        object
            .iter()
            .map(|(label, info)| object_entry_to_check_item(label, info))
            .collect()
    }

    let daemon_items: Vec<CheckItem> = data
        .get("daemon_checks")
        .and_then(|v| v.as_object())
        .map(object_to_check_items)
        .unwrap_or_default();

    let local_worker_items: Vec<CheckItem> = data
        .get("local_worker_checks")
        .and_then(|v| v.as_object())
        .map(object_to_check_items)
        .unwrap_or_default();

    let mut workstation_items: Vec<CheckItem> = Vec::new();
    if let Some(workstation_checks) = data.get("workstation_checks").and_then(|v| v.as_object()) {
        if let Some(server_mode) = workstation_checks
            .get("server_mode")
            .and_then(|v| v.as_bool())
        {
            workstation_items.push(CheckItem {
                label: "server_mode".to_string(),
                value: if server_mode {
                    "启用".to_string()
                } else {
                    "关闭".to_string()
                },
                exists: Some(true),
            });
        }
        if let Some(workstations) = workstation_checks
            .get("workstations")
            .and_then(|v| v.as_array())
        {
            for workstation in workstations {
                let workstation_id = workstation
                    .get("id")
                    .and_then(|v| v.as_str())
                    .unwrap_or("unknown");
                let host = workstation
                    .get("effective_host")
                    .or_else(|| workstation.get("host"))
                    .and_then(|v| v.as_str())
                    .unwrap_or("");
                let port = workstation
                    .get("port")
                    .and_then(|v| v.as_u64())
                    .map(|value| value.to_string())
                    .unwrap_or_else(|| "22".to_string());
                let mode = workstation
                    .get("connectivity_mode")
                    .and_then(|v| v.as_str())
                    .unwrap_or("");
                let severity = workstation
                    .get("severity")
                    .and_then(|v| v.as_str())
                    .unwrap_or("ok");
                let exists = match severity {
                    "ok" => Some(true),
                    "warning" | "error" => Some(false),
                    _ => None,
                };
                let mut value = if host.is_empty() {
                    "(未设置)".to_string()
                } else {
                    format!("{host}:{port}")
                };
                if !mode.is_empty() {
                    value.push_str(&format!(" ({mode})"));
                }
                workstation_items.push(CheckItem {
                    label: format!("工作站 {workstation_id}"),
                    value,
                    exists,
                });

                if let Some(warning) = workstation
                    .get("warning")
                    .and_then(|v| v.as_str())
                    .filter(|warning| !warning.is_empty())
                {
                    workstation_items.push(CheckItem {
                        label: format!("告警 {workstation_id}"),
                        value: warning.to_string(),
                        exists: Some(false),
                    });
                }
            }
        }
    }

    // ---- 渲染辅助函数 ----
    fn render_items(
        raw_lines: &mut Vec<Line>,
        items: &[CheckItem],
        label_width: u16,
        theme: &AppTheme,
    ) {
        for item in items {
            let icon = match item.exists {
                Some(true) => (" ✅", theme.success),
                Some(false) => (" ❌", theme.error),
                None => ("", theme.bg),
            };
            let mut spans = vec![
                Span::styled("  ", Style::default().bg(theme.bg)),
                Span::styled(
                    pad_label_by_display_width(&item.label, label_width),
                    Style::default().fg(theme.gray_4).bg(theme.bg),
                ),
                Span::styled(
                    truncate_for_display(&item.value, 50),
                    Style::default().fg(theme.gray_3).bg(theme.bg),
                ),
            ];
            if !icon.0.is_empty() {
                spans.push(Span::styled(
                    icon.0,
                    Style::default().fg(icon.1).bg(theme.bg),
                ));
            }
            raw_lines.push(Line::from(spans));
        }
    }

    fn render_section(
        raw_lines: &mut Vec<Line>,
        header: &str,
        target_header_w: usize,
        items: &[CheckItem],
        label_width: u16,
        theme: &AppTheme,
    ) {
        let header_dw = unicode_width::UnicodeWidthStr::width(header);
        let header_pad = if header_dw < target_header_w {
            "─".repeat(target_header_w - header_dw)
        } else {
            String::new()
        };
        raw_lines.push(Line::from(""));
        raw_lines.push(Line::from(Span::styled(
            format!("  {}{}", header, header_pad),
            Style::default()
                .fg(theme.success)
                .add_modifier(Modifier::BOLD),
        )));
        raw_lines.push(Line::from(""));
        render_items(raw_lines, items, label_width, theme);
    }

    fn render_sub_header(
        raw_lines: &mut Vec<Line>,
        label: &str,
        target_header_w: usize,
        theme: &AppTheme,
    ) {
        let sub_header = format!("─── {} ──", label);
        let header_dw = unicode_width::UnicodeWidthStr::width(sub_header.as_str());
        let header_pad = if header_dw < target_header_w {
            "─".repeat(target_header_w - header_dw)
        } else {
            String::new()
        };
        raw_lines.push(Line::from(Span::styled(
            format!("  {}{}", sub_header, header_pad),
            Style::default().fg(theme.muted),
        )));
    }

    if !daemon_items.is_empty() {
        render_section(
            &mut raw_lines,
            daemon_header,
            target_header_w,
            &daemon_items,
            label_width,
            theme,
        );
    }

    // ---- 渲染本地检查 ----
    if !local_items.is_empty() {
        render_section(
            &mut raw_lines,
            local_header,
            target_header_w,
            &local_items,
            label_width,
            theme,
        );
    }

    if !local_worker_items.is_empty() {
        render_section(
            &mut raw_lines,
            local_worker_header,
            target_header_w,
            &local_worker_items,
            label_width,
            theme,
        );
    }

    if !workstation_items.is_empty() {
        render_section(
            &mut raw_lines,
            workstation_header,
            target_header_w,
            &workstation_items,
            label_width,
            theme,
        );
    }

    // ---- 渲染远程检查（分组显示） ----
    let has_remote = !conn_items.is_empty()
        || !dir_items.is_empty()
        || !prog_items.is_empty()
        || scripts_info.is_some()
        || ref_files_info.is_some()
        || !sys_items.is_empty();
    if has_remote {
        // 主标题
        let header_dw = unicode_width::UnicodeWidthStr::width(remote_header);
        let header_pad = if header_dw < target_header_w {
            "─".repeat(target_header_w - header_dw)
        } else {
            String::new()
        };
        raw_lines.push(Line::from(""));
        raw_lines.push(Line::from(Span::styled(
            format!("  {}{}", remote_header, header_pad),
            Style::default()
                .fg(theme.success)
                .add_modifier(Modifier::BOLD),
        )));
        raw_lines.push(Line::from(""));

        // 连接状态
        render_items(&mut raw_lines, &conn_items, label_width, theme);

        // 远程目录
        if !dir_items.is_empty() {
            render_sub_header(&mut raw_lines, "远程目录", target_header_w, theme);
            render_items(&mut raw_lines, &dir_items, label_width, theme);
        }

        // 远程程序
        if !prog_items.is_empty() {
            render_sub_header(&mut raw_lines, "远程程序", target_header_w, theme);
            render_items(&mut raw_lines, &prog_items, label_width, theme);
        }

        // 脚本与引用文件部署
        if scripts_info.is_some() || ref_files_info.is_some() {
            render_sub_header(&mut raw_lines, "文件部署", target_header_w, theme);
            if let Some((total, deployed, ref missing)) = scripts_info {
                let (value, exists) = if missing.is_empty() {
                    (format!("全部就绪 ({}/{})", deployed, total), Some(true))
                } else {
                    (
                        format!("缺失 {} 个: {}", missing.len(), missing.join(", ")),
                        Some(false),
                    )
                };
                let items = vec![CheckItem {
                    label: "远程脚本文件".to_string(),
                    value,
                    exists,
                }];
                render_items(&mut raw_lines, &items, label_width, theme);
            }
            if let Some((total, deployed, ref missing)) = ref_files_info {
                let (value, exists) = if missing.is_empty() {
                    (format!("全部就绪 ({}/{})", deployed, total), Some(true))
                } else {
                    (
                        format!("缺失 {} 个: {}", missing.len(), missing.join(", ")),
                        Some(false),
                    )
                };
                let items = vec![CheckItem {
                    label: "仿真引用文件".to_string(),
                    value,
                    exists,
                }];
                render_items(&mut raw_lines, &items, label_width, theme);
            }
        }

        // 系统信息
        if !sys_items.is_empty() {
            render_sub_header(&mut raw_lines, "系统信息", target_header_w, theme);
            render_items(&mut raw_lines, &sys_items, label_width, theme);
        }
    }

    raw_lines
}

pub fn render_check_result(
    frame: &mut Frame,
    area: Rect,
    data: &serde_json::Value,
    scroll: u16,
    hovered_dialog_button: Option<u8>,
    clicked_dialog_button: Option<u8>,
    theme: &AppTheme,
) -> DialogRenderInfo {
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

    // Title bar: title left-aligned, hint right-aligned
    let title_text = "系统自检结果 (System Check)";
    let title_w = unicode_width::UnicodeWidthStr::width(title_text) as u16 + 2;

    let title_row = Rect {
        x: inner.x,
        y: inner.y,
        width: inner.width,
        height: 1,
    };
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
            "↑↓/滚轮 滚动 | Esc 关闭",
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
        Rect {
            x: inner.x,
            y: sep_y,
            width: inner.width,
            height: 1,
        },
    );

    // Layout: top(2 rows) + content + bottom(2 rows = separator + button)
    let top_height: u16 = 2;
    let bottom_height: u16 = 2;
    let content_top = inner.y + top_height;
    let content_height = inner
        .height
        .saturating_sub(top_height)
        .saturating_sub(bottom_height);

    let content_area = Rect {
        x: inner.x,
        y: content_top,
        width: inner.width.saturating_sub(1),
        height: content_height,
    };

    let scrollbar_area = Rect {
        x: inner.x + inner.width.saturating_sub(1),
        y: content_top,
        width: 1,
        height: content_height,
    };

    let content_lines = build_check_content_lines(data, content_area.width as usize, theme);
    let total_lines = content_lines.len();
    let visible = content_height as usize;
    let max_scroll = total_lines.saturating_sub(visible);
    let scroll_offset = (scroll as usize).min(max_scroll);

    let paragraph = Paragraph::new(content_lines)
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
    let sep2_y = content_top + content_height;
    frame.render_widget(
        Paragraph::new(Span::styled(&sep, sep_style)),
        Rect {
            x: inner.x,
            y: sep2_y,
            width: inner.width,
            height: 1,
        },
    );

    // Button bar — btn_y = inner.y + inner.height - 1
    let btn_y = inner.y + inner.height - 1;
    let close_label = " 关闭 (Esc) ";
    let close_style = theme.dialog_btn_style(0, hovered_dialog_button, clicked_dialog_button);

    let btn_line = Line::from(vec![Span::styled(close_label.to_string(), close_style)]);

    frame.render_widget(
        Paragraph::new(btn_line).alignment(Alignment::Center),
        Rect {
            x: inner.x,
            y: btn_y,
            width: inner.width,
            height: 1,
        },
    );

    DialogRenderInfo {
        content_total_lines: total_lines,
        content_visible_lines: visible,
        scrollbar_area,
        button_bar_y: btn_y,
    }
}

pub fn centered_rect(percent_x: u16, percent_y: u16, r: Rect) -> Rect {
    let popup_layout = Layout::default()
        .direction(Direction::Vertical)
        .constraints([
            Constraint::Percentage((100 - percent_y) / 2),
            Constraint::Percentage(percent_y),
            Constraint::Percentage((100 - percent_y) / 2),
        ])
        .split(r);

    Layout::default()
        .direction(Direction::Horizontal)
        .constraints([
            Constraint::Percentage((100 - percent_x) / 2),
            Constraint::Percentage(percent_x),
            Constraint::Percentage((100 - percent_x) / 2),
        ])
        .split(popup_layout[1])[1]
}

#[cfg(test)]
mod tests {
    use super::{build_check_content_lines, symbol_is_wide};
    use crate::theme::AppTheme;
    use ratatui::text::Line;
    use serde_json::json;

    fn line_text(lines: &[Line<'static>]) -> String {
        lines
            .iter()
            .flat_map(|line| line.spans.iter())
            .map(|span| span.content.as_ref())
            .collect::<Vec<_>>()
            .join("")
    }

    #[test]
    fn wide_symbol_detection_handles_composite_emoji() {
        assert!(symbol_is_wide("⏸️"));
        assert!(symbol_is_wide("✅"));
        assert!(symbol_is_wide("❌"));
    }

    #[test]
    fn wide_symbol_detection_keeps_ascii_narrow() {
        assert!(!symbol_is_wide("A"));
        assert!(!symbol_is_wide("?"));
    }

    #[test]
    fn check_content_lines_supports_daemon_and_local_worker_schema() {
        let theme = AppTheme::default();
        let data = json!({
            "daemon_checks": {
                "server_mode": {"ok": true, "message": "server mode enabled"}
            },
            "local_worker_checks": {
                "SW可执行文件": {
                    "path": "C:\\SW\\SLDWORKS.exe",
                    "exists": true
                }
            }
        });

        let text = line_text(&build_check_content_lines(&data, 80, &theme));

        assert!(text.contains("Daemon"));
        assert!(text.contains("server_mode"));
        assert!(text.contains("LocalWorker"));
        assert!(text.contains("SW可执行文件"));
        assert!(text.contains("C:\\SW\\SLDWORKS.exe"));
        assert!(text.contains("✅"));
    }

    #[test]
    fn check_content_lines_supports_workstation_schema() {
        let theme = AppTheme::default();
        let data = json!({
            "workstation_checks": {
                "server_mode": true,
                "workstations": [{
                    "id": "WS-A",
                    "host": "172.17.135.240",
                    "effective_host": "100.64.1.20",
                    "port": 22,
                    "connectivity_mode": "tailscale",
                    "severity": "error",
                    "warning": "私网地址不可达"
                }]
            }
        });

        let text = line_text(&build_check_content_lines(&data, 80, &theme));

        assert!(text.contains("工作站"));
        assert!(text.contains("WS-A"));
        assert!(text.contains("100.64.1.20"));
        assert!(text.contains("私网地址不可达"));
        assert!(text.contains("❌"));
    }

    #[test]
    fn check_content_lines_keeps_legacy_local_remote_schema() {
        let theme = AppTheme::default();
        let data = json!({
            "local_checks": {
                "Excel参数表": {
                    "path": "C:\\models\\params.xlsx",
                    "exists": true
                }
            },
            "remote_checks": {
                "ssh": "连接成功",
                "scripts_status": {
                    "total": 1,
                    "deployed": 1,
                    "missing": []
                }
            }
        });

        let text = line_text(&build_check_content_lines(&data, 80, &theme));

        assert!(text.contains("本地环境检查"));
        assert!(text.contains("远程工作站检查"));
        assert!(text.contains("SSH连接"));
        assert!(text.contains("Excel参数表"));
        assert!(text.contains("远程脚本文件"));
    }
}
