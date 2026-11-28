use agent_client_protocol::v1::schema::ToolKind;
use ratatui::Frame;
use ratatui::layout::{Constraint, Layout, Rect};
use ratatui::style::{Style, Stylize};
use ratatui::text::Line;
use ratatui::widgets::{Block, BorderType, List, ListState, Paragraph};

use super::super::{MCP_CONSENT_CHOICES, PendingApproval, PendingElicitation, PendingMcpConsent};
use crate::elicitation::FieldKind;
use crate::theme::Palette;
use crate::transcript::wrap;

pub(crate) fn draw_choice_list<'a>(
    frame: &mut Frame,
    area: Rect,
    options: impl IntoIterator<Item = Line<'a>>,
    selected: Option<usize>,
    palette: &Palette,
) {
    let list = List::new(options)
        .highlight_symbol("› ")
        .highlight_style(Style::default().fg(palette.accent).bg(palette.panel).bold());
    let mut state = ListState::default().with_selected(selected);
    frame.render_stateful_widget(list, area, &mut state);
}

pub(super) fn draw_mcp_consent(
    frame: &mut Frame,
    area: Rect,
    pending: &PendingMcpConsent,
    details: &str,
    palette: &Palette,
) {
    let block = Block::bordered()
        .border_type(BorderType::Rounded)
        .border_style(Style::default().fg(palette.warning))
        .style(Style::default().bg(palette.surface))
        .title(format!(" MCP server · {} ", pending.name))
        .title_style(Style::default().fg(palette.warning).bold())
        .padding(ratatui::widgets::Padding::horizontal(1));
    let inner = block.inner(area);
    frame.render_widget(block, area);
    let choice_height = inner.height.min(MCP_CONSENT_CHOICES.len() as u16);
    let [preview, choices] =
        Layout::vertical([Constraint::Min(0), Constraint::Length(choice_height)]).areas(inner);
    let lines = wrap(details, usize::from(preview.width.max(1)))
        .into_iter()
        .take(usize::from(preview.height))
        .map(|range| Line::from(&details[range]).fg(palette.muted))
        .collect::<Vec<_>>();
    frame.render_widget(Paragraph::new(lines), preview);

    let options = MCP_CONSENT_CHOICES
        .iter()
        .enumerate()
        .map(|(index, (label, _))| Line::from(format!("{}. {label}", index + 1)));
    draw_choice_list(frame, choices, options, Some(pending.selected), palette);
}

pub(super) fn draw_approval(
    frame: &mut Frame,
    area: Rect,
    pending: &mut PendingApproval,
    palette: &Palette,
) {
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
    let selected = (!request.options.is_empty()).then_some(pending.selected);
    draw_choice_list(frame, choices, options, selected, palette);
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

pub(super) fn question_hint(state: &crate::elicitation::FormState) -> &'static str {
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

pub(super) fn elicitation_lines(
    pending: &PendingElicitation,
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
