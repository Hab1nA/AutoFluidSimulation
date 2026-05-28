//! AutoFluid TUI 主题系统。
//!
//! 定义与 ratatui-themes ThemePalette 兼容的 10 字段语义色结构，
//! 并扩展了 UI 交互所需的额外颜色字段。
//! 后续升级到 ratatui 0.30+ 后可直接替换为 ratatui_themes::ThemePalette。

use ratatui::style::{Color, Modifier, Style};

/// 核心语义色板（与 ratatui-themes ThemePalette 的 10 字段完全对齐）。
#[derive(Debug, Clone, Copy)]
pub struct ThemePalette {
    pub accent: Color,
    pub secondary: Color,
    pub bg: Color,
    pub fg: Color,
    pub muted: Color,
    pub selection: Color,
    pub error: Color,
    pub warning: Color,
    pub success: Color,
    pub info: Color,
}

/// AutoFluid TUI 完整主题。
#[derive(Debug, Clone, Copy)]
pub struct AppTheme {
    pub palette: ThemePalette,
    pub input_bg: Color,
    pub border_inactive: Color,
    pub table_bg: Color,
    pub panel_bg: Color,
    pub gray_1: Color,
    pub gray_3: Color,
    pub gray_4: Color,
    pub gray_5: Color,
    pub scrollbar_track: Color,
    pub scrollbar_thumb: Color,
    pub cursor_fg: Color,
    pub click_fg: Color,
    pub click_bg: Color,
}

impl std::ops::Deref for AppTheme {
    type Target = ThemePalette;
    fn deref(&self) -> &ThemePalette {
        &self.palette
    }
}

impl AppTheme {
    pub fn btn_normal(&self) -> Style {
        Style::default().fg(self.fg).bg(self.secondary)
    }
    pub fn btn_hover(&self) -> Style {
        Style::default()
            .fg(Color::Rgb(255, 255, 255))
            .bg(self.accent)
            .add_modifier(Modifier::BOLD)
    }
    pub fn btn_click(&self) -> Style {
        Style::default()
            .fg(self.click_fg)
            .bg(self.click_bg)
            .add_modifier(Modifier::BOLD)
    }
    pub fn border_style_for(&self, focused: bool) -> Style {
        if focused {
            Style::default().fg(self.accent)
        } else {
            Style::default().fg(self.border_inactive)
        }
    }
    pub fn title_style(&self) -> Style {
        Style::default()
            .fg(self.accent)
            .add_modifier(Modifier::BOLD)
    }
    pub fn detail_title_style(&self) -> Style {
        Style::default().fg(self.info).add_modifier(Modifier::BOLD)
    }
    pub fn hover_style(&self) -> Style {
        Style::default()
            .bg(self.secondary)
            .add_modifier(Modifier::BOLD)
    }
    /// 对话框/设置页面通用按钮样式（hover/click/normal 三态）。
    pub fn dialog_btn_style(&self, idx: u8, hovered: Option<u8>, clicked: Option<u8>) -> Style {
        if clicked == Some(idx) {
            self.btn_click()
        } else if hovered == Some(idx) {
            self.btn_hover()
        } else {
            self.btn_normal()
        }
    }
}

impl Default for AppTheme {
    fn default() -> Self {
        Self {
            palette: ThemePalette {
                accent: Color::Rgb(233, 69, 96),
                secondary: Color::Rgb(15, 52, 96),
                bg: Color::Rgb(22, 33, 62),
                fg: Color::Rgb(224, 224, 224),
                muted: Color::Rgb(80, 80, 80),
                selection: Color::Rgb(26, 26, 46),
                error: Color::Rgb(255, 68, 68),
                warning: Color::Rgb(255, 136, 0),
                success: Color::Rgb(0, 255, 136),
                info: Color::Rgb(0, 188, 240),
            },
            input_bg: Color::Rgb(13, 13, 13),
            border_inactive: Color::Rgb(51, 51, 51),
            table_bg: Color::Rgb(26, 26, 46),
            panel_bg: Color::Rgb(13, 13, 13),
            gray_1: Color::Rgb(51, 51, 51),
            gray_3: Color::Rgb(120, 120, 120),
            gray_4: Color::Rgb(180, 180, 180),
            gray_5: Color::Rgb(200, 200, 200),
            scrollbar_track: Color::Rgb(80, 80, 80),
            scrollbar_thumb: Color::Rgb(160, 160, 160),
            cursor_fg: Color::Rgb(0, 0, 0),
            click_fg: Color::Rgb(0, 0, 0),
            click_bg: Color::Rgb(255, 255, 255),
        }
    }
}
