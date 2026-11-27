mod overlays;
mod panels;

use std::ops::Range;

use ratatui::Frame;
use ratatui::layout::{Constraint, Layout, Margin, Position, Rect};
use ratatui::style::{Style, Stylize};
use ratatui::text::{Line, Span, Text};
use ratatui::widgets::{Block, BorderType, Borders, Paragraph, Wrap};
use unicode_width::UnicodeWidthStr;

use super::{App, MCP_CONSENT_CHOICES, ScrollBar, is_local_command, slash_command};
use crate::theme::Palette;
use crate::transcript::{wrap, wrap_up_to};
#[cfg(not(target_family = "wasm"))]
pub(crate) use overlays::draw_choice_list;
use overlays::{draw_approval, draw_mcp_consent, elicitation_lines, question_hint};

const PROMPT_MARKER_WIDTH: u16 = 2;
const TRANSCRIPT_AND_STATUS_ROWS: u16 = 2;

impl App {
    pub fn draw(&mut self, frame: &mut Frame, animation_tick: u128) {
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
            animation_tick,
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
                // Pad labels to the widest one so descriptions share a column.
                let label_width = completion
                    .items
                    .iter()
                    .map(|suggestion| suggestion.insert.trim_end().width())
                    .max()
                    .unwrap_or_default();
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
                            let label = suggestion.insert.trim_end();
                            let padding = label_width.saturating_sub(label.width());
                            format!(
                                " {label}{}  {}",
                                " ".repeat(padding),
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
        let command_status =
            if prompt_active && self.draft.text().is_empty() && self.status != "Disconnected" {
                self.command_status.as_deref()
            } else {
                None
            };
        self.draw_status(
            frame,
            status.inner(gutter),
            status_hint.fg(hint_color),
            command_status,
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
}
