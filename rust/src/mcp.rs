//! Native MCP-over-ACP export through the Python MCP sidecar.
//!
//! The sidecar owns the configured MCP clients and consent policy; this module
//! owns the ACP process boundary, forwards `mcp/message`, and surfaces consent
//! and health to the application.

use std::collections::HashMap;
use std::path::Path;
use std::process::Stdio;
use std::sync::Arc;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::Duration;

use agent_client_protocol::v1::schema::{
    McpRequestId, McpServerAcpId, MessageMcpNotification, MessageMcpRequest, MessageMcpResponse,
};
use agent_client_protocol::{Error, ErrorCode};
use futures::channel::{mpsc, oneshot};
use serde::{Deserialize, Serialize};
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::process::{Child, ChildStdin, ChildStdout, Command};
use tokio::sync::Mutex;

use crate::acp::Event;
use crate::mcp_types::{ConsentDecision, Declaration, Health};

#[derive(Serialize)]
#[serde(tag = "type", rename_all = "snake_case")]
enum ToSidecar<'a> {
    Start,
    Declare,
    Consent { id: u64, decision: &'a str },
    Request { id: u64, params: serde_json::Value },
    Shutdown,
}

#[derive(Deserialize)]
#[serde(
    tag = "type",
    rename_all = "snake_case",
    rename_all_fields = "camelCase"
)]
enum FromSidecar {
    Ready,
    Consent {
        id: u64,
        name: String,
        #[serde(default)]
        transport: String,
        #[serde(default)]
        target: String,
    },
    Declarations {
        servers: Vec<Declaration>,
        #[serde(default)]
        health: Vec<Health>,
    },
    Response {
        id: u64,
        outcome: serde_json::Value,
    },
    OuterError {
        id: u64,
        code: i32,
        message: String,
    },
    Notify {
        server_id: String,
        request_id: String,
        method: String,
        #[serde(default)]
        params: Option<serde_json::Map<String, serde_json::Value>>,
    },
    Health {
        servers: Vec<Health>,
    },
}

enum Reply {
    Response(serde_json::Value),
    Outer { code: i32, message: String },
}

type Notifier = Arc<dyn Fn(MessageMcpNotification) + Send + Sync>;
type DeclarationReply = (Vec<Declaration>, Vec<Health>);

struct Inner {
    writer: Mutex<Option<ChildStdin>>,
    pending: Mutex<HashMap<u64, oneshot::Sender<Reply>>>,
    declarations: Mutex<Option<oneshot::Sender<DeclarationReply>>>,
    ready: Mutex<Option<oneshot::Sender<()>>>,
    health: Mutex<Vec<Health>>,
    events: mpsc::UnboundedSender<Event>,
    notifier: Mutex<Option<Notifier>>,
    next_id: AtomicU64,
}

#[derive(Clone)]
pub struct Bridge {
    inner: Arc<Inner>,
    child: Arc<Mutex<Option<Child>>>,
}

impl Bridge {
    pub async fn start(
        executable: &Path,
        config: Option<&Path>,
        events: mpsc::UnboundedSender<Event>,
    ) -> Result<Self, Error> {
        let mut command = Command::new(executable);
        command.arg("-m").arg("hrns.client.mcp_sidecar");
        if let Some(config) = config {
            command.arg("--config").arg(config);
        }
        command
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .kill_on_drop(true);
        let mut child = command.spawn().map_err(|error| {
            Error::new(
                ErrorCode::INTERNAL_ERROR,
                format!("cannot start the MCP sidecar: {error}"),
            )
        })?;
        let stdin = child.stdin.take();
        let stdout = child.stdout.take();
        let (ready_sender, ready_receiver) = oneshot::channel();
        let inner = Arc::new(Inner {
            writer: Mutex::new(stdin),
            pending: Mutex::new(HashMap::new()),
            declarations: Mutex::new(None),
            ready: Mutex::new(Some(ready_sender)),
            health: Mutex::new(Vec::new()),
            events,
            notifier: Mutex::new(None),
            next_id: AtomicU64::new(1),
        });
        if let Some(stdout) = stdout {
            tokio::spawn(read_sidecar(stdout, Arc::clone(&inner)));
        }
        let bridge = Self {
            inner,
            child: Arc::new(Mutex::new(Some(child))),
        };
        bridge.send(ToSidecar::Start).await?;
        match tokio::time::timeout(Duration::from_secs(20), ready_receiver).await {
            Ok(Ok(())) => Ok(bridge),
            Ok(Err(_)) => Err(Error::new(
                ErrorCode::INTERNAL_ERROR,
                "the MCP sidecar stopped before it was ready",
            )),
            Err(_) => Err(Error::new(
                ErrorCode::INTERNAL_ERROR,
                "the MCP sidecar did not become ready",
            )),
        }
    }

    pub async fn declare(&self) -> Result<(Vec<Declaration>, Vec<Health>), Error> {
        let (sender, receiver) = oneshot::channel();
        *self.inner.declarations.lock().await = Some(sender);
        self.send(ToSidecar::Declare).await?;
        receiver.await.map_err(|_| {
            Error::new(
                ErrorCode::INTERNAL_ERROR,
                "the MCP sidecar did not answer the declaration",
            )
        })
    }

    pub async fn request(&self, request: &MessageMcpRequest) -> Result<MessageMcpResponse, Error> {
        let id = self.inner.next_id.fetch_add(1, Ordering::Relaxed);
        let params = serde_json::to_value(request).map_err(internal)?;
        let (sender, receiver) = oneshot::channel();
        self.inner.pending.lock().await.insert(id, sender);
        if let Err(error) = self.send(ToSidecar::Request { id, params }).await {
            self.inner.pending.lock().await.remove(&id);
            return Err(error);
        }
        match receiver.await {
            Ok(Reply::Response(outcome)) => serde_json::from_value(outcome).map_err(|error| {
                Error::new(
                    ErrorCode::INTERNAL_ERROR,
                    format!("invalid MCP sidecar outcome: {error}"),
                )
            }),
            Ok(Reply::Outer { code, message }) => Err(outer_error(code, message)),
            Err(_) => Err(Error::new(
                ErrorCode::INTERNAL_ERROR,
                "the MCP sidecar stopped responding",
            )),
        }
    }

    pub async fn set_notifier(&self, notifier: Notifier) {
        *self.inner.notifier.lock().await = Some(notifier);
    }

    pub async fn shutdown(&self) {
        let _ = self.send(ToSidecar::Shutdown).await;
        self.inner.writer.lock().await.take();
        let mut guard = self.child.lock().await;
        if let Some(mut child) = guard.take()
            && tokio::time::timeout(Duration::from_secs(2), child.wait())
                .await
                .is_err()
        {
            let _ = child.kill().await;
        }
    }

    async fn send(&self, message: ToSidecar<'_>) -> Result<(), Error> {
        write_message(&self.inner, &message).await
    }
}

async fn write_message(inner: &Inner, message: &ToSidecar<'_>) -> Result<(), Error> {
    let mut line = serde_json::to_vec(message).map_err(internal)?;
    line.push(b'\n');
    let mut guard = inner.writer.lock().await;
    let Some(writer) = guard.as_mut() else {
        return Err(Error::new(
            ErrorCode::INTERNAL_ERROR,
            "the MCP sidecar is not writable",
        ));
    };
    writer.write_all(&line).await.map_err(io_internal)?;
    writer.flush().await.map_err(io_internal)?;
    Ok(())
}

async fn read_sidecar(stdout: ChildStdout, inner: Arc<Inner>) {
    let mut lines = BufReader::new(stdout).lines();
    while let Ok(Some(line)) = lines.next_line().await {
        let message = match serde_json::from_str::<FromSidecar>(&line) {
            Ok(message) => message,
            Err(error) => {
                eprintln!("mcp sidecar: ignored an invalid message ({error}): {line}");
                continue;
            }
        };
        match message {
            FromSidecar::Ready => {
                if let Some(sender) = inner.ready.lock().await.take() {
                    let _ = sender.send(());
                }
            }
            FromSidecar::Consent {
                id,
                name,
                transport,
                target,
                ..
            } => {
                let inner = Arc::clone(&inner);
                tokio::spawn(async move {
                    let (sender, receiver) = oneshot::channel();
                    if inner
                        .events
                        .unbounded_send(Event::McpConsent {
                            name,
                            transport,
                            target,
                            respond: sender,
                        })
                        .is_err()
                    {
                        return;
                    }
                    let decision = receiver.await.unwrap_or(ConsentDecision::Deny);
                    let _ = write_message(
                        &inner,
                        &ToSidecar::Consent {
                            id,
                            decision: decision.label(),
                        },
                    )
                    .await;
                });
            }
            FromSidecar::Declarations { servers, health } => {
                *inner.health.lock().await = health.clone();
                let _ = inner
                    .events
                    .unbounded_send(Event::McpHealth(health.clone()));
                if let Some(sender) = inner.declarations.lock().await.take() {
                    let _ = sender.send((servers, health));
                }
            }
            FromSidecar::Response { id, outcome } => {
                if let Some(sender) = inner.pending.lock().await.remove(&id) {
                    let _ = sender.send(Reply::Response(outcome));
                }
            }
            FromSidecar::OuterError { id, code, message } => {
                if let Some(sender) = inner.pending.lock().await.remove(&id) {
                    let _ = sender.send(Reply::Outer { code, message });
                }
            }
            FromSidecar::Notify {
                server_id,
                request_id,
                method,
                params,
            } => {
                let notification = MessageMcpNotification::new(
                    McpServerAcpId::new(server_id),
                    McpRequestId::new(request_id),
                    method,
                )
                .params(params);
                if let Some(notifier) = inner.notifier.lock().await.as_ref() {
                    notifier(notification);
                }
            }
            FromSidecar::Health { servers } => {
                *inner.health.lock().await = servers.clone();
                let _ = inner.events.unbounded_send(Event::McpHealth(servers));
            }
        }
    }
    for (_, sender) in inner.pending.lock().await.drain() {
        let _ = sender.send(Reply::Outer {
            code: -32603,
            message: "MCP sidecar exited".into(),
        });
    }
    if let Some(sender) = inner.declarations.lock().await.take() {
        drop(sender);
    }
    if let Some(sender) = inner.ready.lock().await.take() {
        drop(sender);
    }
}

fn io_internal(error: std::io::Error) -> Error {
    Error::new(ErrorCode::INTERNAL_ERROR, error.to_string())
}

fn internal(error: serde_json::Error) -> Error {
    Error::new(ErrorCode::INTERNAL_ERROR, error.to_string())
}

fn outer_error(code: i32, message: String) -> Error {
    let code = match code {
        -32602 => ErrorCode::INVALID_PARAMS,
        -32800 => ErrorCode::REQUEST_CANCELLED,
        _ => ErrorCode::INTERNAL_ERROR,
    };
    Error::new(code, message)
}
