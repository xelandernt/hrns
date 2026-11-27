use crate::skills::Skill;
use crate::theme;

use super::{App, COMMANDS, Completion, DETAILS_CHOICES, Suggestion};

impl App {
    pub(super) fn refresh_completion(&mut self) {
        #[cfg(not(target_family = "wasm"))]
        if let Some(completion) = self.file_completion() {
            self.completion = Some(completion);
            return;
        }

        let input = self.draft.text();
        self.completion = self
            .argument_suggestions(input)
            .or_else(|| suggestions(input, &self.skills))
            .map(|items| Completion {
                items,
                selected: 0,
                file: None,
            });
    }

    pub(super) fn is_command(&self, command: &str) -> bool {
        is_local_command(command)
            || self
                .commands
                .iter()
                .any(|(name, _)| command.strip_prefix('/') == Some(name.as_str()))
    }

    pub(super) fn has_unknown_command(&self, input: &str) -> bool {
        let Some(command) = slash_command(input) else {
            return false;
        };
        !self.is_command(command)
            || command == "/queue"
                && slash_command(input[command.len()..].trim_start())
                    .is_some_and(|queued| !self.is_command(queued))
    }

    pub(super) fn argument_suggestions(&self, input: &str) -> Option<Vec<Suggestion>> {
        use agent_client_protocol::v1::schema::SessionConfigOptionCategory;

        let (command, prefix) = input.split_once(' ')?;
        let prefix = prefix.to_ascii_lowercase();
        let choices: Vec<(String, String)> = match command {
            "/theme" => theme::NAMES
                .iter()
                .map(|name| (name.to_string(), "Terminal theme".into()))
                .collect(),
            "/permissions" => [
                ("ask", "Ask before approving tools"),
                ("reads", "Approve read-only tools"),
                ("all", "Approve all tools"),
            ]
            .into_iter()
            .map(|(value, description)| (value.into(), description.into()))
            .collect(),
            "/details" => DETAILS_CHOICES
                .into_iter()
                .map(|(value, label)| (value.into(), label.into()))
                .collect(),
            "/provider" if self.provider_editor => [
                ("add", "Add a provider"),
                ("edit", "Edit a provider"),
                ("remove", "Remove a provider"),
            ]
            .into_iter()
            .map(|(value, description)| (value.into(), description.into()))
            .collect(),
            "/provider" if self.provider_routing => [
                ("set", "Set a provider route"),
                ("disable", "Disable a provider route"),
            ]
            .into_iter()
            .map(|(value, description)| (value.into(), description.into()))
            .collect(),
            "/effort" | "/model" | "/models" => {
                let category = if command == "/effort" {
                    SessionConfigOptionCategory::ThoughtLevel
                } else {
                    SessionConfigOptionCategory::Model
                };
                let option = self
                    .options
                    .iter()
                    .find(|option| crate::options::matches_category(option, &category))?;
                crate::options::choices(option)
                    .into_iter()
                    .map(|choice| {
                        (
                            choice.value.to_string(),
                            choice
                                .description
                                .clone()
                                .unwrap_or_else(|| choice.name.clone()),
                        )
                    })
                    .collect()
            }
            _ => return None,
        };
        let items: Vec<_> = choices
            .into_iter()
            .filter(|(value, _)| {
                !value.eq_ignore_ascii_case(&prefix)
                    && value.to_ascii_lowercase().starts_with(&prefix)
            })
            .map(|(value, description)| {
                let suffix = if command == "/provider" { " " } else { "" };
                Suggestion {
                    insert: format!("{command} {value}{suffix}"),
                    description,
                }
            })
            .collect();
        (!items.is_empty()).then_some(items)
    }

    pub(super) fn accept_completion(&mut self) {
        let Some(completion) = self.completion.take() else {
            return;
        };
        let Some(suggestion) = completion.items.get(completion.selected) else {
            self.completion = Some(completion);
            return;
        };
        #[cfg(not(target_family = "wasm"))]
        if let Some(file) = completion.file {
            self.insert_file_reference(file.range, &suggestion.insert);
            return;
        }
        let text = self.draft.text();
        if let Some(start) = last_token_start(text).filter(|start| text[*start..].starts_with('$'))
        {
            self.draft.replace_from(start, &suggestion.insert);
        } else {
            self.draft.set_text(&suggestion.insert);
        }
    }

    pub(super) fn select_completion(&mut self, forward: bool) {
        let Some(completion) = &mut self.completion else {
            return;
        };
        let len = completion.items.len();
        if len == 0 {
            return;
        }
        completion.selected = if forward {
            (completion.selected + 1) % len
        } else {
            (completion.selected + len - 1) % len
        };
    }
}

pub(super) fn is_local_command(command: &str) -> bool {
    command == "/todos" || COMMANDS.iter().any(|(name, _)| *name == command)
}

pub(super) fn slash_command(text: &str) -> Option<&str> {
    text.starts_with('/')
        .then(|| &text[..text.find(char::is_whitespace).unwrap_or(text.len())])
}

pub(super) fn suggestions(text: &str, skills: &[Skill]) -> Option<Vec<Suggestion>> {
    if text.contains('\n') || text.trim().is_empty() {
        return None;
    }
    let start = last_token_start(text)?;
    let token = &text[start..];
    if let Some(prefix) = token.strip_prefix('$') {
        let items: Vec<Suggestion> = skills
            .iter()
            .filter(|skill| {
                skill.user_invocable && skill.id.starts_with(prefix) && skill.id != prefix
            })
            .map(|skill| Suggestion {
                insert: format!("${} ", skill.id),
                description: skill.description.clone(),
            })
            .collect();
        return (!items.is_empty()).then_some(items);
    }
    if start == 0 && token.starts_with('/') {
        let items: Vec<Suggestion> = COMMANDS
            .iter()
            .filter(|(command, _)| command.starts_with(token) && *command != token)
            .map(|(command, description)| Suggestion {
                insert: format!("{command} "),
                description: (*description).to_owned(),
            })
            .collect();
        return (!items.is_empty()).then_some(items);
    }
    None
}

pub(super) fn last_token_start(text: &str) -> Option<usize> {
    let mut start = 0;
    for (index, character) in text.char_indices() {
        if character.is_whitespace() {
            start = index + character.len_utf8();
        }
    }
    (start < text.len()).then_some(start)
}
