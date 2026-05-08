use ratatui::Frame;
use ratatui::style::{Color, Modifier, Style};
use ratatui::widgets::{Block, Borders, Cell, Row, Table, Scrollbar, ScrollbarOrientation, ScrollbarState};

use crate::state::app_state::{AppState, FocusZone, STEP_NAMES, status_icon, status_color, step_display_name};

pub fn render_table(frame: &mut Frame, area: ratatui::layout::Rect, state: &AppState, scrollbar_state: &mut ScrollbarState) {
    let visible_data_rows = area.height.saturating_sub(3) as usize;
    let total_rows = state.configs.len();
    let scroll = state.table_scroll_offset as usize;

    let header_cells: Vec<Cell> = std::iter::once(Cell::new("构型").style(Style::default().add_modifier(Modifier::BOLD)))
        .chain(STEP_NAMES.iter().map(|step| {
            Cell::new(step_display_name(step)).style(Style::default().add_modifier(Modifier::BOLD))
        }))
        .collect();

    let border_style = if state.focus_zone == FocusZone::Table {
        Style::default().fg(Color::Rgb(233, 69, 96))
    } else {
        Style::default().fg(Color::Rgb(51, 51, 51))
    };

    let header = Row::new(header_cells)
        .style(Style::default().fg(Color::Rgb(233, 69, 96)).bg(Color::Rgb(22, 33, 62)))
        .height(1);

    let visible_configs: Vec<u64> = state.configs.iter()
        .skip(scroll)
        .take(visible_data_rows)
        .copied()
        .collect();

    let rows: Vec<Row> = visible_configs.iter().enumerate().map(|(i, cn)| {
        let cn_str = cn.to_string();
        let is_hovered = state.hovered_table_row == Some(scroll as u16 + i as u16);
        let row_style = if is_hovered {
            Style::default().bg(Color::Rgb(15, 52, 96)).add_modifier(Modifier::BOLD)
        } else {
            Style::default()
        };

        let cells: Vec<Cell> = std::iter::once(Cell::new(cn_str.clone()))
            .chain(STEP_NAMES.iter().map(|step| {
                let status = state.get_step_status(&cn_str, step);
                let icon = status_icon(status);
                let color = status_color(status);
                let text = format!("{} {}", icon, status);
                Cell::new(text).style(Style::default().fg(color))
            }))
            .collect();
        Row::new(cells).height(1).style(row_style)
    }).collect();

    let widths = {
        let config_width = ratatui::layout::Constraint::Length(8);
        let step_widths = STEP_NAMES.iter().map(|_| ratatui::layout::Constraint::Length(18));
        std::iter::once(config_width).chain(step_widths).collect::<Vec<_>>()
    };

    let block = Block::default()
        .borders(Borders::ALL)
        .border_style(border_style)
        .style(Style::default().bg(Color::Rgb(26, 26, 46)));
    let inner = block.inner(area);

    let table = Table::new(rows, &widths)
        .header(header)
        .block(block)
        .style(Style::default().fg(Color::Rgb(224, 224, 224)));

    frame.render_widget(table, area);

    if total_rows > visible_data_rows {
        *scrollbar_state = ScrollbarState::new(total_rows)
            .viewport_content_length(visible_data_rows)
            .position(scroll);
        let scrollbar_area = ratatui::layout::Rect {
            x: inner.x + inner.width.saturating_sub(1),
            y: inner.y,
            width: 1,
            height: inner.height,
        };
        let scrollbar = Scrollbar::new(ScrollbarOrientation::VerticalRight)
            .style(Style::default().fg(Color::Rgb(100, 100, 100)));
        frame.render_stateful_widget(scrollbar, scrollbar_area, scrollbar_state);
    }
}
