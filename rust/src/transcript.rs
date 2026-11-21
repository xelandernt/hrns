use std::ops::Range;

use agent_client_protocol::v1::schema::ToolCallStatus;
use ratatui::style::{Color, Style, Stylize};
use ratatui::text::{Line, Span};
use unicode_segmentation::UnicodeSegmentation;
use unicode_width::UnicodeWidthStr;

use crate::acp::ToolDisplay;
use crate::theme::Palette;

const SHELL_PREVIEW_ROWS: usize = 4;

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
    more_output: bool,
    id: Option<String>,
    expanded: bool,
    tool_status: Option<ToolCallStatus>,
    end: usize,
}

impl Entry {
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
                let mut spans = vec![bullet, " ".into()];
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
        if self.more_output && detail_row == self.detail_rows.len() {
            return Line::from("    … Ctrl+O for more output").fg(palette.muted);
        }
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

#[derive(Default)]
pub struct Transcript {
    entries: Vec<Entry>,
    measured: usize,
    width: u16,
    total: usize,
    top: usize,
    following: bool,
    expanded_tools: bool,
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
        text.retain(|ch| ch == '\n' || !ch.is_control());
        self.entries.push(Entry {
            role,
            text,
            rows: Vec::new(),
            code_rows: Vec::new(),
            detail: None,
            detail_rows: Vec::new(),
            shell: false,
            more_output: false,
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
            more_output: false,
            id: Some(id),
            expanded: self.expanded_tools,
            tool_status: Some(status),
            end: 0,
        });
    }

    pub fn toggle_tools(&mut self) -> bool {
        self.expanded_tools = !self.expanded_tools;
        for entry in &mut self.entries {
            if entry.role == Role::Tool {
                entry.expanded = self.expanded_tools;
            }
        }
        self.measured = 0;
        self.total = 0;
        self.expanded_tools
    }

    pub fn set_tools_expanded(&mut self, expanded: bool) {
        self.expanded_tools = expanded;
        for entry in &mut self.entries {
            if entry.role == Role::Tool {
                entry.expanded = expanded;
            }
        }
        self.measured = 0;
        self.total = 0;
    }

    pub fn show_thoughts(&mut self, show: bool) {
        if self.show_thoughts != show {
            self.show_thoughts = show;
            self.measured = 0;
        }
    }

    pub fn append(&mut self, role: Role, mut text: String) {
        if let Some(entry) = self.entries.last_mut().filter(|entry| entry.role == role) {
            text.retain(|ch| ch == '\n' || !ch.is_control());
            entry.text.push_str(&text);
            self.measured = self.measured.min(self.entries.len() - 1);
        } else {
            self.push(role, text);
        }
    }

    pub fn scroll_up(&mut self) {
        self.following = false;
        self.top = self.top.saturating_sub(6);
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
        self.total = 0;
        self.top = 0;
    }

    fn anchor(&self) -> (usize, usize) {
        let index = self
            .entries
            .partition_point(|entry| entry.end <= self.top)
            .min(self.entries.len().saturating_sub(1));
        let Some(entry) = self.entries.get(index) else {
            return (0, 0);
        };
        let start = index
            .checked_sub(1)
            .map_or(0, |previous| self.entries[previous].end);
        let row = self.top.saturating_sub(start);
        let label = usize::from(!matches!(entry.role, Role::Note | Role::Tool));
        let byte = row
            .checked_sub(label)
            .and_then(|row| entry.rows.get(row))
            .map_or(0, |range| range.start);
        (index, byte)
    }

    fn restore(&self, (index, byte): (usize, usize)) -> usize {
        let Some(entry) = self.entries.get(index) else {
            return self.top;
        };
        let start = index
            .checked_sub(1)
            .map_or(0, |previous| self.entries[previous].end);
        let row = entry
            .rows
            .iter()
            .position(|range| range.start <= byte && byte < range.end.max(range.start + 1))
            .unwrap_or_else(|| entry.rows.len().saturating_sub(1));
        start + usize::from(!matches!(entry.role, Role::Note | Role::Tool)) + row
    }

    pub fn scroll_down(&mut self) {
        self.top = self.top.saturating_add(6);
    }

    pub fn follow(&mut self) {
        self.following = true;
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
        let label = usize::from(!matches!(entry.role, Role::Note | Role::Tool));
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
        if self.width != width {
            let anchor = (!self.following).then(|| self.anchor());
            self.width = width;
            self.measured = 0;
            self.measure();
            if let Some(anchor) = anchor {
                self.top = self.restore(anchor);
            }
        } else {
            self.measure();
        }
        let end = self.total.saturating_sub(usize::from(height));
        if self.following || self.top >= end {
            self.top = end;
            self.follow();
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
                let indent = if matches!(entry.role, Role::Tool | Role::Command(_)) {
                    2
                } else {
                    0
                };
                entry.rows = wrap(&entry.text, width.saturating_sub(indent).max(1));
                entry.code_rows.clear();
            }
            entry.detail_rows = if entry.expanded || entry.shell {
                let indent = if entry.shell { 4 } else { 2 };
                entry.detail.as_deref().map_or_else(Vec::new, |detail| {
                    wrap(detail, width.saturating_sub(indent).max(1))
                })
            } else {
                Vec::new()
            };
            entry.more_output =
                entry.shell && !entry.expanded && entry.detail_rows.len() > SHELL_PREVIEW_ROWS;
            if entry.more_output {
                entry.detail_rows.truncate(SHELL_PREVIEW_ROWS);
            }
            end += entry.rows.len()
                + entry.detail_rows.len()
                + usize::from(entry.more_output)
                + 1
                + usize::from(!matches!(entry.role, Role::Note | Role::Tool));
            entry.end = end;
        }
        self.measured = self.entries.len();
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
