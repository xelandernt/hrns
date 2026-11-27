mod agents_actions;
mod agents_view;
mod commands;
mod completion;
mod events;
#[cfg(not(target_family = "wasm"))]
mod file_completion;
mod input;
mod mcp_view;
mod picker;
mod providers;
mod render;
mod sessions;

#[cfg(not(target_family = "wasm"))]
pub(crate) use render::draw_choice_list;

use std::collections::{BTreeMap, HashMap, HashSet, VecDeque};
use std::path::{Path, PathBuf};

use agent_client_protocol::v1::schema::{ElicitationContentValue, PlanEntry, SessionConfigOption};
use futures::channel::{mpsc, oneshot};
use ratatui::layout::Rect;

use crate::acp::{
    Action, AgentCommand, ClientIoMode, ConnectTarget, ExecutionState, PermissionRequest,
    SessionTarget,
};
use crate::attachments::{Attachment, Prompt};
use crate::composer::Composer;
use crate::elicitation::FormState;
#[cfg(not(target_family = "wasm"))]
use crate::mcp_types::AuthAction;
use crate::mcp_types::{ConsentDecision, Health};
use crate::preferences::{ApprovalKey, ApprovalMode, Preferences};
use crate::skills::Skill;
use crate::theme::{self, Palette};
use crate::transcript::Transcript;

use completion::{is_local_command, slash_command};

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

#[derive(Clone, Copy, PartialEq, Eq)]
enum SuggestionKind {
    LocalCommand,
    AgentCommand,
    Skill,
    #[cfg_attr(target_family = "wasm", allow(dead_code))]
    File,
}

struct Suggestion {
    insert: String,
    description: String,
    kind: SuggestionKind,
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
    ("/yolo", "Auto-approve tools and collapse agent actions"),
    ("/details", "Show or collapse agent actions"),
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

const DETAILS_CHOICES: [(&str, &str); 4] = [
    ("on", "On for this session"),
    ("off", "Off for this session"),
    ("always-on", "Always on"),
    ("always-off", "Always off"),
];

pub struct App {
    destination: String,
    workspace: Option<PathBuf>,
    remote: bool,
    session: Option<String>,
    session_title: Option<String>,
    mode: Option<String>,
    status: &'static str,
    command_status: Option<String>,
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
    commands: Vec<AgentCommand>,
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
        transcript.show_details(preferences.show_details);
        let theme = preferences
            .theme
            .clone()
            .or(startup.theme)
            .filter(|name| theme::NAMES.contains(&name.as_str()))
            .unwrap_or_else(|| theme::DEFAULT.into());
        Self {
            destination,
            workspace: None,
            remote: false,
            session: None,
            session_title: None,
            mode: None,
            status: "Connecting",
            command_status: None,
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

    #[cfg(not(target_family = "wasm"))]
    pub fn animating_actions(&self) -> bool {
        self.transcript.animating_actions()
    }
}
