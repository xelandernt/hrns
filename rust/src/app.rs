mod commands;
mod picker;
mod render;

use std::collections::{BTreeMap, HashSet, VecDeque};
use std::path::PathBuf;

use agent_client_protocol::v1::schema::{
    ElicitationContentValue, PlanEntry, SessionConfigOption, ToolKind,
};
use futures::channel::{mpsc, oneshot};
use ratatui::layout::{Position, Rect};

use crate::acp::{
    Action, ClientIoMode, ConnectTarget, Event, ExecutionState, ExecutionStatus, PermissionRequest,
    ProviderInfo, SessionTarget,
};
use crate::attachments::{Attachment, MAX_PROMPT_BYTES, Prompt};
use crate::composer::Composer;
use crate::elicitation::FormState;
use crate::mcp_types::{ConsentDecision, Health};
use crate::preferences::{ApprovalMode, Preferences};
use crate::skills::{self, Skill};
use crate::theme::{self, Palette};
use crate::transcript::{Role, Transcript};

pub enum LocalEffect {
    Copy(String),
    Attach(String),
    Reconnect,
    Switch(SessionTarget),
    Connect(ConnectTarget),
    SavePreferences(Preferences),
}

#[derive(Clone)]
pub struct Startup {
    pub client_io: ClientIoMode,
    pub theme: Option<String>,
    pub permission: Option<ApprovalMode>,
}

impl Default for Startup {
    fn default() -> Self {
        Self {
            client_io: ClientIoMode::Off,
            theme: None,
            permission: None,
        }
    }
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
    PreviousPrompt,
    NextPrompt,
    LatestOutput,
    Tab,
    BackTab,
    ToggleTools,
    ToggleSidebar,
    Reconnect,
    Escape,
}

struct ProviderPopup {
    result: Option<Result<Vec<ProviderInfo>, String>>,
    selected: usize,
}

struct Suggestion {
    insert: String,
    description: String,
}

struct Completion {
    items: Vec<Suggestion>,
    selected: usize,
}

pub struct PendingElicitation {
    pub state: FormState,
    pub respond: oneshot::Sender<Option<BTreeMap<String, ElicitationContentValue>>>,
}

struct PendingApproval {
    request: PermissionRequest,
    selected: usize,
    detail_scroll: usize,
}

pub struct PendingMcpConsent {
    pub name: String,
    pub transport: String,
    pub target: String,
    pub respond: oneshot::Sender<ConsentDecision>,
}

const COMMANDS: &[(&str, &str)] = &[
    ("/help", "List commands"),
    ("/sessions", "Search and resume saved sessions"),
    ("/new", "Start a new session"),
    ("/load", "Load a session by id"),
    ("/close", "Close a session"),
    ("/delete", "Delete a saved session"),
    ("/models", "Search and select a model"),
    ("/model", "Select a model"),
    ("/effort", "Select reasoning effort"),
    ("/providers", "Browse and manage providers"),
    ("/provider", "Add, edit or remove a provider"),
    ("/permissions", "Show or set the approval mode"),
    ("/settings", "Show or set terminal settings"),
    ("/theme", "Show or select a theme"),
    ("/mcp", "Show MCP server health"),
    ("/todo", "Show the current plan"),
    ("/find", "Find transcript text"),
    ("/copy", "Copy the latest reply"),
    ("/connect", "Connect to an ACP endpoint"),
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

pub struct App {
    destination: String,
    workspace: Option<String>,
    session: Option<String>,
    session_title: Option<String>,
    mode: Option<String>,
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
    provider_editor: bool,
    loaded: bool,
    replay_from: Option<usize>,
    queue_paused: bool,
    draft: Composer,
    transcript: Transcript,
    transcript_area: Rect,
    approvals: VecDeque<PendingApproval>,
    elicitations: VecDeque<PendingElicitation>,
    mcp_consent: VecDeque<PendingMcpConsent>,
    mcp_grants: HashSet<String>,
    mcp_health: Vec<Health>,
    mcp_failures: usize,
    local_effect: Option<LocalEffect>,
    options: Vec<SessionConfigOption>,
    commands: Vec<(String, String)>,
    skills: Vec<Skill>,
    completion: Option<Completion>,
    plan: Vec<PlanEntry>,
    replay_plan: Option<Vec<PlanEntry>>,
    todo_open: bool,
    todo_auto_opened: bool,
    todo_scroll: Option<u16>,
    usage: Option<(u64, u64)>,
    usage_cost: Option<String>,
    last_usage_tokens: Option<u64>,
    providers: Option<ProviderPopup>,
    picker: Option<picker::Picker>,
    session_request: u64,
    preferences: Preferences,
    theme: String,
    approval_mode: ApprovalMode,
    client_io: ClientIoMode,
    activity: VecDeque<String>,
    actions: mpsc::UnboundedSender<Action>,
}

impl App {
    pub fn new(
        destination: String,
        actions: mpsc::UnboundedSender<Action>,
        preferences: Preferences,
        startup: Startup,
    ) -> Self {
        let mut transcript = Transcript::new();
        transcript.show_thoughts(preferences.show_thoughts);
        let theme = preferences
            .theme
            .clone()
            .or(startup.theme)
            .unwrap_or_else(|| "hrns".into());
        let approval_mode = preferences
            .permission_mode
            .or(startup.permission)
            .unwrap_or_default();
        Self {
            destination,
            workspace: None,
            session: None,
            session_title: None,
            mode: None,
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
            provider_editor: false,
            loaded: false,
            replay_from: None,
            queue_paused: false,
            draft: Composer::default(),
            transcript,
            transcript_area: Rect::default(),
            approvals: VecDeque::new(),
            elicitations: VecDeque::new(),
            mcp_consent: VecDeque::new(),
            mcp_grants: HashSet::new(),
            mcp_health: Vec::new(),
            mcp_failures: 0,
            local_effect: None,
            options: Vec::new(),
            commands: Vec::new(),
            skills: Vec::new(),
            completion: None,
            plan: Vec::new(),
            replay_plan: None,
            todo_open: false,
            todo_auto_opened: false,
            todo_scroll: None,
            usage: None,
            usage_cost: None,
            last_usage_tokens: None,
            providers: None,
            picker: None,
            session_request: 0,
            preferences,
            theme,
            approval_mode,
            client_io: startup.client_io,
            activity: VecDeque::new(),
            actions,
        }
    }

    pub fn palette(&self) -> Palette {
        theme::palette(&self.theme)
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
                provider_editor,
            } => {
                if let Some(from) = self.replay_from.take() {
                    self.transcript.replace(from);
                }
                self.attachments_supported = attachments_supported;
                self.load_supported = load_supported;
                self.close_supported = close_supported;
                self.delete_supported = delete_supported;
                self.provider_editor = provider_editor;
                self.loaded = loaded;
                if !loaded {
                    self.session_title = None;
                }
                self.workspace = Some(workspace.display().to_string());
                if self.session.as_deref() != Some(&session) {
                    self.plan.clear();
                    self.todo_auto_opened = false;
                    self.todo_scroll = self.todo_scroll.map(|_| 0);
                }
                if let Some(plan) = self.replay_plan.take() {
                    self.update_plan(plan);
                }
                self.session = Some(session);
                self.ready = true;
                self.status = "Connected";
                self.load_waiting_sessions();
                self.refresh_completion();
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
                    if let Some(usage) = &state.last_usage {
                        self.last_usage_tokens = usage.total_tokens;
                    }
                    self.execution = Some(state);
                    self.update_execution();
                }
            }
            Event::ModelSelected(options) => {
                self.event(Event::Options(options));
                if self.draft.text().is_empty() && self.picker.is_none() && self.providers.is_none()
                {
                    self.choose_model_effort();
                }
            }
            Event::Options(options) => {
                self.options = options;
                self.refresh_completion();
                if let Some(picker) = self.picker.as_ref() {
                    let last = self.picker_rows(picker).len().saturating_sub(1);
                    if let Some(picker) = self.picker.as_mut() {
                        picker.selected = picker.selected.min(last);
                    }
                }
            }
            Event::Sessions { request_id, result } => self.sessions_received(request_id, result),
            Event::AgentCommands(commands) => self.commands = commands,
            Event::Skills(skills) => {
                self.skills = skills;
                self.refresh_completion();
            }
            Event::SessionTitle(title) => self.session_title = title,
            Event::Mode(mode) => self.mode = Some(mode),
            Event::Providers(result) => {
                if let Some(popup) = &mut self.providers {
                    popup.result = Some(result);
                    popup.selected = 0;
                }
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
            Event::Plan(plan) => {
                if self.ready {
                    self.update_plan(plan);
                } else {
                    self.replay_plan = Some(plan);
                }
            }
            Event::Usage { used, size, cost } => {
                self.usage = Some((used, size));
                self.usage_cost = cost;
            }
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
            Event::Tool { id, display } => {
                self.activity.push_back(format!(
                    "{} · {}",
                    crate::acp::status_label(display.status),
                    display.title
                ));
                self.trim_activity();
                self.transcript.tool(id, display);
            }
            Event::UserChunk(text) => self.transcript.append(Role::User, text),
            Event::Chunk(text) => self.transcript.append(Role::Agent, text),
            Event::Thought(text) => self.transcript.append(Role::Thought, text),
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
                    self.elicitations.clear();
                    self.status = "Connected";
                    self.update_execution();
                    self.flush_queue();
                }
            }
            Event::Disconnected => {
                self.sessions_received(
                    self.session_request,
                    Err("Disconnected from the agent".into()),
                );
                self.replay_plan = None;
                if let Some(popup) = &mut self.providers {
                    popup.result = Some(Err("Disconnected from the agent".into()));
                }
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
                self.elicitations.clear();
                self.mcp_consent.clear();
                self.status = "Disconnected";
                self.activity.push_back("disconnected".into());
                self.trim_activity();
            }
            Event::ConnectionChanged(destination) => {
                self.mcp_grants.clear();
                self.destination = destination;
                self.workspace = None;
                self.session = None;
                self.session_title = None;
                self.mode = None;
                self.loaded = false;
                self.ready = false;
                self.replay_from = Some(self.transcript.len());
                self.replay_plan = None;
                self.plan.clear();
                self.todo_auto_opened = false;
                self.usage = None;
                self.usage_cost = None;
                self.last_usage_tokens = None;
                self.options.clear();
                self.commands.clear();
                self.skills.clear();
                self.mcp_health.clear();
                self.load_supported = false;
                self.close_supported = false;
                self.delete_supported = false;
                self.provider_editor = false;
                self.attachments_supported = false;
                self.queue_paused = !self.queue.is_empty();
                self.refresh_completion();
            }
            Event::Connecting => {
                self.ready = false;
                self.turns = 0;
                self.execution = None;
                self.approvals.clear();
                self.elicitations.clear();
                self.mcp_consent.clear();
                self.status = "Reconnecting";
            }
            Event::McpConsent {
                name,
                transport,
                target,
                respond,
            } => {
                if self.mcp_grants.contains(&name) {
                    self.transcript
                        .push(Role::Note, format!("MCP {name} reused until exit"));
                    let _ = respond.send(ConsentDecision::Session);
                } else {
                    self.transcript
                        .push(Role::Note, format!("MCP consent · {name} ({transport})"));
                    self.mcp_consent.push_back(PendingMcpConsent {
                        name,
                        transport,
                        target,
                        respond,
                    });
                }
            }
            Event::McpHealth(health) => {
                let failures = health.iter().filter(|item| item.state == "failed").count();
                if failures > self.mcp_failures {
                    let names = health
                        .iter()
                        .filter(|item| item.state == "failed")
                        .map(|item| item.name.as_str())
                        .collect::<Vec<_>>()
                        .join(", ");
                    self.transcript
                        .push(Role::Error, format!("MCP server failed · {names}"));
                }
                self.mcp_failures = failures;
                self.mcp_health = health;
            }
            Event::Permission(request) => self.request_permission(request),
            Event::Elicitation { form, respond } => {
                self.transcript
                    .push(Role::Note, format!("Agent question · {}", form.message));
                self.elicitations.push_back(PendingElicitation {
                    state: FormState::new(form),
                    respond,
                });
            }
        }
    }

    fn show_providers(&mut self) {
        let loading = self.ready && self.actions.unbounded_send(Action::ListProviders).is_ok();
        self.providers = Some(ProviderPopup {
            result: (!loading).then(|| Err("Connect to an agent to list providers".into())),
            selected: 0,
        });
    }

    fn provider_key(&mut self, key: Key) {
        if matches!(key, Key::Escape) {
            self.providers = None;
            return;
        }
        let Some(popup) = &mut self.providers else {
            return;
        };
        let Some(Ok(providers)) = &popup.result else {
            return;
        };
        if matches!(key, Key::Text('a')) && self.provider_editor {
            self.providers = None;
            self.provider_usage();
            self.draft.set_text("/provider add ");
            self.refresh_completion();
            return;
        }
        let Some(provider) = providers.get(popup.selected) else {
            return;
        };
        let action = match key {
            Key::Enter => {
                let api_type = provider
                    .current
                    .as_ref()
                    .map(|current| current.api_type.as_str())
                    .or_else(|| {
                        (provider.supported.len() == 1).then(|| provider.supported[0].as_str())
                    });
                let operation = if self.provider_editor { "edit" } else { "set" };
                let mut command = match api_type {
                    Some(api_type) => format!("/provider {operation} {} {api_type} ", provider.id),
                    None => format!("/provider {operation} {} ", provider.id),
                };
                if let Some(url) = provider
                    .current
                    .as_ref()
                    .and_then(|current| current.base_url.as_deref())
                {
                    command.push_str(url);
                }
                if self.provider_editor && command.ends_with(' ') {
                    command.push('-');
                }
                Some(command)
            }
            Key::Text('d') if self.provider_editor && !provider.required => {
                Some(format!("/provider remove {}", provider.id))
            }
            Key::Text('d') if !provider.required => {
                Some(format!("/provider disable {}", provider.id))
            }
            Key::HistoryUp => {
                popup.selected = popup.selected.saturating_sub(1);
                None
            }
            Key::HistoryDown => {
                popup.selected = (popup.selected + 1).min(providers.len() - 1);
                None
            }
            Key::Home => {
                popup.selected = 0;
                None
            }
            Key::End => {
                popup.selected = providers.len() - 1;
                None
            }
            _ => None,
        };
        if let Some(command) = action {
            self.providers = None;
            self.draft.set_text(&command);
            self.refresh_completion();
        }
    }

    fn request_permission(&mut self, request: PermissionRequest) {
        if let Some(answer) = self.auto_permission(&request) {
            let _ = request.respond.send(answer);
            return;
        }
        self.transcript.push(
            Role::Note,
            format!("Permission requested · {}", request.title),
        );
        self.approvals.push_back(PendingApproval {
            request,
            selected: 0,
            detail_scroll: 0,
        });
    }

    fn auto_permission(&self, request: &PermissionRequest) -> Option<Option<usize>> {
        let allowed = |kind: agent_client_protocol::v1::schema::PermissionOptionKind| {
            matches!(
                kind,
                agent_client_protocol::v1::schema::PermissionOptionKind::AllowOnce
                    | agent_client_protocol::v1::schema::PermissionOptionKind::AllowAlways
            )
        };
        let allow = request
            .options
            .iter()
            .position(|option| allowed(option.kind))?;
        match self.approval_mode {
            ApprovalMode::Ask => None,
            ApprovalMode::Reads => matches!(
                request.kind,
                Some(ToolKind::Read | ToolKind::Search | ToolKind::Think)
            )
            .then_some(Some(allow)),
            ApprovalMode::All => Some(Some(allow)),
        }
    }

    pub fn paste(&mut self, text: &str) {
        if self.elicitations.front().is_some() {
            if let Some(pending) = self.elicitations.front_mut() {
                pending.state.insert(text);
            }
            return;
        }
        if self.providers.is_some() || !self.approvals.is_empty() || !self.mcp_consent.is_empty() {
            return;
        }
        if let Some(picker) = &mut self.picker {
            picker
                .query
                .extend(text.chars().filter(|ch| !ch.is_control()));
            picker.selected = 0;
            self.maybe_page_sessions();
            return;
        }
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
        self.session_title = None;
        self.usage = None;
        self.usage_cost = None;
        self.plan.clear();
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

    #[cfg(target_family = "wasm")]
    pub fn transcript_page_height(&self) -> f64 {
        f64::from(self.transcript_area.height.saturating_sub(2).max(1))
    }

    pub fn pointer_scroll(&mut self, position: Position, rows: i32) -> bool {
        if self.picker.is_some()
            || self.providers.is_some()
            || !self.transcript_area.contains(position)
        {
            return false;
        }
        self.transcript.scroll(rows);
        true
    }

    pub fn pointer_click(&mut self, position: Position) -> bool {
        if self.picker.is_some()
            || self.providers.is_some()
            || !self.transcript_area.contains(position)
        {
            return false;
        }
        self.transcript
            .toggle_disclosure_at(position.y - self.transcript_area.y)
    }

    pub fn key(&mut self, key: Key) {
        if self.mcp_consent.front().is_some() {
            self.mcp_consent_key(key);
            return;
        }
        if self.elicitations.front().is_some() {
            self.elicitation_key(key);
            return;
        }

        if !self.approvals.is_empty() {
            self.approval_key(key);
            return;
        }
        if self.providers.is_some() {
            self.provider_key(key);
            return;
        }
        if self.picker.is_some() {
            self.picker_key(key);
            return;
        }

        if self.completion.is_some() {
            match key {
                Key::Escape => {
                    self.completion = None;
                    return;
                }
                Key::Tab => {
                    self.accept_completion();
                    self.refresh_completion();
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
            Key::ScrollUp if let Some(scroll) = &mut self.todo_scroll => {
                *scroll = scroll.saturating_sub(3);
            }
            Key::ScrollDown if let Some(scroll) = &mut self.todo_scroll => {
                *scroll = scroll.saturating_add(3);
            }
            Key::Escape if self.todo_scroll.is_some() => {
                self.todo_open = false;
                self.todo_scroll = None;
            }
            Key::ScrollUp => self.transcript.scroll_page(false),
            Key::ScrollDown => self.transcript.scroll_page(true),
            Key::PreviousPrompt => self.transcript.jump_prompt(false),
            Key::NextPrompt => self.transcript.jump_prompt(true),
            Key::LatestOutput => self.transcript.follow(),
            Key::ToggleTools => self.transcript.toggle_tools(),
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
            Key::Escape | Key::Enter | Key::BackTab => {}
        }
        self.refresh_completion();
    }

    fn approval_key(&mut self, key: Key) {
        let Some(pending) = self.approvals.front_mut() else {
            return;
        };
        let count = pending.request.options.len();
        let answer = match key {
            Key::HistoryUp => {
                pending.selected = pending.selected.saturating_sub(1);
                None
            }
            Key::HistoryDown => {
                pending.selected = (pending.selected + 1).min(count.saturating_sub(1));
                None
            }
            Key::ScrollUp => {
                pending.detail_scroll = pending.detail_scroll.saturating_sub(3);
                None
            }
            Key::ScrollDown => {
                pending.detail_scroll = pending.detail_scroll.saturating_add(3);
                None
            }
            Key::Enter if pending.selected < count => Some(Some(pending.selected)),
            Key::Text(digit @ '1'..='9') => {
                let index = usize::from(digit as u8 - b'1');
                (index < count).then_some(Some(index))
            }
            Key::Escape => Some(None),
            _ => None,
        };
        if let Some(answer) = answer
            && let Some(pending) = self.approvals.pop_front()
        {
            let _ = pending.request.respond.send(answer);
        }
    }

    fn mcp_consent_key(&mut self, key: Key) {
        let decision = match key {
            Key::Escape | Key::Text('n') | Key::Text('N') | Key::Text('4') => {
                Some(ConsentDecision::Deny)
            }
            Key::Enter | Key::Text('y') | Key::Text('Y') | Key::Text('1') => {
                Some(ConsentDecision::Once)
            }
            Key::Text('2') => Some(ConsentDecision::Session),
            Key::Text('3') => Some(ConsentDecision::Always),
            _ => None,
        };
        if let Some(decision) = decision
            && let Some(pending) = self.mcp_consent.pop_front()
        {
            if matches!(decision, ConsentDecision::Session | ConsentDecision::Always) {
                self.mcp_grants.insert(pending.name.clone());
                self.transcript.push(
                    Role::Note,
                    format!("MCP {} approved until exit", pending.name),
                );
            }
            let _ = pending.respond.send(decision);
        }
    }

    fn elicitation_key(&mut self, key: Key) {
        enum Done {
            None,
            Decline,
            Accept(BTreeMap<String, ElicitationContentValue>),
        }
        let mut done = Done::None;
        if let Some(pending) = self.elicitations.front_mut() {
            match key {
                Key::Escape => done = Done::Decline,
                Key::Enter => {
                    if let Some(answers) = pending.state.enter() {
                        done = Done::Accept(answers);
                    }
                }
                Key::Backspace => pending.state.backspace(),
                Key::Tab | Key::Right => pending.state.move_field(true),
                Key::BackTab | Key::Left => pending.state.move_field(false),
                Key::HistoryDown | Key::ScrollDown => pending.state.move_choice(true),
                Key::HistoryUp | Key::ScrollUp => pending.state.move_choice(false),
                Key::Text(character) if !pending.state.choice_key(character) => {
                    pending.state.insert(character.encode_utf8(&mut [0; 4]));
                }
                _ => {}
            }
        }
        match done {
            Done::None => {}
            Done::Decline => {
                if let Some(pending) = self.elicitations.pop_front() {
                    let _ = pending.respond.send(None);
                }
            }
            Done::Accept(answers) => {
                if let Some(pending) = self.elicitations.pop_front() {
                    let _ = pending.respond.send(Some(answers));
                }
            }
        }
    }

    fn refresh_completion(&mut self) {
        let input = self.draft.text();
        self.completion = self
            .argument_suggestions(input)
            .or_else(|| suggestions(input, &self.skills))
            .map(|items| Completion { items, selected: 0 });
    }

    fn is_command(&self, command: &str) -> bool {
        is_local_command(command)
            || self
                .commands
                .iter()
                .any(|(name, _)| command.strip_prefix('/') == Some(name.as_str()))
    }

    fn has_unknown_command(&self, input: &str) -> bool {
        let Some(command) = slash_command(input) else {
            return false;
        };
        !self.is_command(command)
            || command == "/queue"
                && slash_command(input[command.len()..].trim_start())
                    .is_some_and(|queued| !self.is_command(queued))
    }

    fn argument_suggestions(&self, input: &str) -> Option<Vec<Suggestion>> {
        use agent_client_protocol::v1::schema::SessionConfigOptionCategory;

        let (command, prefix) = input.split_once(' ')?;
        let prefix = prefix.to_ascii_lowercase();
        let choices: Vec<(String, String)> = match command {
            "/theme" => theme::NAMES
                .iter()
                .map(|name| (name.to_string(), "Terminal theme".into()))
                .collect(),
            "/permissions" => [
                ("ask", "Ask before approving tools"),
                ("reads", "Approve read-only tools"),
                ("all", "Approve all tools"),
            ]
            .into_iter()
            .map(|(value, description)| (value.into(), description.into()))
            .collect(),
            "/provider" if self.provider_editor => [
                ("add", "Add a provider"),
                ("edit", "Edit a provider"),
                ("remove", "Remove a provider"),
            ]
            .into_iter()
            .map(|(value, description)| (value.into(), description.into()))
            .collect(),
            "/provider" => [
                ("set", "Set a provider route"),
                ("disable", "Disable a provider route"),
            ]
            .into_iter()
            .map(|(value, description)| (value.into(), description.into()))
            .collect(),
            "/effort" | "/model" | "/models" => {
                let category = if command == "/effort" {
                    SessionConfigOptionCategory::ThoughtLevel
                } else {
                    SessionConfigOptionCategory::Model
                };
                let option = self
                    .options
                    .iter()
                    .find(|option| crate::options::matches_category(option, &category))?;
                crate::options::choices(option)
                    .into_iter()
                    .map(|choice| {
                        (
                            choice.value.to_string(),
                            choice
                                .description
                                .clone()
                                .unwrap_or_else(|| choice.name.clone()),
                        )
                    })
                    .collect()
            }
            _ => return None,
        };
        let items: Vec<_> = choices
            .into_iter()
            .filter(|(value, _)| {
                !value.eq_ignore_ascii_case(&prefix)
                    && value.to_ascii_lowercase().starts_with(&prefix)
            })
            .map(|(value, description)| {
                let suffix = if command == "/provider" { " " } else { "" };
                Suggestion {
                    insert: format!("{command} {value}{suffix}"),
                    description,
                }
            })
            .collect();
        (!items.is_empty()).then_some(items)
    }

    fn update_plan(&mut self, plan: Vec<PlanEntry>) {
        if !plan.is_empty() && !self.todo_auto_opened {
            self.todo_auto_opened = true;
            self.todo_open = true;
        }
        self.plan = plan;
    }

    fn show_todos(&mut self) {
        self.todo_open = true;
        self.todo_scroll = Some(0);
    }

    pub fn toggle_sidebar(&mut self) {
        if self.todo_open {
            self.todo_open = false;
            self.todo_scroll = None;
            self.preferences.sidebar = false;
            self.save_preferences();
            return;
        }
        self.preferences.sidebar = !self.preferences.sidebar;
        self.save_preferences();
    }

    pub fn toggle_thoughts(&mut self) {
        self.preferences.show_thoughts = !self.preferences.show_thoughts;
        self.transcript
            .show_thoughts(self.preferences.show_thoughts);
        let shown = if self.preferences.show_thoughts {
            "shown"
        } else {
            "hidden"
        };
        self.transcript
            .push(Role::Note, format!("Reasoning is now {shown}"));
        self.save_preferences();
    }

    pub fn set_theme(&mut self, name: String) {
        self.theme.clone_from(&name);
        self.preferences.theme = Some(name);
        self.save_preferences();
    }

    pub fn set_approval_mode(&mut self, mode: ApprovalMode) {
        self.approval_mode = mode;
        self.preferences.permission_mode = Some(mode);
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

fn is_local_command(command: &str) -> bool {
    command == "/todos" || COMMANDS.iter().any(|(name, _)| *name == command)
}

fn slash_command(text: &str) -> Option<&str> {
    text.starts_with('/')
        .then(|| &text[..text.find(char::is_whitespace).unwrap_or(text.len())])
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
