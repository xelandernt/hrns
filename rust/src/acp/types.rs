use std::collections::BTreeMap;
use std::path::PathBuf;

use agent_client_protocol::JsonRpcNotification;
use agent_client_protocol::v1::schema::{
    DisableProviderRequest, ElicitationContentValue, PermissionOptionKind, SessionConfigId,
    SessionConfigOption, SessionConfigOptionCategory, SessionConfigOptionValue, SessionInfo,
    SetProviderRequest, ToolKind,
};
use futures::channel::{mpsc, oneshot};
use serde::{Deserialize, Serialize};

use crate::attachments::{Attachment, Prompt};
use crate::elicitation::Form;
#[cfg(not(target_family = "wasm"))]
use crate::mcp_types::AuthAction;
use crate::mcp_types::{ConsentDecision, Health};
use crate::skills::Skill;

use super::tools::ToolDisplay;

#[derive(Clone, Debug, Deserialize, Serialize, JsonRpcNotification)]
#[notification(method = "_hrns/session/state")]
#[serde(rename_all = "camelCase")]
pub struct ExecutionState {
    pub session_id: String,
    pub revision: u64,
    pub status: ExecutionStatus,
    #[serde(default)]
    pub active_turn_id: Option<String>,
    #[serde(default)]
    pub last_outcome: Option<String>,
    #[serde(default)]
    pub last_usage: Option<TurnUsage>,
}

#[derive(Clone, Copy, Debug, Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ExecutionStatus {
    Idle,
    Running,
    Waiting,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct SessionActivity {
    pub status: ExecutionStatus,
    pub attached_elsewhere: bool,
}

pub fn session_activity(info: &SessionInfo) -> Option<SessionActivity> {
    let activity = info.meta.as_ref()?.get("hrns/session/activity")?;
    SessionActivity::deserialize(activity).ok()
}

#[derive(Clone, Copy, Debug, Default, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct TurnUsage {
    #[serde(default)]
    pub input_tokens: Option<u64>,
    #[serde(default)]
    pub output_tokens: Option<u64>,
    #[serde(default)]
    pub total_tokens: Option<u64>,
    #[serde(default)]
    pub cached_read_tokens: Option<u64>,
    #[serde(default)]
    pub cached_write_tokens: Option<u64>,
}

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub enum ClientIoMode {
    #[default]
    Off,
    #[cfg_attr(target_family = "wasm", allow(dead_code))]
    Read,
    #[cfg_attr(target_family = "wasm", allow(dead_code))]
    All,
}

impl ClientIoMode {
    #[cfg_attr(target_family = "wasm", allow(dead_code))]
    pub fn parse(value: &str) -> Self {
        match value {
            "read" => Self::Read,
            "all" => Self::All,
            _ => Self::Off,
        }
    }

    pub fn label(self) -> &'static str {
        match self {
            Self::Off => "off",
            Self::Read => "read",
            Self::All => "all",
        }
    }

    pub(crate) fn reads(self) -> bool {
        matches!(self, Self::Read | Self::All)
    }

    pub(crate) fn writes(self) -> bool {
        matches!(self, Self::All)
    }

    pub(crate) fn terminal(self) -> bool {
        matches!(self, Self::All)
    }
}

#[cfg_attr(target_family = "wasm", allow(dead_code))]
#[derive(Clone, Default)]
pub struct Services {
    pub client_io: ClientIoMode,
    pub approval_scope: String,
    pub mcp_config: Option<PathBuf>,
    pub remote: bool,
    pub local_provider_management: bool,
    pub managed_sessions: bool,
    #[cfg(not(target_family = "wasm"))]
    pub effort_profile: Option<crate::effort::EffortProfile>,
}

pub enum Action {
    Prompt(Prompt),
    Cancel,
    SetOption {
        id: SessionConfigId,
        value: SessionConfigOptionValue,
        category: SessionConfigOptionCategory,
        model_change: Option<ModelChange>,
    },
    ListSessions {
        request_id: u64,
        cursor: Option<String>,
        cwd: Option<PathBuf>,
    },
    PrepareSwitch(SessionTarget),
    #[cfg(not(target_family = "wasm"))]
    CheckQuit,
    #[cfg(not(target_family = "wasm"))]
    StopForQuit,
    #[cfg(not(target_family = "wasm"))]
    PruneForQuit {
        discard: Option<String>,
    },
    CloseSession(String),
    DeleteSession(String),
    ListProviders,
    ListProviderRoutes,
    SetProviderRoute(SetProviderRequest),
    DisableProviderRoute(DisableProviderRequest),
    SaveProvider(ProviderEdit),
    RemoveProvider(String),
    ProviderLogin {
        id: String,
        method: LoginMethod,
        key: Option<String>,
    },
    ProviderLoginStatus,
    CancelProviderLogin,
    #[cfg(not(target_family = "wasm"))]
    McpAuthenticate {
        name: String,
        action: AuthAction,
    },
    #[cfg(not(target_family = "wasm"))]
    McpTools(String),
}

#[derive(Clone, Copy)]
pub enum ModelChange {
    Explicit { choose_effort: bool },
    Provider,
}

pub struct PermissionChoice {
    pub name: String,
    pub kind: PermissionOptionKind,
}

pub struct PermissionRequest {
    pub tool_call_id: String,
    pub title: String,
    pub kind: Option<ToolKind>,
    pub detail: String,
    pub shell: bool,
    pub options: Vec<PermissionChoice>,
    pub respond: oneshot::Sender<Option<usize>>,
}

#[derive(Deserialize)]
pub struct ProviderInfo {
    pub id: String,
    pub api_type: String,
    pub base_url: String,
    pub models: Vec<String>,
    pub aws_profile: Option<String>,
    pub aws_region: Option<String>,
    pub login_methods: Vec<LoginMethod>,
    pub setup_message: Option<String>,
}

#[derive(Deserialize)]
pub(super) struct ProviderList {
    pub(super) providers: Vec<ProviderInfo>,
}

#[derive(Serialize)]
pub struct ProviderEdit {
    pub id: String,
    pub create: bool,
    pub api_type: String,
    pub base_url: String,
    pub models: Option<Vec<String>>,
    pub aws_profile: Option<String>,
    pub aws_region: Option<String>,
}

#[derive(Clone, Copy, Deserialize, Serialize)]
#[serde(rename_all = "kebab-case")]
pub enum LoginMethod {
    ApiKey,
    Copilot,
    Codex,
    Aws,
    AwsSso,
}

impl LoginMethod {
    pub fn label(self) -> &'static str {
        match self {
            Self::ApiKey => "API key",
            Self::Copilot => "GitHub device login",
            Self::Codex => "Sign in with ChatGPT",
            Self::Aws => "Use the AWS credential chain",
            Self::AwsSso => "Sign in with AWS SSO",
        }
    }
}

#[derive(Deserialize)]
pub struct ProviderLoginState {
    #[serde(default)]
    pub done: bool,
    pub url: Option<String>,
    pub code: Option<String>,
    pub warning: Option<String>,
}

#[derive(Clone, Debug)]
pub struct AgentCommand {
    pub name: String,
    pub description: String,
    pub input_hint: Option<String>,
}

impl AgentCommand {
    pub fn hint_suffix(&self) -> String {
        self.input_hint
            .as_deref()
            .filter(|hint| !hint.is_empty())
            .map_or(String::new(), |hint| format!(" <{hint}>"))
    }

    pub fn description_with_hint(&self) -> String {
        match self.input_hint.as_deref().filter(|hint| !hint.is_empty()) {
            Some(hint) => format!("{}  <{hint}>", self.description),
            None => self.description.clone(),
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum CompactionPhase {
    InProgress,
    Completed,
    Failed,
    Cancelled,
}

#[derive(Clone, Debug)]
pub struct CompactionEvent {
    pub id: String,
    pub status: CompactionPhase,
    pub summary: Option<String>,
    pub clear_summary: bool,
    pub error: Option<String>,
    pub mode: Option<String>,
    pub tokens_before: Option<u64>,
    pub tokens_after: Option<u64>,
    pub messages_before: Option<u64>,
    pub messages_after: Option<u64>,
    pub focus: Option<String>,
}

pub enum Event {
    Ready {
        session: String,
        approval_scope: String,
        workspace: PathBuf,
        remote: bool,
        client_io: ClientIoMode,
        attachments_supported: bool,
        loaded: bool,
        load_supported: bool,
        close_supported: bool,
        delete_supported: bool,
        managed_sessions: bool,
        provider_editor: bool,
        provider_routing: bool,
        execution: Option<ExecutionState>,
    },
    Execution(ExecutionState),
    Options(Vec<SessionConfigOption>),
    ModelSelected {
        change: ModelChange,
        result: Result<Vec<SessionConfigOption>, String>,
    },
    AgentCommands(Vec<AgentCommand>),
    Compaction(CompactionEvent),
    CompactionSummaryChunk {
        id: String,
        text: String,
    },
    Skills(Vec<Skill>),
    SessionTitle(Option<String>),
    ReasoningTokens(Option<u64>),
    Mode(String),
    Providers(Result<Vec<ProviderInfo>, String>),
    ProviderRoutes(Result<Vec<agent_client_protocol::v1::schema::ProviderInfo>, String>),
    ProviderChanged(Result<(), String>),
    ProviderLogin(Result<ProviderLoginState, String>),
    ProviderLoginCancelled,
    LoadingAttachment,
    Attachment(Result<Box<Attachment>, String>),
    Plan(Vec<agent_client_protocol::v1::schema::PlanEntry>),
    Usage {
        context: Option<(u64, u64)>,
        cost: Option<String>,
    },
    Sessions {
        request_id: u64,
        result: Result<SessionPage, String>,
    },
    SwitchPrepared(Result<SessionTarget, String>),
    #[cfg(not(target_family = "wasm"))]
    QuitChecked(Result<bool, String>),
    #[cfg(not(target_family = "wasm"))]
    QuitStopped(Result<(), String>),
    #[cfg(not(target_family = "wasm"))]
    QuitDeleted,
    SessionClosed(String),
    SessionDeleteResult {
        id: String,
        result: Result<(), String>,
    },
    Tool {
        id: String,
        display: ToolDisplay,
    },
    UserChunk(String),
    Chunk(String),
    Thought(String),
    CommandStatus(String),
    Warning(String),
    End {
        interrupted: bool,
    },
    Error(String),
    Connecting,
    SwitchReplayReset,
    ConnectionChanged(String),
    Disconnected,
    Permission(PermissionRequest),
    Elicitation {
        form: Form,
        respond: oneshot::Sender<Option<BTreeMap<String, ElicitationContentValue>>>,
    },
    #[cfg_attr(target_family = "wasm", allow(dead_code))]
    McpConsent {
        name: String,
        transport: String,
        target: String,
        respond: oneshot::Sender<ConsentDecision>,
    },
    #[cfg_attr(target_family = "wasm", allow(dead_code))]
    McpHealth(Vec<Health>),
    #[cfg(not(target_family = "wasm"))]
    McpAuthenticated {
        name: String,
        action: AuthAction,
        result: Result<(), String>,
    },
    #[cfg(not(target_family = "wasm"))]
    McpTools {
        name: String,
        result: Result<Vec<String>, String>,
    },
}

pub struct SessionPage {
    pub items: Vec<SessionInfo>,
    pub cursor: Option<String>,
}

pub enum Connection {
    Reconnect,
    Switch(SessionTarget),
    Connect(ConnectTarget),
}

pub struct ConnectTarget {
    pub endpoint: String,
    pub cwd: Option<PathBuf>,
}

#[derive(Clone)]
pub struct SessionTarget {
    pub cwd: Option<PathBuf>,
    pub session: Option<String>,
}

pub struct SessionConnection {
    target: SessionTarget,
    previous: Option<SessionTarget>,
    warning: Option<String>,
    ready: bool,
}

impl SessionConnection {
    pub fn new(target: SessionTarget) -> Self {
        Self {
            target,
            previous: None,
            warning: None,
            ready: false,
        }
    }

    pub fn begin_attempt(&mut self) -> SessionTarget {
        self.ready = false;
        self.target.clone()
    }

    pub fn switch(&mut self, target: SessionTarget, can_restore: bool) {
        self.previous = (can_restore && self.ready && self.target.session.is_some())
            .then(|| self.target.clone());
        self.target = target;
    }

    pub fn forward(&mut self, event: Event, events: &Events) {
        if let Event::Ready {
            session,
            workspace,
            load_supported,
            ..
        } = &event
        {
            if *load_supported {
                self.target.cwd = Some(workspace.clone());
                self.target.session = Some(session.clone());
            }
            self.ready = true;
            self.previous = None;
        }
        let _ = events.unbounded_send(event);
        if self.ready
            && let Some(warning) = self.warning.take()
        {
            let _ = events.unbounded_send(Event::Warning(warning));
        }
    }

    /// Returns true when the previous session should be reattached immediately.
    pub fn finish(&mut self, result: Result<(), String>, events: &Events) -> bool {
        self.ready = false;
        if let Some(previous) = self.previous.take() {
            self.target = previous;
            let reason = result
                .err()
                .unwrap_or_else(|| "Disconnected before the session opened".into());
            self.warning = Some(format!("Could not switch sessions: {reason}"));
            let _ = events.unbounded_send(Event::SwitchReplayReset);
            return true;
        }
        if let Err(error) = result {
            let _ = events.unbounded_send(Event::Error(error));
        }
        let _ = events.unbounded_send(Event::Disconnected);
        if let Some(warning) = self.warning.take() {
            let _ = events.unbounded_send(Event::Warning(warning));
        }
        false
    }
}

pub type Events = mpsc::UnboundedSender<Event>;
pub type Actions = mpsc::UnboundedReceiver<Action>;
