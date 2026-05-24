use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};

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
        KeyCode::Char('c') if key.modifiers.contains(KeyModifiers::CONTROL)
            && state.focus_zone != FocusZone::CommandInput =>
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
        KeyCode::Char('a') if key.modifiers.contains(KeyModifiers::CONTROL) => {
            state.command_buffer.select_all();
            state.needs_redraw = true;
            AppAction::None
        }
        KeyCode::Char('c') if key.modifiers.contains(KeyModifiers::CONTROL) => {
            state.command_buffer.copy_selection();
            AppAction::None
        }
        KeyCode::Char('x') if key.modifiers.contains(KeyModifiers::CONTROL) => {
            state.command_buffer.cut_selection();
            state.needs_redraw = true;
            AppAction::None
        }
        KeyCode::Char('v') if key.modifiers.contains(KeyModifiers::CONTROL) => {
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
            area.set_offset(area.offset() - 1);
            area.disable_auto_scroll();
            return true;
        }
        KeyCode::Down => {
            area.set_offset(area.offset().saturating_add(1));
            return true;
        }
        KeyCode::PageUp => {
            let new = area.offset().saturating_sub(10);
            area.set_offset(new);
            area.disable_auto_scroll();
            return true;
        }
        KeyCode::PageDown => {
            area.set_offset(area.offset().saturating_add(10));
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
fn handle_command_passthrough(key: KeyCode, state: &mut AppState) -> AppAction {
    match key {
        KeyCode::Enter => {
            let cmd = state.command_buffer.text.clone();
            if !cmd.is_empty() {
                state.command_buffer = Default::default();
                return AppAction::SubmitCommand(cmd);
            }
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
    fn offset(&self) -> u16 { self.0.table_scroll_offset }
    fn set_offset(&mut self, val: u16) { self.0.table_scroll_offset = val; }
}

struct InfoLogScroll<'a>(&'a mut AppState);
impl ScrollArea for InfoLogScroll<'_> {
    fn offset(&self) -> u16 { self.0.info_log_scroll }
    fn set_offset(&mut self, val: u16) { self.0.info_log_scroll = val; }
    fn disable_auto_scroll(&mut self) { self.0.info_log_auto_scroll = false; }
    fn handle_end(&mut self) { self.0.info_log_auto_scroll = true; }
}

struct DetailLogScroll<'a>(&'a mut AppState);
impl ScrollArea for DetailLogScroll<'_> {
    fn offset(&self) -> u16 { self.0.detail_log_scroll }
    fn set_offset(&mut self, val: u16) { self.0.detail_log_scroll = val; }
    fn disable_auto_scroll(&mut self) { self.0.detail_log_auto_scroll = false; }
    fn handle_end(&mut self) { self.0.detail_log_auto_scroll = true; }
}

// ── 简化后的各区域处理函数 ─────────────────────────────────────

fn handle_table_scroll(key: KeyEvent, state: &mut AppState) -> AppAction {
    let mut area = TableScroll(state);
    if handle_scroll_keys(key.code, &mut area) {
        state.needs_redraw = true;
        return AppAction::None;
    }
    handle_command_passthrough(key.code, state)
}

fn handle_info_log_scroll(key: KeyEvent, state: &mut AppState) -> AppAction {
    let mut area = InfoLogScroll(state);
    if handle_scroll_keys(key.code, &mut area) {
        state.needs_redraw = true;
        return AppAction::None;
    }
    handle_command_passthrough(key.code, state)
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
    handle_command_passthrough(key.code, state)
}

/// 处理对话框通用滚动键（Up/Down/PageUp/PageDown/Home/End），返回是否已处理。
fn handle_dialog_scroll_keys(key: KeyCode, state: &mut AppState) -> bool {
    match key {
        KeyCode::Up => {
            if state.dialog_scroll > 0 {
                state.dialog_scroll -= 1;
                state.needs_redraw = true;
            }
        }
        KeyCode::Down => {
            state.dialog_scroll = state.dialog_scroll.saturating_add(1);
            state.needs_redraw = true;
        }
        KeyCode::PageUp => {
            if state.dialog_scroll >= 10 {
                state.dialog_scroll -= 10;
            } else {
                state.dialog_scroll = 0;
            }
            state.needs_redraw = true;
        }
        KeyCode::PageDown => {
            state.dialog_scroll = state.dialog_scroll.saturating_add(10);
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
        KeyCode::Char('c') if key.modifiers.contains(KeyModifiers::CONTROL) => {
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
        KeyCode::Char('s') if key.modifiers.contains(KeyModifiers::CONTROL) => {
            AppAction::SaveSettings
        }
        KeyCode::Char('z') if key.modifiers.contains(KeyModifiers::CONTROL) => {
            if let Some(ref mut ss) = state.settings_state {
                ss.undo();
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
                ss.scroll = ss.scroll.saturating_sub(10);
                state.needs_redraw = true;
            }
            AppAction::None
        }
        KeyCode::PageDown => {
            if let Some(ref mut ss) = state.settings_state {
                ss.scroll = ss.scroll.saturating_add(10);
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
        KeyCode::Char('a') if key.modifiers.contains(KeyModifiers::CONTROL) => {
            ss.select_all();
            AppAction::None
        }
        KeyCode::Char('c') if key.modifiers.contains(KeyModifiers::CONTROL) => {
            ss.copy_selection();
            AppAction::None
        }
        KeyCode::Char('x') if key.modifiers.contains(KeyModifiers::CONTROL) => {
            ss.cut_selection();
            AppAction::None
        }
        KeyCode::Char('v') if key.modifiers.contains(KeyModifiers::CONTROL) => {
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
