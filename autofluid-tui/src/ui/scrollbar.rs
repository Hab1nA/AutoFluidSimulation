use ratatui::buffer::Buffer;
use ratatui::layout::Rect;
use ratatui::style::{Color, Style};
use ratatui::widgets::Widget;

const TRACK_STYLE: Style = Style::new().fg(Color::Rgb(80, 80, 80));
const THUMB_STYLE: Style = Style::new().fg(Color::Rgb(160, 160, 160));

fn calc_thumb_size(visible: usize, total: usize, track_length: usize) -> usize {
    let size = ((visible as f64 / total as f64) * track_length as f64).round() as usize;
    size.max(1).min(track_length)
}

pub struct ScrollbarThumbInfo {
    pub thumb_start: usize,
    pub thumb_size: usize,
    pub track_space: usize,
}

pub struct VerticalScrollbar {
    pub total: usize,
    pub visible: usize,
    pub scroll: usize,
}

impl VerticalScrollbar {
    pub fn thumb_info(&self, track_length: usize) -> Option<ScrollbarThumbInfo> {
        if self.total <= self.visible || track_length == 0 {
            return None;
        }

        let thumb_size = calc_thumb_size(self.visible, self.total, track_length);
        let track_space = track_length - thumb_size;
        let max_scroll = self.total - self.visible;

        let thumb_start = if max_scroll == 0 {
            0
        } else {
            ((self.scroll as f64 / max_scroll as f64) * track_space as f64).round() as usize
        };

        Some(ScrollbarThumbInfo {
            thumb_start,
            thumb_size,
            track_space,
        })
    }

    pub fn scroll_from_thumb_position(&self, thumb_pos: usize, track_length: usize) -> usize {
        let ti = match self.thumb_info(track_length) {
            Some(ti) => ti,
            None => return self.scroll,
        };
        if ti.track_space == 0 {
            return 0;
        }
        let max_scroll = self.total - self.visible;
        let clamped_pos = thumb_pos.min(ti.track_space);
        let new_scroll = ((clamped_pos as f64 / ti.track_space as f64) * max_scroll as f64).round() as usize;
        new_scroll.min(max_scroll)
    }
}

impl Widget for VerticalScrollbar {
    fn render(self, area: Rect, buf: &mut Buffer) {
        let track_height = area.height as usize;
        if self.total <= self.visible || track_height == 0 {
            return;
        }

        let max_scroll = self.total - self.visible;

        let thumb_size = calc_thumb_size(self.visible, self.total, track_height);

        let track_space = track_height - thumb_size;

        let thumb_start = if max_scroll == 0 {
            0
        } else {
            ((self.scroll as f64 / max_scroll as f64) * track_space as f64).round() as usize
        };

        let x = area.x;
        for i in 0..track_height {
            let y = area.y + i as u16;
            if i >= thumb_start && i < thumb_start + thumb_size {
                buf.set_string(x, y, "█", THUMB_STYLE);
            } else {
                buf.set_string(x, y, "│", TRACK_STYLE);
            }
        }
    }
}

pub struct HorizontalScrollbar {
    pub total: usize,
    pub visible: usize,
    pub scroll: usize,
}

impl HorizontalScrollbar {
    pub fn thumb_info(&self, track_length: usize) -> Option<ScrollbarThumbInfo> {
        if self.total <= self.visible || track_length == 0 {
            return None;
        }

        let thumb_size = calc_thumb_size(self.visible, self.total, track_length);

        let track_space = track_length - thumb_size;
        let max_scroll = self.total - self.visible;

        let scroll = self.scroll.min(max_scroll);
        let thumb_start = if max_scroll == 0 {
            0
        } else {
            ((scroll as f64 / max_scroll as f64) * track_space as f64).round() as usize
        };

        Some(ScrollbarThumbInfo {
            thumb_start,
            thumb_size,
            track_space,
        })
    }

    pub fn scroll_from_thumb_position(&self, thumb_pos: usize, track_length: usize) -> usize {
        let ti = match self.thumb_info(track_length) {
            Some(ti) => ti,
            None => return self.scroll,
        };
        if ti.track_space == 0 {
            return 0;
        }
        let max_scroll = self.total - self.visible;
        let clamped_pos = thumb_pos.min(ti.track_space);
        let new_scroll = ((clamped_pos as f64 / ti.track_space as f64) * max_scroll as f64).round() as usize;
        new_scroll.min(max_scroll)
    }
}

impl Widget for HorizontalScrollbar {
    fn render(self, area: Rect, buf: &mut Buffer) {
        let track_width = area.width as usize;
        if self.total <= self.visible || track_width == 0 {
            return;
        }

        let max_scroll = self.total - self.visible;
        let scroll = self.scroll.min(max_scroll);

        let thumb_size = calc_thumb_size(self.visible, self.total, track_width);

        let track_space = track_width - thumb_size;

        let thumb_start = if max_scroll == 0 {
            0
        } else {
            ((scroll as f64 / max_scroll as f64) * track_space as f64).round() as usize
        };

        let y = area.y;
        for i in 0..track_width {
            let x = area.x + i as u16;
            if i >= thumb_start && i < thumb_start + thumb_size {
                buf.set_string(x, y, "█", THUMB_STYLE);
            } else {
                buf.set_string(x, y, "─", TRACK_STYLE);
            }
        }
    }
}
