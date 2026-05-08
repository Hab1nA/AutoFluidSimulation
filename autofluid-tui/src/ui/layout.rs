use ratatui::layout::{Constraint, Direction, Layout, Rect};

pub struct AppLayout {
    pub header: Rect,
    pub info_bar: Rect,
    pub status_table: Rect,
    pub info_panel: Rect,
    pub detail_panel: Rect,
    pub cmd_input: Rect,
    pub quick_buttons: Rect,
}

impl AppLayout {
    pub fn new(area: Rect) -> Self {
        let outer = Layout::default()
            .direction(Direction::Vertical)
            .constraints([
                Constraint::Length(3),
                Constraint::Length(1),
                Constraint::Min(8),
                Constraint::Length(5),
            ])
            .split(area);

        let header = outer[0];
        let info_bar = outer[1];
        let main_content = outer[2];
        let command_area = outer[3];

        let main_split = Layout::default()
            .direction(Direction::Vertical)
            .constraints([
                Constraint::Ratio(3, 5),
                Constraint::Ratio(2, 5),
            ])
            .split(main_content);

        let status_table = main_split[0];
        let log_panels = main_split[1];

        let log_split = Layout::default()
            .direction(Direction::Horizontal)
            .constraints([
                Constraint::Ratio(1, 2),
                Constraint::Ratio(1, 2),
            ])
            .split(log_panels);

        let info_panel = log_split[0];
        let detail_panel = log_split[1];

        let cmd_split = Layout::default()
            .direction(Direction::Vertical)
            .constraints([
                Constraint::Length(3),
                Constraint::Length(2),
            ])
            .split(command_area);

        let cmd_input = cmd_split[0];
        let quick_buttons = cmd_split[1];

        Self {
            header,
            info_bar,
            status_table,
            info_panel,
            detail_panel,
            cmd_input,
            quick_buttons,
        }
    }
}
