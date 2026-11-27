use std::collections::BTreeMap;
use std::ops::Range;

use agent_client_protocol::v1::schema::ToolCallStatus;
use ratatui::style::{Color, Style, Stylize};
use ratatui::text::{Line, Span};

use crate::theme::Palette;

#[derive(PartialEq)]
pub enum Role {
    User,
    Agent,
    Thought,
    Tool,
    Command(String),
    Note,
    Warning,
    Error,
}

impl Role {
    pub(super) fn is_action(&self) -> bool {
        matches!(self, Self::Thought | Self::Tool)
    }

    pub(super) fn notice_color(&self, palette: &Palette) -> Option<Color> {
        match self {
            Self::Note => Some(palette.system),
            Self::Warning => Some(palette.warning),
            Self::Error => Some(palette.error),
            _ => None,
        }
    }

    pub(super) fn indent(&self, width: usize, nested: bool) -> &'static str {
        if width < 10 || !self.is_action() {
            ""
        } else if nested {
            "   "
        } else {
            "  "
        }
    }

    pub(super) fn label(&self, palette: &Palette, expanded: bool) -> Option<Line<'_>> {
        let (name, color) = match self {
            Self::User => ("User", palette.accent),
            Self::Agent => ("Agent", palette.muted),
            Self::Note | Self::Warning | Self::Error => ("System", self.notice_color(palette)?),
            Self::Thought => (
                if expanded {
                    "▾ Reasoning"
                } else {
                    "▸ Reasoning"
                },
                palette.muted,
            ),
            Self::Command(name) => {
                return Some(
                    Line::from(vec![
                        "▌ ".fg(palette.accent),
                        name.as_str().fg(palette.accent).bold(),
                    ])
                    .bg(palette.panel),
                );
            }
            Self::Tool => return None,
        };
        Some(Line::styled(name, Style::default().fg(color).bold()))
    }
}

#[derive(Clone, Copy, PartialEq, Eq)]
pub enum ManualApproval {
    Pending,
    Granted,
    Rejected,
}

pub(super) struct Entry {
    pub(super) role: Role,
    pub(super) text: String,
    pub(super) rows: Vec<Range<usize>>,
    pub(super) code_rows: Vec<bool>,
    pub(super) table_rows: BTreeMap<usize, Line<'static>>,
    pub(super) detail: Option<String>,
    pub(super) detail_rows: Vec<Range<usize>>,
    pub(super) preview_start: Option<usize>,
    pub(super) shell: bool,
    pub(super) id: Option<String>,
    pub(super) expanded: bool,
    pub(super) tool_status: Option<ToolCallStatus>,
    pub(super) approval: Option<ManualApproval>,
    pub(super) action_heading: bool,
    pub(super) action_visible: bool,
    pub(super) action_preview: bool,
    pub(super) end: usize,
}

impl Entry {
    pub(super) fn preview_prefix_width(&self, width: usize) -> usize {
        if self.action_preview && width >= 10 {
            2
        } else {
            0
        }
    }

    pub(super) fn has_more_output(&self) -> bool {
        !self.expanded
            && self.detail_rows.last().is_some_and(|row| {
                self.detail
                    .as_ref()
                    .is_some_and(|detail| row.end < detail.len())
            })
    }

    pub(super) fn has_detail(&self) -> bool {
        self.approval.is_some()
            || self
                .detail
                .as_ref()
                .is_some_and(|detail| !detail.is_empty())
    }

    pub(super) fn heading_rows(&self) -> usize {
        usize::from(self.action_heading)
            + usize::from(self.action_visible && self.role != Role::Tool)
    }

    pub(super) fn approval_rows(&self) -> usize {
        usize::from(self.expanded && self.approval.is_some())
    }

    pub(super) fn line(
        &self,
        mut row: usize,
        palette: &Palette,
        is_command: fn(&str) -> bool,
    ) -> Line<'_> {
        if let Some(heading) = self.role.label(palette, self.expanded) {
            if row == 0 {
                return heading;
            }
            row -= 1;
        }
        if let Some(line) = self.table_rows.get(&row) {
            return Line::from(
                line.spans
                    .iter()
                    .map(|span| Span::styled(span.content.as_ref(), span.style))
                    .collect::<Vec<_>>(),
            );
        }
        if let Some(range) = self.rows.get(row) {
            let text = &self.text[range.clone()];
            if let Some(color) = self.role.notice_color(palette) {
                return notice_line(&self.text, range.clone(), color, is_command);
            }
            if let Some(status) = self.tool_status {
                let (marker, color) = match status {
                    _ if self.approval == Some(ManualApproval::Rejected) => ("–", palette.muted),
                    _ if self.approval == Some(ManualApproval::Pending) => ("○", palette.muted),
                    ToolCallStatus::Pending => ("○", palette.muted),
                    ToolCallStatus::InProgress => ("◐", palette.warning),
                    ToolCallStatus::Completed => ("✓", palette.success),
                    ToolCallStatus::Failed => ("✕", palette.error),
                    _ => ("·", palette.muted),
                };
                let bullet = if row == 0 {
                    marker.fg(color).bold()
                } else {
                    " ".into()
                };
                let disclosure = if row != 0 || !self.has_detail() {
                    " "
                } else if self.expanded {
                    "▾"
                } else {
                    "▸"
                };
                let mut spans = vec![bullet, " ".into()];
                if let Some(approval) = self.approval {
                    let diamond = match (row, approval) {
                        (0, ManualApproval::Pending) => "◈".fg(palette.warning),
                        (0, ManualApproval::Granted) => "◆".fg(palette.accent),
                        (0, ManualApproval::Rejected) => "◇".fg(palette.error),
                        _ => " ".into(),
                    };
                    spans.extend([diamond, " ".into()]);
                }
                spans.extend([disclosure.fg(palette.muted), " ".into()]);
                if row == 0 {
                    let (verb, subject) = text.split_once(' ').unwrap_or((text, ""));
                    let verb = if self.shell && self.approval == Some(ManualApproval::Pending) {
                        "Run"
                    } else {
                        verb
                    };
                    spans.push(verb.fg(palette.foreground).bold());
                    if !subject.is_empty() {
                        spans.push(" ".into());
                        let color = if self.shell {
                            palette.accent
                        } else {
                            palette.muted
                        };
                        spans.push(subject.fg(color));
                    }
                } else {
                    let color = if self.shell {
                        palette.accent
                    } else {
                        palette.muted
                    };
                    spans.push(text.fg(color));
                }
                return Line::from(spans);
            }
            if text.trim_start().starts_with("```") {
                return Line::default();
            }
            if self.code_rows.get(row).copied().unwrap_or(false) {
                return Line::styled(text, Style::default().fg(Color::Yellow));
            }
            if matches!(self.role, Role::Agent | Role::Thought) {
                let at_line_start = range.start == 0 || self.text[..range.start].ends_with('\n');
                if at_line_start {
                    let trimmed = text.trim_start();
                    let hashes = trimmed.chars().take_while(|ch| *ch == '#').count();
                    if (1..=6).contains(&hashes) && trimmed[hashes..].starts_with(' ') {
                        return Line::styled(
                            trimmed[hashes..].trim_start(),
                            Style::default().add_modifier(ratatui::style::Modifier::BOLD),
                        );
                    }
                }
                let line = Line::from(inline(text));
                return if self.role == Role::Thought {
                    line.fg(palette.muted).italic()
                } else {
                    line
                };
            }
            if matches!(self.role, Role::Command(_)) {
                let mut parts = vec!["▏ ".fg(palette.border)];
                if let Some((name, description)) = text.split_once("  ") {
                    parts.push(name.fg(palette.accent));
                    parts.push("  ".into());
                    parts.push(description.fg(palette.foreground));
                } else {
                    parts.push(text.fg(palette.foreground));
                }
                return Line::from(parts).bg(palette.panel);
            }
            return Line::raw(text);
        }
        let body_row = row - self.rows.len();
        if body_row == 0
            && self.expanded
            && let Some(approval) = self.approval
        {
            let (label, color) = match approval {
                ManualApproval::Pending => ("Approval · Waiting for your choice", palette.warning),
                ManualApproval::Granted => ("Approval · Manual", palette.accent),
                ManualApproval::Rejected => ("Approval · Rejected", palette.error),
            };
            return Line::from(vec!["    ".into(), label.fg(color)]);
        }
        let detail_row = body_row - self.approval_rows();
        if detail_row == self.detail_rows.len() && self.has_more_output() {
            return Line::from("    … more output").fg(palette.muted);
        }
        if let Some(detail) = &self.detail
            && let Some(range) = self.detail_rows.get(detail_row)
        {
            let text = &detail[range.clone()];
            if self.shell || !self.expanded {
                let prefix = if detail_row == 0 { "  ↳ " } else { "    " };
                return Line::from(vec![prefix.fg(palette.border), text.fg(palette.muted)]);
            }
            let start = detail[..range.start]
                .rfind('\n')
                .map_or(0, |index| index + 1);
            let style = match &detail[start..] {
                line if line.starts_with("+ ") => Style::default().fg(palette.success),
                line if line.starts_with("- ") => Style::default().fg(palette.error),
                _ if matches!(text, "Arguments" | "Changes" | "Contents" | "Output") => {
                    Style::default().fg(palette.accent).bold()
                }
                _ => Style::default().fg(palette.muted),
            };
            return Line::from(vec!["│ ".fg(palette.border), Span::styled(text, style)]);
        }
        Line::default()
    }
}

pub(super) fn notice_line(
    text: &str,
    row: Range<usize>,
    color: Color,
    is_command: fn(&str) -> bool,
) -> Line<'_> {
    let mut spans = Vec::new();
    // Recognize the full command even when the viewport splits it across rows.
    let start = text[..row.start].rfind(char::is_whitespace).unwrap_or(0);
    let end = text[row.end..]
        .find(char::is_whitespace)
        .map_or(text.len(), |offset| row.end + offset);
    let mut offset = start;
    let mut plain_start = row.start;
    for word in text[start..end].split_inclusive(char::is_whitespace) {
        let token = word.trim_start_matches(['`', '\'', '"', '(', '[']);
        let command = token.trim_end_matches(|ch: char| {
            ch.is_whitespace()
                || matches!(
                    ch,
                    '`' | '\'' | '"' | ')' | ']' | '.' | ',' | ':' | ';' | '!' | '?'
                )
        });
        if command.starts_with('/') && is_command(command) {
            let command_start = offset + word.len() - token.len();
            let start = command_start.max(row.start);
            let end = (command_start + command.len()).min(row.end);
            if start < end {
                spans.push(Span::raw(&text[plain_start..start]));
                spans.push(text[start..end].fg(color).bold());
                plain_start = end;
            }
        }
        offset += word.len();
    }
    spans.push(Span::raw(&text[plain_start..row.end]));
    Line::from(spans)
}

pub(super) fn inline(text: &str) -> Vec<Span<'_>> {
    let mut spans = Vec::new();
    let mut rest = text;
    while !rest.is_empty() {
        if let Some((label, url, tail)) = link(rest) {
            spans.push(Span::styled(
                label,
                Style::default().add_modifier(ratatui::style::Modifier::UNDERLINED),
            ));
            if !url.is_empty() {
                spans.push(Span::raw(" "));
                spans.push(Span::styled(url, Style::default().fg(Color::Blue)));
            }
            rest = tail;
            continue;
        }
        if let Some(tail) = rest.strip_prefix("**")
            && let Some(end) = tail.find("**")
        {
            spans.push(Span::styled(
                &tail[..end],
                Style::default().add_modifier(ratatui::style::Modifier::BOLD),
            ));
            rest = &tail[end + 2..];
            continue;
        }
        if let Some(tail) = rest.strip_prefix('`')
            && let Some(end) = tail.find('`')
        {
            spans.push(Span::styled(&tail[..end], Style::default().fg(Color::Cyan)));
            rest = &tail[end + 1..];
            continue;
        }
        if let Some(marker) = rest.chars().next().filter(|ch| matches!(ch, '*' | '_'))
            && let Some(tail) = rest.strip_prefix(marker)
            && let Some(end) = tail.find(marker)
        {
            spans.push(Span::styled(
                &tail[..end],
                Style::default().add_modifier(ratatui::style::Modifier::ITALIC),
            ));
            rest = &tail[end + marker.len_utf8()..];
            continue;
        }
        let next = rest
            .char_indices()
            .skip(1)
            .find(|(_, ch)| matches!(ch, '*' | '_' | '`' | '['))
            .map_or(rest.len(), |(index, _)| index);
        spans.push(Span::raw(&rest[..next]));
        rest = &rest[next..];
    }
    spans
}

fn link(text: &str) -> Option<(&str, &str, &str)> {
    let tail = text.strip_prefix('[')?;
    let close = tail.find("](")?;
    let rest = &tail[close + 2..];
    let end = rest.find(')')?;
    Some((&tail[..close], &rest[..end], &rest[end + 1..]))
}
