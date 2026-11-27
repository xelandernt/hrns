use std::path::PathBuf;

use agent_client_protocol::v1::schema::{
    PlanEntry, SessionConfigOption, SessionConfigOptionCategory,
};

use crate::acp::{Action, ExecutionStatus, ModelChange, SessionTarget};
use crate::attachments::{MAX_PROMPT_BYTES, Prompt};
use crate::options;
use crate::preferences::ApprovalMode;
use crate::skills;
use crate::transcript::Role;

use super::{App, LocalEffect, SessionInput, session_workspace_path};

impl App {
    pub fn take_local_effect(&mut self) -> Option<LocalEffect> {
        self.local_effect.take()
    }

    pub fn reconnect(&mut self) {
        if self.ready || matches!(self.status, "Reconnecting" | "Connecting") {
            self.command_status = Some("The connection is already open".into());
            return;
        }
        if self.loaded || (self.load_supported && self.session.is_some()) {
            self.transcript.begin_replay();
        }
        self.status = "Reconnecting";
        self.local_effect = Some(LocalEffect::Reconnect);
    }

    pub fn new_session(&mut self) {
        self.new_session_at(None);
    }

    pub(super) fn new_session_at(&mut self, path: Option<&str>) {
        if !self.prepare_session_switch() {
            return;
        }
        let Some(mut workspace) = self.workspace.clone() else {
            self.transcript.push(
                Role::Warning,
                "Connect once so the new session has a workspace".into(),
            );
            return;
        };
        if let Some(path) = path {
            if !self.managed_sessions {
                self.transcript.push(
                    Role::Warning,
                    "Selecting a workspace requires a Harness server".into(),
                );
                return;
            }
            workspace = session_workspace_path(&workspace, path, self.remote);
        }
        self.switch_session(SessionTarget {
            cwd: Some(workspace),
            session: None,
        });
    }

    pub fn load_session(&mut self, id: String) {
        self.load_session_at(id, None);
    }

    pub(super) fn load_session_at(&mut self, id: String, cwd: Option<PathBuf>) {
        if self.session.as_deref() == Some(&id) && self.ready {
            return;
        }
        if !self.prepare_session_switch() {
            return;
        }
        if !self.load_supported {
            self.transcript.push(
                Role::Warning,
                "The agent does not advertise session/load".into(),
            );
            return;
        }
        self.switch_session(SessionTarget {
            cwd,
            session: Some(id),
        });
    }

    pub(super) fn switch_session(&mut self, target: SessionTarget) {
        if self.managed_sessions && self.ready {
            self.preparing_switch = self
                .actions
                .unbounded_send(Action::PrepareSwitch(target))
                .is_ok();
        } else {
            self.connect_session(target);
        }
    }

    pub(super) fn connect_session(&mut self, target: SessionTarget) {
        self.ready = false;
        self.status = "Connecting";
        self.transcript.begin_replay();
        self.local_effect = Some(LocalEffect::Switch(target));
    }

    pub(super) fn can_switch(&self) -> bool {
        self.managed_sessions || (self.turns == 0 && !self.remote_busy())
    }

    pub(super) fn prepare_session_switch(&mut self) -> bool {
        if let Some(message) = self.session_switch_blocker() {
            self.transcript.push(Role::Warning, message.into());
            false
        } else {
            true
        }
    }

    pub(super) fn session_switch_blocker(&self) -> Option<&'static str> {
        if self.preparing_switch || (self.transcript.replaying() && !self.ready) {
            Some("Wait for the current session to connect")
        } else if self.attachment_reads > 0 {
            Some("Wait for the attachment to finish loading")
        } else if !self.can_switch() {
            Some("Stop the running turn before switching sessions")
        } else {
            None
        }
    }

    #[cfg(not(target_family = "wasm"))]
    pub fn local_attachment_path(&self, path: PathBuf) -> PathBuf {
        if !self.remote
            && let Some(workspace) = &self.workspace
        {
            return workspace.join(path);
        }
        path
    }

    pub(super) fn restore_session_input(&mut self, session: &str) {
        let Some(previous) = self.session.as_ref() else {
            return;
        };
        let input = self.session_inputs.remove(session).unwrap_or_default();
        self.session_inputs.insert(
            previous.clone(),
            SessionInput {
                draft: std::mem::replace(&mut self.draft, input.draft),
                queue: std::mem::replace(&mut self.queue, input.queue),
                attachments: std::mem::replace(&mut self.attachments, input.attachments),
            },
        );
        self.completion = None;
    }

    #[cfg(not(target_family = "wasm"))]
    pub fn check_quit(&self) -> bool {
        self.managed_sessions
            && self.ready
            && self.actions.unbounded_send(Action::CheckQuit).is_ok()
    }

    #[cfg(not(target_family = "wasm"))]
    pub fn stop_for_quit(&self) -> bool {
        self.ready && self.actions.unbounded_send(Action::StopForQuit).is_ok()
    }

    /// Ask the worker to discard untitled sessions and the current one when it has no prompt.
    #[cfg(not(target_family = "wasm"))]
    pub fn prune_on_quit(&self) -> bool {
        if !self.ready || !self.delete_supported {
            return false;
        }
        let discard = if self.prompted {
            None
        } else {
            self.session.clone()
        };
        self.actions
            .unbounded_send(Action::PruneForQuit { discard })
            .is_ok()
    }

    pub fn close_session(&mut self, id: Option<String>) {
        if !self.ready {
            self.transcript
                .push(Role::Warning, "Connect before closing a session".into());
            return;
        }
        if !self.close_supported {
            self.transcript.push(
                Role::Warning,
                "The agent does not advertise session/close".into(),
            );
            return;
        }
        let Some(id) = id
            .filter(|id| !id.is_empty())
            .or_else(|| self.session.clone())
        else {
            self.command_status = Some("There is no current session to close".into());
            return;
        };
        if self.session.as_deref() == Some(&id) && (self.turns > 0 || self.remote_busy()) {
            self.transcript.push(
                Role::Warning,
                "Stop the running turn before closing its session".into(),
            );
            return;
        }
        let _ = self.actions.unbounded_send(Action::CloseSession(id));
    }

    pub fn delete_session(&mut self, id: String) {
        if id.is_empty() {
            self.transcript
                .push(Role::Warning, "Use /delete <session id>".into());
            return;
        }
        if let Err(message) = self.request_session_delete(&id) {
            self.transcript.push(Role::Warning, message.into());
        }
    }

    pub(super) fn session_delete_blocker(&self, id: &str) -> Option<&'static str> {
        if !self.ready {
            Some("Connect before deleting a session")
        } else if !self.delete_supported {
            Some("The agent does not advertise session/delete")
        } else if self.session.as_deref() == Some(id) && !self.can_switch() {
            Some("Stop the running turn before deleting its session")
        } else {
            None
        }
    }

    pub(super) fn request_session_delete(&self, id: &str) -> Result<(), &'static str> {
        if let Some(message) = self.session_delete_blocker(id) {
            return Err(message);
        }
        self.actions
            .unbounded_send(Action::DeleteSession(id.to_owned()))
            .map_err(|_| "Disconnected from the agent")
    }

    pub(super) fn session_delete_result(&mut self, id: String, result: Result<(), String>) {
        if let Err(error) = result {
            if !self.agents_delete_failed(&id, &error) {
                self.transcript.push(Role::Error, error);
            }
            return;
        }
        self.session_inputs.remove(&id);
        self.command_status = Some(format!("Deleted session {id}"));
        let active = self.session.as_deref() == Some(&id);
        self.agents_delete_completed(&id, active);
        if active {
            self.new_session();
        }
    }

    pub(super) fn update_plan(&mut self, plan: Vec<PlanEntry>) {
        if !plan.is_empty() && !self.todo_auto_opened {
            self.todo_auto_opened = true;
            self.todo_open = true;
        }
        self.plan = plan;
    }

    pub(super) fn show_todos(&mut self) {
        self.todo_open = true;
        self.todo_scroll = Some(0);
    }

    pub fn toggle_sidebar(&mut self) {
        if self.todo_open {
            self.todo_open = false;
            self.todo_scroll = None;
            self.preferences.sidebar = false;
            self.save_preferences();
            return;
        }
        self.preferences.sidebar = !self.preferences.sidebar;
        self.save_preferences();
    }

    pub fn toggle_thoughts(&mut self) {
        self.preferences.show_thoughts = !self.preferences.show_thoughts;
        self.transcript
            .show_thoughts(self.preferences.show_thoughts);
        let state = if self.preferences.show_thoughts {
            "shown"
        } else {
            "collapsed"
        };
        self.command_status = Some(format!("Reasoning is now {state}"));
        self.save_preferences();
    }

    pub fn set_theme(&mut self, name: String) {
        self.theme.clone_from(&name);
        self.preferences.theme = Some(name);
        self.command_status = Some(format!("Theme · {}", self.theme));
        self.save_preferences();
    }

    pub fn set_approval_mode(&mut self, mode: ApprovalMode) -> bool {
        let Some(key) = self.approval_preference.as_ref().filter(|_| self.ready) else {
            self.transcript.push(
                Role::Warning,
                "Open a session before changing its approval mode".into(),
            );
            return false;
        };
        self.approval_mode = mode;
        self.command_status = Some(format!("Session approval mode · {}", mode.label()));
        if let Err(error) = key.save(mode) {
            self.transcript.push(
                Role::Warning,
                format!("Could not save this session's approval mode ({error})"),
            );
        }
        true
    }

    pub(super) fn save_preferences(&mut self) {
        self.local_effect = Some(LocalEffect::SavePreferences(self.preferences.clone()));
    }

    pub(super) fn trim_activity(&mut self) {
        while self.activity.len() > 8 {
            self.activity.pop_front();
        }
    }

    pub(super) fn update_execution(&mut self) {
        if !self.ready
            || self.session.as_ref() != self.execution.as_ref().map(|state| &state.session_id)
        {
            return;
        }
        let Some(state) = &self.execution else { return };
        match state.status {
            ExecutionStatus::Idle if self.turns == 0 => {
                self.status = "Connected";
                self.flush_queue();
            }
            ExecutionStatus::Waiting if self.status != "Cancelling" => {
                self.status = "Waiting";
            }
            ExecutionStatus::Running if self.status != "Cancelling" => {
                self.status = "Streaming";
            }
            _ => {}
        }
    }

    pub(super) fn remote_busy(&self) -> bool {
        self.execution.as_ref().is_some_and(|state| {
            self.session.as_ref() == Some(&state.session_id)
                && !matches!(state.status, ExecutionStatus::Idle)
        })
    }

    pub(super) fn flush_queue(&mut self) {
        if !self.ready
            || self.turns > 0
            || self.remote_busy()
            || self.prompt_setup_message().is_some()
        {
            return;
        }
        if let Some(prompt) = self.queue.pop_front()
            && let Err(prompt) = self.send_prompt(prompt)
        {
            self.queue.push_front(prompt);
        }
    }

    pub(super) fn clear_queue_for_switch(&mut self) {
        if !self.queue.is_empty() {
            self.queue.clear();
            self.transcript.push(
                Role::Note,
                "Cleared queued prompts for the previous session".into(),
            );
        }
    }

    pub(super) fn update_options(&mut self, options: Vec<SessionConfigOption>) {
        if options::selected_model(&options).map(|choice| &choice.value)
            != options::selected_model(&self.options).map(|choice| &choice.value)
        {
            self.usage = None;
            self.last_usage_tokens = None;
        }
        self.options = options;
        self.refresh_completion();
        if let Some(picker) = &self.picker {
            let last = self.picker_rows(picker).len().saturating_sub(1);
            if let Some(picker) = &mut self.picker {
                picker.selected = picker.selected.min(last);
            }
        }
    }

    pub(super) fn model_selected(
        &mut self,
        change: ModelChange,
        result: Result<Vec<SessionConfigOption>, String>,
    ) {
        self.model_change_pending = false;
        let options = match result {
            Ok(options) => options,
            Err(error) => {
                self.transcript.push(Role::Error, error);
                return;
            }
        };
        self.update_options(options);
        if let ModelChange::Explicit { choose_effort } = change {
            if self.provider_model_missing() {
                self.selected_provider = None;
            }
            if choose_effort
                && self.draft.text().is_empty()
                && self.picker.is_none()
                && self.providers.is_none()
            {
                self.choose_model_effort();
            }
        }
        self.flush_queue();
    }

    pub(super) fn active_model_provider(&self) -> Option<&str> {
        options::selected_model(&self.options).and_then(options::model_provider)
    }

    pub(super) fn provider_model_missing(&self) -> bool {
        self.selected_provider
            .as_ref()
            .is_some_and(|selected| self.active_model_provider() != Some(selected.id.as_str()))
    }

    pub(super) fn prompt_setup_message(&self) -> Option<&str> {
        if !self.ready {
            return None;
        }
        if self.model_change_pending {
            return Some("Switching models…");
        }
        if !self.provider_editor {
            return None;
        }
        if self.provider_model_missing() {
            return Some(self.provider_setup_message().unwrap_or(
                "No model is active. Check /login or configure a model in /providers.",
            ));
        }
        let model = self
            .options
            .iter()
            .find(|option| options::matches_category(option, &SessionConfigOptionCategory::Model));
        match model {
            None => Some("Set up a provider and model to send messages."),
            Some(option) => option.meta.as_ref()?.get("hrns/prompt/setup")?.as_str(),
        }
    }

    pub(super) fn prepare_prompt(&mut self, text: String) -> Option<Prompt> {
        if let Some(message) = self.prompt_setup_message() {
            self.transcript.push(Role::Warning, message.to_owned());
            if !self.model_change_pending {
                if self.provider_setup_message().is_some() {
                    self.show_provider_login();
                } else if self.selected_provider.is_none() {
                    self.show_providers();
                }
            }
            return None;
        }
        if self.attachment_reads > 0 {
            self.transcript.push(
                Role::Warning,
                "Wait for the attachment to finish loading".into(),
            );
            return None;
        }
        let size = self
            .attachments
            .iter()
            .fold(text.len(), |size, attachment| {
                size.saturating_add(attachment.size)
            });
        if size > MAX_PROMPT_BYTES {
            self.transcript.push(
                Role::Error,
                "Prompt and attachments exceed the ACP prompt limit".into(),
            );
            return None;
        }
        Some(Prompt {
            skill_meta: skills::prompt_meta(&text, &self.skills),
            text,
            attachments: std::mem::take(&mut self.attachments),
        })
    }

    pub(super) fn send_draft(&mut self, text: String) {
        let Some(prompt) = self.prepare_prompt(text) else {
            return;
        };
        match self.send_prompt(prompt) {
            Ok(()) => {
                self.draft.submit();
            }
            Err(prompt) => self.attachments = prompt.attachments,
        }
    }

    pub(super) fn send_prompt(&mut self, prompt: Prompt) -> Result<(), Prompt> {
        if self.preparing_switch {
            return Err(prompt);
        }
        let mut displayed = prompt.text.clone();
        for attachment in &prompt.attachments {
            displayed.push_str(&format!("\n[Attachment: {}]", attachment.name));
        }
        if let Err(error) = self.actions.unbounded_send(Action::Prompt(prompt)) {
            let Action::Prompt(prompt) = error.into_inner() else {
                unreachable!()
            };
            return Err(prompt);
        }
        self.transcript.push(Role::User, displayed);
        self.prompted = true;
        self.turns += 1;
        self.transcript.follow();
        self.status = "Streaming";
        self.activity.push_back("prompt sent".into());
        self.trim_activity();
        Ok(())
    }
}
