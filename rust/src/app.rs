mod agents_actions;
mod agents_view;
mod commands;
#[cfg(not(target_family = "wasm"))]
mod file_completion;
mod mcp_view;
mod picker;
mod providers;
mod render;

#[cfg(not(target_family = "wasm"))]
pub(crate) use render::draw_choice_list;

use std::collections::{BTreeMap, HashMap, HashSet, VecDeque};
use std::path::{Path, PathBuf};

use agent_client_protocol::v1::schema::{
    ElicitationContentValue, PlanEntry, SessionConfigOption, SessionConfigOptionCategory, ToolKind,
};
use futures::channel::{mpsc, oneshot};
use ratatui::layout::{Position, Rect};

use crate::acp::{
    Action, ClientIoMode, ConnectTarget, Event, ExecutionState, ExecutionStatus, ModelChange,
    PermissionRequest, SessionTarget,
};
use crate::attachments::{Attachment, MAX_PROMPT_BYTES, Prompt};
use crate::composer::Composer;
use crate::elicitation::FormState;
#[cfg(not(target_family = "wasm"))]
use crate::mcp_types::AuthAction;
use crate::mcp_types::{ConsentDecision, Health};
use crate::options;
use crate::preferences::{ApprovalKey, ApprovalMode, Preferences};
use crate::skills::{self, Skill};
use crate::theme::{self, Palette};
use crate::transcript::{ManualApproval, Role, Transcript};

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
}

impl Default for Startup {
    fn default() -> Self {
        Self {
            client_io: ClientIoMode::Off,
            theme: None,
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
    EditQueuedPrompt,
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
    Agents,
    NewSession,
    Reconnect,
    Escape,
}

struct Suggestion {
    insert: String,
    description: String,
}

fn path_match_rank(path: &str, query: &str) -> Option<u8> {
    let name = path.rsplit('/').next()?;
    if path == query || name == query {
        return Some(0);
    }
    if name.starts_with(query) {
        return Some(1);
    }
    if path.contains(query) {
        return Some(2);
    }
    let mut path = path.chars();
    query
        .chars()
        .all(|ch| path.any(|candidate| candidate == ch))
        .then_some(3)
}

fn session_workspace_path(workspace: &Path, path: &str, remote: bool) -> PathBuf {
    if remote {
        PathBuf::from(path)
    } else {
        workspace.join(path)
    }
}

struct Completion {
    items: Vec<Suggestion>,
    selected: usize,
    file: Option<FileCompletion>,
}

struct FileCompletion {
    #[cfg(not(target_family = "wasm"))]
    range: std::ops::Range<usize>,
    status: Option<&'static str>,
}

#[derive(Clone, Copy)]
struct ScrollBar {
    area: Rect,
    /// The thumb's length in cells.
    thumb: u16,
    /// The furthest the transcript can scroll, in rows.
    max_top: usize,
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

#[derive(Default)]
struct SessionInput {
    draft: Composer,
    queue: VecDeque<Prompt>,
    attachments: Vec<Attachment>,
}

pub struct PendingMcpConsent {
    pub name: String,
    pub transport: String,
    pub target: String,
    pub respond: oneshot::Sender<ConsentDecision>,
    selected: usize,
}

const MCP_CONSENT_CHOICES: [(&str, ConsentDecision); 4] = [
    ("Allow once", ConsentDecision::Once),
    ("Allow for this connection", ConsentDecision::Session),
    ("Always allow", ConsentDecision::Always),
    ("Deny", ConsentDecision::Deny),
];

const COMMANDS: &[(&str, &str)] = &[
    ("/help", "List commands"),
    ("/agents", "Open the Agents view"),
    ("/sessions", "Open the Agents view"),
    ("/new", "Start a new session"),
    ("/load", "Load a session by id"),
    ("/close", "Close a session"),
    ("/delete", "Delete a saved session"),
    ("/models", "Search and select a model"),
    ("/model", "Select a model"),
    ("/effort", "Select reasoning effort"),
    ("/providers", "Browse providers"),
    ("/login", "Sign in to the selected local provider"),
    ("/provider", "Add, edit or remove a provider"),
    ("/permissions", "Show or set the approval mode"),
    ("/yolo", "Auto-approve tools in this session"),
    ("/settings", "Show or set terminal settings"),
    ("/theme", "Show or select a theme"),
    ("/mcp", "Show MCP server health"),
    ("/todo", "Show the current plan"),
    ("/find", "Find transcript text"),
    ("/copy", "Copy the latest reply"),
    ("/connect", "Connect to an ACP endpoint"),
    ("/reconnect", "Reconnect the transport"),
    ("/queue", "Queue a follow-up"),
    ("/edit-queue", "Edit the last queued message"),
    ("/clear-queue", "Clear the local queue"),
    ("/attach", "Stage a file"),
    ("/detach", "Remove a staged file"),
    ("/skills", "Browse and insert skills"),
    ("/sidebar", "Toggle the activity sidebar"),
    ("/thoughts", "Show or collapse reasoning"),
    ("/agent", "Run an advertised agent command"),
];

pub struct App {
    destination: String,
    workspace: Option<PathBuf>,
    remote: bool,
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
    managed_sessions: bool,
    session_inputs: HashMap<String, SessionInput>,
    provider_editor: bool,
    provider_routing: bool,
    loaded: bool,
    /// The current session produced a user turn, so quitting keeps it.
    prompted: bool,
    preparing_switch: bool,
    draft: Composer,
    transcript: Transcript,
    transcript_area: Rect,
    scroll_bar: Option<ScrollBar>,
    scroll_drag: Option<u16>,
    bar_hover: bool,
    follow_area: Option<Rect>,
    follow_hover: bool,
    follow_pressed: bool,
    approvals: VecDeque<PendingApproval>,
    elicitations: VecDeque<PendingElicitation>,
    mcp_consent: VecDeque<PendingMcpConsent>,
    mcp_grants: HashSet<String>,
    mcp_health: Vec<Health>,
    #[cfg(not(target_family = "wasm"))]
    mcp_authentication: Option<(String, AuthAction)>,
    local_effect: Option<LocalEffect>,
    options: Vec<SessionConfigOption>,
    commands: Vec<(String, String)>,
    skills: Vec<Skill>,
    completion: Option<Completion>,
    #[cfg(not(target_family = "wasm"))]
    file_search: file_completion::Search,
    plan: Vec<PlanEntry>,
    replay_plan: Option<Vec<PlanEntry>>,
    todo_open: bool,
    todo_auto_opened: bool,
    todo_scroll: Option<u16>,
    usage: Option<(u64, u64)>,
    usage_cost: Option<String>,
    last_usage_tokens: Option<u64>,
    reasoning_tokens: Option<u64>,
    providers: Option<providers::Dialog>,
    selected_provider: Option<providers::Selected>,
    model_change_pending: bool,
    picker: Option<picker::Picker>,
    session_request: u64,
    preferences: Preferences,
    theme: String,
    approval_mode: ApprovalMode,
    approval_preference: Option<ApprovalKey>,
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
        Self {
            destination,
            workspace: None,
            remote: false,
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
            managed_sessions: false,
            session_inputs: HashMap::new(),
            provider_editor: false,
            provider_routing: false,
            loaded: false,
            prompted: false,
            preparing_switch: false,
            draft: Composer::default(),
            transcript,
            transcript_area: Rect::default(),
            scroll_bar: None,
            scroll_drag: None,
            bar_hover: false,
            follow_area: None,
            follow_hover: false,
            follow_pressed: false,
            approvals: VecDeque::new(),
            elicitations: VecDeque::new(),
            mcp_consent: VecDeque::new(),
            mcp_grants: HashSet::new(),
            mcp_health: Vec::new(),
            #[cfg(not(target_family = "wasm"))]
            mcp_authentication: None,
            local_effect: None,
            options: Vec::new(),
            commands: Vec::new(),
            skills: Vec::new(),
            completion: None,
            #[cfg(not(target_family = "wasm"))]
            file_search: file_completion::Search::default(),
            plan: Vec::new(),
            replay_plan: None,
            todo_open: false,
            todo_auto_opened: false,
            todo_scroll: None,
            usage: None,
            usage_cost: None,
            last_usage_tokens: None,
            reasoning_tokens: None,
            providers: None,
            selected_provider: None,
            model_change_pending: false,
            picker: None,
            session_request: 0,
            preferences,
            theme,
            approval_mode: ApprovalMode::Ask,
            approval_preference: None,
            client_io: startup.client_io,
            activity: VecDeque::new(),
            actions,
        }
    }

    pub fn palette(&self) -> Palette {
        theme::palette(&self.theme)
    }

    pub fn event(&mut self, event: Event) {
        if matches!(
            &event,
            Event::UserChunk(_)
                | Event::Chunk(_)
                | Event::Thought(_)
                | Event::Tool { .. }
                | Event::Plan(_)
        ) {
            self.prompted = true;
        }
        match event {
            Event::Ready {
                session,
                approval_scope,
                workspace,
                remote,
                attachments_supported,
                loaded,
                load_supported,
                close_supported,
                delete_supported,
                managed_sessions,
                provider_editor,
                provider_routing,
                execution,
            } => {
                let key = ApprovalKey::new(&approval_scope, &session);
                self.approval_mode = if loaded {
                    key.load()
                } else {
                    ApprovalMode::Ask
                };
                self.approval_preference = Some(key);
                self.transcript.commit_replay();
                self.attachments_supported = attachments_supported;
                self.load_supported = load_supported;
                self.close_supported = close_supported;
                self.delete_supported = delete_supported;
                self.managed_sessions = managed_sessions;
                self.provider_editor = provider_editor;
                self.provider_routing = provider_routing;
                self.reasoning_tokens = None;
                self.loaded = loaded;
                if !loaded {
                    self.session_title = None;
                }
                self.workspace = Some(workspace);
                self.remote = remote;
                if self.session.as_deref() != Some(&session) {
                    self.selected_provider = None;
                    if self.managed_sessions {
                        self.restore_session_input(&session);
                    } else if self.session.is_some() {
                        self.clear_queue_for_switch();
                    }
                    self.plan.clear();
                    self.todo_auto_opened = false;
                    self.todo_scroll = self.todo_scroll.map(|_| 0);
                }
                if let Some(plan) = self.replay_plan.take() {
                    self.update_plan(plan);
                }
                self.session = Some(session);
                if let Some(state) = execution {
                    self.event(Event::Execution(state));
                }
                self.ready = true;
                self.status = "Connected";
                self.load_waiting_sessions();
                self.refresh_completion();
                self.activity.push_back("connected".into());
                self.trim_activity();
                if self.provider_model_missing() {
                    self.activate_provider_model();
                }
                self.update_execution();
                self.flush_queue();
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
            Event::ModelSelected { change, result } => self.model_selected(change, result),
            Event::Options(options) => {
                self.update_options(options);
                if self.ready
                    && (self.take_provider_login_refresh() || self.provider_model_missing())
                {
                    self.activate_provider_model();
                }
                self.flush_queue();
            }
            Event::Sessions { request_id, result } => self.sessions_received(request_id, result),
            Event::SwitchPrepared(result) => {
                self.preparing_switch = false;
                match result {
                    Ok(target) => self.connect_session(target),
                    Err(error) => self.transcript.push(Role::Error, error),
                }
            }
            #[cfg(not(target_family = "wasm"))]
            Event::QuitChecked(_) | Event::QuitStopped(_) | Event::QuitDeleted => {}
            Event::AgentCommands(commands) => self.commands = commands,
            Event::Skills(skills) => {
                self.skills = skills;
                if let Some(picker) = &mut self.picker
                    && matches!(picker.source, picker::Source::Skills)
                {
                    picker.selected = 0;
                }
                self.refresh_completion();
            }
            Event::SessionTitle(title) => self.session_title = title,
            Event::ReasoningTokens(tokens) => self.reasoning_tokens = tokens,
            Event::Mode(mode) => self.mode = Some(mode),
            Event::Providers(result) => self.provider_list(result),
            Event::ProviderRoutes(result) => self.provider_routes(result),
            Event::ProviderChanged(result) => self.provider_changed(result),
            Event::ProviderLogin(result) => self.provider_login(result),
            Event::ProviderLoginCancelled => self.provider_login_cancelled(),
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
            Event::Usage { context, cost } => {
                self.usage = context;
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
            Event::SessionDeleteResult { id, result } => self.session_delete_result(id, result),
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
            Event::Thought(text) => self.transcript.append_thought(text, self.turns > 0),
            Event::Note(text) => self.transcript.push(Role::Note, text),
            Event::Warning(text) => self.transcript.push(Role::Warning, text),
            Event::Error(text) => {
                if !self.ready {
                    self.transcript.cancel_replay();
                }
                self.transcript.push(Role::Error, text);
            }
            Event::End { interrupted } => {
                self.turns = self.turns.saturating_sub(1);
                self.activity.push_back(if interrupted {
                    "turn cancelled".into()
                } else {
                    "turn complete".into()
                });
                self.trim_activity();
                if self.turns == 0 {
                    self.clear_approvals();
                    self.elicitations.clear();
                    self.status = "Connected";
                    self.update_execution();
                    self.flush_queue();
                }
            }
            Event::Disconnected => {
                self.approval_mode = ApprovalMode::Ask;
                self.approval_preference = None;
                self.preparing_switch = false;
                self.model_change_pending = false;
                self.agents_delete_interrupted();
                self.sessions_received(
                    self.session_request,
                    Err("Disconnected from the agent".into()),
                );
                self.replay_plan = None;
                self.providers = None;
                if self.transcript.cancel_replay() {
                    self.transcript
                        .push(Role::Warning, "The session did not finish loading".into());
                }
                self.ready = false;
                self.turns = 0;
                self.execution = None;
                self.clear_approvals();
                self.elicitations.clear();
                self.mcp_consent.clear();
                self.status = "Disconnected";
                self.refresh_completion();
                self.activity.push_back("disconnected".into());
                self.trim_activity();
            }
            Event::ConnectionChanged(destination) => {
                self.approval_mode = ApprovalMode::Ask;
                self.approval_preference = None;
                self.mcp_grants.clear();
                self.destination = destination;
                self.workspace = None;
                self.session = None;
                self.session_title = None;
                self.mode = None;
                self.loaded = false;
                self.prompted = false;
                self.ready = false;
                self.transcript.begin_replay();
                self.replay_plan = None;
                self.plan.clear();
                self.todo_auto_opened = false;
                self.usage = None;
                self.usage_cost = None;
                self.last_usage_tokens = None;
                self.reasoning_tokens = None;
                self.options.clear();
                self.commands.clear();
                self.skills.clear();
                self.mcp_health.clear();
                #[cfg(not(target_family = "wasm"))]
                {
                    self.mcp_authentication = None;
                }
                self.load_supported = false;
                self.close_supported = false;
                self.delete_supported = false;
                self.managed_sessions = false;
                self.session_inputs.clear();
                self.provider_editor = false;
                self.provider_routing = false;
                self.providers = None;
                self.selected_provider = None;
                self.attachments_supported = false;
                self.clear_queue_for_switch();
                self.refresh_completion();
            }
            Event::Connecting => {
                #[cfg(not(target_family = "wasm"))]
                {
                    self.mcp_authentication = None;
                }
                self.approval_mode = ApprovalMode::Ask;
                self.approval_preference = None;
                self.model_change_pending = false;
                self.ready = false;
                self.prompted = false;
                self.turns = 0;
                self.execution = None;
                self.clear_approvals();
                self.elicitations.clear();
                self.mcp_consent.clear();
                if self.transcript.replaying() {
                    self.transcript.begin_replay();
                    self.session_title = None;
                    self.usage = None;
                    self.usage_cost = None;
                    self.last_usage_tokens = None;
                }
                self.status = "Reconnecting";
                self.refresh_completion();
            }
            #[cfg(not(target_family = "wasm"))]
            Event::SwitchReplayReset => {
                self.transcript.retry_replay();
                self.replay_plan = None;
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
                        .push(Role::Warning, format!("MCP consent · {name} ({transport})"));
                    self.mcp_consent.push_back(PendingMcpConsent {
                        name,
                        transport,
                        target,
                        respond,
                        selected: 0,
                    });
                }
            }
            Event::McpHealth(health) => {
                for item in &health {
                    if let Some(reason) = &item.startup_failure
                        && !self.mcp_health.iter().any(|previous| {
                            previous.name == item.name
                                && previous.startup_failure.as_ref() == Some(reason)
                        })
                    {
                        self.transcript.push(
                            Role::Error,
                            format!("MCP {} unavailable · {reason}", item.name),
                        );
                    }
                }
                self.mcp_health = health;
            }
            #[cfg(not(target_family = "wasm"))]
            Event::McpAuthenticated {
                name,
                action,
                result,
            } => {
                self.mcp_authentication_result(name, action, result);
            }
            #[cfg(not(target_family = "wasm"))]
            Event::McpTools { name, result } => self.mcp_tools_result(&name, result),
            Event::Permission(request) => self.request_permission(request),
            Event::Elicitation { form, respond } => {
                self.transcript
                    .push(Role::Warning, format!("Agent question · {}", form.message));
                self.elicitations.push_back(PendingElicitation {
                    state: FormState::new(form),
                    respond,
                });
            }
        }
    }

    fn request_permission(&mut self, request: PermissionRequest) {
        if self.ready
            && let Some(answer) = self.auto_permission(&request)
        {
            let _ = request.respond.send(answer);
            return;
        }
        self.transcript.show_pending_approval(&request);
        self.approvals.push_back(PendingApproval {
            request,
            selected: 0,
            detail_scroll: 0,
        });
    }

    fn clear_approvals(&mut self) {
        for pending in self.approvals.drain(..) {
            let request = pending.request;
            self.transcript
                .resolve_approval(&request.tool_call_id, &request.title, None);
        }
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
        if self.picker.as_ref().is_some_and(picker::Picker::is_agents) {
            self.agents_paste(text);
            return;
        }
        if self.elicitations.front().is_some() {
            if let Some(pending) = self.elicitations.front_mut() {
                pending.state.insert(text);
            }
            return;
        }
        if !self.approvals.is_empty() || !self.mcp_consent.is_empty() {
            return;
        }
        if let Some(dialog) = &mut self.providers {
            dialog.paste(text);
            return;
        }
        if self.picker.is_some() {
            self.paste_into_picker(text);
            return;
        }
        self.draft.paste(text);
        self.refresh_completion();
    }

    fn paste_into_picker(&mut self, text: &str) {
        if let Some(picker) = &mut self.picker {
            picker
                .query
                .extend(text.chars().filter(|ch| !ch.is_control()));
            picker.selected = 0;
            self.maybe_page_sessions();
        }
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
        if self.loaded || (self.load_supported && self.session.is_some()) {
            self.transcript.begin_replay();
        }
        self.status = "Reconnecting";
        self.local_effect = Some(LocalEffect::Reconnect);
    }

    pub fn new_session(&mut self) {
        self.new_session_at(None);
    }

    fn new_session_at(&mut self, path: Option<&str>) {
        if !self.prepare_session_switch() {
            return;
        }
        let Some(mut workspace) = self.workspace.clone() else {
            self.transcript.push(
                Role::Warning,
                "Connect once so the new session has a workspace".into(),
            );
            return;
        };
        if let Some(path) = path {
            if !self.managed_sessions {
                self.transcript.push(
                    Role::Warning,
                    "Selecting a workspace requires a Harness server".into(),
                );
                return;
            }
            workspace = session_workspace_path(&workspace, path, self.remote);
        }
        self.switch_session(SessionTarget {
            cwd: Some(workspace),
            session: None,
        });
    }

    pub fn load_session(&mut self, id: String) {
        self.load_session_at(id, None);
    }

    fn load_session_at(&mut self, id: String, cwd: Option<PathBuf>) {
        if self.session.as_deref() == Some(&id) && self.ready {
            return;
        }
        if !self.prepare_session_switch() {
            return;
        }
        if !self.load_supported {
            self.transcript.push(
                Role::Warning,
                "The agent does not advertise session/load".into(),
            );
            return;
        }
        self.switch_session(SessionTarget {
            cwd,
            session: Some(id),
        });
    }

    fn switch_session(&mut self, target: SessionTarget) {
        if self.managed_sessions && self.ready {
            self.preparing_switch = self
                .actions
                .unbounded_send(Action::PrepareSwitch(target))
                .is_ok();
        } else {
            self.connect_session(target);
        }
    }

    fn connect_session(&mut self, target: SessionTarget) {
        self.ready = false;
        self.status = "Connecting";
        self.transcript.begin_replay();
        self.local_effect = Some(LocalEffect::Switch(target));
    }

    fn can_switch(&self) -> bool {
        self.managed_sessions || (self.turns == 0 && !self.remote_busy())
    }

    fn prepare_session_switch(&mut self) -> bool {
        if let Some(message) = self.session_switch_blocker() {
            self.transcript.push(Role::Warning, message.into());
            false
        } else {
            true
        }
    }

    fn session_switch_blocker(&self) -> Option<&'static str> {
        if self.preparing_switch || (self.transcript.replaying() && !self.ready) {
            Some("Wait for the current session to connect")
        } else if self.attachment_reads > 0 {
            Some("Wait for the attachment to finish loading")
        } else if !self.can_switch() {
            Some("Stop the running turn before switching sessions")
        } else {
            None
        }
    }

    #[cfg(not(target_family = "wasm"))]
    pub fn local_attachment_path(&self, path: PathBuf) -> PathBuf {
        if !self.remote
            && let Some(workspace) = &self.workspace
        {
            return workspace.join(path);
        }
        path
    }

    fn restore_session_input(&mut self, session: &str) {
        let Some(previous) = self.session.as_ref() else {
            return;
        };
        let input = self.session_inputs.remove(session).unwrap_or_default();
        self.session_inputs.insert(
            previous.clone(),
            SessionInput {
                draft: std::mem::replace(&mut self.draft, input.draft),
                queue: std::mem::replace(&mut self.queue, input.queue),
                attachments: std::mem::replace(&mut self.attachments, input.attachments),
            },
        );
        self.completion = None;
    }

    #[cfg(not(target_family = "wasm"))]
    pub fn check_quit(&self) -> bool {
        self.managed_sessions
            && self.ready
            && self.actions.unbounded_send(Action::CheckQuit).is_ok()
    }

    #[cfg(not(target_family = "wasm"))]
    pub fn stop_for_quit(&self) -> bool {
        self.ready && self.actions.unbounded_send(Action::StopForQuit).is_ok()
    }

    /// Ask the worker to discard untitled sessions and the current one when it has no prompt.
    #[cfg(not(target_family = "wasm"))]
    pub fn prune_on_quit(&self) -> bool {
        if !self.ready || !self.delete_supported {
            return false;
        }
        let discard = if self.prompted {
            None
        } else {
            self.session.clone()
        };
        self.actions
            .unbounded_send(Action::PruneForQuit { discard })
            .is_ok()
    }

    pub fn close_session(&mut self, id: Option<String>) {
        if !self.ready {
            self.transcript
                .push(Role::Warning, "Connect before closing a session".into());
            return;
        }
        if !self.close_supported {
            self.transcript.push(
                Role::Warning,
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
        if self.session.as_deref() == Some(&id) && (self.turns > 0 || self.remote_busy()) {
            self.transcript.push(
                Role::Warning,
                "Stop the running turn before closing its session".into(),
            );
            return;
        }
        let _ = self.actions.unbounded_send(Action::CloseSession(id));
    }

    pub fn delete_session(&mut self, id: String) {
        if id.is_empty() {
            self.transcript
                .push(Role::Warning, "Use /delete <session id>".into());
            return;
        }
        if let Err(message) = self.request_session_delete(&id) {
            self.transcript.push(Role::Warning, message.into());
        }
    }

    fn session_delete_blocker(&self, id: &str) -> Option<&'static str> {
        if !self.ready {
            Some("Connect before deleting a session")
        } else if !self.delete_supported {
            Some("The agent does not advertise session/delete")
        } else if self.session.as_deref() == Some(id) && !self.can_switch() {
            Some("Stop the running turn before deleting its session")
        } else {
            None
        }
    }

    fn request_session_delete(&self, id: &str) -> Result<(), &'static str> {
        if let Some(message) = self.session_delete_blocker(id) {
            return Err(message);
        }
        self.actions
            .unbounded_send(Action::DeleteSession(id.to_owned()))
            .map_err(|_| "Disconnected from the agent")
    }

    fn session_delete_result(&mut self, id: String, result: Result<(), String>) {
        if let Err(error) = result {
            if !self.agents_delete_failed(&id, &error) {
                self.transcript.push(Role::Error, error);
            }
            return;
        }
        self.session_inputs.remove(&id);
        self.transcript
            .push(Role::Note, format!("Deleted session {id}"));
        let active = self.session.as_deref() == Some(&id);
        self.agents_delete_completed(&id, active);
        if active {
            self.new_session();
        }
    }

    #[cfg(target_family = "wasm")]
    pub fn transcript_page_height(&self) -> f64 {
        f64::from(self.transcript_area.height.saturating_sub(2).max(1))
    }

    pub fn pointer_scroll(&mut self, position: Position, rows: i32) -> bool {
        if self.picker.is_some()
            || self.providers.is_some()
            || !(self.transcript_area.contains(position)
                || self
                    .scroll_bar
                    .is_some_and(|bar| bar.area.contains(position)))
        {
            return false;
        }
        self.transcript.scroll(rows);
        true
    }

    /// Tracks what the pointer is over so the scrollbar thumb and follow control highlight.
    pub fn pointer_move(&mut self, position: Position) -> bool {
        let over_bar = self
            .scroll_bar
            .is_some_and(|bar| bar.area.contains(position));
        let over_follow = self.follow_area.is_some_and(|area| area.contains(position));
        if (over_bar, over_follow) == (self.bar_hover, self.follow_hover) {
            return false;
        }
        self.bar_hover = over_bar;
        self.follow_hover = over_follow;
        true
    }

    pub fn pointer_down(&mut self, position: Position) -> bool {
        if self.picker.is_some() || self.providers.is_some() {
            return false;
        }
        if self.follow_area.is_some_and(|area| area.contains(position)) {
            self.follow_pressed = true;
            return true;
        }
        let Some(bar) = self.scroll_bar.filter(|bar| bar.area.contains(position)) else {
            return self.transcript_area.contains(position)
                && self
                    .transcript
                    .toggle_disclosure_at(position.y - self.transcript_area.y);
        };
        let row = position.y.saturating_sub(bar.area.y);
        let start = self.thumb_start(bar);
        if row >= start && row < start.saturating_add(bar.thumb) {
            self.scroll_drag = Some(row - start);
        } else {
            // A press on the track centres the thumb on the pointer, then drags from there.
            let grab = bar.thumb / 2;
            self.transcript
                .scroll_to(self.scroll_top_at(bar, row, grab));
            let start = self.thumb_start(bar);
            self.scroll_drag = Some(row.saturating_sub(start).min(bar.thumb.saturating_sub(1)));
        }
        true
    }

    pub fn pointer_drag(&mut self, position: Position) -> bool {
        let (Some(bar), Some(grab)) = (self.scroll_bar, self.scroll_drag) else {
            return false;
        };
        let row = position.y.saturating_sub(bar.area.y);
        self.transcript
            .scroll_to(self.scroll_top_at(bar, row, grab));
        true
    }

    pub fn pointer_up(&mut self, position: Position) -> bool {
        let mut changed = self.scroll_drag.take().is_some();
        if std::mem::take(&mut self.follow_pressed)
            && self.follow_area.is_some_and(|area| area.contains(position))
        {
            self.transcript.follow();
            changed = true;
        }
        changed
    }

    fn thumb_start(&self, bar: ScrollBar) -> u16 {
        let travel = bar.area.height.saturating_sub(bar.thumb);
        if travel == 0 || bar.max_top == 0 {
            return 0;
        }
        let offset = self
            .transcript
            .top()
            .saturating_mul(usize::from(travel))
            .saturating_add(bar.max_top / 2)
            / bar.max_top;
        u16::try_from(offset).unwrap_or(travel).min(travel)
    }

    fn scroll_top_at(&self, bar: ScrollBar, row: u16, grab: u16) -> usize {
        let travel = bar.area.height.saturating_sub(bar.thumb);
        if travel == 0 {
            return 0;
        }
        let start = row.saturating_sub(grab).min(travel);
        (usize::from(start) * bar.max_top + usize::from(travel) / 2) / usize::from(travel)
    }

    pub fn key(&mut self, key: Key) {
        if self.agents_action_key(key) {
            return;
        }
        if matches!(key, Key::Agents) {
            if self.picker.as_ref().is_some_and(picker::Picker::is_agents) {
                self.picker = None;
            } else {
                self.show_sessions();
            }
            return;
        }
        if self.picker.as_ref().is_some_and(picker::Picker::is_agents) {
            self.picker_key(key);
            return;
        }
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

        if let Some(completion) = &self.completion {
            match key {
                Key::Escape => {
                    #[cfg(not(target_family = "wasm"))]
                    if let Some(file) = &completion.file {
                        self.file_search
                            .dismiss(file.range.clone(), self.draft.text());
                    }
                    self.completion = None;
                    return;
                }
                Key::Tab | Key::Enter
                    if matches!(key, Key::Tab)
                        || (completion.file.is_some() && !completion.items.is_empty()) =>
                {
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
            Key::EditQueuedPrompt if self.draft.text().is_empty() && !self.queue.is_empty() => {
                self.edit_queued_prompt();
            }
            Key::EditQueuedPrompt => self.draft.left(),
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
            Key::Escape
                if !self.transcript.following() && self.turns == 0 && !self.remote_busy() =>
            {
                self.transcript.follow()
            }
            Key::ScrollUp => self.transcript.scroll_page(false),
            Key::ScrollDown => self.transcript.scroll_page(true),
            Key::PreviousPrompt => self.transcript.jump_prompt(false),
            Key::NextPrompt => self.transcript.jump_prompt(true),
            Key::LatestOutput => self.transcript.follow(),
            Key::ToggleTools => self.transcript.toggle_tools(),
            Key::ToggleSidebar => self.toggle_sidebar(),
            Key::Agents | Key::NewSession => {}
            Key::Reconnect => self.reconnect(),
            Key::Escape
                if self.status != "Cancelling" && (self.turns > 0 || self.remote_busy()) =>
            {
                if self.actions.unbounded_send(Action::Cancel).is_ok() {
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
            use agent_client_protocol::v1::schema::PermissionOptionKind;

            let request = pending.request;
            let decision = answer.and_then(|index| match request.options[index].kind {
                PermissionOptionKind::AllowOnce | PermissionOptionKind::AllowAlways => {
                    Some(ManualApproval::Granted)
                }
                PermissionOptionKind::RejectOnce | PermissionOptionKind::RejectAlways => {
                    Some(ManualApproval::Rejected)
                }
                _ => None,
            });
            let decision = if request.respond.send(answer).is_ok() {
                decision
            } else {
                None
            };
            self.transcript
                .resolve_approval(&request.tool_call_id, &request.title, decision);
        }
    }

    fn mcp_consent_key(&mut self, key: Key) {
        let Some(pending) = self.mcp_consent.front_mut() else {
            return;
        };
        let decision = match key {
            Key::HistoryUp => {
                pending.selected = pending.selected.saturating_sub(1);
                None
            }
            Key::HistoryDown => {
                pending.selected = (pending.selected + 1).min(MCP_CONSENT_CHOICES.len() - 1);
                None
            }
            Key::Enter => Some(MCP_CONSENT_CHOICES[pending.selected].1),
            Key::Text(digit @ '1'..='4') => {
                Some(MCP_CONSENT_CHOICES[usize::from(digit as u8 - b'1')].1)
            }
            Key::Text('y' | 'Y') => Some(ConsentDecision::Once),
            Key::Escape | Key::Text('n' | 'N') => Some(ConsentDecision::Deny),
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
        #[cfg(not(target_family = "wasm"))]
        if let Some(completion) = self.file_completion() {
            self.completion = Some(completion);
            return;
        }

        let input = self.draft.text();
        self.completion = self
            .argument_suggestions(input)
            .or_else(|| suggestions(input, &self.skills))
            .map(|items| Completion {
                items,
                selected: 0,
                file: None,
            });
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
            "/provider" if self.provider_routing => [
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
        let state = if self.preferences.show_thoughts {
            "shown"
        } else {
            "collapsed"
        };
        self.transcript
            .push(Role::Note, format!("Reasoning is now {state}"));
        self.save_preferences();
    }

    pub fn set_theme(&mut self, name: String) {
        self.theme.clone_from(&name);
        self.preferences.theme = Some(name);
        self.save_preferences();
    }

    pub fn set_approval_mode(&mut self, mode: ApprovalMode) {
        let Some(key) = self.approval_preference.as_ref().filter(|_| self.ready) else {
            self.transcript.push(
                Role::Warning,
                "Open a session before changing its approval mode".into(),
            );
            return;
        };
        self.approval_mode = mode;
        self.transcript.push(
            Role::Note,
            format!("Session approval mode · {}", mode.label()),
        );
        if let Err(error) = key.save(mode) {
            self.transcript.push(
                Role::Warning,
                format!("Could not save this session's approval mode ({error})"),
            );
        }
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
            self.completion = Some(completion);
            return;
        };
        #[cfg(not(target_family = "wasm"))]
        if let Some(file) = completion.file {
            self.insert_file_reference(file.range, &suggestion.insert);
            return;
        }
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
        if len == 0 {
            return;
        }
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
        if !self.ready
            || self.turns > 0
            || self.remote_busy()
            || self.prompt_setup_message().is_some()
        {
            return;
        }
        if let Some(prompt) = self.queue.pop_front()
            && let Err(prompt) = self.send_prompt(prompt)
        {
            self.queue.push_front(prompt);
        }
    }

    fn clear_queue_for_switch(&mut self) {
        if !self.queue.is_empty() {
            self.queue.clear();
            self.transcript.push(
                Role::Note,
                "Cleared queued prompts for the previous session".into(),
            );
        }
    }

    fn update_options(&mut self, options: Vec<SessionConfigOption>) {
        if options::selected_model(&options).map(|choice| &choice.value)
            != options::selected_model(&self.options).map(|choice| &choice.value)
        {
            self.usage = None;
            self.last_usage_tokens = None;
        }
        self.options = options;
        self.refresh_completion();
        if let Some(picker) = &self.picker {
            let last = self.picker_rows(picker).len().saturating_sub(1);
            if let Some(picker) = &mut self.picker {
                picker.selected = picker.selected.min(last);
            }
        }
    }

    fn model_selected(
        &mut self,
        change: ModelChange,
        result: Result<Vec<SessionConfigOption>, String>,
    ) {
        self.model_change_pending = false;
        let options = match result {
            Ok(options) => options,
            Err(error) => {
                self.transcript.push(Role::Error, error);
                return;
            }
        };
        self.update_options(options);
        if let ModelChange::Explicit { choose_effort } = change {
            if self.provider_model_missing() {
                self.selected_provider = None;
            }
            if choose_effort
                && self.draft.text().is_empty()
                && self.picker.is_none()
                && self.providers.is_none()
            {
                self.choose_model_effort();
            }
        }
        self.flush_queue();
    }

    fn active_model_provider(&self) -> Option<&str> {
        options::selected_model(&self.options).and_then(options::model_provider)
    }

    fn provider_model_missing(&self) -> bool {
        self.selected_provider
            .as_ref()
            .is_some_and(|selected| self.active_model_provider() != Some(selected.id.as_str()))
    }

    fn prompt_setup_message(&self) -> Option<&str> {
        if !self.ready {
            return None;
        }
        if self.model_change_pending {
            return Some("Switching models…");
        }
        if !self.provider_editor {
            return None;
        }
        if self.provider_model_missing() {
            return Some(self.provider_setup_message().unwrap_or(
                "No model is active. Check /login or configure a model in /providers.",
            ));
        }
        let model = self
            .options
            .iter()
            .find(|option| options::matches_category(option, &SessionConfigOptionCategory::Model));
        match model {
            None => Some("Set up a provider and model to send messages."),
            Some(option) => option.meta.as_ref()?.get("hrns/prompt/setup")?.as_str(),
        }
    }

    fn prepare_prompt(&mut self, text: String) -> Option<Prompt> {
        if let Some(message) = self.prompt_setup_message() {
            self.transcript.push(Role::Warning, message.to_owned());
            if !self.model_change_pending {
                if self.provider_setup_message().is_some() {
                    self.show_provider_login();
                } else if self.selected_provider.is_none() {
                    self.show_providers();
                }
            }
            return None;
        }
        if self.attachment_reads > 0 {
            self.transcript.push(
                Role::Warning,
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
        if self.preparing_switch {
            return Err(prompt);
        }
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
        self.prompted = true;
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
