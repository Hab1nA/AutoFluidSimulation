use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};

use crate::state::app_state::{AppState, FocusZone, UiMode};
use crate::utils::char_to_byte_index;

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
        KeyCode::Char('c') if key.modifiers.contains(KeyModifiers::CONTROL) => {
            state.should_quit = true;
            AppAction::Quit
        }
        KeyCode::Char('q') if key.modifiers.contains(KeyModifiers::CONTROL) => {
            state.should_quit = true;
            AppAction::Quit
        }
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
            let cmd = state.command_input.clone();
            state.command_input.clear();
            state.command_cursor = 0;
            if !cmd.is_empty() {
                AppAction::SubmitCommand(cmd)
            } else {
                AppAction::None
            }
        }
        KeyCode::Char(c) => {
            let byte_pos = char_to_byte_index(&state.command_input, state.command_cursor);
            state.command_input.insert(byte_pos, c);
            state.command_cursor += 1;
            state.needs_redraw = true;
            AppAction::None
        }
        KeyCode::Backspace => {
            if state.command_cursor > 0 {
                state.command_cursor -= 1;
                let byte_pos = char_to_byte_index(&state.command_input, state.command_cursor);
                state.command_input.remove(byte_pos);
                state.needs_redraw = true;
            }
            AppAction::None
        }
        KeyCode::Delete => {
            if state.command_cursor < state.command_input.chars().count() {
                let byte_pos = char_to_byte_index(&state.command_input, state.command_cursor);
                state.command_input.remove(byte_pos);
                state.needs_redraw = true;
            }
            AppAction::None
        }
        KeyCode::Left => {
            if state.command_cursor > 0 {
                state.command_cursor -= 1;
                state.needs_redraw = true;
            }
            AppAction::None
        }
        KeyCode::Right => {
            if state.command_cursor < state.command_input.chars().count() {
                state.command_cursor += 1;
                state.needs_redraw = true;
            }
            AppAction::None
        }
        KeyCode::Home => {
            state.command_cursor = 0;
            state.needs_redraw = true;
            AppAction::None
        }
        KeyCode::End => {
            state.command_cursor = state.command_input.chars().count();
            state.needs_redraw = true;
            AppAction::None
        }
        _ => AppAction::None,
    }
}

fn handle_table_scroll(key: KeyEvent, state: &mut AppState) -> AppAction {
    match key.code {
        KeyCode::Up => {
            if state.table_scroll_offset > 0 {
                state.table_scroll_offset -= 1;
                state.needs_redraw = true;
            }
            AppAction::None
        }
        KeyCode::Down => {
            state.table_scroll_offset = state.table_scroll_offset.saturating_add(1);
            state.needs_redraw = true;
            AppAction::None
        }
        KeyCode::PageUp => {
            if state.table_scroll_offset >= 10 {
                state.table_scroll_offset -= 10;
            } else {
                state.table_scroll_offset = 0;
            }
            state.needs_redraw = true;
            AppAction::None
        }
        KeyCode::PageDown => {
            state.table_scroll_offset = state.table_scroll_offset.saturating_add(10);
            state.needs_redraw = true;
            AppAction::None
        }
        KeyCode::Home => {
            state.table_scroll_offset = 0;
            state.needs_redraw = true;
            AppAction::None
        }
        KeyCode::End => {
            state.table_scroll_offset = u16::MAX;
            state.needs_redraw = true;
            AppAction::None
        }
        KeyCode::Enter => {
            let cmd = state.command_input.clone();
            if !cmd.is_empty() {
                state.command_input.clear();
                state.command_cursor = 0;
                AppAction::SubmitCommand(cmd)
            } else {
                AppAction::None
            }
        }
        KeyCode::Char(c) => {
            let byte_pos = char_to_byte_index(&state.command_input, state.command_cursor);
            state.command_input.insert(byte_pos, c);
            state.command_cursor += 1;
            state.focus_zone = FocusZone::CommandInput;
            state.needs_redraw = true;
            AppAction::None
        }
        KeyCode::Backspace => {
            if state.command_cursor > 0 {
                state.command_cursor -= 1;
                let byte_pos = char_to_byte_index(&state.command_input, state.command_cursor);
                state.command_input.remove(byte_pos);
                state.focus_zone = FocusZone::CommandInput;
                state.needs_redraw = true;
            }
            AppAction::None
        }
        _ => AppAction::None,
    }
}

fn handle_info_log_scroll(key: KeyEvent, state: &mut AppState) -> AppAction {
    match key.code {
        KeyCode::Up => {
            if state.info_log_scroll > 0 {
                state.info_log_scroll -= 1;
                state.info_log_auto_scroll = false;
                state.needs_redraw = true;
            }
            AppAction::None
        }
        KeyCode::Down => {
            state.info_log_scroll = state.info_log_scroll.saturating_add(1);
            state.needs_redraw = true;
            AppAction::None
        }
        KeyCode::PageUp => {
            if state.info_log_scroll >= 10 {
                state.info_log_scroll -= 10;
            } else {
                state.info_log_scroll = 0;
            }
            state.info_log_auto_scroll = false;
            state.needs_redraw = true;
            AppAction::None
        }
        KeyCode::PageDown => {
            state.info_log_scroll = state.info_log_scroll.saturating_add(10);
            state.needs_redraw = true;
            AppAction::None
        }
        KeyCode::Home => {
            state.info_log_scroll = 0;
            state.info_log_auto_scroll = false;
            state.needs_redraw = true;
            AppAction::None
        }
        KeyCode::End => {
            state.info_log_auto_scroll = true;
            state.needs_redraw = true;
            AppAction::None
        }
        KeyCode::Enter => {
            let cmd = state.command_input.clone();
            if !cmd.is_empty() {
                state.command_input.clear();
                state.command_cursor = 0;
                AppAction::SubmitCommand(cmd)
            } else {
                AppAction::None
            }
        }
        KeyCode::Char(c) => {
            let byte_pos = char_to_byte_index(&state.command_input, state.command_cursor);
            state.command_input.insert(byte_pos, c);
            state.command_cursor += 1;
            state.focus_zone = FocusZone::CommandInput;
            state.needs_redraw = true;
            AppAction::None
        }
        KeyCode::Backspace => {
            if state.command_cursor > 0 {
                state.command_cursor -= 1;
                let byte_pos = char_to_byte_index(&state.command_input, state.command_cursor);
                state.command_input.remove(byte_pos);
                state.focus_zone = FocusZone::CommandInput;
                state.needs_redraw = true;
            }
            AppAction::None
        }
        _ => AppAction::None,
    }
}

fn handle_detail_log_scroll(key: KeyEvent, state: &mut AppState) -> AppAction {
    match key.code {
        KeyCode::Up => {
            if state.detail_log_scroll > 0 {
                state.detail_log_scroll -= 1;
                state.detail_log_auto_scroll = false;
                state.needs_redraw = true;
            }
            AppAction::None
        }
        KeyCode::Down => {
            state.detail_log_scroll = state.detail_log_scroll.saturating_add(1);
            state.needs_redraw = true;
            AppAction::None
        }
        KeyCode::PageUp => {
            if state.detail_log_scroll >= 10 {
                state.detail_log_scroll -= 10;
            } else {
                state.detail_log_scroll = 0;
            }
            state.detail_log_auto_scroll = false;
            state.needs_redraw = true;
            AppAction::None
        }
        KeyCode::PageDown => {
            state.detail_log_scroll = state.detail_log_scroll.saturating_add(10);
            state.needs_redraw = true;
            AppAction::None
        }
        KeyCode::Home => {
            state.detail_log_scroll = 0;
            state.detail_log_auto_scroll = false;
            state.needs_redraw = true;
            AppAction::None
        }
        KeyCode::End => {
            state.detail_log_auto_scroll = true;
            state.needs_redraw = true;
            AppAction::None
        }
        KeyCode::Char('t') => {
            state.detail_log_auto_scroll = !state.detail_log_auto_scroll;
            state.needs_redraw = true;
            AppAction::None
        }
        KeyCode::Enter => {
            let cmd = state.command_input.clone();
            if !cmd.is_empty() {
                state.command_input.clear();
                state.command_cursor = 0;
                AppAction::SubmitCommand(cmd)
            } else {
                AppAction::None
            }
        }
        KeyCode::Char(c) => {
            let byte_pos = char_to_byte_index(&state.command_input, state.command_cursor);
            state.command_input.insert(byte_pos, c);
            state.command_cursor += 1;
            state.focus_zone = FocusZone::CommandInput;
            state.needs_redraw = true;
            AppAction::None
        }
        KeyCode::Backspace => {
            if state.command_cursor > 0 {
                state.command_cursor -= 1;
                let byte_pos = char_to_byte_index(&state.command_input, state.command_cursor);
                state.command_input.remove(byte_pos);
                state.focus_zone = FocusZone::CommandInput;
                state.needs_redraw = true;
            }
            AppAction::None
        }
        _ => AppAction::None,
    }
}

fn handle_key_confirm(key: KeyEvent, state: &mut AppState) -> AppAction {
    match key.code {
        KeyCode::Up => {
            if state.dialog_scroll > 0 {
                state.dialog_scroll -= 1;
                state.needs_redraw = true;
            }
            AppAction::None
        }
        KeyCode::Down => {
            state.dialog_scroll = state.dialog_scroll.saturating_add(1);
            state.needs_redraw = true;
            AppAction::None
        }
        KeyCode::PageUp => {
            if state.dialog_scroll >= 10 {
                state.dialog_scroll -= 10;
            } else {
                state.dialog_scroll = 0;
            }
            state.needs_redraw = true;
            AppAction::None
        }
        KeyCode::PageDown => {
            state.dialog_scroll = state.dialog_scroll.saturating_add(10);
            state.needs_redraw = true;
            AppAction::None
        }
        KeyCode::Home => {
            state.dialog_scroll = 0;
            state.needs_redraw = true;
            AppAction::None
        }
        KeyCode::End => {
            state.dialog_scroll = u16::MAX;
            state.needs_redraw = true;
            AppAction::None
        }
        KeyCode::Char('y') | KeyCode::Char('Y') => {
            state.ui_mode = UiMode::Normal;
            state.confirm_message = None;
            state.dialog_scroll = 0;
            AppAction::Confirm
        }
        KeyCode::Char('n') | KeyCode::Char('N') | KeyCode::Esc => {
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
    match key.code {
        KeyCode::Up => {
            if state.dialog_scroll > 0 {
                state.dialog_scroll -= 1;
                state.needs_redraw = true;
            }
            AppAction::None
        }
        KeyCode::Down => {
            state.dialog_scroll = state.dialog_scroll.saturating_add(1);
            state.needs_redraw = true;
            AppAction::None
        }
        KeyCode::PageUp => {
            if state.dialog_scroll >= 10 {
                state.dialog_scroll -= 10;
            } else {
                state.dialog_scroll = 0;
            }
            state.needs_redraw = true;
            AppAction::None
        }
        KeyCode::PageDown => {
            state.dialog_scroll = state.dialog_scroll.saturating_add(10);
            state.needs_redraw = true;
            AppAction::None
        }
        KeyCode::Home => {
            state.dialog_scroll = 0;
            state.needs_redraw = true;
            AppAction::None
        }
        KeyCode::End => {
            state.dialog_scroll = u16::MAX;
            state.needs_redraw = true;
            AppAction::None
        }
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
