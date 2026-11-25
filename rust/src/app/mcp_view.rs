use ratatui::Frame;
use ratatui::layout::{Constraint, Layout, Rect};
use ratatui::style::{Style, Stylize};
use ratatui::text::Line;
use ratatui::widgets::{List, ListState, Paragraph};

use super::{
    App, Key,
    picker::{Picker, Source},
};
#[cfg(not(target_family = "wasm"))]
use crate::acp::Action;
use crate::{
    mcp_types::{Health, SIGN_IN_REQUIRED},
    theme::Palette,
    transcript::wrap,
};

pub(super) struct McpDetails {
    name: String,
    tools: Option<Result<Vec<String>, String>>,
    scroll: usize,
}

impl App {
    pub(super) fn mcp_status<'a>(&'a self, item: &'a Health) -> &'a str {
        if self.mcp_signing_in.as_deref() == Some(&item.name) {
            "Signing in…"
        } else {
            &item.state
        }
    }

    pub(super) fn mcp_picker_key(&mut self, key: Key) -> bool {
        let Some(picker) = &self.picker else {
            return false;
        };
        let Source::Mcp(detail) = &picker.source else {
            return false;
        };
        #[cfg(not(target_family = "wasm"))]
        if matches!(key, Key::Text(' ')) {
            let name = detail
                .as_ref()
                .map(|detail| detail.name.clone())
                .or_else(|| {
                    self.picker_rows(picker)
                        .get(picker.selected)
                        .map(|row| row.value.clone())
                });
            if let Some(name) = name
                && self.mcp_signing_in.is_none()
                && self
                    .mcp_health
                    .iter()
                    .any(|item| item.name == name && item.state == SIGN_IN_REQUIRED)
                && self
                    .actions
                    .unbounded_send(Action::McpSignIn(name.clone()))
                    .is_ok()
            {
                self.mcp_signing_in = Some(name);
            }
            return true;
        }
        if detail.is_some() {
            if let Some(Picker {
                source: Source::Mcp(detail),
                ..
            }) = &mut self.picker
            {
                if matches!(key, Key::Escape) {
                    *detail = None;
                } else if let Some(detail) = detail {
                    match key {
                        Key::HistoryUp => detail.scroll = detail.scroll.saturating_sub(1),
                        Key::HistoryDown => detail.scroll = detail.scroll.saturating_add(1),
                        Key::ScrollUp => detail.scroll = detail.scroll.saturating_sub(10),
                        Key::ScrollDown => detail.scroll = detail.scroll.saturating_add(10),
                        Key::Home => detail.scroll = 0,
                        _ => {}
                    }
                }
            }
            return true;
        }
        if !matches!(key, Key::Enter) {
            return false;
        }
        if let Some(row) = self.picker_rows(picker).get(picker.selected) {
            let name = row.value.clone();
            let mut tools = None;
            if row.detail == "connected" {
                #[cfg(not(target_family = "wasm"))]
                if self
                    .actions
                    .unbounded_send(Action::McpTools(name.clone()))
                    .is_err()
                {
                    tools = Some(Err("MCP connection closed".into()));
                }
                #[cfg(target_family = "wasm")]
                {
                    tools = Some(Err("MCP tools are unavailable in the browser".into()));
                }
            }
            if let Some(Picker {
                source: Source::Mcp(detail),
                ..
            }) = &mut self.picker
            {
                *detail = Some(McpDetails {
                    name,
                    tools,
                    scroll: 0,
                });
            }
        }
        true
    }

    #[cfg(not(target_family = "wasm"))]
    pub(super) fn mcp_sign_in_result(&mut self, name: String, result: Result<(), String>) {
        if self.mcp_signing_in.as_deref() != Some(&name) {
            return;
        }
        self.mcp_signing_in = None;
        if let Some(item) = self.mcp_health.iter_mut().find(|item| item.name == name) {
            match result {
                Ok(()) => {
                    item.state = "Signed in · reconnect or start a new session".into();
                    item.detail = "Available at the next session setup. Start a new session or reopen a saved one.".into();
                    item.startup_failure = None;
                }
                Err(error) => {
                    item.detail = error;
                    if let Some(Picker {
                        source: Source::Mcp(detail),
                        ..
                    }) = &mut self.picker
                    {
                        *detail = Some(McpDetails {
                            name,
                            tools: None,
                            scroll: 0,
                        });
                    }
                }
            }
        }
    }

    #[cfg(not(target_family = "wasm"))]
    pub(super) fn mcp_tools_result(&mut self, name: &str, result: Result<Vec<String>, String>) {
        if let Some(Picker {
            source: Source::Mcp(Some(detail)),
            ..
        }) = &mut self.picker
            && detail.name == name
        {
            detail.tools = Some(result);
        }
    }

    pub(super) fn draw_mcp_picker(
        &self,
        frame: &mut Frame,
        area: Rect,
        picker: &Picker,
        palette: &Palette,
    ) {
        let [heading, body, footer] = Layout::vertical([
            Constraint::Length(1),
            Constraint::Min(0),
            Constraint::Length(1),
        ])
        .areas(area);
        let rows = self.picker_rows(picker);
        let detail = match &picker.source {
            Source::Mcp(Some(detail)) => self
                .mcp_health
                .iter()
                .find(|item| item.name == detail.name)
                .map(|item| (item, detail)),
            _ => None,
        };
        let selected = rows.get(picker.selected);
        let status = detail
            .map(|(item, _)| self.mcp_status(item))
            .or_else(|| selected.map(|row| row.detail.as_str()));
        let sign_in = if status == Some(SIGN_IN_REQUIRED) && self.mcp_signing_in.is_none() {
            "   Space sign in"
        } else {
            ""
        };
        let hint = if let Some((item, detail)) = detail {
            frame.render_widget(
                Line::from(format!(" {} · {}", item.name, self.mcp_status(item))).bold(),
                heading,
            );
            let mut text = format!("{} · {}", item.transport, item.target);
            if !item.protocol.is_empty() {
                text.push_str(&format!(" · MCP {}", item.protocol));
            }
            if !item.detail.is_empty() {
                text.push_str("\n\n");
                text.push_str(&item.detail);
            }
            if item.state == "unavailable" && item.startup_failure.is_some() {
                text.push_str("\n\nFix the startup error, then start a new session.");
            }
            if item.state == "connected" {
                text.push_str("\n\nTools");
                match &detail.tools {
                    None => text.push_str("\nLoading tools…"),
                    Some(Ok(tools)) if tools.is_empty() => text.push_str("\nNo available tools"),
                    Some(Ok(tools)) => {
                        text.push('\n');
                        text.push_str(&tools.join("\n"));
                    }
                    Some(Err(error)) => {
                        text.push('\n');
                        text.push_str(error);
                    }
                }
            }
            text.retain(|ch| ch == '\n' || !ch.is_control());
            let lines = wrap(&text, usize::from(body.width.max(1)));
            let height = usize::from(body.height);
            let top = detail.scroll.min(lines.len().saturating_sub(height));
            frame.render_widget(
                Paragraph::new(
                    lines
                        .into_iter()
                        .skip(top)
                        .take(height)
                        .map(|range| Line::from(&text[range]))
                        .collect::<Vec<_>>(),
                ),
                body,
            );
            " ↑/↓ scroll   Esc back"
        } else {
            let search = if picker.query.is_empty() {
                " Search…".into()
            } else {
                format!(" Search  {}", picker.query)
            };
            frame.render_widget(Line::from(search).fg(palette.muted), heading);
            if rows.is_empty() {
                frame.render_widget(Line::from(picker.status(true)).fg(palette.muted), body);
            } else {
                let list = List::new(
                    rows.iter()
                        .map(|row| format!("{}  ·  {}", row.label, row.detail)),
                )
                .highlight_symbol(" › ")
                .highlight_style(Style::default().fg(palette.accent).bg(palette.panel).bold());
                let mut state =
                    ListState::default().with_selected(Some(picker.selected.min(rows.len() - 1)));
                frame.render_stateful_widget(list, body, &mut state);
            }
            picker.hint(false, false)
        };
        frame.render_widget(
            Line::from(format!("{hint}{sign_in}")).fg(palette.muted),
            footer,
        );
    }
}
