use agent_client_protocol::v1::schema::SessionConfigOptionCategory;

use super::{App, LocalEffect};
use crate::acp::{Action, ConnectTarget};
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
                    "Sessions  /sessions · /new · /load <id> · /close · /delete <id>".to_owned(),
                    "Connection  /connect · /reconnect · /providers · /mcp".to_owned(),
                    "Model  /models · /effort · /provider".to_owned(),
                    "Transcript  /thoughts · /find <text> · /copy · /todo".to_owned(),
                    "Composer  /attach · /detach · /queue · /resume-queue · /clear-queue"
                        .to_owned(),
                    "Settings  /settings · /permissions · /theme · /sidebar".to_owned(),
                    "Agent  /skills · /agent <command>".to_owned(),
                    String::new(),
                    "Tab  Complete a command or argument".to_owned(),
                    "Ctrl+O  Expand or collapse tool details".to_owned(),
                    "PgUp / PgDn  Scroll the transcript".to_owned(),
                    "Esc  Close a picker or cancel the turn".to_owned(),
                ];
                commands.extend(
                    self.commands
                        .iter()
                        .map(|(name, description)| format!("/agent {name}  {description}")),
                );
                self.transcript
                    .push(Role::Command("/help".into()), commands.join("\n"));
            }
            "/sessions" | "/load" => {
                self.draft.submit();
                self.show_sessions();
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
                self.transcript.push(Role::Command("/skills".into()), text);
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
                self.transcript.push(Role::Command("/mcp".into()), text);
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
            "/providers" | "/provider" => {
                self.draft.submit();
                self.show_providers();
            }
            "/provider add" | "/provider edit" | "/provider remove" | "/provider set"
            | "/provider disable" => {
                self.draft.submit();
                self.provider_usage();
            }
            text if text.starts_with("/provider add ") || text.starts_with("/provider edit ") => {
                self.draft.set_text("");
                let mut words = text.split_whitespace().skip(1);
                let (Some(operation), Some(id), Some(api_type), Some(base_url)) =
                    (words.next(), words.next(), words.next(), words.next())
                else {
                    self.provider_usage();
                    return;
                };
                let models = words.next().map(|value| {
                    if value == "-" {
                        Vec::new()
                    } else {
                        value.split(',').map(str::to_owned).collect()
                    }
                });
                let api_key_env = words
                    .next()
                    .map(|value| if value == "-" { "" } else { value }.to_owned());
                if words.next().is_some() {
                    self.provider_usage();
                    return;
                }
                self.edit_local_provider(Action::EditProvider {
                    id: id.to_owned(),
                    create: operation == "add",
                    api_type: api_type.to_owned(),
                    base_url: if base_url == "-" {
                        String::new()
                    } else {
                        base_url.to_owned()
                    },
                    models,
                    api_key_env,
                });
            }
            text if text.starts_with("/provider remove ") => {
                self.draft.submit();
                let id = text["/provider remove ".len()..].trim();
                self.edit_local_provider(Action::RemoveProvider(id.to_owned()));
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
            "/model" | "/models" => {
                self.draft.submit();
                self.show_setting(SessionConfigOptionCategory::Model);
            }
            "/effort" => {
                self.draft.submit();
                self.show_setting(SessionConfigOptionCategory::ThoughtLevel);
            }
            text if text.starts_with("/model ") || text.starts_with("/models ") => {
                self.draft.submit();
                self.set_config(
                    SessionConfigOptionCategory::Model,
                    text.split_once(' ').map_or("", |(_, value)| value.trim()),
                    false,
                );
            }
            text if text.starts_with("/effort ") => {
                self.draft.submit();
                self.set_config(
                    SessionConfigOptionCategory::ThoughtLevel,
                    text["/effort ".len()..].trim(),
                    false,
                );
            }
            "/connect" => {
                self.draft.submit();
                self.transcript.push(
                    Role::Command("/connect".into()),
                    "Remote   /connect wss://host/acp/agent [workspace]\nNative   /connect local [workspace]".into(),
                );
            }
            text if text.starts_with("/connect ") => {
                self.draft.set_text("");
                if !self.can_switch() {
                    self.transcript.push(
                        Role::Error,
                        "Stop the running turn before changing the connection".into(),
                    );
                    return;
                }
                let (endpoint, workspace) = text["/connect ".len()..]
                    .trim()
                    .split_once(char::is_whitespace)
                    .unwrap_or((text["/connect ".len()..].trim(), ""));
                let cwd = (!workspace.trim().is_empty()).then(|| workspace.trim().into());
                self.local_effect = Some(LocalEffect::Connect(ConnectTarget {
                    endpoint: endpoint.into(),
                    cwd,
                }));
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

    pub(super) fn provider_usage(&mut self) {
        let text = if self.provider_editor {
            "/provider add <id> <type> <url|-> [model,model] [key-env]\n/provider edit <id> <type> <url|-> [model,model] [key-env]\n/provider remove <id>\nUse - for a provider default URL or to clear an optional field."
        } else {
            "/provider set <id> <type> [url]\n/provider disable <id>"
        };
        self.transcript
            .push(Role::Command("/providers".into()), text.into());
    }

    fn edit_local_provider(&mut self, action: Action) {
        if !self.ready || !self.provider_editor {
            self.transcript.push(
                Role::Error,
                "Provider configuration requires the built-in stdio agent".into(),
            );
        } else if !self.can_switch() {
            self.transcript.push(
                Role::Error,
                "Stop the running turn before changing providers".into(),
            );
        } else {
            let _ = self.actions.unbounded_send(action);
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
        self.transcript
            .push(Role::Command("/settings".into()), text);
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

    pub(super) fn set_config(
        &mut self,
        category: SessionConfigOptionCategory,
        input: &str,
        choose_effort: bool,
    ) {
        let selected = self
            .options
            .iter()
            .find(|option| options::matches_category(option, &category));
        let notice = match selected {
            Some(option) if input.is_empty() => options::summary(option),
            Some(option) => match options::value(option, input) {
                Some(value) => {
                    let _ = self.actions.unbounded_send(Action::SetOption(
                        option.id.clone(),
                        value,
                        choose_effort,
                    ));
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
