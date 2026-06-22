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
    content_width: usize,
    theme: &AppTheme,
) -> Vec<Line<'static>> {
    let label_width: u16 = (content_width / 4).clamp(14, 24) as u16;
    let value_width = content_width
        .saturating_sub(label_width as usize)
        .saturating_sub(8)
        .max(24);

    struct CheckItem {
        label: String,
        value: String,
        exists: Option<bool>,
    }

    let mut raw_lines: Vec<Line> = Vec::new();

    let passed = data
        .get("summary")
        .and_then(|summary| summary.get("passed"))
        .and_then(|v| v.as_u64())
        .unwrap_or(0);
    let failed = data
        .get("summary")
        .and_then(|summary| summary.get("failed"))
        .and_then(|v| v.as_u64())
        .unwrap_or(0);
    let warnings = data
        .get("summary")
        .and_then(|summary| summary.get("warnings"))
        .and_then(|v| v.as_u64())
        .unwrap_or(0);
    let overall_ok = data
        .get("overall_ok")
        .and_then(|v| v.as_bool())
        .unwrap_or(failed == 0);
    let overview_items = vec![CheckItem {
        label: "总体结论".to_string(),
        value: format!(
            "{} | 通过 {passed} | 失败 {failed} | 告警 {warnings}",
            if overall_ok { "通过" } else { "未通过" }
        ),
        exists: Some(overall_ok),
    }];

    // ---- 本地环境检查 ----
    let overview_header = "─── 总览 ──";
    let daemon_header = "─── Daemon 检查 ──";
    let local_header = "─── 本地环境检查 ──";
    let local_worker_health_header = "─── LocalWorker 在线状态 ──";
    let config_warning_header = "─── 配置告警 ──";
    let local_worker_header = "─── LocalWorker 本地环境检查 ──";
    let workstation_header = "─── 工作站配置检查 ──";
    let remote_header = "─── 远程工作站检查 ──";
    let target_header_w = {
        [
            daemon_header,
            local_header,
            local_worker_health_header,
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

    if let Some(remote) = data.get("remote_checks").and_then(|v| v.as_object()) {
        if let Some(workstations) = remote.get("workstations").and_then(|v| v.as_object()) {
            let mut workstation_ids: Vec<&String> = workstations.keys().collect();
            workstation_ids.sort();
            for workstation_id in workstation_ids {
                let workstation = &workstations[workstation_id];
                conn_items.push(CheckItem {
                    label: format!("工作站 {workstation_id}"),
                    value: workstation
                        .get("status")
                        .and_then(|v| v.as_str())
                        .unwrap_or("unknown")
                        .to_string(),
                    exists: workstation.get("ok").and_then(|v| v.as_bool()),
                });
                if let Some(ssh_status) = workstation.get("ssh").and_then(|v| v.as_str()) {
                    conn_items.push(CheckItem {
                        label: format!("{workstation_id} SSH"),
                        value: ssh_status.to_string(),
                        exists: Some(ssh_status.contains("成功")),
                    });
                }
                if let Some(dirs) = workstation.get("remote_dirs").and_then(|v| v.as_array()) {
                    for d in dirs {
                        let label = d
                            .get("label")
                            .and_then(|v| v.as_str())
                            .unwrap_or("远程目录");
                        let path = d.get("path").and_then(|v| v.as_str()).unwrap_or("");
                        dir_items.push(CheckItem {
                            label: format!("{workstation_id} {label}"),
                            value: if path.is_empty() {
                                "(未设置)".to_string()
                            } else {
                                path.to_string()
                            },
                            exists: d.get("exists").and_then(|v| v.as_bool()),
                        });
                    }
                }
                if let Some(progs) = workstation
                    .get("remote_programs")
                    .and_then(|v| v.as_array())
                {
                    for p in progs {
                        let label = p
                            .get("label")
                            .and_then(|v| v.as_str())
                            .unwrap_or("远程程序");
                        let path = p.get("path").and_then(|v| v.as_str()).unwrap_or("");
                        prog_items.push(CheckItem {
                            label: format!("{workstation_id} {label}"),
                            value: if path.is_empty() {
                                "(未设置)".to_string()
                            } else {
                                path.to_string()
                            },
                            exists: p.get("exists").and_then(|v| v.as_bool()),
                        });
                    }
                }
                if let Some(scripts) = workstation.get("scripts_status") {
                    let (value, exists) = deployment_status_item(scripts);
                    dir_items.push(CheckItem {
                        label: format!("{workstation_id} 远程脚本文件"),
                        value,
                        exists,
                    });
                }
                if let Some(refs) = workstation.get("ref_files_status") {
                    let (value, exists) = deployment_status_item(refs);
                    dir_items.push(CheckItem {
                        label: format!("{workstation_id} 仿真引用文件"),
                        value,
                        exists,
                    });
                }
                if let Some(py_ver) = workstation.get("python_version").and_then(|v| v.as_str()) {
                    sys_items.push(CheckItem {
                        label: format!("{workstation_id} Python版本"),
                        value: if py_ver.is_empty() {
                            "未安装或无法检测".to_string()
                        } else {
                            py_ver.to_string()
                        },
                        exists: Some(!py_ver.is_empty()),
                    });
                }
                if let Some(disk) = workstation.get("disk_space").and_then(|v| v.as_str()) {
                    sys_items.push(CheckItem {
                        label: format!("{workstation_id} 磁盘空间"),
                        value: if disk.is_empty() {
                            "未知".to_string()
                        } else {
                            disk.to_string()
                        },
                        exists: None,
                    });
                }
                if let Some(procs) = workstation
                    .get("background_processes")
                    .and_then(|v| v.as_array())
                {
                    let proc_list: Vec<&str> =
                        procs.iter().map(|v| v.as_str().unwrap_or("?")).collect();
                    sys_items.push(CheckItem {
                        label: format!("{workstation_id} 后台进程"),
                        value: if proc_list.is_empty() {
                            "无".to_string()
                        } else {
                            proc_list.join(", ")
                        },
                        exists: None,
                    });
                }
            }
        }
    }

    fn deployment_status_item(status: &serde_json::Value) -> (String, Option<bool>) {
        if status.get("status").and_then(|v| v.as_str()) == Some("skipped") {
            let message = status
                .get("message")
                .and_then(|v| v.as_str())
                .unwrap_or("未检查");
            return (message.to_string(), None);
        }
        let total = status.get("total").and_then(|v| v.as_u64()).unwrap_or(0) as usize;
        let deployed = status.get("deployed").and_then(|v| v.as_u64()).unwrap_or(0) as usize;
        let missing: Vec<String> = status
            .get("missing")
            .and_then(|v| v.as_array())
            .map(|arr| {
                arr.iter()
                    .filter_map(|v| v.as_str().map(String::from))
                    .collect()
            })
            .unwrap_or_default();
        if total == 0 && deployed == 0 && missing.is_empty() {
            return ("未检查".to_string(), None);
        }
        if missing.is_empty() && deployed >= total {
            (format!("全部就绪 ({}/{})", deployed, total), Some(true))
        } else {
            (
                format!("缺失 {} 个: {}", missing.len(), missing.join(", ")),
                Some(false),
            )
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
            label: match label {
                "server_mode" => "服务模式".to_string(),
                "ipc" => "IPC 配置".to_string(),
                "data_dir" => "数据目录".to_string(),
                "scdoc_dir" => "SCDOC 目录".to_string(),
                "remote_scripts_dir" => "远程脚本目录".to_string(),
                "active_check" => "主动自检".to_string(),
                _ => label.to_string(),
            },
            value,
            exists,
        }
    }

    fn object_to_check_items(
        object: &serde_json::Map<String, serde_json::Value>,
    ) -> Vec<CheckItem> {
        object
            .iter()
            .filter(|(label, _)| !matches!(label.as_str(), "ok" | "status" | "summary"))
            .map(|(label, info)| object_entry_to_check_item(label, info))
            .collect()
    }

    let daemon_items: Vec<CheckItem> = data
        .get("daemon_checks")
        .and_then(|v| v.as_object())
        .map(object_to_check_items)
        .unwrap_or_default();

    let mut config_warning_items: Vec<CheckItem> = Vec::new();
    let local_worker_health_items = data
        .get("health")
        .and_then(|v| v.as_object())
        .map(|health| {
            let mut items = Vec::new();
            if let Some(online) = health.get("local_worker_online").and_then(|v| v.as_bool()) {
                items.push(CheckItem {
                    label: "LW注册状态".to_string(),
                    value: if online {
                        "在线".to_string()
                    } else {
                        "离线".to_string()
                    },
                    exists: Some(online),
                });
            }
            if let Some(status) = health.get("server_to_local_ssh").and_then(|v| v.as_str()) {
                let exists = match status {
                    "ok" => Some(true),
                    "disconnected" | "error" => Some(false),
                    _ => None,
                };
                items.push(CheckItem {
                    label: "S→L反向隧道".to_string(),
                    value: status.to_string(),
                    exists,
                });
            }
            if let Some(status) = health
                .get("server_to_workstation_ssh")
                .and_then(|v| v.as_str())
            {
                let exists = match status {
                    "ok" => Some(true),
                    "disconnected" | "error" => Some(false),
                    _ => None,
                };
                items.push(CheckItem {
                    label: "S→W工作站SSH".to_string(),
                    value: status.to_string(),
                    exists,
                });
            }
            if let Some(details) = health
                .get("workstation_ssh_details")
                .and_then(|v| v.as_object())
            {
                let mut workstation_ids: Vec<&String> = details.keys().collect();
                workstation_ids.sort();
                for workstation_id in workstation_ids {
                    if let Some(status) = details[workstation_id].as_str() {
                        let exists = match status {
                            "ok" => Some(true),
                            "disconnected" | "error" => Some(false),
                            _ if status.starts_with("error:") => Some(false),
                            _ => None,
                        };
                        items.push(CheckItem {
                            label: format!("{workstation_id} SSH详情"),
                            value: status.to_string(),
                            exists,
                        });
                    }
                }
            }
            if let Some(warnings) = health.get("config_warnings").and_then(|v| v.as_array()) {
                for (idx, warning) in warnings.iter().filter_map(|v| v.as_str()).enumerate() {
                    config_warning_items.push(CheckItem {
                        label: format!("配置告警 {}", idx + 1),
                        value: warning.to_string(),
                        exists: Some(false),
                    });
                }
            }
            items
        })
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
        value_width: usize,
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
                    truncate_for_display(&item.value, value_width),
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
        value_width: usize,
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
        render_items(raw_lines, items, label_width, theme, value_width);
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

    render_section(
        &mut raw_lines,
        overview_header,
        target_header_w,
        &overview_items,
        label_width,
        theme,
        value_width,
    );

    if !daemon_items.is_empty() {
        render_section(
            &mut raw_lines,
            daemon_header,
            target_header_w,
            &daemon_items,
            label_width,
            theme,
            value_width,
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
            value_width,
        );
    }

    if !local_worker_health_items.is_empty() {
        render_section(
            &mut raw_lines,
            local_worker_health_header,
            target_header_w,
            &local_worker_health_items,
            label_width,
            theme,
            value_width,
        );
    }

    if !config_warning_items.is_empty() {
        render_section(
            &mut raw_lines,
            config_warning_header,
            target_header_w,
            &config_warning_items,
            label_width,
            theme,
            value_width,
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
            value_width,
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
            value_width,
        );
    }

    // ---- 渲染远程检查（分组显示） ----
    let has_remote = !conn_items.is_empty()
        || !dir_items.is_empty()
        || !prog_items.is_empty()
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
        render_items(&mut raw_lines, &conn_items, label_width, theme, value_width);

        // 远程目录
        if !dir_items.is_empty() {
            render_sub_header(&mut raw_lines, "远程目录", target_header_w, theme);
            render_items(&mut raw_lines, &dir_items, label_width, theme, value_width);
        }

        // 远程程序
        if !prog_items.is_empty() {
            render_sub_header(&mut raw_lines, "远程程序", target_header_w, theme);
            render_items(&mut raw_lines, &prog_items, label_width, theme, value_width);
        }

        // 系统信息
        if !sys_items.is_empty() {
            render_sub_header(&mut raw_lines, "系统信息", target_header_w, theme);
            render_items(&mut raw_lines, &sys_items, label_width, theme, value_width);
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
            "health": {
                "local_worker_online": true,
                "server_to_local_ssh": "ok"
            },
            "local_worker_checks": {
                "active_check": {
                    "message": "LocalWorker 主动自检超时",
                    "exists": false
                }
            }
        });

        let text = line_text(&build_check_content_lines(&data, 80, &theme));

        assert!(text.contains("Daemon"));
        assert!(text.contains("服务模式"));
        assert!(text.contains("LocalWorker"));
        assert!(text.contains("LW注册状态"));
        assert!(text.contains("在线"));
        assert!(text.contains("S→L反向隧道"));
        assert!(text.contains("ok"));
        assert!(text.contains("主动自检"));
        assert!(text.contains("主动自检超时"));
        assert!(text.contains("✅"));
        assert!(text.contains("❌"));
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
    fn check_content_lines_lists_each_remote_workstation_ssh_status() {
        let theme = AppTheme::default();
        let data = json!({
            "remote_checks": {
                "status": "partial",
                "workstations": {
                    "WS-A": {"ssh": "连接成功"},
                    "WS-B": {"ssh": "连接成功"},
                    "WS-C": {"ssh": "连接失败"}
                }
            }
        });

        let text = line_text(&build_check_content_lines(&data, 80, &theme));

        assert!(text.contains("WS-A SSH"));
        assert!(text.contains("WS-B SSH"));
        assert!(text.contains("WS-C SSH"));
        assert!(text.contains("连接失败"));
        assert!(!text.contains("SSH连接"));
    }

    #[test]
    fn check_content_lines_renders_new_summary_health_and_per_workstation_details() {
        let theme = AppTheme::default();
        let data = json!({
            "overall_ok": false,
            "summary": {"passed": 7, "failed": 2, "warnings": 1},
            "health": {
                "local_worker_online": true,
                "server_to_local_ssh": "ok",
                "server_to_workstation_ssh": "disconnected",
                "workstation_ssh_details": {
                    "WS-A": "ok",
                    "WS-B": "error: timed out"
                },
                "config_warnings": ["工作站 WS-B 缺少 reachable_host"]
            },
            "remote_checks": {
                "status": "partial",
                "ok": false,
                "workstations": {
                    "WS-A": {
                        "status": "passed",
                        "ok": true,
                        "ssh": "连接成功",
                        "remote_dirs": [{"label": "仿真工作目录", "path": "D:/ws-a/work", "exists": true}],
                        "remote_programs": [
                            {"label": "MPI", "path": "C:/mpi", "exists": true},
                            {
                                "label": "Fluent可执行文件",
                                "path": "D:/ANSYS Inc/v241/fluent/ntbin/win64/fluent.exe",
                                "exists": true
                            }
                        ],
                        "scripts_status": {"total": 2, "deployed": 2, "missing": []},
                        "ref_files_status": {"total": 1, "deployed": 1, "missing": []},
                        "python_version": "Python 3.11",
                        "disk_space": "100 GB free",
                        "background_processes": []
                    },
                    "WS-B": {
                        "status": "failed",
                        "ok": false,
                        "ssh": "连接失败",
                        "remote_dirs": [{"label": "仿真工作目录", "path": "D:/ws-b/work", "exists": false}],
                        "remote_programs": [{"label": "MPI", "path": "C:/missing-mpi", "exists": false}],
                        "scripts_status": {"status": "skipped", "message": "SSH 未连接，未检查"},
                        "ref_files_status": {"status": "skipped", "message": "SSH 未连接，未检查"},
                        "python_version": "",
                        "disk_space": "",
                        "background_processes": ["fluent.exe"]
                    }
                }
            }
        });

        let text = line_text(&build_check_content_lines(&data, 100, &theme));

        assert!(text.contains("总体结论"));
        assert!(text.contains("未通过"));
        assert!(text.contains("通过 7"));
        assert!(text.contains("失败 2"));
        assert!(text.contains("告警 1"));
        assert!(text.contains("S→W工作站SSH"));
        assert!(text.contains("WS-B SSH详情"));
        assert!(text.contains("timed out"));
        assert!(text.contains("配置告警"));
        assert!(text.contains("缺少 reachable_host"));
        assert!(text.contains("工作站 WS-A"));
        assert!(text.contains("D:/ws-a/work"));
        assert!(text.contains("Fluent可执行文件"));
        assert!(text.contains("D:/ANSYS Inc/v241/fluent/ntbin/win64/fluent.exe"));
        assert!(text.contains("Python 3.11"));
        assert!(text.contains("工作站 WS-B"));
        assert!(text.contains("D:/ws-b/work"));
        assert!(text.contains("SSH 未连接，未检查"));
        assert!(!text.contains("solver.py"));
        assert!(!text.contains("udf.c"));
        assert!(text.contains("fluent.exe"));
    }
}
