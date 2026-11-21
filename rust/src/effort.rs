use std::collections::BTreeMap;
use std::io;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};

use agent_client_protocol::Agent;
use agent_client_protocol::v1::ConnectionTo;
use agent_client_protocol::v1::schema::{
    SessionConfigKind, SessionConfigOption, SessionConfigOptionCategory, SessionId,
    SetSessionConfigOptionRequest,
};
use serde::Deserialize;
use toml_edit::{Item, Table, TableLike, value};

use crate::acp::{Event, Events};
use crate::options;

#[derive(Clone, Debug, Deserialize)]
pub struct EffortProfile {
    path: PathBuf,
    profile: Option<String>,
    builtin: bool,
    #[serde(deserialize_with = "read_values")]
    values: Arc<Mutex<BTreeMap<String, String>>>,
}

fn read_values<'de, D: serde::Deserializer<'de>>(
    input: D,
) -> Result<Arc<Mutex<BTreeMap<String, String>>>, D::Error> {
    BTreeMap::deserialize(input).map(|values| Arc::new(Mutex::new(values)))
}

impl EffortProfile {
    pub async fn restore(
        &self,
        connection: &ConnectionTo<Agent>,
        session: &SessionId,
        config: Vec<SessionConfigOption>,
        model_changed: bool,
        events: &Events,
    ) -> Vec<SessionConfigOption> {
        let Some((model, option)) = model_and_effort(&config) else {
            return config;
        };
        let saved = self
            .values
            .lock()
            .ok()
            .and_then(|values| values.get(model).cloned());
        let desired = saved
            .as_deref()
            .filter(|value| options::value(option, value).is_some());
        let desired = desired.or((model_changed && self.builtin).then_some("default"));
        let Some(desired) = desired else {
            return config;
        };
        if current(option) == Some(desired) {
            return config;
        }
        let Some(value) = options::value(option, desired) else {
            return config;
        };
        let request = SetSessionConfigOptionRequest::new(session.clone(), option.id.clone(), value);
        match connection.send_request(request).block_task().await {
            Ok(reply) => reply.config_options,
            Err(error) => {
                let _ = events.unbounded_send(Event::Note(format!(
                    "Could not restore saved reasoning effort: {}",
                    error.message
                )));
                config
            }
        }
    }

    pub async fn remember(&self, config: &[SessionConfigOption], events: &Events) {
        let Some((model, option)) = model_and_effort(config) else {
            return;
        };
        let Some(effort) = current(option) else {
            return;
        };
        let path = self.path.clone();
        let profile = self.profile.clone();
        let model = model.to_owned();
        let effort = effort.to_owned();
        let saved = tokio::task::spawn_blocking(move || {
            save_effort(&path, profile.as_deref(), &model, &effort)?;
            Ok::<_, io::Error>((model, effort))
        })
        .await;
        match saved {
            Ok(Ok((model, effort))) => {
                if let Ok(mut values) = self.values.lock() {
                    values.insert(model, effort);
                }
            }
            _ => {
                let _ = events.unbounded_send(Event::Note("Reasoning effort was applied, but it could not be saved to the profile configuration".into()));
            }
        }
    }
}

fn save_effort(path: &Path, profile: Option<&str>, model: &str, effort: &str) -> io::Result<()> {
    crate::config_file::edit(path, |document| {
        let values = if let Some(profile) = profile {
            let saved = document
                .get_mut("profile")
                .and_then(Item::as_table_like_mut)
                .and_then(|profiles| profiles.get_mut(profile))
                .and_then(Item::as_table_like_mut)
                .ok_or_else(|| io::Error::other("The saved profile no longer exists"))?;
            effort_table(saved, "model_effort")?
        } else {
            effort_table(document.as_table_mut(), "local_model_effort")?
        };
        values.insert(model, value(effort));
        Ok(())
    })
}

fn effort_table<'a>(parent: &'a mut dyn TableLike, key: &str) -> io::Result<&'a mut dyn TableLike> {
    if !parent.contains_key(key) {
        parent.insert(key, Item::Table(Table::new()));
    }
    parent
        .get_mut(key)
        .and_then(Item::as_table_like_mut)
        .ok_or_else(|| io::Error::other("Expected an effort table"))
}

fn model_and_effort(config: &[SessionConfigOption]) -> Option<(&str, &SessionConfigOption)> {
    let model = config
        .iter()
        .find(|option| options::matches_category(option, &SessionConfigOptionCategory::Model))
        .and_then(current)?;
    let effort = config.iter().find(|option| {
        options::matches_category(option, &SessionConfigOptionCategory::ThoughtLevel)
    })?;
    Some((model, effort))
}

fn current(option: &SessionConfigOption) -> Option<&str> {
    match &option.kind {
        SessionConfigKind::Select(select) => Some(select.current_value.0.as_ref()),
        SessionConfigKind::Boolean(toggle) => Some(if toggle.current_value { "on" } else { "off" }),
        _ => None,
    }
}
