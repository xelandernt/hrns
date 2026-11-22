use std::borrow::Cow;
use std::collections::{BTreeMap, HashMap, HashSet};
use std::path::PathBuf;

use agent_client_protocol::v1::schema::{
    CancelNotification, ClientCapabilities, CloseSessionRequest, ContentBlock,
    CreateElicitationResponse, DeleteSessionRequest, ElicitationCapabilities,
    ElicitationContentValue, ElicitationFormCapabilities, FileSystemCapabilities, Implementation,
    InitializeRequest, ListSessionsRequest, LoadSessionRequest, NewSessionRequest,
    PermissionOptionKind, RequestPermissionOutcome, RequestPermissionRequest,
    RequestPermissionResponse, SelectedPermissionOutcome, SessionConfigId, SessionConfigOption,
    SessionConfigOptionCategory, SessionConfigOptionValue, SessionId, SessionInfo,
    SessionNotification, SessionUpdate, SetSessionConfigOptionRequest, StopReason, ToolCallContent,
    ToolCallLocation, ToolCallStatus, ToolKind,
};
use agent_client_protocol::v1::{ConnectionTo, SessionMessage};
use agent_client_protocol::{
    Agent, Client, Error, ErrorCode, JsonRpcNotification, UntypedMessage, schema::ProtocolVersion,
    util::MatchDispatch,
};
use futures::channel::{mpsc, oneshot};
use futures::stream::FuturesUnordered;
use futures::{FutureExt, StreamExt, select_biased};
use serde::{Deserialize, Serialize};

use crate::attachments::{Attachment, Prompt};
use crate::elicitation::{self, Form};
#[cfg(not(target_family = "wasm"))]
use crate::mcp_types::{AcpMcpRequest, AcpMcpResponse};
use crate::mcp_types::{ConsentDecision, Health};
use crate::skills::Skill;
use crate::tool_input;

#[cfg(not(target_family = "wasm"))]
use std::sync::{Arc, Mutex};

#[cfg(not(target_family = "wasm"))]
use crate::client_io::{self, ClientIo};

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
#[derive(Clone, Debug, Default)]
pub struct Services {
    pub client_io: ClientIoMode,
    pub mcp_config: Option<PathBuf>,
    pub remote: bool,
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
        choose_effort: bool,
    },
    ListSessions {
        request_id: u64,
        cursor: Option<String>,
        cwd: Option<PathBuf>,
    },
    CloseSession(String),
    DeleteSession(String),
    ListProviders,
    SetProvider {
        id: String,
        api_type: String,
        base_url: Option<String>,
    },
    DisableProvider(String),
    EditProvider {
        id: String,
        create: bool,
        api_type: String,
        base_url: String,
        models: Option<Vec<String>>,
        api_key_env: Option<String>,
    },
    RemoveProvider(String),
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

#[derive(Clone, Debug)]
pub struct ProviderConfig {
    pub api_type: String,
    pub base_url: Option<String>,
}

#[derive(Clone, Debug)]
pub struct ProviderInfo {
    pub id: String,
    pub supported: Vec<String>,
    pub required: bool,
    pub current: Option<ProviderConfig>,
}

pub enum Event {
    Ready {
        session: String,
        workspace: PathBuf,
        attachments_supported: bool,
        loaded: bool,
        load_supported: bool,
        close_supported: bool,
        delete_supported: bool,
        provider_editor: bool,
        execution: Option<ExecutionState>,
    },
    Execution(ExecutionState),
    Options(Vec<SessionConfigOption>),
    ModelSelected(Vec<SessionConfigOption>),
    AgentCommands(Vec<(String, String)>),
    Skills {
        catalog: Vec<Skill>,
        remote: bool,
    },
    SessionTitle(Option<String>),
    Mode(String),
    Providers(Result<Vec<ProviderInfo>, String>),
    LoadingAttachment,
    Attachment(Result<Box<Attachment>, String>),
    Plan(Vec<agent_client_protocol::v1::schema::PlanEntry>),
    Usage {
        used: u64,
        size: u64,
        cost: Option<String>,
    },
    Sessions {
        request_id: u64,
        result: Result<SessionPage, String>,
    },
    SessionClosed(String),
    SessionDeleted(String),
    Tool {
        id: String,
        display: ToolDisplay,
    },
    UserChunk(String),
    Chunk(String),
    Thought(String),
    Note(String),
    End {
        interrupted: bool,
    },
    Error(String),
    Connecting,
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

pub type Events = mpsc::UnboundedSender<Event>;
pub type Actions = mpsc::UnboundedReceiver<Action>;

pub async fn connect(
    transport: impl agent_client_protocol::ConnectTo<Client> + 'static,
    target: SessionTarget,
    actions: &mut Actions,
    events: Events,
    services: Services,
) -> Result<(), Error> {
    let notifications = events.clone();
    let elicit_events = events.clone();
    #[cfg(not(target_family = "wasm"))]
    let provider = crate::mcp::Provider::default();
    #[cfg(not(target_family = "wasm"))]
    let request_provider = provider.clone();
    let client = Client
        .v1()
        .name("hrns")
        .on_receive_notification(
            async move |state: ExecutionState, _| {
                let _ = notifications.unbounded_send(Event::Execution(state));
                Ok(())
            },
            agent_client_protocol::on_receive_notification!(),
        )
        .on_receive_request(
            async move |request: agent_client_protocol::v1::schema::CreateElicitationRequest,
                        responder,
                        _cx| {
                if let Some(form) = elicitation::display(&request) {
                    let (sender, receiver) = oneshot::channel();
                    let _ = elicit_events.unbounded_send(Event::Elicitation {
                        form,
                        respond: sender,
                    });
                    let answers = receiver.await.ok().flatten();
                    if let Some(answers) = answers {
                        responder.respond(elicitation::accept(answers))
                    } else {
                        responder.respond(elicitation::decline())
                    }
                } else {
                    responder.respond(elicitation::decline())
                }
            },
            agent_client_protocol::on_receive_request!(),
        );
    #[cfg(not(target_family = "wasm"))]
    let client = client.on_receive_request(
        async move |request: AcpMcpRequest, responder, cx| {
            let provider = request_provider.clone();
            let connection = cx.clone();
            cx.spawn(async move {
                let cancellation = responder.cancellation();
                let response = provider.request(request.0, connection, cancellation).await;
                let _ = match response {
                    Ok(response) => responder.respond(AcpMcpResponse(response)),
                    Err(error) => responder.respond_with_error(error),
                };
                Ok(())
            })
        },
        agent_client_protocol::on_receive_request!(),
    );
    client.connect_with(transport, |connection: ConnectionTo<Agent>| async move {
            let info = Implementation::new("hrns", env!("CARGO_PKG_VERSION"));
            let capabilities = ClientCapabilities::new()
                .fs(
                    FileSystemCapabilities::new()
                        .read_text_file(services.client_io.reads())
                        .write_text_file(services.client_io.writes()),
                )
                .terminal(services.client_io.terminal())
                .elicitation(
                    ElicitationCapabilities::new().form(ElicitationFormCapabilities::new()),
                );
            let initialized = connection
                .send_request(
                    InitializeRequest::new(ProtocolVersion::V1)
                        .client_capabilities(capabilities)
                        .client_info(info),
                )
                .block_task()
                .await?;

            let provider_editor = initialized.meta.as_ref()
                .and_then(|meta| meta.get("hrns/providers/manage"))
                .and_then(serde_json::Value::as_bool)
                .unwrap_or(false);
            let caps = initialized.agent_capabilities;
            let loaded = target.session.is_some();
            let load_supported = caps.load_session;
            let close_supported = caps.session_capabilities.close.is_some();
            let delete_supported = caps.session_capabilities.delete.is_some();
            #[cfg(not(target_family = "wasm"))]
            let mcp_servers = provider.prepare(
                services.mcp_config.as_deref(),
                &events,
                caps.mcp_capabilities.acp,
            ).await;
            #[cfg(target_family = "wasm")]
            let mcp_servers: Vec<agent_client_protocol::v1::schema::McpServer> = Vec::new();
            let (mut session, cwd, options, meta) = if let Some(id) = target.session {
                if !caps.load_session {
                    return Err(Error::new(ErrorCode::INVALID_PARAMS, "The agent does not support loading sessions"));
                }
                let id = SessionId::new(id);
                let cwd = match target.cwd {
                    Some(cwd) => cwd,
                    None if caps.session_capabilities.list.is_some() => find_workspace(&connection, &id).await?,
                    None => return Err(needs_workspace()),
                };
                let setup = connection
                    .load_session_from(
                        LoadSessionRequest::new(id, &cwd).mcp_servers(mcp_servers.clone()),
                    )
                    .block_task()
                    .start_session()
                    .await?;
                (setup.session, cwd, setup.response.config_options, setup.response.meta)
            } else {
                let cwd = target.cwd.ok_or_else(needs_workspace)?;
                let setup = connection
                    .build_session_from(
                        NewSessionRequest::new(&cwd).mcp_servers(mcp_servers.clone()),
                    )
                    .block_task()
                    .start_session()
                    .await?;
                (setup.session, cwd, setup.response.config_options, setup.response.meta)
            };
            #[cfg(not(target_family = "wasm"))]
            let io = Arc::new(Mutex::new(ClientIo::new(
                services.client_io,
                session.session_id().to_string(),
                &cwd,
            )));
            let options = options.unwrap_or_default();
            #[cfg(not(target_family = "wasm"))]
            let options = if !loaded && let Some(profile) = &services.effort_profile {
                profile.restore(session.connection(), session.session_id(), options, false, &events).await
            } else {
                options
            };
            let _ = events.unbounded_send(Event::Options(options));
            let skills = meta
                .as_ref()
                .map(crate::skills::catalog)
                .unwrap_or_default();
            let _ = events.unbounded_send(Event::Skills {
                catalog: skills,
                remote: services.remote,
            });
            let execution = meta
                .and_then(|mut meta| meta.remove("hrns/session/state"))
                .and_then(|state| serde_json::from_value(state).ok());
            let _ = events.unbounded_send(Event::Ready {
                session: session.session_id().to_string(),
                workspace: cwd,
                attachments_supported: caps.prompt_capabilities.embedded_context,
                loaded,
                load_supported,
                close_supported,
                delete_supported,
                provider_editor,
                execution,
            });

            let mut permissions = FuturesUnordered::new();
            let mut elicitations = FuturesUnordered::new();
            let mut tools: HashMap<String, ToolState> = HashMap::new();
            loop {
                let permission = async {
                    if permissions.is_empty() {
                        std::future::pending().await
                    } else {
                        permissions.next().await
                    }
                };
                let elicitation_ready = async {
                    if elicitations.is_empty() {
                        std::future::pending::<
                            Option<(
                                agent_client_protocol::Responder<CreateElicitationResponse>,
                                Option<BTreeMap<String, ElicitationContentValue>>,
                            )>,
                        >()
                        .await
                    } else {
                        elicitations.next().await
                    }
                };
                select_biased! {
                    permission = permission.fuse() => {
                        if let Some((request, responder, option)) = permission {
                            respond(request, responder, option)?;
                        }
                    }
                    elicitation = elicitation_ready.fuse() => {
                        if let Some((responder, answers)) = elicitation {
                            if let Some(answers) = answers {
                                responder.respond(elicitation::accept(answers))?;
                            } else {
                                responder.respond(elicitation::decline())?;
                            }
                        }
                    }
                    action = actions.next().fuse() => match action {
                        Some(Action::Prompt(prompt)) => session.send_prompt_blocks(prompt.into_blocks())?,
                        Some(Action::Cancel) => {
                            session.connection().send_notification(
                                CancelNotification::new(session.session_id().clone())
                            )?;
                        }
                        Some(Action::SetOption { id, value, category, choose_effort }) => {
                            let request = SetSessionConfigOptionRequest::new(session.session_id().clone(), id, value);
                            let reply = session.connection().send_request(request).block_task().await;
                            let event = match reply {
                                Ok(reply) => {
                                    let options = reply.config_options;
                                    #[cfg(not(target_family = "wasm"))]
                                    let options = if let Some(profile) = &services.effort_profile {
                                        match category {
                                            SessionConfigOptionCategory::Model => profile.restore(session.connection(), session.session_id(), options, true, &events).await,
                                            SessionConfigOptionCategory::ThoughtLevel => {
                                                profile.remember(&options, &events).await;
                                                options
                                            }
                                            _ => options,
                                        }
                                    } else {
                                        options
                                    };
                                    #[cfg(target_family = "wasm")]
                                    let _ = category;
                                    if choose_effort { Event::ModelSelected(options) } else { Event::Options(options) }
                                }
                                Err(error) => Event::Error(error.message),
                            };
                            let _ = events.unbounded_send(event);
                        }
                        Some(Action::ListSessions { request_id, cursor, cwd }) => {
                            let result = if caps.session_capabilities.list.is_none() {
                                Err("The agent does not advertise session listing".into())
                            } else {
                                session.connection().send_request(
                                    ListSessionsRequest::new().cursor(cursor).cwd(cwd)
                                ).block_task().await.map(|page| SessionPage {
                                    items: page.sessions,
                                    cursor: page.next_cursor,
                                }).map_err(|error| error.message)
                            };
                            let _ = events.unbounded_send(Event::Sessions { request_id, result });
                        }
                        Some(Action::CloseSession(id)) => {
                            let event = if !close_supported {
                                Event::Error(
                                    "The agent does not advertise session/close".into(),
                                )
                            } else {
                                let reply = session
                                    .connection()
                                    .send_request(CloseSessionRequest::new(id.clone()))
                                    .block_task()
                                    .await;
                                match reply {
                                    Ok(_) => Event::SessionClosed(id),
                                    Err(error) => Event::Error(error.message),
                                }
                            };
                            let _ = events.unbounded_send(event);
                        }
                        Some(Action::DeleteSession(id)) => {
                            let event = if !delete_supported {
                                Event::Error(
                                    "The agent does not advertise session/delete".into(),
                                )
                            } else {
                                let reply = session
                                    .connection()
                                    .send_request(DeleteSessionRequest::new(id.clone()))
                                    .block_task()
                                    .await;
                                match reply {
                                    Ok(_) => Event::SessionDeleted(id),
                                    Err(error)
                                        if error.code == ErrorCode::RESOURCE_NOT_FOUND =>
                                    {
                                        Event::SessionDeleted(id)
                                    }
                                    Err(error) => Event::Error(error.message),
                                }
                            };
                            let _ = events.unbounded_send(event);
                        }
                        Some(Action::ListProviders) => {
                            let event = Event::Providers(list_providers(session.connection()).await);
                            let _ = events.unbounded_send(event);
                        }
                        Some(Action::SetProvider { id, api_type, base_url }) => {
                            let mut params = serde_json::Map::new();
                            params.insert("providerId".into(), id.into());
                            params.insert("apiType".into(), api_type.into());
                            params.insert("baseUrl".into(), base_url.unwrap_or_default().into());
                            let event = match provider_call(session.connection(), "providers/set", params.into()).await {
                                Ok(_) => Event::Note("Provider updated".into()),
                                Err(error) => Event::Error(error),
                            };
                            let _ = events.unbounded_send(event);
                        }
                        Some(Action::DisableProvider(id)) => {
                            let mut params = serde_json::Map::new();
                            params.insert("providerId".into(), id.into());
                            let event = match provider_call(session.connection(), "providers/disable", params.into()).await {
                                Ok(_) => Event::Note("Provider disabled".into()),
                                Err(error) => Event::Error(error),
                            };
                            let _ = events.unbounded_send(event);
                        }
                        Some(Action::EditProvider { id, create, api_type, base_url, models, api_key_env }) => {
                            let params = serde_json::json!({ "id": id, "create": create, "api_type": api_type,
                                "base_url": base_url, "models": models, "api_key_env": api_key_env });
                            let event = match provider_call(session.connection(), "_hrns/providers/save", params).await {
                                Ok(_) => Event::Note(if create { "Provider added".into() } else { "Provider saved".into() }),
                                Err(error) => Event::Error(error),
                            };
                            let _ = events.unbounded_send(event);
                        }
                        Some(Action::RemoveProvider(id)) => {
                            let params = serde_json::json!({ "id": id });
                            let event = match provider_call(session.connection(), "_hrns/providers/remove", params).await {
                                Ok(_) => Event::Note("Provider removed".into()),
                                Err(error) => Event::Error(error),
                            };
                            let _ = events.unbounded_send(event);
                        }
                        None => break,
                    },
                    update = session.read_update().fuse() => match update? {
                        SessionMessage::PromptResponse(result) => {
                            let interrupted = match result {
                                Ok(response) => response.stop_reason == StopReason::Cancelled,
                                Err(error) => {
                                    let _ = events.unbounded_send(Event::Error(error.message));
                                    true
                                }
                            };
                            let _ = events.unbounded_send(Event::End { interrupted });
                        }
                        SessionMessage::Message(dispatch) => {
                            MatchDispatch::new(dispatch.into_raw())
                                .if_notification(async |notification: SessionNotification| {
                                    if let Some(event) = text_update(notification.update, &mut tools) {
                                        let _ = events.unbounded_send(event);
                                    }
                                    Ok(())
                                })
                                .await
                                .if_request(async |request: RequestPermissionRequest, responder| {
                                    let (sender, receiver) = oneshot::channel();
                                    let tool_call_id = request.tool_call.tool_call_id.to_string();
                                    let mut state = tools.get(&tool_call_id).cloned().unwrap_or_default();
                                    state.apply(&request.tool_call);
                                    let mut display = state.display();
                                    let operation = if display.shell { "Run" } else { display.title.as_str() };
                                    let mut title = if display.subject.is_empty() { operation.to_owned() } else { format!("{operation} {}", display.subject) };
                                    title.retain(|ch| !ch.is_control());
                                    display.prepare_detail();
                                    let _ = events.unbounded_send(Event::Permission(PermissionRequest {
                                        tool_call_id,
                                        title,
                                        kind: request.tool_call.fields.kind,
                                        detail: display.detail,
                                        shell: display.shell,
                                        options: request.options.iter().map(|option| PermissionChoice {
                                            name: option.name.clone(),
                                            kind: option.kind,
                                        }).collect(),
                                        respond: sender,
                                    }));
                                    permissions.push(async move {
                                        (request, responder, receiver.await.ok().flatten())
                                    });
                                    Ok(())
                                })
                                .await
                                .if_request(async |request: agent_client_protocol::v1::schema::CreateElicitationRequest, responder| {
                                    if let Some(form) = elicitation::display(&request) {
                                        let (sender, receiver) = oneshot::channel();
                                        let _ = events.unbounded_send(Event::Elicitation {
                                            form,
                                            respond: sender,
                                        });
                                        elicitations.push(async move {
                                            (responder, receiver.await.ok().flatten())
                                        });
                                        Ok(())
                                    } else {
                                        responder.respond(elicitation::decline())
                                    }
                                })
                                .await
                                .if_request(async |request: agent_client_protocol::v1::schema::ReadTextFileRequest, responder| {
                                    #[cfg(not(target_family = "wasm"))]
                                    {
                                        let result = with_client_io(&io, |io| {
                                            io.read(
                                                &request.session_id.to_string(),
                                                &request.path,
                                                request.line,
                                                request.limit,
                                            )
                                        });
                                        match result {
                                            Ok(content) => responder.respond(agent_client_protocol::v1::schema::ReadTextFileResponse::new(content)),
                                            Err(error) => responder.respond_with_error(error),
                                        }
                                    }
                                    #[cfg(target_family = "wasm")]
                                    {
                                        let _ = request;
                                        responder.respond_with_error(Error::method_not_found())
                                    }
                                })
                                .await
                                .if_request(async |request: agent_client_protocol::v1::schema::WriteTextFileRequest, responder| {
                                    #[cfg(not(target_family = "wasm"))]
                                    {
                                        let result = with_client_io(&io, |io| {
                                            io.write(&request.session_id.to_string(), &request.path, &request.content)
                                        });
                                        match result {
                                            Ok(()) => responder.respond(agent_client_protocol::v1::schema::WriteTextFileResponse::new()),
                                            Err(error) => responder.respond_with_error(error),
                                        }
                                    }
                                    #[cfg(target_family = "wasm")]
                                    {
                                        let _ = request;
                                        responder.respond_with_error(Error::method_not_found())
                                    }
                                })
                                .await
                                .if_request(async |request: agent_client_protocol::v1::schema::CreateTerminalRequest, responder| {
                                    #[cfg(not(target_family = "wasm"))]
                                    {
                                        let result = with_client_io(&io, |io| {
                                            let env = request.env.iter().map(|variable| (variable.name.clone(), variable.value.clone())).collect::<Vec<_>>();
                                            io.create_terminal(
                                                &request.session_id.to_string(),
                                                &request.command,
                                                &request.args,
                                                &env,
                                                request.cwd.as_deref(),
                                                request.output_byte_limit,
                                            )
                                        });
                                        match result {
                                            Ok(id) => responder.respond(agent_client_protocol::v1::schema::CreateTerminalResponse::new(
                                                agent_client_protocol::v1::schema::TerminalId::new(id),
                                            )),
                                            Err(error) => responder.respond_with_error(error),
                                        }
                                    }
                                    #[cfg(target_family = "wasm")]
                                    {
                                        let _ = request;
                                        responder.respond_with_error(Error::method_not_found())
                                    }
                                })
                                .await
                                .if_request(async |request: agent_client_protocol::v1::schema::TerminalOutputRequest, responder| {
                                    #[cfg(not(target_family = "wasm"))]
                                    {
                                        let result = with_client_io(&io, |io| {
                                            io.terminal_output(&request.session_id.to_string(), &request.terminal_id.to_string())
                                        });
                                        match result {
                                            Ok((output, truncated, status)) => responder.respond(
                                                agent_client_protocol::v1::schema::TerminalOutputResponse::new(output, truncated)
                                                    .exit_status(status.map(exit_status)),
                                            ),
                                            Err(error) => responder.respond_with_error(error),
                                        }
                                    }
                                    #[cfg(target_family = "wasm")]
                                    {
                                        let _ = request;
                                        responder.respond_with_error(Error::method_not_found())
                                    }
                                })
                                .await
                                .if_request(async |request: agent_client_protocol::v1::schema::WaitForTerminalExitRequest, responder| {
                                    #[cfg(not(target_family = "wasm"))]
                                    {
                                        let io = Arc::clone(&io);
                                        connection.spawn(async move {
                                            let session_id = request.session_id.to_string();
                                            let terminal_id = request.terminal_id.to_string();
                                            let cancellation = responder.cancellation();
                                            let result = tokio::select! {
                                                biased;
                                                _ = cancellation.cancelled() => Err(Error::request_cancelled()),
                                                result = wait_for_terminal_exit(&io, &session_id, &terminal_id) => result,
                                            };
                                            let _ = match result {
                                                Ok(exit) => responder.respond(
                                                    agent_client_protocol::v1::schema::WaitForTerminalExitResponse::new(exit_status(exit)),
                                                ),
                                                Err(error) => responder.respond_with_error(error),
                                            };
                                            Ok(())
                                        })
                                    }
                                    #[cfg(target_family = "wasm")]
                                    {
                                        let _ = request;
                                        responder.respond_with_error(Error::method_not_found())
                                    }
                                })
                                .await
                                .if_request(async |request: agent_client_protocol::v1::schema::KillTerminalRequest, responder| {
                                    #[cfg(not(target_family = "wasm"))]
                                    {
                                        let result = with_client_io(&io, |io| {
                                            io.kill_terminal(&request.session_id.to_string(), &request.terminal_id.to_string())
                                        });
                                        match result {
                                            Ok(()) => responder.respond(agent_client_protocol::v1::schema::KillTerminalResponse::new()),
                                            Err(error) => responder.respond_with_error(error),
                                        }
                                    }
                                    #[cfg(target_family = "wasm")]
                                    {
                                        let _ = request;
                                        responder.respond_with_error(Error::method_not_found())
                                    }
                                })
                                .await
                                .if_request(async |request: agent_client_protocol::v1::schema::ReleaseTerminalRequest, responder| {
                                    #[cfg(not(target_family = "wasm"))]
                                    {
                                        let result = with_client_io(&io, |io| {
                                            io.release_terminal(&request.session_id.to_string(), &request.terminal_id.to_string())
                                        });
                                        match result {
                                            Ok(()) => responder.respond(agent_client_protocol::v1::schema::ReleaseTerminalResponse::new()),
                                            Err(error) => responder.respond_with_error(error),
                                        }
                                    }
                                    #[cfg(target_family = "wasm")]
                                    {
                                        let _ = request;
                                        responder.respond_with_error(Error::method_not_found())
                                    }
                                })
                                .await
                                .otherwise_ignore()?;
                        }
                        _ => {}
                    },
                    _ = connection.incoming_closed().fuse() => break,
                }
            }
            Ok(())
        })
        .await
}

#[cfg(not(target_family = "wasm"))]
async fn wait_for_terminal_exit(
    io: &Mutex<ClientIo>,
    session_id: &str,
    terminal_id: &str,
) -> Result<client_io::Exit, Error> {
    loop {
        let status = with_client_io(io, |io| io.wait_for_exit(session_id, terminal_id))?;
        if let Some(exit) = status {
            return Ok(exit);
        }
        tokio::time::sleep(std::time::Duration::from_millis(25)).await;
    }
}

#[cfg(not(target_family = "wasm"))]
fn with_client_io<T>(
    io: &Mutex<ClientIo>,
    operation: impl FnOnce(&mut ClientIo) -> Result<T, String>,
) -> Result<T, Error> {
    let mut io = io
        .lock()
        .map_err(|_| Error::new(ErrorCode::INTERNAL_ERROR, "client I/O is unavailable"))?;
    operation(&mut io).map_err(io_error)
}

#[cfg(not(target_family = "wasm"))]
fn io_error(message: String) -> Error {
    // The I/O service reports an unadvertised capability with this wording; map it
    // to the JSON-RPC method-not-found the ACP contract expects.
    if message.ends_with("is not advertised") {
        Error::method_not_found()
    } else {
        Error::new(ErrorCode::INVALID_PARAMS, message)
    }
}

#[cfg(not(target_family = "wasm"))]
fn exit_status(exit: client_io::Exit) -> agent_client_protocol::v1::schema::TerminalExitStatus {
    let status = agent_client_protocol::v1::schema::TerminalExitStatus::new();
    let status = match exit.code {
        Some(code) => status.exit_code(u32::try_from(code).unwrap_or(0)),
        None => status,
    };
    match exit.signal {
        Some(signal) => status.signal(signal),
        None => status,
    }
}

async fn list_providers(connection: &ConnectionTo<Agent>) -> Result<Vec<ProviderInfo>, String> {
    let request = UntypedMessage::new("providers/list", serde_json::json!({}))
        .map_err(|error| error.message)?;
    let reply = connection
        .send_request(request)
        .block_task()
        .await
        .map_err(|error| format!("Could not list providers: {}", error.message))?;
    let providers = reply
        .get("providers")
        .and_then(|value| value.as_array())
        .ok_or("The agent returned an invalid provider list")?;
    Ok(providers
        .iter()
        .filter_map(|provider| {
            let id = provider.get("providerId")?.as_str()?.to_owned();
            let supported = provider
                .get("supported")
                .and_then(|value| value.as_array())
                .map(|values| {
                    values
                        .iter()
                        .filter_map(|value| value.as_str().map(str::to_owned))
                        .collect()
                })
                .unwrap_or_default();
            let required = provider
                .get("required")
                .and_then(serde_json::Value::as_bool)
                .unwrap_or(false);
            let current = provider
                .get("current")
                .filter(|value| !value.is_null())
                .map(|current| ProviderConfig {
                    api_type: current
                        .get("apiType")
                        .and_then(|value| value.as_str())
                        .unwrap_or("custom")
                        .to_owned(),
                    base_url: current
                        .get("baseUrl")
                        .and_then(|value| value.as_str())
                        .map(str::to_owned),
                });
            Some(ProviderInfo {
                id,
                supported,
                required,
                current,
            })
        })
        .collect())
}

async fn provider_call(
    connection: &ConnectionTo<Agent>,
    method: &str,
    params: serde_json::Value,
) -> Result<(), String> {
    let request = UntypedMessage::new(method, &params).map_err(|error| error.message)?;
    connection
        .send_request(request)
        .block_task()
        .await
        .map(|_| ())
        .map_err(|error| {
            error
                .data
                .as_ref()
                .and_then(|data| data.get("reason"))
                .and_then(serde_json::Value::as_str)
                .map(str::to_owned)
                .unwrap_or(error.message)
        })
}

async fn find_workspace(
    connection: &ConnectionTo<Agent>,
    id: &SessionId,
) -> Result<PathBuf, Error> {
    let mut cursor = None;
    let mut visited = HashSet::new();
    loop {
        let page = connection
            .send_request(ListSessionsRequest::new().cursor(cursor))
            .block_task()
            .await?;
        if let Some(session) = page
            .sessions
            .into_iter()
            .find(|session| &session.session_id == id)
        {
            return Ok(session.cwd);
        }
        let Some(next) = page.next_cursor else {
            return Err(Error::new(
                ErrorCode::INVALID_PARAMS,
                "The agent did not list that session; provide its workspace with --cwd",
            ));
        };
        if !visited.insert(next.clone()) {
            return Err(Error::new(
                ErrorCode::INTERNAL_ERROR,
                "The agent returned a repeated session-list cursor",
            ));
        }
        cursor = Some(next);
    }
}

fn needs_workspace() -> Error {
    Error::new(
        ErrorCode::INVALID_PARAMS,
        "Provide the saved session workspace with --cwd or the browser cwd parameter",
    )
}

fn respond(
    request: RequestPermissionRequest,
    responder: agent_client_protocol::Responder<RequestPermissionResponse>,
    option: Option<usize>,
) -> Result<(), Error> {
    let outcome = option.and_then(|index| request.options.get(index)).map_or(
        RequestPermissionOutcome::Cancelled,
        |option| {
            RequestPermissionOutcome::Selected(SelectedPermissionOutcome::new(
                option.option_id.clone(),
            ))
        },
    );
    responder.respond(RequestPermissionResponse::new(outcome))
}

fn text_update(update: SessionUpdate, tools: &mut HashMap<String, ToolState>) -> Option<Event> {
    match update {
        SessionUpdate::UserMessageChunk(chunk) => {
            content_text(&chunk.content).map(|text| Event::UserChunk(text.into_owned()))
        }
        SessionUpdate::AgentMessageChunk(chunk) => {
            content_text(&chunk.content).map(|text| Event::Chunk(text.into_owned()))
        }
        SessionUpdate::AgentThoughtChunk(chunk) => {
            content_text(&chunk.content).map(|text| Event::Thought(text.into_owned()))
        }
        SessionUpdate::ConfigOptionUpdate(update) => Some(Event::Options(update.config_options)),
        SessionUpdate::AvailableCommandsUpdate(update) => Some(Event::AgentCommands(
            update
                .available_commands
                .into_iter()
                .map(|command| (command.name, command.description))
                .collect(),
        )),
        SessionUpdate::SessionInfoUpdate(update) => Some(Event::SessionTitle(update.title.take())),
        SessionUpdate::CurrentModeUpdate(update) => {
            Some(Event::Mode(update.current_mode_id.to_string()))
        }
        SessionUpdate::Plan(plan) => Some(Event::Plan(plan.entries)),
        SessionUpdate::UsageUpdate(usage) => Some(Event::Usage {
            used: usage.used,
            size: usage.size,
            cost: usage
                .cost
                .map(|cost| format!("{:.4} {}", cost.amount, cost.currency)),
        }),
        SessionUpdate::ToolCall(call) => {
            let id = call.tool_call_id.to_string();
            let state = ToolState {
                title: call.title,
                status: call.status,
                kind: Some(call.kind),
                content: call.content,
                locations: call.locations,
                raw_input: call.raw_input,
                raw_output: call.raw_output,
            };
            let event = state.event(&id);
            tools.insert(id, state);
            Some(event)
        }
        SessionUpdate::ToolCallUpdate(update) => {
            let id = update.tool_call_id.to_string();
            let state = tools.entry(id.clone()).or_default();
            state.apply(&update);
            Some(state.event(&id))
        }
        _ => None,
    }
}

fn content_text(block: &ContentBlock) -> Option<Cow<'_, str>> {
    match block {
        ContentBlock::Text(text) => Some(text.text.as_str().into()),
        ContentBlock::Image(_) => Some("[image]".into()),
        ContentBlock::Audio(_) => Some("[audio]".into()),
        ContentBlock::ResourceLink(link) => Some(format!("[resource] {}", link.name).into()),
        ContentBlock::Resource(_) => Some("[embedded resource]".into()),
        _ => None,
    }
}

pub struct ToolDisplay {
    pub title: String,
    pub subject: String,
    pub status: ToolCallStatus,
    pub detail: String,
    pub output: String,
    pub preview_output: bool,
    pub shell: bool,
}

impl ToolDisplay {
    pub fn prepare_detail(&mut self) -> Option<usize> {
        for text in [&mut self.detail, &mut self.output] {
            text.retain(|ch| matches!(ch, '\n' | '\t') || !ch.is_control());
            *text = text.trim_end().replace('\t', "    ");
        }
        if self.output.is_empty() {
            return None;
        }
        if self.shell && self.detail.is_empty() {
            self.detail = std::mem::take(&mut self.output);
            return Some(0);
        }
        if !self.detail.is_empty() {
            self.detail.push('\n');
        }
        self.detail.push_str("Output\n");
        let start = self.detail.len();
        self.detail.push_str(&self.output);
        Some(start)
    }
}

#[derive(Clone, Default)]
struct ToolState {
    title: String,
    status: ToolCallStatus,
    kind: Option<ToolKind>,
    content: Vec<ToolCallContent>,
    locations: Vec<ToolCallLocation>,
    raw_input: Option<serde_json::Value>,
    raw_output: Option<serde_json::Value>,
}

impl ToolState {
    fn apply(&mut self, update: &agent_client_protocol::v1::schema::ToolCallUpdate) {
        if let Some(title) = update.fields.title.clone() {
            self.title = title;
        }
        if let Some(status) = update.fields.status {
            self.status = status;
        }
        if let Some(kind) = update.fields.kind {
            self.kind = Some(kind);
        }
        if let Some(locations) = &update.fields.locations {
            self.locations.clone_from(locations);
        }
        if let Some(content) = update.fields.content.clone() {
            self.content = content;
        }
        if let Some(raw_input) = update.fields.raw_input.clone() {
            self.raw_input = Some(raw_input);
        }
        if let Some(raw_output) = update.fields.raw_output.clone() {
            self.raw_output = Some(raw_output);
        }
    }

    fn event(&self, id: &str) -> Event {
        Event::Tool {
            id: id.to_owned(),
            display: self.display(),
        }
    }

    fn display(&self) -> ToolDisplay {
        let decoded = self.raw_input.as_ref().map(tool_input::decode);
        let input = decoded.as_deref().unwrap_or(&serde_json::Value::Null);
        let has_diff = self
            .content
            .iter()
            .any(|part| matches!(part, ToolCallContent::Diff(_)));
        let presentation = tool_input::present(&self.title, input, has_diff);
        if presentation.is_none() && self.is_shell(input) {
            return self.shell_display(input);
        }
        let (title, subject, arguments) = if let Some(presentation) = presentation {
            (
                presentation.title.to_owned(),
                presentation.summary,
                presentation.detail,
            )
        } else {
            let (title, subject) = self.summary(input);
            let arguments = match input {
                serde_json::Value::Null => String::new(),
                serde_json::Value::Object(values) if values.is_empty() => String::new(),
                _ if input.get("truncated").and_then(serde_json::Value::as_bool) == Some(true) => {
                    tool_input::text(input, "preview").map_or_else(
                        || tool_input::pretty_value(input),
                        |preview| format!("… Input truncated\n{preview}"),
                    )
                }
                _ => tool_input::pretty_value(input),
            };
            (title, subject, arguments)
        };
        let (detail, output) = self.detail(arguments);
        ToolDisplay {
            title,
            subject,
            status: self.status,
            detail,
            output,
            preview_output: self.status == ToolCallStatus::Failed
                || matches!(self.kind, Some(ToolKind::Read | ToolKind::Search))
                || matches!(
                    self.title.as_str(),
                    "read_file"
                        | "read_text_file"
                        | "list_files"
                        | "list_directory"
                        | "glob"
                        | "grep"
                        | "search_files"
                        | "find_files"
                ),
            shell: false,
        }
    }

    fn is_shell(&self, input: &serde_json::Value) -> bool {
        matches!(
            self.title.as_str(),
            "shell" | "bash" | "terminal" | "execute" | "run_command"
        ) || (self.kind == Some(ToolKind::Execute)
            && (tool_input::text(input, "command").is_some() || self.title.starts_with("Run ")))
    }

    fn shell_display(&self, input: &serde_json::Value) -> ToolDisplay {
        let (_, fallback) = self.summary(input);
        let command = tool_input::text(input, "command").unwrap_or(&fallback);
        let mut arguments =
            tool_input::fields(input, &[("mode", "Mode"), ("timeout", "Timeout (seconds)")]);
        let summary = tool_input::preview(command);
        if summary != command {
            arguments = if arguments.is_empty() {
                format!("Command\n{command}")
            } else {
                format!("Command\n{command}\n{arguments}")
            };
        }
        let content: Vec<_> = self
            .content
            .iter()
            .filter_map(|part| match part {
                ToolCallContent::Content(content) => content_text(&content.content),
                ToolCallContent::Terminal(terminal) => {
                    Some(format!("Terminal {}", terminal.terminal_id).into())
                }
                _ => None,
            })
            .collect();
        let raw = self.raw_output.as_ref();
        let detail = if content.is_empty() {
            raw.map_or_else(String::new, tool_input::pretty_value)
        } else {
            content.join("\n")
        };
        let (output, exit_code) = shell_result(&detail);
        let status = if self.status == ToolCallStatus::Completed
            && exit_code.is_some_and(|code| code != 0)
        {
            ToolCallStatus::Failed
        } else {
            self.status
        };
        let title = match status {
            ToolCallStatus::Pending => "Queued",
            ToolCallStatus::InProgress => "Running",
            ToolCallStatus::Failed if exit_code.is_none() => "Failed",
            _ => "Ran",
        };
        let subject = match exit_code {
            Some(code) if code != 0 => format!("{summary}  (exit {code})"),
            _ => summary,
        };
        ToolDisplay {
            title: title.into(),
            subject,
            status,
            detail: arguments,
            output: output.to_owned(),
            preview_output: true,
            shell: true,
        }
    }

    fn summary(&self, arguments: &serde_json::Value) -> (String, String) {
        let verb = match self.kind {
            Some(ToolKind::Read) => "Read",
            Some(ToolKind::Edit) => "Edit",
            Some(ToolKind::Delete) => "Delete",
            Some(ToolKind::Move) => "Move",
            Some(ToolKind::Search) => "Search",
            Some(ToolKind::Execute) => "Run",
            Some(ToolKind::Think) => "Think",
            Some(ToolKind::Fetch) => "Fetch",
            Some(ToolKind::SwitchMode) => "Switch mode",
            _ => "",
        };
        let location = self.locations.first().map(|location| {
            let mut path = location.path.display().to_string();
            if let Some(line) = location.line {
                path.push_str(&format!(":{line}"));
            }
            if self.locations.len() > 1 {
                path.push_str(&format!(" (+{})", self.locations.len() - 1));
            }
            path
        });
        let input = [
            "command",
            "path",
            "file_path",
            "query",
            "pattern",
            "url",
            "target",
            "command_id",
        ]
        .into_iter()
        .find_map(|key| tool_input::text(arguments, key));
        let inferred = match self.title.as_str() {
            "read_file" | "read_text_file" => "Read",
            "write_file" | "write_text_file" | "edit_file" | "apply_patch" => "Edit",
            "list_files" | "list_directory" => "List",
            "grep" | "glob" | "search_files" | "find_files" => "Search",
            "shell" | "bash" | "terminal" | "execute" | "run_command" => "Run",
            _ => "",
        };
        let verb = if verb.is_empty() { inferred } else { verb };
        if verb.is_empty() {
            (
                if self.title.is_empty() {
                    "Tool call".into()
                } else {
                    self.title.clone()
                },
                location
                    .or_else(|| input.map(str::to_owned))
                    .unwrap_or_default(),
            )
        } else {
            let subject =
                if !self.title.is_empty() && inferred.is_empty() && self.title.contains(' ') {
                    let (leading, remainder) =
                        self.title.split_once(' ').unwrap_or((&self.title, ""));
                    if leading.eq_ignore_ascii_case(verb) {
                        remainder.to_owned()
                    } else {
                        self.title.clone()
                    }
                } else {
                    location
                        .or_else(|| input.map(str::to_owned))
                        .or_else(|| {
                            (inferred.is_empty()
                                && !self.title.is_empty()
                                && !self.title.eq_ignore_ascii_case(verb))
                            .then(|| self.title.clone())
                        })
                        .unwrap_or_default()
                };
            (verb.into(), subject)
        }
    }

    fn detail(&self, arguments: String) -> (String, String) {
        let mut changes = Vec::new();
        let mut output = Vec::new();
        for content in &self.content {
            match content {
                ToolCallContent::Content(content) => {
                    if let Some(text) = content_text(&content.content) {
                        output.push(text);
                    }
                }
                ToolCallContent::Diff(diff) => {
                    changes.push(diff.path.display().to_string());
                    if let Some(old) = &diff.old_text {
                        changes.extend(old.lines().map(|line| format!("- {line}")));
                    }
                    changes.extend(diff.new_text.lines().map(|line| format!("+ {line}")));
                }
                ToolCallContent::Terminal(terminal) => {
                    output.push(format!("Terminal {}", terminal.terminal_id).into());
                }
                _ => {}
            }
        }
        if output.is_empty()
            && let Some(raw) = self.raw_output.as_ref().filter(|v| !v.is_null())
        {
            output.push(tool_input::pretty_value(raw).into());
        }
        let detail = [
            (!arguments.is_empty()).then(|| format!("Arguments\n{arguments}")),
            (!changes.is_empty()).then(|| format!("Changes\n{}", changes.join("\n"))),
        ]
        .into_iter()
        .flatten()
        .collect::<Vec<_>>()
        .join("\n");
        (detail, output.join("\n"))
    }
}

// The workspace shell ends a completed result with its supervisor handles and exit record.
fn shell_result(text: &str) -> (&str, Option<i64>) {
    let text = text.trim_end();
    let Some((handles, exit)) = text.rsplit_once('\n') else {
        return (text, None);
    };
    let Some(exit) = serde_json::from_str::<serde_json::Value>(exit).ok() else {
        return (text, None);
    };
    let (output, trailer) = handles
        .rsplit_once("\nPID: ")
        .unwrap_or(("", handles.strip_prefix("PID: ").unwrap_or("")));
    let mut lines = trailer.lines();
    let pid = lines
        .next()
        .and_then(|line| line.split_once(" (supervisor; use `"))
        .and_then(|(pid, _)| pid.parse::<u64>().ok());
    if pid.is_some()
        && exit.get("pid").and_then(serde_json::Value::as_u64) == pid
        && lines
            .next()
            .is_some_and(|line| line.starts_with("Output: "))
        && lines
            .next()
            .is_some_and(|line| line.starts_with("Status: "))
        && lines.next().is_none()
        && let Some(code) = exit.get("exit_code").and_then(serde_json::Value::as_i64)
    {
        return (output, Some(code));
    }
    (text, None)
}

pub fn status_label(status: ToolCallStatus) -> &'static str {
    match status {
        ToolCallStatus::Pending => "pending",
        ToolCallStatus::InProgress => "running",
        ToolCallStatus::Completed => "done",
        ToolCallStatus::Failed => "failed",
        _ => "unknown",
    }
}
