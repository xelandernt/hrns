use std::collections::BTreeMap;

use agent_client_protocol::v1::schema::ElicitationContentValue;
use ratatui::layout::Position;

use crate::acp::Action;
use crate::mcp_types::ConsentDecision;
use crate::transcript::{ManualApproval, Role};

use super::picker;
use super::{App, Key, MCP_CONSENT_CHOICES, ScrollBar};

impl App {
    pub fn paste(&mut self, text: &str) {
        if self.picker.as_ref().is_some_and(picker::Picker::is_agents) {
            self.agents_paste(text);
            return;
        }
        if self.elicitations.front().is_some() {
            if let Some(pending) = self.elicitations.front_mut() {
                pending.state.insert(text);
            }
            return;
        }
        if !self.approvals.is_empty() || !self.mcp_consent.is_empty() {
            return;
        }
        if let Some(dialog) = &mut self.providers {
            dialog.paste(text);
            return;
        }
        if self.picker.is_some() {
            self.paste_into_picker(text);
            return;
        }
        self.draft.paste(text);
        self.refresh_completion();
    }

    pub(super) fn paste_into_picker(&mut self, text: &str) {
        if let Some(picker) = &mut self.picker
            && !matches!(picker.source, picker::Source::Details)
        {
            picker
                .query
                .extend(text.chars().filter(|ch| !ch.is_control()));
            picker.selected = 0;
            self.maybe_page_sessions();
        }
    }

    #[cfg(target_family = "wasm")]
    pub fn transcript_page_height(&self) -> f64 {
        f64::from(self.transcript_area.height.saturating_sub(2).max(1))
    }

    pub fn pointer_scroll(&mut self, position: Position, rows: i32) -> bool {
        if self.picker.is_some()
            || self.providers.is_some()
            || !(self.transcript_area.contains(position)
                || self
                    .scroll_bar
                    .is_some_and(|bar| bar.area.contains(position)))
        {
            return false;
        }
        self.transcript.scroll(rows);
        true
    }

    /// Tracks what the pointer is over so the scrollbar thumb and follow control highlight.
    pub fn pointer_move(&mut self, position: Position) -> bool {
        let over_bar = self
            .scroll_bar
            .is_some_and(|bar| bar.area.contains(position));
        let over_follow = self.follow_area.is_some_and(|area| area.contains(position));
        if (over_bar, over_follow) == (self.bar_hover, self.follow_hover) {
            return false;
        }
        self.bar_hover = over_bar;
        self.follow_hover = over_follow;
        true
    }

    pub fn pointer_down(&mut self, position: Position) -> bool {
        if self.picker.is_some() || self.providers.is_some() {
            return false;
        }
        if self.follow_area.is_some_and(|area| area.contains(position)) {
            self.follow_pressed = true;
            return true;
        }
        let Some(bar) = self.scroll_bar.filter(|bar| bar.area.contains(position)) else {
            return self.transcript_area.contains(position)
                && self
                    .transcript
                    .toggle_disclosure_at(position.y - self.transcript_area.y);
        };
        let row = position.y.saturating_sub(bar.area.y);
        let start = self.thumb_start(bar);
        if row >= start && row < start.saturating_add(bar.thumb) {
            self.scroll_drag = Some(row - start);
        } else {
            // A press on the track centres the thumb on the pointer, then drags from there.
            let grab = bar.thumb / 2;
            self.transcript
                .scroll_to(self.scroll_top_at(bar, row, grab));
            let start = self.thumb_start(bar);
            self.scroll_drag = Some(row.saturating_sub(start).min(bar.thumb.saturating_sub(1)));
        }
        true
    }

    pub fn pointer_drag(&mut self, position: Position) -> bool {
        let (Some(bar), Some(grab)) = (self.scroll_bar, self.scroll_drag) else {
            return false;
        };
        let row = position.y.saturating_sub(bar.area.y);
        self.transcript
            .scroll_to(self.scroll_top_at(bar, row, grab));
        true
    }

    pub fn pointer_up(&mut self, position: Position) -> bool {
        let mut changed = self.scroll_drag.take().is_some();
        if std::mem::take(&mut self.follow_pressed)
            && self.follow_area.is_some_and(|area| area.contains(position))
        {
            self.transcript.follow();
            changed = true;
        }
        changed
    }

    fn thumb_start(&self, bar: ScrollBar) -> u16 {
        let travel = bar.area.height.saturating_sub(bar.thumb);
        if travel == 0 || bar.max_top == 0 {
            return 0;
        }
        let offset = self
            .transcript
            .top()
            .saturating_mul(usize::from(travel))
            .saturating_add(bar.max_top / 2)
            / bar.max_top;
        u16::try_from(offset).unwrap_or(travel).min(travel)
    }

    fn scroll_top_at(&self, bar: ScrollBar, row: u16, grab: u16) -> usize {
        let travel = bar.area.height.saturating_sub(bar.thumb);
        if travel == 0 {
            return 0;
        }
        let start = row.saturating_sub(grab).min(travel);
        (usize::from(start) * bar.max_top + usize::from(travel) / 2) / usize::from(travel)
    }

    pub fn key(&mut self, key: Key) {
        if self.agents_action_key(key) {
            return;
        }
        if matches!(key, Key::Agents) {
            if self.picker.as_ref().is_some_and(picker::Picker::is_agents) {
                self.picker = None;
            } else {
                self.show_sessions();
            }
            return;
        }
        if self.picker.as_ref().is_some_and(picker::Picker::is_agents) {
            self.picker_key(key);
            return;
        }
        if self.mcp_consent.front().is_some() {
            self.mcp_consent_key(key);
            return;
        }
        if self.elicitations.front().is_some() {
            self.elicitation_key(key);
            return;
        }

        if !self.approvals.is_empty() {
            self.approval_key(key);
            return;
        }
        if self.providers.is_some() {
            self.provider_key(key);
            return;
        }
        if self.picker.is_some() {
            self.picker_key(key);
            return;
        }

        if let Some(completion) = &self.completion {
            match key {
                Key::Escape => {
                    #[cfg(not(target_family = "wasm"))]
                    if let Some(file) = &completion.file {
                        self.file_search
                            .dismiss(file.range.clone(), self.draft.text());
                    }
                    self.completion = None;
                    return;
                }
                Key::Tab | Key::Enter
                    if matches!(key, Key::Tab)
                        || (completion.file.is_some() && !completion.items.is_empty()) =>
                {
                    self.accept_completion();
                    self.refresh_completion();
                    return;
                }
                Key::HistoryUp => {
                    self.select_completion(false);
                    return;
                }
                Key::HistoryDown => {
                    self.select_completion(true);
                    return;
                }
                _ => {}
            }
        }

        match key {
            Key::Text(ch) => self.draft.insert(ch.encode_utf8(&mut [0; 4])),
            Key::Newline => self.draft.insert("\n"),
            Key::Backspace => self.draft.backspace(),
            Key::Delete => self.draft.delete(),
            Key::Left => self.draft.left(),
            Key::EditQueuedPrompt if self.draft.text().is_empty() && !self.queue.is_empty() => {
                self.edit_queued_prompt();
            }
            Key::EditQueuedPrompt => self.draft.left(),
            Key::Right => self.draft.right(),
            Key::Home => self.draft.home(),
            Key::End => self.draft.end(),
            Key::HistoryUp => self.draft.recall(true),
            Key::HistoryDown => self.draft.recall(false),
            Key::ScrollUp if let Some(scroll) = &mut self.todo_scroll => {
                *scroll = scroll.saturating_sub(3);
            }
            Key::ScrollDown if let Some(scroll) = &mut self.todo_scroll => {
                *scroll = scroll.saturating_add(3);
            }
            Key::Escape if self.todo_scroll.is_some() => {
                self.todo_open = false;
                self.todo_scroll = None;
            }
            Key::Escape
                if !self.transcript.following() && self.turns == 0 && !self.remote_busy() =>
            {
                self.transcript.follow()
            }
            Key::ScrollUp => self.transcript.scroll_page(false),
            Key::ScrollDown => self.transcript.scroll_page(true),
            Key::PreviousPrompt => self.transcript.jump_prompt(false),
            Key::NextPrompt => self.transcript.jump_prompt(true),
            Key::LatestOutput => self.transcript.follow(),
            Key::ToggleTools => self.transcript.toggle_tools(),
            Key::ToggleSidebar => self.toggle_sidebar(),
            Key::Agents | Key::NewSession => {}
            Key::Reconnect => self.reconnect(),
            Key::Escape
                if self.status != "Cancelling" && (self.turns > 0 || self.remote_busy()) =>
            {
                if self.actions.unbounded_send(Action::Cancel).is_ok() {
                    self.status = "Cancelling";
                }
            }
            Key::Enter if !self.draft.text().trim().is_empty() => self.submit(),
            Key::Tab => self.draft.insert("\t"),
            Key::Escape | Key::Enter | Key::BackTab => {}
        }
        self.refresh_completion();
    }

    fn approval_key(&mut self, key: Key) {
        let Some(pending) = self.approvals.front_mut() else {
            return;
        };
        let count = pending.request.options.len();
        let answer = match key {
            Key::HistoryUp => {
                pending.selected = pending.selected.saturating_sub(1);
                None
            }
            Key::HistoryDown => {
                pending.selected = (pending.selected + 1).min(count.saturating_sub(1));
                None
            }
            Key::ScrollUp => {
                pending.detail_scroll = pending.detail_scroll.saturating_sub(3);
                None
            }
            Key::ScrollDown => {
                pending.detail_scroll = pending.detail_scroll.saturating_add(3);
                None
            }
            Key::Enter if pending.selected < count => Some(Some(pending.selected)),
            Key::Text(digit @ '1'..='9') => {
                let index = usize::from(digit as u8 - b'1');
                (index < count).then_some(Some(index))
            }
            Key::Escape => Some(None),
            _ => None,
        };
        if let Some(answer) = answer
            && let Some(pending) = self.approvals.pop_front()
        {
            use agent_client_protocol::v1::schema::PermissionOptionKind;

            let request = pending.request;
            let decision = answer.and_then(|index| match request.options[index].kind {
                PermissionOptionKind::AllowOnce | PermissionOptionKind::AllowAlways => {
                    Some(ManualApproval::Granted)
                }
                PermissionOptionKind::RejectOnce | PermissionOptionKind::RejectAlways => {
                    Some(ManualApproval::Rejected)
                }
                _ => None,
            });
            let decision = if request.respond.send(answer).is_ok() {
                decision
            } else {
                None
            };
            self.transcript
                .resolve_approval(&request.tool_call_id, &request.title, decision);
        }
    }

    fn mcp_consent_key(&mut self, key: Key) {
        let Some(pending) = self.mcp_consent.front_mut() else {
            return;
        };
        let decision = match key {
            Key::HistoryUp => {
                pending.selected = pending.selected.saturating_sub(1);
                None
            }
            Key::HistoryDown => {
                pending.selected = (pending.selected + 1).min(MCP_CONSENT_CHOICES.len() - 1);
                None
            }
            Key::Enter => Some(MCP_CONSENT_CHOICES[pending.selected].1),
            Key::Text(digit @ '1'..='4') => {
                Some(MCP_CONSENT_CHOICES[usize::from(digit as u8 - b'1')].1)
            }
            Key::Text('y' | 'Y') => Some(ConsentDecision::Once),
            Key::Escape | Key::Text('n' | 'N') => Some(ConsentDecision::Deny),
            _ => None,
        };
        if let Some(decision) = decision
            && let Some(pending) = self.mcp_consent.pop_front()
        {
            if matches!(decision, ConsentDecision::Session | ConsentDecision::Always) {
                self.mcp_grants.insert(pending.name.clone());
                self.transcript.push(
                    Role::Note,
                    format!("MCP {} approved until exit", pending.name),
                );
            }
            let _ = pending.respond.send(decision);
        }
    }

    fn elicitation_key(&mut self, key: Key) {
        enum Done {
            None,
            Decline,
            Accept(BTreeMap<String, ElicitationContentValue>),
        }
        let mut done = Done::None;
        if let Some(pending) = self.elicitations.front_mut() {
            match key {
                Key::Escape => done = Done::Decline,
                Key::Enter => {
                    if let Some(answers) = pending.state.enter() {
                        done = Done::Accept(answers);
                    }
                }
                Key::Backspace => pending.state.backspace(),
                Key::Tab | Key::Right => pending.state.move_field(true),
                Key::BackTab | Key::Left => pending.state.move_field(false),
                Key::HistoryDown | Key::ScrollDown => pending.state.move_choice(true),
                Key::HistoryUp | Key::ScrollUp => pending.state.move_choice(false),
                Key::Text(character) if !pending.state.choice_key(character) => {
                    pending.state.insert(character.encode_utf8(&mut [0; 4]));
                }
                _ => {}
            }
        }
        match done {
            Done::None => {}
            Done::Decline => {
                if let Some(pending) = self.elicitations.pop_front() {
                    let _ = pending.respond.send(None);
                }
            }
            Done::Accept(answers) => {
                if let Some(pending) = self.elicitations.pop_front() {
                    let _ = pending.respond.send(Some(answers));
                }
            }
        }
    }
}
