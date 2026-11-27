use std::collections::HashSet;
#[cfg(not(target_family = "wasm"))]
use std::path::Path;
use std::path::PathBuf;
#[cfg(not(target_family = "wasm"))]
use std::sync::{Arc, Mutex};

use agent_client_protocol::v1::ConnectionTo;
use agent_client_protocol::v1::schema::{
    DeleteSessionRequest, ListSessionsRequest, RequestPermissionOutcome, RequestPermissionRequest,
    RequestPermissionResponse, SelectedPermissionOutcome, SessionId,
};
use agent_client_protocol::{Agent, Error, ErrorCode, UntypedMessage};
use serde::Serialize;

#[cfg(not(target_family = "wasm"))]
use crate::client_io::{self, ClientIo};

#[cfg(not(target_family = "wasm"))]
use super::{ExecutionStatus, session_activity};

#[cfg(not(target_family = "wasm"))]
pub(super) async fn wait_for_terminal_exit(
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
pub(super) async fn with_client_io_blocking<T: Send + 'static>(
    io: Arc<Mutex<ClientIo>>,
    operation: impl FnOnce(&mut ClientIo) -> Result<T, String> + Send + 'static,
) -> Result<T, Error> {
    tokio::task::spawn_blocking(move || with_client_io(&io, operation))
        .await
        .map_err(|_| Error::new(ErrorCode::INTERNAL_ERROR, "client I/O task failed"))?
}

#[cfg(not(target_family = "wasm"))]
pub(super) fn with_client_io<T>(
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
pub(super) fn exit_status(
    exit: client_io::Exit,
) -> agent_client_protocol::v1::schema::TerminalExitStatus {
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

pub(super) async fn provider_call<T: serde::de::DeserializeOwned>(
    connection: &ConnectionTo<Agent>,
    method: &str,
    params: impl Serialize,
) -> Result<T, String> {
    let request = UntypedMessage::new(&format!("_hrns/providers/{method}"), &params)
        .map_err(|error| error.message)?;
    let result = connection
        .send_request(request)
        .block_task()
        .await
        .map_err(|error| {
            error
                .data
                .as_ref()
                .and_then(|data| data.get("reason"))
                .and_then(serde_json::Value::as_str)
                .map(str::to_owned)
                .unwrap_or(error.message)
        })?;
    serde_json::from_value(result).map_err(|_| "Invalid local provider response".into())
}

pub(super) async fn find_workspace(
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

#[cfg(not(target_family = "wasm"))]
pub(super) async fn only_active_session(
    connection: &ConnectionTo<Agent>,
    current: &SessionId,
) -> Result<bool, Error> {
    flush_prompts(connection).await?;
    let mut cursor = None;
    let mut visited = HashSet::new();
    let mut current_active = false;
    loop {
        let page = connection
            .send_request(ListSessionsRequest::new().cursor(cursor))
            .block_task()
            .await?;
        for info in &page.sessions {
            if session_activity(info)
                .is_some_and(|activity| !matches!(activity.status, ExecutionStatus::Idle))
            {
                if &info.session_id != current {
                    return Ok(false);
                }
                current_active = true;
            }
        }
        let Some(next) = page.next_cursor else {
            return Ok(current_active);
        };
        if !visited.insert(next.clone()) {
            return Err(Error::new(
                ErrorCode::INTERNAL_ERROR,
                "Repeated session-list cursor",
            ));
        }
        cursor = Some(next);
    }
}

/// Discard the session the client opened and every untitled session left in its workspace.
#[cfg(not(target_family = "wasm"))]
pub(super) async fn prune_for_quit(
    connection: &ConnectionTo<Agent>,
    current: &SessionId,
    cwd: &Path,
    discard: Option<String>,
    supported: bool,
) {
    if !supported {
        return;
    }
    let sweep = untitled_sessions(connection, current, cwd)
        .await
        .unwrap_or_default();
    for id in discard.into_iter().chain(sweep) {
        let _ = delete_session(connection, id).await;
    }
}

/// The sessions in `cwd` that never received a title and that no client is using.
#[cfg(not(target_family = "wasm"))]
async fn untitled_sessions(
    connection: &ConnectionTo<Agent>,
    current: &SessionId,
    cwd: &Path,
) -> Result<Vec<String>, Error> {
    let mut cursor = None;
    let mut visited = HashSet::new();
    let mut ids = Vec::new();
    loop {
        let page = connection
            .send_request(
                ListSessionsRequest::new()
                    .cursor(cursor)
                    .cwd(Some(cwd.to_path_buf())),
            )
            .block_task()
            .await?;
        for info in &page.sessions {
            let in_use = session_activity(info).is_some_and(|activity| {
                activity.attached_elsewhere || !matches!(activity.status, ExecutionStatus::Idle)
            });
            let untitled = info.title.as_deref().is_none_or(str::is_empty);
            if untitled && !in_use && &info.session_id != current {
                ids.push(info.session_id.to_string());
            }
        }
        let Some(next) = page.next_cursor else {
            return Ok(ids);
        };
        if !visited.insert(next.clone()) {
            return Err(Error::new(
                ErrorCode::INTERNAL_ERROR,
                "Repeated session-list cursor",
            ));
        }
        cursor = Some(next);
    }
}

pub(super) async fn delete_session(
    connection: &ConnectionTo<Agent>,
    id: String,
) -> Result<(), String> {
    match connection
        .send_request(DeleteSessionRequest::new(id))
        .block_task()
        .await
    {
        Ok(_) => Ok(()),
        Err(error) if error.code == ErrorCode::RESOURCE_NOT_FOUND => Ok(()),
        Err(error) => Err(error.message),
    }
}

pub(super) async fn flush_prompts(connection: &ConnectionTo<Agent>) -> Result<(), Error> {
    let request = UntypedMessage::new("_hrns/session/flush", serde_json::json!({}))?;
    connection.send_request(request).block_task().await?;
    Ok(())
}

pub fn request_error(error: &Error) -> String {
    error
        .data
        .as_ref()
        .and_then(|data| data.get("reason"))
        .and_then(serde_json::Value::as_str)
        .map(str::to_owned)
        .unwrap_or_else(|| error.message.clone())
}

pub(super) fn needs_workspace() -> Error {
    Error::new(
        ErrorCode::INVALID_PARAMS,
        "Provide the saved session workspace with --cwd or the browser cwd parameter",
    )
}

pub(super) fn respond(
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
