use agent_client_protocol::v1::schema::{
    PlanEntryPriority, PlanEntryStatus, SessionConfigKind, SessionConfigOptionCategory, ToolKind,
};
use ratatui::Frame;
use ratatui::layout::{Constraint, Layout, Margin, Position, Rect};
use ratatui::style::{Style, Stylize};
use ratatui::text::{Line, Span, Text};
use ratatui::widgets::{Block, BorderType, Borders, Clear, List, ListState, Paragraph, Wrap};

use super::{App, PendingApproval, slash_command};
use crate::elicitation::FieldKind;
use crate::options;
use crate::theme::Palette;
use crate::transcript::wrap;

impl App {
    pub fn draw(&mut self, frame: &mut Frame) {
        let palette = self.palette();
        let area = frame.area();
        frame.render_widget(
            Block::default().style(
                Style::default()
                    .fg(palette.foreground)
                    .bg(palette.background),
            ),
            area,
        );
        let approval_active = !self.approvals.is_empty()
            && self.mcp_consent.is_empty()
            && self.elicitations.is_empty();
        let gutter = Margin::new(if area.width >= 80 { 2 } else { 1 }, 0);
        let prompt_height = if approval_active {
            0
        } else if area.height >= 12 {
            6
        } else {
            area.height / 2
        };
        let mcp_height = if self.mcp_consent.front().is_some() {
            7
        } else {
            0
        };
        let approval_height =
            self.approvals
                .front()
                .filter(|_| approval_active)
                .map_or(0, |pending| {
                    let width = usize::from(area.inner(gutter).width.saturating_sub(4).max(1));
                    let request = &pending.request;
                    let details =
                        wrap(&request.title, width).len() + wrap(&request.detail, width).len();
                    (3 + details.min(8) + request.options.len().min(9))
                        .min(usize::from(area.height.saturating_sub(3))) as u16
                });
        let question_limit = area.height.saturating_sub(prompt_height + 3).min(18);
        let question = self.elicitations.front().map(|pending| {
            elicitation_lines(
                pending,
                usize::from(question_limit.saturating_sub(2)),
                area.width,
                &palette,
            )
        });
        let elicitation_height = question.as_ref().map_or(0, |lines| {
            u16::try_from(lines.len() + 2)
                .unwrap_or(u16::MAX)
                .min(question_limit)
        });
        let completion_height = self
            .completion
            .as_ref()
            .filter(|_| !approval_active)
            .map_or(0, |completion| {
                u16::try_from(completion.items.len().min(6)).unwrap_or(6)
            });
        let [
            transcript,
            mcp,
            approval,
            elicitation,
            suggestions,
            composer,
            status,
            help,
        ] = Layout::vertical([
            Constraint::Min(1),
            Constraint::Length(mcp_height),
            Constraint::Length(approval_height),
            Constraint::Length(elicitation_height),
            Constraint::Length(completion_height),
            Constraint::Length(prompt_height),
            Constraint::Length(1),
            Constraint::Length(1),
        ])
        .areas(area);

        let sidebar_width = if (self.preferences.sidebar || self.todo_open) && area.width >= 96 {
            self.preferences.sidebar_width(area.width)
        } else {
            0
        };
        let [transcript, mut sidebar] =
            Layout::horizontal([Constraint::Min(0), Constraint::Length(sidebar_width)])
                .areas(transcript);
        if self.todo_open && sidebar.width == 0 {
            let width = (transcript.width / 2).max(36).min(transcript.width);
            sidebar = Rect::new(
                transcript.right() - width,
                transcript.y,
                width,
                transcript.height,
            );
        }
        let transcript = transcript.inner(gutter);
        self.transcript_area = transcript;
        if sidebar.width > 0 {
            self.transcript_area.width = self
                .transcript_area
                .width
                .min(sidebar.x.saturating_sub(transcript.x));
        }
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
        if approval_active && let Some(pending) = self.approvals.front_mut() {
            draw_approval(frame, approval.inner(gutter), pending, &palette);
        }
        if let Some(lines) = question {
            let block = Block::bordered()
                .border_type(BorderType::Rounded)
                .border_style(Style::default().fg(palette.accent))
                .style(Style::default().bg(palette.surface))
                .padding(ratatui::widgets::Padding::horizontal(1));
            frame.render_widget(
                Paragraph::new(lines).block(block).wrap(Wrap { trim: true }),
                elicitation.inner(gutter),
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
            frame.render_widget(Paragraph::new(rows), suggestions.inner(gutter));
        }

        let composer = composer.inner(gutter);
        let prompt_active = self.mcp_consent.is_empty()
            && self.approvals.is_empty()
            && self.elicitations.is_empty()
            && self.providers.is_none()
            && self.picker.is_none();
        let border = if prompt_active {
            palette.accent
        } else {
            palette.border
        };
        let mut block = Block::default().style(Style::default().bg(palette.surface));
        if composer.width >= 6 && composer.height >= 3 {
            block = block
                .borders(Borders::ALL)
                .border_type(BorderType::Rounded)
                .border_style(Style::default().fg(border));
        }
        let input = block.inner(composer);
        frame.render_widget(block, composer);
        if input.width > 2 && input.height > 0 {
            let [prompt, input] =
                Layout::horizontal([Constraint::Length(2), Constraint::Min(1)]).areas(input);
            frame.render_widget(Paragraph::new("›").fg(border).bold(), prompt);
            let (rows, (row, column)) = self.draft.display(input.width);
            let scroll = row.saturating_sub(input.height - 1);
            let text = if self.draft.text().is_empty() {
                Text::from("Send a message…").fg(palette.muted)
            } else {
                let mut remaining = slash_command(self.draft.text())
                    .filter(|command| self.is_command(command))
                    .map_or(0, str::len);
                Text::from(
                    rows.into_iter()
                        .map(|row| {
                            if remaining == 0 {
                                return Line::raw(row);
                            }
                            let (command, rest) = row.split_at(remaining.min(row.len()));
                            remaining -= command.len();
                            Line::from(vec![Span::raw(command).fg(palette.accent), Span::raw(rest)])
                        })
                        .collect::<Vec<_>>(),
                )
            };
            frame.render_widget(Paragraph::new(text).scroll((scroll, 0)), input);
            if prompt_active {
                frame.set_cursor_position(Position::new(input.x + column, input.y + row - scroll));
            }
        }
        let help_text = if self.mcp_consent.front().is_some() {
            "1 allow once · 2 until exit · 3 always · 4 deny · Esc deny"
        } else if let Some(pending) = self.elicitations.front() {
            question_hint(&pending.state)
        } else if self.approvals.front().is_some() {
            "PgUp/PgDn approval details"
        } else if self.transcript.output_pending() {
            "New output ↓ · Ctrl+End"
        } else if self.status == "Disconnected" {
            "Ctrl-R reconnect · Ctrl-B panel"
        } else if self.has_unknown_command(self.draft.text()) {
            if self.completion.is_some() {
                "Tab complete · /help commands"
            } else {
                "Unknown command · /help commands"
            }
        } else if self.provider_draft.is_some() {
            "Enter save provider · Esc return to draft"
        } else if self.prompt_setup_message().is_some() {
            "Set up a provider and model with /providers"
        } else {
            "Enter send · Wheel scroll · Click a header for details · /help commands"
        };
        let help_color = if prompt_active && self.transcript.output_pending() {
            palette.accent
        } else {
            palette.muted
        };
        let remaining_metrics = self.draw_footer(
            frame,
            help.inner(gutter),
            Line::from(help_text).fg(help_color),
            &palette,
        );
        self.draw_status(frame, status.inner(gutter), remaining_metrics, &palette);
        if sidebar.width > 0 {
            if self.todo_open {
                self.draw_todos(frame, sidebar, &palette);
            } else {
                self.draw_sidebar(frame, sidebar, &palette);
            }
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

    fn draw_footer(
        &self,
        frame: &mut Frame,
        area: Rect,
        hint: Line<'_>,
        palette: &Palette,
    ) -> Line<'_> {
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
        let mut metrics = self.usage_metrics();
        if !metrics.spans.is_empty()
            && metrics.width() + 3 + settings.width() <= usize::from(area.width)
        {
            metrics.push_span(" · ");
            metrics.spans.append(&mut settings.spans);
            settings = std::mem::take(&mut metrics);
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
        metrics
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

    fn draw_status(&self, frame: &mut Frame, area: Rect, metrics: Line<'_>, palette: &Palette) {
        let status_color = match self.status {
            "Connected" => palette.success,
            "Disconnected" => palette.error,
            "Waiting" | "Cancelling" => palette.warning,
            _ => palette.accent,
        };
        let mut left = vec!["● ".fg(status_color), self.status.fg(status_color)];
        if self.attachment_reads > 0 {
            left.push(" · Loading attachment".into());
        } else if !self.attachments.is_empty() {
            left.push(format!(" · {} attached", self.attachments.len()).into());
        }
        if !self.queue.is_empty() {
            let state = if self.queue_paused {
                "queued · paused"
            } else {
                "queued"
            };
            left.push(format!(" · {} {state}", self.queue.len()).into());
        }
        let context = self.session_title.as_deref().or(self.workspace.as_deref());
        if let Some(context) = context {
            left.push(format!(" · {context}").into());
        }
        left.push(format!(" · {}", self.destination).into());

        let mut right = Line::default();
        if let Some(model) = self.options.iter().find_map(|option| match &option.kind {
            SessionConfigKind::Select(select)
                if options::matches_category(option, &SessionConfigOptionCategory::Model) =>
            {
                Some(select.current_value.to_string())
            }
            _ => None,
        }) {
            right.push_span(model.fg(palette.accent).bold());
        }
        let available = usize::from(area.width.saturating_sub(16));
        let right_limit = available.max(metrics.width());
        if !metrics.spans.is_empty() {
            if right.spans.is_empty() || right.width() + 3 + metrics.width() > available {
                right = metrics;
            } else {
                right.push_span(" · ");
                right.spans.extend(metrics.spans);
            }
        }
        let right_width = right.width().min(right_limit).min(usize::from(area.width)) as u16;
        let [context, metrics] =
            Layout::horizontal([Constraint::Min(0), Constraint::Length(right_width)]).areas(area);
        let style = Style::default().fg(palette.muted);
        frame.render_widget(Paragraph::new(Line::from(left)).style(style), context);
        frame.render_widget(Paragraph::new(right).style(style), metrics);
    }

    fn usage_metrics(&self) -> Line<'_> {
        let usage = self
            .usage
            .map(|(used, size)| format!("ctx {used}/{size}"))
            .or_else(|| {
                self.last_usage_tokens
                    .map(|tokens| format!("{tokens} tokens"))
            });
        match (usage, self.usage_cost.as_deref()) {
            (Some(usage), Some(cost)) => Line::from(format!("{usage} · {cost}")),
            (Some(usage), None) => Line::from(usage),
            (None, Some(cost)) => Line::from(cost),
            (None, None) => Line::default(),
        }
    }

    fn draw_todos(&mut self, frame: &mut Frame, area: Rect, palette: &Palette) {
        let focused = self.todo_scroll.is_some();
        let block = Block::bordered()
            .border_type(BorderType::Rounded)
            .border_style(Style::default().fg(palette.accent))
            .style(Style::default().bg(palette.panel))
            .title(" Todos ")
            .title_style(Style::default().fg(palette.accent).bold());
        let inner = block.inner(area);
        frame.render_widget(Clear, area);
        frame.render_widget(block, area);
        let [summary, entries, hint] = Layout::vertical([
            Constraint::Length(1),
            Constraint::Min(0),
            Constraint::Length(u16::from(focused)),
        ])
        .areas(inner);
        let done = self
            .plan
            .iter()
            .filter(|item| item.status == PlanEntryStatus::Completed)
            .count();
        let summary_text = if self.plan.is_empty() {
            "No plan yet.".into()
        } else {
            format!("{done} of {} done", self.plan.len())
        };
        frame.render_widget(Paragraph::new(summary_text).fg(palette.muted), summary);
        if let Some(offset) = &mut self.todo_scroll {
            *offset =
                (*offset).min(u16::try_from(self.plan.len().saturating_sub(1)).unwrap_or(u16::MAX));
        }
        let lines = self
            .plan
            .iter()
            .skip(usize::from(self.todo_scroll.unwrap_or(0)))
            .map(|item| {
                let (marker, content) = match item.status {
                    PlanEntryStatus::Completed => ("●", item.content.as_str().fg(palette.muted)),
                    PlanEntryStatus::InProgress => ("◐", item.content.as_str().bold()),
                    _ => ("○", item.content.as_str().fg(palette.foreground)),
                };
                let mut line = Line::from(vec![marker.fg(palette.muted), " ".into(), content]);
                if item.priority == PlanEntryPriority::High {
                    line.push_span("  high".fg(palette.warning).bold());
                }
                line
            })
            .collect::<Vec<_>>();
        frame.render_widget(Paragraph::new(lines).wrap(Wrap { trim: false }), entries);
        if focused {
            frame.render_widget(
                Paragraph::new("PgUp/PgDn scroll · Esc close").fg(palette.muted),
                hint,
            );
        }
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
                lines.push(
                    Line::from(format!(
                        "{} · {} · {}",
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

fn draw_approval(frame: &mut Frame, area: Rect, pending: &mut PendingApproval, palette: &Palette) {
    let request = &pending.request;
    let title = request.kind.map_or_else(
        || " Approval ".into(),
        |kind| format!(" Approval · {} ", kind_label(kind)),
    );
    let block = Block::bordered()
        .border_type(BorderType::Rounded)
        .border_style(Style::default().fg(palette.warning))
        .style(Style::default().bg(palette.surface))
        .title(title)
        .title_style(Style::default().fg(palette.warning).bold())
        .padding(ratatui::widgets::Padding::horizontal(1));
    let inner = block.inner(area);
    frame.render_widget(block, area);
    if inner.is_empty() {
        return;
    }
    let hint_height = u16::from(inner.height >= 3);
    let list_height = request.options.len().min(usize::from(
        inner
            .height
            .saturating_sub(hint_height + u16::from(inner.height >= 2)),
    )) as u16;
    let [preview, choices, hint] = Layout::vertical([
        Constraint::Min(0),
        Constraint::Length(list_height),
        Constraint::Length(hint_height),
    ])
    .areas(inner);
    let width = usize::from(preview.width.max(1));
    let mut details = wrap(&request.title, width)
        .into_iter()
        .map(|range| {
            Line::from(&request.title[range])
                .fg(palette.foreground)
                .bold()
        })
        .collect::<Vec<_>>();
    details.extend(
        wrap(&request.detail, width)
            .into_iter()
            .map(|range| Line::from(&request.detail[range]).fg(palette.muted)),
    );
    pending.detail_scroll = pending
        .detail_scroll
        .min(details.len().saturating_sub(usize::from(preview.height)));
    let visible = details
        .into_iter()
        .skip(pending.detail_scroll)
        .take(usize::from(preview.height))
        .collect::<Vec<_>>();
    frame.render_widget(Paragraph::new(visible), preview);

    let options = request.options.iter().enumerate().map(|(index, option)| {
        let name = option.name.lines().next().unwrap_or_default();
        if index < 9 {
            Line::from(format!("{}. {name}", index + 1))
        } else {
            Line::from(format!("   {name}"))
        }
    });
    let list = List::new(options)
        .highlight_symbol("› ")
        .highlight_style(Style::default().fg(palette.accent).bg(palette.panel).bold());
    let mut state = ListState::default()
        .with_selected((!request.options.is_empty()).then_some(pending.selected));
    frame.render_stateful_widget(list, choices, &mut state);
    frame.render_widget(
        Paragraph::new("↑/↓ move · Enter choose · Esc cancel").fg(palette.muted),
        hint,
    );
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

fn question_hint(state: &crate::elicitation::FormState) -> &'static str {
    if state.current().is_none() {
        "Enter submit · Shift+Tab previous · Esc decline"
    } else if state.editing_custom {
        "Type your answer · Enter save · Esc decline"
    } else if state
        .current()
        .is_some_and(|field| matches!(field.kind, FieldKind::Choice { multiple: true, .. }))
    {
        "↑/↓ choose · Space toggle · Enter continue · Esc decline"
    } else if state.form.fields.len() > 1 {
        "↑/↓ choose · Tab/Shift+Tab switch · Enter next · Esc decline"
    } else {
        "↑/↓ choose · Enter send · Esc decline"
    }
}

fn elicitation_lines(
    pending: &super::PendingElicitation,
    limit: usize,
    width: u16,
    palette: &Palette,
) -> Vec<Line<'static>> {
    let state = &pending.state;
    let mut lines = Vec::new();
    let tabs = state.form.fields.len() > 1;
    let current = state.current();
    if !state.form.message.is_empty()
        && (tabs
            || current
                .is_none_or(|field| field.description.as_deref() != Some(&state.form.message)))
    {
        lines.push(Line::from(state.form.message.clone()).bold());
    }
    if tabs {
        let mut labels = state
            .form
            .fields
            .iter()
            .enumerate()
            .map(|(index, field)| format!("{}. {}", index + 1, field.title))
            .chain(std::iter::once("Confirm".into()))
            .collect::<Vec<_>>();
        let mut start = 0;
        let budget = usize::from(width.saturating_sub(8));
        while start < state.index
            && labels[start..=state.index]
                .iter()
                .map(|label| unicode_width::UnicodeWidthStr::width(label.as_str()) + 3)
                .sum::<usize>()
                > budget
        {
            start += 1;
        }
        let mut line = Line::default();
        for (index, label) in labels.drain(..).enumerate().skip(start) {
            if index != start {
                line.push_span(" · ".fg(palette.muted));
            }
            line.push_span(if index == state.index {
                label.fg(palette.accent).bold()
            } else {
                label.fg(palette.muted)
            });
        }
        lines.push(line);
    }
    let available = limit.saturating_sub(lines.len() + usize::from(state.error.is_some()));
    if let Some(field) = current {
        lines.push(Line::from(field.title.clone()).bold());
        if let Some(description) = &field.description {
            lines.push(Line::from(description.clone()));
        }
        match &field.kind {
            FieldKind::Choice { options, multiple } => {
                let answer = state.answer(field);
                let options = options.iter().map(|option| {
                    let selected = match answer {
                        Some(
                            agent_client_protocol::v1::schema::ElicitationContentValue::StringArray(
                                values,
                            ),
                        ) => values.contains(&option.value),
                        Some(
                            agent_client_protocol::v1::schema::ElicitationContentValue::String(
                                value,
                            ),
                        ) if field
                            .custom_key
                            .as_ref()
                            .is_none_or(|key| !state.answers.contains_key(key)) =>
                        {
                            *value == option.value
                        }
                        _ => false,
                    };
                    let label = match &option.detail {
                        Some(detail) => format!("{} · {detail}", option.label),
                        None => option.label.clone(),
                    };
                    (label, selected)
                });
                let custom = field.custom_key.as_ref().map(|key| {
                    let answer = state.answers.get(key);
                    let label = if state.editing_custom {
                        format!("Type your own answer › {}", state.draft)
                    } else if let Some(value) = answer {
                        format!("Type your own answer · {}", answer_text(value))
                    } else {
                        "Type your own answer".into()
                    };
                    (label, answer.is_some())
                });
                let slots = available
                    .saturating_sub(1 + usize::from(field.description.is_some()))
                    .max(1);
                lines.extend(
                    options
                        .chain(custom)
                        .enumerate()
                        .skip(state.choice.saturating_sub(slots - 1))
                        .take(slots)
                        .map(|(index, (label, selected))| {
                            let focused = state.choice == index;
                            let pointer = if focused { "›" } else { " " };
                            let mark = match (*multiple, selected) {
                                (true, true) => "☑",
                                (true, false) => "☐",
                                (false, true) => "●",
                                (false, false) => "○",
                            };
                            let line =
                                Line::from(format!("{pointer} {mark} {}. {label}", index + 1));
                            if focused {
                                line.fg(palette.accent).bold()
                            } else {
                                line
                            }
                        }),
                );
            }
            FieldKind::Boolean => {
                lines.extend(["Yes", "No"].into_iter().enumerate().map(|(index, label)| {
                    if state.choice == index {
                        Line::from(format!("› {label}")).fg(palette.accent).bold()
                    } else {
                        Line::from(format!("  {label}"))
                    }
                }));
            }
            FieldKind::Text(_) => {
                lines.push(Line::from(format!("› {}", state.draft)).fg(palette.accent))
            }
        }
    } else if tabs {
        lines.push(Line::from("Review your answers").bold());
        lines.extend(
            state
                .form
                .fields
                .iter()
                .take(available.saturating_sub(1))
                .enumerate()
                .map(|(index, field)| {
                    let answer = state
                        .answer(field)
                        .map(answer_text)
                        .unwrap_or_else(|| "(unanswered)".into());
                    Line::from(format!("{}. {} · {answer}", index + 1, field.title))
                }),
        );
    }
    if let Some(error) = &state.error {
        lines.push(Line::from(error.clone()).fg(palette.error));
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
