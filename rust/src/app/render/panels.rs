use agent_client_protocol::v1::schema::{
    PlanEntryPriority, PlanEntryStatus, SessionConfigKind, SessionConfigOptionCategory,
};
use ratatui::Frame;
use ratatui::layout::{Constraint, Layout, Margin, Rect};
use ratatui::style::{Style, Stylize};
use ratatui::text::{Line, Span};
use ratatui::widgets::{Block, BorderType, Borders, Clear, List, ListState, Paragraph, Tabs, Wrap};
use unicode_width::UnicodeWidthStr;

use super::super::picker::{Picker, Row, Source, context_tier};
use super::super::{App, DETAILS_CHOICES};
use super::overlays::draw_choice_list;
use crate::options;
use crate::theme::Palette;
use crate::transcript::wrap_up_to;

impl App {
    pub(super) fn draw_picker(
        &self,
        frame: &mut Frame,
        area: Rect,
        picker: &Picker,
        palette: &Palette,
    ) {
        let details = matches!(picker.source, Source::Details);
        let width = area
            .width
            .saturating_sub(2)
            .min(if details { 48 } else { 88 });
        let height = area
            .height
            .saturating_sub(2)
            .min(if details { 7 } else { 26 });
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
        if details {
            let [choices, keys] =
                Layout::vertical([Constraint::Min(1), Constraint::Length(1)]).areas(inner);
            draw_choice_list(
                frame,
                choices,
                DETAILS_CHOICES.map(|(_, label)| Line::from(label)),
                Some(picker.selected),
                palette,
            );
            frame.render_widget(
                Paragraph::new(picker.hint(false, false)).fg(palette.muted),
                keys,
            );
            return;
        }
        if matches!(picker.source, Source::Mcp(_)) {
            self.draw_mcp_picker(frame, inner, picker, palette);
            return;
        }
        let rows = self.picker_rows(picker);
        let tabs = self.model_provider_tabs(picker);
        let show_tabs = tabs.len() > 1;
        let skills = matches!(picker.source, Source::Skills);
        let detail_height = if skills && !rows.is_empty() {
            (inner.height / 2)
                .clamp(4, 10)
                .min(inner.height.saturating_sub(3))
        } else {
            1
        };
        let [provider_tabs, search, list, status, keys] = Layout::vertical([
            Constraint::Length(u16::from(show_tabs)),
            Constraint::Length(if skills {
                u16::from(inner.height >= 4)
            } else {
                2
            }),
            Constraint::Min(1),
            Constraint::Length(detail_height),
            Constraint::Length(1),
        ])
        .areas(inner);
        if show_tabs {
            let active = picker.provider_tab.min(tabs.len() - 1);
            let labels: Vec<_> = tabs
                .iter()
                .map(|name| Line::from(name.unwrap_or("Other")))
                .collect();
            let mut remaining = usize::from(provider_tabs.width);
            let first = labels[..=active]
                .iter()
                .rev()
                .take_while(|label| {
                    let width = label.width() + 3;
                    if width > remaining {
                        return false;
                    }
                    remaining -= width;
                    true
                })
                .count()
                .max(1);
            let start = active + 1 - first;
            frame.render_widget(
                Tabs::new(labels.into_iter().skip(start))
                    .select(active - start)
                    .style(Style::default().fg(palette.muted))
                    .highlight_style(Style::default().fg(palette.accent).bg(palette.panel).bold()),
                provider_tabs,
            );
        }
        let search_text = if picker.query.is_empty() {
            Line::from("  Search…").fg(palette.muted)
        } else {
            Line::from(format!("  Search  {}", picker.query)).fg(palette.accent)
        };
        frame.render_widget(Paragraph::new(search_text), search);
        frame.render_widget(
            Paragraph::new(
                picker.hint(
                    show_tabs,
                    rows.get(picker.selected)
                        .is_some_and(|row| row.context_tiers.len() > 1),
                ),
            )
            .fg(palette.muted),
            keys,
        );

        if skills && let Some(row) = rows.get(picker.selected) {
            let entries = List::new(rows.iter().map(|row| row.label.as_str()))
                .highlight_symbol("  › ")
                .highlight_style(
                    Style::default()
                        .fg(palette.skill_reference)
                        .bg(palette.panel)
                        .bold(),
                );
            let mut state = ListState::default().with_selected(Some(picker.selected));
            frame.render_stateful_widget(entries, list, &mut state);
            self.draw_skill_details(frame, status, row, palette);
            return;
        }

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
                let mut title = Line::from(format!(" {marker}  {}", row.label));
                for (index, tier) in row.context_tiers.iter().enumerate() {
                    title.push_span(if index == 0 { "  " } else { " | " });
                    let label = Span::raw(tier.label.as_str());
                    title.push_span(if index == row.selected_tier {
                        label.fg(palette.background).bg(palette.accent).bold()
                    } else {
                        label.fg(palette.muted)
                    });
                }
                title.push_span(active);
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
    }

    fn draw_skill_details(&self, frame: &mut Frame, area: Rect, row: &Row, palette: &Palette) {
        let divider = Block::new()
            .borders(if area.height >= 3 {
                Borders::TOP
            } else {
                Borders::NONE
            })
            .border_style(Style::default().fg(palette.border));
        let detail = divider.inner(area).inner(Margin::new(2, 0));
        frame.render_widget(divider, area);
        if detail.is_empty() {
            return;
        }

        let source = if self.remote {
            "Remote"
        } else {
            self.skills
                .iter()
                .find(|skill| skill.id == row.value)
                .and_then(crate::skills::Skill::directory)
                .unwrap_or("Unavailable")
        };
        let source = format!("Source  {source}");
        let limit = usize::from(detail.height).saturating_sub(1).max(1);
        let lines = wrap_up_to(&source, usize::from(detail.width), limit);
        let source_height = u16::try_from(lines.len()).unwrap_or(detail.height);
        let [origin, description] =
            Layout::vertical([Constraint::Length(source_height), Constraint::Min(0)]).areas(detail);
        let lines: Vec<_> = lines
            .into_iter()
            .map(|range| Line::from(&source[range]).fg(palette.muted))
            .collect();
        frame.render_widget(Paragraph::new(lines), origin);
        frame.render_widget(
            Paragraph::new(row.detail.trim()).wrap(Wrap { trim: true }),
            description,
        );
    }

    pub(super) fn draw_status(
        &self,
        frame: &mut Frame,
        area: Rect,
        hint: Span<'_>,
        command_status: Option<&str>,
        palette: &Palette,
    ) {
        let width = usize::from(area.width);
        let model_ready = !self.model_change_pending && !self.provider_model_missing();
        let model = if model_ready {
            self.options.iter().find_map(|option| match &option.kind {
                SessionConfigKind::Select(select)
                    if options::matches_category(option, &SessionConfigOptionCategory::Model) =>
                {
                    let current = select.current_value.0.as_ref();
                    Some(
                        options::selected_model(&self.options)
                            .filter(|choice| context_tier(choice).is_some())
                            .map_or(current, |choice| choice.name.as_str()),
                    )
                }
                _ => None,
            })
        } else {
            self.selected_provider
                .as_ref()
                .map(|provider| provider.id.as_str())
        };
        let model = model.map_or_else(Line::default, |model| {
            Line::from(model.fg(palette.accent).bold())
        });
        let model_budget = width.saturating_sub(command_status.map_or(0, |text| text.width() + 2));
        let model_width = match model.width() {
            size if size <= model_budget => size,
            _ => 0,
        };
        let left_budget = if model_width > 0 {
            width.saturating_sub(model_width + 2)
        } else {
            width
        };
        let status_color = match self.status {
            "Connected" => palette.success,
            "Disconnected" => palette.error,
            "Waiting" | "Cancelling" => palette.warning,
            _ => palette.accent,
        };
        let mut left = Line::from(vec!["● ".fg(status_color), self.status.fg(status_color)]);
        if let Some(text) = command_status
            && !append_status_field(&mut left, Line::from(text.fg(palette.accent)), left_budget)
        {
            left = Line::from(text.fg(palette.accent));
        }
        if hint.width() > 0 {
            if left.width() + 3 + hint.width() <= left_budget {
                left.push_span(" · ");
                left.push_span(hint);
            } else if left_budget == width && command_status.is_none() {
                left = Line::from(hint);
            }
        }
        if self.attachment_reads > 0 {
            append_status_field(&mut left, Line::from("Loading attachment"), left_budget);
        } else if !self.attachments.is_empty() {
            append_status_field(
                &mut left,
                Line::from(format!("{} attached", self.attachments.len())),
                left_budget,
            );
        }
        if !self.queue.is_empty() {
            append_status_field(
                &mut left,
                Line::from(format!("{} queued", self.queue.len())),
                left_budget,
            );
        }

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
        let mut right = Line::default();
        let budget = width.saturating_sub(left.width() + 2).max(model_width);
        append_status_field(&mut right, model, budget);
        if model_ready {
            for field in [
                self.token_usage(),
                Line::from(self.usage_cost.as_deref().unwrap_or_default()),
                Line::from(vec!["Effort ".into(), effort.fg(palette.accent)]),
            ] {
                append_status_field(&mut right, field, budget);
            }
        }
        append_status_field(
            &mut right,
            Line::from(vec![
                "Approvals ".into(),
                self.approval_mode.label().fg(palette.accent),
            ]),
            budget,
        );

        let right_width = right.width();
        let left_width = width.saturating_sub(right_width);
        let gap = if right_width == 0 {
            0
        } else {
            left_width.saturating_sub(left.width()).clamp(1, 2)
        };
        let left_width = left_width.saturating_sub(gap);
        if let Some(title) = self.session_title.as_deref() {
            append_status_field(&mut left, Line::from(title), left_width);
        } else if let Some(workspace) = &self.workspace {
            let fits = append_status_field(
                &mut left,
                Line::from(workspace.to_string_lossy()),
                left_width,
            );
            if !fits && let Some(name) = workspace.file_name() {
                append_status_field(&mut left, Line::from(name.to_string_lossy()), left_width);
            }
        }
        append_status_field(&mut left, Line::from(self.destination.as_str()), left_width);

        let context = Rect::new(area.x, area.y, left_width as u16, area.height);
        let values = Rect::new(
            area.right() - right_width as u16,
            area.y,
            right_width as u16,
            area.height,
        );
        let style = Style::default().fg(palette.muted);
        frame.render_widget(Paragraph::new(left).style(style), context);
        frame.render_widget(Paragraph::new(right).style(style), values);
    }

    fn token_usage(&self) -> Line<'_> {
        self.usage
            .map(|(used, size)| format!("ctx {used}/{size}"))
            .or_else(|| {
                self.last_usage_tokens
                    .map(|tokens| format!("{tokens} tokens"))
            })
            .map_or_else(Line::default, Line::from)
    }

    pub(super) fn draw_todos(&mut self, frame: &mut Frame, area: Rect, palette: &Palette) {
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

    pub(super) fn draw_sidebar(
        &self,
        frame: &mut Frame,
        area: ratatui::layout::Rect,
        palette: &Palette,
    ) {
        let heading = |text: &'static str| Line::from(text).fg(palette.accent).bold();
        let mut lines = vec![
            heading("Connection"),
            Line::from(self.destination.clone()).fg(palette.muted),
            Line::from(format!("{} · io {}", self.status, self.client_io.label()))
                .fg(palette.muted),
        ];
        if let Some(provider) = &self.selected_provider {
            lines.push(
                Line::from(vec![
                    "Provider ".into(),
                    provider.id.as_str().fg(palette.accent),
                ])
                .fg(palette.muted),
            );
        }
        if let Some(workspace) = &self.workspace {
            lines.push(Line::from(workspace.to_string_lossy()).fg(palette.muted));
        }
        if let Some(session) = &self.session {
            lines.push(Line::from(session.clone()).fg(palette.muted));
        }
        if let Some(title) = &self.session_title {
            lines.push(Line::from(title.clone()).fg(palette.foreground));
        }
        if !self.model_change_pending
            && !self.provider_model_missing()
            && let Some(model) = self.options.iter().find_map(|option| match &option.kind {
                SessionConfigKind::Select(select)
                    if options::matches_category(option, &SessionConfigOptionCategory::Model) =>
                {
                    Some(select.current_value.clone())
                }
                _ => None,
            })
        {
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
        if let Some(tokens) = self.reasoning_tokens {
            lines.push(Line::from(format!("{tokens} thinking tokens")).fg(palette.muted));
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
                let detail = item.startup_failure.as_deref().unwrap_or(&item.detail);
                if !detail.is_empty() {
                    lines.push(Line::from(format!("  {detail}")).fg(palette.muted));
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

fn append_status_field<'a>(line: &mut Line<'a>, field: Line<'a>, limit: usize) -> bool {
    let width = field.width();
    let separator_width = if line.spans.is_empty() { 0 } else { 3 };
    if width == 0 || line.width() + separator_width + width > limit {
        return false;
    }
    if separator_width > 0 {
        line.push_span(" · ");
    }
    line.spans.extend(field.spans);
    true
}
