use std::collections::HashSet;
use std::path::PathBuf;

use agent_client_protocol::component::ConnectTo;
use agent_client_protocol::v1::schema::{
    CancelNotification, ContentBlock, ContentChunk, Implementation, InitializeRequest,
    ListSessionsRequest, RequestPermissionOutcome, RequestPermissionRequest,
    RequestPermissionResponse, SelectedPermissionOutcome, SessionConfigId, SessionConfigOption,
    SessionConfigOptionValue, SessionId, SessionNotification, SessionUpdate,
    SetSessionConfigOptionRequest, StopReason,
};
use agent_client_protocol::v1::{ConnectionTo, SessionMessage};
use agent_client_protocol::{
    Agent, Client, Error, ErrorCode, JsonRpcNotification, schema::ProtocolVersion,
    util::MatchDispatch,
};
use futures::channel::{mpsc, oneshot};
use futures::{FutureExt, StreamExt, select_biased};
use serde::{Deserialize, Serialize};

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
    Prompt(String),
    Cancel,
    SetOption(SessionConfigId, SessionConfigOptionValue),
}

pub enum Event {
    Ready {
        session: String,
        workspace: PathBuf,
    },
    Execution(ExecutionState),
    Options(Vec<SessionConfigOption>),
    UserChunk(String),
    Chunk(String),
    Thought(String),
    Note(String),
    End {
        interrupted: bool,
    },
    Error(String),
    Disconnected,
    Permission {
        title: String,
        options: Vec<String>,
        respond: oneshot::Sender<Option<usize>>,
    },
}

pub struct SessionTarget {
    pub cwd: Option<PathBuf>,
    pub session: Option<String>,
}

pub type Events = mpsc::UnboundedSender<Event>;
pub type Actions = mpsc::UnboundedReceiver<Action>;

pub async fn connect(
    transport: impl ConnectTo<Client> + 'static,
    target: SessionTarget,
    mut actions: Actions,
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
            });
            if let Some(state) = meta.and_then(|mut meta| meta.remove("hrns/session/state"))
                .and_then(|state| serde_json::from_value(state).ok())
            {
                let _ = events.unbounded_send(Event::Execution(state));
            }

            let mut decisions: Option<oneshot::Receiver<Option<usize>>> = None;
            let mut permission = None;
            loop {
                let decision = async {
                    match decisions.as_mut() {
                        Some(receiver) => receiver.await,
                        None => std::future::pending().await,
                    }
                };
                select_biased! {
                    decision = decision.fuse() => {
                        if let Some((request, responder)) = permission.take() {
                            respond(request, responder, decision.ok().flatten())?;
                        }
                        decisions = None;
                    }
                    action = actions.next().fuse() => match action {
                        Some(Action::Prompt(prompt)) => session.send_prompt(prompt)?,
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
                                    if permission.is_some() {
                                        return respond(request, responder, None);
                                    }
                                    let (sender, receiver) = oneshot::channel();
                                    let _ = events.unbounded_send(Event::Permission {
                                        title: request.tool_call.fields.title.clone().unwrap_or_else(|| "Tool call".into()),
                                        options: request.options.iter().map(|option| option.name.clone()).collect(),
                                        respond: sender,
                                    });
                                    decisions = Some(receiver);
                                    permission = Some((request, responder));
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
            return Err(needs_workspace());
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
        SessionUpdate::ToolCall(call) => Some(Event::Note(format!("Tool: {}", call.title))),
        _ => None,
    }
}
