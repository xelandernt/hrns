use agent_client_protocol::v1::schema::{ProviderInfo as Route, SessionConfigOptionCategory};
use ratatui::Frame;
use ratatui::layout::{Constraint, Layout, Rect};
use ratatui::style::{Style, Stylize};
use ratatui::text::Line;
use ratatui::widgets::{Block, BorderType, Clear, Paragraph, Wrap};

use super::{App, Key, LocalEffect};
use crate::acp::{
    Action, LoginMethod, ModelChange, ProviderEdit, ProviderInfo, ProviderLoginState,
};
use crate::composer::Composer;
use crate::options;
use crate::theme::Palette;
use crate::transcript::Role;

const TYPES: [&str; 5] = [
    "openai-compatible",
    "deepseek",
    "github-copilot",
    "openai-codex",
    "bedrock",
];
const TYPE_FIELD: usize = 1;

pub(super) struct Selected {
    pub(super) id: String,
    methods: Vec<LoginMethod>,
    setup_message: Option<String>,
    refresh_model_after_login: bool,
}

impl From<&ProviderInfo> for Selected {
    fn from(provider: &ProviderInfo) -> Self {
        Self {
            id: provider.id.clone(),
            methods: provider.login_methods.clone(),
            setup_message: provider.setup_message.clone(),
            refresh_model_after_login: false,
        }
    }
}

pub(super) struct Dialog {
    screen: Screen,
    error: Option<String>,
    saving: bool,
}

enum Screen {
    Loading,
    LoadingLogin(String),
    List {
        items: Vec<ProviderInfo>,
        index: usize,
    },
    Routes {
        items: Vec<Route>,
        index: usize,
    },
    Edit(Box<ProviderForm>),
    Remove(String),
    Methods {
        index: usize,
    },
    ApiKey(Composer),
    SigningIn {
        url: Option<String>,
        code: Option<String>,
    },
    Cancelling,
}

struct ProviderForm {
    create: bool,
    fields: [Composer; 6],
    focused: usize,
}

impl ProviderForm {
    fn new(provider: Option<&ProviderInfo>) -> Self {
        let mut fields: [Composer; 6] = Default::default();
        fields[TYPE_FIELD].set_text(TYPES[0]);
        if let Some(provider) = provider {
            for (field, value) in fields.iter_mut().zip([
                provider.id.as_str(),
                provider.api_type.as_str(),
                provider.base_url.as_str(),
                &provider.models.join(", "),
                provider.aws_profile.as_deref().unwrap_or_default(),
                provider.aws_region.as_deref().unwrap_or_default(),
            ]) {
                field.set_text(value);
            }
        }
        Self {
            create: provider.is_none(),
            fields,
            focused: usize::from(provider.is_some()),
        }
    }

    fn visible(&self) -> &'static [usize] {
        match self.fields[TYPE_FIELD].text() {
            "bedrock" => &[0, 1, 2, 3, 4, 5],
            "github-copilot" | "openai-codex" | "deepseek" => &[0, 1, 3],
            _ => &[0, 1, 2, 3],
        }
    }

    fn key(&mut self, key: Key) {
        match key {
            Key::Tab | Key::BackTab => {
                let visible = self.visible();
                let start = usize::from(!self.create);
                let position = visible
                    .iter()
                    .position(|&field| field == self.focused)
                    .unwrap_or(start);
                let next = if matches!(key, Key::Tab) {
                    if position + 1 == visible.len() {
                        start
                    } else {
                        position + 1
                    }
                } else if position == start {
                    visible.len() - 1
                } else {
                    position - 1
                };
                self.focused = visible[next];
            }
            Key::Left | Key::Right if self.focused == TYPE_FIELD => {
                let field = &mut self.fields[TYPE_FIELD];
                let current = TYPES.iter().position(|&kind| kind == field.text());
                let next = if matches!(key, Key::Left) {
                    current.map_or(TYPES.len() - 1, |i| (i + TYPES.len() - 1) % TYPES.len())
                } else {
                    current.map_or(0, |i| (i + 1) % TYPES.len())
                };
                field.set_text(TYPES[next]);
            }
            _ => {
                if let Some(input) = self.focused_input() {
                    input_key(input, key);
                }
            }
        }
    }

    fn focused_input(&mut self) -> Option<&mut Composer> {
        (self.focused != TYPE_FIELD).then(|| &mut self.fields[self.focused])
    }

    fn edit(&self) -> ProviderEdit {
        let [id, api_type, base_url, models, profile, region] =
            self.fields.each_ref().map(|field| field.text().trim());
        ProviderEdit {
            id: id.into(),
            create: self.create,
            api_type: api_type.into(),
            base_url: if self.visible().contains(&2) {
                base_url.into()
            } else {
                String::new()
            },
            models: Some(model_ids(models)),
            aws_profile: (api_type == "bedrock").then(|| profile.into()),
            aws_region: (api_type == "bedrock").then(|| region.into()),
        }
    }
}

pub(super) fn model_ids(value: &str) -> Vec<String> {
    value
        .split(',')
        .map(str::trim)
        .filter(|model| !model.is_empty() && *model != "-")
        .map(str::to_owned)
        .collect()
}

impl Dialog {
    fn new(screen: Screen) -> Self {
        Self {
            screen,
            error: None,
            saving: false,
        }
    }

    pub(super) fn paste(&mut self, text: &str) {
        if !self.saving {
            match &mut self.screen {
                Screen::Edit(form) => {
                    if let Some(input) = form.focused_input() {
                        insert(input, text);
                    }
                }
                Screen::ApiKey(input) => insert(input, text),
                _ => {}
            }
        }
    }
}

fn insert(input: &mut Composer, text: &str) {
    let clean: String = text.chars().filter(|ch| !ch.is_control()).collect();
    input.insert(&clean);
}

fn input_key(input: &mut Composer, key: Key) {
    match key {
        Key::Text(ch) => input.insert(ch.encode_utf8(&mut [0; 4])),
        Key::Backspace => input.backspace(),
        Key::Delete => input.delete(),
        Key::Left => input.left(),
        Key::Right => input.right(),
        Key::Home => input.home(),
        Key::End => input.end(),
        _ => {}
    }
}

impl App {
    pub(super) fn provider_setup_message(&self) -> Option<&str> {
        self.selected_provider.as_ref()?.setup_message.as_deref()
    }

    pub(super) fn take_provider_login_refresh(&mut self) -> bool {
        self.selected_provider
            .as_mut()
            .is_some_and(|selected| std::mem::take(&mut selected.refresh_model_after_login))
    }

    pub(super) fn activate_provider_model(&mut self) {
        let Some(selected) = &self.selected_provider else {
            return;
        };
        if self.model_change_pending {
            return;
        }
        let category = SessionConfigOptionCategory::Model;
        let Some(option) = self
            .options
            .iter()
            .find(|option| options::matches_category(option, &category))
        else {
            return;
        };
        let Some(preferred) = options::choices(option).into_iter().find(|choice| {
            options::model_provider(choice) == Some(selected.id.as_str())
                && choice
                    .meta
                    .as_ref()
                    .and_then(|meta| meta.get("hrns/model/preferred"))
                    .and_then(serde_json::Value::as_bool)
                    == Some(true)
        }) else {
            return;
        };
        if options::selected_model(&self.options)
            .is_some_and(|active| active.value == preferred.value)
        {
            return;
        }
        self.set_config(
            category,
            &preferred.value.to_string(),
            Some(ModelChange::Provider),
        );
    }

    fn local_providers_available(&mut self) -> bool {
        if self.ready && self.provider_editor {
            return true;
        }
        self.transcript.push(
            Role::Warning,
            "Provider management is available in Local mode.".into(),
        );
        false
    }

    pub(super) fn show_providers(&mut self) {
        let action = if self.ready && self.provider_routing {
            Action::ListProviderRoutes
        } else if self.local_providers_available() {
            Action::ListProviders
        } else {
            return;
        };
        if self.actions.unbounded_send(action).is_ok() {
            self.providers = Some(Dialog::new(Screen::Loading));
        }
    }

    pub(super) fn show_provider_form(&mut self) {
        if self.local_providers_available() {
            self.providers = Some(Dialog::new(Screen::Edit(Box::new(ProviderForm::new(None)))));
        }
    }

    pub(super) fn confirm_provider_removal(&mut self, id: String) {
        if self.local_providers_available() {
            self.providers = Some(Dialog::new(Screen::Remove(id)));
        }
    }

    pub(super) fn edit_local_provider(&mut self, action: Action) {
        if !self.local_providers_available() {
            return;
        }
        let error = if !self.can_switch() {
            Some("Stop the current turn before changing providers.")
        } else if self.actions.unbounded_send(action).is_err() {
            Some("The local agent disconnected.")
        } else {
            None
        };
        if let Some(dialog) = &mut self.providers {
            dialog.saving = error.is_none();
            dialog.error = error.map(str::to_owned);
        } else if let Some(error) = error {
            self.transcript.push(Role::Error, error.into());
        }
    }

    pub(super) fn provider_list(&mut self, result: Result<Vec<ProviderInfo>, String>) {
        let active = self.active_model_provider().map(str::to_owned);
        let Some(dialog) = &mut self.providers else {
            return;
        };
        let login_provider = match &dialog.screen {
            Screen::Loading => None,
            Screen::LoadingLogin(id) => Some(id),
            _ => return,
        };
        match result {
            Ok(items) => {
                if let Some(id) = login_provider
                    && let Some(item) = items.iter().find(|item| &item.id == id)
                {
                    self.selected_provider = Some(Selected::from(item));
                    *dialog = Dialog::new(Screen::Methods { index: 0 });
                    return;
                }
                if let Some(selected) = &mut self.selected_provider {
                    let item = items.iter().find(|item| item.id == selected.id);
                    selected.setup_message = item.and_then(|item| item.setup_message.clone());
                    selected.methods =
                        item.map_or_else(Vec::new, |item| item.login_methods.clone());
                }
                let provider = self
                    .selected_provider
                    .as_ref()
                    .map(|selected| selected.id.as_str())
                    .or(active.as_deref());
                let index = items
                    .iter()
                    .position(|item| Some(item.id.as_str()) == provider);
                dialog.screen = Screen::List {
                    items,
                    index: index.unwrap_or_default(),
                };
            }
            Err(error) => dialog.error = Some(error),
        }
    }

    pub(super) fn provider_routes(&mut self, result: Result<Vec<Route>, String>) {
        let Some(dialog) = &mut self.providers else {
            return;
        };
        if matches!(dialog.screen, Screen::Loading) {
            match result {
                Ok(items) => dialog.screen = Screen::Routes { items, index: 0 },
                Err(error) => dialog.error = Some(error),
            }
        }
    }

    pub(super) fn provider_changed(&mut self, result: Result<(), String>) {
        match result {
            Ok(()) => self.show_providers(),
            Err(error) => {
                if let Some(dialog) = &mut self.providers {
                    dialog.saving = false;
                    dialog.error = Some(error);
                } else {
                    self.transcript.push(Role::Error, error);
                }
            }
        }
    }

    pub(super) fn show_provider_login(&mut self) {
        if !self.local_providers_available() {
            return;
        }
        if self.selected_provider.is_some() {
            self.providers = Some(Dialog::new(Screen::Methods { index: 0 }));
        } else if let Some(provider) = self.active_model_provider().map(str::to_owned) {
            if self.actions.unbounded_send(Action::ListProviders).is_ok() {
                self.providers = Some(Dialog::new(Screen::LoadingLogin(provider)));
            }
        } else {
            self.show_providers();
        }
    }

    fn start_provider_login(&mut self, method: LoginMethod, key: Option<String>) {
        let Some(selected) = &self.selected_provider else {
            return;
        };
        let action = Action::ProviderLogin {
            id: selected.id.clone(),
            method,
            key,
        };
        if self.actions.unbounded_send(action).is_ok() {
            self.providers = Some(Dialog::new(Screen::SigningIn {
                url: None,
                code: None,
            }));
        }
    }

    pub(super) fn provider_login(&mut self, result: Result<ProviderLoginState, String>) {
        let Some(Dialog {
            screen: Screen::SigningIn { url, code },
            ..
        }) = &mut self.providers
        else {
            return;
        };
        match result {
            Ok(result) if result.done => {
                if let Some(selected) = &mut self.selected_provider {
                    selected.setup_message = None;
                    selected.refresh_model_after_login = true;
                    self.transcript
                        .push(Role::Note, format!("Signed in to {}.", selected.id));
                }
                if let Some(warning) = result.warning {
                    self.transcript.push(Role::Warning, warning);
                }
                self.providers = None;
            }
            Ok(result) => {
                if result.url.is_some() {
                    *url = result.url;
                }
                if result.code.is_some() {
                    *code = result.code;
                }
                let _ = self.actions.unbounded_send(Action::ProviderLoginStatus);
            }
            Err(error) => {
                let mut dialog = Dialog::new(Screen::Methods { index: 0 });
                dialog.error = Some(error);
                self.providers = Some(dialog);
            }
        }
    }

    pub(super) fn provider_login_cancelled(&mut self) {
        if self
            .providers
            .as_ref()
            .is_some_and(|dialog| matches!(dialog.screen, Screen::Cancelling))
        {
            self.providers = None;
        }
    }

    pub(super) fn provider_key(&mut self, key: Key) {
        let Some(dialog) = &mut self.providers else {
            return;
        };
        if dialog.saving {
            return;
        }
        if matches!(key, Key::Escape) {
            match dialog.screen {
                Screen::Edit(_) | Screen::Remove(_) => self.show_providers(),
                Screen::ApiKey(_) => self.show_provider_login(),
                Screen::SigningIn { .. } => {
                    if self
                        .actions
                        .unbounded_send(Action::CancelProviderLogin)
                        .is_ok()
                    {
                        dialog.screen = Screen::Cancelling;
                    } else {
                        self.providers = None;
                    }
                }
                Screen::Cancelling => {}
                _ => self.providers = None,
            }
            return;
        }
        match &mut dialog.screen {
            Screen::Loading | Screen::LoadingLogin(_) | Screen::Cancelling => {}
            Screen::List { items, index } => match key {
                Key::Text('a') => self.show_provider_form(),
                Key::HistoryUp | Key::HistoryDown | Key::Home | Key::End => {
                    move_index(key, index, items.len());
                }
                _ => {
                    let Some(item) = items.get(*index) else {
                        return;
                    };
                    match key {
                        Key::Enter => {
                            if self.model_change_pending {
                                dialog.error = Some("Wait for the model switch to finish.".into());
                                return;
                            }
                            self.selected_provider = Some(Selected::from(item));
                            self.providers = None;
                            self.activate_provider_model();
                            if let Some(message) = self.provider_setup_message().or_else(|| {
                                self.prompt_setup_message()
                                    .filter(|_| !self.model_change_pending)
                            }) {
                                self.transcript.push(Role::Warning, message.to_owned());
                            }
                        }
                        Key::Text('e') => {
                            dialog.screen = Screen::Edit(Box::new(ProviderForm::new(Some(item))));
                        }
                        Key::Text('d') => dialog.screen = Screen::Remove(item.id.clone()),
                        _ => {}
                    }
                }
            },
            Screen::Routes { items, index } => {
                move_index(key, index, items.len());
                let Some(route) = items.get(*index) else {
                    return;
                };
                let command = match key {
                    Key::Enter => {
                        let protocol = route
                            .current
                            .as_ref()
                            .map(|active| &active.api_type)
                            .or_else(|| route.supported.first())
                            .and_then(|kind| serde_json::to_value(kind).ok())
                            .and_then(|value| value.as_str().map(str::to_owned))
                            .unwrap_or_default();
                        let url = route
                            .current
                            .as_ref()
                            .map_or("", |active| active.base_url.as_str());
                        format!("/provider set {} {protocol} {url}", route.provider_id)
                    }
                    Key::Text('d') if !route.required => {
                        format!("/provider disable {}", route.provider_id)
                    }
                    _ => return,
                };
                self.providers = None;
                self.draft.set_text(&command);
                self.refresh_completion();
            }
            Screen::Edit(form) if matches!(key, Key::Enter) => {
                let edit = form.edit();
                self.edit_local_provider(Action::SaveProvider(edit));
            }
            Screen::Edit(form) => {
                dialog.error = None;
                form.key(key);
            }
            Screen::Remove(id) if matches!(key, Key::Enter) => {
                let id = id.clone();
                self.edit_local_provider(Action::RemoveProvider(id));
            }
            Screen::Remove(_) => {}
            Screen::Methods { index } => {
                let Some(selected) = &self.selected_provider else {
                    return;
                };
                if matches!(key, Key::Enter) {
                    match selected.methods.get(*index).copied() {
                        Some(LoginMethod::ApiKey) => {
                            self.providers = Some(Dialog::new(Screen::ApiKey(Composer::default())))
                        }
                        Some(method) => self.start_provider_login(method, None),
                        None => {}
                    }
                } else {
                    move_index(key, index, selected.methods.len());
                }
            }
            Screen::ApiKey(input)
                if matches!(key, Key::Enter) && !input.text().trim().is_empty() =>
            {
                let key = input.text().to_owned();
                self.start_provider_login(LoginMethod::ApiKey, Some(key));
            }
            Screen::ApiKey(input) => input_key(input, key),
            Screen::SigningIn { url, code } => {
                let text = match key {
                    Key::Enter => url.as_ref(),
                    Key::Text('c') => code.as_ref(),
                    _ => None,
                };
                if let Some(text) = text {
                    self.local_effect = Some(LocalEffect::Copy(text.clone()));
                }
            }
        }
    }
}

fn move_index(key: Key, index: &mut usize, len: usize) {
    let last = len.saturating_sub(1);
    match key {
        Key::HistoryUp => *index = index.saturating_sub(1),
        Key::HistoryDown => *index = (*index + 1).min(last),
        Key::Home => *index = 0,
        Key::End => *index = last,
        _ => {}
    }
}

impl Dialog {
    pub(super) fn draw(
        &self,
        frame: &mut Frame,
        area: Rect,
        selected: Option<&Selected>,
        palette: &Palette,
    ) {
        let title = match &self.screen {
            Screen::Edit(form) if form.create => " Add provider ",
            Screen::Edit(_) => " Edit provider ",
            Screen::Remove(_) => " Delete provider ",
            Screen::LoadingLogin(_)
            | Screen::Methods { .. }
            | Screen::ApiKey(_)
            | Screen::SigningIn { .. }
            | Screen::Cancelling => " Provider login ",
            _ => " Providers ",
        };
        let width = area.width.saturating_sub(2).min(84);
        let height = match &self.screen {
            Screen::List { items, .. } => (items.len().clamp(1, 10) as u16) + 6,
            Screen::Routes { items, .. } => (items.len().clamp(1, 10) as u16) + 6,
            Screen::Methods { .. } => {
                selected.map_or(6, |provider| provider.methods.len() as u16 + 6)
            }
            Screen::Edit(form) => form.visible().len() as u16 + 9,
            Screen::SigningIn { .. } => 19,
            _ => 10,
        }
        .min(area.height);
        let popup = Rect::new(
            area.x + (area.width - width) / 2,
            area.y + (area.height - height) / 2,
            width,
            height,
        );
        let block = Block::bordered()
            .border_type(BorderType::Rounded)
            .border_style(Style::default().fg(palette.accent))
            .style(Style::default().fg(palette.foreground).bg(palette.surface))
            .title(title)
            .title_style(Style::default().fg(palette.accent).bold());
        let inner = block.inner(popup);
        frame.render_widget(Clear, popup);
        frame.render_widget(block, popup);
        let [body, footer] =
            Layout::vertical([Constraint::Min(1), Constraint::Length(2)]).areas(inner);
        let (mut lines, hint) = self.contents(body, selected, palette);
        if let Some(error) = &self.error {
            lines.push(Line::from(error.as_str()).fg(palette.error));
        }
        let hint = if self.saving {
            "Saving provider configuration…"
        } else {
            hint
        };
        let paragraph = Paragraph::new(lines);
        let paragraph = if matches!(
            &self.screen,
            Screen::List { .. } | Screen::Routes { .. } | Screen::Methods { .. }
        ) {
            paragraph
        } else {
            paragraph.wrap(Wrap { trim: false })
        };
        frame.render_widget(paragraph, body);
        frame.render_widget(
            Paragraph::new(hint)
                .wrap(Wrap { trim: true })
                .fg(palette.muted),
            footer,
        );
    }

    fn contents<'a>(
        &'a self,
        area: Rect,
        selected: Option<&'a Selected>,
        palette: &Palette,
    ) -> (Vec<Line<'a>>, &'static str) {
        match &self.screen {
            Screen::Loading | Screen::LoadingLogin(_) => {
                let lines = self
                    .error
                    .is_none()
                    .then(|| Line::from("Loading providers…"))
                    .into_iter()
                    .collect();
                (lines, "Esc close")
            }
            Screen::List { items, index } => {
                if items.is_empty() {
                    return (
                        vec![Line::from("No providers configured")],
                        "a add · Esc close",
                    );
                }
                let rows = usize::from(area.height.saturating_sub(1)).clamp(1, 10);
                let lines = items
                    .iter()
                    .enumerate()
                    .skip(index.saturating_sub(rows - 1))
                    .take(rows)
                    .map(|(row, item)| {
                        let active = selected.is_some_and(|selected| selected.id == item.id);
                        option_line(
                            format!(
                                "{}  ·  {}{}",
                                item.id,
                                item.api_type,
                                if active { "  ·  selected" } else { "" }
                            ),
                            row == *index,
                            palette,
                        )
                    })
                    .collect();
                (
                    lines,
                    "↑/↓ browse · Enter select · a add · e edit · d delete · Esc close",
                )
            }
            Screen::Routes { items, index } => {
                let rows = usize::from(area.height.saturating_sub(1)).clamp(1, 10);
                let mut lines: Vec<_> = items
                    .iter()
                    .enumerate()
                    .skip(index.saturating_sub(rows - 1))
                    .take(rows)
                    .map(|(row, route)| {
                        let url = route
                            .current
                            .as_ref()
                            .map_or("disabled", |config| config.base_url.as_str());
                        option_line(
                            format!("{}  ·  {url}", route.provider_id),
                            row == *index,
                            palette,
                        )
                    })
                    .collect();
                if lines.is_empty() {
                    lines.push(Line::from("No provider routes advertised"));
                }
                let hint = match items.get(*index) {
                    Some(route) if !route.required => {
                        "↑/↓ browse · Enter edit route · d disable · Esc close"
                    }
                    Some(_) => "↑/↓ browse · Enter edit route · Esc close",
                    None => "Esc close",
                };
                (lines, hint)
            }
            Screen::Edit(form) => {
                let labels = [
                    "Name",
                    "Provider type",
                    "Base URL",
                    "Custom model IDs",
                    "AWS profile",
                    "AWS region",
                ];
                let mut lines: Vec<_> = form
                    .visible()
                    .iter()
                    .map(|&index| {
                        let focused = index == form.focused;
                        let field = &form.fields[index];
                        let value = if index == TYPE_FIELD && focused {
                            format!("◀ {} ▶", field.text())
                        } else {
                            field_value(field, focused)
                        };
                        option_line(format!("{}  {value}", labels[index]), focused, palette)
                    })
                    .collect();
                if form.focused == TYPE_FIELD {
                    lines.push(Line::from("←/→ change provider type").fg(palette.muted));
                }
                (lines, "Tab / Shift+Tab field · Enter save · Esc back")
            }
            Screen::Remove(id) => (
                vec![Line::from(format!("Delete provider {id}?"))],
                "Enter delete · Esc back",
            ),
            Screen::Methods { index } => {
                let mut lines = Vec::new();
                if let Some(selected) = selected {
                    let show_provider = area.height > 1;
                    if show_provider {
                        lines.push(Line::from(selected.id.as_str()));
                    }
                    let rows = usize::from(area.height)
                        .saturating_sub(
                            usize::from(show_provider) + usize::from(self.error.is_some()),
                        )
                        .max(1);
                    lines.extend(
                        selected
                            .methods
                            .iter()
                            .enumerate()
                            .skip(index.saturating_sub(rows - 1))
                            .take(rows)
                            .map(|(row, method)| {
                                option_line(method.label().to_owned(), row == *index, palette)
                            }),
                    );
                }
                (
                    lines,
                    "↑/↓ choose a sign-in method · Enter continue · Esc close",
                )
            }
            Screen::ApiKey(input) => (
                vec![
                    Line::from("API key"),
                    Line::from(format!(
                        "{}▏",
                        "•".repeat(input.text().chars().count().min(72))
                    ))
                    .fg(palette.accent),
                ],
                "Enter save to keyring · Esc back",
            ),
            Screen::Cancelling => (vec![Line::from("Cancelling sign-in…")], ""),
            Screen::SigningIn { url, code } => {
                let mut lines = vec![Line::from("Waiting for sign-in…")];
                if let Some(code) = code {
                    lines.push(
                        Line::from(format!("Device code  {code}"))
                            .fg(palette.accent)
                            .bold(),
                    );
                }
                if let Some(url) = url {
                    lines.push(Line::from(url.as_str()));
                }
                let hint = match (url, code) {
                    (Some(_), Some(_)) => "Enter copy sign-in URL · c copy code · Esc cancel",
                    (Some(_), None) => "Enter copy sign-in URL · Esc cancel",
                    _ => "Esc cancel",
                };
                (lines, hint)
            }
        }
    }
}

fn option_line(text: String, focused: bool, palette: &Palette) -> Line<'static> {
    if focused {
        Line::from(format!("› {text}")).fg(palette.accent).bold()
    } else {
        Line::from(format!("  {text}"))
    }
}

fn field_value(field: &Composer, focused: bool) -> String {
    let mut value = field.text().to_owned();
    if focused {
        value.insert(field.cursor(), '▏');
    }
    value
}
