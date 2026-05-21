use ratatui::buffer::Buffer;
use ratatui::layout::Rect;
use ratatui::style::{Color, Style};
use ratatui::widgets::Widget;

const DEFAULT_TRACK: Color = Color::Rgb(80, 80, 80);
const DEFAULT_THUMB: Color = Color::Rgb(160, 160, 160);

fn calc_thumb_size(visible: usize, total: usize, track_length: usize) -> usize {
    let size = ((visible as f64 / total as f64) * track_length as f64).round() as usize;
    size.max(1).min(track_length)
}

pub struct ScrollbarThumbInfo {
    pub thumb_start: usize,
    pub thumb_size: usize,
    pub track_space: usize,
}

fn thumb_info_impl(total: usize, visible: usize, scroll: usize, track_length: usize) -> Option<ScrollbarThumbInfo> {
    if total <= visible || track_length == 0 {
        return None;
    }
    let thumb_size = calc_thumb_size(visible, total, track_length);
    let track_space = track_length - thumb_size;
    let max_scroll = total - visible;
    let clamped = scroll.min(max_scroll);
    let thumb_start = if max_scroll == 0 {
        0
    } else {
        ((clamped as f64 / max_scroll as f64) * track_space as f64).round() as usize
    };
    Some(ScrollbarThumbInfo { thumb_start, thumb_size, track_space })
}

fn scroll_from_thumb_impl(total: usize, visible: usize, scroll: usize, thumb_pos: usize, track_length: usize) -> usize {
    let ti = match thumb_info_impl(total, visible, scroll, track_length) {
        Some(ti) => ti,
        None => return scroll,
    };
    if ti.track_space == 0 {
        return 0;
    }
    let max_scroll = total - visible;
    let clamped_pos = thumb_pos.min(ti.track_space);
    let new_scroll = ((clamped_pos as f64 / ti.track_space as f64) * max_scroll as f64).round() as usize;
    new_scroll.min(max_scroll)
}

pub struct VerticalScrollbar {
    pub total: usize,
    pub visible: usize,
    pub scroll: usize,
    pub track_color: Option<Color>,
    pub thumb_color: Option<Color>,
}

impl VerticalScrollbar {
    pub fn thumb_info(&self, track_length: usize) -> Option<ScrollbarThumbInfo> {
        thumb_info_impl(self.total, self.visible, self.scroll, track_length)
    }

    pub fn scroll_from_thumb_position(&self, thumb_pos: usize, track_length: usize) -> usize {
        scroll_from_thumb_impl(self.total, self.visible, self.scroll, thumb_pos, track_length)
    }
}

impl Widget for VerticalScrollbar {
    fn render(self, area: Rect, buf: &mut Buffer) {
        let track_height = area.height as usize;
        let ti = match thumb_info_impl(self.total, self.visible, self.scroll, track_height) {
            Some(ti) => ti,
            None => return,
        };
        let track_style = Style::default().fg(self.track_color.unwrap_or(DEFAULT_TRACK));
        let thumb_style = Style::default().fg(self.thumb_color.unwrap_or(DEFAULT_THUMB));
        let x = area.x;
        for i in 0..track_height {
            let y = area.y + i as u16;
            if i >= ti.thumb_start && i < ti.thumb_start + ti.thumb_size {
                buf.set_string(x, y, "█", thumb_style);
            } else {
                buf.set_string(x, y, "│", track_style);
            }
        }
    }
}

pub struct HorizontalScrollbar {
    pub total: usize,
    pub visible: usize,
    pub scroll: usize,
    pub track_color: Option<Color>,
    pub thumb_color: Option<Color>,
}

impl HorizontalScrollbar {
    pub fn thumb_info(&self, track_length: usize) -> Option<ScrollbarThumbInfo> {
        thumb_info_impl(self.total, self.visible, self.scroll, track_length)
    }

    pub fn scroll_from_thumb_position(&self, thumb_pos: usize, track_length: usize) -> usize {
        scroll_from_thumb_impl(self.total, self.visible, self.scroll, thumb_pos, track_length)
    }
}

impl Widget for HorizontalScrollbar {
    fn render(self, area: Rect, buf: &mut Buffer) {
        let track_width = area.width as usize;
        let ti = match thumb_info_impl(self.total, self.visible, self.scroll, track_width) {
            Some(ti) => ti,
            None => return,
        };
        let track_style = Style::default().fg(self.track_color.unwrap_or(DEFAULT_TRACK));
        let thumb_style = Style::default().fg(self.thumb_color.unwrap_or(DEFAULT_THUMB));
        let y = area.y;
        for i in 0..track_width {
            let x = area.x + i as u16;
            if i >= ti.thumb_start && i < ti.thumb_start + ti.thumb_size {
                buf.set_string(x, y, "█", thumb_style);
            } else {
                buf.set_string(x, y, "─", track_style);
            }
        }
    }
}
