use std::collections::{BTreeMap, HashMap, HashSet};
use std::path::PathBuf;

use agent_client_protocol::v1::schema::{
    CancelNotification, ClientCapabilities, CloseSessionRequest, ContentBlock,
    CreateElicitationResponse, DeleteSessionRequest, ElicitationCapabilities,
    ElicitationContentValue, ElicitationFormCapabilities, FileSystemCapabilities, Implementation,
    InitializeRequest, ListSessionsRequest, PermissionOptionKind, RequestPermissionOutcome,
    RequestPermissionRequest, RequestPermissionResponse, SelectedPermissionOutcome,
    SessionConfigId, SessionConfigOption, SessionConfigOptionValue, SessionId, SessionNotification,
    SessionUpdate, SetSessionConfigOptionRequest, StopReason, ToolCallContent, ToolCallStatus,
    ToolKind,
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
use crate::skills::Skill;

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

    fn reads(self) -> bool {
        matches!(self, Self::Read | Self::All)
    }

    fn writes(self) -> bool {
        matches!(self, Self::All)
    }

    fn terminal(self) -> bool {
        matches!(self, Self::All)
    }

    #[cfg(not(target_family = "wasm"))]
    fn native(self) -> client_io::Mode {
        match self {
            Self::Off => client_io::Mode::Off,
            Self::Read => client_io::Mode::Read,
            Self::All => client_io::Mode::All,
        }
    }
}

#[derive(Clone, Copy, Debug, Default)]
pub struct Services {
    pub client_io: ClientIoMode,
}

pub enum Action {
    Prompt(Prompt),
    Cancel,
    SetOption(SessionConfigId, SessionConfigOptionValue),
    ListSessions {
        more: bool,
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
}

pub struct PermissionChoice {
    pub name: String,
    pub kind: PermissionOptionKind,
}

pub struct PermissionRequest {
    pub title: String,
    pub kind: Option<ToolKind>,
    pub detail: String,
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
    },
    Execution(ExecutionState),
    Options(Vec<SessionConfigOption>),
    AgentCommands(Vec<(String, String)>),
    Skills(Vec<Skill>),
    SessionTitle(Option<String>),
    Mode(String),
    Providers(Vec<ProviderInfo>),
    LoadingAttachment,
    Attachment(Result<Box<Attachment>, String>),
    Plan(Vec<String>),
    Usage {
        used: u64,
        size: u64,
        cost: Option<String>,
    },
    SessionClosed(String),
    SessionDeleted(String),
    Tool {
        id: String,
        title: String,
        status: &'static str,
        detail: String,
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
    Disconnected,
    Permission(PermissionRequest),
    Elicitation {
        form: Form,
        respond: oneshot::Sender<Option<BTreeMap<String, ElicitationContentValue>>>,
    },
}

pub enum Connection {
    Reconnect,
    Switch(SessionTarget),
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
    #[cfg(not(target_family = "wasm"))] io: Arc<Mutex<ClientIo>>,
) -> Result<(), Error> {
    let notifications = events.clone();
    let elicit_events = events.clone();
    Client
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
        )
        .connect_with(transport, |connection: ConnectionTo<Agent>| async move {
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

            let caps = initialized.agent_capabilities;
            let loaded = target.session.is_some();
            let load_supported = caps.load_session;
            let close_supported = caps.session_capabilities.close.is_some();
            let delete_supported = caps.session_capabilities.delete.is_some();
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
                let setup = connection.load_session(id, &cwd).block_task().start_session().await?;
                (setup.session, cwd, setup.response.config_options, setup.response.meta)
            } else {
                let cwd = target.cwd.ok_or_else(needs_workspace)?;
                let setup = connection.build_session(&cwd).block_task().start_session().await?;
                (setup.session, cwd, setup.response.config_options, setup.response.meta)
            };
            #[cfg(not(target_family = "wasm"))]
            if let Ok(mut io) = io.lock() {
                io.mode = services.client_io.native();
                io.remember(&session.session_id().to_string(), &cwd);
            }
            let _ = events.unbounded_send(Event::Options(options.unwrap_or_default()));
            let skills = meta
                .as_ref()
                .map(crate::skills::catalog)
                .unwrap_or_default();
            let _ = events.unbounded_send(Event::Skills(skills));
            let _ = events.unbounded_send(Event::Ready {
                session: session.session_id().to_string(),
                workspace: cwd,
                attachments_supported: caps.prompt_capabilities.embedded_context,
                loaded,
                load_supported,
                close_supported,
                delete_supported,
            });
            if let Some(state) = meta.and_then(|mut meta| meta.remove("hrns/session/state"))
                .and_then(|state| serde_json::from_value(state).ok())
            {
                let _ = events.unbounded_send(Event::Execution(state));
            }

            let mut permissions = FuturesUnordered::new();
            let mut elicitations = FuturesUnordered::new();
            let mut tools: HashMap<String, ToolState> = HashMap::new();
            let mut session_cursor: Option<String> = None;
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
                        Some(Action::SetOption(id, value)) => {
                            let request = SetSessionConfigOptionRequest::new(session.session_id().clone(), id, value);
                            let reply = session.connection().send_request(request).block_task().await;
                            let event = match reply {
                                Ok(reply) => Event::Options(reply.config_options),
                                Err(error) => Event::Error(error.message),
                            };
                            let _ = events.unbounded_send(event);
                        }
                        Some(Action::ListSessions { more, cwd }) => {
                            let event = if caps.session_capabilities.list.is_none() {
                                Event::Error("The agent does not advertise session listing".into())
                            } else if more && session_cursor.is_none() {
                                Event::Note("No further session pages".into())
                            } else {
                                let cursor = if more { session_cursor.take() } else { None };
                                let reply = session.connection().send_request(
                                    ListSessionsRequest::new().cursor(cursor.clone()).cwd(cwd)
                                ).block_task().await;
                                match reply {
                                    Ok(page) => {
                                        session_cursor = page.next_cursor;
                                        let mut lines = page.sessions.into_iter().map(|item| {
                                            format!("{} · {} · {}", item.session_id, item.title.as_deref().unwrap_or("Untitled"), item.cwd.display())
                                        }).collect::<Vec<_>>();
                                        if lines.is_empty() {
                                            lines.push("No saved sessions".into());
                                        } else {
                                            lines.push("/load <id> to open one".into());
                                        }
                                        if session_cursor.is_some() {
                                            lines.push("/sessions next to see more".into());
                                        }
                                        Event::Note(lines.join("\n"))
                                    }
                                    Err(error) => {
                                        session_cursor = cursor;
                                        Event::Error(error.message)
                                    }
                                }
                            };
                            let _ = events.unbounded_send(event);
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
                            let event = list_providers(session.connection()).await;
                            let _ = events.unbounded_send(event);
                        }
                        Some(Action::SetProvider { id, api_type, base_url }) => {
                            let mut params = serde_json::Map::new();
                            params.insert("providerId".into(), id.into());
                            params.insert("apiType".into(), api_type.into());
                            if let Some(base_url) = base_url {
                                params.insert("baseUrl".into(), base_url.into());
                            }
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
                                    let mut state = ToolState::default();
                                    state.apply(&request.tool_call);
                                    let _ = events.unbounded_send(Event::Permission(PermissionRequest {
                                        title: request.tool_call.fields.title.clone().unwrap_or_else(|| "Tool call".into()),
                                        kind: request.tool_call.fields.kind,
                                        detail: state.detail(),
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
                                        let result = io.lock()
                                            .map_err(|_| Error::new(ErrorCode::INTERNAL_ERROR, "client I/O is unavailable"))
                                            .and_then(|io| {
                                                io.read(
                                                    &request.session_id.to_string(),
                                                    &request.path,
                                                    request.line,
                                                    request.limit,
                                                )
                                                .map_err(io_error)
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
                                        let result = io.lock()
                                            .map_err(|_| Error::new(ErrorCode::INTERNAL_ERROR, "client I/O is unavailable"))
                                            .and_then(|io| {
                                                io.write(&request.session_id.to_string(), &request.path, &request.content)
                                                    .map_err(io_error)
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
                                        let result = io.lock()
                                            .map_err(|_| Error::new(ErrorCode::INTERNAL_ERROR, "client I/O is unavailable"))
                                            .and_then(|mut io| {
                                                let env = request.env.iter().map(|variable| (variable.name.clone(), variable.value.clone())).collect::<Vec<_>>();
                                                let session_id = request.session_id.to_string();
                                                io.create_terminal(
                                                    &session_id,
                                                    &request.command,
                                                    &request.args,
                                                    &env,
                                                    request.cwd.as_deref(),
                                                    request.output_byte_limit,
                                                )
                                                .map_err(io_error)
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
                                        let result = io.lock()
                                            .map_err(|_| Error::new(ErrorCode::INTERNAL_ERROR, "client I/O is unavailable"))
                                            .and_then(|mut io| {
                                                let session_id = request.session_id.to_string();
                                                let terminal_id = request.terminal_id.to_string();
                                                io.terminal_output(&session_id, &terminal_id)
                                                    .map_err(io_error)
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
                                        let session_id = request.session_id.to_string();
                                        let terminal_id = request.terminal_id.to_string();
                                        loop {
                                            let result = io.lock()
                                                .map_err(|_| Error::new(ErrorCode::INTERNAL_ERROR, "client I/O is unavailable"))
                                                .and_then(|mut io| {
                                                    io.wait_for_exit(&session_id, &terminal_id)
                                                        .map_err(io_error)
                                                });
                                            match result {
                                                Ok(Some(exit)) => {
                                                    responder.respond(
                                                        agent_client_protocol::v1::schema::WaitForTerminalExitResponse::new(exit_status(exit)),
                                                    )?;
                                                    break;
                                                }
                                                Ok(None) => {
                                                    tokio::time::sleep(std::time::Duration::from_millis(25)).await;
                                                }
                                                Err(error) => {
                                                    responder.respond_with_error(error)?;
                                                    break;
                                                }
                                            }
                                        }
                                        Ok(())
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
                                        let result = io.lock()
                                            .map_err(|_| Error::new(ErrorCode::INTERNAL_ERROR, "client I/O is unavailable"))
                                            .and_then(|mut io| {
                                                let session_id = request.session_id.to_string();
                                                let terminal_id = request.terminal_id.to_string();
                                                io.kill_terminal(&session_id, &terminal_id)
                                                    .map_err(io_error)
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
                                        let result = io.lock()
                                            .map_err(|_| Error::new(ErrorCode::INTERNAL_ERROR, "client I/O is unavailable"))
                                            .and_then(|mut io| {
                                                let session_id = request.session_id.to_string();
                                                let terminal_id = request.terminal_id.to_string();
                                                io.release_terminal(&session_id, &terminal_id)
                                                    .map_err(io_error)
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
            #[cfg(not(target_family = "wasm"))]
            if let Ok(mut io) = io.lock() {
                io.close();
            }
            Ok(())
        })
        .await
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

async fn list_providers(connection: &ConnectionTo<Agent>) -> Event {
    let request = match UntypedMessage::new("providers/list", serde_json::json!({})) {
        Ok(request) => request,
        Err(error) => return Event::Error(error.message),
    };
    let reply = match connection.send_request(request).block_task().await {
        Ok(reply) => reply,
        Err(error) => {
            return Event::Error(format!(
                "The agent does not support provider routing: {}",
                error.message
            ));
        }
    };
    let Some(providers) = reply.get("providers").and_then(|value| value.as_array()) else {
        return Event::Error("The agent returned an invalid provider list".into());
    };
    Event::Providers(
        providers
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
            .collect(),
    )
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
        .map_err(|error| error.message)
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
            content_text(&chunk.content).map(Event::UserChunk)
        }
        SessionUpdate::AgentMessageChunk(chunk) => content_text(&chunk.content).map(Event::Chunk),
        SessionUpdate::AgentThoughtChunk(chunk) => content_text(&chunk.content).map(Event::Thought),
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
        SessionUpdate::Plan(plan) => Some(Event::Plan(
            plan.entries
                .into_iter()
                .map(|entry| format!("{:?} · {}", entry.status, entry.content))
                .collect(),
        )),
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

fn content_text(block: &ContentBlock) -> Option<String> {
    match block {
        ContentBlock::Text(text) => Some(text.text.clone()),
        ContentBlock::Image(_) => Some("[image]".into()),
        ContentBlock::Audio(_) => Some("[audio]".into()),
        ContentBlock::ResourceLink(link) => Some(format!("[resource] {}", link.name)),
        ContentBlock::Resource(_) => Some("[embedded resource]".into()),
        _ => None,
    }
}

#[derive(Default)]
struct ToolState {
    title: String,
    status: ToolCallStatus,
    kind: Option<ToolKind>,
    content: Vec<ToolCallContent>,
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
        let title = if self.title.is_empty() {
            "Tool call".to_owned()
        } else {
            self.title.clone()
        };
        Event::Tool {
            id: id.to_owned(),
            title,
            status: status_label(self.status),
            detail: self.detail(),
        }
    }

    fn detail(&self) -> String {
        let mut parts = Vec::new();
        if let Some(kind) = self.kind {
            parts.push(format!("{kind:?}"));
        }
        for content in &self.content {
            match content {
                ToolCallContent::Content(content) => {
                    if let Some(text) = content_text(&content.content) {
                        parts.push(text);
                    }
                }
                ToolCallContent::Diff(diff) => {
                    parts.push(diff.path.display().to_string());
                    if let Some(old) = &diff.old_text {
                        parts.extend(old.lines().map(|line| format!("- {line}")));
                    }
                    parts.extend(diff.new_text.lines().map(|line| format!("+ {line}")));
                }
                ToolCallContent::Terminal(terminal) => {
                    parts.push(format!("[terminal] {}", terminal.terminal_id));
                }
                _ => {}
            }
        }
        if let Some(input) = &self.raw_input {
            parts.push(format!("input: {input}"));
        }
        if let Some(output) = &self.raw_output {
            parts.push(format!("output: {output}"));
        }
        parts.join("\n").chars().take(4000).collect()
    }
}

fn status_label(status: ToolCallStatus) -> &'static str {
    match status {
        ToolCallStatus::Pending => "pending",
        ToolCallStatus::InProgress => "running",
        ToolCallStatus::Completed => "done",
        ToolCallStatus::Failed => "failed",
        _ => "unknown",
    }
}
