use ratatui::layout::Alignment;
use ratatui::style::{Modifier, Style};
use ratatui::text::Text;
use ratatui::widgets::{Block, Borders, Cell, Row, Table};
use ratatui::Frame;

use crate::state::app_state::{
    status_color, status_icon, step_display_name, AppState, FocusZone, STEP_NAMES,
};
use crate::ui::scrollbar;

pub fn render_table(frame: &mut Frame, area: ratatui::layout::Rect, state: &AppState) {
    let theme = &state.theme;
    let visible_data_rows = area.height.saturating_sub(3) as usize;
    let total_rows = state.configs.len();
    let scroll = state.table_scroll_offset as usize;

    let header_cells: Vec<Cell> = std::iter::once(
        Cell::new(Text::from("构型").alignment(Alignment::Center))
            .style(Style::default().add_modifier(Modifier::BOLD)),
    )
    .chain(STEP_NAMES.iter().map(|step| {
        Cell::new(Text::from(step_display_name(step)).alignment(Alignment::Center))
            .style(Style::default().add_modifier(Modifier::BOLD))
    }))
    .collect();

    let border_style = theme.border_style_for(state.focus_zone == FocusZone::Table);

    let header = Row::new(header_cells)
        .style(Style::default().fg(theme.accent).bg(theme.bg))
        .height(1);

    let visible_configs: Vec<u64> = state
        .configs
        .iter()
        .skip(scroll)
        .take(visible_data_rows)
        .copied()
        .collect();

    let rows: Vec<Row> = visible_configs
        .iter()
        .enumerate()
        .map(|(i, cn)| {
            let cn_str = cn.to_string();
            let is_hovered = state.hovered_table_row == Some(scroll as u16 + i as u16);
            let row_style = if is_hovered {
                theme.hover_style()
            } else {
                Style::default()
            };

            let cells: Vec<Cell> = std::iter::once(Cell::new(
                Text::from(cn_str.clone()).alignment(Alignment::Center),
            ))
            .chain(STEP_NAMES.iter().map(|step| {
                let status = state.get_step_status(&cn_str, step);
                let icon = status_icon(status);
                let color = status_color(status);
                let text = format!("{} {}", icon, status);
                Cell::new(Text::from(text).alignment(Alignment::Center))
                    .style(Style::default().fg(color))
            }))
            .collect();
            Row::new(cells).height(1).style(row_style)
        })
        .collect();

    let widths = {
        let config_width = ratatui::layout::Constraint::Length(8);
        let step_widths = STEP_NAMES
            .iter()
            .map(|_| ratatui::layout::Constraint::Length(18));
        std::iter::once(config_width)
            .chain(step_widths)
            .collect::<Vec<_>>()
    };

    let block = Block::default()
        .borders(Borders::ALL)
        .border_style(border_style)
        .style(Style::default().bg(theme.table_bg));
    let inner = block.inner(area);

    let table = Table::new(rows, &widths)
        .header(header)
        .block(block)
        .style(Style::default().fg(theme.fg));

    frame.render_widget(table, area);

    if total_rows > visible_data_rows {
        let scrollbar_area = ratatui::layout::Rect {
            x: inner.x + inner.width.saturating_sub(1),
            y: inner.y + 1,
            width: 1,
            height: inner.height.saturating_sub(1),
        };
        frame.render_widget(
            scrollbar::VerticalScrollbar {
                total: total_rows,
                visible: visible_data_rows,
                scroll,
                track_color: Some(theme.scrollbar_track),
                thumb_color: Some(theme.scrollbar_thumb),
            },
            scrollbar_area,
        );
    }
}
