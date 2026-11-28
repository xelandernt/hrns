mod store;

use std::sync::{Arc, Mutex};
use std::time::Duration;

use axum::Router;
use axum::extract::Query;
use axum::http::{StatusCode, header};
use axum::response::{Html, IntoResponse, Response};
use axum::routing::get;
use rmcp::transport::auth::{
    AuthClient, AuthError, AuthorizationManager, AuthorizationRequest, AuthorizationSession,
    CredentialStore, OAuthClientConfig,
};
use serde::Deserialize;
use tokio::net::TcpListener;
use tokio::sync::oneshot;

use super::config::{CredentialSettings, OAuthSettings};
pub(super) use store::Store;

const CALLBACK_PATH: &str = "/callback";
const SIGN_IN_TIMEOUT: Duration = Duration::from_secs(180);

#[derive(Clone)]
pub(super) struct Login {
    url: reqwest::Url,
    challenge: String,
    settings: OAuthSettings,
    store: Store,
}

impl Login {
    pub(super) fn new(
        url: reqwest::Url,
        challenge: String,
        settings: OAuthSettings,
        credentials: &CredentialSettings,
    ) -> Result<Self, String> {
        let store = Store::new(credentials, &url, settings.client_id.trim())?;
        Ok(Self {
            url,
            challenge,
            settings,
            store,
        })
    }

    async fn discover(&self) -> Result<AuthorizationManager, String> {
        let mut manager = AuthorizationManager::new(self.url.as_str())
            .await
            .map_err(|_| "Could not initialize MCP OAuth")?;
        let resolution = manager
            .resolve_metadata_from_challenge(Some(&self.challenge))
            .await
            .map_err(|_| "Could not discover the MCP authorization server")?;
        if !resolution.source.is_discovered() {
            return Err("The MCP server did not publish OAuth metadata".into());
        }
        manager.set_metadata(resolution.metadata);
        manager.set_credential_store(self.store.clone());
        Ok(manager)
    }

    pub(super) async fn client(
        &self,
        http: reqwest::Client,
    ) -> Result<Option<AuthClient<reqwest::Client>>, String> {
        if self.store.load().await.map_err(credential_error)?.is_none() {
            return Ok(None);
        }
        let mut manager = self.discover().await?;
        let _guard = self
            .store
            .acquire_refresh_guard()
            .await
            .map_err(credential_error)?;
        if !manager
            .initialize_from_store()
            .await
            .map_err(credential_error)?
        {
            return Ok(None);
        }
        if !self.settings.client_id.trim().is_empty()
            && !self.settings.client_secret.trim().is_empty()
        {
            // Refresh uses the configured secret. The store keeps tokens and client ID only.
            let config = OAuthClientConfig::new(self.settings.client_id.trim(), self.url.as_str())
                .with_client_secret(self.settings.client_secret.trim());
            manager
                .configure_client(config)
                .map_err(|_| "Invalid MCP OAuth client configuration")?;
        }
        Ok(Some(AuthClient::new(http, manager)))
    }

    pub(super) async fn sign_in(&self) -> Result<(), String> {
        tokio::time::timeout(SIGN_IN_TIMEOUT, self.authorize())
            .await
            .map_err(|_| "MCP sign-in timed out")?
    }

    async fn authorize(&self) -> Result<(), String> {
        let manager = self.discover().await?;
        let listener = TcpListener::bind((
            std::net::Ipv4Addr::LOCALHOST,
            self.settings.callback_port.unwrap_or(0),
        ))
        .await
        .map_err(|error| format!("Could not listen for the MCP OAuth callback: {error}"))?;
        let port = listener
            .local_addr()
            .map_err(|error| error.to_string())?
            .port();
        let redirect = format!("http://127.0.0.1:{port}{CALLBACK_PATH}");
        let mut request = AuthorizationRequest::new(redirect)
            .with_client_name("hrns")
            .with_initial_scope_selection()
            .with_scopes(self.settings.scopes.iter().map(|scope| scope.trim()));
        if !self.settings.client_id.trim().is_empty() {
            request = request.with_preregistered_client(self.settings.client_id.trim());
        }
        if !self.settings.client_secret.trim().is_empty() {
            request = request.with_client_secret(self.settings.client_secret.trim());
        }
        let session = AuthorizationSession::new(manager, request)
            .await
            .map_err(|(_, error)| match error {
                AuthError::PkceUnsupported => {
                    "The authorization server does not support PKCE".into()
                }
                AuthError::RegistrationFailed(_) => {
                    "MCP OAuth client registration failed. Check the server's OAuth configuration."
                        .into()
                }
                _ => "Could not start MCP OAuth authorization".to_owned(),
            })?;
        let authorization = reqwest::Url::parse(session.get_authorization_url())
            .map_err(|_| "Invalid MCP authorization URL")?;
        if authorization.scheme() != "https"
            && !(authorization.scheme() == "http"
                && matches!(
                    authorization.host_str(),
                    Some("localhost" | "127.0.0.1" | "[::1]")
                ))
        {
            return Err("MCP authorization requires HTTPS or a loopback HTTP URL".into());
        }
        let state = authorization
            .query_pairs()
            .find(|(key, _)| key == "state")
            .map(|(_, state)| state.into_owned())
            .ok_or("MCP authorization URL has no state")?;
        tokio::task::spawn_blocking(move || open::that_detached(authorization.as_str()))
            .await
            .map_err(|_| "Could not launch the MCP sign-in browser")?
            .map_err(|error| format!("Could not open the browser for MCP sign-in: {error}"))?;
        receive_callback(listener, session, state, self.store.clone()).await
    }
}

pub(super) fn credential_error(error: AuthError) -> String {
    match error {
        AuthError::CredentialStoreError(detail) => {
            format!("MCP OAuth credential storage failed: {detail}")
        }
        _ => "Could not load MCP OAuth credentials".into(),
    }
}

#[derive(Deserialize)]
struct Callback {
    state: String,
    code: Option<String>,
    iss: Option<String>,
}

async fn receive_callback(
    listener: TcpListener,
    session: AuthorizationSession,
    expected_state: String,
    store: Store,
) -> Result<(), String> {
    let (arrived, callback) = oneshot::channel();
    let arrived = Arc::new(Mutex::new(Some(arrived)));
    let app = Router::new().route(
        CALLBACK_PATH,
        get(move |Query(callback): Query<Callback>| {
            let is_expected = callback.state == expected_state;
            let arrived = Arc::clone(&arrived);
            async move {
                if !is_expected {
                    return response(
                        StatusCode::BAD_REQUEST,
                        "This callback does not match the current sign-in.",
                    );
                }
                let Some(arrived) = arrived.lock().ok().and_then(|mut sender| sender.take()) else {
                    return response(
                        StatusCode::CONFLICT,
                        "This sign-in callback has already been received.",
                    );
                };
                let (reply, outcome) = oneshot::channel();
                if arrived.send((callback, reply)).is_ok() && outcome.await == Ok(true) {
                    response(StatusCode::OK, "MCP sign-in complete. Return to hrns.")
                } else {
                    response(
                        StatusCode::BAD_REQUEST,
                        "MCP sign-in failed. Return to hrns for details.",
                    )
                }
            }
        }),
    );
    let (shutdown, stopped) = oneshot::channel();
    let server = axum::serve(listener, app).with_graceful_shutdown(async {
        let _ = stopped.await;
    });
    let wait = async {
        let (callback, reply) = callback
            .await
            .map_err(|_| "The MCP OAuth callback closed unexpectedly")?;
        let result = complete(&session, &store, callback).await;
        let _ = reply.send(result.is_ok());
        let _ = shutdown.send(());
        result
    };
    let (served, result) = tokio::join!(std::future::IntoFuture::into_future(server), wait);
    served.map_err(|error| format!("The MCP OAuth callback server failed: {error}"))?;
    result
}

async fn complete(
    session: &AuthorizationSession,
    store: &Store,
    callback: Callback,
) -> Result<(), String> {
    let code = callback.code.ok_or("MCP authorization was declined")?;
    let _guard = store
        .acquire_refresh_guard()
        .await
        .map_err(credential_error)?;
    session
        .handle_callback_with_issuer(&code, &callback.state, callback.iss.as_deref())
        .await
        .map_err(|error| match error {
            AuthError::CredentialStoreError(_) => credential_error(error),
            _ => "MCP authorization could not be completed".into(),
        })?;
    Ok(())
}

fn response(status: StatusCode, message: &'static str) -> Response {
    (
        status,
        [
            (header::CACHE_CONTROL, "no-store"),
            (header::REFERRER_POLICY, "no-referrer"),
        ],
        Html(format!(
            "<!doctype html><meta charset=utf-8><title>hrns MCP sign-in</title><p>{message}</p>"
        )),
    )
        .into_response()
}
