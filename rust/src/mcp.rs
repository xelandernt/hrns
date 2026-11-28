//! Client-owned MCP servers exposed through the ACP v1 MCP-over-ACP binding.

mod auth;
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
    ClientRequest, CustomRequest, DiscoverResult, ProgressNotificationParam, ProtocolVersion,
    RequestMetaObject, ServerPeerInfo,
};
use rmcp::service::{
    ClientInitializeError, ClientLifecycleMode, ClientServiceExt, NotificationContext,
    PeerRequestOptions, RoleClient, RunningService,
};
use rmcp::transport::streamable_http_client::{
    StreamableHttpClient, StreamableHttpClientTransportConfig,
};
use rmcp::transport::{StreamableHttpClientTransport, TokioChildProcess};
use rmcp::{ClientHandler, ServiceError};
use serde_json::{Value, json};
use tokio::io::{self, AsyncReadExt};
use tokio::process::{ChildStderr, Command};
use tokio::sync::broadcast;
use tokio::task::JoinHandle;

use crate::acp::{Event, Events};
use crate::mcp_types::{AcpMcpNotification, AuthAction, ConsentDecision, Health, SIGN_IN_REQUIRED};

const MCP_VERSION: &str = "2026-07-28";
const PROTOCOL_VERSION: &str = "io.modelcontextprotocol/protocolVersion";
const CLIENT_CAPABILITIES: &str = "io.modelcontextprotocol/clientCapabilities";
const CLIENT_INFO: &str = "io.modelcontextprotocol/clientInfo";
const MAX_ACTIVE_REQUESTS: usize = 32;
const STARTUP_STDERR_LIMIT: usize = 16 * 1024;
const STDERR_FLUSH_TIMEOUT: Duration = Duration::from_millis(100);
const AUTH_REQUIRED: &str = "MCP authentication required. Set credentials in the server headers.";

#[derive(Clone, Default)]
pub struct Provider(Arc<Mutex<Registry>>);

#[derive(Default)]
struct Registry {
    servers: HashMap<McpServerAcpId, Arc<Upstream>>,
    active: HashSet<(McpServerAcpId, McpRequestId)>,
    logins: HashMap<String, auth::Login>,
    credential_stores: HashMap<String, auth::Store>,
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
    name: String,
    service: RunningService<RoleClient, UpstreamClient>,
    progress: broadcast::Sender<ProgressNotificationParam>,
    settings: config::Server,
    timeout: Duration,
    stderr_drain: Option<JoinHandle<()>>,
}

impl Drop for Upstream {
    fn drop(&mut self) {
        if let Some(drain) = &self.stderr_drain {
            drain.abort();
        }
    }
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
                let _ = events.unbounded_send(Event::Warning(format!(
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
            if let (Some(oauth), Ok(url)) = (settings.oauth(), reqwest::Url::parse(&settings.url))
                && let Ok(store) =
                    auth::Store::new(&settings.credentials, &url, oauth.client_id.trim())
                && let Ok(mut registry) = self.0.lock()
            {
                registry.credential_stores.insert(name.clone(), store);
                status.can_sign_out = true;
            }
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
                    match Upstream::connect(&name, settings).await {
                        Ok((upstream, login)) => {
                            if let Some(info) = upstream.service.peer_info() {
                                status.protocol = info.protocol_version.to_string();
                            }
                            let id = McpServerAcpId::new(uuid::Uuid::new_v4().to_string());
                            if let Ok(mut registry) = self.0.lock() {
                                registry.servers.insert(id.clone(), Arc::new(upstream));
                                if let Some(login) = login {
                                    registry.logins.insert(name.clone(), login);
                                    status.oauth_signed_in = true;
                                }
                                declarations.push(McpServer::Acp(McpServerAcp::new(name, id)));
                                status.state = "connected".into();
                            }
                            if decision == ConsentDecision::Always
                                && let Err(error) = config::allow(&path, &status.name)
                            {
                                let _ = events.unbounded_send(Event::Warning(format!(
                                    "Could not save MCP consent for {}: {error}",
                                    status.name
                                )));
                            }
                        }
                        Err(StartupError::SignInRequired(login)) => {
                            status.state = SIGN_IN_REQUIRED.into();
                            status.startup_failure =
                                Some("Sign-in required. Open /mcp to sign in.".into());
                            status.detail =
                                "Sign in, then start a new session to use this server.".into();
                            if let Ok(mut registry) = self.0.lock() {
                                registry.logins.insert(name, *login);
                            }
                        }
                        Err(StartupError::Unavailable(error)) => {
                            status.state = "unavailable".into();
                            status.startup_failure = Some(summarize_startup_error(&error));
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

    pub async fn authenticate(&self, name: &str, action: AuthAction) -> Result<(), String> {
        match action {
            AuthAction::SignIn => {
                let login = self
                    .0
                    .lock()
                    .ok()
                    .and_then(|registry| registry.logins.get(name).cloned())
                    .ok_or("MCP OAuth is unavailable for this server")?;
                login.sign_in().await
            }
            AuthAction::SignOut => {
                let store = self
                    .0
                    .lock()
                    .ok()
                    .and_then(|registry| registry.credential_stores.get(name).cloned())
                    .ok_or("MCP OAuth is unavailable for this server")?;
                store.sign_out().await.map_err(auth::credential_error)?;
                if let Ok(mut registry) = self.0.lock() {
                    registry.servers.retain(|_, server| server.name != name);
                }
                Ok(())
            }
        }
    }

    pub async fn tools(&self, name: &str) -> Result<Vec<String>, String> {
        let upstream = self
            .0
            .lock()
            .ok()
            .and_then(|registry| {
                registry
                    .servers
                    .values()
                    .find(|upstream| upstream.name == name)
                    .cloned()
            })
            .ok_or("MCP server is unavailable")?;
        if upstream
            .service
            .peer_info()
            .is_some_and(|info| info.capabilities.tools.is_none())
        {
            return Ok(Vec::new());
        }
        let tools = tokio::time::timeout(upstream.timeout, upstream.service.list_all_tools())
            .await
            .map_err(|_| "Listing MCP tools timed out")?
            .map_err(|_| "Could not list MCP tools")?;
        Ok(tools
            .into_iter()
            .filter(|tool| upstream.tool_allowed(&tool.name))
            .map(|tool| tool.name.into_owned())
            .collect())
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

enum StartupError {
    Unavailable(String),
    SignInRequired(Box<auth::Login>),
}

impl From<&str> for StartupError {
    fn from(detail: &str) -> Self {
        Self::Unavailable(detail.into())
    }
}

impl From<String> for StartupError {
    fn from(detail: String) -> Self {
        Self::Unavailable(detail)
    }
}

fn startup_result(
    result: Result<
        Result<RunningService<RoleClient, UpstreamClient>, ClientInitializeError>,
        tokio::time::error::Elapsed,
    >,
) -> Result<RunningService<RoleClient, UpstreamClient>, String> {
    result
        .map_err(|_| "MCP startup timed out")?
        .map_err(|error| {
            if error.auth_challenge().is_some() {
                AUTH_REQUIRED.into()
            } else {
                "MCP negotiation failed".into()
            }
        })
}

async fn capture_startup_stderr(stderr: &mut ChildStderr, output: &mut Vec<u8>) {
    let mut buf = [0; 4096];
    while let Ok(count @ 1..) = stderr.read(&mut buf).await {
        // Continue draining even after the capture limit so the child cannot block on stderr.
        let remaining = STARTUP_STDERR_LIMIT + 1 - output.len();
        output.extend_from_slice(&buf[..count.min(remaining)]);
    }
}

fn startup_error(reason: String, mut output: Vec<u8>) -> String {
    let truncated = output.len() > STARTUP_STDERR_LIMIT;
    output.truncate(STARTUP_STDERR_LIMIT);
    let stripped = strip_ansi_escapes::strip(output);
    let mut detail = String::from_utf8_lossy(&stripped).replace('\t', "    ");
    detail.retain(|ch| ch == '\n' || !ch.is_control());
    let mut detail = detail.trim().to_owned();
    if truncated {
        detail.push_str("\n[stderr truncated]");
    }
    if detail.is_empty() {
        reason
    } else {
        format!("{detail}\n\n{reason}")
    }
}

fn summarize_startup_error(detail: &str) -> String {
    let mut lines = detail.lines();
    let first = lines.next().unwrap_or(detail);
    let summary = if first.starts_with("npm error code ") {
        lines
            .find_map(|line| line.strip_prefix("npm error notarget "))
            .filter(|line| !line.trim().is_empty())
            .unwrap_or(first)
    } else {
        first
    };
    match summary.char_indices().nth(120) {
        Some((end, _)) => format!("{}…", summary[..end].trim_end()),
        None => summary.to_owned(),
    }
}

impl Upstream {
    async fn connect(
        name: &str,
        settings: config::Server,
    ) -> Result<(Self, Option<auth::Login>), StartupError> {
        let timeout = Duration::try_from_secs_f64(settings.timeout)
            .ok()
            .filter(|value| !value.is_zero())
            .ok_or("Invalid MCP timeout")?;
        if settings.url.is_empty() == settings.command.is_empty() {
            return Err("Set either an MCP command or an HTTP URL".into());
        }
        let (progress, _) = broadcast::channel(32);
        let lifecycle = ClientLifecycleMode::Auto {
            preferred_versions: vec![ProtocolVersion::V_2026_07_28],
            legacy_version: Some(ProtocolVersion::LATEST_WITH_INITIALIZE),
        };
        let mut stderr_drain = None;
        let mut login = None;
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
            let (transport, stderr) = TokioChildProcess::builder(command)
                .stderr(Stdio::piped())
                .spawn()
                .map_err(|error| format!("Could not start process: {error}"))?;
            let mut stderr = stderr.ok_or("Could not open process stderr")?;
            let mut output = Vec::new();
            let service = {
                let client = UpstreamClient(progress.clone());
                let discovery = tokio::time::timeout(
                    timeout,
                    client.serve_with_lifecycle(transport, lifecycle),
                );
                tokio::pin!(discovery);
                tokio::select! {
                    result = &mut discovery => result,
                    () = capture_startup_stderr(&mut stderr, &mut output) => discovery.await,
                }
            };
            match startup_result(service) {
                Ok(service) => {
                    stderr_drain = Some(tokio::spawn(async move {
                        let _ = io::copy(&mut stderr, &mut io::sink()).await;
                    }));
                    service
                }
                Err(error) => {
                    // Discovery can see stdout close before stderr has been drained.
                    let _ = tokio::time::timeout(
                        STDERR_FLUSH_TIMEOUT,
                        capture_startup_stderr(&mut stderr, &mut output),
                    )
                    .await;
                    return Err(startup_error(error, output).into());
                }
            }
        } else {
            let url = reqwest::Url::parse(&settings.url).map_err(|_| "Invalid MCP URL")?;
            if !matches!(url.scheme(), "http" | "https")
                || url.host_str().is_none()
                || !url.username().is_empty()
                || url.password().is_some()
            {
                return Err("Use an HTTP(S) MCP URL with credentials in headers".into());
            }
            let headers: HashMap<_, _> = settings
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
            let config =
                StreamableHttpClientTransportConfig::with_uri(url.as_str()).custom_headers(headers);
            let result =
                connect_http(http.clone(), config.clone(), &progress, &lifecycle, timeout).await;
            let challenge = result
                .as_ref()
                .ok()
                .and_then(|result| result.as_ref().err())
                .and_then(bearer_challenge);
            if let (Some(challenge), Some(oauth)) = (challenge, settings.oauth()) {
                let authorization =
                    auth::Login::new(url, challenge.to_owned(), oauth, &settings.credentials)?;
                let client = tokio::time::timeout(timeout, authorization.client(http))
                    .await
                    .map_err(|_| "MCP OAuth startup timed out")??;
                let Some(client) = client else {
                    return Err(StartupError::SignInRequired(Box::new(authorization)));
                };
                let result = connect_http(client, config, &progress, &lifecycle, timeout).await;
                if matches!(&result, Ok(Err(error)) if bearer_challenge(error).is_some()) {
                    return Err(StartupError::SignInRequired(Box::new(authorization)));
                }
                login = Some(authorization);
                startup_result(result)?
            } else {
                startup_result(result)?
            }
        };
        Ok((
            Self {
                name: name.into(),
                service,
                progress,
                settings,
                timeout,
                stderr_drain,
            },
            login,
        ))
    }

    async fn request(
        &self,
        mut request: MessageMcpRequest,
        connection: ConnectionTo<Agent>,
        cancellation: RequestCancellation,
        mut meta: RequestMetaObject,
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
        let legacy = self
            .service
            .peer_info()
            .filter(|info| info.protocol_version.has_initialize());
        if let Some(info) = legacy.as_deref() {
            if request.method == "server/discover" {
                return legacy_discovery(info);
            }
            if params.contains_key("inputResponses") || params.contains_key("requestState") {
                return Ok(MessageMcpResponse::error(McpError::new(
                    -32602,
                    "MCP continuation requires a server that supports multi-round-trip requests",
                )));
            }
            // Client identity and capabilities were already established by initialize.
            for key in [PROTOCOL_VERSION, CLIENT_INFO, CLIENT_CAPABILITIES] {
                meta.remove(key);
            }
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
                if request.method == "server/discover" {
                    restrict_discovery(&mut result);
                } else if legacy.is_some() {
                    translate_legacy_result(&request.method, &mut result);
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

fn legacy_discovery(info: &ServerPeerInfo) -> Result<MessageMcpResponse, Error> {
    let mut discovery = DiscoverResult::new(
        vec![ProtocolVersion::V_2026_07_28],
        info.capabilities.clone(),
    );
    discovery.instructions.clone_from(&info.instructions);
    if let Some(server_info) = &info.server_info {
        discovery.set_server_info(server_info.clone());
    }
    let mut result = serde_json::to_value(discovery).map_err(backend_error)?;
    restrict_discovery(&mut result);
    Ok(MessageMcpResponse::success(result))
}

fn restrict_discovery(result: &mut Value) {
    let Some(discovery) = result.as_object_mut() else {
        return;
    };
    discovery.insert("supportedVersions".into(), json!([MCP_VERSION]));
    if let Some(capabilities) = discovery
        .get_mut("capabilities")
        .and_then(Value::as_object_mut)
    {
        for key in ["extensions", "experimental", "logging"] {
            capabilities.remove(key);
        }
        if let Some(resources) = capabilities
            .get_mut("resources")
            .and_then(Value::as_object_mut)
        {
            resources.remove("subscribe");
        }
        for key in ["resources", "prompts", "tools"] {
            if let Some(capability) = capabilities.get_mut(key).and_then(Value::as_object_mut) {
                capability.remove("listChanged");
            }
        }
    }
}

fn translate_legacy_result(method: &str, result: &mut Value) {
    let Some(result) = result.as_object_mut() else {
        return;
    };
    match method {
        "tools/list"
        | "resources/list"
        | "resources/templates/list"
        | "prompts/list"
        | "resources/read" => {
            result.insert("ttlMs".into(), json!(0));
            result.insert("cacheScope".into(), json!("private"));
        }
        "tools/call" | "prompts/get" | "completion/complete" => {}
        _ => return,
    }
    result
        .entry("resultType")
        .or_insert_with(|| json!("complete"));
}

fn bearer_challenge(error: &ClientInitializeError) -> Option<&str> {
    error.auth_challenge().filter(|challenge| {
        challenge
            .split_whitespace()
            .next()
            .is_some_and(|scheme| scheme.eq_ignore_ascii_case("Bearer"))
    })
}

async fn connect_http(
    http: impl StreamableHttpClient,
    config: StreamableHttpClientTransportConfig,
    progress: &broadcast::Sender<ProgressNotificationParam>,
    lifecycle: &ClientLifecycleMode,
    timeout: Duration,
) -> Result<
    Result<RunningService<RoleClient, UpstreamClient>, ClientInitializeError>,
    tokio::time::error::Elapsed,
> {
    let transport = StreamableHttpClientTransport::with_client(http, config);
    let client = UpstreamClient(progress.clone());
    tokio::time::timeout(
        timeout,
        client.serve_with_lifecycle(transport, lifecycle.clone()),
    )
    .await
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
