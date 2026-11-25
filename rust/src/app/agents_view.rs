use ratatui::Frame;
use ratatui::layout::{Constraint, Layout, Margin, Position, Rect};
use ratatui::style::{Style, Stylize};
use ratatui::text::{Line, Span};
use ratatui::widgets::{
    Block, BorderType, Borders, Cell, Clear, List, ListState, Paragraph, Row as TableRow, Table,
    TableState, Wrap,
};

use super::App;
use super::agents_actions::{DeleteDialog, DeleteProgress, SessionDialog, WorkspaceInput};
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
        let Source::Sessions {
            all_workspaces,
            dialog,
            notice,
            ..
        } = &picker.source
        else {
            return;
        };
        let rows = self.picker_rows(picker);
        let selected = rows.get(picker.selected);
        let message = picker.status(rows.is_empty());
        let feedback_text = notice
            .as_deref()
            .unwrap_or(if rows.is_empty() { "" } else { message });
        let area = area.inner(Margin::new(if area.width >= 48 { 2 } else { 1 }, 0));
        let show_full_controls = area.width >= 92;
        let [header, filters, content, feedback, footer] = Layout::vertical([
            Constraint::Length(2),
            Constraint::Length(2),
            Constraint::Min(1),
            Constraint::Length(u16::from(!feedback_text.is_empty())),
            Constraint::Length(if area.height < 8 {
                1
            } else if show_full_controls || area.height < 13 {
                2
            } else {
                3
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
        let (scope, short_scope) = if *all_workspaces {
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
        frame.render_widget(Paragraph::new(feedback_text).fg(palette.muted), feedback);
        let controls = if show_full_controls {
            "Esc  Back    ↑/↓  Select    Tab  Workspace    Ctrl+R  Refresh"
        } else {
            "Esc  Back    ↑/↓  Select    Tab  Workspace"
        };
        let mut footer_text = if footer.height >= 3 {
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
        let actions = if !self.delete_supported {
            "Ctrl+N  New session"
        } else if footer.width >= 44 {
            "Ctrl+N  New session    Delete  Delete session"
        } else {
            "Ctrl+N New    Del Delete"
        };
        footer_text.insert(0, Line::from(actions).fg(palette.accent));
        frame.render_widget(Paragraph::new(footer_text), footer);
        match dialog.as_deref() {
            Some(SessionDialog::Workspace(input)) => {
                draw_workspace_input(frame, area, input, palette)
            }
            Some(SessionDialog::Delete(dialog)) => draw_delete_dialog(frame, area, dialog, palette),
            None => {}
        }
    }
}

fn draw_workspace_input(frame: &mut Frame, area: Rect, input: &WorkspaceInput, palette: &Palette) {
    let height = 6 + input.suggestions.len().min(6) as u16;
    let body = agents_dialog(frame, area, "New session · Workspace path", height, palette);
    if body.is_empty() {
        return;
    }
    let [field, suggestions, feedback, footer] = Layout::vertical([
        Constraint::Length(2),
        Constraint::Min(0),
        Constraint::Length(1),
        Constraint::Length(1),
    ])
    .areas(body);
    if !field.is_empty() {
        let (rows, (row, col)) = input.editor.display(field.width);
        let scroll = row.saturating_sub(field.height - 1);
        let text: Vec<_> = rows
            .into_iter()
            .map(|range| Line::from(&input.editor.text()[range]))
            .collect();
        frame.render_widget(Paragraph::new(text).scroll((scroll, 0)), field);
        frame.set_cursor_position(Position::new(field.x + col, field.y + row - scroll));
    }
    if !input.suggestions.is_empty() {
        let mut state = ListState::default().with_selected(Some(input.selected));
        let list = List::new(input.suggestions.iter().map(String::as_str))
            .highlight_style(Style::default().fg(palette.background).bg(palette.accent));
        frame.render_stateful_widget(list, suggestions, &mut state);
    }
    if let Some(error) = input.error {
        frame.render_widget(Paragraph::new(error).fg(palette.error), feedback);
    }
    let hint = if input.suggestions.is_empty() {
        "Enter  Create    Tab  Complete path    Esc  Cancel"
    } else {
        "↑/↓  Select    Tab/Enter  Insert    Esc  Close suggestions"
    };
    frame.render_widget(Paragraph::new(hint).fg(palette.muted), footer);
}

fn draw_delete_dialog(frame: &mut Frame, area: Rect, dialog: &DeleteDialog, palette: &Palette) {
    let body = agents_dialog(frame, area, "Delete session", 9, palette);
    let [details, footer] =
        Layout::vertical([Constraint::Min(0), Constraint::Length(1)]).areas(body);
    let mut lines = vec![
        Line::from(dialog.title.as_str()).bold(),
        Line::from(dialog.workspace.as_str()).fg(palette.muted),
        Line::default(),
    ];
    let hint = match &dialog.progress {
        DeleteProgress::Confirm => {
            lines.push(
                Line::from("This permanently deletes the session and its history.")
                    .fg(palette.warning),
            );
            "Enter  Delete    Esc  Cancel"
        }
        DeleteProgress::Pending => "Deleting session…",
        DeleteProgress::Failed(error) => {
            lines.push(Line::from(error.as_str()).fg(palette.error));
            "Enter  Retry    Esc  Back"
        }
    };
    frame.render_widget(Paragraph::new(lines).wrap(Wrap { trim: false }), details);
    frame.render_widget(Paragraph::new(hint).fg(palette.muted), footer);
}

fn agents_dialog(
    frame: &mut Frame,
    area: Rect,
    title: &str,
    height: u16,
    palette: &Palette,
) -> Rect {
    let width = area.width.saturating_sub(2).min(76);
    let height = area.height.min(height);
    let popup = Rect::new(
        area.x + (area.width - width) / 2,
        area.y + (area.height - height) / 2,
        width,
        height,
    );
    let block = Block::bordered()
        .border_type(BorderType::Rounded)
        .border_style(Style::default().fg(palette.accent))
        .style(Style::default().fg(palette.foreground).bg(palette.surface))
        .title(title);
    let body = block.inner(popup);
    frame.render_widget(Clear, popup);
    frame.render_widget(block, popup);
    body
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
