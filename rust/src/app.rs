mod commands;
mod render;

use std::collections::VecDeque;

use agent_client_protocol::v1::schema::SessionConfigOption;
use futures::channel::{mpsc, oneshot};

use crate::acp::{Action, Event, ExecutionState, ExecutionStatus};
use crate::attachments::{Attachment, MAX_PROMPT_BYTES, Prompt};
use crate::composer::Composer;
use crate::transcript::{Role, Transcript};

pub enum LocalEffect {
    Copy(String),
    Attach(String),
    Reconnect,
}

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
    Reconnect,
    Escape,
}

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
    queue_paused: bool,
    draft: Composer,
    transcript: Transcript,
    approvals: VecDeque<Approval>,
    local_effect: Option<LocalEffect>,
    options: Vec<SessionConfigOption>,
    commands: Vec<(String, String)>,
    plan: Vec<String>,
    usage: Option<(u64, u64)>,
    actions: mpsc::UnboundedSender<Action>,
}

impl App {
    pub fn new(destination: String, actions: mpsc::UnboundedSender<Action>) -> Self {
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
            queue_paused: false,
            draft: Composer::default(),
            transcript: Transcript::new(),
            approvals: VecDeque::new(),
            local_effect: None,
            options: Vec::new(),
            commands: Vec::new(),
            plan: Vec::new(),
            usage: None,
            actions,
        }
    }

    pub fn event(&mut self, event: Event) {
        match event {
            Event::Ready {
                session,
                workspace,
                attachments_supported,
            } => {
                self.attachments_supported = attachments_supported;
                self.workspace = Some(workspace.display().to_string());
                self.session = Some(session);
                self.ready = true;
                self.status = "Connected";
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
            Event::UserChunk(text) => self.transcript.append(Role::User, text),
            Event::Chunk(text) => self.transcript.append(Role::Agent, text),
            Event::Thought(text) => self.transcript.append(Role::Thought, text),
            Event::Note(text) => self.transcript.push(Role::Note, text),
            Event::Error(text) => self.transcript.push(Role::Error, text),
            Event::End { interrupted } => {
                self.turns = self.turns.saturating_sub(1);
                self.queue_paused |= interrupted;
                if self.turns == 0 {
                    self.approvals.clear();
                    self.status = "Connected";
                    self.update_execution();
                    self.flush_queue();
                }
            }
            Event::Disconnected => {
                self.ready = false;
                self.turns = 0;
                self.execution = None;
                self.queue_paused = true;
                self.approvals.clear();
                self.status = "Disconnected";
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
    }

    pub fn take_local_effect(&mut self) -> Option<LocalEffect> {
        self.local_effect.take()
    }

    pub fn reconnect(&mut self) {
        if self.ready || self.status == "Reconnecting" {
            self.transcript
                .push(Role::Note, "The connection is already open".into());
            return;
        }
        self.status = "Reconnecting";
        self.local_effect = Some(LocalEffect::Reconnect);
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
            Key::Reconnect => self.reconnect(),
            Key::Escape if self.turns > 0 || self.remote_busy() => {
                if self.actions.unbounded_send(Action::Cancel).is_ok() {
                    self.queue_paused = true;
                    self.status = "Cancelling";
                }
            }
            Key::Enter if self.ready && !self.draft.text().trim().is_empty() => self.submit(),
            Key::Escape | Key::Enter => {}
        }
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
        Ok(())
    }
}
