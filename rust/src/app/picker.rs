use std::collections::HashMap;

use agent_client_protocol::v1::schema::{
    SessionConfigKind, SessionConfigOptionCategory, SessionConfigSelectOption, SessionInfo,
};

use super::{App, Key};
use crate::{
    acp::{Action, SessionPage},
    options,
    transcript::Role,
};

pub(super) enum Source {
    Sessions {
        items: Vec<SessionInfo>,
        cursor: Option<String>,
        all_workspaces: bool,
        loading: bool,
        error: Option<String>,
    },
    Setting(SessionConfigOptionCategory),
    Skills,
}

pub(super) struct Picker {
    pub source: Source,
    pub query: String,
    pub selected: usize,
    pub provider_tab: usize,
    tiers: HashMap<String, usize>,
}

pub(super) struct ContextTier {
    pub value: String,
    pub label: String,
    current: bool,
}

pub(super) struct Row {
    pub value: String,
    pub label: String,
    pub detail: String,
    pub active: bool,
    pub context_tiers: Vec<ContextTier>,
    pub selected_tier: usize,
}

impl Picker {
    pub fn title(&self) -> &'static str {
        match self.source {
            Source::Sessions { .. } => "Resume a session",
            Source::Setting(SessionConfigOptionCategory::Model) => "Select a model",
            Source::Setting(_) => "Select reasoning effort",
            Source::Skills => "Skills",
        }
    }

    pub fn status(&self, empty: bool) -> &str {
        match &self.source {
            Source::Sessions {
                error: Some(error), ..
            } => error,
            Source::Sessions { loading: true, .. } => "Loading sessions…",
            Source::Sessions { .. } if empty && self.query.is_empty() => "No saved sessions",
            Source::Skills if empty && self.query.is_empty() => "No skills available",
            Source::Setting(SessionConfigOptionCategory::Model)
                if empty && self.query.is_empty() =>
            {
                "No models available for this provider"
            }
            _ if empty => "No matches",
            _ => "",
        }
    }

    pub fn hint(&self, provider_tabs: bool, context_tiers: bool) -> &'static str {
        match self.source {
            Source::Sessions { error: Some(_), .. } => {
                "  Ctrl+R retry   Tab switch workspace scope   Esc close"
            }
            Source::Sessions {
                all_workspaces: true,
                ..
            } => "  ↑/↓ select   Enter resume   Tab current workspace   Esc close",
            Source::Sessions { .. } => {
                "  ↑/↓ select   Enter resume   Tab all workspaces   Esc close"
            }
            Source::Setting(_) if context_tiers && provider_tabs => {
                "  ↑/↓ model   ←/→ context   Tab provider   Enter confirm"
            }
            Source::Setting(_) if context_tiers => {
                "  ↑/↓ model   ←/→ context   Enter confirm   Esc close"
            }
            Source::Setting(_) if provider_tabs => {
                "  ↑/↓ select   Tab / Shift+Tab provider   Enter confirm"
            }
            Source::Setting(_) => "  ↑/↓ select   Enter confirm   Esc close",
            Source::Skills => "  ↑/↓ move   Enter insert   Esc close",
        }
    }

    fn matches(&self, row: &Row) -> bool {
        let query = self.query.to_lowercase();
        [&row.label, &row.value, &row.detail]
            .iter()
            .any(|text| text.to_lowercase().contains(&query))
            || row
                .context_tiers
                .iter()
                .any(|tier| tier.label.to_lowercase().contains(&query))
    }
}

fn model_provider(choice: &SessionConfigSelectOption) -> Option<&str> {
    choice.meta.as_ref()?.get("providerId")?.as_str()
}

pub(super) fn context_tier(choice: &SessionConfigSelectOption) -> Option<(&str, &str)> {
    let meta = choice.meta.as_ref()?;
    Some((
        meta.get("hrns/model/context_group")?.as_str()?,
        meta.get("hrns/model/context_size")?.as_str()?,
    ))
}

fn group_context_tiers(rows: Vec<Row>, selections: &HashMap<String, usize>) -> Vec<Row> {
    let mut grouped: Vec<Row> = Vec::new();
    let mut positions = HashMap::new();
    for mut row in rows {
        if !row.context_tiers.is_empty() {
            let position = positions.entry(row.label.clone()).or_insert(grouped.len());
            if let Some(existing) = grouped.get_mut(*position) {
                existing.context_tiers.append(&mut row.context_tiers);
                continue;
            }
        }
        grouped.push(row);
    }
    for row in &mut grouped {
        if row.context_tiers.is_empty() {
            continue;
        }
        row.context_tiers
            .sort_by_key(|tier| tier.value != row.label);
        row.selected_tier = selections
            .get(&row.label)
            .copied()
            .filter(|&index| index < row.context_tiers.len())
            .or_else(|| row.context_tiers.iter().position(|tier| tier.current))
            .unwrap_or(0);
        let tier = &row.context_tiers[row.selected_tier];
        row.value.clone_from(&tier.value);
        row.active = tier.current;
    }
    grouped
}

impl App {
    pub(super) fn show_skills(&mut self) {
        self.picker = Some(Picker {
            source: Source::Skills,
            query: String::new(),
            selected: 0,
            provider_tab: 0,
            tiers: HashMap::new(),
        });
    }

    pub(super) fn show_sessions(&mut self) {
        self.picker = Some(Picker {
            source: Source::Sessions {
                items: Vec::new(),
                cursor: None,
                all_workspaces: false,
                loading: false,
                error: None,
            },
            query: String::new(),
            selected: 0,
            provider_tab: 0,
            tiers: HashMap::new(),
        });
        self.request_sessions(false);
    }

    pub(super) fn load_waiting_sessions(&mut self) {
        if matches!(
            self.picker.as_ref().map(|picker| &picker.source),
            Some(Source::Sessions { loading: true, .. })
        ) {
            self.request_sessions(false);
        }
    }

    fn request_sessions(&mut self, next: bool) {
        let Some(Picker {
            source:
                Source::Sessions {
                    loading,
                    error,
                    cursor,
                    all_workspaces,
                    ..
                },
            ..
        }) = &mut self.picker
        else {
            return;
        };
        self.session_request = self.session_request.wrapping_add(1);
        if !self.ready && matches!(self.status, "Connecting" | "Reconnecting") {
            *loading = true;
            *error = None;
            return;
        }
        *loading = self.ready
            && self
                .actions
                .unbounded_send(Action::ListSessions {
                    request_id: self.session_request,
                    cursor: if next { cursor.clone() } else { None },
                    cwd: if *all_workspaces {
                        None
                    } else {
                        self.workspace.as_ref().map(Into::into)
                    },
                })
                .is_ok();
        *error = (!*loading).then(|| "Connect to an agent to list sessions".into());
    }

    pub(super) fn sessions_received(
        &mut self,
        request_id: u64,
        result: Result<SessionPage, String>,
    ) {
        if request_id != self.session_request {
            return;
        }
        let Some(Picker {
            source:
                Source::Sessions {
                    items,
                    cursor,
                    loading,
                    error,
                    ..
                },
            ..
        }) = &mut self.picker
        else {
            return;
        };
        *loading = false;
        match result {
            Ok(page) => {
                *cursor = page.cursor.filter(|next| cursor.as_ref() != Some(next));
                for session in page.items {
                    if !items
                        .iter()
                        .any(|item| item.session_id == session.session_id)
                    {
                        items.push(session);
                    }
                }
            }
            Err(message) => *error = Some(message),
        }
        self.maybe_page_sessions();
    }

    pub(super) fn maybe_page_sessions(&mut self) {
        let Some(picker) = self.picker.as_ref() else {
            return;
        };
        if let Source::Sessions {
            cursor: Some(_),
            loading: false,
            error: None,
            ..
        } = &picker.source
            && (self.picker_rows(picker).len() <= picker.selected + 3)
        {
            self.request_sessions(true);
        }
    }

    pub(super) fn model_provider_tabs(&self, picker: &Picker) -> Vec<Option<&str>> {
        if !matches!(
            picker.source,
            Source::Setting(SessionConfigOptionCategory::Model)
        ) {
            return Vec::new();
        }
        let Some(option) = self
            .options
            .iter()
            .find(|option| options::matches_category(option, &SessionConfigOptionCategory::Model))
        else {
            return Vec::new();
        };
        let mut providers: Vec<_> = option
            .meta
            .as_ref()
            .and_then(|meta| meta.get("hrns/model/providers"))
            .and_then(serde_json::Value::as_array)
            .into_iter()
            .flatten()
            .filter_map(serde_json::Value::as_str)
            .map(Some)
            .collect();
        for choice in options::choices(option) {
            let provider = model_provider(choice);
            if !providers.contains(&provider) {
                providers.push(provider);
            }
        }
        providers
    }

    pub(super) fn picker_rows(&self, picker: &Picker) -> Vec<Row> {
        let tabs = self.model_provider_tabs(picker);
        let provider = tabs.get(picker.provider_tab.min(tabs.len().saturating_sub(1)));
        let mut rows: Vec<Row> = match &picker.source {
            Source::Skills => self
                .skills
                .iter()
                .filter(|skill| skill.user_invocable)
                .map(|skill| Row {
                    value: skill.id.clone(),
                    label: skill.label().to_owned(),
                    detail: skill.description.clone(),
                    active: false,
                    context_tiers: Vec::new(),
                    selected_tier: 0,
                })
                .collect(),
            Source::Sessions { items, .. } => items
                .iter()
                .map(|item| {
                    let value = item.session_id.to_string();
                    Row {
                        active: self.session.as_deref() == Some(&value),
                        value,
                        label: item
                            .title
                            .clone()
                            .filter(|title| !title.is_empty())
                            .unwrap_or_else(|| "Untitled session".into()),
                        detail: format!(
                            "{}  {}",
                            item.updated_at.as_deref().unwrap_or("").replace('T', " "),
                            item.cwd.display()
                        ),
                        context_tiers: Vec::new(),
                        selected_tier: 0,
                    }
                })
                .collect(),
            Source::Setting(category) => self
                .options
                .iter()
                .find(|option| options::matches_category(option, category))
                .and_then(|option| match &option.kind {
                    SessionConfigKind::Select(select) => Some(
                        options::choices(option)
                            .into_iter()
                            .filter(|choice| {
                                provider.is_none_or(|tab| *tab == model_provider(choice))
                            })
                            .map(|choice| {
                                let value = choice.value.to_string();
                                let tier = context_tier(choice);
                                let active = choice.value == select.current_value;
                                let provider = model_provider(choice);
                                let detail = choice
                                    .description
                                    .clone()
                                    .or_else(|| {
                                        provider
                                            .filter(|provider| {
                                                !choice.name.starts_with(&format!("{provider}:"))
                                            })
                                            .map(|provider| format!("{provider} · {value}"))
                                    })
                                    .unwrap_or_else(|| {
                                        if tier.is_some() || choice.name == value {
                                            String::new()
                                        } else {
                                            value.clone()
                                        }
                                    });
                                Row {
                                    context_tiers: tier.map_or_else(Vec::new, |(_, label)| {
                                        vec![ContextTier {
                                            value: value.clone(),
                                            label: label.into(),
                                            current: active,
                                        }]
                                    }),
                                    value,
                                    label: tier.map_or_else(
                                        || choice.name.clone(),
                                        |(family, _)| family.into(),
                                    ),
                                    detail,
                                    active,
                                    selected_tier: 0,
                                }
                            })
                            .collect(),
                    ),
                    _ => None,
                })
                .unwrap_or_default(),
        };
        if rows.iter().any(|row| !row.context_tiers.is_empty()) {
            rows = group_context_tiers(rows, &picker.tiers);
        }
        if picker.query.is_empty() {
            return rows;
        }
        rows.into_iter().filter(|row| picker.matches(row)).collect()
    }

    pub(super) fn show_setting(&mut self, category: SessionConfigOptionCategory) {
        if !self.open_setting(category) {
            self.transcript.push(
                Role::Note,
                "The agent advertised no choices for this setting".into(),
            );
        }
    }

    pub(super) fn choose_model_effort(&mut self) {
        self.open_setting(SessionConfigOptionCategory::ThoughtLevel);
    }

    fn open_setting(&mut self, category: SessionConfigOptionCategory) -> bool {
        let mut picker = Picker {
            source: Source::Setting(category),
            query: String::new(),
            selected: 0,
            provider_tab: 0,
            tiers: HashMap::new(),
        };
        let tabs = self.model_provider_tabs(&picker);
        let preferred = self.selected_provider.as_ref().and_then(|selected| {
            tabs.iter()
                .position(|name| *name == Some(selected.id.as_str()))
        });
        let active = self
            .options
            .iter()
            .find(|option| options::matches_category(option, &SessionConfigOptionCategory::Model))
            .and_then(|option| {
                let SessionConfigKind::Select(select) = &option.kind else {
                    return None;
                };
                let choice = options::choices(option)
                    .into_iter()
                    .find(|choice| choice.value == select.current_value)?;
                tabs.iter().position(|name| *name == model_provider(choice))
            });
        picker.provider_tab = preferred.or(active).unwrap_or_default();
        let rows = self.picker_rows(&picker);
        if rows.is_empty() && tabs.is_empty() {
            return false;
        }
        picker.selected = rows.iter().position(|row| row.active).unwrap_or(0);
        self.picker = Some(picker);
        true
    }

    pub(super) fn picker_key(&mut self, key: Key) {
        let Some(picker) = &self.picker else { return };
        if matches!(key, Key::Tab | Key::BackTab) {
            let count = self.model_provider_tabs(picker).len();
            if count > 1 {
                let current = picker.provider_tab.min(count - 1);
                let offset = if matches!(key, Key::Tab) {
                    1
                } else {
                    count - 1
                };
                if let Some(picker) = &mut self.picker {
                    picker.provider_tab = (current + offset) % count;
                    picker.selected = 0;
                }
                return;
            }
        }
        let rows = self.picker_rows(picker);
        let last = rows.len().saturating_sub(1);
        if matches!(key, Key::Left | Key::Right) {
            if let Some(row) = rows.get(picker.selected)
                && row.context_tiers.len() > 1
            {
                let index = if matches!(key, Key::Left) {
                    row.selected_tier.saturating_sub(1)
                } else {
                    (row.selected_tier + 1).min(row.context_tiers.len() - 1)
                };
                if let Some(picker) = &mut self.picker {
                    picker.tiers.insert(row.label.clone(), index);
                }
            }
            return;
        }
        if matches!(key, Key::Enter) {
            let Some(row) = rows.get(picker.selected) else {
                return;
            };
            let source = self.picker.take().expect("picker is open").source;
            match source {
                Source::Sessions { .. } if !row.active => self.load_session(row.value.clone()),
                Source::Setting(SessionConfigOptionCategory::Model) if row.active => {
                    self.choose_model_effort()
                }
                Source::Setting(category) if !row.active => {
                    let choose_effort = category == SessionConfigOptionCategory::Model;
                    self.set_config(category, &row.value, choose_effort);
                }
                Source::Skills => self.draft.insert(&format!("${} ", row.value)),
                _ => {}
            }
            return;
        }
        if matches!(key, Key::Escape) {
            self.picker = None;
            return;
        }
        if let Some(Picker {
            source:
                Source::Sessions {
                    items,
                    cursor,
                    all_workspaces,
                    error,
                    ..
                },
            selected,
            ..
        }) = &mut self.picker
        {
            if matches!(key, Key::Tab) {
                items.clear();
                *cursor = None;
                *all_workspaces = !*all_workspaces;
                *selected = 0;
                self.request_sessions(false);
                return;
            }
            if matches!(key, Key::Reconnect) && error.is_some() {
                let more = cursor.is_some();
                self.request_sessions(more);
                return;
            }
        }
        let Some(picker) = &mut self.picker else {
            return;
        };
        match key {
            Key::HistoryUp => picker.selected = picker.selected.saturating_sub(1),
            Key::HistoryDown => picker.selected = (picker.selected + 1).min(last),
            Key::ScrollUp => picker.selected = picker.selected.saturating_sub(7),
            Key::ScrollDown => picker.selected = (picker.selected + 7).min(last),
            Key::Home => picker.selected = 0,
            Key::End => picker.selected = last,
            Key::Text(ch) if !ch.is_control() => {
                picker.query.push(ch);
                picker.selected = 0;
            }
            Key::Backspace => {
                picker.query.pop();
                picker.selected = 0;
            }
            _ => return,
        }
        self.maybe_page_sessions();
    }
}
