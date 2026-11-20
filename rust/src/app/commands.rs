use agent_client_protocol::v1::schema::SessionConfigOptionCategory;
use std::path::PathBuf;

use super::{App, LocalEffect};
use crate::acp::Action;
use crate::options;
use crate::preferences::ApprovalMode;
use crate::theme;
use crate::transcript::Role;

impl App {
    pub(super) fn submit(&mut self) {
        let input = self.draft.text().to_owned();
        match input.trim() {
            "/help" => {
                self.draft.submit();
                let mut commands = vec![
                    "/sessions [next] · /new · /load <id> · /models · /effort · /todo".to_owned(),
                    "/find <text> · /copy · /reconnect · /skills · /rename <title>".to_owned(),
                    "/sidebar · /thoughts · /queue <prompt> · /resume-queue · /clear-queue"
                        .to_owned(),
                    "/attach [path] · /detach <filename> · /close [id] · /delete <id>".to_owned(),
                    "/providers · /provider set <id> <type> [url|disable]".to_owned(),
                    "/permissions [ask|reads|all] · /settings · /theme [name]".to_owned(),
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
                    cwd: self.workspace.clone().map(PathBuf::from),
                });
            }
            "/new" => {
                self.draft.submit();
                self.new_session();
            }
            "/sidebar" => {
                self.draft.submit();
                self.toggle_sidebar();
            }
            "/thoughts" => {
                self.draft.submit();
                self.toggle_thoughts();
            }
            "/skills" => {
                self.draft.submit();
                let text = if self.skills.is_empty() {
                    "The agent advertises no skills".into()
                } else {
                    self.skills
                        .iter()
                        .filter(|skill| skill.user_invocable)
                        .map(|skill| {
                            format!("${} · {} · {}", skill.id, skill.label(), skill.description)
                        })
                        .collect::<Vec<_>>()
                        .join("\n")
                };
                self.transcript.push(Role::Note, text);
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
            "/mcp" => {
                self.draft.submit();
                let text = if self.mcp_health.is_empty() {
                    "No MCP servers are exported for this connection".into()
                } else {
                    self.mcp_health
                        .iter()
                        .map(|item| {
                            let protocol = if item.protocol.is_empty() {
                                String::new()
                            } else {
                                format!(" · {}", item.protocol)
                            };
                            let detail = if item.detail.is_empty() {
                                String::new()
                            } else {
                                format!("\n  {}", item.detail)
                            };
                            format!(
                                "{} · {} · {} · {}{protocol}{detail}",
                                item.name, item.transport, item.state, item.target
                            )
                        })
                        .collect::<Vec<_>>()
                        .join("\n")
                };
                self.transcript.push(Role::Note, text);
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
            "/providers" => {
                self.draft.submit();
                if self.ready {
                    let _ = self.actions.unbounded_send(Action::ListProviders);
                } else {
                    let text = self.provider_summary();
                    self.transcript.push(Role::Note, text);
                }
            }
            "/provider" | "/provider set" | "/provider disable" => {
                self.draft.submit();
                self.transcript.push(
                    Role::Note,
                    "Use /provider set <id> <api-type> [base-url] or /provider disable <id>".into(),
                );
            }
            text if text.starts_with("/provider set ") => {
                self.draft.submit();
                let mut words = text["/provider set ".len()..].split_whitespace();
                let (Some(id), Some(api_type)) = (words.next(), words.next()) else {
                    self.transcript.push(
                        Role::Note,
                        "Use /provider set <id> <api-type> [base-url]".into(),
                    );
                    return;
                };
                let base_url = words.next().map(str::to_owned);
                let _ = self.actions.unbounded_send(Action::SetProvider {
                    id: id.to_owned(),
                    api_type: api_type.to_owned(),
                    base_url,
                });
            }
            text if text.starts_with("/provider disable ") => {
                self.draft.submit();
                let id = text["/provider disable ".len()..].trim();
                if id.is_empty() {
                    self.transcript
                        .push(Role::Note, "Use /provider disable <id>".into());
                } else {
                    let _ = self
                        .actions
                        .unbounded_send(Action::DisableProvider(id.to_owned()));
                }
            }
            "/permissions" => {
                self.draft.submit();
                self.transcript.push(
                    Role::Note,
                    format!(
                        "Approval mode · {}\n/permissions ask · reads · all",
                        self.approval_mode.label()
                    ),
                );
            }
            text if text.starts_with("/permissions ") => {
                self.draft.submit();
                match ApprovalMode::parse(text["/permissions ".len()..].trim()) {
                    Some(mode) => {
                        let label = mode.label().to_owned();
                        self.set_approval_mode(mode);
                        self.transcript
                            .push(Role::Note, format!("Approval mode · {label}"));
                    }
                    None => self
                        .transcript
                        .push(Role::Note, "Use /permissions ask · reads · all".into()),
                }
            }
            "/theme" => {
                self.draft.submit();
                self.transcript.push(
                    Role::Note,
                    format!("Theme · {}\n{}", self.theme, theme::NAMES.join(" · ")),
                );
            }
            text if text.starts_with("/theme ") => {
                self.draft.submit();
                let name = text["/theme ".len()..].trim();
                if theme::NAMES.contains(&name) {
                    self.set_theme(name.to_owned());
                    self.transcript.push(Role::Note, format!("Theme · {name}"));
                } else {
                    self.transcript.push(
                        Role::Note,
                        format!("Unknown theme · {}", theme::NAMES.join(" · ")),
                    );
                }
            }
            "/settings" => {
                self.draft.submit();
                self.print_settings();
            }
            text if text.starts_with("/settings ") => {
                self.draft.submit();
                self.apply_setting(text["/settings ".len()..].trim());
            }
            command if command.starts_with("/rename ") || command == "/rename" => {
                self.draft.submit();
                self.transcript.push(
                    Role::Note,
                    "Standard ACP has no session rename; the agent updates its own title".into(),
                );
            }
            command if command.starts_with("/agent ") => {
                let command = command["/agent ".len()..].trim();
                let name = command.split_whitespace().next();
                if self
                    .commands
                    .iter()
                    .any(|(advertised, _)| name == Some(advertised.as_str()))
                {
                    if super::COMMANDS
                        .iter()
                        .any(|(local, _)| local.trim_start_matches('/') == name.unwrap_or_default())
                    {
                        self.transcript.push(
                            Role::Note,
                            format!(
                                "{} also names a local command; this is the agent's",
                                name.unwrap_or_default()
                            ),
                        );
                    }
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
                if self.turns > 0 || self.remote_busy() {
                    self.transcript.push(
                        Role::Note,
                        "Wait for the turn to finish before copying".into(),
                    );
                } else if let Some(text) = self.transcript.latest_agent() {
                    self.local_effect = Some(LocalEffect::Copy(text.to_owned()));
                } else {
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

    fn print_settings(&mut self) {
        let model = self
            .options
            .iter()
            .find(|option| options::matches_category(option, &SessionConfigOptionCategory::Model))
            .map(options::summary)
            .unwrap_or_else(|| "none advertised".into());
        let text = format!(
            "theme {} · /theme <name>\npermissions {} · /permissions <mode>\nclient I/O {} (applies to the next connection)\nsidebar {} · tools {} · thoughts {}\nworkspace {}\nsession {}\nmodel {model}\n/settings theme <name> · permissions <ask|reads|all> · sidebar on|off · tools on|off · thoughts on|off",
            self.theme,
            self.approval_mode.label(),
            self.client_io.label(),
            on_off(self.preferences.sidebar),
            on_off(self.preferences.expand_tools),
            on_off(self.preferences.show_thoughts),
            self.workspace.as_deref().unwrap_or("none"),
            self.session
                .as_ref()
                .map_or("none", |session| session.as_str()),
        );
        self.transcript.push(Role::Note, text);
    }

    fn apply_setting(&mut self, setting: &str) {
        let mut words = setting.split_whitespace();
        let Some(name) = words.next() else {
            self.print_settings();
            return;
        };
        let value = words.next();
        match (name, value) {
            ("theme", Some(name)) if theme::NAMES.contains(&name) => {
                self.set_theme(name.to_owned());
                self.transcript.push(Role::Note, format!("Theme · {name}"));
            }
            ("theme", _) => self
                .transcript
                .push(Role::Note, format!("Theme · {}", theme::NAMES.join(" · "))),
            ("permissions", Some(value)) => match ApprovalMode::parse(value) {
                Some(mode) => {
                    let label = mode.label().to_owned();
                    self.set_approval_mode(mode);
                    self.transcript
                        .push(Role::Note, format!("Approval mode · {label}"));
                }
                None => self
                    .transcript
                    .push(Role::Note, "Use ask, reads or all".into()),
            },
            ("sidebar", Some(value)) => {
                if let Some(value) = parse_on_off(value) {
                    self.preferences.sidebar = value;
                    self.save_preferences();
                }
            }
            ("thoughts", Some(value)) => {
                if let Some(value) = parse_on_off(value)
                    && value != self.preferences.show_thoughts
                {
                    self.toggle_thoughts();
                }
            }
            ("tools", Some(value)) => {
                if let Some(value) = parse_on_off(value)
                    && value != self.preferences.expand_tools
                {
                    self.toggle_tools_setting();
                }
            }
            _ => self.print_settings(),
        }
    }

    fn toggle_tools_setting(&mut self) {
        let expanded = self.transcript.toggle_tools();
        self.preferences.expand_tools = expanded;
        self.save_preferences();
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

fn on_off(value: bool) -> &'static str {
    if value { "on" } else { "off" }
}

fn parse_on_off(value: &str) -> Option<bool> {
    match value {
        "on" | "true" | "yes" => Some(true),
        "off" | "false" | "no" => Some(false),
        _ => None,
    }
}
