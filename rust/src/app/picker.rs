use std::collections::HashMap;

use agent_client_protocol::v1::schema::{
    SessionConfigKind, SessionConfigOptionCategory, SessionConfigSelectOption, SessionInfo,
};

use super::{App, Key};
use crate::{
    acp::{Action, ExecutionStatus, ModelChange, SessionActivity, SessionPage, session_activity},
    options::{self, model_provider},
    transcript::Role,
};

pub(super) enum Source {
    Sessions {
        items: Vec<SessionInfo>,
        cursor: Option<String>,
        all_workspaces: bool,
        loading: bool,
        error: Option<String>,
        refresh: Option<Vec<SessionInfo>>,
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
    pub session: Option<SessionDetails>,
}

pub(super) struct SessionDetails {
    pub workspace: String,
    pub activity: Option<SessionActivity>,
    pub updated: Option<String>,
}

impl SessionDetails {
    pub fn status(&self) -> &'static str {
        match self.activity.as_ref().map(|activity| activity.status) {
            Some(ExecutionStatus::Running) => "Running",
            Some(ExecutionStatus::Waiting) => "Needs input",
            Some(ExecutionStatus::Idle) => "Idle",
            None => "Saved",
        }
    }

    pub fn attached_elsewhere(&self) -> bool {
        self.activity
            .as_ref()
            .is_some_and(|activity| activity.attached_elsewhere)
    }
}

impl Picker {
    pub fn is_agents(&self) -> bool {
        matches!(self.source, Source::Sessions { .. })
    }

    pub fn title(&self) -> &'static str {
        match self.source {
            Source::Sessions { .. } => "Agents",
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
            Source::Sessions { loading: true, .. } if empty => "Loading sessions…",
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
            Source::Sessions { .. } => "",
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
                all_workspaces: self.managed_sessions,
                loading: false,
                error: None,
                refresh: None,
            },
            query: String::new(),
            selected: 0,
            provider_tab: 0,
            tiers: HashMap::new(),
        });
        self.request_sessions(false);
    }

    #[cfg(not(target_family = "wasm"))]
    pub fn refresh_agents(&mut self) {
        if self.managed_sessions
            && self.ready
            && let Some(Picker {
                source:
                    Source::Sessions {
                        loading: false,
                        error: None,
                        refresh,
                        ..
                    },
                ..
            }) = &mut self.picker
        {
            *refresh = Some(Vec::new());
            self.request_sessions(false);
        }
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
        let selection = self.picker.as_ref().and_then(|picker| {
            self.picker_rows(picker)
                .get(picker.selected)
                .map(|row| row.value.clone())
        });
        let Some(Picker {
            source:
                Source::Sessions {
                    items,
                    cursor,
                    loading,
                    error,
                    refresh,
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
                if let Some(snapshot) = refresh {
                    snapshot.extend(page.items);
                    if snapshot.len() < items.len() && cursor.is_some() {
                        self.request_sessions(true);
                        return;
                    }
                    *items = std::mem::take(snapshot);
                    *refresh = None;
                    self.restore_agents_selection(selection.as_deref());
                    self.maybe_page_sessions();
                    return;
                }
                for session in page.items {
                    if !items
                        .iter()
                        .any(|item| item.session_id == session.session_id)
                    {
                        items.push(session);
                    }
                }
                self.restore_agents_selection(selection.as_deref());
            }
            Err(message) => {
                *refresh = None;
                *error = Some(message);
            }
        }
        self.maybe_page_sessions();
    }

    fn restore_agents_selection(&mut self, selection: Option<&str>) {
        let Some(picker) = &self.picker else {
            return;
        };
        let rows = self.picker_rows(picker);
        let selected = rows
            .iter()
            .position(|row| Some(row.value.as_str()) == selection)
            .unwrap_or_else(|| picker.selected.min(rows.len().saturating_sub(1)));
        if let Some(picker) = &mut self.picker {
            picker.selected = selected;
        }
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
                    session: None,
                })
                .collect(),
            Source::Sessions { items, .. } => workspace_sessions(items)
                .into_iter()
                .map(|item| {
                    let value = item.session_id.to_string();
                    let active = self.session.as_deref() == Some(&value);
                    let session = SessionDetails {
                        workspace: item.cwd.display().to_string(),
                        activity: self
                            .managed_sessions
                            .then(|| session_activity(item))
                            .flatten(),
                        updated: item
                            .updated_at
                            .as_ref()
                            .map(|timestamp| timestamp.replace('T', " ")),
                    };
                    let owner = if session.attached_elsewhere() {
                        "Open in another TUI"
                    } else {
                        ""
                    };
                    let detail = format!(
                        "{} {} {} {owner}",
                        session.workspace,
                        session.status(),
                        session.updated.as_deref().unwrap_or("")
                    );
                    Row {
                        active,
                        value,
                        label: item
                            .title
                            .clone()
                            .filter(|title| !title.is_empty())
                            .unwrap_or_else(|| {
                                item.cwd.file_name().map_or_else(
                                    || "Untitled session".into(),
                                    |directory| {
                                        format!("Untitled · {}", directory.to_string_lossy())
                                    },
                                )
                            }),
                        detail,
                        context_tiers: Vec::new(),
                        selected_tier: 0,
                        session: Some(session),
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
                                    session: None,
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
            if let Source::Sessions { items, .. } = &picker.source {
                if let Some(info) = items
                    .iter()
                    .find(|info| info.session_id.0.as_ref() == row.value)
                {
                    if self.managed_sessions
                        && session_activity(info).is_some_and(|state| state.attached_elsewhere)
                    {
                        return;
                    }
                    let cwd = info.cwd.clone();
                    self.picker = None;
                    if !row.active {
                        self.load_session_at(row.value.clone(), Some(cwd));
                    }
                }
                return;
            }
            let source = self.picker.take().expect("picker is open").source;
            match source {
                Source::Setting(SessionConfigOptionCategory::Model)
                    if row.active && !self.provider_editor =>
                {
                    self.choose_model_effort()
                }
                Source::Setting(category)
                    if !row.active || category == SessionConfigOptionCategory::Model =>
                {
                    let model_change = (category == SessionConfigOptionCategory::Model).then_some(
                        ModelChange::Explicit {
                            choose_effort: true,
                        },
                    );
                    self.set_config(category, &row.value, model_change);
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
                    refresh,
                    ..
                },
            selected,
            ..
        }) = &mut self.picker
        {
            if matches!(key, Key::Tab) {
                items.clear();
                *cursor = None;
                *refresh = None;
                *all_workspaces = !*all_workspaces;
                *selected = 0;
                self.request_sessions(false);
                return;
            }
            if matches!(key, Key::Reconnect) {
                *error = None;
                *refresh = Some(Vec::new());
                self.request_sessions(false);
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

fn workspace_sessions(items: &[SessionInfo]) -> Vec<&SessionInfo> {
    // Session pages arrive newest first. A workspace takes the position of its newest session.
    let mut workspace_positions = HashMap::new();
    let mut sessions: Vec<_> = items
        .iter()
        .enumerate()
        .map(|(index, item)| (*workspace_positions.entry(&item.cwd).or_insert(index), item))
        .collect();
    sessions.sort_by_key(|&(position, _)| position);
    sessions.into_iter().map(|(_, item)| item).collect()
}
