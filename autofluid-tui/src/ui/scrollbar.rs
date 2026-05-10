use ratatui::buffer::Buffer;
use ratatui::layout::Rect;
use ratatui::style::{Color, Style};
use ratatui::widgets::Widget;

const TRACK_STYLE: Style = Style::new().fg(Color::Rgb(80, 80, 80));
const THUMB_STYLE: Style = Style::new().fg(Color::Rgb(160, 160, 160));

pub struct VerticalScrollbar {
    pub total: usize,
    pub visible: usize,
    pub scroll: usize,
}

impl Widget for VerticalScrollbar {
    fn render(self, area: Rect, buf: &mut Buffer) {
        let track_height = area.height as usize;
        if self.total <= self.visible || track_height == 0 {
            return;
        }

        let max_scroll = self.total - self.visible;

        let thumb_size = ((self.visible as f64 / self.total as f64) * track_height as f64).round() as usize;
        let thumb_size = thumb_size.max(1).min(track_height);

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

impl Widget for HorizontalScrollbar {
    fn render(self, area: Rect, buf: &mut Buffer) {
        let track_width = area.width as usize;
        if self.total <= self.visible || track_width == 0 {
            return;
        }

        let max_scroll = self.total - self.visible;
        let scroll = self.scroll.min(max_scroll);

        let thumb_size = ((self.visible as f64 / self.total as f64) * track_width as f64).round() as usize;
        let thumb_size = thumb_size.max(1).min(track_width);

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
