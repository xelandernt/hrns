mod commands;
mod render;

use std::collections::VecDeque;
use std::path::PathBuf;

use agent_client_protocol::v1::schema::SessionConfigOption;
use futures::channel::{mpsc, oneshot};

use crate::acp::{Action, Event, ExecutionState, ExecutionStatus, SessionTarget};
use crate::attachments::{Attachment, MAX_PROMPT_BYTES, Prompt};
use crate::composer::Composer;
use crate::preferences::Preferences;
use crate::skills::{self, Skill};
use crate::transcript::{Role, Transcript};

pub enum LocalEffect {
    Copy(String),
    Attach(String),
    Reconnect,
    Switch(SessionTarget),
    SavePreferences(Preferences),
}

#[derive(Clone, Copy)]
pub enum Key {
    Text(char),
    Enter,
    Newline,
    Backspace,
    Delete,
    Left,
    Right,
    Home,
    End,
    HistoryUp,
    HistoryDown,
    ScrollUp,
    ScrollDown,
    Tab,
    ToggleTools,
    ToggleSidebar,
    Reconnect,
    Escape,
}

struct Suggestion {
    insert: String,
    description: String,
}

struct Completion {
    items: Vec<Suggestion>,
    selected: usize,
}

const COMMANDS: &[(&str, &str)] = &[
    ("/help", "List commands"),
    ("/sessions", "List saved sessions"),
    ("/sessions next", "Next session page"),
    ("/new", "Start a new session"),
    ("/load", "Load a session by id"),
    ("/close", "Close a session"),
    ("/delete", "Delete a saved session"),
    ("/models", "Show the session models"),
    ("/model", "Select a model"),
    ("/effort", "Show the reasoning effort"),
    ("/todo", "Show the current plan"),
    ("/find", "Find transcript text"),
    ("/copy", "Copy the latest reply"),
    ("/reconnect", "Reconnect the transport"),
    ("/queue", "Queue a follow-up"),
    ("/resume-queue", "Resume the local queue"),
    ("/clear-queue", "Clear the local queue"),
    ("/attach", "Stage a file"),
    ("/detach", "Remove a staged file"),
    ("/skills", "List available skills"),
    ("/sidebar", "Toggle the activity sidebar"),
    ("/thoughts", "Show or hide reasoning"),
    ("/agent", "Run an advertised agent command"),
];

type Approval = (Vec<String>, oneshot::Sender<Option<usize>>);

pub struct App {
    destination: String,
    workspace: Option<String>,
    session: Option<String>,
    status: &'static str,
    ready: bool,
    turns: usize,
    execution: Option<ExecutionState>,
    queue: VecDeque<Prompt>,
    attachments: Vec<Attachment>,
    attachment_reads: usize,
    attachments_supported: bool,
    load_supported: bool,
    close_supported: bool,
    delete_supported: bool,
    loaded: bool,
    replay_from: Option<usize>,
    queue_paused: bool,
    draft: Composer,
    transcript: Transcript,
    approvals: VecDeque<Approval>,
    local_effect: Option<LocalEffect>,
    options: Vec<SessionConfigOption>,
    commands: Vec<(String, String)>,
    skills: Vec<Skill>,
    completion: Option<Completion>,
    plan: Vec<String>,
    usage: Option<(u64, u64)>,
    preferences: Preferences,
    activity: VecDeque<String>,
    actions: mpsc::UnboundedSender<Action>,
}

impl App {
    pub fn new(
        destination: String,
        actions: mpsc::UnboundedSender<Action>,
        preferences: Preferences,
    ) -> Self {
        let mut transcript = Transcript::new();
        transcript.set_tools_expanded(preferences.expand_tools);
        Self {
            destination,
            workspace: None,
            session: None,
            status: "Connecting",
            ready: false,
            turns: 0,
            execution: None,
            queue: VecDeque::new(),
            attachments: Vec::new(),
            attachment_reads: 0,
            attachments_supported: false,
            load_supported: false,
            close_supported: false,
            delete_supported: false,
            loaded: false,
            replay_from: None,
            queue_paused: false,
            draft: Composer::default(),
            transcript,
            approvals: VecDeque::new(),
            local_effect: None,
            options: Vec::new(),
            commands: Vec::new(),
            skills: Vec::new(),
            completion: None,
            plan: Vec::new(),
            usage: None,
            preferences,
            activity: VecDeque::new(),
            actions,
        }
    }

    pub fn event(&mut self, event: Event) {
        match event {
            Event::Ready {
                session,
                workspace,
                attachments_supported,
                loaded,
                load_supported,
                close_supported,
                delete_supported,
            } => {
                if let Some(from) = self.replay_from.take() {
                    self.transcript.replace(from);
                }
                self.attachments_supported = attachments_supported;
                self.load_supported = load_supported;
                self.close_supported = close_supported;
                self.delete_supported = delete_supported;
                self.loaded = loaded;
                self.workspace = Some(workspace.display().to_string());
                self.session = Some(session);
                self.ready = true;
                self.status = "Connected";
                self.activity.push_back("connected".into());
                self.trim_activity();
                self.update_execution();
            }
            Event::Execution(state) => {
                let matches = self
                    .session
                    .as_ref()
                    .is_none_or(|id| *id == state.session_id);
                let newer = self.execution.as_ref().is_none_or(|old| {
                    old.session_id != state.session_id || old.revision < state.revision
                });
                if matches && newer {
                    self.execution = Some(state);
                    self.update_execution();
                }
            }
            Event::Options(options) => self.options = options,
            Event::AgentCommands(commands) => self.commands = commands,
            Event::Skills(skills) => {
                self.skills = skills;
                self.refresh_completion();
            }
            Event::LoadingAttachment => self.attachment_reads += 1,
            Event::Attachment(attachment) => {
                self.attachment_reads = self.attachment_reads.saturating_sub(1);
                let attachment = match attachment {
                    Ok(attachment) => attachment,
                    Err(error) => {
                        self.transcript.push(Role::Error, error);
                        return;
                    }
                };
                let staged: usize = self.attachments.iter().map(|item| item.size).sum();
                if staged.saturating_add(attachment.size) > MAX_PROMPT_BYTES {
                    self.transcript.push(
                        Role::Error,
                        "Attachments exceed the ACP prompt limit".into(),
                    );
                } else if !self
                    .attachments
                    .iter()
                    .any(|item| item.name == attachment.name)
                {
                    self.transcript
                        .push(Role::Note, format!("Attached {}", attachment.name));
                    self.attachments.push(*attachment);
                } else {
                    self.transcript.push(
                        Role::Note,
                        format!("{} is already attached", attachment.name),
                    );
                }
            }
            Event::Plan(plan) => self.plan = plan,
            Event::Usage { used, size } => self.usage = Some((used, size)),
            Event::SessionClosed(id) => {
                self.transcript.push(
                    Role::Note,
                    format!("Closed session {id}; the agent keeps its history"),
                );
                if self.session.as_deref() == Some(&id) {
                    self.new_session();
                }
            }
            Event::SessionDeleted(id) => {
                self.transcript
                    .push(Role::Note, format!("Deleted session {id}"));
                if self.session.as_deref() == Some(&id) {
                    self.new_session();
                }
            }
            Event::Tool {
                id,
                title,
                status,
                detail,
            } => {
                self.activity.push_back(format!("{status} · {title}"));
                self.trim_activity();
                self.transcript.tool(id, title, status, detail);
            }
            Event::UserChunk(text) => self.transcript.append(Role::User, text),
            Event::Chunk(text) => self.transcript.append(Role::Agent, text),
            Event::Thought(text) => {
                if self.preferences.show_thoughts {
                    self.transcript.append(Role::Thought, text);
                }
            }
            Event::Note(text) => self.transcript.push(Role::Note, text),
            Event::Error(text) => {
                if !self.ready
                    && let Some(from) = self.replay_from.take()
                {
                    self.transcript.trim(from);
                }
                self.transcript.push(Role::Error, text);
            }
            Event::End { interrupted } => {
                self.turns = self.turns.saturating_sub(1);
                self.queue_paused |= interrupted;
                self.activity.push_back(if interrupted {
                    "turn cancelled".into()
                } else {
                    "turn complete".into()
                });
                self.trim_activity();
                if self.turns == 0 {
                    self.approvals.clear();
                    self.status = "Connected";
                    self.update_execution();
                    self.flush_queue();
                }
            }
            Event::Disconnected => {
                if let Some(from) = self.replay_from.take() {
                    self.transcript.trim(from);
                    self.transcript
                        .push(Role::Note, "The session did not finish loading".into());
                }
                self.ready = false;
                self.turns = 0;
                self.execution = None;
                self.queue_paused = true;
                self.approvals.clear();
                self.status = "Disconnected";
                self.activity.push_back("disconnected".into());
                self.trim_activity();
            }
            Event::Connecting => {
                self.ready = false;
                self.turns = 0;
                self.execution = None;
                self.approvals.clear();
                self.status = "Reconnecting";
            }
            Event::Permission {
                title,
                options,
                respond,
            } => {
                self.transcript
                    .push(Role::Note, format!("Permission requested · {title}"));
                self.approvals.push_back((options, respond));
            }
        }
    }

    pub fn paste(&mut self, text: &str) {
        self.draft.insert(text);
        self.refresh_completion();
    }

    pub fn take_local_effect(&mut self) -> Option<LocalEffect> {
        self.local_effect.take()
    }

    pub fn reconnect(&mut self) {
        if self.ready || matches!(self.status, "Reconnecting" | "Connecting") {
            self.transcript
                .push(Role::Note, "The connection is already open".into());
            return;
        }
        if self.loaded {
            self.replay_from = Some(self.transcript.len());
        }
        self.status = "Reconnecting";
        self.local_effect = Some(LocalEffect::Reconnect);
    }

    pub fn new_session(&mut self) {
        if !self.can_switch() {
            self.transcript.push(
                Role::Note,
                "Stop the running turn before switching sessions".into(),
            );
            return;
        }
        let Some(workspace) = self.workspace.clone() else {
            self.transcript.push(
                Role::Note,
                "Connect once so the new session has a workspace".into(),
            );
            return;
        };
        self.loaded = false;
        self.ready = false;
        self.replay_from = Some(self.transcript.len());
        self.status = "Connecting";
        self.local_effect = Some(LocalEffect::Switch(SessionTarget {
            cwd: Some(PathBuf::from(workspace)),
            session: None,
        }));
    }

    pub fn load_session(&mut self, id: String) {
        if !self.can_switch() {
            self.transcript.push(
                Role::Note,
                "Stop the running turn before switching sessions".into(),
            );
            return;
        }
        if !self.load_supported {
            self.transcript.push(
                Role::Note,
                "The agent does not advertise session/load".into(),
            );
            return;
        }
        self.loaded = true;
        self.ready = false;
        self.replay_from = Some(self.transcript.len());
        self.status = "Connecting";
        self.local_effect = Some(LocalEffect::Switch(SessionTarget {
            cwd: None,
            session: Some(id),
        }));
    }

    fn can_switch(&self) -> bool {
        self.turns == 0 && !self.remote_busy()
    }

    pub fn close_session(&mut self, id: Option<String>) {
        if !self.ready {
            self.transcript
                .push(Role::Note, "Connect before closing a session".into());
            return;
        }
        if !self.close_supported {
            self.transcript.push(
                Role::Note,
                "The agent does not advertise session/close".into(),
            );
            return;
        }
        let Some(id) = id
            .filter(|id| !id.is_empty())
            .or_else(|| self.session.clone())
        else {
            self.transcript
                .push(Role::Note, "There is no current session to close".into());
            return;
        };
        if self.session.as_deref() == Some(&id) && !self.can_switch() {
            self.transcript.push(
                Role::Note,
                "Stop the running turn before closing its session".into(),
            );
            return;
        }
        let _ = self.actions.unbounded_send(Action::CloseSession(id));
    }

    pub fn delete_session(&mut self, id: String) {
        if id.is_empty() {
            self.transcript
                .push(Role::Note, "Use /delete <session id>".into());
            return;
        }
        if !self.ready {
            self.transcript
                .push(Role::Note, "Connect before deleting a session".into());
            return;
        }
        if !self.delete_supported {
            self.transcript.push(
                Role::Note,
                "The agent does not advertise session/delete".into(),
            );
            return;
        }
        if self.session.as_deref() == Some(id.as_str()) && !self.can_switch() {
            self.transcript.push(
                Role::Note,
                "Stop the running turn before deleting its session".into(),
            );
            return;
        }
        let _ = self.actions.unbounded_send(Action::DeleteSession(id));
    }

    pub fn key(&mut self, key: Key) {
        if let Some((options, _)) = self.approvals.front() {
            let answer = match key {
                Key::Escape => Some(None),
                Key::Text(digit) => digit
                    .to_digit(10)
                    .and_then(|digit| digit.checked_sub(1))
                    .map(|index| index as usize)
                    .filter(|index| *index < options.len())
                    .map(Some),
                _ => None,
            };
            if let Some(answer) = answer {
                if let Some((_, sender)) = self.approvals.pop_front() {
                    let _ = sender.send(answer);
                }
                return;
            }
        }

        if self.completion.is_some() {
            match key {
                Key::Escape => {
                    self.completion = None;
                    return;
                }
                Key::Tab => {
                    self.accept_completion();
                    return;
                }
                Key::HistoryUp => {
                    self.select_completion(false);
                    return;
                }
                Key::HistoryDown => {
                    self.select_completion(true);
                    return;
                }
                _ => {}
            }
        }

        match key {
            Key::Text(ch) => self.draft.insert(ch.encode_utf8(&mut [0; 4])),
            Key::Newline => self.draft.insert("\n"),
            Key::Backspace => self.draft.backspace(),
            Key::Delete => self.draft.delete(),
            Key::Left => self.draft.left(),
            Key::Right => self.draft.right(),
            Key::Home => self.draft.home(),
            Key::End => self.draft.end(),
            Key::HistoryUp => self.draft.recall(true),
            Key::HistoryDown => self.draft.recall(false),
            Key::ScrollUp => self.transcript.scroll_up(),
            Key::ScrollDown => self.transcript.scroll_down(),
            Key::ToggleTools => {
                let expanded = self.transcript.toggle_tools();
                self.preferences.expand_tools = expanded;
                self.save_preferences();
            }
            Key::ToggleSidebar => self.toggle_sidebar(),
            Key::Reconnect => self.reconnect(),
            Key::Escape if self.turns > 0 || self.remote_busy() => {
                if self.actions.unbounded_send(Action::Cancel).is_ok() {
                    self.queue_paused = true;
                    self.status = "Cancelling";
                }
            }
            Key::Enter if !self.draft.text().trim().is_empty() => self.submit(),
            Key::Tab => self.draft.insert("\t"),
            Key::Escape | Key::Enter => {}
        }
        self.refresh_completion();
    }

    fn refresh_completion(&mut self) {
        self.completion = suggestions(self.draft.text(), &self.skills)
            .map(|items| Completion { items, selected: 0 });
    }

    pub fn toggle_sidebar(&mut self) {
        self.preferences.sidebar = !self.preferences.sidebar;
        self.save_preferences();
    }

    pub fn toggle_thoughts(&mut self) {
        self.preferences.show_thoughts = !self.preferences.show_thoughts;
        let shown = if self.preferences.show_thoughts {
            "shown"
        } else {
            "hidden"
        };
        self.transcript
            .push(Role::Note, format!("Reasoning is now {shown}"));
        self.save_preferences();
    }

    fn save_preferences(&mut self) {
        self.local_effect = Some(LocalEffect::SavePreferences(self.preferences.clone()));
    }

    fn trim_activity(&mut self) {
        while self.activity.len() > 8 {
            self.activity.pop_front();
        }
    }

    fn accept_completion(&mut self) {
        let Some(completion) = self.completion.take() else {
            return;
        };
        let Some(suggestion) = completion.items.get(completion.selected) else {
            return;
        };
        let text = self.draft.text();
        if let Some(start) = last_token_start(text).filter(|start| text[*start..].starts_with('$'))
        {
            self.draft.replace_from(start, &suggestion.insert);
        } else {
            self.draft.set_text(&suggestion.insert);
        }
    }

    fn select_completion(&mut self, forward: bool) {
        let Some(completion) = &mut self.completion else {
            return;
        };
        let len = completion.items.len();
        completion.selected = if forward {
            (completion.selected + 1) % len
        } else {
            (completion.selected + len - 1) % len
        };
    }

    fn update_execution(&mut self) {
        if !self.ready
            || self.session.as_ref() != self.execution.as_ref().map(|state| &state.session_id)
        {
            return;
        }
        let Some(state) = &self.execution else { return };
        match state.status {
            ExecutionStatus::Idle if self.turns == 0 => {
                self.status = "Connected";
                self.flush_queue();
            }
            ExecutionStatus::Waiting if self.status != "Cancelling" => {
                self.status = "Waiting";
            }
            ExecutionStatus::Running if self.status != "Cancelling" => {
                self.status = "Streaming";
            }
            _ => {}
        }
    }

    fn remote_busy(&self) -> bool {
        self.execution.as_ref().is_some_and(|state| {
            self.session.as_ref() == Some(&state.session_id)
                && !matches!(state.status, ExecutionStatus::Idle)
        })
    }

    fn flush_queue(&mut self) {
        if !self.ready || self.turns > 0 || self.remote_busy() || self.queue_paused {
            return;
        }
        if let Some(prompt) = self.queue.pop_front()
            && let Err(prompt) = self.send_prompt(prompt)
        {
            self.queue.push_front(prompt);
        }
    }

    fn prepare_prompt(&mut self, text: String) -> Option<Prompt> {
        if self.attachment_reads > 0 {
            self.transcript.push(
                Role::Note,
                "Wait for the attachment to finish loading".into(),
            );
            return None;
        }
        let size = self
            .attachments
            .iter()
            .fold(text.len(), |size, attachment| {
                size.saturating_add(attachment.size)
            });
        if size > MAX_PROMPT_BYTES {
            self.transcript.push(
                Role::Error,
                "Prompt and attachments exceed the ACP prompt limit".into(),
            );
            return None;
        }
        Some(Prompt {
            skill_meta: skills::prompt_meta(&text, &self.skills),
            text,
            attachments: std::mem::take(&mut self.attachments),
        })
    }

    fn send_draft(&mut self, text: String) {
        let Some(prompt) = self.prepare_prompt(text) else {
            return;
        };
        match self.send_prompt(prompt) {
            Ok(()) => {
                self.draft.submit();
            }
            Err(prompt) => self.attachments = prompt.attachments,
        }
    }

    fn send_prompt(&mut self, prompt: Prompt) -> Result<(), Prompt> {
        let mut displayed = prompt.text.clone();
        for attachment in &prompt.attachments {
            displayed.push_str(&format!("\n[Attachment: {}]", attachment.name));
        }
        if let Err(error) = self.actions.unbounded_send(Action::Prompt(prompt)) {
            let Action::Prompt(prompt) = error.into_inner() else {
                unreachable!()
            };
            return Err(prompt);
        }
        self.transcript.push(Role::User, displayed);
        self.turns += 1;
        self.transcript.follow();
        self.status = "Streaming";
        self.activity.push_back("prompt sent".into());
        self.trim_activity();
        Ok(())
    }
}

fn suggestions(text: &str, skills: &[Skill]) -> Option<Vec<Suggestion>> {
    if text.contains('\n') || text.trim().is_empty() {
        return None;
    }
    let start = last_token_start(text)?;
    let token = &text[start..];
    if let Some(prefix) = token.strip_prefix('$') {
        let items: Vec<Suggestion> = skills
            .iter()
            .filter(|skill| {
                skill.user_invocable && skill.id.starts_with(prefix) && skill.id != prefix
            })
            .map(|skill| Suggestion {
                insert: format!("${} ", skill.id),
                description: skill.description.clone(),
            })
            .collect();
        return (!items.is_empty()).then_some(items);
    }
    if start == 0 && token.starts_with('/') {
        let items: Vec<Suggestion> = COMMANDS
            .iter()
            .filter(|(command, _)| command.starts_with(token) && *command != token)
            .map(|(command, description)| Suggestion {
                insert: format!("{command} "),
                description: (*description).to_owned(),
            })
            .collect();
        return (!items.is_empty()).then_some(items);
    }
    None
}

fn last_token_start(text: &str) -> Option<usize> {
    let mut start = 0;
    for (index, character) in text.char_indices() {
        if character.is_whitespace() {
            start = index + character.len_utf8();
        }
    }
    (start < text.len()).then_some(start)
}
