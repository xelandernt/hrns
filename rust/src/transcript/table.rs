//! Layout for GitHub Flavored Markdown tables in the transcript.

use std::ops::Range;

use pulldown_cmark::{Alignment, Event, Options, Parser, Tag, TagEnd};
use ratatui::style::Stylize;
use ratatui::text::{Line, Span};
use unicode_width::UnicodeWidthStr;

use super::wrap;

pub(super) struct Table {
    pub source: Range<usize>,
    alignments: Vec<Alignment>,
    rows: Vec<TableRow>,
}

struct TableRow {
    source: Range<usize>,
    cells: Vec<String>,
}

pub(super) fn parse(text: &str) -> Vec<Table> {
    if !text.contains('|') {
        return Vec::new();
    }
    let mut tables = Vec::new();
    let mut table: Option<Table> = None;
    let mut depth = 0;
    for (event, source) in Parser::new_ext(text, Options::ENABLE_TABLES).into_offset_iter() {
        match &event {
            Event::Start(_) => depth += 1,
            Event::End(_) => depth -= 1,
            _ => {}
        }
        if let Event::Start(Tag::Table(alignments)) = event {
            if depth == 1 {
                table = Some(Table {
                    source,
                    alignments,
                    rows: Vec::new(),
                });
            }
            continue;
        }
        let Some(current) = &mut table else { continue };
        let row = current.rows.last_mut();
        match event {
            Event::Start(Tag::TableHead | Tag::TableRow) => {
                current.rows.push(TableRow {
                    source,
                    cells: Vec::new(),
                });
            }
            Event::Start(Tag::TableCell) => {
                if let Some(row) = row {
                    row.cells.push(String::new());
                }
            }
            Event::Text(value) | Event::Code(value) => {
                if let Some(cell) = row.and_then(|row| row.cells.last_mut()) {
                    cell.push_str(&value);
                }
            }
            Event::SoftBreak | Event::HardBreak => {
                if let Some(cell) = row.and_then(|row| row.cells.last_mut()) {
                    cell.push('\n');
                }
            }
            Event::End(TagEnd::Table) => {
                if let Some(current) = table.take() {
                    tables.push(current);
                }
            }
            _ => {}
        }
    }
    tables
}

impl Table {
    pub fn lines(&self, width: usize) -> Vec<(Range<usize>, Line<'static>)> {
        let count = self.alignments.len();
        let padding = usize::from(width > count * 5);
        let budget = width.saturating_sub(count * (2 * padding + 1) + 1);
        if count == 0 || budget < count * 2 {
            return self.stacked(width);
        }

        let desired: Vec<_> = (0..count)
            .map(|column| {
                self.rows
                    .iter()
                    .filter_map(|row| row.cells.get(column))
                    .flat_map(|cell| cell.lines())
                    .map(UnicodeWidthStr::width)
                    .max()
                    .unwrap_or(0)
                    .max(2)
            })
            .collect();
        let mut order: Vec<_> = (0..count).collect();
        order.sort_by_key(|&index| desired[index]);
        let mut columns = vec![0; count];
        let mut remaining = budget;
        for (index, column) in order.into_iter().enumerate() {
            let assigned = desired[column].min(remaining / (count - index));
            columns[column] = assigned;
            remaining -= assigned;
        }

        let mut lines = Vec::new();
        for (index, row) in self.rows.iter().enumerate() {
            let rule = if index == 0 {
                ['┌', '┬', '┐']
            } else {
                ['├', '┼', '┤']
            };
            lines.push((row.source.clone(), border(&columns, padding, rule)));
            let wrapped: Vec<_> = columns
                .iter()
                .enumerate()
                .map(|(column, &width)| {
                    let value = row.cells.get(column).map_or("", String::as_str);
                    wrap_cell(value, width)
                })
                .collect();
            let height = wrapped.iter().map(Vec::len).max().unwrap_or(0);
            for line in 0..height {
                let mut spans = Vec::with_capacity(columns.len() * 2 + 1);
                spans.push("│".dim());
                for (column, &width) in columns.iter().enumerate() {
                    let text = wrapped[column].get(line).map_or("", String::as_str);
                    let space = width.saturating_sub(text.width());
                    let left = match self.alignments[column] {
                        Alignment::Right => space,
                        Alignment::Center => space / 2,
                        _ => 0,
                    };
                    let left_pad = left + padding;
                    let right_pad = space - left + padding;
                    let mut cell = String::with_capacity(left_pad + text.len() + right_pad);
                    cell.extend(std::iter::repeat_n(' ', left_pad));
                    cell.push_str(text);
                    cell.extend(std::iter::repeat_n(' ', right_pad));
                    spans.push(if index == 0 {
                        cell.bold()
                    } else {
                        Span::raw(cell)
                    });
                    spans.push("│".dim());
                }
                lines.push((row.source.clone(), Line::from(spans)));
            }
        }
        if let Some(last) = self.rows.last() {
            lines.push((
                last.source.clone(),
                border(&columns, padding, ['└', '┴', '┘']),
            ));
        }
        lines
    }

    fn stacked(&self, width: usize) -> Vec<(Range<usize>, Line<'static>)> {
        let Some((header, rows)) = self.rows.split_first() else {
            return Vec::new();
        };
        if rows.is_empty() {
            return wrap_cell(&header.cells.join(" · "), width)
                .into_iter()
                .map(|line| (header.source.clone(), Line::from(line.bold())))
                .collect();
        }
        let mut lines = Vec::new();
        for (index, row) in rows.iter().enumerate() {
            if index > 0 {
                lines.push((row.source.clone(), Line::default()));
            }
            for (key, value) in header.cells.iter().zip(&row.cells) {
                for line in wrap_cell(&format!("{key}: {value}"), width) {
                    lines.push((row.source.clone(), Line::raw(line)));
                }
            }
        }
        lines
    }
}

fn border(columns: &[usize], padding: usize, [left, join, right]: [char; 3]) -> Line<'static> {
    let dashes: usize = columns.iter().map(|width| width + 2 * padding).sum();
    let mut output = String::with_capacity(
        left.len_utf8()
            + right.len_utf8()
            + columns.len().saturating_sub(1) * join.len_utf8()
            + dashes * '─'.len_utf8(),
    );
    output.push(left);
    for (index, width) in columns.iter().enumerate() {
        if index > 0 {
            output.push(join);
        }
        output.extend(std::iter::repeat_n('─', width + 2 * padding));
    }
    output.push(right);
    Line::from(output.dim())
}

fn wrap_cell(text: &str, width: usize) -> Vec<String> {
    let mut rows = Vec::new();
    for line in text.split('\n') {
        let mut segment = String::new();
        let mut columns = 0;
        for word in line.split_whitespace() {
            let word_width = word.width();
            if columns > 0 && columns + 1 + word_width > width {
                rows.push(std::mem::take(&mut segment));
                columns = 0;
            }
            if word_width > width {
                let mut pieces = wrap(word, width);
                if let Some(last) = pieces.pop() {
                    rows.extend(pieces.into_iter().map(|range| word[range].to_owned()));
                    segment.push_str(&word[last]);
                    columns = segment.width();
                }
                continue;
            }
            if columns > 0 {
                segment.push(' ');
                columns += 1;
            }
            segment.push_str(word);
            columns += word_width;
        }
        if !segment.is_empty() || line.trim().is_empty() {
            rows.push(segment);
        }
    }
    rows
}
