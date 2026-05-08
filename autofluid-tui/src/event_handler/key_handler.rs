use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};

use crate::state::app_state::{AppState, FocusZone, UiMode};

pub enum AppAction {
    None,
    Quit,
    SubmitCommand(String),
    Confirm,
    Cancel,
    DismissDialog,
}

pub fn handle_key(key: KeyEvent, state: &mut AppState) -> AppAction {
    match state.ui_mode {
        UiMode::Normal => handle_key_normal(key, state),
        UiMode::ConfirmDialog => handle_key_confirm(key, state),
        UiMode::CheckResult => handle_key_check_result(key, state),
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

fn char_to_byte_index(s: &str, char_idx: usize) -> usize {
    s.char_indices()
        .nth(char_idx)
        .map(|(i, _)| i)
        .unwrap_or(s.len())
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
            if state.command_cursor < state.command_input.len() {
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
            if state.command_cursor < state.command_input.len() {
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
            state.command_cursor = state.command_input.len();
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
            state.needs_redraw = true;
            AppAction::None
        }
        KeyCode::End => {
            state.info_log_scroll = u16::MAX;
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
        KeyCode::Char('y') | KeyCode::Char('Y') => {
            state.ui_mode = UiMode::Normal;
            state.confirm_message = None;
            AppAction::Confirm
        }
        KeyCode::Char('n') | KeyCode::Char('N') | KeyCode::Esc => {
            state.ui_mode = UiMode::Normal;
            state.confirm_message = None;
            state.confirm_callback = None;
            AppAction::Cancel
        }
        _ => AppAction::None,
    }
}

fn handle_key_check_result(key: KeyEvent, state: &mut AppState) -> AppAction {
    match key.code {
        KeyCode::Char('q') | KeyCode::Char('Q') | KeyCode::Esc => {
            state.ui_mode = UiMode::Normal;
            state.check_data = None;
            AppAction::DismissDialog
        }
        _ => AppAction::None,
    }
}
