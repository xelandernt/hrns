use std::collections::BTreeMap;
use std::ops::Range;

use ratatui::style::{Color, Stylize};
use ratatui::text::{Line, Span};
use unicode_segmentation::UnicodeSegmentation;
use unicode_width::UnicodeWidthStr;

use crate::theme::Palette;

use super::Transcript;
use super::actions::ActionRows;
use super::entry::{ManualApproval, Role};
use super::table;

pub(super) enum AnchorRow {
    Start,
    Spacer,
    Text { byte: usize, line_offset: usize },
    Detail(usize),
    Approval,
}

impl Transcript {
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

    pub fn begin_replay(&mut self) {
        self.finish_actions();
        self.replay_from = Some(self.entries.len());
    }

    pub fn replaying(&self) -> bool {
        self.replay_from.is_some()
    }

    pub fn cancel_replay(&mut self) -> bool {
        let Some(from) = self.replay_from.take() else {
            return false;
        };
        self.trim(from);
        true
    }

    pub fn retry_replay(&mut self) {
        if let Some(from) = self.replay_from {
            self.trim(from);
        }
    }

    /// Discards everything from `from` onward, keeping the prefix intact.
    pub(super) fn trim(&mut self, from: usize) {
        self.entries.truncate(from.min(self.entries.len()));
        self.actions.trim(from);
        self.reset_layout();
    }

    pub fn commit_replay(&mut self) {
        if let Some(from) = self.replay_from.take() {
            self.finish_actions();
            self.entries.drain(..from.min(self.entries.len()));
            self.actions.discard_prefix(from);
            self.reset_layout();
            self.follow();
        }
    }

    pub(super) fn reset_layout(&mut self) {
        self.measured = 0;
        self.laid_out = 0;
        self.total = 0;
        self.top = 0;
        self.selected_prompt = None;
    }

    pub(super) fn anchor(&self) -> Option<(usize, AnchorRow)> {
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

    pub(super) fn restore(&self, (index, anchor): (usize, AnchorRow)) -> usize {
        let entry = &self.entries[index];
        let start = self.entry_start(index);
        if entry.end == start {
            return start;
        }
        if !entry.action_visible {
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

    pub(super) fn entry_start(&self, index: usize) -> usize {
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
                    if entry.role == Role::Thought && entry.text.trim().is_empty() {
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
        let mut reflow = false;
        if self.entries[index].role.is_action()
            && let Some(first) = self.actions.reveal(index)
        {
            self.measured = self.measured.min(first);
            reflow = true;
        }
        let reveal = !in_text || self.entries[index].role == Role::Thought;
        if reveal && !self.entries[index].expanded {
            self.entries[index].expanded = true;
            self.measured = self.measured.min(index);
            reflow = true;
        }
        if reflow {
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
        animation_tick: u128,
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
        let animation_tick = self.animating_actions().then_some(animation_tick);
        let compact = self.actions.compact();
        visible.lines.reserve(usize::from(height));
        while let Some(entry) = self.entries.get(index) {
            let start = self.entry_start(index);
            let border = entry.role.notice_color(palette);
            let content_rows = entry.heading_rows() + entry.rows.len();
            while row < entry.end {
                let entry_row = row - start;
                let is_action_heading = entry_row == 0 && entry.action_heading;
                let mut line =
                    if is_action_heading && let Some(heading) = self.actions.layout(index).0 {
                        heading.line(palette, animation_tick)
                    } else {
                        entry.line(
                            entry_row.saturating_sub(usize::from(entry.action_heading)),
                            palette,
                            animation_tick,
                            is_command,
                        )
                    };
                let indent = entry
                    .role
                    .indent(usize::from(width), compact && !is_action_heading);
                if !line.spans.is_empty() {
                    if !is_action_heading && entry.preview_prefix_width(usize::from(width)) > 0 {
                        let prefix = if entry_row == usize::from(entry.action_heading) {
                            "↳ "
                        } else {
                            "  "
                        };
                        line.spans.insert(0, prefix.fg(palette.border));
                    }
                    if !indent.is_empty() {
                        line.spans.insert(0, Span::raw(indent));
                    }
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
            if self
                .entries
                .get(index)
                .is_some_and(|entry| entry.end <= row)
            {
                index += self.entries[index..].partition_point(|entry| entry.end <= row);
            }
        }
        visible
    }

    fn measure(&mut self) {
        let compact = self.actions.compact();
        let mut end = self
            .measured
            .checked_sub(1)
            .map_or(0, |prev| self.entries[prev].end);
        for (index, entry) in self.entries.iter_mut().enumerate().skip(self.measured) {
            if entry.role == Role::Thought && entry.text.trim().is_empty() {
                entry.end = end;
                continue;
            }
            if entry.role.is_action() {
                let (heading, rows) = self.actions.layout(index);
                entry.action_heading = heading.is_some();
                entry.action_preview = rows == ActionRows::Preview;
                entry.action_visible =
                    rows != ActionRows::Hidden || entry.approval == Some(ManualApproval::Pending);
                if !entry.action_visible {
                    end += usize::from(entry.action_heading);
                    entry.end = end;
                    continue;
                }
            }
            let viewport_width = usize::from(self.width);
            let width = viewport_width.saturating_sub(
                entry.role.indent(viewport_width, compact).len()
                    + entry.preview_prefix_width(viewport_width),
            );
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
                    Role::Compaction => 4 + if entry.has_detail() { 16 } else { 0 },
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
pub(super) struct MarkdownLayout {
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
