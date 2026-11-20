use agent_client_protocol::v1::schema::SessionConfigOptionCategory;

use super::{App, LocalEffect};
use crate::acp::Action;
use crate::options;
use crate::transcript::Role;

impl App {
    pub(super) fn submit(&mut self) {
        let input = self.draft.text().to_owned();
        match input.trim() {
            "/help" => {
                self.draft.submit();
                let mut commands = vec![
                    "/sessions [next] · /new · /load <id> · /models · /effort · /todo".to_owned(),
                    "/find <text> · /copy · /reconnect".to_owned(),
                    "/queue <prompt> · /resume-queue · /clear-queue".to_owned(),
                    "/attach [path] · /detach <filename> · /close [id] · /delete <id>".to_owned(),
                ];
                commands.extend(
                    self.commands
                        .iter()
                        .map(|(name, description)| format!("/agent {name} · {description}")),
                );
                self.transcript.push(Role::Note, commands.join("\n"));
            }
            command @ ("/sessions" | "/sessions next") => {
                self.draft.submit();
                let _ = self.actions.unbounded_send(Action::ListSessions {
                    more: command.ends_with(" next"),
                });
            }
            "/new" => {
                self.draft.submit();
                self.new_session();
            }
            "/load" => {
                self.draft.submit();
                self.transcript
                    .push(Role::Note, "Use /load <session id>".into());
            }
            text if text.starts_with("/load ") => {
                self.draft.submit();
                let id = text["/load ".len()..].trim();
                if id.is_empty() {
                    self.transcript
                        .push(Role::Note, "Use /load <session id>".into());
                } else {
                    self.load_session(id.to_owned());
                }
            }
            "/close" => {
                self.draft.submit();
                self.close_session(None);
            }
            text if text.starts_with("/close ") => {
                self.draft.submit();
                self.close_session(Some(text["/close ".len()..].trim().to_owned()));
            }
            "/delete" => {
                self.draft.submit();
                self.transcript
                    .push(Role::Note, "Use /delete <session id>".into());
            }
            text if text.starts_with("/delete ") => {
                self.draft.submit();
                self.delete_session(text["/delete ".len()..].trim().to_owned());
            }
            "/todo" | "/todos" => {
                self.draft.submit();
                let text = if self.plan.is_empty() {
                    "No plan available".into()
                } else {
                    self.plan.join("\n")
                };
                self.transcript.push(Role::Note, text);
            }
            command if command.starts_with("/agent ") => {
                let command = command["/agent ".len()..].trim();
                let name = command.split_whitespace().next();
                if self
                    .commands
                    .iter()
                    .any(|(advertised, _)| name == Some(advertised.as_str()))
                {
                    self.send_draft(format!("/{command}"));
                } else {
                    self.transcript.push(
                        Role::Error,
                        "The agent did not advertise that command".into(),
                    );
                }
            }
            "/model" | "/models" => self.config_command(SessionConfigOptionCategory::Model, ""),
            "/effort" => self.config_command(SessionConfigOptionCategory::ThoughtLevel, ""),
            text if text.starts_with("/model ") => {
                self.config_command(
                    SessionConfigOptionCategory::Model,
                    text["/model ".len()..].trim(),
                );
            }
            text if text.starts_with("/effort ") => {
                self.config_command(
                    SessionConfigOptionCategory::ThoughtLevel,
                    text["/effort ".len()..].trim(),
                );
            }
            "/reconnect" => {
                self.draft.submit();
                self.reconnect();
            }
            "/copy" => {
                self.draft.submit();
                if self.turns == 0 && !self.remote_busy() {
                    self.local_effect = self
                        .transcript
                        .latest_agent()
                        .map(|text| LocalEffect::Copy(text.to_owned()));
                }
                if self.local_effect.is_none() {
                    self.transcript
                        .push(Role::Note, "No completed response to copy".into());
                }
            }
            text if text.starts_with("/find ") => {
                self.draft.submit();
                if !self.transcript.find(text["/find ".len()..].trim()) {
                    self.transcript
                        .push(Role::Note, "No matching transcript text".into());
                }
            }
            command if command == "/attach" || command.starts_with("/attach ") => {
                self.draft.submit();
                if self.attachments_supported {
                    self.local_effect = Some(LocalEffect::Attach(
                        command["/attach".len()..].trim().to_owned(),
                    ));
                } else {
                    self.transcript.push(
                        Role::Error,
                        "The agent does not accept embedded attachments".into(),
                    );
                }
            }
            command if command.starts_with("/detach ") => {
                self.draft.submit();
                let name = command["/detach ".len()..].trim();
                self.attachments
                    .retain(|attachment| attachment.name != name);
            }
            "/resume-queue" => {
                self.draft.submit();
                self.queue_paused = false;
                self.flush_queue();
            }
            "/clear-queue" => {
                self.draft.submit();
                self.queue.clear();
                self.queue_paused = false;
            }
            text if text.starts_with("/queue ") => {
                let text = text["/queue ".len()..].trim();
                if !text.is_empty()
                    && let Some(prompt) = self.prepare_prompt(text.to_owned())
                {
                    self.draft.submit();
                    self.queue.push_back(prompt);
                    self.flush_queue();
                }
            }
            _ => {
                if !self.ready {
                    self.transcript.push(
                        Role::Note,
                        "Not connected · use /reconnect, /new or /load <id>".into(),
                    );
                    return;
                }
                let available = self.status != "Cancelling";
                if available {
                    self.send_draft(input);
                }
            }
        }
    }

    fn config_command(&mut self, category: SessionConfigOptionCategory, input: &str) {
        self.draft.submit();
        let selected = self
            .options
            .iter()
            .find(|option| options::matches_category(option, &category));
        let notice = match selected {
            Some(option) if input.is_empty() => options::summary(option),
            Some(option) => match options::value(option, input) {
                Some(value) => {
                    let _ = self
                        .actions
                        .unbounded_send(Action::SetOption(option.id.clone(), value));
                    return;
                }
                None => "Invalid configuration value".into(),
            },
            None => "The agent did not advertise this session setting".into(),
        };
        self.transcript.push(Role::Note, notice);
    }
}
