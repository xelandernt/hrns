use std::path::PathBuf;

use agent_client_protocol::Error;
use agent_client_protocol::v1::schema::{
    CancelNotification, CloseSessionRequest, ListProvidersRequest, ListSessionsRequest,
    SessionConfigId, SessionConfigOptionCategory, SessionConfigOptionValue,
    SetSessionConfigOptionRequest,
};
#[cfg(not(target_family = "wasm"))]
use futures::FutureExt;
use serde_json::{Map, Value, json};

use super::rpc::{delete_session, flush_prompts, provider_call, request_error};
#[cfg(not(target_family = "wasm"))]
use super::rpc::{only_active_session, prune_for_quit};
use super::types::ProviderList;
use super::{Action, Event, ModelChange, SessionPage, SessionRunner};

impl SessionRunner<'_> {
    pub(super) async fn handle_action(&mut self, action: Action) -> Result<(), Error> {
        match action {
            #[cfg(not(target_family = "wasm"))]
            Action::McpAuthenticate { name, action } => {
                let provider = self.provider.clone();
                self.mcp_jobs.push(
                    async move {
                        let result = provider.authenticate(&name, action).await;
                        Event::McpAuthenticated {
                            name,
                            action,
                            result,
                        }
                    }
                    .boxed_local(),
                );
            }
            #[cfg(not(target_family = "wasm"))]
            Action::McpTools(name) => {
                let provider = self.provider.clone();
                self.mcp_jobs.push(
                    async move {
                        let result = provider.tools(&name).await;
                        Event::McpTools { name, result }
                    }
                    .boxed_local(),
                );
            }
            Action::Prompt(prompt) => self.session.send_prompt_blocks(prompt.into_blocks())?,
            Action::Cancel => {
                self.session
                    .connection()
                    .send_notification(CancelNotification::new(
                        self.session.session_id().clone(),
                    ))?;
            }
            Action::SetOption {
                id,
                value,
                category,
                model_change,
            } => {
                self.handle_set_option(id, value, category, model_change)
                    .await;
            }
            Action::ListSessions {
                request_id,
                cursor,
                cwd,
            } => self.handle_list_sessions(request_id, cursor, cwd).await,
            Action::PrepareSwitch(target) => {
                let result = flush_prompts(self.session.connection())
                    .await
                    .map(|()| target)
                    .map_err(|error| request_error(&error));
                self.send_event(Event::SwitchPrepared(result));
            }
            #[cfg(not(target_family = "wasm"))]
            Action::CheckQuit | Action::StopForQuit | Action::PruneForQuit { .. } => {
                self.handle_quit_action(action).await;
            }
            Action::CloseSession(id) => self.handle_close_session(id).await,
            Action::DeleteSession(id) => self.handle_delete_session(id).await,
            Action::ListProviderRoutes
            | Action::SetProviderRoute(_)
            | Action::DisableProviderRoute(_)
                if self.features.provider_routing =>
            {
                self.handle_provider_route_action(action).await;
            }
            Action::ListProviders
            | Action::SaveProvider(_)
            | Action::RemoveProvider(_)
            | Action::ProviderLogin { .. }
            | Action::ProviderLoginStatus
            | Action::CancelProviderLogin
                if self.features.provider_editor =>
            {
                self.handle_provider_editor_action(action).await;
            }
            _ => {}
        }
        Ok(())
    }

    async fn handle_set_option(
        &self,
        id: SessionConfigId,
        value: SessionConfigOptionValue,
        category: SessionConfigOptionCategory,
        model_change: Option<ModelChange>,
    ) {
        let mut request =
            SetSessionConfigOptionRequest::new(self.session.session_id().clone(), id, value);
        if matches!(model_change, Some(ModelChange::Provider)) {
            request.meta = Some(Map::from_iter([(
                "hrns/model/automatic".into(),
                Value::Bool(true),
            )]));
        }
        let reply = self
            .session
            .connection()
            .send_request(request)
            .block_task()
            .await;
        let result = match reply {
            Ok(reply) => {
                let options = reply.config_options;
                #[cfg(not(target_family = "wasm"))]
                let options = if let Some(profile) = &self.services.effort_profile {
                    match category {
                        SessionConfigOptionCategory::Model => {
                            profile
                                .restore(
                                    self.session.connection(),
                                    self.session.session_id(),
                                    options,
                                    true,
                                    &self.events,
                                )
                                .await
                        }
                        SessionConfigOptionCategory::ThoughtLevel => {
                            profile.remember(&options, &self.events).await;
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
        self.send_event(event);
    }

    async fn handle_list_sessions(
        &self,
        request_id: u64,
        cursor: Option<String>,
        cwd: Option<PathBuf>,
    ) {
        let result = if !self.features.list_supported {
            Err("The agent does not advertise session listing".into())
        } else {
            self.session
                .connection()
                .send_request(ListSessionsRequest::new().cursor(cursor).cwd(cwd))
                .block_task()
                .await
                .map(|page| SessionPage {
                    items: page.sessions,
                    cursor: page.next_cursor,
                })
                .map_err(|error| error.message)
        };
        self.send_event(Event::Sessions { request_id, result });
    }

    #[cfg(not(target_family = "wasm"))]
    async fn handle_quit_action(&self, action: Action) {
        match action {
            Action::CheckQuit => {
                let result = tokio::time::timeout(
                    std::time::Duration::from_secs(5),
                    only_active_session(self.session.connection(), self.session.session_id()),
                )
                .await
                .map_err(|_| "Timed out checking running sessions".to_owned())
                .and_then(|result| result.map_err(|error| request_error(&error)));
                self.send_event(Event::QuitChecked(result));
            }
            Action::StopForQuit => {
                let result = self
                    .session
                    .connection()
                    .send_request(CloseSessionRequest::new(self.session.session_id().clone()))
                    .block_task()
                    .await
                    .map(|_| ())
                    .map_err(|error| request_error(&error));
                self.send_event(Event::QuitStopped(result));
            }
            Action::PruneForQuit { discard } => {
                let _ = tokio::time::timeout(
                    std::time::Duration::from_secs(5),
                    prune_for_quit(
                        self.session.connection(),
                        self.session.session_id(),
                        &self.cwd,
                        discard,
                        self.features.delete_supported,
                    ),
                )
                .await;
                self.send_event(Event::QuitDeleted);
            }
            _ => {}
        }
    }

    async fn handle_close_session(&self, id: String) {
        let event = if !self.features.close_supported {
            Event::Error("The agent does not advertise session/close".into())
        } else {
            match self
                .session
                .connection()
                .send_request(CloseSessionRequest::new(id.clone()))
                .block_task()
                .await
            {
                Ok(_) => Event::SessionClosed(id),
                Err(error) => Event::Error(error.message),
            }
        };
        self.send_event(event);
    }

    async fn handle_delete_session(&self, id: String) {
        let result = if self.features.delete_supported {
            delete_session(self.session.connection(), id.clone()).await
        } else {
            Err("The agent does not advertise session/delete".into())
        };
        self.send_event(Event::SessionDeleteResult { id, result });
    }

    async fn handle_provider_route_action(&self, action: Action) {
        match action {
            Action::ListProviderRoutes => {
                let result = self
                    .session
                    .connection()
                    .send_request(ListProvidersRequest::new())
                    .block_task()
                    .await
                    .map(|reply| reply.providers)
                    .map_err(|error| error.message);
                self.send_event(Event::ProviderRoutes(result));
            }
            Action::SetProviderRoute(request) => {
                let event = match self
                    .session
                    .connection()
                    .send_request(request)
                    .block_task()
                    .await
                {
                    Ok(_) => Event::CommandStatus("Provider route updated".into()),
                    Err(error) => Event::Error(error.message),
                };
                self.send_event(event);
            }
            Action::DisableProviderRoute(request) => {
                let event = match self
                    .session
                    .connection()
                    .send_request(request)
                    .block_task()
                    .await
                {
                    Ok(_) => Event::CommandStatus("Provider route disabled".into()),
                    Err(error) => Event::Error(error.message),
                };
                self.send_event(event);
            }
            _ => {}
        }
    }

    async fn handle_provider_editor_action(&self, action: Action) {
        match action {
            Action::ListProviders => {
                let result =
                    provider_call::<ProviderList>(self.session.connection(), "list", json!({}))
                        .await
                        .map(|list| list.providers);
                self.send_event(Event::Providers(result));
            }
            Action::SaveProvider(edit) => {
                let result = provider_call::<Value>(self.session.connection(), "save", edit)
                    .await
                    .map(|_| ());
                self.send_event(Event::ProviderChanged(result));
            }
            Action::RemoveProvider(id) => {
                let result = provider_call::<Value>(
                    self.session.connection(),
                    "remove",
                    json!({ "id": id }),
                )
                .await
                .map(|_| ());
                self.send_event(Event::ProviderChanged(result));
            }
            Action::ProviderLogin { id, method, key } => {
                let result = provider_call(
                    self.session.connection(),
                    "login",
                    json!({ "id": id, "method": method, "key": key }),
                )
                .await;
                self.send_event(Event::ProviderLogin(result));
            }
            Action::ProviderLoginStatus => {
                let result =
                    provider_call(self.session.connection(), "login-status", json!({})).await;
                self.send_event(Event::ProviderLogin(result));
            }
            Action::CancelProviderLogin => {
                let _ =
                    provider_call::<Value>(self.session.connection(), "login-cancel", json!({}))
                        .await;
                self.send_event(Event::ProviderLoginCancelled);
            }
            _ => {}
        }
    }
}
