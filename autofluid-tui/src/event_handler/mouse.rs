//! 鼠标事件处理模块。
//!
//! 从 `main.rs::handle_mouse` 拆分而来，按事件类型分为独立子处理函数。

use crossterm::event::{MouseEvent, MouseEventKind};

use crate::daemon_mgr::DaemonManager;
use crate::event_handler::{actions, HORIZONTAL_SCROLL_STEP, SCROLL_LINE_STEP, SCROLL_WHEEL_STEP};
use crate::ipc::client::IpcClient;
use crate::settings::SettingsState;
use crate::state::app_state::{FocusZone, ScrollbarDragZone, UiMode};
use crate::state::AppState;
use crate::state::LogBuffer;
use crate::ui;
use crate::ui::command_bar;
use crate::ui::command_bar::BUTTON_DEFS;
use crate::ui::layout::AppLayout;
use crate::ui::scrollbar::{HorizontalScrollbar, VerticalScrollbar};
use crate::worker_mgr::WorkerManager;

use crate::point_in_rect;

pub struct MouseRuntime<'a> {
    pub ipc: &'a mut IpcClient,
    pub rt: &'a tokio::runtime::Runtime,
    pub daemon: &'a mut DaemonManager,
    pub worker: &'a mut WorkerManager,
    pub project_dir: &'a str,
    pub full_quit: &'a mut bool,
}

// ====================================================================
// 公共入口
// ====================================================================

pub fn handle_mouse(
    mouse: MouseEvent,
    state: &mut AppState,
    log_buffer: &mut LogBuffer,
    mut runtime: MouseRuntime<'_>,
) {
    let area = state.terminal_size;
    if area.width == 0 || area.height == 0 {
        return;
    }

    let layout = AppLayout::new(ratatui::layout::Rect::new(0, 0, area.width, area.height));
    let col = mouse.column;
    let row = mouse.row;

    let in_table = point_in_rect(col, row, layout.status_table);
    let in_info = point_in_rect(col, row, layout.info_panel);
    let in_detail = point_in_rect(col, row, layout.detail_panel);
    let in_buttons = point_in_rect(col, row, layout.quick_buttons);
    let in_cmd_input = point_in_rect(col, row, layout.cmd_input);

    match mouse.kind {
        MouseEventKind::Moved => {
            handle_mouse_moved(state, col, row, &layout, area);
        }
        MouseEventKind::ScrollUp => {
            handle_scroll_up(state, in_table, in_info, in_detail, mouse.modifiers);
        }
        MouseEventKind::ScrollDown => {
            handle_scroll_down(state, in_table, in_info, in_detail, mouse.modifiers);
        }
        MouseEventKind::Drag(_button) => {
            handle_drag(state, col, row);
        }
        MouseEventKind::Down(_button) => {
            handle_mouse_down(
                state,
                log_buffer,
                col,
                row,
                &layout,
                area,
                in_buttons,
                in_table,
                in_info,
                in_detail,
                in_cmd_input,
            );
        }
        MouseEventKind::Up(_button) => {
            handle_mouse_up(
                state,
                log_buffer,
                &mut runtime,
                col,
                row,
                &layout,
                in_buttons,
            );
        }
        _ => {}
    }
}

// ====================================================================
// Moved — 悬停检测
// ====================================================================

fn handle_mouse_moved(
    state: &mut AppState,
    col: u16,
    row: u16,
    layout: &AppLayout,
    area: ratatui::layout::Rect,
) {
    let prev_hover_row = state.hovered_table_row;
    let prev_hover_btn = state.hovered_button;
    let prev_hover_daemon_menu = state.hovered_daemon_menu_item;
    let prev_hover_detail = state.hovered_detail_row;
    let prev_hover_dialog_btn = state.hovered_dialog_button;

    // 对话框激活时跳过背景面板的 hover 检测，只处理对话框内按钮
    if state.ui_mode == UiMode::Normal {
        let in_table = point_in_rect(col, row, layout.status_table);
        let in_detail = point_in_rect(col, row, layout.detail_panel);
        let in_buttons = point_in_rect(col, row, layout.quick_buttons);

        // 表格行 hover
        if in_table {
            let inner_y = row.saturating_sub(layout.status_table.y + 1);
            if inner_y > 0 {
                let data_row = state.table_scroll_offset + inner_y - 1;
                if (data_row as usize) < state.configs.len() {
                    state.hovered_table_row = Some(data_row);
                } else {
                    state.hovered_table_row = None;
                }
            } else {
                state.hovered_table_row = None;
            }
        } else {
            state.hovered_table_row = None;
        }

        // 详细日志行 hover
        if in_detail {
            let inner_top = layout.detail_panel.y + 1;
            let inner_bottom = layout.detail_panel.y + layout.detail_panel.height.saturating_sub(1);
            if row >= inner_top && row < inner_bottom {
                let inner_y = row - inner_top;
                let visual_line = state.detail_log_scroll + inner_y;
                state.hovered_detail_row = Some(visual_line);
            } else {
                state.hovered_detail_row = None;
            }
        } else {
            state.hovered_detail_row = None;
        }

        // 按钮 / Daemon 菜单 / Worker 菜单 hover
        if state.daemon_menu_open {
            state.hovered_daemon_menu_item =
                command_bar::detect_daemon_menu_item(col, row, layout.quick_buttons);
            if let Some(btn_idx) = command_bar::daemon_button_index() {
                state.hovered_button = command_bar::button_bounds(layout.quick_buttons, btn_idx)
                    .and_then(|rect| {
                        if point_in_rect(col, row, rect) {
                            Some(btn_idx as u8)
                        } else {
                            None
                        }
                    });
            } else {
                state.hovered_button = None;
            }
        } else if state.worker_menu_open {
            state.hovered_worker_menu_item =
                command_bar::detect_worker_menu_item(col, row, layout.quick_buttons);
            if let Some(btn_idx) = command_bar::worker_button_index() {
                state.hovered_button = command_bar::button_bounds(layout.quick_buttons, btn_idx)
                    .and_then(|rect| {
                        if point_in_rect(col, row, rect) {
                            Some(btn_idx as u8)
                        } else {
                            None
                        }
                    });
            } else {
                state.hovered_button = None;
            }
        } else {
            state.hovered_daemon_menu_item = None;
            if in_buttons {
                state.hovered_button = detect_button(col, row, layout);
            } else {
                state.hovered_button = None;
            }
        }
    } // end Normal-mode-only hover detection

    // 对话框按钮 hover
    if state.ui_mode == UiMode::ConfirmDialog
        || state.ui_mode == UiMode::CheckResult
        || state.ui_mode == UiMode::Settings
    {
        state.hovered_dialog_button = detect_dialog_button(col, row, area, state);
    } else {
        state.hovered_dialog_button = None;
    }

    // Settings 模式下检测字段 hover
    let prev_hovered_field = state
        .settings_state
        .as_ref()
        .and_then(|ss| ss.hovered_field);
    if state.ui_mode == UiMode::Settings {
        if let Some(ref mut ss) = state.settings_state {
            ss.hovered_field = detect_settings_field(col, row, area, ss);
        }
    }
    let new_hovered_field = state
        .settings_state
        .as_ref()
        .and_then(|ss| ss.hovered_field);

    if state.hovered_table_row != prev_hover_row
        || state.hovered_button != prev_hover_btn
        || state.hovered_daemon_menu_item != prev_hover_daemon_menu
        || state.hovered_detail_row != prev_hover_detail
        || state.hovered_dialog_button != prev_hover_dialog_btn
        || prev_hovered_field != new_hovered_field
    {
        state.needs_redraw = true;
    }
}

// ====================================================================
// ScrollUp / ScrollDown
// ====================================================================

fn handle_scroll_up(
    state: &mut AppState,
    in_table: bool,
    in_info: bool,
    in_detail: bool,
    modifiers: crossterm::event::KeyModifiers,
) {
    if state.ui_mode == UiMode::ConfirmDialog
        || state.ui_mode == UiMode::CheckResult
        || state.ui_mode == UiMode::Settings
    {
        if state.ui_mode == UiMode::Settings {
            if let Some(ref mut ss) = state.settings_state {
                if ss.scroll > 0 {
                    ss.scroll = ss.scroll.saturating_sub(SCROLL_LINE_STEP);
                    state.needs_redraw = true;
                }
            }
        } else if state.dialog_scroll > 0 {
            state.dialog_scroll = state.dialog_scroll.saturating_sub(SCROLL_LINE_STEP);
            state.needs_redraw = true;
        }
    } else if modifiers.contains(crossterm::event::KeyModifiers::CONTROL) {
        if in_info {
            state.info_log_hscroll = state
                .info_log_hscroll
                .saturating_sub(HORIZONTAL_SCROLL_STEP);
            state.needs_redraw = true;
        } else if in_detail {
            state.detail_log_hscroll = state
                .detail_log_hscroll
                .saturating_sub(HORIZONTAL_SCROLL_STEP);
            state.needs_redraw = true;
        }
    } else if in_table {
        if state.table_scroll_offset > 0 {
            state.table_scroll_offset = state.table_scroll_offset.saturating_sub(SCROLL_LINE_STEP);
            state.focus_zone = FocusZone::Table;
            state.needs_redraw = true;
        }
    } else if in_info {
        if state.info_log_scroll > 0 {
            state.info_log_scroll = state.info_log_scroll.saturating_sub(SCROLL_WHEEL_STEP);
            state.info_log_auto_scroll = false;
            state.focus_zone = FocusZone::InfoLog;
            state.needs_redraw = true;
        }
    } else if in_detail && state.detail_log_scroll > 0 {
        state.detail_log_scroll = state.detail_log_scroll.saturating_sub(SCROLL_WHEEL_STEP);
        state.detail_log_auto_scroll = false;
        state.focus_zone = FocusZone::DetailLog;
        state.needs_redraw = true;
    }
}

fn handle_scroll_down(
    state: &mut AppState,
    in_table: bool,
    in_info: bool,
    in_detail: bool,
    modifiers: crossterm::event::KeyModifiers,
) {
    if state.ui_mode == UiMode::ConfirmDialog
        || state.ui_mode == UiMode::CheckResult
        || state.ui_mode == UiMode::Settings
    {
        if state.ui_mode == UiMode::Settings {
            if let Some(ref mut ss) = state.settings_state {
                ss.scroll = ss.scroll.saturating_add(SCROLL_LINE_STEP);
                state.needs_redraw = true;
            }
        } else {
            state.dialog_scroll = state.dialog_scroll.saturating_add(SCROLL_LINE_STEP);
            state.needs_redraw = true;
        }
    } else if modifiers.contains(crossterm::event::KeyModifiers::CONTROL) {
        if in_info {
            state.info_log_hscroll = state
                .info_log_hscroll
                .saturating_add(HORIZONTAL_SCROLL_STEP);
            state.needs_redraw = true;
        } else if in_detail {
            state.detail_log_hscroll = state
                .detail_log_hscroll
                .saturating_add(HORIZONTAL_SCROLL_STEP);
            state.needs_redraw = true;
        }
    } else if in_table {
        state.table_scroll_offset = state.table_scroll_offset.saturating_add(SCROLL_LINE_STEP);
        state.focus_zone = FocusZone::Table;
        state.needs_redraw = true;
    } else if in_info {
        state.info_log_scroll = state.info_log_scroll.saturating_add(SCROLL_WHEEL_STEP);
        state.info_log_auto_scroll = false;
        state.focus_zone = FocusZone::InfoLog;
        state.needs_redraw = true;
    } else if in_detail {
        state.detail_log_scroll = state.detail_log_scroll.saturating_add(SCROLL_WHEEL_STEP);
        state.detail_log_auto_scroll = false;
        state.focus_zone = FocusZone::DetailLog;
        state.needs_redraw = true;
    }
}

// ====================================================================
// Drag — 滚动条拖拽
// ====================================================================

fn handle_drag(state: &mut AppState, col: u16, row: u16) {
    if let Some((zone, start_pos, start_scroll)) = state.scrollbar_drag {
        use ScrollbarDragZone::*;
        let result: Option<(u16, FocusZone)> = match zone {
            TableVertical => state.scrollbar_info.table_v.as_ref().map(|info| {
                (
                    sb_vertical_scroll_from_drag(
                        &info.area,
                        info.total,
                        info.visible,
                        start_scroll,
                        start_pos,
                        row.saturating_sub(info.area.y),
                    ),
                    FocusZone::Table,
                )
            }),
            InfoVertical => state.scrollbar_info.info_v.as_ref().map(|info| {
                (
                    sb_vertical_scroll_from_drag(
                        &info.area,
                        info.total,
                        info.visible,
                        start_scroll,
                        start_pos,
                        row.saturating_sub(info.area.y),
                    ),
                    FocusZone::InfoLog,
                )
            }),
            InfoHorizontal => state.scrollbar_info.info_h.as_ref().map(|info| {
                (
                    sb_horizontal_scroll_from_drag(
                        &info.area,
                        info.total,
                        info.visible,
                        start_scroll,
                        start_pos,
                        col.saturating_sub(info.area.x),
                    ),
                    FocusZone::InfoLog,
                )
            }),
            DetailVertical => state.scrollbar_info.detail_v.as_ref().map(|info| {
                (
                    sb_vertical_scroll_from_drag(
                        &info.area,
                        info.total,
                        info.visible,
                        start_scroll,
                        start_pos,
                        row.saturating_sub(info.area.y),
                    ),
                    FocusZone::DetailLog,
                )
            }),
            DetailHorizontal => state.scrollbar_info.detail_h.as_ref().map(|info| {
                (
                    sb_horizontal_scroll_from_drag(
                        &info.area,
                        info.total,
                        info.visible,
                        start_scroll,
                        start_pos,
                        col.saturating_sub(info.area.x),
                    ),
                    FocusZone::DetailLog,
                )
            }),
            DialogVertical => state.scrollbar_info.dialog_v.as_ref().map(|info| {
                (
                    sb_vertical_scroll_from_drag(
                        &info.area,
                        info.total,
                        info.visible,
                        start_scroll,
                        start_pos,
                        row.saturating_sub(info.area.y),
                    ),
                    state.focus_zone,
                )
            }),
        };
        if let Some((new_scroll, focus)) = result {
            match zone {
                TableVertical => state.table_scroll_offset = new_scroll,
                InfoVertical => {
                    state.info_log_scroll = new_scroll;
                    state.info_log_auto_scroll = false;
                }
                InfoHorizontal => state.info_log_hscroll = new_scroll,
                DetailVertical => {
                    state.detail_log_scroll = new_scroll;
                    state.detail_log_auto_scroll = false;
                }
                DetailHorizontal => state.detail_log_hscroll = new_scroll,
                DialogVertical => {
                    state.dialog_scroll = new_scroll;
                    if let Some(ref mut ss) = state.settings_state {
                        if state.ui_mode == UiMode::Settings {
                            ss.scroll = new_scroll;
                        }
                    }
                }
            }
            state.focus_zone = focus;
            state.needs_redraw = true;
        }
    }
}

// ====================================================================
// Down — 鼠标按下
// ====================================================================

#[allow(clippy::too_many_arguments)]
fn handle_mouse_down(
    state: &mut AppState,
    log_buffer: &mut LogBuffer,
    col: u16,
    row: u16,
    layout: &AppLayout,
    area: ratatui::layout::Rect,
    in_buttons: bool,
    in_table: bool,
    in_info: bool,
    in_detail: bool,
    in_cmd_input: bool,
) {
    // 对话框/设置模式下检测按钮点击
    if state.ui_mode == UiMode::ConfirmDialog
        || state.ui_mode == UiMode::CheckResult
        || state.ui_mode == UiMode::Settings
    {
        if let Some(btn_idx) = detect_dialog_button(col, row, area, state) {
            state.clicked_dialog_button = Some(btn_idx);
            state.dialog_click_time = Some(std::time::Instant::now());
            state.needs_redraw = true;
            return;
        }
    }

    // 滚动条检测
    {
        let sb_info = state.scrollbar_info;
        let mut sb_detected = false;

        // Settings 模式下仅检测对话框滚动条，跳过背景面板滚动条
        if state.ui_mode == UiMode::Settings {
            if let Some(info) = sb_info.dialog_v {
                if sb_vertical_track_hit(&info.area, col, row) {
                    let current_scroll = state
                        .settings_state
                        .as_ref()
                        .map(|ss| ss.scroll)
                        .unwrap_or(info.scroll as u16);
                    if let Some(rel_pos) =
                        sb_vertical_hit(&info.area, info.total, info.visible, info.scroll, col, row)
                    {
                        state.scrollbar_drag = Some((
                            ScrollbarDragZone::DialogVertical,
                            rel_pos as u16,
                            current_scroll,
                        ));
                    } else {
                        let new_scroll = sb_vertical_scroll_from_click(
                            &info.area,
                            info.total,
                            info.visible,
                            info.scroll,
                            row,
                        );
                        if let Some(ref mut ss) = state.settings_state {
                            ss.scroll = new_scroll;
                        }
                    }
                    state.needs_redraw = true;
                    sb_detected = true;
                }
            }
        } else {
            // 非 Settings 模式下检测所有面板滚动条
            sb_detected = detect_panel_scrollbars(state, &sb_info, col, row);
        } // end else (non-Settings scrollbar detection)
        if sb_detected {
            return;
        }
    }

    // Settings 模式下检测字段点击（双击触发编辑，布尔字段单击即切换）
    if state.ui_mode == UiMode::Settings {
        if let Some(ref mut ss) = state.settings_state {
            // 编辑模式下阻止鼠标对其他行进行双击/单击操作
            if !ss.focus.editing {
                if let Some((cat_idx, fi)) = detect_settings_field(col, row, area, ss) {
                    let now = std::time::Instant::now();
                    // Double-click: same field clicked within 400ms
                    let is_double = ss.last_clicked_field == Some((cat_idx, fi))
                        && ss
                            .last_click_time
                            .is_some_and(|t| now.duration_since(t).as_millis() < 400);
                    if is_double {
                        ss.focus.category_index = cat_idx;
                        ss.focus.field_index = fi;
                        let cat = ss.current_category();
                        if cat.is_bool_field(fi) {
                            // Boolean fields: toggle directly, don't enter text editing
                            ss.toggle_boolean();
                        } else {
                            ss.begin_edit_current_field();
                        }
                        ss.clicked_field = None;
                        ss.field_click_time = None;
                        ss.last_clicked_field = None;
                        ss.last_click_time = None;
                    } else {
                        // Single click: select field + animation + track for double-click
                        ss.focus.category_index = cat_idx;
                        ss.focus.field_index = fi;
                        ss.clicked_field = Some((cat_idx, fi));
                        ss.field_click_time = Some(now);
                        ss.last_clicked_field = Some((cat_idx, fi));
                        ss.last_click_time = Some(now);
                    }
                    state.needs_redraw = true;
                    return;
                }
            }
        }
    }

    // Daemon 菜单打开时的点击处理
    if state.ui_mode == UiMode::Normal && state.daemon_menu_open {
        if let Some(menu_idx) = command_bar::detect_daemon_menu_item(col, row, layout.quick_buttons)
        {
            state.clicked_daemon_menu_item = Some(menu_idx);
            state.daemon_menu_click_time = Some(std::time::Instant::now());
            state.needs_redraw = true;
            return;
        }

        if let Some(btn_idx) = command_bar::daemon_button_index() {
            if let Some(button_rect) = command_bar::button_bounds(layout.quick_buttons, btn_idx) {
                if point_in_rect(col, row, button_rect) {
                    close_daemon_menu(state);
                    state.needs_redraw = true;
                    return;
                }
            }
        }

        close_daemon_menu(state);
        state.needs_redraw = true;
        return;
    }

    // Worker 菜单打开时的点击处理
    if state.ui_mode == UiMode::Normal && state.worker_menu_open {
        if let Some(menu_idx) = command_bar::detect_worker_menu_item(col, row, layout.quick_buttons)
        {
            state.clicked_worker_menu_item = Some(menu_idx);
            state.worker_menu_click_time = Some(std::time::Instant::now());
            state.needs_redraw = true;
            return;
        }

        if let Some(btn_idx) = command_bar::worker_button_index() {
            if let Some(button_rect) = command_bar::button_bounds(layout.quick_buttons, btn_idx) {
                if point_in_rect(col, row, button_rect) {
                    close_worker_menu(state);
                    state.needs_redraw = true;
                    return;
                }
            }
        }

        close_worker_menu(state);
        state.needs_redraw = true;
        return;
    }

    // 当对话框覆盖层激活时，阻止鼠标事件穿透到背景面板
    if state.ui_mode != UiMode::Normal {
        return;
    }

    // 普通模式下的面板点击
    if in_buttons {
        if let Some(btn_idx) = detect_button(col, row, layout) {
            state.clicked_button = Some(btn_idx);
            state.click_time = Some(std::time::Instant::now());
            state.needs_redraw = true;
        }
    } else if in_table {
        state.focus_zone = FocusZone::Table;
        state.needs_redraw = true;
    } else if in_info {
        state.focus_zone = FocusZone::InfoLog;
        state.needs_redraw = true;
    } else if in_detail {
        handle_detail_click(state, log_buffer, layout, col, row);
    } else if in_cmd_input {
        state.focus_zone = FocusZone::CommandInput;
        state.needs_redraw = true;
    }
}

/// 检测普通面板（非 Settings）的滚动条点击/拖拽。
fn detect_panel_scrollbars(
    state: &mut AppState,
    sb_info: &crate::state::app_state::ScrollbarRenderedInfo,
    col: u16,
    row: u16,
) -> bool {
    let mut sb_detected = false;

    if let Some(info) = sb_info.table_v {
        if sb_vertical_track_hit(&info.area, col, row) {
            if let Some(rel_pos) =
                sb_vertical_hit(&info.area, info.total, info.visible, info.scroll, col, row)
            {
                state.scrollbar_drag = Some((
                    ScrollbarDragZone::TableVertical,
                    rel_pos as u16,
                    state.table_scroll_offset,
                ));
            } else {
                state.table_scroll_offset = sb_vertical_scroll_from_click(
                    &info.area,
                    info.total,
                    info.visible,
                    info.scroll,
                    row,
                );
            }
            state.focus_zone = FocusZone::Table;
            state.needs_redraw = true;
            sb_detected = true;
        }
    }
    if !sb_detected {
        if let Some(info) = sb_info.info_v {
            if sb_vertical_track_hit(&info.area, col, row) {
                if let Some(rel_pos) =
                    sb_vertical_hit(&info.area, info.total, info.visible, info.scroll, col, row)
                {
                    state.scrollbar_drag = Some((
                        ScrollbarDragZone::InfoVertical,
                        rel_pos as u16,
                        state.info_log_scroll,
                    ));
                } else {
                    state.info_log_scroll = sb_vertical_scroll_from_click(
                        &info.area,
                        info.total,
                        info.visible,
                        info.scroll,
                        row,
                    );
                }
                state.info_log_auto_scroll = false;
                state.focus_zone = FocusZone::InfoLog;
                state.needs_redraw = true;
                sb_detected = true;
            }
        }
    }
    if !sb_detected {
        if let Some(info) = sb_info.info_h {
            if sb_horizontal_track_hit(&info.area, col, row) {
                if let Some(rel_pos) =
                    sb_horizontal_hit(&info.area, info.total, info.visible, info.scroll, col, row)
                {
                    state.scrollbar_drag = Some((
                        ScrollbarDragZone::InfoHorizontal,
                        rel_pos as u16,
                        state.info_log_hscroll,
                    ));
                } else {
                    state.info_log_hscroll = sb_horizontal_scroll_from_click(
                        &info.area,
                        info.total,
                        info.visible,
                        info.scroll,
                        col,
                    );
                }
                state.focus_zone = FocusZone::InfoLog;
                state.needs_redraw = true;
                sb_detected = true;
            }
        }
    }
    if !sb_detected {
        if let Some(info) = sb_info.detail_v {
            if sb_vertical_track_hit(&info.area, col, row) {
                if let Some(rel_pos) =
                    sb_vertical_hit(&info.area, info.total, info.visible, info.scroll, col, row)
                {
                    state.scrollbar_drag = Some((
                        ScrollbarDragZone::DetailVertical,
                        rel_pos as u16,
                        state.detail_log_scroll,
                    ));
                } else {
                    state.detail_log_scroll = sb_vertical_scroll_from_click(
                        &info.area,
                        info.total,
                        info.visible,
                        info.scroll,
                        row,
                    );
                    state.detail_log_auto_scroll = false;
                }
                state.focus_zone = FocusZone::DetailLog;
                state.needs_redraw = true;
                sb_detected = true;
            }
        }
    }
    if !sb_detected {
        if let Some(info) = sb_info.detail_h {
            if sb_horizontal_track_hit(&info.area, col, row) {
                if let Some(rel_pos) =
                    sb_horizontal_hit(&info.area, info.total, info.visible, info.scroll, col, row)
                {
                    state.scrollbar_drag = Some((
                        ScrollbarDragZone::DetailHorizontal,
                        rel_pos as u16,
                        state.detail_log_hscroll,
                    ));
                } else {
                    state.detail_log_hscroll = sb_horizontal_scroll_from_click(
                        &info.area,
                        info.total,
                        info.visible,
                        info.scroll,
                        col,
                    );
                }
                state.focus_zone = FocusZone::DetailLog;
                state.needs_redraw = true;
                sb_detected = true;
            }
        }
    }
    if !sb_detected {
        if let Some(info) = sb_info.dialog_v {
            if sb_vertical_track_hit(&info.area, col, row) {
                let current_scroll = if state.ui_mode == UiMode::Settings {
                    state
                        .settings_state
                        .as_ref()
                        .map(|ss| ss.scroll)
                        .unwrap_or(info.scroll as u16)
                } else {
                    state.dialog_scroll
                };
                if let Some(rel_pos) =
                    sb_vertical_hit(&info.area, info.total, info.visible, info.scroll, col, row)
                {
                    state.scrollbar_drag = Some((
                        ScrollbarDragZone::DialogVertical,
                        rel_pos as u16,
                        current_scroll,
                    ));
                } else {
                    let new_scroll = sb_vertical_scroll_from_click(
                        &info.area,
                        info.total,
                        info.visible,
                        info.scroll,
                        row,
                    );
                    if state.ui_mode == UiMode::Settings {
                        if let Some(ref mut ss) = state.settings_state {
                            ss.scroll = new_scroll;
                        }
                    } else {
                        state.dialog_scroll = new_scroll;
                    }
                }
                state.needs_redraw = true;
                sb_detected = true;
            }
        }
    }

    sb_detected
}

/// 处理详细日志面板的点击（单击选行、双击复制）。
fn handle_detail_click(
    state: &mut AppState,
    log_buffer: &mut LogBuffer,
    layout: &AppLayout,
    _col: u16,
    row: u16,
) {
    state.focus_zone = FocusZone::DetailLog;
    let now = std::time::Instant::now();
    let inner_top = layout.detail_panel.y + 1;
    let inner_y = row.saturating_sub(inner_top);
    let visual_line = state.detail_log_scroll + inner_y;
    state.clicked_detail_row = Some(visual_line);
    state.detail_click_time = Some(now);
    let is_double_click = state.last_detail_click_row == Some(row)
        && state
            .last_detail_click_time
            .is_some_and(|t| now.duration_since(t).as_millis() < 400);
    if is_double_click {
        let max_width = layout.detail_panel.width.saturating_sub(2) as usize;
        if let Some(msg) = ui::logs::get_raw_message_at_visual_line(
            log_buffer,
            &state.log_filter_level,
            &state.log_filter_source,
            max_width,
            visual_line as usize,
        ) {
            if clipboard_win::set_clipboard_string(&msg).is_ok() {
                log_buffer.push_info(format!(
                    "已复制到剪贴板: {}",
                    if msg.chars().count() > 60 {
                        let s: String = msg.chars().take(60).collect();
                        format!("{}...", s)
                    } else {
                        msg.clone()
                    }
                ));
            }
        }
        state.last_detail_click_time = None;
        state.last_detail_click_row = None;
    } else {
        state.last_detail_click_time = Some(now);
        state.last_detail_click_row = Some(row);
    }
    state.needs_redraw = true;
}

// ====================================================================
// Up — 鼠标释放
// ====================================================================

#[allow(clippy::too_many_arguments)] // 鼠标事件处理需要布局上下文
fn handle_mouse_up(
    state: &mut AppState,
    log_buffer: &mut LogBuffer,
    runtime: &mut MouseRuntime<'_>,
    col: u16,
    row: u16,
    layout: &AppLayout,
    in_buttons: bool,
) {
    state.scrollbar_drag = None;

    // 对话框按钮释放
    if let Some(btn_idx) = state.clicked_dialog_button {
        let area = state.terminal_size;
        if let Some(hover_idx) = detect_dialog_button(col, row, area, state) {
            if hover_idx == btn_idx {
                handle_dialog_button_click(btn_idx, state, log_buffer, runtime);
            }
        }
        state.clicked_dialog_button = None;
        state.needs_redraw = true;
        return;
    }

    // Daemon 菜单项释放
    if let Some(menu_idx) = state.clicked_daemon_menu_item {
        if state.daemon_menu_open {
            if let Some(hover_idx) =
                command_bar::detect_daemon_menu_item(col, row, layout.quick_buttons)
            {
                if hover_idx == menu_idx {
                    if let Some(cmd) = command_bar::daemon_menu_command(menu_idx) {
                        state.pending_command = Some(cmd.to_string());
                        state.pending_command_source = Some("mouse");
                        state.focus_zone = FocusZone::CommandInput;
                    }
                }
            }
        }
        close_daemon_menu(state);
        state.clicked_daemon_menu_item = None;
        state.daemon_menu_click_time = None;
        state.needs_redraw = true;
        return;
    }

    // Worker 菜单项释放
    if let Some(menu_idx) = state.clicked_worker_menu_item {
        if state.worker_menu_open {
            if let Some(hover_idx) =
                command_bar::detect_worker_menu_item(col, row, layout.quick_buttons)
            {
                if hover_idx == menu_idx {
                    if let Some(cmd) = command_bar::worker_menu_command(menu_idx) {
                        state.pending_command = Some(cmd.to_string());
                        state.pending_command_source = Some("mouse");
                        state.focus_zone = FocusZone::CommandInput;
                    }
                }
            }
        }
        close_worker_menu(state);
        state.clicked_worker_menu_item = None;
        state.worker_menu_click_time = None;
        state.needs_redraw = true;
        return;
    }

    // 快捷按钮释放
    if let Some(btn_idx) = state.clicked_button {
        if in_buttons {
            if let Some(hover_idx) = detect_button(col, row, layout) {
                if hover_idx == btn_idx {
                    let cmd = BUTTON_DEFS[btn_idx as usize].1;
                    if cmd == "daemon" {
                        close_worker_menu(state);
                        state.daemon_menu_open = !state.daemon_menu_open;
                        if state.daemon_menu_open {
                            state.hovered_daemon_menu_item = None;
                        } else {
                            close_daemon_menu(state);
                        }
                    } else if cmd == "worker" {
                        close_daemon_menu(state);
                        state.worker_menu_open = !state.worker_menu_open;
                        if state.worker_menu_open {
                            state.hovered_worker_menu_item = None;
                        } else {
                            close_worker_menu(state);
                        }
                    } else {
                        close_daemon_menu(state);
                        close_worker_menu(state);
                        state.pending_command = Some(cmd.to_string());
                        state.pending_command_source = Some("mouse");
                        state.focus_zone = FocusZone::CommandInput;
                    }
                }
            }
        }
        state.clicked_button = None;
        state.needs_redraw = true;
    }
}

// ====================================================================
// 检测辅助函数
// ====================================================================

pub fn detect_button(col: u16, row: u16, layout: &AppLayout) -> Option<u8> {
    let buttons_area = layout.quick_buttons;
    if !point_in_rect(col, row, buttons_area) {
        return None;
    }

    (0..BUTTON_DEFS.len()).find_map(|idx| {
        command_bar::button_bounds(buttons_area, idx).and_then(|rect| {
            if point_in_rect(col, row, rect) {
                Some(idx as u8)
            } else {
                None
            }
        })
    })
}

pub fn detect_dialog_button(
    col: u16,
    row: u16,
    area: ratatui::layout::Rect,
    state: &AppState,
) -> Option<u8> {
    let dialog_area = match state.ui_mode {
        UiMode::ConfirmDialog => ui::dialogs::centered_rect(80, 40, area),
        UiMode::CheckResult => ui::dialogs::centered_rect(90, 90, area),
        UiMode::Settings => ui::dialogs::centered_rect(90, 90, area),
        UiMode::Normal => return None,
    };

    if !point_in_rect(col, row, dialog_area) {
        return None;
    }

    let inner = ratatui::layout::Rect {
        x: dialog_area.x + 1,
        y: dialog_area.y + 1,
        width: dialog_area.width.saturating_sub(2),
        height: dialog_area.height.saturating_sub(2),
    };

    let btn_bar_height: u16 = 2;
    let content_height = inner.height.saturating_sub(btn_bar_height);
    let btn_y = inner.y + content_height + 1;

    if row != btn_y {
        return None;
    }

    match state.ui_mode {
        UiMode::ConfirmDialog => {
            let confirm_label = " 确认 [Y] ";
            let cancel_label = " 取消 [N] ";
            let confirm_w = unicode_width::UnicodeWidthStr::width(confirm_label) as u16;
            let cancel_w = unicode_width::UnicodeWidthStr::width(cancel_label) as u16;
            let gap: u16 = 3;
            let total_w = confirm_w + cancel_w + gap;
            let start_x = inner.x + (inner.width.saturating_sub(total_w)) / 2;

            let confirm_start = start_x;
            let confirm_end = confirm_start + confirm_w;
            let cancel_start = confirm_end + gap;
            let cancel_end = cancel_start + cancel_w;

            if col >= confirm_start && col < confirm_end {
                return Some(0);
            }
            if col >= cancel_start && col < cancel_end {
                return Some(1);
            }
            None
        }
        UiMode::CheckResult => {
            let close_label = " 关闭 [Q] ";
            let close_w = unicode_width::UnicodeWidthStr::width(close_label) as u16;
            let start_x = inner.x + (inner.width.saturating_sub(close_w)) / 2;

            if col >= start_x && col < start_x + close_w {
                return Some(0);
            }
            None
        }
        UiMode::Settings => {
            let save_label = " 保存更改 (Ctrl+S) ";
            let cancel_label = " 取消 (Esc) ";
            let save_w = unicode_width::UnicodeWidthStr::width(save_label) as u16;
            let cancel_w = unicode_width::UnicodeWidthStr::width(cancel_label) as u16;
            let gap: u16 = 4;
            let mut total_w = save_w + cancel_w + gap;

            // 附加消息宽度（与渲染逻辑保持一致）
            if let Some(ref ss) = state.settings_state {
                if let Some(ref err) = ss.save_error {
                    total_w += 2 + unicode_width::UnicodeWidthStr::width(err.as_str()) as u16;
                } else if ss.saved {
                    total_w += 2 + unicode_width::UnicodeWidthStr::width("✅ 已保存") as u16;
                }
            }

            let start_x = inner.x + (inner.width.saturating_sub(total_w)) / 2;

            if col >= start_x && col < start_x + save_w {
                return Some(0);
            }
            if col >= start_x + save_w + gap && col < start_x + save_w + gap + cancel_w {
                return Some(1);
            }
            None
        }
        UiMode::Normal => None,
    }
}

/// 检测鼠标是否悬停在 Settings 对话框的某一行字段上。
/// 返回 (category_index, field_index)
pub fn detect_settings_field(
    col: u16,
    row: u16,
    area: ratatui::layout::Rect,
    ss: &SettingsState,
) -> Option<(usize, usize)> {
    let dialog_area = ui::dialogs::centered_rect(90, 90, area);
    if !point_in_rect(col, row, dialog_area) {
        return None;
    }
    let inner_y = dialog_area.y + 1;
    let top_height: u16 = 2; // title + separator
    let content_y = inner_y + top_height;
    let bottom_height: u16 = 2;
    let content_height = dialog_area
        .height
        .saturating_sub(2)
        .saturating_sub(top_height)
        .saturating_sub(bottom_height);
    let content_bottom = content_y + content_height;

    if row < content_y || row >= content_bottom {
        return None;
    }
    let rel_row = row - content_y;
    let visual_line = ss.scroll + rel_row;

    for (cat_idx, fi, y) in &ss.field_positions {
        if *y == visual_line {
            return Some((*cat_idx, *fi));
        }
    }
    None
}

pub fn handle_dialog_button_click(
    btn_idx: u8,
    state: &mut AppState,
    log_buffer: &mut LogBuffer,
    runtime: &mut MouseRuntime<'_>,
) {
    match state.ui_mode {
        UiMode::ConfirmDialog => match btn_idx {
            0 => {
                if let Some(callback) = state.confirm_callback.take() {
                    let result =
                        runtime
                            .rt
                            .block_on(crate::event_handler::command::execute_confirm_action(
                                &callback,
                                &mut *runtime.ipc,
                                log_buffer,
                            ));
                    actions::handle_confirm_result(
                        result,
                        runtime.rt,
                        &mut *runtime.ipc,
                        state,
                        log_buffer,
                        &mut *runtime.daemon,
                        &mut *runtime.worker,
                        runtime.project_dir,
                        &mut *runtime.full_quit,
                    );
                }
                state.ui_mode = UiMode::Normal;
                state.confirm_message = None;
                state.dialog_scroll = 0;
            }
            1 => {
                state.ui_mode = UiMode::Normal;
                state.confirm_message = None;
                state.confirm_callback = None;
                state.dialog_scroll = 0;
            }
            _ => {}
        },
        UiMode::CheckResult => {
            if btn_idx == 0 {
                state.ui_mode = UiMode::Normal;
                state.check_data = None;
                state.dialog_scroll = 0;
            }
        }
        UiMode::Settings => {
            match btn_idx {
                0 => {
                    actions::save_settings(state, &mut *runtime.ipc, runtime.rt, log_buffer);
                }
                1 => {
                    // Cancel
                    state.close_settings();
                }
                _ => {}
            }
        }
        UiMode::Normal => {}
    }
}

fn close_daemon_menu(state: &mut AppState) {
    state.daemon_menu_open = false;
    state.hovered_daemon_menu_item = None;
    state.clicked_daemon_menu_item = None;
    state.daemon_menu_click_time = None;
}

fn close_worker_menu(state: &mut AppState) {
    state.worker_menu_open = false;
    state.hovered_worker_menu_item = None;
    state.clicked_worker_menu_item = None;
    state.worker_menu_click_time = None;
}

// ====================================================================
// 滚动条命中检测与拖拽辅助函数
// ====================================================================

pub fn sb_vertical_hit(
    area: &ratatui::layout::Rect,
    total: usize,
    visible: usize,
    scroll: usize,
    col: u16,
    row: u16,
) -> Option<usize> {
    if col != area.x || row < area.y || row >= area.y + area.height {
        return None;
    }
    let sb = VerticalScrollbar {
        total,
        visible,
        scroll,
        track_color: None,
        thumb_color: None,
    };
    let ti = sb.thumb_info(area.height as usize)?;
    let rel = (row - area.y) as usize;
    if rel >= ti.thumb_start && rel < ti.thumb_start + ti.thumb_size {
        Some(rel)
    } else {
        None
    }
}

pub fn sb_horizontal_hit(
    area: &ratatui::layout::Rect,
    total: usize,
    visible: usize,
    scroll: usize,
    col: u16,
    row: u16,
) -> Option<usize> {
    if row != area.y || col < area.x || col >= area.x + area.width {
        return None;
    }
    let sb = HorizontalScrollbar {
        total,
        visible,
        scroll,
        track_color: None,
        thumb_color: None,
    };
    let ti = sb.thumb_info(area.width as usize)?;
    let rel = (col - area.x) as usize;
    if rel >= ti.thumb_start && rel < ti.thumb_start + ti.thumb_size {
        Some(rel)
    } else {
        None
    }
}

pub fn sb_vertical_track_hit(area: &ratatui::layout::Rect, col: u16, row: u16) -> bool {
    col == area.x && row >= area.y && row < area.y + area.height
}

pub fn sb_horizontal_track_hit(area: &ratatui::layout::Rect, col: u16, row: u16) -> bool {
    row == area.y && col >= area.x && col < area.x + area.width
}

pub fn sb_vertical_scroll_from_click(
    area: &ratatui::layout::Rect,
    total: usize,
    visible: usize,
    scroll: usize,
    row: u16,
) -> u16 {
    let sb = VerticalScrollbar {
        total,
        visible,
        scroll,
        track_color: None,
        thumb_color: None,
    };
    let rel = (row.saturating_sub(area.y)) as usize;
    let track_length = area.height as usize;
    if let Some(ti) = sb.thumb_info(track_length) {
        let half_thumb = (ti.thumb_size / 2).min(track_length.saturating_sub(1));
        let center_pos = rel.saturating_sub(half_thumb);
        sb.scroll_from_thumb_position(center_pos, track_length) as u16
    } else {
        scroll as u16
    }
}

pub fn sb_horizontal_scroll_from_click(
    area: &ratatui::layout::Rect,
    total: usize,
    visible: usize,
    scroll: usize,
    col: u16,
) -> u16 {
    let sb = HorizontalScrollbar {
        total,
        visible,
        scroll,
        track_color: None,
        thumb_color: None,
    };
    let rel = (col.saturating_sub(area.x)) as usize;
    let track_length = area.width as usize;
    if let Some(ti) = sb.thumb_info(track_length) {
        let half_thumb = (ti.thumb_size / 2).min(track_length.saturating_sub(1));
        let center_pos = rel.saturating_sub(half_thumb);
        sb.scroll_from_thumb_position(center_pos, track_length) as u16
    } else {
        scroll as u16
    }
}

pub fn sb_vertical_scroll_from_drag(
    area: &ratatui::layout::Rect,
    total: usize,
    visible: usize,
    start_scroll: u16,
    start_pos: u16,
    current_pos: u16,
) -> u16 {
    let sb = VerticalScrollbar {
        total,
        visible,
        scroll: start_scroll as usize,
        track_color: None,
        thumb_color: None,
    };
    let track_length = area.height as usize;
    if let Some(ti) = sb.thumb_info(track_length) {
        let delta = current_pos as i32 - start_pos as i32;
        let new_thumb_start = (ti.thumb_start as i32 + delta)
            .max(0)
            .min(ti.track_space as i32) as usize;
        sb.scroll_from_thumb_position(new_thumb_start, track_length) as u16
    } else {
        start_scroll
    }
}

pub fn sb_horizontal_scroll_from_drag(
    area: &ratatui::layout::Rect,
    total: usize,
    visible: usize,
    start_scroll: u16,
    start_pos: u16,
    current_pos: u16,
) -> u16 {
    let sb = HorizontalScrollbar {
        total,
        visible,
        scroll: start_scroll as usize,
        track_color: None,
        thumb_color: None,
    };
    let track_length = area.width as usize;
    if let Some(ti) = sb.thumb_info(track_length) {
        let delta = current_pos as i32 - start_pos as i32;
        let new_thumb_start = (ti.thumb_start as i32 + delta)
            .max(0)
            .min(ti.track_space as i32) as usize;
        sb.scroll_from_thumb_position(new_thumb_start, track_length) as u16
    } else {
        start_scroll
    }
}
