mod rpc;
mod tools;
mod types;

use std::collections::{BTreeMap, HashMap};
#[cfg(not(target_family = "wasm"))]
use std::sync::{Arc, Mutex};

#[cfg(not(target_family = "wasm"))]
use agent_client_protocol::v1::schema::SessionConfigOptionCategory;
use agent_client_protocol::v1::schema::{
    CancelNotification, ClientCapabilities, CloseSessionRequest, CreateElicitationResponse,
    ElicitationCapabilities, ElicitationContentValue, ElicitationFormCapabilities,
    FileSystemCapabilities, Implementation, InitializeRequest, ListProvidersRequest,
    ListSessionsRequest, LoadSessionRequest, NewSessionRequest, RequestPermissionRequest,
    SessionId, SessionNotification, SetSessionConfigOptionRequest, StopReason,
};
use agent_client_protocol::v1::{ConnectionTo, SessionMessage};
use agent_client_protocol::{
    Agent, Client, Error, ErrorCode, schema::ProtocolVersion, util::MatchDispatch,
};
use futures::channel::oneshot;
use futures::stream::FuturesUnordered;
use futures::{FutureExt, StreamExt, select_biased};

#[cfg(not(target_family = "wasm"))]
use crate::client_io::ClientIo;
use crate::elicitation;
#[cfg(not(target_family = "wasm"))]
use crate::mcp_types::{AcpMcpRequest, AcpMcpResponse};

pub use rpc::request_error;
pub use tools::{ToolDisplay, status_label};
pub use types::{
    Action, Actions, ClientIoMode, ConnectTarget, Connection, Event, Events, ExecutionState,
    ExecutionStatus, LoginMethod, ModelChange, PermissionChoice, PermissionRequest, ProviderEdit,
    ProviderInfo, ProviderLoginState, Services, SessionActivity, SessionConnection, SessionPage,
    SessionTarget, session_activity,
};

use rpc::{delete_session, find_workspace, flush_prompts, needs_workspace, provider_call, respond};
#[cfg(not(target_family = "wasm"))]
use rpc::{
    exit_status, only_active_session, prune_for_quit, wait_for_terminal_exit, with_client_io,
    with_client_io_blocking,
};
use tools::{ToolState, text_update};
use types::ProviderList;

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

            let provider_editor = services.local_provider_management && initialized.meta.as_ref()
                .and_then(|meta| meta.get("hrns/providers/manage"))
                .and_then(serde_json::Value::as_bool)
                .unwrap_or(false);
            let managed_sessions = services.managed_sessions && initialized.meta.as_ref()
                .and_then(|meta| meta.get("hrns/session/activity"))
                .and_then(serde_json::Value::as_bool)
                .unwrap_or(false);
            let caps = initialized.agent_capabilities;
            let provider_routing = !provider_editor && caps.providers.is_some();
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
            let _ = events.unbounded_send(Event::Skills(skills));
            let execution = meta
                .and_then(|mut meta| meta.remove("hrns/session/state"))
                .and_then(|state| serde_json::from_value(state).ok());
            let _ = events.unbounded_send(Event::Ready {
                session: session.session_id().to_string(),
                approval_scope: services.approval_scope,
                workspace: cwd.clone(),
                remote: services.remote,
                client_io: services.client_io,
                attachments_supported: caps.prompt_capabilities.embedded_context,
                loaded,
                load_supported,
                close_supported,
                delete_supported,
                managed_sessions,
                provider_editor,
                provider_routing,
                execution,
            });

            let mut permissions = FuturesUnordered::new();
            let mut elicitations = FuturesUnordered::new();
            let mut mcp_jobs = FuturesUnordered::<futures::future::LocalBoxFuture<'static, Event>>::new();
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
                let mcp_job = async {
                    if mcp_jobs.is_empty() {
                        std::future::pending().await
                    } else {
                        mcp_jobs.next().await
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
                    event = mcp_job.fuse() => {
                        if let Some(event) = event {
                            let _ = events.unbounded_send(event);
                        }
                    }
                    action = actions.next().fuse() => match action {
                        #[cfg(not(target_family = "wasm"))]
                        Some(Action::McpAuthenticate { name, action }) => {
                            let provider = provider.clone();
                            mcp_jobs.push(async move {
                                let result = provider.authenticate(&name, action).await;
                                Event::McpAuthenticated { name, action, result }
                            }.boxed_local());
                        }
                        #[cfg(not(target_family = "wasm"))]
                        Some(Action::McpTools(name)) => {
                            let provider = provider.clone();
                            mcp_jobs.push(async move {
                                let result = provider.tools(&name).await;
                                Event::McpTools { name, result }
                            }.boxed_local());
                        }
                        Some(Action::Prompt(prompt)) => session.send_prompt_blocks(prompt.into_blocks())?,
                        Some(Action::Cancel) => {
                            session.connection().send_notification(
                                CancelNotification::new(session.session_id().clone())
                            )?;
                        }
                        Some(Action::SetOption { id, value, category, model_change }) => {
                            let mut request = SetSessionConfigOptionRequest::new(session.session_id().clone(), id, value);
                            if matches!(model_change, Some(ModelChange::Provider)) {
                                request.meta = Some(serde_json::Map::from_iter([(
                                    "hrns/model/automatic".into(), serde_json::Value::Bool(true),
                                )]));
                            }
                            let reply = session.connection().send_request(request).block_task().await;
                            let result = match reply {
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
                                    Ok(options)
                                }
                                Err(error) => Err(error.message),
                            };
                            let event = match (model_change, result) {
                                (Some(change), result) => Event::ModelSelected { change, result },
                                (None, Ok(options)) => Event::Options(options),
                                (None, Err(error)) => Event::Error(error),
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
                        Some(Action::PrepareSwitch(target)) => {
                            let result = flush_prompts(session.connection()).await
                                .map(|()| target).map_err(|error| request_error(&error));
                            let _ = events.unbounded_send(Event::SwitchPrepared(result));
                        }
                        #[cfg(not(target_family = "wasm"))]
                        Some(Action::CheckQuit) => {
                            let result = tokio::time::timeout(
                                std::time::Duration::from_secs(5),
                                only_active_session(session.connection(), session.session_id()),
                            ).await
                                .map_err(|_| "Timed out checking running sessions".to_owned())
                                .and_then(|result| result.map_err(|error| request_error(&error)));
                            let _ = events.unbounded_send(Event::QuitChecked(result));
                        }
                        #[cfg(not(target_family = "wasm"))]
                        Some(Action::StopForQuit) => {
                            let result = session.connection()
                                .send_request(CloseSessionRequest::new(session.session_id().clone()))
                                .block_task().await
                                .map(|_| ())
                                .map_err(|error| request_error(&error));
                            let _ = events.unbounded_send(Event::QuitStopped(result));
                        }
                        #[cfg(not(target_family = "wasm"))]
                        Some(Action::PruneForQuit { discard }) => {
                            let _ = tokio::time::timeout(
                                std::time::Duration::from_secs(5),
                                prune_for_quit(
                                    session.connection(),
                                    session.session_id(),
                                    &cwd,
                                    discard,
                                    delete_supported,
                                ),
                            ).await;
                            let _ = events.unbounded_send(Event::QuitDeleted);
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
                            let result = if delete_supported {
                                delete_session(session.connection(), id.clone()).await
                            } else {
                                Err("The agent does not advertise session/delete".into())
                            };
                            let _ = events.unbounded_send(Event::SessionDeleteResult { id, result });
                        }
                        Some(Action::ListProviderRoutes) if provider_routing => {
                            let result = session.connection()
                                .send_request(ListProvidersRequest::new())
                                .block_task()
                                .await
                                .map(|reply| reply.providers)
                                .map_err(|error| error.message);
                            let _ = events.unbounded_send(Event::ProviderRoutes(result));
                        }
                        Some(Action::SetProviderRoute(request)) if provider_routing => {
                            let event = match session.connection().send_request(request).block_task().await {
                                Ok(_) => Event::CommandStatus("Provider route updated".into()),
                                Err(error) => Event::Error(error.message),
                            };
                            let _ = events.unbounded_send(event);
                        }
                        Some(Action::DisableProviderRoute(request)) if provider_routing => {
                            let event = match session.connection().send_request(request).block_task().await {
                                Ok(_) => Event::CommandStatus("Provider route disabled".into()),
                                Err(error) => Event::Error(error.message),
                            };
                            let _ = events.unbounded_send(event);
                        }
                        Some(Action::ListProviders) if provider_editor => {
                            let result = provider_call::<ProviderList>(
                                session.connection(), "list", serde_json::json!({}),
                            ).await.map(|list| list.providers);
                            let _ = events.unbounded_send(Event::Providers(result));
                        }
                        Some(Action::SaveProvider(edit)) if provider_editor => {
                            let result = provider_call::<serde_json::Value>(
                                session.connection(), "save", edit,
                            ).await.map(|_| ());
                            let _ = events.unbounded_send(Event::ProviderChanged(result));
                        }
                        Some(Action::RemoveProvider(id)) if provider_editor => {
                            let result = provider_call::<serde_json::Value>(
                                session.connection(), "remove", serde_json::json!({ "id": id }),
                            ).await.map(|_| ());
                            let _ = events.unbounded_send(Event::ProviderChanged(result));
                        }
                        Some(Action::ProviderLogin { id, method, key }) if provider_editor => {
                            let result = provider_call(
                                session.connection(), "login",
                                serde_json::json!({ "id": id, "method": method, "key": key }),
                            ).await;
                            let _ = events.unbounded_send(Event::ProviderLogin(result));
                        }
                        Some(Action::ProviderLoginStatus) if provider_editor => {
                            let result = provider_call(
                                session.connection(), "login-status", serde_json::json!({}),
                            ).await;
                            let _ = events.unbounded_send(Event::ProviderLogin(result));
                        }
                        Some(Action::CancelProviderLogin) if provider_editor => {
                            let _ = provider_call::<serde_json::Value>(
                                session.connection(), "login-cancel", serde_json::json!({}),
                            ).await;
                            let _ = events.unbounded_send(Event::ProviderLoginCancelled);
                        }
                        Some(_) => {}
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
                                        let io = Arc::clone(&io);
                                        connection.spawn(async move {
                                            let result = with_client_io_blocking(io, move |io| {
                                                io.read(
                                                    &request.session_id.to_string(),
                                                    &request.path,
                                                    request.line,
                                                    request.limit,
                                                )
                                            })
                                            .await;
                                            let _ = match result {
                                                Ok(content) => responder.respond(
                                                    agent_client_protocol::v1::schema::ReadTextFileResponse::new(content),
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
                                .if_request(async |request: agent_client_protocol::v1::schema::WriteTextFileRequest, responder| {
                                    #[cfg(not(target_family = "wasm"))]
                                    {
                                        let io = Arc::clone(&io);
                                        connection.spawn(async move {
                                            let result = with_client_io_blocking(io, move |io| {
                                                io.write(
                                                    &request.session_id.to_string(),
                                                    &request.path,
                                                    &request.content,
                                                )
                                            })
                                            .await;
                                            let _ = match result {
                                                Ok(()) => responder.respond(
                                                    agent_client_protocol::v1::schema::WriteTextFileResponse::new(),
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
                                .if_request(async |request: agent_client_protocol::v1::schema::CreateTerminalRequest, responder| {
                                    #[cfg(not(target_family = "wasm"))]
                                    {
                                        let io = Arc::clone(&io);
                                        connection.spawn(async move {
                                            let result = with_client_io_blocking(io, move |io| {
                                                let env = request
                                                    .env
                                                    .iter()
                                                    .map(|variable| (variable.name.clone(), variable.value.clone()))
                                                    .collect::<Vec<_>>();
                                                io.create_terminal(
                                                    &request.session_id.to_string(),
                                                    &request.command,
                                                    &request.args,
                                                    &env,
                                                    request.cwd.as_deref(),
                                                    request.output_byte_limit,
                                                )
                                            })
                                            .await;
                                            let _ = match result {
                                                Ok(id) => responder.respond(
                                                    agent_client_protocol::v1::schema::CreateTerminalResponse::new(
                                                        agent_client_protocol::v1::schema::TerminalId::new(id),
                                                    ),
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
                                        let io = Arc::clone(&io);
                                        connection.spawn(async move {
                                            let result = with_client_io_blocking(io, move |io| {
                                                io.kill_terminal(
                                                    &request.session_id.to_string(),
                                                    &request.terminal_id.to_string(),
                                                )
                                            })
                                            .await;
                                            let _ = match result {
                                                Ok(()) => responder.respond(
                                                    agent_client_protocol::v1::schema::KillTerminalResponse::new(),
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
                                .if_request(async |request: agent_client_protocol::v1::schema::ReleaseTerminalRequest, responder| {
                                    #[cfg(not(target_family = "wasm"))]
                                    {
                                        let io = Arc::clone(&io);
                                        connection.spawn(async move {
                                            let result = with_client_io_blocking(io, move |io| {
                                                io.release_terminal(
                                                    &request.session_id.to_string(),
                                                    &request.terminal_id.to_string(),
                                                )
                                            })
                                            .await;
                                            let _ = match result {
                                                Ok(()) => responder.respond(
                                                    agent_client_protocol::v1::schema::ReleaseTerminalResponse::new(),
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
