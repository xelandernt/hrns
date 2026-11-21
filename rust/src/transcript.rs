use std::ops::Range;

use agent_client_protocol::v1::schema::ToolCallStatus;
use ratatui::style::{Color, Style, Stylize};
use ratatui::text::{Line, Span};
use unicode_segmentation::UnicodeSegmentation;
use unicode_width::UnicodeWidthStr;

use crate::acp::ToolDisplay;
use crate::theme::Palette;

#[derive(PartialEq)]
pub enum Role {
    User,
    Agent,
    Thought,
    Tool,
    Command(String),
    Note,
    Error,
}

impl Role {
    fn label(&self, palette: &Palette) -> Option<Line<'_>> {
        let (name, color) = match self {
            Self::User => ("You", palette.accent),
            Self::Agent => ("Agent", palette.muted),
            Self::Thought => ("Reasoning", palette.muted),
            Self::Command(name) => {
                return Some(
                    Line::from(vec![
                        "▌ ".fg(palette.accent),
                        name.as_str().fg(palette.accent).bold(),
                    ])
                    .bg(palette.panel),
                );
            }
            Self::Tool | Self::Note => return None,
            Self::Error => ("Error", palette.error),
        };
        Some(Line::styled(name, Style::default().fg(color).bold()))
    }
}

struct Entry {
    role: Role,
    text: String,
    rows: Vec<Range<usize>>,
    code_rows: Vec<bool>,
    detail: Option<String>,
    detail_rows: Vec<Range<usize>>,
    shell: bool,
    id: Option<String>,
    expanded: bool,
    tool_status: Option<ToolCallStatus>,
    end: usize,
}

impl Entry {
    fn has_detail(&self) -> bool {
        self.detail
            .as_ref()
            .is_some_and(|detail| !detail.is_empty())
    }

    fn heading_rows(&self) -> usize {
        usize::from(!matches!(self.role, Role::Note | Role::Tool))
    }

    fn line(&self, mut row: usize, palette: &Palette) -> Line<'_> {
        if let Some(heading) = self.role.label(palette) {
            if row == 0 {
                return heading;
            }
            row -= 1;
        }
        if let Some(range) = self.rows.get(row) {
            let text = &self.text[range.clone()];
            if let Some(status) = self.tool_status {
                let (marker, color) = match status {
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
                let mut spans = vec![bullet, " ".into(), disclosure.fg(palette.muted), " ".into()];
                if row == 0 {
                    let (verb, subject) = text.split_once(' ').unwrap_or((text, ""));
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
        let detail_row = row - self.rows.len();
        if let Some(detail) = &self.detail
            && let Some(range) = self.detail_rows.get(detail_row)
        {
            let text = &detail[range.clone()];
            if self.shell {
                let prefix = if detail_row == 0 { "  ↳ " } else { "    " };
                return Line::from(vec![prefix.fg(palette.border), text.fg(palette.muted)]);
            }
            let start = detail[..range.start]
                .rfind('\n')
                .map_or(0, |index| index + 1);
            let style = match &detail[start..] {
                line if line.starts_with("+ ") => Style::default().fg(palette.success),
                line if line.starts_with("- ") => Style::default().fg(palette.error),
                _ if matches!(text, "Arguments" | "Output") => {
                    Style::default().fg(palette.accent).bold()
                }
                _ => Style::default().fg(palette.muted),
            };
            return Line::from(vec!["│ ".fg(palette.border), Span::styled(text, style)]);
        }
        Line::default()
    }
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
    Text(usize),
    Detail(usize),
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
    show_thoughts: bool,
}

impl Transcript {
    pub fn new() -> Self {
        Self {
            following: true,
            ..Self::default()
        }
    }

    pub fn push(&mut self, role: Role, mut text: String) {
        self.output_pending |= !self.following && (role != Role::Thought || self.show_thoughts);
        text.retain(|ch| ch == '\n' || !ch.is_control());
        self.entries.push(Entry {
            role,
            text,
            rows: Vec::new(),
            code_rows: Vec::new(),
            detail: None,
            detail_rows: Vec::new(),
            shell: false,
            id: None,
            expanded: false,
            tool_status: None,
            end: 0,
        });
    }

    pub fn tool(&mut self, id: String, display: ToolDisplay) {
        let ToolDisplay {
            title,
            subject,
            status,
            mut detail,
            shell,
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
        detail.retain(|ch| ch == '\n' || ch == '\t' || !ch.is_control());
        let detail = detail.trim_end().replace('\t', "    ");
        if let Some(index) = self
            .entries
            .iter()
            .position(|entry| entry.id.as_deref() == Some(id.as_str()))
        {
            let entry = &mut self.entries[index];
            entry.text = text;
            entry.tool_status = Some(status);
            entry.shell = shell;
            entry.detail = Some(detail);
            self.measured = self.measured.min(index);
            return;
        }
        self.entries.push(Entry {
            role: Role::Tool,
            text,
            rows: Vec::new(),
            code_rows: Vec::new(),
            detail: Some(detail),
            detail_rows: Vec::new(),
            shell,
            id: Some(id),
            expanded: false,
            tool_status: Some(status),
            end: 0,
        });
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

    pub fn toggle_tool_at(&mut self, row: u16) -> bool {
        let row = self.top.saturating_add(usize::from(row));
        let index = self.entries[..self.laid_out].partition_point(|entry| entry.end <= row);
        if index == self.laid_out {
            return false;
        }
        let start = self.entry_start(index);
        let entry = &mut self.entries[index];
        if entry.role != Role::Tool || !entry.has_detail() || row >= start + entry.rows.len() {
            return false;
        }
        entry.expanded = !entry.expanded;
        self.measured = self.measured.min(index);
        self.following = false;

        true
    }

    pub fn show_thoughts(&mut self, show: bool) {
        if self.show_thoughts != show {
            self.show_thoughts = show;
            self.measured = 0;
        }
    }

    pub fn append(&mut self, role: Role, mut text: String) {
        self.output_pending |= !self.following && (role != Role::Thought || self.show_thoughts);
        if let Some(entry) = self.entries.last_mut().filter(|entry| entry.role == role) {
            text.retain(|ch| ch == '\n' || !ch.is_control());
            entry.text.push_str(&text);
            self.measured = self.measured.min(self.entries.len() - 1);
        } else {
            self.push(role, text);
        }
    }

    pub fn scroll_page(&mut self, down: bool) {
        let distance = self.viewport_height.saturating_sub(2).max(1) as i32;
        self.scroll(if down { distance } else { -distance });
    }

    pub fn scroll(&mut self, rows: i32) {
        let end = self.total.saturating_sub(self.viewport_height);
        self.top = self.top.saturating_add_signed(rows as isize).min(end);
        if rows > 0 && self.top == end {
            self.follow();
        } else {
            self.following = false;
        }
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
    }

    fn anchor(&self) -> Option<(usize, AnchorRow)> {
        let index = self.entries[..self.laid_out].partition_point(|entry| entry.end <= self.top);
        let entry = self.entries.get(index)?;
        let row = self.top.saturating_sub(self.entry_start(index));
        let anchor = match row.checked_sub(entry.heading_rows()) {
            Some(row) if row < entry.rows.len() => AnchorRow::Text(entry.rows[row].start),
            Some(row) => entry
                .detail_rows
                .get(row - entry.rows.len())
                .map_or(AnchorRow::Spacer, |range| AnchorRow::Detail(range.start)),
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
            AnchorRow::Text(byte) => start + entry.heading_rows() + row_at(&entry.rows, byte),
            AnchorRow::Detail(byte) if !entry.detail_rows.is_empty() => {
                start + entry.heading_rows() + entry.rows.len() + row_at(&entry.detail_rows, byte)
            }
            _ => start,
        }
    }

    pub fn follow(&mut self) {
        self.following = true;
        self.output_pending = false;
    }

    pub fn output_pending(&self) -> bool {
        self.output_pending
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
                .map_or(0, |row| row + label + entry.rows.len())
        };
        self.top = index
            .checked_sub(1)
            .map_or(0, |prev| self.entries[prev].end)
            + row;
        self.following = false;
        true
    }

    pub fn visible(&mut self, width: u16, height: u16, palette: &Palette) -> Vec<Line<'_>> {
        if width == 0 || height == 0 {
            return Vec::new();
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
        if self.following {
            self.top = end;
        } else {
            self.top = self.top.min(end);
        }

        let mut row = self.top;
        let mut index = self.entries.partition_point(|entry| entry.end <= row);
        let mut lines = Vec::with_capacity(usize::from(height));
        while let Some(entry) = self.entries.get(index) {
            let start = self.entry_start(index);
            while row < entry.end {
                let line = entry.line(row - start, palette);
                lines.push(line);
                if lines.len() == usize::from(height) {
                    return lines;
                }
                row += 1;
            }
            index += 1;
        }
        lines
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
            let width = usize::from(self.width);
            if matches!(entry.role, Role::Agent | Role::Thought) {
                (entry.rows, entry.code_rows) = wrap_markdown(&entry.text, width);
            } else {
                let indent = match entry.role {
                    Role::Tool => 4,
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
            } else {
                Vec::new()
            };
            end += entry.rows.len() + entry.detail_rows.len() + 1 + entry.heading_rows();
            entry.end = end;
        }
        self.measured = self.entries.len();
        self.laid_out = self.measured;
        self.total = end;
    }
}

fn wrap_markdown(text: &str, width: usize) -> (Vec<Range<usize>>, Vec<bool>) {
    let width = width.max(1);
    let mut rows = Vec::new();
    let mut code_rows = Vec::new();
    let mut fence = false;
    let mut start = 0;
    for line in text.split_inclusive('\n') {
        let marker = line.trim_start().starts_with("```");
        let code = fence || marker;
        for range in wrap(line, width) {
            rows.push(start + range.start..start + range.end);
            code_rows.push(code);
        }
        if marker {
            fence = !fence;
        }
        start += line.len();
    }
    (rows, code_rows)
}

fn wrap(text: &str, width: usize) -> Vec<Range<usize>> {
    let mut rows = Vec::new();
    let mut line_start = 0;
    let mut columns = 0;
    for (index, grapheme) in text.grapheme_indices(true) {
        if grapheme == "\n" {
            rows.push(line_start..index);
            line_start = index + 1;
            columns = 0;
            continue;
        }
        let next = grapheme.width();
        if columns != 0 && columns + next > width {
            rows.push(line_start..index);
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
