use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};

use crate::event_handler::{SCROLL_LINE_STEP, SCROLL_PAGE_STEP};
use crate::settings::settings_ui::settings_workstation_visible_columns;
use crate::state::app_state::{AppState, FocusZone, UiMode};

pub enum AppAction {
    None,
    Quit,
    SubmitCommand(String),
    Confirm,
    Cancel,
    DismissDialog,
    SaveSettings,
    DiscardSettings,
}

pub fn handle_key(key: KeyEvent, state: &mut AppState) -> AppAction {
    match state.ui_mode {
        UiMode::Normal => handle_key_normal(key, state),
        UiMode::ConfirmDialog => handle_key_confirm(key, state),
        UiMode::CheckResult => handle_key_check_result(key, state),
        UiMode::Settings => handle_key_settings(key, state),
    }
}

fn is_ctrl_char(key: KeyEvent, expected: char) -> bool {
    key.modifiers.contains(KeyModifiers::CONTROL)
        && matches!(key.code, KeyCode::Char(c) if c.eq_ignore_ascii_case(&expected))
}

fn handle_key_normal(key: KeyEvent, state: &mut AppState) -> AppAction {
    match key.code {
        KeyCode::Tab => {
            state.focus_zone = state.focus_zone.cycle_next();
            state.needs_redraw = true;
            AppAction::None
        }
        KeyCode::BackTab => {
            state.focus_zone = state.focus_zone.cycle_prev();
            state.needs_redraw = true;
            AppAction::None
        }
        KeyCode::Char(_)
            if is_ctrl_char(key, 'c') && state.focus_zone != FocusZone::CommandInput =>
        {
            // Ctrl+C 退出（命令输入区的 Ctrl+C 由 handle_command_input 处理为复制）
            state.should_quit = true;
            AppAction::Quit
        }
        _ => match state.focus_zone {
            FocusZone::CommandInput => handle_command_input(key, state),
            FocusZone::Table => handle_table_scroll(key, state),
            FocusZone::InfoLog => handle_info_log_scroll(key, state),
            FocusZone::DetailLog => handle_detail_log_scroll(key, state),
        },
    }
}

fn handle_command_input(key: KeyEvent, state: &mut AppState) -> AppAction {
    match key.code {
        KeyCode::Enter => {
            let cmd = state.command_buffer.text.clone();
            state.command_buffer = Default::default();
            if !cmd.is_empty() {
                AppAction::SubmitCommand(cmd)
            } else {
                AppAction::None
            }
        }
        // ── Clipboard shortcuts ──────────────────────────────────
        KeyCode::Char(_) if is_ctrl_char(key, 'a') => {
            state.command_buffer.select_all();
            state.needs_redraw = true;
            AppAction::None
        }
        KeyCode::Char(_) if is_ctrl_char(key, 'c') => {
            state.command_buffer.copy_selection();
            AppAction::None
        }
        KeyCode::Char(_) if is_ctrl_char(key, 'x') => {
            state.command_buffer.cut_selection();
            state.needs_redraw = true;
            AppAction::None
        }
        KeyCode::Char(_) if is_ctrl_char(key, 'u') => {
            state.command_buffer.clear();
            state.needs_redraw = true;
            AppAction::None
        }
        KeyCode::Char(_) if is_ctrl_char(key, 'v') => {
            state.command_buffer.paste_from_clipboard();
            state.needs_redraw = true;
            AppAction::None
        }
        // ── Regular editing ─────────────────────────────────────
        KeyCode::Char(c) => {
            state.command_buffer.input_char(c);
            state.needs_redraw = true;
            AppAction::None
        }
        KeyCode::Backspace => {
            state.command_buffer.input_backspace();
            state.needs_redraw = true;
            AppAction::None
        }
        KeyCode::Delete => {
            state.command_buffer.input_delete();
            state.needs_redraw = true;
            AppAction::None
        }
        KeyCode::Left => {
            state.command_buffer.move_cursor_left();
            state.needs_redraw = true;
            AppAction::None
        }
        KeyCode::Right => {
            state.command_buffer.move_cursor_right();
            state.needs_redraw = true;
            AppAction::None
        }
        KeyCode::Home => {
            state.command_buffer.move_cursor_home();
            state.needs_redraw = true;
            AppAction::None
        }
        KeyCode::End => {
            state.command_buffer.move_cursor_end();
            state.needs_redraw = true;
            AppAction::None
        }
        _ => AppAction::None,
    }
}

/// 滚动区域的公共行为配置。
/// 各区域通过实现此 trait 来定义自己的滚动偏移和自动滚动语义。
trait ScrollArea {
    /// 获取当前滚动偏移
    fn offset(&self) -> u16;
    /// 设置滚动偏移
    fn set_offset(&mut self, val: u16);
    /// 是否禁用自动滚动（Up/PageUp/Home 时调用）
    fn disable_auto_scroll(&mut self) {}
    /// End 键的自定义行为（默认：跳到底部）
    fn handle_end(&mut self) {
        self.set_offset(u16::MAX);
    }
}

/// 处理滚动区域的 Up/Down/PageUp/PageDown/Home/End 键，返回是否需要重绘
fn handle_scroll_keys(key: KeyCode, area: &mut dyn ScrollArea) -> bool {
    match key {
        KeyCode::Up if area.offset() > 0 => {
            area.set_offset(area.offset().saturating_sub(SCROLL_LINE_STEP));
            area.disable_auto_scroll();
            return true;
        }
        KeyCode::Down => {
            area.set_offset(area.offset().saturating_add(SCROLL_LINE_STEP));
            return true;
        }
        KeyCode::PageUp => {
            let new = area.offset().saturating_sub(SCROLL_PAGE_STEP);
            area.set_offset(new);
            area.disable_auto_scroll();
            return true;
        }
        KeyCode::PageDown => {
            area.set_offset(area.offset().saturating_add(SCROLL_PAGE_STEP));
            return true;
        }
        KeyCode::Home => {
            area.set_offset(0);
            area.disable_auto_scroll();
            return true;
        }
        KeyCode::End => {
            area.handle_end();
            return true;
        }
        _ => {}
    }
    false
}

/// 处理非焦点区域的 Enter/Char/Backspace 命令输入（切换到 CommandInput 焦点）
fn handle_command_passthrough(key: KeyEvent, state: &mut AppState) -> AppAction {
    match key.code {
        KeyCode::Enter => {
            let cmd = state.command_buffer.text.clone();
            if !cmd.is_empty() {
                state.command_buffer = Default::default();
                return AppAction::SubmitCommand(cmd);
            }
        }
        KeyCode::Char(_) if is_ctrl_char(key, 'u') => {
            state.command_buffer.clear();
            state.focus_zone = FocusZone::CommandInput;
            state.needs_redraw = true;
        }
        KeyCode::Char(c) => {
            state.command_buffer.input_char(c);
            state.focus_zone = FocusZone::CommandInput;
            state.needs_redraw = true;
        }
        KeyCode::Backspace if state.command_buffer.cursor > 0 => {
            state.command_buffer.input_backspace();
            state.focus_zone = FocusZone::CommandInput;
            state.needs_redraw = true;
        }
        _ => {}
    }
    AppAction::None
}

// ── 各区域的 ScrollArea 实现 ────────────────────────────────────

struct TableScroll<'a>(&'a mut AppState);
impl ScrollArea for TableScroll<'_> {
    fn offset(&self) -> u16 {
        self.0.table_scroll_offset
    }
    fn set_offset(&mut self, val: u16) {
        self.0.table_scroll_offset = val;
    }
}

struct InfoLogScroll<'a>(&'a mut AppState);
impl ScrollArea for InfoLogScroll<'_> {
    fn offset(&self) -> u16 {
        self.0.info_log_scroll
    }
    fn set_offset(&mut self, val: u16) {
        self.0.info_log_scroll = val;
    }
    fn disable_auto_scroll(&mut self) {
        self.0.info_log_auto_scroll = false;
    }
    fn handle_end(&mut self) {
        self.0.info_log_auto_scroll = true;
    }
}

struct DetailLogScroll<'a>(&'a mut AppState);
impl ScrollArea for DetailLogScroll<'_> {
    fn offset(&self) -> u16 {
        self.0.detail_log_scroll
    }
    fn set_offset(&mut self, val: u16) {
        self.0.detail_log_scroll = val;
    }
    fn disable_auto_scroll(&mut self) {
        self.0.detail_log_auto_scroll = false;
    }
    fn handle_end(&mut self) {
        self.0.detail_log_auto_scroll = true;
    }
}

// ── 简化后的各区域处理函数 ─────────────────────────────────────

fn handle_table_scroll(key: KeyEvent, state: &mut AppState) -> AppAction {
    let mut area = TableScroll(state);
    if handle_scroll_keys(key.code, &mut area) {
        state.needs_redraw = true;
        return AppAction::None;
    }
    handle_command_passthrough(key, state)
}

fn handle_info_log_scroll(key: KeyEvent, state: &mut AppState) -> AppAction {
    let mut area = InfoLogScroll(state);
    if handle_scroll_keys(key.code, &mut area) {
        state.needs_redraw = true;
        return AppAction::None;
    }
    handle_command_passthrough(key, state)
}

fn handle_detail_log_scroll(key: KeyEvent, state: &mut AppState) -> AppAction {
    let mut area = DetailLogScroll(state);
    if handle_scroll_keys(key.code, &mut area) {
        state.needs_redraw = true;
        return AppAction::None;
    }
    // detail_log 独有：'t' 键切换自动滚动
    if key.code == KeyCode::Char('t') {
        state.detail_log_auto_scroll = !state.detail_log_auto_scroll;
        state.needs_redraw = true;
        return AppAction::None;
    }
    handle_command_passthrough(key, state)
}

/// 处理对话框通用滚动键（Up/Down/PageUp/PageDown/Home/End），返回是否已处理。
fn handle_dialog_scroll_keys(key: KeyCode, state: &mut AppState) -> bool {
    match key {
        KeyCode::Up => {
            if state.dialog_scroll > 0 {
                state.dialog_scroll = state.dialog_scroll.saturating_sub(SCROLL_LINE_STEP);
                state.needs_redraw = true;
            }
        }
        KeyCode::Down => {
            state.dialog_scroll = state.dialog_scroll.saturating_add(SCROLL_LINE_STEP);
            state.needs_redraw = true;
        }
        KeyCode::PageUp => {
            state.dialog_scroll = state.dialog_scroll.saturating_sub(SCROLL_PAGE_STEP);
            state.needs_redraw = true;
        }
        KeyCode::PageDown => {
            state.dialog_scroll = state.dialog_scroll.saturating_add(SCROLL_PAGE_STEP);
            state.needs_redraw = true;
        }
        KeyCode::Home => {
            state.dialog_scroll = 0;
            state.needs_redraw = true;
        }
        KeyCode::End => {
            state.dialog_scroll = u16::MAX;
            state.needs_redraw = true;
        }
        _ => return false,
    }
    true
}

fn handle_key_confirm(key: KeyEvent, state: &mut AppState) -> AppAction {
    if handle_dialog_scroll_keys(key.code, state) {
        return AppAction::None;
    }
    match key.code {
        KeyCode::Char('y' | 'Y') => {
            state.ui_mode = UiMode::Normal;
            state.confirm_message = None;
            state.dialog_scroll = 0;
            AppAction::Confirm
        }
        KeyCode::Char('n' | 'N') | KeyCode::Esc => {
            state.ui_mode = UiMode::Normal;
            state.confirm_message = None;
            state.confirm_callback = None;
            state.dialog_scroll = 0;
            AppAction::Cancel
        }
        _ => AppAction::None,
    }
}

fn handle_key_check_result(key: KeyEvent, state: &mut AppState) -> AppAction {
    if handle_dialog_scroll_keys(key.code, state) {
        return AppAction::None;
    }
    match key.code {
        KeyCode::Esc => {
            state.ui_mode = UiMode::Normal;
            state.check_data = None;
            state.dialog_scroll = 0;
            AppAction::DismissDialog
        }
        _ => AppAction::None,
    }
}

fn handle_key_settings(key: KeyEvent, state: &mut AppState) -> AppAction {
    if let Some(ref mut ss) = state.settings_state {
        if ss.is_editing_field() {
            let cat = ss.current_category();
            let idx = ss.focus.field_index;

            if cat.is_bool_field(idx) {
                match key.code {
                    KeyCode::Enter | KeyCode::Char(' ') => {
                        ss.toggle_boolean();
                        ss.cancel_edit_current_field();
                        state.needs_redraw = true;
                        return AppAction::None;
                    }
                    KeyCode::Left | KeyCode::Right => {
                        ss.toggle_boolean();
                        state.needs_redraw = true;
                        return AppAction::None;
                    }
                    KeyCode::Esc => {
                        ss.cancel_edit_current_field();
                        state.needs_redraw = true;
                        return AppAction::None;
                    }
                    _ => return AppAction::None,
                }
            }

            let action = handle_settings_text_input(key, ss);
            state.needs_redraw = true;
            return action;
        }
    }

    match key.code {
        KeyCode::Char(_) if is_ctrl_char(key, 'c') => {
            // Ctrl+C 在设置页非编辑态：退出设置
            if let Some(ref mut ss) = state.settings_state {
                if ss.dirty {
                    ss.cancel_edit_current_field();
                }
            }
            state.close_settings();
            AppAction::DiscardSettings
        }
        KeyCode::Esc => {
            if let Some(ref mut ss) = state.settings_state {
                if ss.dirty {
                    ss.cancel_edit_current_field();
                }
            }
            state.close_settings();
            AppAction::DiscardSettings
        }
        KeyCode::Char(_) if is_ctrl_char(key, 's') => AppAction::SaveSettings,
        KeyCode::Char(_) if is_ctrl_char(key, 'z') => {
            if let Some(ref mut ss) = state.settings_state {
                ss.undo();
                state.needs_redraw = true;
            }
            AppAction::None
        }
        KeyCode::Left => {
            let area = state.terminal_size;
            if let Some(ref mut ss) = state.settings_state {
                let visible_columns =
                    settings_workstation_visible_columns(area, ss.config.workstations.len());
                ss.scroll_workstation_columns_left(visible_columns);
                state.needs_redraw = true;
            }
            AppAction::None
        }
        KeyCode::Right => {
            let area = state.terminal_size;
            if let Some(ref mut ss) = state.settings_state {
                let visible_columns =
                    settings_workstation_visible_columns(area, ss.config.workstations.len());
                ss.scroll_workstation_columns_right(visible_columns);
                state.needs_redraw = true;
            }
            AppAction::None
        }
        KeyCode::Up => {
            if let Some(ref mut ss) = state.settings_state {
                ss.move_focus_up();
                state.needs_redraw = true;
            }
            AppAction::None
        }
        KeyCode::Down => {
            if let Some(ref mut ss) = state.settings_state {
                ss.move_focus_down();
                state.needs_redraw = true;
            }
            AppAction::None
        }
        KeyCode::Tab => {
            if let Some(ref mut ss) = state.settings_state {
                ss.move_focus_next();
                state.needs_redraw = true;
            }
            AppAction::None
        }
        KeyCode::BackTab => {
            if let Some(ref mut ss) = state.settings_state {
                ss.move_focus_prev();
                state.needs_redraw = true;
            }
            AppAction::None
        }
        KeyCode::Enter => {
            if let Some(ref mut ss) = state.settings_state {
                let cat = ss.current_category();
                let idx = ss.focus.field_index;
                if cat.is_bool_field(idx) {
                    ss.toggle_boolean();
                } else {
                    ss.begin_edit_current_field();
                }
                state.needs_redraw = true;
            }
            AppAction::None
        }
        KeyCode::PageUp => {
            if let Some(ref mut ss) = state.settings_state {
                ss.scroll = ss.scroll.saturating_sub(SCROLL_PAGE_STEP);
                state.needs_redraw = true;
            }
            AppAction::None
        }
        KeyCode::PageDown => {
            if let Some(ref mut ss) = state.settings_state {
                ss.scroll = ss.scroll.saturating_add(SCROLL_PAGE_STEP);
                state.needs_redraw = true;
            }
            AppAction::None
        }
        KeyCode::Home => {
            if let Some(ref mut ss) = state.settings_state {
                ss.scroll = 0;
                state.needs_redraw = true;
            }
            AppAction::None
        }
        KeyCode::End => {
            if let Some(ref mut ss) = state.settings_state {
                ss.scroll = u16::MAX;
                state.needs_redraw = true;
            }
            AppAction::None
        }
        _ => AppAction::None,
    }
}

fn handle_settings_text_input(key: KeyEvent, ss: &mut crate::settings::SettingsState) -> AppAction {
    match key.code {
        KeyCode::Esc => {
            ss.cancel_edit_current_field();
            AppAction::None
        }
        KeyCode::Enter => {
            ss.commit_edit_current_field();
            AppAction::None
        }
        // ── Clipboard shortcuts ──────────────────────────────────────
        KeyCode::Char(_) if is_ctrl_char(key, 'a') => {
            ss.select_all();
            AppAction::None
        }
        KeyCode::Char(_) if is_ctrl_char(key, 'c') => {
            ss.copy_selection();
            AppAction::None
        }
        KeyCode::Char(_) if is_ctrl_char(key, 'x') => {
            ss.cut_selection();
            AppAction::None
        }
        KeyCode::Char(_) if is_ctrl_char(key, 'v') => {
            ss.paste_from_clipboard();
            AppAction::None
        }
        // ── Regular editing ──────────────────────────────────────────
        KeyCode::Char(c) => {
            ss.input_char(c);
            AppAction::None
        }
        KeyCode::Backspace => {
            ss.input_backspace();
            AppAction::None
        }
        KeyCode::Delete => {
            ss.input_delete();
            AppAction::None
        }
        KeyCode::Left => {
            ss.move_cursor_left();
            AppAction::None
        }
        KeyCode::Right => {
            ss.move_cursor_right();
            AppAction::None
        }
        KeyCode::Home => {
            ss.move_cursor_home();
            AppAction::None
        }
        KeyCode::End => {
            ss.move_cursor_end();
            AppAction::None
        }
        _ => AppAction::None,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::text_buffer::TextBuffer;

    fn key(code: KeyCode, modifiers: KeyModifiers) -> KeyEvent {
        KeyEvent::new(code, modifiers)
    }

    #[test]
    fn ctrl_u_clears_command_input_before_next_command() {
        let mut state = AppState::new();
        state.command_buffer = TextBuffer::with_text("stale".to_string());

        let action = handle_key(key(KeyCode::Char('u'), KeyModifiers::CONTROL), &mut state);

        assert!(matches!(action, AppAction::None));
        assert_eq!(state.command_buffer.text, "");
        assert_eq!(state.command_buffer.cursor, 0);
    }

    #[test]
    fn ctrl_u_from_non_command_focus_clears_and_returns_to_command_input() {
        let mut state = AppState::new();
        state.focus_zone = FocusZone::DetailLog;
        state.command_buffer = TextBuffer::with_text("stale".to_string());

        let action = handle_key(key(KeyCode::Char('u'), KeyModifiers::CONTROL), &mut state);

        assert!(matches!(action, AppAction::None));
        assert_eq!(state.focus_zone, FocusZone::CommandInput);
        assert_eq!(state.command_buffer.text, "");
        assert_eq!(state.command_buffer.cursor, 0);
    }

    #[test]
    fn settings_left_right_scroll_workstation_columns_when_not_editing() {
        let mut state = AppState::new();
        state.terminal_size = ratatui::layout::Rect::new(0, 0, 60, 40);
        state.open_settings();
        let ss = state.settings_state.as_mut().expect("settings state");
        ss.config.workstations = vec![
            crate::settings::WorkstationConfig {
                id: "WS-A".to_string(),
                ..Default::default()
            },
            crate::settings::WorkstationConfig {
                id: "WS-B".to_string(),
                ..Default::default()
            },
            crate::settings::WorkstationConfig {
                id: "WS-C".to_string(),
                ..Default::default()
            },
            crate::settings::WorkstationConfig {
                id: "WS-D".to_string(),
                ..Default::default()
            },
        ];

        let right = handle_key(key(KeyCode::Right, KeyModifiers::NONE), &mut state);
        assert!(matches!(right, AppAction::None));
        assert_eq!(
            state
                .settings_state
                .as_ref()
                .expect("settings state")
                .workstation_column_offset,
            1
        );

        let left = handle_key(key(KeyCode::Left, KeyModifiers::NONE), &mut state);
        assert!(matches!(left, AppAction::None));
        assert_eq!(
            state
                .settings_state
                .as_ref()
                .expect("settings state")
                .workstation_column_offset,
            0
        );
    }

    #[test]
    fn settings_right_scroll_caps_at_visible_workstation_window() {
        let mut state = AppState::new();
        state.terminal_size = ratatui::layout::Rect::new(0, 0, 60, 40);
        state.open_settings();
        let ss = state.settings_state.as_mut().expect("settings state");
        ss.config.workstations = vec![
            crate::settings::WorkstationConfig {
                id: "WS-A".to_string(),
                ..Default::default()
            },
            crate::settings::WorkstationConfig {
                id: "WS-B".to_string(),
                ..Default::default()
            },
            crate::settings::WorkstationConfig {
                id: "WS-C".to_string(),
                ..Default::default()
            },
            crate::settings::WorkstationConfig {
                id: "WS-D".to_string(),
                ..Default::default()
            },
        ];

        handle_key(key(KeyCode::Right, KeyModifiers::NONE), &mut state);
        handle_key(key(KeyCode::Right, KeyModifiers::NONE), &mut state);

        assert_eq!(
            state
                .settings_state
                .as_ref()
                .expect("settings state")
                .workstation_column_offset,
            1
        );
    }

    #[test]
    fn settings_scroll_keeps_workstation_focus_visible() {
        let mut state = AppState::new();
        state.terminal_size = ratatui::layout::Rect::new(0, 0, 60, 40);
        state.open_settings();
        let ss = state.settings_state.as_mut().expect("settings state");
        ss.config.workstations = vec![
            crate::settings::WorkstationConfig {
                id: "WS-A".to_string(),
                ..Default::default()
            },
            crate::settings::WorkstationConfig {
                id: "WS-B".to_string(),
                ..Default::default()
            },
            crate::settings::WorkstationConfig {
                id: "WS-C".to_string(),
                ..Default::default()
            },
            crate::settings::WorkstationConfig {
                id: "WS-D".to_string(),
                ..Default::default()
            },
        ];
        ss.set_focus(1, 0, Some(0));

        handle_key(key(KeyCode::Right, KeyModifiers::NONE), &mut state);

        let ss = state.settings_state.as_ref().expect("settings state");
        assert_eq!(ss.workstation_column_offset, 1);
        assert_eq!(ss.focus.workstation_index, Some(1));
    }

    #[test]
    fn settings_left_right_keep_text_cursor_behavior_while_editing() {
        let mut state = AppState::new();
        state.open_settings();
        let ss = state.settings_state.as_mut().expect("settings state");
        ss.set_focus(1, 0, Some(0));
        ss.buffer = TextBuffer::with_text("abcd".to_string());
        ss.buffer.move_cursor_end();
        ss.focus.editing = true;

        handle_key(key(KeyCode::Left, KeyModifiers::NONE), &mut state);

        let ss = state.settings_state.as_ref().expect("settings state");
        assert_eq!(ss.workstation_column_offset, 0);
        assert_eq!(ss.buffer.cursor, 3);
    }

    #[test]
    fn settings_ctrl_c_while_editing_does_not_close_or_mutate_field() {
        let mut state = AppState::new();
        state.open_settings();
        let ss = state.settings_state.as_mut().expect("settings state");
        ss.buffer = TextBuffer::with_text("abcd".to_string());
        ss.buffer.select_all();
        ss.focus.editing = true;

        let action = handle_key(
            key(
                KeyCode::Char('C'),
                KeyModifiers::CONTROL | KeyModifiers::SHIFT,
            ),
            &mut state,
        );

        assert!(matches!(action, AppAction::None));
        assert!(!state.should_quit);
        let ss = state.settings_state.as_ref().expect("settings state");
        assert!(ss.focus.editing);
        assert_eq!(ss.buffer.text, "abcd");
        assert_eq!(state.ui_mode, UiMode::Settings);
    }

    #[test]
    fn settings_ctrl_c_when_not_editing_closes_settings() {
        let mut state = AppState::new();
        state.open_settings();

        let action = handle_key(key(KeyCode::Char('c'), KeyModifiers::CONTROL), &mut state);

        assert!(matches!(action, AppAction::DiscardSettings));
        assert!(!state.should_quit);
        assert!(state.settings_state.is_none());
        assert_eq!(state.ui_mode, UiMode::Normal);
    }

    #[test]
    fn settings_escape_while_editing_cancels_edit_only() {
        let mut state = AppState::new();
        state.open_settings();
        let ss = state.settings_state.as_mut().expect("settings state");
        ss.buffer = TextBuffer::with_text("abcd".to_string());
        ss.focus.editing = true;

        let action = handle_key(key(KeyCode::Esc, KeyModifiers::NONE), &mut state);

        assert!(matches!(action, AppAction::None));
        let ss = state.settings_state.as_ref().expect("settings state");
        assert!(!ss.focus.editing);
        assert_eq!(state.ui_mode, UiMode::Settings);
    }
}
