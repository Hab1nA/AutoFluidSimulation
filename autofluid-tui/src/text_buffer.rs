//! 通用单行文本编辑缓冲区，支持光标移动、选区和系统剪贴板操作。
//!
//! 被 SettingsState（设置页面字段编辑）和 AppState（命令栏输入）共用。

use crate::utils::char_to_byte_index;

/// 单行文本编辑缓冲区。
///
/// 所有光标和选区索引均以 **字符**（`char`）为单位，而非字节。
/// 内部通过 `char_to_byte_index()` 转换为字节偏移来操作底层 `String`。
#[derive(Debug, Clone, Default)]
pub struct TextBuffer {
    /// 缓冲区文本内容。
    pub text: String,
    /// 光标位置（字符索引），取值范围 `[0, text.chars().count()]`。
    pub cursor: usize,
    /// 选区锚点（字符索引）。当 `Some(anchor)` 且 `anchor != cursor` 时，
    /// 选区覆盖 `min(anchor, cursor)..max(anchor, cursor)` 范围。
    pub selection_anchor: Option<usize>,
}

impl TextBuffer {
    // ── 构造 ──────────────────────────────────────────────────────

    /// 创建空的文本缓冲区。
    pub fn new() -> Self {
        Self::default()
    }

    /// 用给定文本创建缓冲区，光标置于末尾。
    pub fn with_text(text: String) -> Self {
        let cursor = text.chars().count();
        Self {
            text,
            cursor,
            selection_anchor: None,
        }
    }

    /// 清空缓冲区文本、光标和选区。
    pub fn clear(&mut self) {
        self.text.clear();
        self.cursor = 0;
        self.selection_anchor = None;
    }

    // ── 字符输入 ──────────────────────────────────────────────────

    /// 在光标位置插入单个字符（如有选区则先替换）。
    pub fn input_char(&mut self, c: char) {
        self.delete_selection();
        let byte_pos = char_to_byte_index(&self.text, self.cursor);
        self.text.insert(byte_pos, c);
        self.cursor += 1;
        self.selection_anchor = None;
    }

    /// 退格：删除光标前一个字符，或删除选区。
    pub fn input_backspace(&mut self) {
        if self.delete_selection().is_some() {
            return;
        }
        if self.cursor > 0 {
            self.cursor -= 1;
            let byte_pos = char_to_byte_index(&self.text, self.cursor);
            self.text.remove(byte_pos);
        }
    }

    /// 删除：删除光标后一个字符，或删除选区。
    pub fn input_delete(&mut self) {
        if self.delete_selection().is_some() {
            return;
        }
        if self.cursor < self.text.chars().count() {
            let byte_pos = char_to_byte_index(&self.text, self.cursor);
            self.text.remove(byte_pos);
        }
    }

    // ── 光标移动 ──────────────────────────────────────────────────

    /// 光标左移一格，清除选区。
    pub fn move_cursor_left(&mut self) {
        self.selection_anchor = None;
        if self.cursor > 0 {
            self.cursor -= 1;
        }
    }

    /// 光标右移一格，清除选区。
    pub fn move_cursor_right(&mut self) {
        self.selection_anchor = None;
        if self.cursor < self.text.chars().count() {
            self.cursor += 1;
        }
    }

    /// 光标移至行首，清除选区。
    pub fn move_cursor_home(&mut self) {
        self.selection_anchor = None;
        self.cursor = 0;
    }

    /// 光标移至行尾，清除选区。
    pub fn move_cursor_end(&mut self) {
        self.selection_anchor = None;
        self.cursor = self.text.chars().count();
    }

    // ── 选区与剪贴板 ─────────────────────────────────────────────

    /// 返回当前选区的 `(start, end)` 字符索引（`start <= end`），
    /// 无选区时返回 `None`。
    pub fn selection_range(&self) -> Option<(usize, usize)> {
        let anchor = self.selection_anchor?;
        if anchor == self.cursor {
            return None;
        }
        Some(if anchor < self.cursor {
            (anchor, self.cursor)
        } else {
            (self.cursor, anchor)
        })
    }

    /// 全选。
    pub fn select_all(&mut self) {
        let len = self.text.chars().count();
        if len == 0 {
            self.selection_anchor = None;
            return;
        }
        self.selection_anchor = Some(0);
        self.cursor = len;
    }

    /// 删除选区文本并返回被删除的内容，无选区时返回 `None`。
    pub fn delete_selection(&mut self) -> Option<String> {
        let (start, end) = self.selection_range()?;
        let selected: String = self.text.chars().skip(start).take(end - start).collect();
        let byte_start = char_to_byte_index(&self.text, start);
        let byte_end = char_to_byte_index(&self.text, end);
        self.text.drain(byte_start..byte_end);
        self.cursor = start;
        self.selection_anchor = None;
        Some(selected)
    }

    /// 复制选区文本到系统剪贴板，成功返回 `true`。
    pub fn copy_selection(&self) -> bool {
        let Some((start, end)) = self.selection_range() else {
            return false;
        };
        let text: String = self.text.chars().skip(start).take(end - start).collect();
        if text.is_empty() {
            return false;
        }
        clipboard_win::set_clipboard_string(&text).is_ok()
    }

    /// 剪切：先复制再删除选区。
    pub fn cut_selection(&mut self) -> bool {
        if self.selection_range().is_none() {
            return false;
        }
        let copied = self.copy_selection();
        if copied {
            self.delete_selection();
        }
        copied
    }

    /// 从系统剪贴板粘贴，替换当前选区（如有）。
    /// 自动去除换行符（单行缓冲区语义）。
    pub fn paste_from_clipboard(&mut self) -> bool {
        let text = match clipboard_win::get_clipboard_string() {
            Ok(s) => s,
            Err(_) => return false,
        };
        if text.is_empty() {
            return false;
        }
        let cleaned: String = text.chars().filter(|c| *c != '\n' && *c != '\r').collect();
        if cleaned.is_empty() {
            return false;
        }
        self.delete_selection();
        let byte_pos = char_to_byte_index(&self.text, self.cursor);
        self.text.insert_str(byte_pos, &cleaned);
        self.cursor += cleaned.chars().count();
        true
    }
}
