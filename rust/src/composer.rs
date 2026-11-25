use std::borrow::Cow;
use std::ops::Range;

use unicode_segmentation::{GraphemeCursor, UnicodeSegmentation};
use unicode_width::UnicodeWidthStr;

const LARGE_PASTE_THRESHOLD: usize = 1_000;

#[derive(Default)]
pub struct Composer {
    draft: Draft,
    cursor: usize,
    recalled: Option<usize>,
    saved_draft: Draft,
    history: Vec<Draft>,
}

#[derive(Clone, Default, PartialEq)]
struct Draft {
    text: String,
    pastes: Vec<Paste>,
}

#[derive(Clone, PartialEq)]
struct Paste {
    range: Range<usize>,
    text: String,
}

impl Composer {
    pub fn text(&self) -> &str {
        &self.draft.text
    }

    pub fn expanded_text(&self) -> Cow<'_, str> {
        if self.draft.pastes.is_empty() {
            return Cow::Borrowed(self.text());
        }
        let mut expanded = String::new();
        let mut offset = 0;
        for paste in &self.draft.pastes {
            expanded.push_str(&self.text()[offset..paste.range.start]);
            expanded.push_str(&paste.text);
            offset = paste.range.end;
        }
        expanded.push_str(&self.text()[offset..]);
        Cow::Owned(expanded)
    }

    pub fn paste(&mut self, text: &str) {
        let text = if text
            .chars()
            .any(|ch| ch.is_control() && ch != '\n' && ch != '\t')
        {
            let mut normalized = text.replace("\r\n", "\n").replace('\r', "\n");
            normalized.retain(|ch| !ch.is_control() || ch == '\n' || ch == '\t');
            Cow::Owned(normalized)
        } else {
            Cow::Borrowed(text)
        };
        let count = text.chars().count();
        if count <= LARGE_PASTE_THRESHOLD {
            self.insert(&text);
            return;
        }

        let base = format!("[Pasted Content {count} chars]");
        let suffix = self
            .draft
            .pastes
            .iter()
            .filter_map(|paste| {
                let label = &self.text()[paste.range.clone()];
                match label.strip_prefix(&base)? {
                    "" => Some(1),
                    suffix => suffix.strip_prefix(" #")?.parse::<usize>().ok(),
                }
            })
            .max();
        let label = match suffix {
            Some(suffix) => format!("{base} #{}", suffix + 1),
            None => base,
        };
        let start = self.cursor;
        let index = self
            .draft
            .pastes
            .partition_point(|paste| paste.range.start < start);
        self.edit(start..start, &label);
        self.draft.pastes.insert(
            index,
            Paste {
                range: start..start + label.len(),
                text: text.into_owned(),
            },
        );
        self.cursor = start + label.len();
    }

    #[cfg(not(target_family = "wasm"))]
    pub fn overlaps_paste(&self, range: &Range<usize>) -> bool {
        self.draft
            .pastes
            .iter()
            .any(|paste| paste.range.start < range.end && range.start < paste.range.end)
    }

    pub fn cursor(&self) -> usize {
        self.cursor
    }

    pub fn insert(&mut self, text: &str) {
        let mut inserted = String::with_capacity(text.len());
        for ch in text.chars() {
            match ch {
                '\t' => inserted.push_str("    "),
                '\n' => inserted.push(ch),
                ch if !ch.is_control() => inserted.push(ch),
                _ => {}
            }
        }
        self.edit(self.cursor..self.cursor, &inserted);
    }

    pub fn set_text(&mut self, text: &str) {
        self.draft.text.clear();
        self.draft.text.push_str(text);
        self.draft.pastes.clear();
        self.cursor = text.len();
        self.recalled = None;
        self.saved_draft = Draft::default();
    }

    pub fn replace_from(&mut self, start: usize, text: &str) {
        self.replace_range(start..self.text().len(), text);
    }

    pub fn replace_range(&mut self, range: Range<usize>, text: &str) {
        self.edit(range, text);
        self.recalled = None;
    }

    pub fn submit(&mut self) {
        self.cursor = 0;
        self.recalled = None;
        self.saved_draft = Draft::default();
        let draft = std::mem::take(&mut self.draft);
        if !draft.text.trim().is_empty() && self.history.last() != Some(&draft) {
            self.history.push(draft);
        }
    }

    pub fn left(&mut self) {
        self.cursor = self.previous_boundary();
    }

    pub fn right(&mut self) {
        self.cursor = self.next_boundary();
    }

    pub fn backspace(&mut self) {
        let next = self.previous_boundary();
        self.edit(next..self.cursor, "");
    }

    pub fn delete(&mut self) {
        let next = self.next_boundary();
        self.edit(self.cursor..next, "");
    }

    pub fn home(&mut self) {
        self.cursor = self.text()[..self.cursor]
            .rfind('\n')
            .map_or(0, |index| index + 1);
    }

    pub fn end(&mut self) {
        self.cursor = self.text()[self.cursor..]
            .find('\n')
            .map_or(self.text().len(), |index| self.cursor + index);
    }

    pub fn recall(&mut self, previous: bool) {
        let next = if previous {
            match self.recalled {
                Some(index) => index.checked_sub(1).or(Some(0)),
                None => {
                    self.saved_draft.clone_from(&self.draft);
                    self.history.len().checked_sub(1)
                }
            }
        } else {
            let Some(index) = self.recalled else { return };
            (index + 1 < self.history.len()).then_some(index + 1)
        };

        if let Some(index) = next {
            self.draft.clone_from(&self.history[index]);
        } else {
            self.draft = std::mem::take(&mut self.saved_draft);
        }
        self.recalled = next;
        self.cursor = self.text().len();
    }

    pub fn display(&self, width: u16) -> (Vec<Range<usize>>, (u16, u16)) {
        let width = usize::from(width);
        let mut rows = Vec::new();
        let mut row_start = 0;
        let mut row_width = 0;
        let mut cursor = (0, 0);
        for (index, grapheme) in self.text().grapheme_indices(true) {
            if grapheme == "\n" {
                if index == self.cursor {
                    cursor = cell(rows.len(), row_width, width);
                }
                rows.push(row_start..index);
                row_start = index + 1;
                row_width = 0;
                continue;
            }
            let columns = grapheme.width();
            if row_width != 0 && row_width + columns > width {
                rows.push(row_start..index);
                row_start = index;
                row_width = 0;
            }
            if index == self.cursor {
                cursor = cell(rows.len(), row_width, width);
            }
            row_width += columns;
            if index < self.cursor && self.cursor < index + grapheme.len() {
                cursor = cell(rows.len(), row_width, width);
            }
        }
        rows.push(row_start..self.text().len());
        if self.cursor == self.text().len() {
            if row_width >= width {
                rows.push(self.cursor..self.cursor);
                cursor = cell(rows.len() - 1, 0, width);
            } else {
                cursor = cell(rows.len() - 1, row_width, width);
            }
        }
        (rows, cursor)
    }

    fn previous_boundary(&self) -> usize {
        let previous = self
            .draft
            .pastes
            .iter()
            .rev()
            .find(|paste| paste.range.start < self.cursor);
        if let Some(paste) = previous.filter(|paste| self.cursor <= paste.range.end) {
            return paste.range.start;
        }
        let boundary = GraphemeCursor::new(self.cursor, self.text().len(), true)
            .prev_boundary(self.text(), 0)
            .expect("the full text is available")
            .unwrap_or(0);
        previous.map_or(boundary, |paste| boundary.max(paste.range.end))
    }

    fn next_boundary(&self) -> usize {
        let next = self
            .draft
            .pastes
            .iter()
            .find(|paste| self.cursor < paste.range.end);
        if let Some(paste) = next.filter(|paste| paste.range.start <= self.cursor) {
            return paste.range.end;
        }
        let boundary = GraphemeCursor::new(self.cursor, self.text().len(), true)
            .next_boundary(self.text(), 0)
            .expect("the full text is available")
            .unwrap_or(self.text().len());
        next.map_or(boundary, |paste| boundary.min(paste.range.start))
    }

    fn edit(&mut self, mut range: Range<usize>, text: &str) {
        let pastes = &mut self.draft.pastes;
        let first = pastes.partition_point(|paste| paste.range.end <= range.start);
        let last = pastes.partition_point(|paste| paste.range.start < range.end);
        if first < last {
            range.start = range.start.min(pastes[first].range.start);
            range.end = range.end.max(pastes[last - 1].range.end);
            pastes.drain(first..last);
        }
        for paste in &mut pastes[first..] {
            let shift = |offset| offset - range.len() + text.len();
            paste.range = shift(paste.range.start)..shift(paste.range.end);
        }
        self.cursor = range.start + text.len();
        self.draft.text.replace_range(range, text);
        self.snap_cursor_to_boundary();
    }

    fn snap_cursor_to_boundary(&mut self) {
        // An edit can join a grapheme across the cursor.
        if self
            .draft
            .pastes
            .iter()
            .any(|paste| self.cursor == paste.range.start || self.cursor == paste.range.end)
        {
            return;
        }
        let mut cursor = GraphemeCursor::new(self.cursor, self.text().len(), true);
        if !cursor
            .is_boundary(self.text(), 0)
            .expect("the full text is available")
        {
            self.cursor = self.next_boundary();
        }
    }
}

fn cell(row: usize, column: usize, width: usize) -> (u16, u16) {
    (
        u16::try_from(row).unwrap_or(u16::MAX),
        u16::try_from(column.min(width.saturating_sub(1))).unwrap_or(u16::MAX),
    )
}

pub(crate) fn prompt_tokens(text: &str) -> impl Iterator<Item = Range<usize>> + '_ {
    let mut tail = text;
    std::iter::from_fn(move || {
        tail = tail.trim_start_matches(char::is_whitespace);
        if tail.is_empty() {
            return None;
        }
        let start = text.len() - tail.len();
        let length = quoted_path_token_len(tail)
            .unwrap_or_else(|| tail.find(char::is_whitespace).unwrap_or(tail.len()));
        tail = &tail[length..];
        Some(start..start + length)
    })
}

fn quoted_path_token_len(token: &str) -> Option<usize> {
    let path = token
        .strip_prefix('@')
        .filter(|path| path.starts_with('"'))?;
    let mut values = serde_json::Deserializer::from_str(path).into_iter::<serde::de::IgnoredAny>();
    values.next()?.ok()?;
    let end = values.byte_offset();
    let rest = &path[end..];
    (rest.is_empty() || rest.starts_with(char::is_whitespace)).then_some(end + 1)
}
