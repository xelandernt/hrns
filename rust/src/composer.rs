use std::ops::Range;

use unicode_segmentation::{GraphemeCursor, UnicodeSegmentation};
use unicode_width::UnicodeWidthStr;

#[derive(Default)]
pub struct Composer {
    text: String,
    cursor: usize,
    recalled: Option<usize>,
    saved_draft: String,
    history: Vec<String>,
}

impl Composer {
    pub fn text(&self) -> &str {
        &self.text
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
        self.text.insert_str(self.cursor, &inserted);
        self.cursor += inserted.len();
        self.snap_cursor_to_boundary();
    }

    pub fn set_text(&mut self, text: &str) {
        self.text.clear();
        self.text.push_str(text);
        self.cursor = self.text.len();
        self.recalled = None;
        self.saved_draft.clear();
    }

    pub fn replace_from(&mut self, start: usize, text: &str) {
        self.replace_range(start..self.text.len(), text);
    }

    pub fn replace_range(&mut self, range: Range<usize>, text: &str) {
        self.cursor = range.start + text.len();
        self.text.replace_range(range, text);
        self.snap_cursor_to_boundary();
        self.recalled = None;
    }

    pub fn submit(&mut self) -> String {
        self.cursor = 0;
        self.recalled = None;
        self.saved_draft.clear();
        let text = std::mem::take(&mut self.text);
        if !text.trim().is_empty() && self.history.last() != Some(&text) {
            self.history.push(text.clone());
        }
        text
    }

    pub fn left(&mut self) {
        self.cursor = self.previous_boundary();
    }

    pub fn right(&mut self) {
        self.cursor = self.next_boundary();
    }

    pub fn backspace(&mut self) {
        let next = self.previous_boundary();
        self.text.drain(next..self.cursor);
        self.cursor = next;
        self.snap_cursor_to_boundary();
    }

    pub fn delete(&mut self) {
        let next = self.next_boundary();
        self.text.drain(self.cursor..next);
        self.snap_cursor_to_boundary();
    }

    pub fn home(&mut self) {
        self.cursor = self.text[..self.cursor]
            .rfind('\n')
            .map_or(0, |index| index + 1);
    }

    pub fn end(&mut self) {
        self.cursor = self.text[self.cursor..]
            .find('\n')
            .map_or(self.text.len(), |index| self.cursor + index);
    }

    pub fn recall(&mut self, previous: bool) {
        let next = if previous {
            match self.recalled {
                Some(index) => index.checked_sub(1).or(Some(0)),
                None => {
                    self.saved_draft.clone_from(&self.text);
                    self.history.len().checked_sub(1)
                }
            }
        } else {
            let Some(index) = self.recalled else { return };
            (index + 1 < self.history.len()).then_some(index + 1)
        };

        if let Some(index) = next {
            self.text.clone_from(&self.history[index]);
        } else {
            self.text.clone_from(&self.saved_draft);
        }
        self.recalled = next;
        self.cursor = self.text.len();
    }

    pub fn display(&self, width: u16) -> (Vec<Range<usize>>, (u16, u16)) {
        let width = usize::from(width);
        let mut rows = Vec::new();
        let mut row_start = 0;
        let mut row_width = 0;
        let mut cursor = (0, 0);
        for (index, grapheme) in self.text.grapheme_indices(true) {
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
        }
        rows.push(row_start..self.text.len());
        if self.cursor == self.text.len() {
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
        GraphemeCursor::new(self.cursor, self.text.len(), true)
            .prev_boundary(&self.text, 0)
            .expect("the full text is available")
            .unwrap_or(0)
    }

    fn next_boundary(&self) -> usize {
        GraphemeCursor::new(self.cursor, self.text.len(), true)
            .next_boundary(&self.text, 0)
            .expect("the full text is available")
            .unwrap_or(self.text.len())
    }

    fn snap_cursor_to_boundary(&mut self) {
        // An edit can join a grapheme across the cursor.
        let mut cursor = GraphemeCursor::new(self.cursor, self.text.len(), true);
        if !cursor
            .is_boundary(&self.text, 0)
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
