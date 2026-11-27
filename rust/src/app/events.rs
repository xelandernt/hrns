use agent_client_protocol::v1::schema::ToolKind;

use crate::acp::{Event, PermissionRequest};
use crate::attachments::MAX_PROMPT_BYTES;
use crate::elicitation::FormState;
use crate::mcp_types::ConsentDecision;
use crate::preferences::{ApprovalKey, ApprovalMode};
use crate::transcript::Role;

use super::picker;
use super::{App, PendingApproval, PendingElicitation, PendingMcpConsent};

impl App {
    pub fn event(&mut self, event: Event) {
        if matches!(
            &event,
            Event::UserChunk(_)
                | Event::Chunk(_)
                | Event::Thought(_)
                | Event::Tool { .. }
                | Event::Plan(_)
        ) {
            self.prompted = true;
        }
        match event {
            Event::Ready {
                session,
                approval_scope,
                workspace,
                remote,
                client_io,
                attachments_supported,
                loaded,
                load_supported,
                close_supported,
                delete_supported,
                managed_sessions,
                provider_editor,
                provider_routing,
                execution,
            } => {
                let key = ApprovalKey::new(&approval_scope, &session);
                self.approval_mode = if loaded {
                    key.load()
                } else {
                    ApprovalMode::Ask
                };
                self.approval_preference = Some(key);
                self.transcript.commit_replay();
                self.attachments_supported = attachments_supported;
                self.load_supported = load_supported;
                self.close_supported = close_supported;
                self.delete_supported = delete_supported;
                self.managed_sessions = managed_sessions;
                self.provider_editor = provider_editor;
                self.provider_routing = provider_routing;
                self.reasoning_tokens = None;
                self.loaded = loaded;
                if !loaded {
                    self.session_title = None;
                }
                self.workspace = Some(workspace);
                self.remote = remote;
                self.client_io = client_io;
                let session_changed = self.session.as_deref() != Some(&session);
                if !loaded || session_changed {
                    self.transcript.show_details(self.preferences.show_details);
                }
                if session_changed {
                    self.selected_provider = None;
                    if self.managed_sessions {
                        self.restore_session_input(&session);
                    } else if self.session.is_some() {
                        self.clear_queue_for_switch();
                    }
                    self.plan.clear();
                    self.todo_auto_opened = false;
                    self.todo_scroll = self.todo_scroll.map(|_| 0);
                }
                if let Some(plan) = self.replay_plan.take() {
                    self.update_plan(plan);
                }
                self.session = Some(session);
                if let Some(state) = execution {
                    self.event(Event::Execution(state));
                }
                self.ready = true;
                self.status = "Connected";
                self.load_waiting_sessions();
                self.refresh_completion();
                self.activity.push_back("connected".into());
                self.trim_activity();
                if self.provider_model_missing() {
                    self.activate_provider_model();
                }
                self.update_execution();
                self.flush_queue();
            }
            Event::Execution(state) => {
                let matches = self
                    .session
                    .as_ref()
                    .is_none_or(|id| *id == state.session_id);
                let newer = self.execution.as_ref().is_none_or(|old| {
                    old.session_id != state.session_id || old.revision < state.revision
                });
                if matches && newer {
                    if let Some(usage) = &state.last_usage {
                        self.last_usage_tokens = usage.total_tokens;
                    }
                    self.execution = Some(state);
                    self.update_execution();
                }
            }
            Event::ModelSelected { change, result } => self.model_selected(change, result),
            Event::Options(options) => {
                self.update_options(options);
                if self.ready
                    && (self.take_provider_login_refresh() || self.provider_model_missing())
                {
                    self.activate_provider_model();
                }
                self.flush_queue();
            }
            Event::Sessions { request_id, result } => self.sessions_received(request_id, result),
            Event::SwitchPrepared(result) => {
                self.preparing_switch = false;
                match result {
                    Ok(target) => self.connect_session(target),
                    Err(error) => self.transcript.push(Role::Error, error),
                }
            }
            #[cfg(not(target_family = "wasm"))]
            Event::QuitChecked(_) | Event::QuitStopped(_) | Event::QuitDeleted => {}
            Event::AgentCommands(commands) => self.commands = commands,
            Event::Skills(skills) => {
                self.skills = skills;
                if let Some(picker) = &mut self.picker
                    && matches!(picker.source, picker::Source::Skills)
                {
                    picker.selected = 0;
                }
                self.refresh_completion();
            }
            Event::SessionTitle(title) => self.session_title = title,
            Event::ReasoningTokens(tokens) => self.reasoning_tokens = tokens,
            Event::Mode(mode) => self.mode = Some(mode),
            Event::Providers(result) => self.provider_list(result),
            Event::ProviderRoutes(result) => self.provider_routes(result),
            Event::ProviderChanged(result) => self.provider_changed(result),
            Event::ProviderLogin(result) => self.provider_login(result),
            Event::ProviderLoginCancelled => self.provider_login_cancelled(),
            Event::LoadingAttachment => self.attachment_reads += 1,
            Event::Attachment(attachment) => {
                self.attachment_reads = self.attachment_reads.saturating_sub(1);
                let attachment = match attachment {
                    Ok(attachment) => attachment,
                    Err(error) => {
                        self.transcript.push(Role::Error, error);
                        return;
                    }
                };
                let staged: usize = self.attachments.iter().map(|item| item.size).sum();
                if staged.saturating_add(attachment.size) > MAX_PROMPT_BYTES {
                    self.transcript.push(
                        Role::Error,
                        "Attachments exceed the ACP prompt limit".into(),
                    );
                } else if !self
                    .attachments
                    .iter()
                    .any(|item| item.name == attachment.name)
                {
                    self.command_status = Some(format!("Attached {}", attachment.name));
                    self.attachments.push(*attachment);
                } else {
                    self.command_status = Some(format!("{} is already attached", attachment.name));
                }
            }
            Event::Plan(plan) => {
                if self.ready {
                    self.update_plan(plan);
                } else {
                    self.replay_plan = Some(plan);
                }
            }
            Event::Usage { context, cost } => {
                self.usage = context;
                self.usage_cost = cost;
            }
            Event::SessionClosed(id) => {
                self.command_status =
                    Some(format!("Closed session {id}; the agent keeps its history"));
                if self.session.as_deref() == Some(&id) {
                    self.new_session();
                }
            }
            Event::SessionDeleteResult { id, result } => self.session_delete_result(id, result),
            Event::Tool { id, display } => {
                self.activity.push_back(format!(
                    "{} · {}",
                    crate::acp::status_label(display.status),
                    display.title
                ));
                self.trim_activity();
                self.transcript.tool(id, display, self.turns > 0);
            }
            Event::UserChunk(text) => self.transcript.append(Role::User, text),
            Event::Chunk(text) => self.transcript.append(Role::Agent, text),
            Event::Thought(text) => self.transcript.append_thought(text, self.turns > 0),
            Event::CommandStatus(text) => self.command_status = Some(text),
            Event::Warning(text) => self.transcript.push(Role::Warning, text),
            Event::Error(text) => {
                if !self.ready {
                    self.transcript.cancel_replay();
                }
                self.transcript.push(Role::Error, text);
            }
            Event::End { interrupted } => {
                self.transcript.finish_actions();
                self.turns = self.turns.saturating_sub(1);
                self.activity.push_back(if interrupted {
                    "turn cancelled".into()
                } else {
                    "turn complete".into()
                });
                self.trim_activity();
                if self.turns == 0 {
                    self.clear_approvals();
                    self.elicitations.clear();
                    self.status = "Connected";
                    self.update_execution();
                    self.flush_queue();
                }
            }
            Event::Disconnected => {
                self.transcript.finish_actions();
                self.approval_mode = ApprovalMode::Ask;
                self.approval_preference = None;
                self.preparing_switch = false;
                self.model_change_pending = false;
                self.agents_delete_interrupted();
                self.sessions_received(
                    self.session_request,
                    Err("Disconnected from the agent".into()),
                );
                self.replay_plan = None;
                self.providers = None;
                if self.transcript.cancel_replay() {
                    self.transcript
                        .push(Role::Warning, "The session did not finish loading".into());
                }
                self.ready = false;
                self.turns = 0;
                self.execution = None;
                self.clear_approvals();
                self.elicitations.clear();
                self.mcp_consent.clear();
                self.status = "Disconnected";
                self.refresh_completion();
                self.activity.push_back("disconnected".into());
                self.trim_activity();
            }
            Event::ConnectionChanged(destination) => {
                self.approval_mode = ApprovalMode::Ask;
                self.approval_preference = None;
                self.mcp_grants.clear();
                self.destination = destination;
                self.workspace = None;
                self.session = None;
                self.session_title = None;
                self.mode = None;
                self.loaded = false;
                self.prompted = false;
                self.ready = false;
                self.transcript.begin_replay();
                self.replay_plan = None;
                self.plan.clear();
                self.todo_auto_opened = false;
                self.usage = None;
                self.usage_cost = None;
                self.last_usage_tokens = None;
                self.reasoning_tokens = None;
                self.options.clear();
                self.commands.clear();
                self.skills.clear();
                self.mcp_health.clear();
                #[cfg(not(target_family = "wasm"))]
                {
                    self.mcp_authentication = None;
                }
                self.load_supported = false;
                self.close_supported = false;
                self.delete_supported = false;
                self.managed_sessions = false;
                self.session_inputs.clear();
                self.provider_editor = false;
                self.provider_routing = false;
                self.providers = None;
                self.selected_provider = None;
                self.attachments_supported = false;
                self.clear_queue_for_switch();
                self.refresh_completion();
            }
            Event::Connecting => {
                #[cfg(not(target_family = "wasm"))]
                {
                    self.mcp_authentication = None;
                }
                self.approval_mode = ApprovalMode::Ask;
                self.approval_preference = None;
                self.model_change_pending = false;
                self.ready = false;
                self.prompted = false;
                self.turns = 0;
                self.execution = None;
                self.clear_approvals();
                self.elicitations.clear();
                self.mcp_consent.clear();
                if self.transcript.replaying() {
                    self.transcript.begin_replay();
                    self.session_title = None;
                    self.usage = None;
                    self.usage_cost = None;
                    self.last_usage_tokens = None;
                }
                self.status = "Reconnecting";
                self.refresh_completion();
            }
            Event::SwitchReplayReset => {
                self.transcript.retry_replay();
                self.replay_plan = None;
                self.event(Event::Connecting);
            }
            Event::McpConsent {
                name,
                transport,
                target,
                respond,
            } => {
                if self.mcp_grants.contains(&name) {
                    self.transcript
                        .push(Role::Note, format!("MCP {name} reused until exit"));
                    let _ = respond.send(ConsentDecision::Session);
                } else {
                    self.transcript
                        .push(Role::Warning, format!("MCP consent · {name} ({transport})"));
                    self.mcp_consent.push_back(PendingMcpConsent {
                        name,
                        transport,
                        target,
                        respond,
                        selected: 0,
                    });
                }
            }
            Event::McpHealth(health) => {
                for item in &health {
                    if let Some(reason) = &item.startup_failure
                        && !self.mcp_health.iter().any(|previous| {
                            previous.name == item.name
                                && previous.startup_failure.as_ref() == Some(reason)
                        })
                    {
                        self.transcript.push(
                            Role::Error,
                            format!("MCP {} unavailable · {reason}", item.name),
                        );
                    }
                }
                self.mcp_health = health;
            }
            #[cfg(not(target_family = "wasm"))]
            Event::McpAuthenticated {
                name,
                action,
                result,
            } => {
                self.mcp_authentication_result(name, action, result);
            }
            #[cfg(not(target_family = "wasm"))]
            Event::McpTools { name, result } => self.mcp_tools_result(&name, result),
            Event::Permission(request) => self.request_permission(request),
            Event::Elicitation { form, respond } => {
                self.elicitations.push_back(PendingElicitation {
                    state: FormState::new(form),
                    respond,
                });
            }
        }
    }

    fn request_permission(&mut self, request: PermissionRequest) {
        if self.ready
            && let Some(answer) = self.auto_permission(&request)
        {
            let _ = request.respond.send(answer);
            return;
        }
        self.transcript.show_pending_approval(&request);
        self.approvals.push_back(PendingApproval {
            request,
            selected: 0,
            detail_scroll: 0,
        });
    }

    pub(super) fn clear_approvals(&mut self) {
        for pending in self.approvals.drain(..) {
            let request = pending.request;
            self.transcript
                .resolve_approval(&request.tool_call_id, &request.title, None);
        }
    }

    pub(super) fn auto_permission(&self, request: &PermissionRequest) -> Option<Option<usize>> {
        let allowed = |kind: agent_client_protocol::v1::schema::PermissionOptionKind| {
            matches!(
                kind,
                agent_client_protocol::v1::schema::PermissionOptionKind::AllowOnce
                    | agent_client_protocol::v1::schema::PermissionOptionKind::AllowAlways
            )
        };
        let allow = request
            .options
            .iter()
            .position(|option| allowed(option.kind))?;
        match self.approval_mode {
            ApprovalMode::Ask => None,
            ApprovalMode::Reads => matches!(
                request.kind,
                Some(ToolKind::Read | ToolKind::Search | ToolKind::Think)
            )
            .then_some(Some(allow)),
            ApprovalMode::All => Some(Some(allow)),
        }
    }
}
