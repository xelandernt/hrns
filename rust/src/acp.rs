mod actions;
mod messages;
mod rpc;
mod tools;
mod types;

use std::collections::{BTreeMap, HashMap};
use std::future::Future;
use std::path::PathBuf;
#[cfg(not(target_family = "wasm"))]
use std::sync::{Arc, Mutex};

use agent_client_protocol::v1::schema::{
    AgentCapabilities, ClientCapabilities, ClientSessionCapabilities, CompactionCapabilities,
    CreateElicitationRequest, CreateElicitationResponse, ElicitationCapabilities,
    ElicitationContentValue, ElicitationFormCapabilities, FileSystemCapabilities, Implementation,
    InitializeRequest, InitializeResponse, LoadSessionRequest, McpServer, NewSessionRequest,
    RequestPermissionRequest, RequestPermissionResponse, SessionConfigOption, SessionId,
};
use agent_client_protocol::v1::{ActiveSession, ConnectionTo};
use agent_client_protocol::{Agent, Client, Error, ErrorCode, Responder, schema::ProtocolVersion};
use futures::channel::oneshot;
use futures::future::LocalBoxFuture;
use futures::stream::FuturesUnordered;
use futures::{FutureExt, StreamExt, select_biased};
use serde_json::{Map, Value};

#[cfg(not(target_family = "wasm"))]
use crate::client_io::ClientIo;
use crate::elicitation;
#[cfg(not(target_family = "wasm"))]
use crate::mcp_types::{AcpMcpRequest, AcpMcpResponse};

pub use rpc::request_error;
pub use tools::{ToolDisplay, status_label};
pub use types::{
    Action, Actions, AgentCommand, ClientIoMode, CompactionEvent, CompactionPhase, ConnectTarget,
    Connection, Event, Events, ExecutionState, ExecutionStatus, LoginMethod, ModelChange,
    PermissionChoice, PermissionRequest, ProviderEdit, ProviderInfo, ProviderLoginState, Services,
    SessionActivity, SessionConnection, SessionPage, SessionTarget, session_activity,
};

use rpc::{find_workspace, needs_workspace, respond};
use tools::ToolState;

type PermissionFuture = LocalBoxFuture<
    'static,
    (
        RequestPermissionRequest,
        Responder<RequestPermissionResponse>,
        Option<usize>,
    ),
>;

type ElicitationFuture = LocalBoxFuture<
    'static,
    (
        Responder<CreateElicitationResponse>,
        Option<BTreeMap<String, ElicitationContentValue>>,
    ),
>;

type McpJobFuture = LocalBoxFuture<'static, Event>;

struct SessionFeatures {
    list_supported: bool,
    close_supported: bool,
    delete_supported: bool,
    provider_editor: bool,
    provider_routing: bool,
}

struct SessionRunner<'a> {
    session: ActiveSession<'a, Agent>,
    #[cfg_attr(target_family = "wasm", allow(dead_code))]
    cwd: PathBuf,
    events: Events,
    #[cfg_attr(target_family = "wasm", allow(dead_code))]
    services: Services,
    features: SessionFeatures,
    #[cfg(not(target_family = "wasm"))]
    provider: crate::mcp::Provider,
    #[cfg(not(target_family = "wasm"))]
    io: Arc<Mutex<ClientIo>>,
    permissions: FuturesUnordered<PermissionFuture>,
    elicitations: FuturesUnordered<ElicitationFuture>,
    mcp_jobs: FuturesUnordered<McpJobFuture>,
    tools: HashMap<String, ToolState>,
}

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
            async move |request: CreateElicitationRequest, responder, _cx| {
                handle_connection_elicitation(request, responder, &elicit_events).await
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
    client
        .connect_with(transport, |connection: ConnectionTo<Agent>| async move {
            run_connection(
                connection,
                target,
                actions,
                events,
                services,
                #[cfg(not(target_family = "wasm"))]
                provider,
            )
            .await
        })
        .await
}

async fn handle_connection_elicitation(
    request: CreateElicitationRequest,
    responder: agent_client_protocol::v1::Responder<CreateElicitationResponse>,
    events: &Events,
) -> Result<(), Error> {
    let Some(form) = elicitation::display(&request) else {
        return responder.respond(elicitation::decline());
    };
    let (sender, receiver) = oneshot::channel();
    let _ = events.unbounded_send(Event::Elicitation {
        form,
        respond: sender,
    });
    match receiver.await.ok().flatten() {
        Some(answers) => responder.respond(elicitation::accept(answers)),
        None => responder.respond(elicitation::decline()),
    }
}

fn respond_elicitation(
    responder: Responder<CreateElicitationResponse>,
    answers: Option<BTreeMap<String, ElicitationContentValue>>,
) -> Result<(), Error> {
    match answers {
        Some(answers) => responder.respond(elicitation::accept(answers)),
        None => responder.respond(elicitation::decline()),
    }
}

async fn run_connection(
    connection: ConnectionTo<Agent>,
    target: SessionTarget,
    actions: &mut Actions,
    events: Events,
    services: Services,
    #[cfg(not(target_family = "wasm"))] provider: crate::mcp::Provider,
) -> Result<(), Error> {
    let initialized = initialize_connection(&connection, &services).await?;
    let provider_editor = services.local_provider_management
        && meta_flag(initialized.meta.as_ref(), "hrns/providers/manage");
    let managed_sessions =
        services.managed_sessions && meta_flag(initialized.meta.as_ref(), "hrns/session/activity");
    let caps = initialized.agent_capabilities;
    let features = SessionFeatures {
        list_supported: caps.session_capabilities.list.is_some(),
        close_supported: caps.session_capabilities.close.is_some(),
        delete_supported: caps.session_capabilities.delete.is_some(),
        provider_editor,
        provider_routing: !provider_editor && caps.providers.is_some(),
    };

    #[cfg(not(target_family = "wasm"))]
    let mcp_servers = provider
        .prepare(
            services.mcp_config.as_deref(),
            &events,
            caps.mcp_capabilities.acp,
        )
        .await;
    #[cfg(target_family = "wasm")]
    let mcp_servers: Vec<McpServer> = Vec::new();

    let loaded = target.session.is_some();
    let (session, cwd, options, meta) =
        open_session(&connection, target, &caps, mcp_servers).await?;

    #[cfg(not(target_family = "wasm"))]
    let io = Arc::new(Mutex::new(ClientIo::new(
        services.client_io,
        session.session_id().to_string(),
        &cwd,
    )));

    let options = options.unwrap_or_default();
    #[cfg(not(target_family = "wasm"))]
    let options = if !loaded && let Some(profile) = &services.effort_profile {
        profile
            .restore(
                session.connection(),
                session.session_id(),
                options,
                false,
                &events,
            )
            .await
    } else {
        options
    };

    emit_ready(
        &events,
        session.session_id(),
        &cwd,
        &services,
        &caps,
        &features,
        managed_sessions,
        loaded,
        options,
        meta,
    );

    let mut runner = SessionRunner {
        session,
        cwd,
        events,
        services,
        features,
        #[cfg(not(target_family = "wasm"))]
        provider,
        #[cfg(not(target_family = "wasm"))]
        io,
        permissions: FuturesUnordered::new(),
        elicitations: FuturesUnordered::new(),
        mcp_jobs: FuturesUnordered::new(),
        tools: HashMap::new(),
    };
    runner.run(actions).await
}

async fn initialize_connection(
    connection: &ConnectionTo<Agent>,
    services: &Services,
) -> Result<InitializeResponse, Error> {
    let info = Implementation::new("hrns", env!("CARGO_PKG_VERSION"));
    let capabilities = ClientCapabilities::new()
        .fs(FileSystemCapabilities::new()
            .read_text_file(services.client_io.reads())
            .write_text_file(services.client_io.writes()))
        .terminal(services.client_io.terminal())
        .session(ClientSessionCapabilities::new().compaction(CompactionCapabilities::new()))
        .elicitation(ElicitationCapabilities::new().form(ElicitationFormCapabilities::new()));
    connection
        .send_request(
            InitializeRequest::new(ProtocolVersion::V1)
                .client_capabilities(capabilities)
                .client_info(info),
        )
        .block_task()
        .await
}

fn meta_flag(meta: Option<&Map<String, Value>>, key: &str) -> bool {
    meta.and_then(|meta| meta.get(key))
        .and_then(Value::as_bool)
        .unwrap_or(false)
}

async fn open_session<'a>(
    connection: &'a ConnectionTo<Agent>,
    target: SessionTarget,
    caps: &AgentCapabilities,
    mcp_servers: Vec<McpServer>,
) -> Result<
    (
        ActiveSession<'a, Agent>,
        PathBuf,
        Option<Vec<SessionConfigOption>>,
        Option<Map<String, Value>>,
    ),
    Error,
> {
    if let Some(id) = target.session {
        if !caps.load_session {
            return Err(Error::new(
                ErrorCode::INVALID_PARAMS,
                "The agent does not support loading sessions",
            ));
        }
        let id = SessionId::new(id);
        let cwd = match target.cwd {
            Some(cwd) => cwd,
            None if caps.session_capabilities.list.is_some() => {
                find_workspace(connection, &id).await?
            }
            None => return Err(needs_workspace()),
        };
        let setup = connection
            .load_session_from(LoadSessionRequest::new(id, &cwd).mcp_servers(mcp_servers))
            .block_task()
            .start_session()
            .await?;
        Ok((
            setup.session,
            cwd,
            setup.response.config_options,
            setup.response.meta,
        ))
    } else {
        let cwd = target.cwd.ok_or_else(needs_workspace)?;
        let setup = connection
            .build_session_from(NewSessionRequest::new(&cwd).mcp_servers(mcp_servers))
            .block_task()
            .start_session()
            .await?;
        Ok((
            setup.session,
            cwd,
            setup.response.config_options,
            setup.response.meta,
        ))
    }
}

#[allow(clippy::too_many_arguments)]
fn emit_ready(
    events: &Events,
    session_id: &SessionId,
    cwd: &std::path::Path,
    services: &Services,
    caps: &AgentCapabilities,
    features: &SessionFeatures,
    managed_sessions: bool,
    loaded: bool,
    options: Vec<SessionConfigOption>,
    meta: Option<Map<String, Value>>,
) {
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
        session: session_id.to_string(),
        approval_scope: services.approval_scope.clone(),
        workspace: cwd.to_path_buf(),
        remote: services.remote,
        client_io: services.client_io,
        attachments_supported: caps.prompt_capabilities.embedded_context,
        loaded,
        load_supported: caps.load_session,
        close_supported: features.close_supported,
        delete_supported: features.delete_supported,
        managed_sessions,
        provider_editor: features.provider_editor,
        provider_routing: features.provider_routing,
        execution,
    });
}

async fn next_or_pending<F: Future>(futures: &mut FuturesUnordered<F>) -> Option<F::Output> {
    if futures.is_empty() {
        std::future::pending().await
    } else {
        futures.next().await
    }
}

impl SessionRunner<'_> {
    pub(super) fn send_event(&self, event: Event) {
        let _ = self.events.unbounded_send(event);
    }

    async fn run(&mut self, actions: &mut Actions) -> Result<(), Error> {
        let connection = self.session.connection().clone();
        loop {
            select_biased! {
                permission = next_or_pending(&mut self.permissions).fuse() => {
                    if let Some((request, responder, option)) = permission {
                        respond(request, responder, option)?;
                    }
                }
                elicitation = next_or_pending(&mut self.elicitations).fuse() => {
                    if let Some((responder, answers)) = elicitation {
                        respond_elicitation(responder, answers)?;
                    }
                }
                event = next_or_pending(&mut self.mcp_jobs).fuse() => {
                    if let Some(event) = event {
                        self.send_event(event);
                    }
                }
                action = actions.next().fuse() => match action {
                    Some(action) => self.handle_action(action).await?,
                    None => break,
                },
                update = self.session.read_update().fuse() => {
                    self.handle_update(update?).await?;
                }
                _ = connection.incoming_closed().fuse() => break,
            }
        }
        Ok(())
    }
}
