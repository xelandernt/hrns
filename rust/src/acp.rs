use std::path::PathBuf;

use agent_client_protocol::component::ConnectTo;
use agent_client_protocol::v1::schema::{
    CancelNotification, ContentBlock, ContentChunk, Implementation, InitializeRequest,
    RequestPermissionOutcome, RequestPermissionRequest, RequestPermissionResponse,
    SelectedPermissionOutcome, SessionNotification, SessionUpdate,
};
use agent_client_protocol::v1::{ConnectionTo, SessionMessage, SessionSetup};
use agent_client_protocol::{Agent, Client, Error, schema::ProtocolVersion, util::MatchDispatch};
use futures::channel::{mpsc, oneshot};
use futures::{FutureExt, StreamExt, select_biased};

pub enum Action {
    Prompt(String),
    Cancel,
}

pub enum Event {
    Ready,
    Chunk(String),
    Thought(String),
    Note(String),
    End,
    Error(String),
    Disconnected,
    Permission {
        title: String,
        options: Vec<String>,
        respond: oneshot::Sender<Option<usize>>,
    },
}

pub type Events = mpsc::UnboundedSender<Event>;
pub type Actions = mpsc::UnboundedReceiver<Action>;

pub async fn connect(
    transport: impl ConnectTo<Client> + 'static,
    cwd: PathBuf,
    mut actions: Actions,
    events: Events,
) -> Result<(), Error> {
    Client
        .v1()
        .name("hrns")
        .connect_with(transport, |connection: ConnectionTo<Agent>| async move {
            let info = Implementation::new("hrns", env!("CARGO_PKG_VERSION"));
            connection
                .send_request(InitializeRequest::new(ProtocolVersion::V1).client_info(info))
                .block_task()
                .await?;

            let lifecycle = connection.clone();
            connection
                .build_session(cwd)
                .block_task()
                .run_until(async |SessionSetup { mut session, .. }| {
                    let _ = events.unbounded_send(Event::Ready);

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
                                None => break,
                            },
                            update = session.read_update().fuse() => match update? {
                                SessionMessage::PromptResponse(result) => {
                                    if let Err(error) = result {
                                        let _ = events.unbounded_send(Event::Error(error.message.to_string()));
                                    }
                                    let _ = events.unbounded_send(Event::End);
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
                            _ = lifecycle.incoming_closed().fuse() => break,
                        }
                    }
                    Ok(())
                })
                .await
        })
        .await
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
        SessionUpdate::AgentMessageChunk(ContentChunk {
            content: ContentBlock::Text(text),
            ..
        }) => Some(Event::Chunk(text.text)),
        SessionUpdate::AgentThoughtChunk(ContentChunk {
            content: ContentBlock::Text(text),
            ..
        }) => Some(Event::Thought(text.text)),
        SessionUpdate::ToolCall(call) => Some(Event::Note(format!("Tool: {}", call.title))),
        _ => None,
    }
}
