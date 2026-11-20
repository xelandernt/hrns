use std::ops::Range;

use ratatui::style::{Color, Style};
use ratatui::text::{Line, Span};
use unicode_segmentation::UnicodeSegmentation;
use unicode_width::UnicodeWidthStr;

#[derive(PartialEq)]
pub enum Role {
    User,
    Agent,
    Thought,
    Tool,
    Note,
    Error,
}

impl Role {
    fn label(&self) -> Option<Line<'static>> {
        let (name, color) = match self {
            Self::User => ("You", Color::Cyan),
            Self::Agent => ("Agent", Color::Green),
            Self::Thought => ("Thinking", Color::Gray),
            Self::Tool => ("Tool", Color::Magenta),
            Self::Error => ("Error", Color::Red),
            Self::Note => return None,
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
    id: Option<String>,
    expanded: bool,
    end: usize,
}

impl Entry {
    fn line(&self, mut row: usize) -> Line<'_> {
        if let Some(heading) = self.role.label() {
            if row == 0 {
                return heading;
            }
            row -= 1;
        }
        if let Some(range) = self.rows.get(row) {
            let text = &self.text[range.clone()];
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
                return Line::from(inline(text));
            }
            return Line::raw(text);
        }
        let detail_row = row - self.rows.len();
        if let Some(detail) = &self.detail
            && let Some(range) = self.detail_rows.get(detail_row)
        {
            return Line::from(vec![
                Span::raw("  "),
                Span::styled(&detail[range.clone()], Style::default().fg(Color::Gray)),
            ]);
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
            id: None,
            expanded: false,
            end: 0,
        });
    }

    pub fn tool(&mut self, id: String, title: String, status: &str, detail: String) {
        let text = format!("{status} · {title}");
        let detail = detail.trim_end().to_owned();
        if let Some(index) = self
            .entries
            .iter()
            .position(|entry| entry.id.as_deref() == Some(id.as_str()))
        {
            let entry = &mut self.entries[index];
            entry.text = text;
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
            id: Some(id),
            expanded: self.expanded_tools,
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
        self.following = true;
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
        let label = usize::from(entry.role != Role::Note);
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
        start + usize::from(entry.role != Role::Note) + row
    }

    pub fn scroll_down(&mut self) {
        self.top = self.top.saturating_add(6);
    }

    pub fn follow(&mut self) {
        self.following = true;
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
        let entry = &self.entries[index];
        let label = usize::from(entry.role != Role::Note);
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

    pub fn visible(&mut self, width: u16, height: u16) -> Vec<Line<'_>> {
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
            self.following = true;
        }

        let mut row = self.top;
        let mut index = self.entries.partition_point(|entry| entry.end <= row);
        let mut lines = Vec::with_capacity(usize::from(height));
        while let Some(entry) = self.entries.get(index) {
            let start = index
                .checked_sub(1)
                .map_or(0, |prev| self.entries[prev].end);
            while row < entry.end {
                lines.push(entry.line(row - start));
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
            let width = usize::from(self.width);
            if matches!(entry.role, Role::Agent | Role::Thought) {
                (entry.rows, entry.code_rows) = wrap_markdown(&entry.text, width);
            } else {
                entry.rows = wrap(&entry.text, width);
                entry.code_rows.clear();
            }
            entry.detail_rows = if entry.expanded {
                entry.detail.as_deref().map_or_else(Vec::new, |detail| {
                    wrap(detail, usize::from(self.width).saturating_sub(2).max(1))
                })
            } else {
                Vec::new()
            };
            end += entry.rows.len()
                + entry.detail_rows.len()
                + 1
                + usize::from(entry.role != Role::Note);
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
