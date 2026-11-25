use ratatui::Frame;
use ratatui::layout::{Constraint, Layout, Margin, Rect};
use ratatui::style::{Style, Stylize};
use ratatui::text::{Line, Span};
use ratatui::widgets::{Block, Borders, Cell, Paragraph, Row as TableRow, Table, TableState, Wrap};

use super::App;
use super::picker::{Picker, Row, SessionDetails, Source};
use crate::acp::ExecutionStatus;
use crate::theme::Palette;

impl App {
    pub(super) fn draw_agents(
        &self,
        frame: &mut Frame,
        area: Rect,
        picker: &Picker,
        palette: &Palette,
    ) {
        let Source::Sessions { all_workspaces, .. } = picker.source else {
            return;
        };
        let rows = self.picker_rows(picker);
        let selected = rows.get(picker.selected);
        let message = picker.status(rows.is_empty());
        let area = area.inner(Margin::new(if area.width >= 48 { 2 } else { 1 }, 0));
        let show_full_controls = area.width >= 92;
        let [header, filters, content, feedback, footer] = Layout::vertical([
            Constraint::Length(2),
            Constraint::Length(2),
            Constraint::Min(1),
            Constraint::Length(u16::from(!rows.is_empty() && !message.is_empty())),
            Constraint::Length(if show_full_controls || area.height < 12 {
                1
            } else {
                2
            }),
        ])
        .areas(area);

        frame.render_widget(
            Paragraph::new(picker.title())
                .bold()
                .fg(palette.accent)
                .block(
                    Block::new()
                        .borders(Borders::BOTTOM)
                        .border_style(palette.border),
                ),
            header,
        );
        let (scope, short_scope) = if all_workspaces {
            ("All workspaces", "All")
        } else {
            ("Current workspace", "Current")
        };
        let [search, scope_area] = Layout::horizontal([
            Constraint::Min(0),
            Constraint::Length(if filters.width >= 48 { 19 } else { 8 }),
        ])
        .areas(filters);
        frame.render_widget(
            Paragraph::new(Line::from(vec![
                Span::styled("Search  ", Style::default().fg(palette.muted)),
                Span::raw(picker.query.as_str()),
            ])),
            search,
        );
        frame.render_widget(
            Paragraph::new(if filters.width >= 48 {
                scope
            } else {
                short_scope
            })
            .right_aligned()
            .fg(palette.foreground),
            scope_area,
        );

        let (list, details) = if content.width >= 92 {
            let [list, details] =
                Layout::horizontal([Constraint::Percentage(62), Constraint::Percentage(38)])
                    .spacing(2)
                    .areas(content);
            (list, details)
        } else if content.height >= 13 && selected.is_some() {
            let [list, details] = Layout::vertical([Constraint::Min(4), Constraint::Length(7)])
                .spacing(1)
                .areas(content);
            (list, details)
        } else {
            (content, Rect::default())
        };
        if rows.is_empty() {
            frame.render_widget(
                Paragraph::new(message)
                    .fg(palette.muted)
                    .wrap(Wrap { trim: true }),
                list,
            );
        } else {
            draw_session_list(frame, list, &rows, picker.selected, palette);
        }
        if !details.is_empty()
            && let Some(row) = selected
        {
            draw_session_details(frame, details, row, palette);
        }
        let (action, color) = if selected
            .and_then(|row| row.session.as_ref())
            .is_some_and(SessionDetails::attached_elsewhere)
        {
            ("Open in another TUI", palette.warning)
        } else if selected.is_some_and(|row| row.active) {
            ("Enter  Return to chat", palette.muted)
        } else if selected.is_some() {
            ("Enter  Open session", palette.muted)
        } else {
            ("", palette.muted)
        };
        frame.render_widget(Paragraph::new(message).fg(palette.muted), feedback);
        let controls = if show_full_controls {
            "Esc  Back    ↑/↓  Select    Tab  Workspace    Ctrl+R  Refresh"
        } else {
            "Esc  Back    ↑/↓  Select    Tab  Workspace"
        };
        let footer_text = if footer.height >= 2 {
            vec![
                Line::from(controls).fg(palette.muted),
                Line::from(action).fg(color),
            ]
        } else {
            vec![Line::from(vec![
                Span::styled(controls, Style::default().fg(palette.muted)),
                Span::styled(
                    if action.is_empty() { "" } else { "    " },
                    Style::default(),
                ),
                Span::styled(action, Style::default().fg(color)),
            ])]
        };
        frame.render_widget(Paragraph::new(footer_text), footer);
    }
}

fn draw_session_list(
    frame: &mut Frame,
    area: Rect,
    rows: &[Row],
    selected: usize,
    palette: &Palette,
) {
    let state_width = match area.width {
        52.. => 25,
        22.. => 12,
        _ => 0,
    };
    let mut table_rows = Vec::with_capacity(rows.len());
    let mut selected_row = None;
    let mut workspace = None;
    for (index, row) in rows.iter().enumerate() {
        let Some(session) = &row.session else {
            continue;
        };
        let path = session.workspace.as_str();
        if workspace != Some(path) {
            workspace = Some(path);
            let heading = workspace_heading(path, palette);
            table_rows.push(TableRow::new([Cell::from(heading), Cell::default()]));
        }
        if index == selected {
            selected_row = Some(table_rows.len());
        }
        let marker = if row.active { "  Current" } else { "" };
        let state = session.status();
        let owner = session.attached_elsewhere();
        let state_line = if !owner {
            Line::from(state).fg(status_color(session, palette))
        } else if state_width >= 25 {
            Line::from(vec![
                Span::styled(state, Style::default().fg(status_color(session, palette))),
                Span::styled("  Other TUI", Style::default().fg(palette.warning)),
            ])
        } else {
            Line::from("Other TUI").fg(palette.warning)
        };
        table_rows.push(TableRow::new([
            Cell::from(Line::from(vec![
                Span::raw("  "),
                Span::raw(row.label.as_str()),
                Span::styled(marker, Style::default().fg(palette.accent)),
            ])),
            Cell::from(state_line),
        ]));
    }
    let table = Table::new(
        table_rows,
        [Constraint::Min(0), Constraint::Length(state_width)],
    )
    .header(TableRow::new(["Session", "Status"]).fg(palette.muted))
    .column_spacing(1)
    .highlight_symbol("› ")
    .row_highlight_style(Style::default().bg(palette.panel).bold());
    let mut state = TableState::default().with_selected(selected_row);
    frame.render_stateful_widget(table, area, &mut state);
}

fn workspace_heading<'a>(path: &'a str, palette: &Palette) -> Line<'a> {
    let name = Path::new(path)
        .file_name()
        .and_then(|name| name.to_str())
        .unwrap_or(path);
    let mut line = Line::from(Span::raw(name).fg(palette.accent).bold());
    if name != path {
        line.push_span(Span::raw("  "));
        line.push_span(Span::raw(path).fg(palette.muted));
    }
    line
}

fn draw_session_details(frame: &mut Frame, area: Rect, row: &Row, palette: &Palette) {
    let Some(session) = &row.session else { return };
    let mut status = Line::from(session.status()).fg(status_color(session, palette));
    if session.attached_elsewhere() {
        status.push_span(Span::styled(
            "  Open in another TUI",
            Style::default().fg(palette.warning),
        ));
    }
    let mut lines = vec![
        Line::from(row.label.as_str()).bold(),
        status,
        Line::default(),
        Line::from("Workspace").fg(palette.muted),
        Line::from(session.workspace.as_str()),
    ];
    if let Some(updated) = &session.updated {
        lines.push(Line::from(vec![
            Span::raw("Updated  ").fg(palette.muted),
            Span::raw(updated),
        ]));
    }
    lines.push(Line::from(vec![
        Span::raw("ID  ").fg(palette.muted),
        Span::raw(row.value.as_str()),
    ]));
    let block = Block::new()
        .borders(Borders::TOP)
        .border_style(palette.border)
        .title(" Session details ")
        .style(Style::default().bg(palette.surface));
    frame.render_widget(
        Paragraph::new(lines).block(block).wrap(Wrap { trim: true }),
        area,
    );
}

fn status_color(session: &SessionDetails, palette: &Palette) -> ratatui::style::Color {
    match session.activity.as_ref().map(|activity| activity.status) {
        Some(ExecutionStatus::Running) => palette.success,
        Some(ExecutionStatus::Waiting) => palette.warning,
        _ => palette.muted,
    }
}
use std::path::Path;
