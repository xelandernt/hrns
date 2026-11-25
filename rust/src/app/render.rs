use std::ops::Range;

use agent_client_protocol::v1::schema::{
    PlanEntryPriority, PlanEntryStatus, SessionConfigKind, SessionConfigOptionCategory, ToolKind,
};
use ratatui::Frame;
use ratatui::layout::{Constraint, Layout, Margin, Position, Rect};
use ratatui::style::{Style, Stylize};
use ratatui::text::{Line, Span, Text};
use ratatui::widgets::{Block, BorderType, Borders, Clear, List, ListState, Paragraph, Tabs, Wrap};

use super::picker::{Picker, Row, Source, context_tier};
use super::{
    App, MCP_CONSENT_CHOICES, PendingApproval, PendingMcpConsent, ScrollBar, is_local_command,
    slash_command,
};
use crate::elicitation::FieldKind;
use crate::options;
use crate::theme::Palette;
use crate::transcript::{wrap, wrap_up_to};

const PROMPT_MARKER_WIDTH: u16 = 2;
const TRANSCRIPT_AND_STATUS_ROWS: u16 = 2;

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
        if let Some(picker) = &self.picker
            && picker.is_agents()
        {
            self.transcript_area = Rect::default();
            self.scroll_bar = None;
            self.scroll_drag = None;
            self.follow_area = None;
            self.follow_pressed = false;
            self.draw_agents(frame, area, picker, &palette);
            return;
        }
        let mcp_pending = self.mcp_consent.front();
        let approval_active =
            !self.approvals.is_empty() && mcp_pending.is_none() && self.elicitations.is_empty();
        let consent_active = mcp_pending.is_some();
        let gutter = Margin::new(if area.width >= 80 { 2 } else { 1 }, 0);
        let prompt_height = if approval_active || consent_active {
            0
        } else if area.height >= 12 {
            6
        } else {
            area.height / 2
        };
        let mcp_details =
            mcp_pending.map(|pending| format!("{} · {}", pending.transport, pending.target));
        let mcp_height = mcp_details.as_ref().map_or(0, |details| {
            let width = usize::from(area.inner(gutter).width.saturating_sub(4).max(1));
            let available = usize::from(area.height.saturating_sub(TRANSCRIPT_AND_STATUS_ROWS));
            (2 + MCP_CONSENT_CHOICES.len() + wrap(details, width).len()).min(available) as u16
        });
        let approval_height =
            self.approvals
                .front()
                .filter(|_| approval_active)
                .map_or(0, |pending| {
                    let width = usize::from(area.inner(gutter).width.saturating_sub(4).max(1));
                    let request = &pending.request;
                    let details =
                        wrap(&request.title, width).len() + wrap(&request.detail, width).len();
                    (3 + details.min(8) + request.options.len().min(9)).min(usize::from(
                        area.height.saturating_sub(TRANSCRIPT_AND_STATUS_ROWS),
                    )) as u16
                });
        let follow_height = u16::from(!self.transcript.following());
        let question_limit = area
            .height
            .saturating_sub(prompt_height + TRANSCRIPT_AND_STATUS_ROWS + follow_height)
            .min(18);
        let question = self
            .elicitations
            .front()
            .filter(|_| !consent_active)
            .map(|pending| {
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
            .filter(|_| !approval_active && !consent_active)
            .map_or(0, |completion| {
                let available = area.height.saturating_sub(
                    mcp_height
                        + elicitation_height
                        + follow_height
                        + prompt_height
                        + TRANSCRIPT_AND_STATUS_ROWS,
                );
                let minimum = usize::from(completion.file.is_some());
                (completion.items.len().clamp(minimum, 6) as u16).min(available)
            });
        let reserved_height = [
            mcp_height,
            approval_height,
            elicitation_height,
            completion_height,
            follow_height,
            prompt_height,
            TRANSCRIPT_AND_STATUS_ROWS,
        ]
        .into_iter()
        .fold(0, u16::saturating_add);
        let queue_height = area
            .height
            .saturating_sub(reserved_height)
            .min((area.height / 4).clamp(2, 7));
        let queue_lines =
            self.queued_prompt_lines(area.inner(gutter).width, queue_height, &palette);
        let [
            transcript,
            mcp,
            approval,
            elicitation,
            queue,
            suggestions,
            follow,
            composer,
            status,
        ] = Layout::vertical([
            Constraint::Min(1),
            Constraint::Length(mcp_height),
            Constraint::Length(approval_height),
            Constraint::Length(elicitation_height),
            Constraint::Length(queue_lines.len() as u16),
            Constraint::Length(completion_height),
            Constraint::Length(follow_height),
            Constraint::Length(prompt_height),
            Constraint::Length(1),
        ])
        .areas(area);
        if !queue_lines.is_empty() {
            frame.render_widget(Text::from(queue_lines), queue.inner(gutter));
        }

        let composer = composer.inner(gutter);
        let framed_composer =
            composer.width >= 6 && (composer.height >= 3 || (approval_active && area.height >= 6));

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
        if sidebar.width > 0 {
            // Pull the panel's right edge in to match the composer's framed edge.
            sidebar.width = sidebar.width.saturating_sub(gutter.horizontal);
        }
        let mut transcript = transcript.inner(gutter);
        if composer.width > PROMPT_MARKER_WIDTH {
            let inset = (PROMPT_MARKER_WIDTH + u16::from(framed_composer)).min(transcript.width);
            transcript.x += inset;
            transcript.width -= inset;
        }
        if transcript.width >= 4 {
            // Reserve the column the composer's right border sits in for the scrollbar.
            transcript.width -= 1;
        }
        self.transcript_area = transcript;
        if sidebar.width > 0 {
            self.transcript_area.width = self
                .transcript_area
                .width
                .min(sidebar.x.saturating_sub(transcript.x));
        }
        let visible = self.transcript.visible(
            transcript.width,
            transcript.height,
            &palette,
            is_local_command,
        );
        frame.render_widget(
            Text::from(visible.lines).style(
                Style::default()
                    .fg(palette.foreground)
                    .bg(palette.background),
            ),
            transcript,
        );
        if transcript.x > area.x {
            for (row, color) in visible.notice_borders {
                frame.buffer_mut().set_string(
                    transcript.x - 1,
                    transcript.y + row,
                    "│",
                    Style::default().fg(color),
                );
            }
        }
        self.scroll_bar = None;
        let total = self.transcript.total();
        let viewport = self.transcript.viewport_height();
        if total > viewport
            && transcript.width > 0
            && transcript.height > 0
            && (sidebar.width == 0 || transcript.right() < sidebar.x)
        {
            let track = transcript.height;
            let thumb = (viewport * usize::from(track))
                .div_ceil(total)
                .clamp(1, usize::from(track)) as u16;
            let max_top = total - viewport;
            let travel = track.saturating_sub(thumb);
            let top = self.transcript.top();
            let start = if travel == 0 {
                0
            } else {
                u16::try_from((top * usize::from(travel) + max_top / 2) / max_top)
                    .unwrap_or(travel)
                    .min(travel)
            };
            let thumb_color = if self.bar_hover || self.scroll_drag.is_some() {
                palette.foreground
            } else {
                palette.muted
            };
            let lines = (0..track)
                .map(|row| {
                    let on_thumb = row >= start && row < start + thumb;
                    let (symbol, color) = if on_thumb {
                        ("█", thumb_color)
                    } else {
                        ("│", palette.border)
                    };
                    Line::from(symbol).fg(color)
                })
                .collect::<Vec<_>>();
            let bar = Rect::new(transcript.right(), transcript.y, 1, track);
            frame.render_widget(Paragraph::new(lines), bar);
            // The gutter beside the bar joins its hit area, so the thumb is easier to catch.
            let mut hit_width = gutter.horizontal + 1;
            if sidebar.width > 0 {
                hit_width = hit_width.min(sidebar.x.saturating_sub(bar.x));
            }
            let hit = Rect::new(bar.x, bar.y, hit_width.max(1), track);
            self.scroll_bar = Some(ScrollBar {
                area: hit,
                thumb,
                max_top,
            });
        }

        if let (Some(pending), Some(details)) = (mcp_pending, mcp_details.as_deref()) {
            draw_mcp_consent(frame, mcp.inner(gutter), pending, details, &palette);
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
            let rows = if let Some(status) = completion.file.as_ref().and_then(|file| file.status) {
                vec![Line::from(format!("  {status}")).fg(palette.muted)]
            } else {
                let visible = usize::from(suggestions.height);
                completion
                    .items
                    .iter()
                    .enumerate()
                    .skip(
                        completion
                            .selected
                            .saturating_sub(visible.saturating_sub(1)),
                    )
                    .take(visible)
                    .map(|(index, suggestion)| {
                        let selected = index == completion.selected;
                        let row = if completion.file.is_some() {
                            let marker = if selected { "›" } else { " " };
                            format!(" {marker} {}", suggestion.insert)
                        } else {
                            format!(
                                " {}  {}",
                                suggestion.insert.trim_end(),
                                suggestion.description
                            )
                        };
                        if selected {
                            let color = if completion.file.is_some() {
                                palette.file_reference
                            } else if suggestion.insert.starts_with('$') {
                                palette.skill_reference
                            } else {
                                palette.accent
                            };
                            Line::from(row).fg(color).bold()
                        } else {
                            Line::from(row).fg(palette.muted)
                        }
                    })
                    .collect()
            };
            frame.render_widget(Paragraph::new(rows), suggestions.inner(gutter));
        }

        self.follow_area = None;
        if !self.transcript.following() && follow.height > 0 {
            self.draw_follow_control(frame, follow.inner(gutter), &palette);
        }

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
        if framed_composer {
            block = block
                .borders(Borders::ALL)
                .border_type(BorderType::Rounded)
                .border_style(Style::default().fg(border));
        }
        let input = block.inner(composer);
        frame.render_widget(block, composer);
        if input.width > PROMPT_MARKER_WIDTH && input.height > 0 {
            let [prompt, input] =
                Layout::horizontal([Constraint::Length(PROMPT_MARKER_WIDTH), Constraint::Min(1)])
                    .areas(input);
            frame.render_widget(Paragraph::new("›").fg(border).bold(), prompt);
            let (rows, (row, column)) = self.draft.display(input.width);
            let scroll = row.saturating_sub(input.height - 1);
            let text = if self.draft.text().is_empty() {
                Text::from("Send a message…").fg(palette.muted)
            } else {
                self.composer_text(rows, &palette)
            };
            frame.render_widget(Paragraph::new(text).scroll((scroll, 0)), input);
            if prompt_active {
                frame.set_cursor_position(Position::new(input.x + column, input.y + row - scroll));
            }
        }
        let status_hint = if self.mcp_consent.front().is_some() {
            "↑/↓ move · Enter choose · Esc deny"
        } else if let Some(pending) = self.elicitations.front() {
            question_hint(&pending.state)
        } else if self.approvals.front().is_some() {
            "PgUp/PgDn approval details"
        } else if prompt_active
            && let Some(completion) = self.completion.as_ref().filter(|item| item.file.is_some())
        {
            if completion.items.is_empty() {
                "Esc close file search"
            } else {
                "↑/↓ select · Tab/Enter insert path · Esc close"
            }
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
        } else if let Some(message) = self.prompt_setup_message() {
            message
        } else if slash_command(self.draft.text()) == Some("/queue") {
            "Enter queue message"
        } else {
            ""
        };
        let hint_color = if prompt_active && self.transcript.output_pending() {
            palette.accent
        } else {
            palette.muted
        };
        self.draw_status(
            frame,
            status.inner(gutter),
            status_hint.fg(hint_color),
            &palette,
        );
        if sidebar.width > 0 {
            if self.todo_open {
                self.draw_todos(frame, sidebar, &palette);
            } else {
                self.draw_sidebar(frame, sidebar, &palette);
            }
        }
        let awaiting_input = !self.approvals.is_empty()
            || !self.elicitations.is_empty()
            || !self.mcp_consent.is_empty();
        if !awaiting_input && let Some(popup) = &self.providers {
            popup.draw(frame, area, self.selected_provider.as_ref(), &palette);
        }
        if !awaiting_input && let Some(picker) = &self.picker {
            self.draw_picker(frame, area, picker, &palette);
        }
    }

    fn composer_text(&self, rows: Vec<Range<usize>>, palette: &Palette) -> Text<'_> {
        let draft = self.draft.text();
        let mut highlights = Vec::new();
        if let Some(command) = slash_command(draft).filter(|command| self.is_command(command)) {
            highlights.push((0..command.len(), palette.accent));
        }
        #[cfg(not(target_family = "wasm"))]
        highlights.extend(
            self.file_search
                .highlights
                .iter()
                .cloned()
                .map(|range| (range, palette.file_reference)),
        );
        highlights.extend(
            crate::skills::mention_ranges(draft, &self.skills)
                .map(|range| (range, palette.skill_reference)),
        );
        highlights.sort_unstable_by_key(|(range, _)| range.start);

        let lines: Vec<_> = rows
            .into_iter()
            .map(|row| {
                let mut line = Line::default();
                let mut offset = row.start;
                let first = highlights.partition_point(|(range, _)| range.end <= offset);
                for (range, color) in highlights[first..]
                    .iter()
                    .take_while(|(range, _)| range.start < row.end)
                {
                    let start = range.start.max(row.start);
                    let end = range.end.min(row.end);
                    if start > offset {
                        line.push_span(Span::raw(&draft[offset..start]));
                    }
                    line.push_span(Span::raw(&draft[start..end]).fg(*color));
                    offset = end;
                }
                if offset < row.end {
                    line.push_span(Span::raw(&draft[offset..row.end]));
                }
                line
            })
            .collect();
        Text::from(lines)
    }

    fn queued_prompt_lines(&self, width: u16, height: u16, palette: &Palette) -> Vec<Line<'_>> {
        if self.queue.is_empty() || width < 8 || height < 2 {
            return Vec::new();
        }
        let height = usize::from(height);
        let mut heading = Line::from("Queued".fg(palette.accent).bold());
        if self.draft.text().is_empty() {
            heading.spans.push(" · Shift+← edit".fg(palette.muted));
        }
        let mut lines = vec![heading];
        for (index, prompt) in self.queue.iter().enumerate() {
            let remaining = height - lines.len();
            let more = self.queue.len() - index - 1;
            if remaining == 1 && more > 0 {
                lines.push(Line::from(format!("+{} more", more + 1)).fg(palette.muted));
                break;
            }
            let limit = (remaining - usize::from(more > 0)).min(3);
            // Leave room for the bullet and for an ellipsis on a truncated last row.
            let rows = wrap_up_to(&prompt.text, usize::from(width) - 3, limit + 1);
            let truncated = rows.len() > limit;
            for (row, range) in rows.into_iter().take(limit).enumerate() {
                let marker = if row == 0 { "• " } else { "  " };
                let mut line = Line::from(vec![
                    marker.fg(palette.accent),
                    Span::raw(&prompt.text[range]),
                ])
                .fg(palette.muted);
                if truncated && row + 1 == limit {
                    line.spans.push(Span::raw("…"));
                }
                lines.push(line);
            }
        }
        lines
    }

    /// A centred, clickable label that returns the transcript to the tail, hidden while it follows.
    fn draw_follow_control(&mut self, frame: &mut Frame, area: Rect, palette: &Palette) {
        let busy = self.turns > 0 || self.remote_busy();
        let pending = self.transcript.output_pending();
        let labels: &[&str] = match (pending, busy) {
            (true, false) => &[
                "New activity · ↓ Back to bottom · esc",
                "New activity · ↓ Back to bottom",
                "↓ Back to bottom · esc",
                "↓ Back to bottom",
                "↓ Bottom",
                "↓",
            ],
            (true, true) => &[
                "New activity · ↓ Back to bottom",
                "New activity · ↓ Bottom",
                "↓ Back to bottom",
                "↓ Bottom",
                "↓",
            ],
            (false, false) => &[
                "↓ Back to bottom · esc",
                "↓ Back to bottom",
                "↓ Bottom",
                "↓",
            ],
            (false, true) => &["↓ Back to bottom", "↓ Bottom", "↓"],
        };
        let Some(label) = labels
            .iter()
            .copied()
            .find(|label| label.chars().count() <= usize::from(area.width))
        else {
            return;
        };
        let width = u16::try_from(label.chars().count()).unwrap_or(area.width);
        let target = Rect::new(area.x + (area.width - width) / 2, area.y, width, 1);
        let style = if self.follow_hover || self.follow_pressed {
            Style::default().fg(palette.accent).bold().reversed()
        } else {
            Style::default().fg(palette.muted)
        };
        frame.render_widget(Paragraph::new(label).style(style), target);
        let left = target.x.saturating_sub(1).max(area.x);
        let right = target.right().saturating_add(1).min(area.right());
        self.follow_area = Some(Rect::new(left, target.y, right.saturating_sub(left), 1));
    }

    fn draw_picker(&self, frame: &mut Frame, area: Rect, picker: &Picker, palette: &Palette) {
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

    fn draw_status(&self, frame: &mut Frame, area: Rect, hint: Span<'_>, palette: &Palette) {
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
        let model_width = match model.width() {
            size if size <= width => size,
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
        if hint.width() > 0 {
            if left.width() + 3 + hint.width() <= left_budget {
                left.push_span(" · ");
                left.push_span(hint);
            } else if left_budget == width {
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

fn draw_mcp_consent(
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
