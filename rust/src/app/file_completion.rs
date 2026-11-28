//! Index the active local workspace while the composer accepts a file query.

use std::borrow::Cow;
use std::ops::Range;
use std::path::PathBuf;

use futures::channel::oneshot;
use ignore::WalkBuilder;

use super::{App, Completion, FileCompletion, Suggestion, path_match_rank, slash_command};
use crate::composer::prompt_tokens;

const MAX_MATCHES: usize = 50;

struct File {
    path: String,
    lowercase: String,
}

enum Index {
    Loading(oneshot::Receiver<Option<Vec<File>>>),
    Ready(Vec<File>),
    Unavailable,
}

#[derive(Default)]
pub(super) struct Search {
    index: Option<Index>,
    dismissed: Option<(Range<usize>, String)>,
    pub(super) highlights: Vec<Range<usize>>,
}

impl Search {
    pub(super) fn dismiss(&mut self, range: Range<usize>, draft: &str) {
        let token = draft[range.clone()].to_owned();
        self.dismissed = Some((range, token));
    }

    fn poll(&mut self) -> bool {
        let Some(Index::Loading(receiver)) = &mut self.index else {
            return false;
        };
        self.index = Some(match receiver.try_recv() {
            Ok(None) => return false,
            Ok(Some(Some(files))) => Index::Ready(files),
            Ok(Some(None)) | Err(_) => Index::Unavailable,
        });
        true
    }
}

impl App {
    pub(crate) fn poll_file_completion(&mut self) -> bool {
        let changed = self.file_search.poll();
        if changed {
            self.refresh_completion();
        }
        changed
    }

    pub(super) fn file_completion(&mut self) -> Option<Completion> {
        let text = self.draft.text();
        let workspace = self.workspace.as_ref().filter(|_| {
            self.ready
                && !self.remote
                && slash_command(text).is_none_or(|command| command == "/queue")
        });
        let references: Vec<_> = file_references(text)
            .filter(|reference| !self.draft.overlaps_paste(&reference.range))
            .collect();
        let Some(workspace) = workspace.filter(|_| !references.is_empty()) else {
            self.file_search = Search::default();
            return None;
        };
        let index = self
            .file_search
            .index
            .get_or_insert_with(|| index(workspace.clone()));

        self.file_search.highlights.clear();
        if let Index::Ready(files) = index {
            for reference in &references {
                let path = reference.path.strip_prefix("./").unwrap_or(&reference.path);
                if files
                    .binary_search_by(|file| file.path.as_str().cmp(path))
                    .is_ok()
                {
                    self.file_search.highlights.push(reference.range.clone());
                }
            }
        }

        let cursor = self.draft.cursor();
        let range = references.into_iter().find_map(|reference| {
            (!reference.quoted && reference.range.start < cursor && cursor <= reference.range.end)
                .then_some(reference.range)
        })?;
        if self
            .file_search
            .dismissed
            .as_ref()
            .is_some_and(|(previous, token)| previous == &range && token == &text[range.clone()])
        {
            return None;
        }
        self.file_search.dismissed = None;
        let query = &text[range.start + 1..cursor];
        let items = match index {
            Index::Ready(files) => matches(files, query),
            _ => Vec::new(),
        };
        let status = match index {
            Index::Loading(_) => Some("Searching workspace…"),
            Index::Unavailable => Some("Workspace files unavailable"),
            Index::Ready(_) if items.is_empty() => Some("No matching files"),
            Index::Ready(_) => None,
        };
        let selected = self
            .completion
            .as_ref()
            .filter(|completion| {
                completion
                    .file
                    .as_ref()
                    .is_some_and(|file| file.range == range)
                    && completion
                        .items
                        .iter()
                        .map(|item| &item.insert)
                        .eq(items.iter().map(|item| &item.insert))
            })
            .map_or(0, |completion| completion.selected);
        Some(Completion {
            items,
            selected,
            file: Some(FileCompletion { range, status }),
        })
    }

    pub(super) fn insert_file_reference(&mut self, range: Range<usize>, path: &str) {
        let remaining = &self.draft.text()[range.end..];
        let advance = remaining.starts_with(' ');
        let mut mention = if path
            .chars()
            .any(|ch| ch.is_whitespace() || matches!(ch, '"' | '\\'))
        {
            format!("@{}", serde_json::Value::String(path.to_owned()))
        } else {
            format!("@{path}")
        };
        let inserted = range.start..range.start + mention.len();
        if remaining.is_empty() {
            mention.push(' ');
        }
        self.draft.replace_range(range, &mention);
        if advance {
            self.draft.right();
        }
        self.file_search.dismiss(inserted, self.draft.text());
    }
}

struct FileReference<'a> {
    range: Range<usize>,
    path: Cow<'a, str>,
    quoted: bool,
}

fn file_references(draft: &str) -> impl Iterator<Item = FileReference<'_>> {
    prompt_tokens(draft).filter_map(|range| {
        let raw = draft[range.clone()].strip_prefix('@')?;
        Some(
            if raw.starts_with('"')
                && let Ok(path) = serde_json::from_str(raw)
            {
                FileReference {
                    range,
                    path: Cow::Owned(path),
                    quoted: true,
                }
            } else {
                FileReference {
                    range,
                    path: Cow::Borrowed(raw),
                    quoted: false,
                }
            },
        )
    })
}

fn index(root: PathBuf) -> Index {
    let (sender, receiver) = oneshot::channel();
    let _ = std::thread::Builder::new()
        .name("workspace-files".into())
        .spawn(move || {
            let files = root.is_dir().then(|| {
                let mut files: Vec<_> = WalkBuilder::new(&root)
                    .build()
                    .take_while(|_| !sender.is_canceled())
                    .filter_map(Result::ok)
                    .filter(|entry| entry.file_type().is_some_and(|kind| kind.is_file()))
                    .filter_map(|entry| {
                        let path = entry.path().strip_prefix(&root).ok()?.to_str()?;
                        if path.chars().any(char::is_control) {
                            return None;
                        }
                        let path = if cfg!(windows) {
                            path.replace('\\', "/")
                        } else {
                            path.to_owned()
                        };
                        Some(File {
                            lowercase: path.to_lowercase(),
                            path,
                        })
                    })
                    .collect();
                files.sort_unstable_by(|a, b| a.path.cmp(&b.path));
                files
            });
            let _ = sender.send(files);
        });
    Index::Loading(receiver)
}

fn matches(files: &[File], query: &str) -> Vec<Suggestion> {
    let query = query.strip_prefix("./").unwrap_or(query).to_lowercase();
    let mut paths: Vec<_> = files
        .iter()
        .filter_map(|file| {
            let rank = path_match_rank(&file.lowercase, &query)?;
            Some((rank, file.path.len(), file.path.as_str()))
        })
        .collect();
    if paths.len() > MAX_MATCHES {
        paths.select_nth_unstable(MAX_MATCHES);
        paths.truncate(MAX_MATCHES);
    }
    paths.sort_unstable();
    paths
        .into_iter()
        .map(|(_, _, path)| Suggestion {
            insert: path.to_owned(),
            description: String::new(),
            kind: super::SuggestionKind::File,
        })
        .collect()
}
