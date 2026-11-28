use std::path::Path;

use agent_client_protocol::v1::schema::SessionInfo;

use super::{
    App, Key,
    picker::{Picker, Source},
    session_workspace_path,
};
use crate::acp::SessionTarget;
use crate::composer::Composer;

pub(super) enum SessionDialog {
    Workspace(WorkspaceInput),
    Delete(DeleteDialog),
}

pub(super) struct DeleteDialog {
    id: String,
    pub title: String,
    pub workspace: String,
    pub progress: DeleteProgress,
}

pub(super) enum DeleteProgress {
    Confirm,
    Pending,
    Failed(String),
}

pub(super) struct WorkspaceInput {
    pub editor: Composer,
    pub suggestions: Vec<String>,
    pub selected: usize,
    pub error: Option<&'static str>,
}

impl WorkspaceInput {
    fn new(workspace: &Path) -> Self {
        let mut editor = Composer::default();
        editor.set_text(&workspace.to_string_lossy());
        Self {
            editor,
            suggestions: Vec::new(),
            selected: 0,
            error: None,
        }
    }

    fn complete(&mut self, workspace: &Path, items: &[SessionInfo], remote: bool, browse: bool) {
        let path = self.editor.text();
        self.suggestions = if remote {
            let query = path.to_lowercase();
            let paths =
                std::iter::once(workspace).chain(items.iter().map(|item| item.cwd.as_path()));
            if !browse && paths.clone().any(|candidate| candidate == Path::new(path)) {
                Vec::new()
            } else {
                let mut paths: Vec<_> = paths
                    .filter_map(Path::to_str)
                    .filter(|candidate| *candidate != path)
                    .filter_map(|candidate| {
                        let rank = super::path_match_rank(&candidate.to_lowercase(), &query)?;
                        Some((rank, candidate.len(), candidate))
                    })
                    .collect();
                paths.sort_unstable();
                paths.dedup();
                paths
                    .into_iter()
                    .take(50)
                    .map(|(_, _, path)| path.to_owned())
                    .collect()
            }
        } else {
            local_directories(workspace, path, browse)
        };
        self.selected = 0;
        self.error = None;
    }

    fn key(&mut self, key: Key, workspace: &Path, items: &[SessionInfo], remote: bool) {
        match key {
            Key::HistoryUp => self.selected = self.selected.saturating_sub(1),
            Key::HistoryDown => {
                self.selected = (self.selected + 1).min(self.suggestions.len().saturating_sub(1))
            }
            Key::Tab | Key::Enter if !self.suggestions.is_empty() => {
                self.editor.set_text(&self.suggestions[self.selected]);
                self.suggestions.clear();
                self.selected = 0;
            }
            Key::Tab => self.complete(workspace, items, remote, true),
            Key::Escape => {
                self.suggestions.clear();
                self.selected = 0;
            }
            Key::Left => self.editor.left(),
            Key::Right => self.editor.right(),
            Key::Home => self.editor.home(),
            Key::End => self.editor.end(),
            Key::Text(ch) if !ch.is_control() => {
                self.editor.insert(ch.encode_utf8(&mut [0; 4]));
                self.complete(workspace, items, remote, false);
            }
            Key::Backspace | Key::Delete => {
                if matches!(key, Key::Backspace) {
                    self.editor.backspace();
                } else {
                    self.editor.delete();
                }
                self.complete(workspace, items, remote, false);
            }
            _ => {}
        }
    }
}

impl App {
    pub(super) fn agents_action_key(&mut self, key: Key) -> bool {
        let blocker = self.agents_new_blocker();
        let Some(Picker {
            source:
                Source::Sessions {
                    all_workspaces,
                    dialog,
                    notice,
                    items,
                    ..
                },
            ..
        }) = &mut self.picker
        else {
            return false;
        };
        if matches!(dialog.as_deref(), Some(SessionDialog::Delete(_))) {
            self.agents_delete_key(key);
            return true;
        }
        if let Some(SessionDialog::Workspace(input)) = dialog.as_deref_mut() {
            if matches!(key, Key::Agents)
                || matches!(key, Key::Escape) && input.suggestions.is_empty()
            {
                *dialog = None;
            } else if let Some(workspace) = self.workspace.as_deref() {
                if matches!(key, Key::Enter) && input.suggestions.is_empty() {
                    let path = input.editor.text();
                    input.error =
                        blocker.or_else(|| workspace_path_error(workspace, path, self.remote));
                    if input.error.is_none() {
                        let cwd = session_workspace_path(workspace, path, self.remote);
                        self.picker = None;
                        self.switch_session(SessionTarget {
                            cwd: Some(cwd),
                            session: None,
                        });
                    }
                } else {
                    input.key(key, workspace, items, self.remote);
                }
            }
            return true;
        }
        match key {
            Key::Delete => {
                self.begin_agents_delete();
                return true;
            }
            Key::NewSession => *notice = None,
            _ => return false,
        }
        if let Some(message) = blocker {
            *notice = Some(message.into());
        } else if *all_workspaces {
            if let Some(workspace) = self.workspace.as_deref() {
                *dialog = Some(Box::new(SessionDialog::Workspace(WorkspaceInput::new(
                    workspace,
                ))));
            }
        } else {
            self.picker = None;
            self.new_session();
        }
        true
    }

    pub(super) fn agents_paste(&mut self, text: &str) {
        if let Some(Picker {
            source:
                Source::Sessions {
                    dialog: Some(dialog),
                    items,
                    ..
                },
            ..
        }) = &mut self.picker
        {
            if let (SessionDialog::Workspace(input), Some(workspace)) =
                (dialog.as_mut(), self.workspace.as_deref())
            {
                let text: String = text.chars().filter(|ch| !ch.is_control()).collect();
                input.editor.insert(&text);
                input.complete(workspace, items, self.remote, false);
            }
        } else {
            self.paste_into_picker(text);
        }
    }

    fn agents_new_blocker(&self) -> Option<&'static str> {
        if !self.ready || self.workspace.is_none() {
            Some("Connect to an agent to create a session")
        } else {
            self.session_switch_blocker()
        }
    }

    fn agents_dialog(&mut self) -> Option<&mut Option<Box<SessionDialog>>> {
        match &mut self.picker.as_mut()?.source {
            Source::Sessions { dialog, .. } => Some(dialog),
            _ => None,
        }
    }

    fn agents_delete_dialog(&mut self) -> Option<&mut DeleteDialog> {
        match self.agents_dialog()?.as_deref_mut()? {
            SessionDialog::Delete(dialog) => Some(dialog),
            SessionDialog::Workspace(_) => None,
        }
    }

    fn begin_agents_delete(&mut self) {
        let Some(picker) = &self.picker else { return };
        let rows = self.picker_rows(picker);
        let Some((row, session)) = rows
            .get(picker.selected)
            .and_then(|row| Some((row, row.session.as_ref()?)))
        else {
            return;
        };
        let blocker = self.session_delete_blocker(&row.value).or_else(|| {
            session
                .attached_elsewhere()
                .then_some("The session is open in another TUI")
        });
        if let Some(message) = blocker {
            if let Some(Picker {
                source: Source::Sessions { notice, .. },
                ..
            }) = &mut self.picker
            {
                *notice = Some(message.into());
            }
        } else if let Some(dialog) = self.agents_dialog() {
            *dialog = Some(Box::new(SessionDialog::Delete(DeleteDialog {
                id: row.value.clone(),
                title: row.label.clone(),
                workspace: session.workspace.clone(),
                progress: DeleteProgress::Confirm,
            })));
        }
    }

    fn agents_delete_key(&mut self, key: Key) {
        let Some(dialog) = self.agents_delete_dialog() else {
            return;
        };
        if matches!(dialog.progress, DeleteProgress::Pending) {
            return;
        }
        match key {
            Key::Escape | Key::Agents => {
                if let Some(dialog) = self.agents_dialog() {
                    *dialog = None;
                }
            }
            Key::Enter => {
                let id = dialog.id.clone();
                let result = self.request_session_delete(&id);
                if let Some(dialog) = self.agents_delete_dialog() {
                    dialog.progress = match result {
                        Ok(()) => DeleteProgress::Pending,
                        Err(error) => DeleteProgress::Failed(error.into()),
                    };
                }
            }
            _ => {}
        }
    }

    pub(super) fn agents_delete_failed(&mut self, id: &str, error: &str) -> bool {
        if let Some(dialog) = self.agents_delete_dialog()
            && dialog.id == id
            && matches!(dialog.progress, DeleteProgress::Pending)
        {
            dialog.progress = DeleteProgress::Failed(error.into());
            return true;
        }
        false
    }

    pub(super) fn agents_delete_interrupted(&mut self) {
        if let Some(dialog) = self.agents_delete_dialog()
            && matches!(dialog.progress, DeleteProgress::Pending)
        {
            dialog.progress =
                DeleteProgress::Failed("Disconnected before deletion was confirmed".into());
        }
    }

    pub(super) fn agents_delete_completed(&mut self, id: &str, active: bool) {
        if self
            .agents_delete_dialog()
            .is_some_and(|dialog| dialog.id == id)
        {
            if active {
                self.picker = None;
            } else if let Some(dialog) = self.agents_dialog() {
                *dialog = None;
            }
        }
        self.remove_agent_session(id);
    }
}

fn workspace_path_error(workspace: &Path, path: &str, remote: bool) -> Option<&'static str> {
    if path.is_empty() {
        return Some("Enter a workspace path");
    }
    #[cfg(not(target_family = "wasm"))]
    if !remote && !workspace.join(path).is_dir() {
        return Some("Workspace directory does not exist");
    }
    #[cfg(target_family = "wasm")]
    let _ = (workspace, remote);
    None
}

#[cfg(not(target_family = "wasm"))]
fn local_directories(workspace: &Path, path: &str, browse: bool) -> Vec<String> {
    let exact = workspace.join(path).is_dir();
    if exact && !browse && !path.is_empty() && !path.ends_with(std::path::is_separator) {
        return Vec::new();
    }
    let (parent, query) = if exact && browse {
        (path, "")
    } else {
        let split = path
            .rfind(std::path::is_separator)
            .map_or(0, |index| index + 1);
        path.split_at(split)
    };
    let query = query.to_lowercase();
    let mut paths: Vec<_> = std::fs::read_dir(workspace.join(parent))
        .into_iter()
        .flatten()
        .filter_map(Result::ok)
        .filter_map(|entry| {
            let name = entry.file_name().into_string().ok()?;
            if name.chars().any(char::is_control)
                || (name.starts_with('.') && !query.starts_with('.'))
            {
                return None;
            }
            let rank = super::path_match_rank(&name.to_lowercase(), &query)?;
            entry.path().is_dir().then_some((rank, name.len(), name))
        })
        .collect();
    if paths.len() > 50 {
        paths.select_nth_unstable(50);
        paths.truncate(50);
    }
    paths.sort_unstable();
    paths
        .into_iter()
        .filter_map(|(_, _, name)| {
            Path::new(parent)
                .join(name)
                .into_os_string()
                .into_string()
                .ok()
        })
        .collect()
}

#[cfg(target_family = "wasm")]
fn local_directories(_: &Path, _: &str, _: bool) -> Vec<String> {
    Vec::new()
}
