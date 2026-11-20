use std::collections::HashSet;
use std::path::PathBuf;

use agent_client_protocol::component::ConnectTo;
use agent_client_protocol::v1::schema::{
    CancelNotification, CloseSessionRequest, ContentBlock, ContentChunk, DeleteSessionRequest,
    Implementation, InitializeRequest, ListSessionsRequest, RequestPermissionOutcome,
    RequestPermissionRequest, RequestPermissionResponse, SelectedPermissionOutcome,
    SessionConfigId, SessionConfigOption, SessionConfigOptionValue, SessionId, SessionNotification,
    SessionUpdate, SetSessionConfigOptionRequest, StopReason,
};
use agent_client_protocol::v1::{ConnectionTo, SessionMessage};
use agent_client_protocol::{
    Agent, Client, Error, ErrorCode, JsonRpcNotification, schema::ProtocolVersion,
    util::MatchDispatch,
};
use futures::channel::{mpsc, oneshot};
use futures::stream::FuturesUnordered;
use futures::{FutureExt, StreamExt, select_biased};
use serde::{Deserialize, Serialize};

use crate::attachments::{Attachment, Prompt};

#[derive(Clone, Debug, Deserialize, Serialize, JsonRpcNotification)]
#[notification(method = "_hrns/session/state")]
#[serde(rename_all = "camelCase")]
pub struct ExecutionState {
    pub session_id: String,
    pub revision: u64,
    pub status: ExecutionStatus,
}

#[derive(Clone, Copy, Debug, Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ExecutionStatus {
    Idle,
    Running,
    Waiting,
}

pub enum Action {
    Prompt(Prompt),
    Cancel,
    SetOption(SessionConfigId, SessionConfigOptionValue),
    ListSessions { more: bool },
    CloseSession(String),
    DeleteSession(String),
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
    LoadingAttachment,
    Attachment(Result<Box<Attachment>, String>),
    Plan(Vec<String>),
    Usage {
        used: u64,
        size: u64,
    },
    SessionClosed(String),
    SessionDeleted(String),
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
    Permission {
        title: String,
        options: Vec<String>,
        respond: oneshot::Sender<Option<usize>>,
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
    transport: impl ConnectTo<Client> + 'static,
    target: SessionTarget,
    actions: &mut Actions,
    events: Events,
) -> Result<(), Error> {
    let notifications = events.clone();
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
        .connect_with(transport, |connection: ConnectionTo<Agent>| async move {
            let info = Implementation::new("hrns", env!("CARGO_PKG_VERSION"));
            let initialized = connection
                .send_request(InitializeRequest::new(ProtocolVersion::V1).client_info(info))
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
            let _ = events.unbounded_send(Event::Options(options.unwrap_or_default()));
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

            let mut decisions = FuturesUnordered::new();
            let mut session_cursor: Option<String> = None;
            loop {
                let decision = async {
                    if decisions.is_empty() {
                        std::future::pending().await
                    } else {
                        decisions.next().await
                    }
                };
                select_biased! {
                    decision = decision.fuse() => {
                        if let Some((request, responder, option)) = decision {
                            respond(request, responder, option)?;
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
                        Some(Action::ListSessions { more }) => {
                            let event = if caps.session_capabilities.list.is_none() {
                                Event::Error("The agent does not advertise session listing".into())
                            } else if more && session_cursor.is_none() {
                                Event::Note("No further session pages".into())
                            } else {
                                let cursor = if more { session_cursor.take() } else { None };
                                let reply = session.connection().send_request(
                                    ListSessionsRequest::new().cursor(cursor.clone())
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
                                    Err(error) => Event::Error(error.message),
                                }
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
                                    if let Some(event) = text_update(notification.update) {
                                        let _ = events.unbounded_send(event);
                                    }
                                    Ok(())
                                })
                                .await
                                .if_request(async |request: RequestPermissionRequest, responder| {
                                    let (sender, receiver) = oneshot::channel();
                                    let _ = events.unbounded_send(Event::Permission {
                                        title: request.tool_call.fields.title.clone().unwrap_or_else(|| "Tool call".into()),
                                        options: request.options.iter().map(|option| option.name.clone()).collect(),
                                        respond: sender,
                                    });
                                    decisions.push(async move {
                                        (request, responder, receiver.await.ok().flatten())
                                    });
                                    Ok(())
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

fn text_update(update: SessionUpdate) -> Option<Event> {
    match update {
        SessionUpdate::UserMessageChunk(ContentChunk {
            content: ContentBlock::Text(text),
            ..
        }) => Some(Event::UserChunk(text.text)),
        SessionUpdate::AgentMessageChunk(ContentChunk {
            content: ContentBlock::Text(text),
            ..
        }) => Some(Event::Chunk(text.text)),
        SessionUpdate::AgentThoughtChunk(ContentChunk {
            content: ContentBlock::Text(text),
            ..
        }) => Some(Event::Thought(text.text)),
        SessionUpdate::ConfigOptionUpdate(update) => Some(Event::Options(update.config_options)),
        SessionUpdate::AvailableCommandsUpdate(update) => Some(Event::AgentCommands(
            update
                .available_commands
                .into_iter()
                .map(|command| (command.name, command.description))
                .collect(),
        )),
        SessionUpdate::Plan(plan) => Some(Event::Plan(
            plan.entries
                .into_iter()
                .map(|entry| format!("{:?} · {}", entry.status, entry.content))
                .collect(),
        )),
        SessionUpdate::UsageUpdate(usage) => Some(Event::Usage {
            used: usage.used,
            size: usage.size,
        }),

        SessionUpdate::ToolCall(call) => Some(Event::Note(format!("Tool: {}", call.title))),
        _ => None,
    }
}
