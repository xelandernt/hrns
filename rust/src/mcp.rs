//! Client-owned MCP servers exposed through the ACP v1 MCP-over-ACP binding.

mod config;

use std::collections::{HashMap, HashSet};
use std::path::Path;
use std::process::Stdio;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use agent_client_protocol::mcp_server::{
    MCP_BACKEND_FAILURE, MCP_RESOURCE_LIMIT_EXCEEDED, MCP_SERVER_UNAVAILABLE,
};
use agent_client_protocol::v1::ConnectionTo;
use agent_client_protocol::v1::schema::{
    McpError, McpRequestId, McpServer, McpServerAcp, McpServerAcpId, MessageMcpNotification,
    MessageMcpRequest, MessageMcpResponse,
};
use agent_client_protocol::{Agent, Error, RequestCancellation};
use futures::channel::oneshot;
use rmcp::model::{
    ClientRequest, CustomRequest, ProgressNotificationParam, ProtocolVersion, RequestMetaObject,
};
use rmcp::service::{
    ClientLifecycleMode, ClientServiceExt, NotificationContext, PeerRequestOptions, RoleClient,
    RunningService,
};
use rmcp::transport::streamable_http_client::StreamableHttpClientTransportConfig;
use rmcp::transport::{StreamableHttpClientTransport, TokioChildProcess};
use rmcp::{ClientHandler, ServiceError};
use serde_json::{Value, json};
use tokio::process::Command;
use tokio::sync::broadcast;

use crate::acp::{Event, Events};
use crate::mcp_types::{AcpMcpNotification, ConsentDecision, Health};

const MCP_VERSION: &str = "2026-07-28";
const PROTOCOL_VERSION: &str = "io.modelcontextprotocol/protocolVersion";
const CLIENT_CAPABILITIES: &str = "io.modelcontextprotocol/clientCapabilities";
const CLIENT_INFO: &str = "io.modelcontextprotocol/clientInfo";
const MAX_ACTIVE_REQUESTS: usize = 32;

#[derive(Clone, Default)]
pub struct Provider(Arc<Mutex<Registry>>);

#[derive(Default)]
struct Registry {
    servers: HashMap<McpServerAcpId, Arc<Upstream>>,
    active: HashSet<(McpServerAcpId, McpRequestId)>,
}

struct Operation {
    provider: Provider,
    key: (McpServerAcpId, McpRequestId),
}

impl Drop for Operation {
    fn drop(&mut self) {
        if let Ok(mut registry) = self.provider.0.lock() {
            registry.active.remove(&self.key);
        }
    }
}

struct Upstream {
    service: RunningService<RoleClient, UpstreamClient>,
    progress: broadcast::Sender<ProgressNotificationParam>,
    settings: config::Server,
    timeout: Duration,
}

struct UpstreamClient(broadcast::Sender<ProgressNotificationParam>);

impl ClientHandler for UpstreamClient {
    async fn on_progress(
        &self,
        params: ProgressNotificationParam,
        _: NotificationContext<RoleClient>,
    ) {
        let _ = self.0.send(params);
    }
}

impl Provider {
    pub async fn prepare(
        &self,
        path: Option<&Path>,
        events: &Events,
        supported: bool,
    ) -> Vec<McpServer> {
        let servers = match config::read(path) {
            Ok(servers) => servers,
            Err(error) => {
                let _ = events.unbounded_send(Event::Note(format!(
                    "Could not load MCP configuration: {error}"
                )));
                return Vec::new();
            }
        };
        let mut declarations = Vec::new();
        let mut health = Vec::new();
        for (name, (settings, path)) in servers {
            let mut status = Health {
                name: name.clone(),
                transport: settings.transport().into(),
                target: settings.target(),
                ..Health::default()
            };
            if !settings.enabled {
                status.state = "disabled".into();
            } else if !supported {
                status.state = "unavailable".into();
                status.detail = "Agent does not advertise MCP-over-ACP".into();
            } else {
                let decision = consent(events, &status, settings.always_allow).await;
                if decision == ConsentDecision::Deny {
                    status.state = "denied".into();
                } else {
                    match Upstream::connect(settings).await {
                        Ok(upstream) => {
                            if let Some(info) = upstream.service.peer_info() {
                                status.protocol = info.protocol_version.to_string();
                            }
                            let id = McpServerAcpId::new(uuid::Uuid::new_v4().to_string());
                            if let Ok(mut registry) = self.0.lock() {
                                registry.servers.insert(id.clone(), Arc::new(upstream));
                                declarations.push(McpServer::Acp(McpServerAcp::new(name, id)));
                                status.state = "connected".into();
                            }
                            if decision == ConsentDecision::Always
                                && let Err(error) = config::allow(&path, &status.name)
                            {
                                let _ = events.unbounded_send(Event::Note(format!(
                                    "Could not save MCP consent for {}: {error}",
                                    status.name
                                )));
                            }
                        }
                        Err(error) => {
                            status.state = "failed".into();
                            status.detail = error;
                        }
                    }
                }
            }
            health.push(status);
        }
        let _ = events.unbounded_send(Event::McpHealth(health));
        declarations
    }

    fn admit(&self, request: &MessageMcpRequest) -> Result<(Arc<Upstream>, Operation), Error> {
        let mut registry = self.0.lock().map_err(|_| Error::internal_error())?;
        let server = registry
            .servers
            .get(&request.server_id)
            .cloned()
            .ok_or_else(|| {
                Error::new(
                    MCP_SERVER_UNAVAILABLE,
                    "MCP server registration is unavailable",
                )
            })?;
        let key = (request.server_id.clone(), request.request_id.clone());
        if registry.active.contains(&key) {
            return Err(Error::invalid_params().data("Duplicate active MCP requestId"));
        }
        if registry.active.len() >= MAX_ACTIVE_REQUESTS {
            return Err(Error::new(
                MCP_RESOURCE_LIMIT_EXCEEDED,
                "Too many active MCP operations",
            ));
        }
        registry.active.insert(key.clone());
        Ok((
            server,
            Operation {
                provider: self.clone(),
                key,
            },
        ))
    }

    pub async fn request(
        &self,
        request: MessageMcpRequest,
        connection: ConnectionTo<Agent>,
        cancellation: RequestCancellation,
    ) -> Result<MessageMcpResponse, Error> {
        if cancellation.is_cancelled() {
            return Err(Error::request_cancelled());
        }
        let (server, _operation) = self.admit(&request)?;
        let meta = match metadata(&request) {
            Ok(meta) => meta,
            Err(error) => return Ok(MessageMcpResponse::error(*error)),
        };
        server
            .request(request, connection, cancellation, meta)
            .await
    }
}

async fn consent(events: &Events, status: &Health, always_allow: bool) -> ConsentDecision {
    if always_allow {
        return ConsentDecision::Once;
    }
    let (respond, answer) = oneshot::channel();
    if events
        .unbounded_send(Event::McpConsent {
            name: status.name.clone(),
            transport: status.transport.clone(),
            target: status.target.clone(),
            respond,
        })
        .is_err()
    {
        return ConsentDecision::Deny;
    }
    answer.await.unwrap_or(ConsentDecision::Deny)
}

fn metadata(request: &MessageMcpRequest) -> Result<RequestMetaObject, Box<McpError>> {
    let meta = request
        .params
        .as_ref()
        .and_then(|params| params.get("_meta"))
        .and_then(Value::as_object)
        .ok_or_else(|| Box::new(McpError::new(-32602, "Missing MCP request metadata")))?;
    let version = meta
        .get(PROTOCOL_VERSION)
        .and_then(Value::as_str)
        .ok_or_else(|| Box::new(McpError::new(-32602, "Missing MCP protocol version")))?;
    if version != MCP_VERSION {
        return Err(Box::new(
            McpError::new(-32022, "Unsupported MCP protocol version")
                .data(json!({"supported": [MCP_VERSION], "requested": version})),
        ));
    }
    if !meta.get(CLIENT_CAPABILITIES).is_some_and(Value::is_object)
        || meta.get(CLIENT_INFO).is_some_and(|info| !info.is_object())
    {
        return Err(Box::new(McpError::new(
            -32602,
            "Invalid MCP client metadata",
        )));
    }
    serde_json::from_value(Value::Object(meta.clone()))
        .map_err(|_| Box::new(McpError::new(-32602, "Invalid MCP request metadata")))
}

impl Upstream {
    async fn connect(settings: config::Server) -> Result<Self, String> {
        let timeout = Duration::try_from_secs_f64(settings.timeout)
            .ok()
            .filter(|value| !value.is_zero())
            .ok_or("Invalid MCP timeout")?;
        if settings.url.is_empty() == settings.command.is_empty() {
            return Err("Set either an MCP command or an HTTP URL".into());
        }
        let (progress, _) = broadcast::channel(32);
        let client = UpstreamClient(progress.clone());
        let lifecycle = ClientLifecycleMode::Discover {
            preferred_versions: vec![ProtocolVersion::V_2026_07_28],
        };
        let service = if settings.url.is_empty() {
            let mut command = Command::new(&settings.command);
            command.args(&settings.args).env_clear();
            for name in [
                "HOME",
                "PATH",
                "USER",
                "LOGNAME",
                "SHELL",
                "LANG",
                "LC_ALL",
                "TMPDIR",
                "TMP",
                "TEMP",
                "SystemRoot",
                "APPDATA",
                "LOCALAPPDATA",
                "USERPROFILE",
                "PATHEXT",
                "COMSPEC",
            ] {
                if let Some(value) = std::env::var_os(name) {
                    command.env(name, value);
                }
            }
            if !settings.cwd.is_empty() {
                command.current_dir(&settings.cwd);
            }
            for (name, value) in &settings.env {
                command.env(name, config::expand(value)?);
            }
            command.stderr(Stdio::null());
            let transport = TokioChildProcess::new(command).map_err(|error| error.to_string())?;
            tokio::time::timeout(timeout, client.serve_with_lifecycle(transport, lifecycle)).await
        } else {
            let url = reqwest::Url::parse(&settings.url).map_err(|_| "Invalid MCP URL")?;
            if !matches!(url.scheme(), "http" | "https")
                || url.host_str().is_none()
                || !url.username().is_empty()
                || url.password().is_some()
            {
                return Err("Use an HTTP(S) MCP URL with credentials in headers".into());
            }
            let headers = settings
                .headers
                .iter()
                .map(|(name, value)| {
                    let name = reqwest::header::HeaderName::from_bytes(name.as_bytes())
                        .map_err(|_| "Invalid MCP header name")?;
                    let mut value = reqwest::header::HeaderValue::from_str(&config::expand(value)?)
                        .map_err(|_| "Invalid MCP header value")?;
                    value.set_sensitive(true);
                    Ok::<_, String>((name, value))
                })
                .collect::<Result<_, _>>()?;
            let http = reqwest::Client::builder()
                .redirect(reqwest::redirect::Policy::none())
                .build()
                .map_err(|_| "Could not initialize HTTP client")?;
            let transport = StreamableHttpClientTransport::with_client(
                http,
                StreamableHttpClientTransportConfig::with_uri(url.as_str()).custom_headers(headers),
            );
            tokio::time::timeout(timeout, client.serve_with_lifecycle(transport, lifecycle)).await
        }
        .map_err(|_| "MCP discovery timed out")?
        .map_err(|error| {
            if error.auth_challenge().is_some() {
                "MCP authentication required. Set credentials in the server headers."
            } else {
                "MCP discovery failed (requires MCP 2026-07-28)"
            }
        })?;
        Ok(Self {
            service,
            progress,
            settings,
            timeout,
        })
    }

    async fn request(
        &self,
        mut request: MessageMcpRequest,
        connection: ConnectionTo<Agent>,
        cancellation: RequestCancellation,
        meta: RequestMetaObject,
    ) -> Result<MessageMcpResponse, Error> {
        let mut params = request.params.take().unwrap_or_default();
        params.remove("_meta");
        if request.method == "tools/call"
            && let Some(name) = params.get("name").and_then(Value::as_str)
            && !self.tool_allowed(name)
        {
            return Ok(MessageMcpResponse::error(McpError::new(
                -32602,
                "Tool is not available",
            )));
        }
        if !matches!(
            request.method.as_str(),
            "server/discover"
                | "ping"
                | "tools/list"
                | "tools/call"
                | "resources/list"
                | "resources/templates/list"
                | "resources/read"
                | "prompts/list"
                | "prompts/get"
                | "completion/complete"
        ) {
            return Ok(MessageMcpResponse::error(McpError::new(
                -32601,
                "MCP method not available over ACP",
            )));
        }
        let mut progress = self.progress.subscribe();
        let progress_token = meta.get_progress_token();
        let rpc = ClientRequest::CustomRequest(CustomRequest::new(
            &request.method,
            Some(Value::Object(params)),
        ));
        let options = PeerRequestOptions::no_options().with_meta(meta);
        let mut handle = self
            .service
            .peer()
            .send_cancellable_request(rpc, options)
            .await
            .map_err(backend_error)?;
        let deadline = tokio::time::sleep(self.timeout);
        tokio::pin!(deadline);
        let shutdown = connection.incoming_closed();
        tokio::pin!(shutdown);
        let result = loop {
            tokio::select! {
                biased;
                _ = cancellation.cancelled() => {
                    cancel(handle).await;
                    return Err(Error::request_cancelled());
                }
                _ = &mut shutdown => {
                    cancel(handle).await;
                    return Err(Error::request_cancelled());
                }
                item = progress.recv(), if progress_token.is_some() => {
                    match item {
                        Ok(mut item) if item.progress_token == handle.progress_token => {
                            if let Some(token) = &progress_token {
                                item.progress_token = token.clone();
                            }
                            let params = serde_json::to_value(item).map_err(backend_error)?;
                            let notification = MessageMcpNotification::new(
                                request.server_id.clone(), request.request_id.clone(), "notifications/progress",
                            ).params(params.as_object().cloned());
                            if let Err(error) = connection.send_notification(AcpMcpNotification(notification)) {
                                cancel(handle).await;
                                return Err(error);
                            }
                        }
                        Ok(_) => {}
                        Err(_) => {
                            cancel(handle).await;
                            return Err(Error::new(MCP_RESOURCE_LIMIT_EXCEEDED, "MCP progress buffer exceeded"));
                        }
                    }
                }
                response = &mut handle.rx => break response.map_err(backend_error)?,
                _ = &mut deadline => {
                    cancel(handle).await;
                    return Err(Error::new(MCP_BACKEND_FAILURE, "MCP operation timed out"));
                }
            }
        };
        match result {
            Ok(result) => {
                let mut result = serde_json::to_value(result).map_err(backend_error)?;
                if request.method == "tools/list"
                    && let Some(tools) = result.get_mut("tools").and_then(Value::as_array_mut)
                {
                    tools.retain(|tool| {
                        tool.get("name")
                            .and_then(Value::as_str)
                            .is_some_and(|name| self.tool_allowed(name))
                    });
                }
                if request.method == "server/discover"
                    && let Some(discovery) = result.as_object_mut()
                {
                    discovery.insert("supportedVersions".into(), json!([MCP_VERSION]));
                    if let Some(capabilities) = discovery
                        .get_mut("capabilities")
                        .and_then(Value::as_object_mut)
                    {
                        capabilities.remove("extensions");
                        capabilities.remove("experimental");
                        capabilities.remove("logging");
                        if let Some(resources) = capabilities
                            .get_mut("resources")
                            .and_then(Value::as_object_mut)
                        {
                            resources.remove("subscribe");
                        }
                        for key in ["resources", "prompts", "tools"] {
                            if let Some(capability) =
                                capabilities.get_mut(key).and_then(Value::as_object_mut)
                            {
                                capability.remove("listChanged");
                            }
                        }
                    }
                }
                Ok(MessageMcpResponse::success(result))
            }
            Err(ServiceError::McpError(error)) => {
                let error = serde_json::to_value(error)
                    .and_then(serde_json::from_value)
                    .map_err(backend_error)?;
                Ok(MessageMcpResponse::error(error))
            }
            Err(error) => Err(backend_error(error)),
        }
    }

    fn tool_allowed(&self, name: &str) -> bool {
        (self.settings.include_tools.is_empty()
            || self.settings.include_tools.iter().any(|tool| tool == name))
            && !self.settings.exclude_tools.iter().any(|tool| tool == name)
    }
}

async fn cancel(handle: rmcp::service::RequestHandle<RoleClient>) {
    let _ = tokio::time::timeout(Duration::from_secs(1), handle.cancel(None)).await;
}

fn backend_error(_: impl std::fmt::Display) -> Error {
    Error::new(
        MCP_BACKEND_FAILURE,
        "MCP upstream did not return a valid result",
    )
}
