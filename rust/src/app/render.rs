use agent_client_protocol::v1::schema::{SessionConfigKind, SessionConfigOptionCategory, ToolKind};
use ratatui::Frame;
use ratatui::layout::{Constraint, Layout, Position, Rect};
use ratatui::style::{Style, Stylize};
use ratatui::text::{Line, Text};
use ratatui::widgets::{Block, BorderType, Borders, Clear, Paragraph, Wrap};

use super::App;
use crate::elicitation::FieldKind;
use crate::options;
use crate::theme::Palette;

impl App {
    pub fn draw(&mut self, frame: &mut Frame) {
        let palette = self.palette();
        let area = frame.area();
        let sidebar_width = if self.preferences.sidebar && area.width >= 96 {
            self.preferences.sidebar_width(area.width)
        } else {
            0
        };
        let (area, sidebar) = if sidebar_width > 0 {
            let [main, sidebar] =
                Layout::horizontal([Constraint::Min(40), Constraint::Length(sidebar_width)])
                    .areas(area);
            (main, Some(sidebar))
        } else {
            (area, None)
        };
        let prompt_height = area.height.clamp(4, 12) / 2;
        let mcp_height = if self.mcp_consent.front().is_some() {
            7
        } else {
            0
        };
        let approval_height = self.approvals.front().map_or(0, |request| {
            u16::try_from((request.options.len() + 4).min(12)).unwrap_or(12)
        });
        let elicitation_height = self.elicitations.front().map_or(0, |pending| {
            u16::try_from((pending.state.form.fields.len() + 6).min(16)).unwrap_or(16)
        });
        let completion_height = self.completion.as_ref().map_or(0, |completion| {
            u16::try_from(completion.items.len().min(6)).unwrap_or(6)
        });
        let [
            header,
            transcript,
            mcp,
            approval,
            elicitation,
            suggestions,
            composer,
            help,
        ] = Layout::vertical([
            Constraint::Length(1),
            Constraint::Min(1),
            Constraint::Length(mcp_height),
            Constraint::Length(approval_height),
            Constraint::Length(elicitation_height),
            Constraint::Length(completion_height),
            Constraint::Length(prompt_height),
            Constraint::Length(1),
        ])
        .areas(area);

        let status_color = match self.status {
            "Connected" => palette.success,
            "Disconnected" => palette.error,
            "Waiting" | "Cancelling" => palette.warning,
            _ => palette.accent,
        };
        let mut header_spans = vec![
            " hrns ".bold().fg(palette.accent),
            format!(" · {} · ", self.destination).into(),
            self.status.fg(status_color),
        ];
        if let Some(title) = &self.session_title {
            header_spans.push(format!(" · {title}").into());
        } else if let Some(workspace) = &self.workspace {
            header_spans.push(format!(" · {workspace}").into());
        }
        if let Some(model) = self.options.iter().find_map(|option| match &option.kind {
            SessionConfigKind::Select(select)
                if options::matches_category(option, &SessionConfigOptionCategory::Model) =>
            {
                Some(format!(" · {}", select.current_value))
            }
            _ => None,
        }) {
            header_spans.push(model.into());
        }
        let tokens = self
            .usage
            .map(|(used, size)| format!(" · {used}/{size}"))
            .or_else(|| {
                self.last_usage_tokens
                    .map(|tokens| format!(" · {tokens} tokens"))
            })
            .unwrap_or_default();
        if !tokens.is_empty() || self.usage_cost.is_some() {
            let cost = self
                .usage_cost
                .as_ref()
                .map(|cost| format!(" · {cost}"))
                .unwrap_or_default();
            header_spans.push(format!("{tokens}{cost}").into());
        }
        if self.attachment_reads > 0 {
            header_spans.push(" · Loading attachment".into());
        } else if !self.attachments.is_empty() {
            header_spans.push(format!(" · {} attached", self.attachments.len()).into());
        }
        if !self.queue.is_empty() {
            let state = if self.queue_paused {
                "queued · paused"
            } else {
                "queued"
            };
            header_spans.push(format!(" · {} {state}", self.queue.len()).into());
        }
        frame.render_widget(
            Paragraph::new(Line::from(header_spans)).style(Style::default().bg(palette.background)),
            header,
        );

        self.transcript_area = transcript;
        let visible = self
            .transcript
            .visible(transcript.width, transcript.height, &palette);
        frame.render_widget(
            Paragraph::new(visible).style(
                Style::default()
                    .fg(palette.foreground)
                    .bg(palette.background),
            ),
            transcript,
        );

        if let Some(pending) = self.mcp_consent.front() {
            let lines = vec![
                Line::from(format!(" MCP server · {}", pending.name))
                    .fg(palette.accent)
                    .bold(),
                Line::from(format!(" {} · {}", pending.transport, pending.target))
                    .fg(palette.muted),
                Line::from(" 1 allow once   2 allow for this connection   3 always allow   4 deny")
                    .fg(palette.foreground),
                Line::from(" y allow once · n deny · Esc deny").fg(palette.muted),
            ];
            frame.render_widget(Paragraph::new(lines).wrap(Wrap { trim: false }), mcp);
        }
        if let Some(request) = self.approvals.front() {
            frame.render_widget(
                Paragraph::new(self.approval_lines(request, &palette)).wrap(Wrap { trim: false }),
                approval,
            );
        }
        if let Some(pending) = self.elicitations.front() {
            let lines = elicitation_lines(pending, &palette);
            frame.render_widget(
                Paragraph::new(lines).wrap(Wrap { trim: false }),
                elicitation,
            );
        }

        if let Some(completion) = &self.completion {
            let rows: Vec<_> = completion
                .items
                .iter()
                .enumerate()
                .skip(completion.selected.saturating_sub(5))
                .take(6)
                .map(|(index, suggestion)| {
                    let text = format!(
                        " {}  {}",
                        suggestion.insert.trim_end(),
                        suggestion.description
                    );
                    if index == completion.selected {
                        Line::from(text).fg(palette.accent).bold()
                    } else {
                        Line::from(text).fg(palette.muted)
                    }
                })
                .collect();
            frame.render_widget(Paragraph::new(rows), suggestions);
        }

        let title = self.session.as_ref().map_or_else(
            || " Prompt ".to_owned(),
            |session| format!(" Prompt · {session} "),
        );
        let block = Block::default()
            .borders(Borders::TOP)
            .border_style(Style::default().fg(palette.border))
            .style(Style::default().bg(palette.surface))
            .title(title)
            .title_style(Style::default().fg(palette.accent));
        let input = block.inner(composer);
        frame.render_widget(block, composer);
        if input.width > 0 && input.height > 0 {
            let (rows, (row, column)) = self.draft.display(input.width);
            let scroll = row.saturating_sub(input.height - 1);
            let text = Paragraph::new(Text::from(
                rows.into_iter().map(Line::raw).collect::<Vec<_>>(),
            ))
            .style(
                Style::default()
                    .fg(palette.foreground)
                    .bg(palette.background),
            );
            frame.render_widget(text.scroll((scroll, 0)), input);
            if self.providers.is_none() && self.picker.is_none() {
                frame.set_cursor_position(Position::new(input.x + column, input.y + row - scroll));
            }
        }
        let help_text = if self.mcp_consent.front().is_some() {
            " 1 allow once · 2 until exit · 3 always · 4 deny · Esc deny"
        } else if !self.elicitations.is_empty() {
            " type to answer · 1-9 choose · Enter next · Esc decline"
        } else if self.approvals.front().is_some() {
            " 1-9 choose · y allow · a always · n reject · N always reject · Esc cancel"
        } else if self.transcript.output_pending() {
            " New output below · Ctrl+End"
        } else if self.status == "Disconnected" {
            " Ctrl-R reconnect · draft is kept · Ctrl-B panel"
        } else {
            "Enter send · Wheel scroll · Click a header for details · /help commands"
        };
        self.draw_footer(frame, help, Line::from(help_text).fg(palette.muted), &palette);
        if let Some(sidebar) = sidebar {
            self.draw_sidebar(frame, sidebar, &palette);
        }
        if self.approvals.is_empty() && self.elicitations.is_empty() && self.mcp_consent.is_empty()
        {
            if let Some(popup) = &self.providers {
                draw_providers(frame, area, popup, self.provider_editor, &palette);
            }
            if let Some(picker) = &self.picker {
                self.draw_picker(frame, area, picker, &palette);
            }
        }
    }

    fn draw_footer(&self, frame: &mut Frame, area: Rect, hint: Line<'_>, palette: &Palette) {
        let effort = self
            .options
            .iter()
            .find(|option| {
                options::matches_category(option, &SessionConfigOptionCategory::ThoughtLevel)
            })
            .and_then(|option| match &option.kind {
                SessionConfigKind::Select(select) => Some(select.current_value.0.as_ref()),
                SessionConfigKind::Boolean(toggle) => {
                    Some(if toggle.current_value { "on" } else { "off" })
                }
                _ => None,
            })
            .unwrap_or("—");
        let approvals = self.approval_mode.label();
        let mut settings = Line::from(vec![
            "Effort ".into(),
            effort.fg(palette.accent),
            " · Approvals ".into(),
            approvals.fg(palette.accent),
        ]);
        if settings.width() > usize::from(area.width) {
            settings = Line::from(vec![
                "Effort ".into(),
                effort.fg(palette.accent),
                " · ".into(),
                approvals.fg(palette.accent),
            ]);
        }
        let width = settings.width().min(usize::from(area.width)) as u16;
        let hints = Rect::new(
            area.x,
            area.y,
            area.width.saturating_sub(width).saturating_sub(1),
            area.height,
        );
        let values = Rect::new(area.right() - width, area.y, width, area.height);
        frame.render_widget(Paragraph::new(hint), hints);
        frame.render_widget(Paragraph::new(settings).fg(palette.muted), values);
    }

    fn draw_picker(
        &self,
        frame: &mut Frame,
        area: Rect,
        picker: &super::picker::Picker,
        palette: &Palette,
    ) {
        let width = area.width.saturating_sub(2).min(88);
        let height = area.height.saturating_sub(2).min(26);
        let popup = Rect::new(
            area.x + (area.width - width) / 2,
            area.y + (area.height - height) / 2,
            width,
            height,
        );
        let panel = Block::bordered()
            .border_type(BorderType::Rounded)
            .border_style(Style::default().fg(palette.accent))
            .style(Style::default().fg(palette.foreground).bg(palette.surface))
            .title(format!(" {} ", picker.title()));
        let inner = panel.inner(popup);
        frame.render_widget(Clear, popup);
        frame.render_widget(panel, popup);
        let [search, list, status, keys] = Layout::vertical([
            Constraint::Length(2),
            Constraint::Min(1),
            Constraint::Length(1),
            Constraint::Length(1),
        ])
        .areas(inner);
        let search_text = if picker.query.is_empty() {
            Line::from("  Search…").fg(palette.muted)
        } else {
            Line::from(format!("  Search  {}", picker.query)).fg(palette.accent)
        };
        frame.render_widget(Paragraph::new(search_text), search);

        let rows = self.picker_rows(picker);
        let page = usize::from(list.height / 2).max(1);
        let start = picker.selected.saturating_sub(page.saturating_sub(1));
        let lines: Vec<_> = rows
            .iter()
            .enumerate()
            .skip(start)
            .take(page)
            .flat_map(|(index, row)| {
                let selected = index == picker.selected;
                let marker = if selected { "›" } else { " " };
                let active = if row.active { "  ✓ current" } else { "" };
                let title = Line::from(format!(" {marker}  {}{active}", row.label));
                let detail = Line::from(format!("    {}", row.detail.trim())).fg(palette.muted);
                if selected {
                    [
                        title.fg(palette.accent).bold().bg(palette.panel),
                        detail.bg(palette.panel),
                    ]
                } else {
                    [title, detail]
                }
            })
            .collect();
        frame.render_widget(Paragraph::new(lines), list);
        frame.render_widget(
            Paragraph::new(picker.status(rows.is_empty())).fg(palette.muted),
            status,
        );
        frame.render_widget(Paragraph::new(picker.hint()).fg(palette.muted), keys);
    }

    fn approval_lines(
        &self,
        request: &crate::acp::PermissionRequest,
        palette: &Palette,
    ) -> Vec<Line<'static>> {
        let kind = request
            .kind
            .map(|kind| format!(" · {}", kind_label(kind)))
            .unwrap_or_default();
        let mut lines = vec![
            Line::from(format!(" Permission · {}{kind}", request.title))
                .fg(palette.warning)
                .bold(),
        ];
        let room = request.options.len().saturating_add(3);
        let detail: Vec<&str> = request
            .detail
            .lines()
            .take(room.saturating_sub(1))
            .collect();
        lines.extend(
            detail
                .into_iter()
                .map(|line| Line::from(format!(" {line}")).fg(palette.muted)),
        );
        let options = request
            .options
            .iter()
            .take(9)
            .enumerate()
            .map(|(index, option)| {
                let shortcut = match option.kind {
                    agent_client_protocol::v1::schema::PermissionOptionKind::AllowOnce => "y",
                    agent_client_protocol::v1::schema::PermissionOptionKind::AllowAlways => "a",
                    agent_client_protocol::v1::schema::PermissionOptionKind::RejectOnce => "n",
                    agent_client_protocol::v1::schema::PermissionOptionKind::RejectAlways => "N",
                    _ => "",
                };
                format!(" {} {shortcut}  {}", index + 1, option.name)
            })
            .collect::<Vec<_>>();
        lines.push(Line::from(options.join("   ")).fg(palette.foreground));
        lines
    }

    fn draw_sidebar(&self, frame: &mut Frame, area: ratatui::layout::Rect, palette: &Palette) {
        let heading = |text: &'static str| Line::from(text).fg(palette.accent).bold();
        let mut lines = vec![
            heading("Connection"),
            Line::from(self.destination.clone()).fg(palette.muted),
            Line::from(format!("{} · io {}", self.status, self.client_io.label()))
                .fg(palette.muted),
        ];
        if let Some(workspace) = &self.workspace {
            lines.push(Line::from(workspace.clone()).fg(palette.muted));
        }
        if let Some(session) = &self.session {
            lines.push(Line::from(session.clone()).fg(palette.muted));
        }
        if let Some(title) = &self.session_title {
            lines.push(Line::from(title.clone()).fg(palette.foreground));
        }
        if let Some(model) = self.options.iter().find_map(|option| match &option.kind {
            SessionConfigKind::Select(select)
                if options::matches_category(option, &SessionConfigOptionCategory::Model) =>
            {
                Some(select.current_value.clone())
            }
            _ => None,
        }) {
            lines.push(Line::from(format!("model {model}")).fg(palette.muted));
        }
        if let Some(mode) = &self.mode {
            lines.push(Line::from(format!("mode {mode}")).fg(palette.muted));
        }
        lines.push(
            Line::from(format!("permissions {}", self.approval_mode.label())).fg(palette.muted),
        );
        if let Some((used, size)) = self.usage {
            lines.push(Line::from(format!("usage {used}/{size}")).fg(palette.muted));
        }
        if let Some(cost) = &self.usage_cost {
            lines.push(Line::from(format!("cost {cost}")).fg(palette.muted));
        }
        lines.push(Line::from(""));
        lines.push(heading("Plan"));
        if self.plan.is_empty() {
            lines.push(Line::from("no plan").fg(palette.muted));
        } else {
            lines.extend(self.plan.iter().cloned().map(Line::from));
        }
        if !self.queue.is_empty() {
            lines.push(Line::from(""));
            lines.push(heading("Queue"));
            lines.push(Line::from(format!("{} waiting", self.queue.len())).fg(palette.muted));
        }
        if !self.skills.is_empty() {
            lines.push(Line::from(""));
            lines.push(heading("Skills"));
            lines.extend(
                self.skills
                    .iter()
                    .filter(|skill| skill.user_invocable)
                    .map(|skill| Line::from(format!("${}", skill.id)).fg(palette.muted)),
            );
        }
        if !self.mcp_health.is_empty() {
            lines.push(Line::from(""));
            lines.push(heading("MCP"));
            for item in &self.mcp_health {
                let scope = if item.export { "export" } else { "local" };
                lines.push(
                    Line::from(format!(
                        "{} · {} · {} · {scope}",
                        item.name, item.transport, item.state
                    ))
                    .fg(palette.muted),
                );
                if !item.detail.is_empty() {
                    lines.push(Line::from(format!("  {}", item.detail)).fg(palette.muted));
                }
            }
        }
        if !self.activity.is_empty() {
            lines.push(Line::from(""));
            lines.push(heading("Activity"));
            lines.extend(self.activity.iter().cloned().map(Line::from));
        }
        let block = Block::default()
            .borders(Borders::LEFT)
            .border_style(Style::default().fg(palette.border))
            .style(Style::default().bg(palette.panel))
            .title(" Panel ")
            .title_style(Style::default().fg(palette.accent));
        frame.render_widget(
            Paragraph::new(lines)
                .block(block)
                .wrap(Wrap { trim: false }),
            area,
        );
    }
}

fn draw_providers(
    frame: &mut Frame,
    area: Rect,
    popup: &super::ProviderPopup,
    editable: bool,
    palette: &Palette,
) {
    let providers = popup
        .result
        .as_ref()
        .and_then(|result| result.as_ref().ok());
    let rows = providers.map_or(1, |providers| providers.len().clamp(1, 8));
    let width = area.width.saturating_sub(2).min(76);
    let height = (rows as u16 + 9).min(area.height);
    let popup_area = Rect::new(
        area.x + (area.width - width) / 2,
        area.y + (area.height - height) / 2,
        width,
        height,
    );
    let block = Block::bordered()
        .border_type(BorderType::Rounded)
        .border_style(Style::default().fg(palette.accent))
        .style(Style::default().fg(palette.foreground).bg(palette.surface))
        .title(" Providers ")
        .title_style(Style::default().fg(palette.accent).bold());
    let inner = block.inner(popup_area);
    frame.render_widget(Clear, popup_area);
    frame.render_widget(block, popup_area);
    let [listing, details, help] = Layout::vertical([
        Constraint::Min(1),
        Constraint::Length(5),
        Constraint::Length(1),
    ])
    .areas(inner);
    let lines = match &popup.result {
        None => vec![Line::from(" Loading providers…").fg(palette.muted)],
        Some(Err(error)) => vec![Line::from(error.as_str()).fg(palette.error)],
        Some(Ok(providers)) if providers.is_empty() => {
            vec![Line::from(" No providers available").fg(palette.muted)]
        }
        Some(Ok(providers)) => {
            let rows = usize::from(listing.height);
            providers
                .iter()
                .enumerate()
                .skip(popup.selected.saturating_sub(rows.saturating_sub(1)))
                .take(rows)
                .map(|(index, provider)| {
                    let current = provider
                        .current
                        .as_ref()
                        .map_or("disabled", |config| config.api_type.as_str());
                    let selected = index == popup.selected;
                    let marker = if selected { "›" } else { " " };
                    let line = Line::from(format!(" {marker} {}  ·  {current}", provider.id));
                    if selected {
                        line.fg(palette.accent).bold()
                    } else {
                        line
                    }
                })
                .collect()
        }
    };
    if providers.is_some() {
        frame.render_widget(Paragraph::new(lines), listing);
    } else {
        frame.render_widget(
            Paragraph::new(lines).wrap(Wrap { trim: true }),
            listing.union(details),
        );
    }
    if let Some(provider) = providers.and_then(|providers| providers.get(popup.selected)) {
        let mut lines = vec![
            Line::from(format!(
                " {} · {}",
                if provider.required {
                    "Required"
                } else {
                    "Optional"
                },
                provider.supported.join(", ")
            ))
            .fg(palette.muted),
        ];
        if let Some(url) = provider
            .current
            .as_ref()
            .and_then(|config| config.base_url.as_deref())
        {
            lines.push(Line::from(format!(" {url}")));
        }
        let command = if editable {
            format!(
                " /provider edit {} <type> <url|-> [models] [key-env]",
                provider.id
            )
        } else {
            format!(" /provider set {} <type> [url]", provider.id)
        };
        lines.push(Line::from(command).fg(palette.muted));
        frame.render_widget(Paragraph::new(lines), details);
    }
    let selected = providers.and_then(|providers| providers.get(popup.selected));
    let hint = match (editable, selected) {
        (true, Some(provider)) if !provider.required => {
            " ↑/↓ select · Enter edit · a add · d remove · Esc close"
        }
        (true, Some(_)) => " ↑/↓ select · Enter edit · a add · Esc close",
        (true, None) if providers.is_some() => " a add · Esc close",
        (false, Some(provider)) if !provider.required => {
            " ↑/↓ select · Enter route · d disable · Esc close"
        }
        (false, Some(_)) => " ↑/↓ select · Enter route · Esc close",
        _ => " Esc close",
    };
    frame.render_widget(Paragraph::new(hint).fg(palette.muted), help);
}

fn kind_label(kind: ToolKind) -> &'static str {
    match kind {
        ToolKind::Read => "read",
        ToolKind::Edit => "edit",
        ToolKind::Delete => "delete",
        ToolKind::Move => "move",
        ToolKind::Search => "search",
        ToolKind::Execute => "execute",
        ToolKind::Think => "think",
        ToolKind::Fetch => "fetch",
        _ => "other",
    }
}

fn elicitation_lines(pending: &super::PendingElicitation, palette: &Palette) -> Vec<Line<'static>> {
    let state = &pending.state;
    let mut lines = vec![
        Line::from(format!(" Question · {}", state.form.message))
            .fg(palette.accent)
            .bold(),
    ];
    for (index, field) in state.form.fields.iter().enumerate() {
        let marker = if index == state.index { "›" } else { " " };
        let value = state
            .answers
            .get(&field.key)
            .map(answer_text)
            .unwrap_or_default();
        let current = index == state.index;
        let mut line = format!("{marker} {}", field.title);
        if !value.is_empty() {
            line.push_str(&format!(" · {value}"));
        }
        lines.push(if current {
            Line::from(line).fg(palette.foreground)
        } else {
            Line::from(line).fg(palette.muted)
        });
        if !current {
            continue;
        }
        if let Some(description) = &field.description {
            lines.push(Line::from(format!("  {description}")).fg(palette.muted));
        }
        match &field.kind {
            FieldKind::Choice { options, multiple } => {
                let selections = state.selections();
                for (index, option) in options.iter().take(6).enumerate() {
                    let selected = selections.get(index).copied().unwrap_or(false);
                    let marker = if selected { "●" } else { "○" };
                    let suffix = option
                        .detail
                        .as_ref()
                        .map(|detail| format!(" · {detail}"))
                        .unwrap_or_default();
                    lines.push(
                        Line::from(format!(
                            "  [{}] {marker} {}{suffix}",
                            index + 1,
                            option.label
                        ))
                        .fg(if selected {
                            palette.success
                        } else {
                            palette.foreground
                        }),
                    );
                }
                if *multiple {
                    lines.push(Line::from("  toggle with 1-6, Enter continues").fg(palette.muted));
                }
            }
            FieldKind::Boolean => {
                lines.push(Line::from("  [y] yes   [n] no").fg(palette.foreground))
            }
            FieldKind::Text { .. } => {
                lines.push(
                    Line::from(format!("  {}", state.draft))
                        .fg(palette.foreground)
                        .italic(),
                );
            }
        }
    }
    if let Some(error) = &state.error {
        lines.push(Line::from(format!(" {error}")).fg(palette.error));
    }
    lines
}

fn answer_text(value: &agent_client_protocol::v1::schema::ElicitationContentValue) -> String {
    use agent_client_protocol::v1::schema::ElicitationContentValue;
    match value {
        ElicitationContentValue::String(value) => value.clone(),
        ElicitationContentValue::Integer(value) => value.to_string(),
        ElicitationContentValue::Number(value) => value.to_string(),
        ElicitationContentValue::Boolean(value) => value.to_string(),
        ElicitationContentValue::StringArray(values) => values.join(", "),
        _ => String::new(),
    }
}
