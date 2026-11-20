use std::ops::Range;

use ratatui::style::{Color, Style};
use ratatui::text::Line;
use unicode_segmentation::UnicodeSegmentation;
use unicode_width::UnicodeWidthStr;

#[derive(PartialEq)]
pub enum Role {
    User,
    Agent,
    Thought,
    Note,
    Error,
}

impl Role {
    fn label(&self) -> Option<Line<'static>> {
        let (name, color) = match self {
            Self::User => ("You", Color::Cyan),
            Self::Agent => ("Agent", Color::Green),
            Self::Thought => ("Thinking", Color::Gray),
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
        self.rows
            .get(row)
            .map_or_else(Line::default, |range| Line::raw(&self.text[range.clone()]))
    }
}

#[derive(Default)]
pub struct Transcript {
    entries: Vec<Entry>,
    measured: usize,
    width: u16,
    total: usize,
    top: usize,
    following: bool,
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
            end: 0,
        });
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
        let Some(index) = self
            .entries
            .iter()
            .rposition(|entry| entry.text.to_lowercase().contains(&query))
        else {
            return false;
        };
        let entry = &self.entries[index];
        let row = entry
            .rows
            .iter()
            .position(|range| entry.text[range.clone()].to_lowercase().contains(&query))
            .map(|row| row + usize::from(entry.role != Role::Note))
            .unwrap_or(0);
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
            self.width = width;
            self.measured = 0;
        }
        self.measure();
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
            entry.rows.clear();
            let mut line_start = 0;
            let mut columns = 0;
            for (index, grapheme) in entry.text.grapheme_indices(true) {
                if grapheme == "\n" {
                    entry.rows.push(line_start..index);
                    line_start = index + 1;
                    columns = 0;
                    continue;
                }
                let next = grapheme.width();
                if columns != 0 && columns + next > usize::from(self.width) {
                    entry.rows.push(line_start..index);
                    line_start = index;
                    columns = 0;
                }
                columns += next;
            }
            if line_start < entry.text.len() {
                entry.rows.push(line_start..entry.text.len());
            }
            end += entry.rows.len() + 1 + usize::from(entry.role != Role::Note);
            entry.end = end;
        }
        self.measured = self.entries.len();
        self.total = end;
    }
}
