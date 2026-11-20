use std::collections::VecDeque;

use agent_client_protocol::v1::schema::{
    SessionConfigKind, SessionConfigOption, SessionConfigOptionCategory,
};
use futures::channel::{mpsc, oneshot};
use ratatui::Frame;
use ratatui::layout::{Constraint, Layout, Position};
use ratatui::style::Stylize;
use ratatui::text::{Line, Text};
use ratatui::widgets::{Block, Borders, Paragraph};

use crate::acp::{Action, Event, ExecutionState, ExecutionStatus};
use crate::composer::Composer;
use crate::options;
use crate::transcript::{Role, Transcript};

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
    queue: VecDeque<String>,
    queue_paused: bool,
    draft: Composer,
    transcript: Transcript,
    approval: Option<Approval>,
    clipboard: Option<String>,
    options: Vec<SessionConfigOption>,
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
            queue_paused: false,
            draft: Composer::default(),
            transcript: Transcript::new(),
            approval: None,
            clipboard: None,
            options: Vec::new(),
            actions,
        }
    }

    pub fn event(&mut self, event: Event) {
        match event {
            Event::Ready { session, workspace } => {
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
            Event::UserChunk(text) => self.transcript.append(Role::User, text),
            Event::Chunk(text) => self.transcript.append(Role::Agent, text),
            Event::Thought(text) => self.transcript.append(Role::Thought, text),
            Event::Note(text) => self.transcript.push(Role::Note, text),
            Event::Error(text) => self.transcript.push(Role::Error, text),
            Event::End { interrupted } => {
                self.turns = self.turns.saturating_sub(1);
                self.queue_paused |= interrupted;
                if self.turns == 0 {
                    self.approval = None;
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
                self.approval = None;
                self.status = "Disconnected";
            }
            Event::Permission {
                title,
                options,
                respond,
            } => {
                self.transcript
                    .push(Role::Note, format!("Permission requested · {title}"));
                self.approval = Some((options, respond));
            }
        }
    }

    pub fn paste(&mut self, text: &str) {
        self.draft.insert(text);
    }

    pub fn take_clipboard(&mut self) -> Option<String> {
        self.clipboard.take()
    }

    pub fn key(&mut self, key: Key) {
        if let Some((options, _)) = &self.approval {
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
                if let Some((_, sender)) = self.approval.take() {
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

    fn submit(&mut self) {
        let input = self.draft.text().to_owned();
        match input.trim() {
            "/help" => {
                self.draft.submit();
                self.transcript.push(
                    Role::Note,
                    "/queue <prompt> · /resume-queue · /clear-queue · /find <text> · /copy".into(),
                );
            }
            "/model" | "/models" => self.config_command(SessionConfigOptionCategory::Model, ""),
            "/effort" => self.config_command(SessionConfigOptionCategory::ThoughtLevel, ""),
            text if text.starts_with("/model ") => {
                self.config_command(
                    SessionConfigOptionCategory::Model,
                    text["/model ".len()..].trim(),
                );
            }
            text if text.starts_with("/effort ") => {
                self.config_command(
                    SessionConfigOptionCategory::ThoughtLevel,
                    text["/effort ".len()..].trim(),
                );
            }
            "/copy" => {
                self.draft.submit();
                if self.turns == 0 && !self.remote_busy() {
                    self.clipboard = self.transcript.latest_agent().map(str::to_owned);
                }
                if self.clipboard.is_none() {
                    self.transcript
                        .push(Role::Note, "No completed response to copy".into());
                }
            }
            text if text.starts_with("/find ") => {
                self.draft.submit();
                if !self.transcript.find(text["/find ".len()..].trim()) {
                    self.transcript
                        .push(Role::Note, "No matching transcript text".into());
                }
            }
            "/resume-queue" => {
                self.draft.submit();
                self.queue_paused = false;
                self.flush_queue();
            }
            "/clear-queue" => {
                self.draft.submit();
                self.queue.clear();
                self.queue_paused = false;
            }
            text if text.starts_with("/queue ") => {
                let text = text["/queue ".len()..].trim();
                if !text.is_empty() {
                    self.draft.submit();
                    self.queue.push_back(text.to_owned());
                    self.flush_queue();
                }
            }
            _ => {
                let accepted = self.status != "Cancelling" && self.send_prompt(input);
                if accepted {
                    self.draft.submit();
                }
            }
        }
    }

    fn config_command(&mut self, category: SessionConfigOptionCategory, input: &str) {
        self.draft.submit();
        let selected = self
            .options
            .iter()
            .find(|option| options::matches_category(option, &category));
        let notice = match selected {
            Some(option) if input.is_empty() => options::summary(option),
            Some(option) => match options::value(option, input) {
                Some(value) => {
                    let _ = self
                        .actions
                        .unbounded_send(Action::SetOption(option.id.clone(), value));
                    return;
                }
                None => "Invalid configuration value".into(),
            },
            None => "The agent did not advertise this session setting".into(),
        };
        self.transcript.push(Role::Note, notice);
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
            && !self.send_prompt(prompt.clone())
        {
            self.queue.push_front(prompt);
        }
    }

    fn send_prompt(&mut self, prompt: String) -> bool {
        if self
            .actions
            .unbounded_send(Action::Prompt(prompt.clone()))
            .is_err()
        {
            return false;
        }
        self.transcript.push(Role::User, prompt);
        self.turns += 1;
        self.transcript.follow();
        self.status = "Streaming";
        true
    }

    pub fn draw(&mut self, frame: &mut Frame) {
        let area = frame.area();
        let prompt_height = area.height.clamp(4, 12) / 2;
        let approval_height = self.approval.as_ref().map_or(0, |(options, _)| {
            u16::try_from(options.len().min(9) + 1).unwrap_or(10)
        });
        let [header, transcript, approval, composer, help] = Layout::vertical([
            Constraint::Length(1),
            Constraint::Min(1),
            Constraint::Length(approval_height),
            Constraint::Length(prompt_height),
            Constraint::Length(1),
        ])
        .areas(area);

        frame.render_widget(
            Paragraph::new(Line::from(vec![
                " hrns ".bold().cyan(),
                format!(" · {} · {}", self.destination, self.status).into(),
                self.workspace
                    .as_ref()
                    .map_or_else(String::new, |cwd| format!(" · {cwd}"))
                    .into(),
                self.options
                    .iter()
                    .find_map(|option| match &option.kind {
                        SessionConfigKind::Select(select)
                            if options::matches_category(
                                option,
                                &SessionConfigOptionCategory::Model,
                            ) =>
                        {
                            Some(format!(" · {}", select.current_value))
                        }
                        _ => None,
                    })
                    .unwrap_or_default()
                    .into(),
                if self.queue.is_empty() {
                    String::new().into()
                } else {
                    let state = if self.queue_paused {
                        "queued · paused"
                    } else {
                        "queued"
                    };
                    format!(" · {} {state}", self.queue.len()).into()
                },
            ])),
            header,
        );
        let visible = self.transcript.visible(transcript.width, transcript.height);
        frame.render_widget(Paragraph::new(visible), transcript);

        if let Some((options, _)) = &self.approval {
            let choices = options
                .iter()
                .take(9)
                .enumerate()
                .map(|(i, option)| Line::from(format!(" {}  {option}", i + 1)));
            let rows: Vec<_> =
                std::iter::once(Line::from(" Choose a permission · Esc cancels".yellow()))
                    .chain(choices)
                    .collect();
            frame.render_widget(Paragraph::new(rows), approval);
        }

        let title = self.session.as_ref().map_or_else(
            || " Prompt ".to_owned(),
            |session| format!(" Prompt · {session} "),
        );
        let block = Block::default().borders(Borders::TOP).title(title).cyan();
        let input = block.inner(composer);
        frame.render_widget(block, composer);
        if input.width > 0 && input.height > 0 {
            let (rows, (row, column)) = self.draft.display(input.width);
            let scroll = row.saturating_sub(input.height - 1);
            let text = Paragraph::new(Text::from(
                rows.into_iter().map(Line::raw).collect::<Vec<_>>(),
            ));
            frame.render_widget(text.scroll((scroll, 0)), input);
            frame.set_cursor_position(Position::new(input.x + column, input.y + row - scroll));
        }
        frame.render_widget(
            Paragraph::new(" Enter send · Shift+Enter newline · PgUp/PgDn scroll · Esc cancel")
                .gray(),
            help,
        );
    }
}
