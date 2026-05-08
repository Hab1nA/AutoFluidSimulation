use ratatui::layout::{Constraint, Direction, Layout, Rect};

pub struct AppLayout {
    pub header: Rect,
    pub info_bar: Rect,
    pub main_content: Rect,
    pub status_table: Rect,
    pub log_panels: Rect,
    pub info_panel: Rect,
    pub info_panel_title: Rect,
    pub info_panel_body: Rect,
    pub detail_panel: Rect,
    pub detail_panel_title: Rect,
    pub detail_panel_body: Rect,
    pub command_area: Rect,
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
                Constraint::Min(10),
                Constraint::Length(4),
            ])
            .split(area);

        let header = outer[0];
        let info_bar = outer[1];
        let main_content = outer[2];
        let command_area = outer[3];

        let main_split = Layout::default()
            .direction(Direction::Vertical)
            .constraints([
                Constraint::Ratio(2, 3),
                Constraint::Ratio(1, 3),
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

        let info_title_split = Layout::default()
            .direction(Direction::Vertical)
            .constraints([
                Constraint::Length(1),
                Constraint::Min(1),
            ])
            .split(info_panel);

        let info_panel_title = info_title_split[0];
        let info_panel_body = info_title_split[1];

        let detail_title_split = Layout::default()
            .direction(Direction::Vertical)
            .constraints([
                Constraint::Length(1),
                Constraint::Min(1),
            ])
            .split(detail_panel);

        let detail_panel_title = detail_title_split[0];
        let detail_panel_body = detail_title_split[1];

        let cmd_split = Layout::default()
            .direction(Direction::Vertical)
            .constraints([
                Constraint::Length(1),
                Constraint::Length(2),
            ])
            .split(command_area);

        let cmd_input = cmd_split[0];
        let quick_buttons = cmd_split[1];

        Self {
            header,
            info_bar,
            main_content,
            status_table,
            log_panels,
            info_panel,
            info_panel_title,
            info_panel_body,
            detail_panel,
            detail_panel_title,
            detail_panel_body,
            command_area,
            cmd_input,
            quick_buttons,
        }
    }
}
