mod table;

use std::collections::BTreeMap;
use std::ops::Range;

use agent_client_protocol::v1::schema::ToolCallStatus;
use ratatui::style::{Color, Style, Stylize};
use ratatui::text::{Line, Span};
use unicode_segmentation::UnicodeSegmentation;
use unicode_width::UnicodeWidthStr;

use crate::acp::{PermissionRequest, ToolDisplay};
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
    fn notice_color(&self, palette: &Palette) -> Option<Color> {
        match self {
            Self::Note => Some(palette.system),
            Self::Warning => Some(palette.warning),
            Self::Error => Some(palette.error),
            _ => None,
        }
    }

    fn indent(&self, width: usize) -> &'static str {
        if width >= 10 && matches!(self, Self::Thought | Self::Tool) {
            "  "
        } else {
            ""
        }
    }

    fn label(&self, palette: &Palette, expanded: bool) -> Option<Line<'_>> {
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

struct Entry {
    role: Role,
    text: String,
    rows: Vec<Range<usize>>,
    code_rows: Vec<bool>,
    table_rows: BTreeMap<usize, Line<'static>>,
    detail: Option<String>,
    detail_rows: Vec<Range<usize>>,
    preview_start: Option<usize>,
    shell: bool,
    id: Option<String>,
    expanded: bool,
    tool_status: Option<ToolCallStatus>,
    approval: Option<ManualApproval>,
    end: usize,
}

impl Entry {
    fn has_more_output(&self) -> bool {
        !self.expanded
            && self.detail_rows.last().is_some_and(|row| {
                self.detail
                    .as_ref()
                    .is_some_and(|detail| row.end < detail.len())
            })
    }

    fn has_detail(&self) -> bool {
        self.approval.is_some()
            || self
                .detail
                .as_ref()
                .is_some_and(|detail| !detail.is_empty())
    }

    fn heading_rows(&self) -> usize {
        usize::from(self.role != Role::Tool)
    }

    fn approval_rows(&self) -> usize {
        usize::from(self.expanded && self.approval.is_some())
    }

    fn line(&self, mut row: usize, palette: &Palette, is_command: fn(&str) -> bool) -> Line<'_> {
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

fn notice_line(
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

fn inline(text: &str) -> Vec<Span<'_>> {
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

enum AnchorRow {
    Start,
    Spacer,
    Text { byte: usize, line_offset: usize },
    Detail(usize),
    Approval,
}

#[derive(Default)]
pub struct Transcript {
    entries: Vec<Entry>,
    measured: usize,
    laid_out: usize,
    viewport_height: usize,
    width: u16,
    total: usize,
    top: usize,
    following: bool,
    output_pending: bool,
    selected_prompt: Option<usize>,
    show_thoughts: bool,
}

impl Transcript {
    pub fn new() -> Self {
        Self {
            following: true,
            ..Self::default()
        }
    }

    pub fn push(&mut self, role: Role, text: String) {
        self.push_entry(role, text, false);
    }

    fn push_entry(&mut self, role: Role, mut text: String, expanded: bool) {
        self.output_pending |= !self.following && (role != Role::Thought || self.show_thoughts);
        text.retain(|ch| ch == '\n' || !ch.is_control());
        self.entries.push(Entry {
            role,
            text,
            rows: Vec::new(),
            code_rows: Vec::new(),
            table_rows: BTreeMap::new(),
            detail: None,
            detail_rows: Vec::new(),
            preview_start: None,
            shell: false,
            id: None,
            expanded,
            tool_status: None,
            approval: None,
            end: 0,
        });
    }

    pub fn tool(&mut self, id: String, mut display: ToolDisplay) {
        let preview_start = display.prepare_detail().filter(|_| {
            display.preview_output
                && matches!(
                    display.status,
                    ToolCallStatus::Completed | ToolCallStatus::Failed
                )
        });
        let ToolDisplay {
            title,
            subject,
            status,
            detail,
            shell,
            ..
        } = display;
        self.output_pending |= !self.following;
        let mut text = if subject.is_empty() {
            title
        } else {
            format!("{title} {subject}")
        };
        if shell {
            text.retain(|ch| matches!(ch, '\n' | '\t') || !ch.is_control());
            text = text.replace('\t', "    ");
        } else {
            text.retain(|ch| !ch.is_control() || ch.is_whitespace());
            text = text.split_whitespace().collect::<Vec<_>>().join(" ");
        }
        if !shell && status != ToolCallStatus::Completed {
            text.push_str("  ·  ");
            text.push_str(crate::acp::status_label(status));
        }
        if let Some(index) = self.tool_index(&id) {
            let entry = &mut self.entries[index];
            if entry.approval != Some(ManualApproval::Rejected) {
                entry.text = text;
            }
            entry.tool_status = Some(status);
            entry.shell = shell;
            entry.detail = Some(detail);
            entry.preview_start = preview_start;
            self.measured = self.measured.min(index);
            return;
        }
        self.entries.push(Entry {
            role: Role::Tool,
            text,
            rows: Vec::new(),
            code_rows: Vec::new(),
            table_rows: BTreeMap::new(),
            detail: Some(detail),
            detail_rows: Vec::new(),
            preview_start,
            shell,
            id: Some(id),
            expanded: false,
            tool_status: Some(status),
            approval: None,
            end: 0,
        });
    }

    fn tool_index(&self, id: &str) -> Option<usize> {
        self.entries
            .iter()
            .rposition(|entry| entry.id.as_deref() == Some(id))
    }

    pub fn show_pending_approval(&mut self, request: &PermissionRequest) {
        let index = self.tool_index(&request.tool_call_id).unwrap_or_else(|| {
            self.tool(
                request.tool_call_id.clone(),
                ToolDisplay {
                    title: request.title.clone(),
                    subject: String::new(),
                    status: ToolCallStatus::Pending,
                    detail: request.detail.clone(),
                    output: String::new(),
                    preview_output: false,
                    shell: request.shell,
                },
            );
            self.entries.len() - 1
        });
        self.entries[index].approval = Some(ManualApproval::Pending);
        self.output_pending |= !self.following;
        self.measured = self.measured.min(index);
    }

    pub fn resolve_approval(&mut self, id: &str, title: &str, decision: Option<ManualApproval>) {
        let Some(index) = self.tool_index(id) else {
            return;
        };
        let entry = &mut self.entries[index];
        if entry.approval != Some(ManualApproval::Pending) {
            return;
        }
        entry.approval = decision;
        if decision == Some(ManualApproval::Rejected) {
            entry.text = title.to_owned();
        }
        self.output_pending |= !self.following;
        self.measured = self.measured.min(index);
    }

    pub fn toggle_tools(&mut self) {
        self.set_tools_expanded(
            self.entries
                .iter()
                .any(|entry| entry.has_detail() && !entry.expanded),
        );
    }

    pub fn set_tools_expanded(&mut self, expanded: bool) {
        for entry in &mut self.entries {
            if entry.role == Role::Tool {
                entry.expanded = expanded;
            }
        }
        self.measured = 0;
    }

    pub fn toggle_disclosure_at(&mut self, row: u16) -> bool {
        let row = self.top.saturating_add(usize::from(row));
        let index = self.entries[..self.laid_out].partition_point(|entry| entry.end <= row);
        if index == self.laid_out {
            return false;
        }
        let start = self.entry_start(index);
        let entry = &mut self.entries[index];
        let header_hit = match entry.role {
            Role::Tool => {
                entry.has_detail()
                    && row
                        < start
                            + entry.rows.len()
                            + if entry.expanded {
                                0
                            } else {
                                entry.detail_rows.len() + usize::from(entry.has_more_output())
                            }
            }
            Role::Thought => self.show_thoughts && row == start,
            _ => false,
        };
        if !header_hit {
            return false;
        }
        entry.expanded = !entry.expanded;
        self.measured = self.measured.min(index);
        self.following = false;
        self.selected_prompt = None;
        true
    }

    pub fn show_thoughts(&mut self, show: bool) {
        if self.show_thoughts != show {
            self.show_thoughts = show;
            for entry in &mut self.entries {
                if entry.role == Role::Thought {
                    entry.expanded = false;
                }
            }
            self.measured = 0;
        }
    }

    pub fn append(&mut self, role: Role, text: String) {
        self.append_chunk(role, text, false);
    }

    pub fn append_thought(&mut self, text: String, live: bool) {
        self.append_chunk(Role::Thought, text, live && self.show_thoughts);
    }

    fn append_chunk(&mut self, role: Role, mut text: String, expand_new: bool) {
        self.output_pending |= !self.following && (role != Role::Thought || self.show_thoughts);
        if let Some(entry) = self.entries.last_mut().filter(|entry| entry.role == role) {
            text.retain(|ch| ch == '\n' || !ch.is_control());
            entry.text.push_str(&text);
            self.measured = self.measured.min(self.entries.len() - 1);
        } else {
            self.push_entry(role, text, expand_new);
        }
    }

    pub fn scroll_page(&mut self, down: bool) {
        let distance = self.viewport_height.saturating_sub(2).max(1) as i32;
        self.scroll(if down { distance } else { -distance });
    }

    pub fn scroll(&mut self, rows: i32) {
        self.selected_prompt = None;
        let end = self.total.saturating_sub(self.viewport_height);
        self.top = self.top.saturating_add_signed(rows as isize).min(end);
        if rows > 0 && self.top == end {
            self.follow();
        } else {
            self.following = false;
        }
    }

    /// Moves to `top`, following the tail when the move reaches it.
    pub fn scroll_to(&mut self, top: usize) {
        self.selected_prompt = None;
        let end = self.total.saturating_sub(self.viewport_height);
        self.top = top.min(end);
        if self.top >= end {
            self.follow();
        } else {
            self.following = false;
        }
    }

    pub fn top(&self) -> usize {
        self.top
    }

    pub fn total(&self) -> usize {
        self.total
    }

    pub fn viewport_height(&self) -> usize {
        self.viewport_height
    }

    pub fn following(&self) -> bool {
        self.following
    }

    pub fn len(&self) -> usize {
        self.entries.len()
    }

    /// Discards everything from `from` onward, keeping the prefix intact.
    pub fn trim(&mut self, from: usize) {
        self.entries.truncate(from.min(self.entries.len()));
        self.reset_layout();
    }

    /// Drops the first `from` entries and keeps the entries a replay appended after them.
    pub fn replace(&mut self, from: usize) {
        self.entries.drain(..from.min(self.entries.len()));
        self.reset_layout();
        self.follow();
    }

    fn reset_layout(&mut self) {
        self.measured = 0;
        self.laid_out = 0;
        self.total = 0;
        self.top = 0;
        self.selected_prompt = None;
    }

    fn anchor(&self) -> Option<(usize, AnchorRow)> {
        let index = self.entries[..self.laid_out].partition_point(|entry| entry.end <= self.top);
        let entry = self.entries.get(index)?;
        let row = self.top.saturating_sub(self.entry_start(index));
        let anchor = match row.checked_sub(entry.heading_rows()) {
            Some(row) if row < entry.rows.len() => {
                let byte = entry.rows[row].start;
                let first = entry.rows.partition_point(|range| range.start < byte);
                AnchorRow::Text {
                    byte,
                    line_offset: row - first,
                }
            }
            Some(row) => row
                .checked_sub(entry.rows.len() + entry.approval_rows())
                .map_or(AnchorRow::Approval, |detail| {
                    entry
                        .detail_rows
                        .get(detail)
                        .map_or(AnchorRow::Spacer, |range| AnchorRow::Detail(range.start))
                }),
            None => AnchorRow::Start,
        };
        Some((index, anchor))
    }

    fn restore(&self, (index, anchor): (usize, AnchorRow)) -> usize {
        let entry = &self.entries[index];
        let start = self.entry_start(index);
        if entry.end == start {
            return start;
        }
        let row_at = |rows: &[Range<usize>], byte: usize| {
            rows.partition_point(|range| range.start <= byte)
                .saturating_sub(1)
        };
        match anchor {
            AnchorRow::Spacer => entry.end.saturating_sub(1),
            AnchorRow::Text { byte, line_offset } => {
                let last = row_at(&entry.rows, byte);
                let row = entry.rows.get(last).map_or(last, |range| {
                    let first = entry
                        .rows
                        .partition_point(|candidate| candidate.start < range.start);
                    first + line_offset.min(last - first)
                });
                start + entry.heading_rows() + row
            }
            AnchorRow::Detail(byte) if !entry.detail_rows.is_empty() => {
                start
                    + entry.heading_rows()
                    + entry.rows.len()
                    + entry.approval_rows()
                    + row_at(&entry.detail_rows, byte)
            }
            AnchorRow::Approval if entry.approval_rows() > 0 => {
                start + entry.heading_rows() + entry.rows.len()
            }
            _ => start,
        }
    }

    pub fn follow(&mut self) {
        self.following = true;
        self.output_pending = false;
        self.selected_prompt = None;
    }

    pub fn output_pending(&self) -> bool {
        self.output_pending
    }

    pub fn jump_prompt(&mut self, forward: bool) {
        self.measure();
        let prompts = self
            .entries
            .iter()
            .enumerate()
            .filter(|(_, entry)| entry.role == Role::User);
        let target = if let Some(selected) = self.selected_prompt {
            if forward {
                prompts
                    .map(|(index, _)| index)
                    .find(|&index| index > selected)
            } else {
                prompts
                    .map(|(index, _)| index)
                    .rfind(|&index| index < selected)
            }
        } else if forward {
            prompts
                .map(|(index, _)| index)
                .find(|&index| self.entry_start(index) > self.top)
        } else {
            prompts
                .map(|(index, _)| index)
                .rfind(|&index| self.following || self.entry_start(index) < self.top)
        };
        if let Some(target) = target {
            self.selected_prompt = Some(target);
            self.following = false;
        }
    }

    fn entry_start(&self, index: usize) -> usize {
        index
            .checked_sub(1)
            .map_or(0, |previous| self.entries[previous].end)
    }

    pub fn latest_agent(&self) -> Option<&str> {
        self.entries
            .iter()
            .rev()
            .find(|entry| entry.role == Role::Agent)
            .map(|entry| entry.text.as_str())
    }

    pub fn find(&mut self, query: &str) -> bool {
        let query = query.to_lowercase();
        self.measure();
        let Some((index, in_text)) =
            self.entries
                .iter()
                .enumerate()
                .rev()
                .find_map(|(index, entry)| {
                    if entry.role == Role::Thought
                        && (!self.show_thoughts || entry.text.trim().is_empty())
                    {
                        return None;
                    }
                    if entry.text.to_lowercase().contains(&query) {
                        Some((index, true))
                    } else if entry
                        .detail
                        .as_deref()
                        .is_some_and(|detail| detail.to_lowercase().contains(&query))
                    {
                        Some((index, false))
                    } else {
                        None
                    }
                })
        else {
            return false;
        };
        if !in_text && !self.entries[index].expanded {
            self.entries[index].expanded = true;
            self.measured = self.measured.min(index);
            self.measure();
        }
        let entry = &self.entries[index];
        let label = entry.heading_rows();
        let row = if in_text {
            entry
                .rows
                .iter()
                .position(|range| entry.text[range.clone()].to_lowercase().contains(&query))
                .map_or(0, |row| row + label)
        } else {
            entry
                .detail_rows
                .iter()
                .position(|range| {
                    entry
                        .detail
                        .as_deref()
                        .is_some_and(|detail| detail[range.clone()].to_lowercase().contains(&query))
                })
                .map_or(0, |row| {
                    row + label + entry.rows.len() + entry.approval_rows()
                })
        };
        self.top = index
            .checked_sub(1)
            .map_or(0, |prev| self.entries[prev].end)
            + row;
        self.following = false;
        self.selected_prompt = None;
        true
    }

    pub fn visible(
        &mut self,
        width: u16,
        height: u16,
        palette: &Palette,
        is_command: fn(&str) -> bool,
    ) -> VisibleTranscript<'_> {
        let mut visible = VisibleTranscript::default();
        if width == 0 || height == 0 {
            return visible;
        }
        let reflow = self.width != width || self.measured < self.entries.len();
        let anchor = if reflow && !self.following {
            self.anchor()
        } else {
            None
        };
        if self.width != width {
            self.width = width;
            self.measured = 0;
        }
        self.viewport_height = usize::from(height);
        self.measure();
        if let Some(anchor) = anchor {
            self.top = self.restore(anchor);
        }
        let end = self.total.saturating_sub(self.viewport_height);
        if let Some(selected) = self.selected_prompt {
            self.top = self.entry_start(selected).min(end);
        } else if self.following {
            self.top = end;
        } else {
            self.top = self.top.min(end);
        }

        let mut row = self.top;
        let mut index = self.entries.partition_point(|entry| entry.end <= row);
        visible.lines.reserve(usize::from(height));
        while let Some(entry) = self.entries.get(index) {
            let start = self.entry_start(index);
            let border = entry.role.notice_color(palette);
            let content_rows = entry.heading_rows() + entry.rows.len();
            while row < entry.end {
                let entry_row = row - start;
                let mut line = entry.line(entry_row, palette, is_command);
                let indent = entry.role.indent(usize::from(width));
                if !indent.is_empty() && !line.spans.is_empty() {
                    line.spans.insert(0, Span::raw(indent));
                }
                if entry.role == Role::User && entry_row < content_rows {
                    if line.width() == 0 {
                        line.spans.push(Span::raw(" "));
                    }
                    line = if self.selected_prompt == Some(index) {
                        line.bg(palette.panel).bold()
                    } else {
                        line.bg(palette.surface)
                    };
                }
                if let Some(color) = border
                    && entry_row < content_rows
                {
                    visible
                        .notice_borders
                        .push(((row - self.top) as u16, color));
                }
                visible.lines.push(line);
                if visible.lines.len() == usize::from(height) {
                    return visible;
                }
                row += 1;
            }
            index += 1;
        }
        visible
    }

    fn measure(&mut self) {
        let mut end = self
            .measured
            .checked_sub(1)
            .map_or(0, |prev| self.entries[prev].end);
        for entry in &mut self.entries[self.measured..] {
            if entry.role == Role::Thought && (!self.show_thoughts || entry.text.trim().is_empty())
            {
                entry.end = end;
                continue;
            }
            let viewport_width = usize::from(self.width);
            let width = viewport_width.saturating_sub(entry.role.indent(viewport_width).len());
            entry.table_rows.clear();
            if entry.role == Role::Thought && !entry.expanded {
                entry.rows.clear();
                entry.code_rows.clear();
            } else if matches!(entry.role, Role::Agent | Role::Thought) {
                let layout = MarkdownLayout::new(&entry.text, width);
                entry.rows = layout.rows;
                entry.code_rows = layout.code_rows;
                entry.table_rows = layout.table_rows;
            } else {
                let indent = match entry.role {
                    Role::Tool => 4 + 2 * usize::from(entry.approval.is_some()),
                    Role::Command(_) => 2,
                    _ => 0,
                };
                entry.rows = wrap(&entry.text, width.saturating_sub(indent).max(1));
                entry.code_rows.clear();
            }
            entry.detail_rows = if entry.expanded {
                let indent = if entry.shell { 4 } else { 2 };
                entry.detail.as_deref().map_or_else(Vec::new, |detail| {
                    wrap(detail, width.saturating_sub(indent).max(1))
                })
            } else if let Some(start) = entry.preview_start
                && let Some(detail) = entry.detail.as_deref()
            {
                let mut rows = wrap_up_to(&detail[start..], width.saturating_sub(4).max(1), 2);
                for range in &mut rows {
                    range.start += start;
                    range.end += start;
                }
                rows
            } else {
                Vec::new()
            };
            end += entry.rows.len()
                + entry.detail_rows.len()
                + entry.approval_rows()
                + usize::from(entry.has_more_output())
                + 1
                + entry.heading_rows();
            entry.end = end;
        }
        self.measured = self.entries.len();
        self.laid_out = self.measured;
        self.total = end;
    }
}

#[derive(Default)]
pub struct VisibleTranscript<'a> {
    pub lines: Vec<Line<'a>>,
    pub notice_borders: Vec<(u16, Color)>,
}

#[derive(Default)]
struct MarkdownLayout {
    rows: Vec<Range<usize>>,
    code_rows: Vec<bool>,
    table_rows: BTreeMap<usize, Line<'static>>,
}

impl MarkdownLayout {
    fn new(text: &str, width: usize) -> Self {
        let width = width.max(1);
        let mut layout = Self::default();
        let mut start = 0;
        for table in table::parse(text) {
            layout.push_text(&text[start..table.source.start], start, width);
            for (source, line) in table.lines(width) {
                layout.table_rows.insert(layout.rows.len(), line);
                layout.rows.push(source);
                layout.code_rows.push(false);
            }
            start = table.source.end;
        }
        layout.push_text(&text[start..], start, width);
        layout
    }

    fn push_text(&mut self, text: &str, mut start: usize, width: usize) {
        let mut fence = false;
        for line in text.split_inclusive('\n') {
            let marker = line.trim_start().starts_with("```");
            let code = fence || marker;
            for range in wrap(line, width) {
                self.rows.push(start + range.start..start + range.end);
                self.code_rows.push(code);
            }
            if marker {
                fence = !fence;
            }
            start += line.len();
        }
    }
}

pub(crate) fn wrap(text: &str, width: usize) -> Vec<Range<usize>> {
    wrap_up_to(text, width, usize::MAX)
}

pub(crate) fn wrap_up_to(text: &str, width: usize, limit: usize) -> Vec<Range<usize>> {
    let mut rows = Vec::new();
    let mut line_start = 0;
    let mut columns = 0;
    for (index, grapheme) in text.grapheme_indices(true) {
        if grapheme == "\n" {
            rows.push(line_start..index);
            if rows.len() == limit {
                return rows;
            }
            line_start = index + 1;
            columns = 0;
            continue;
        }
        let next = grapheme.width();
        if columns != 0 && columns + next > width {
            rows.push(line_start..index);
            if rows.len() == limit {
                return rows;
            }
            line_start = index;
            columns = 0;
        }
        columns += next;
    }
    if line_start < text.len() {
        rows.push(line_start..text.len());
    }
    rows
}
