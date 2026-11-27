mod actions;
mod entry;
mod layout;
mod table;

use std::collections::BTreeMap;

use agent_client_protocol::v1::schema::ToolCallStatus;

pub(crate) use self::actions::ACTION_FRAME_MILLIS;
use self::actions::ActionBlocks;
use crate::acp::{PermissionRequest, ToolDisplay};
use entry::Entry;
pub use entry::{ManualApproval, Role};
pub(crate) use layout::{wrap, wrap_up_to};

#[derive(Default)]
pub struct Transcript {
    entries: Vec<Entry>,
    actions: ActionBlocks,
    replay_from: Option<usize>,
    measured: usize,
    laid_out: usize,
    viewport_height: usize,
    width: u16,
    total: usize,
    top: usize,
    following: bool,
    output_pending: bool,
    selected_prompt: Option<usize>,
    show_thoughts: bool,
}

impl Transcript {
    pub fn new() -> Self {
        Self {
            following: true,
            ..Self::default()
        }
    }

    pub fn push(&mut self, role: Role, text: String) {
        self.push_entry(role, text, false, false);
    }

    fn push_entry(&mut self, role: Role, mut text: String, expanded: bool, live: bool) {
        self.output_pending |= !self.following;
        text.retain(|ch| ch == '\n' || !ch.is_control());
        if matches!(role, Role::User | Role::Agent) {
            self.finish_actions();
        } else if role == Role::Thought && !text.trim().is_empty() {
            self.register_action(self.entries.len(), live);
        }
        self.entries.push(Entry {
            role,
            text,
            rows: Vec::new(),
            code_rows: Vec::new(),
            table_rows: BTreeMap::new(),
            detail: None,
            detail_rows: Vec::new(),
            preview_start: None,
            shell: false,
            id: None,
            expanded,
            tool_status: None,
            approval: None,
            action_heading: false,
            action_visible: true,
            action_preview: false,
            end: 0,
        });
    }

    pub fn tool(&mut self, id: String, mut display: ToolDisplay, live: bool) {
        let preview_start = display.prepare_detail().filter(|_| {
            display.preview_output
                && matches!(
                    display.status,
                    ToolCallStatus::Completed | ToolCallStatus::Failed
                )
        });
        let ToolDisplay {
            title,
            subject,
            status,
            detail,
            shell,
            ..
        } = display;
        self.output_pending |= !self.following;
        let mut text = if subject.is_empty() {
            title
        } else {
            format!("{title} {subject}")
        };
        if shell {
            text.retain(|ch| matches!(ch, '\n' | '\t') || !ch.is_control());
            text = text.replace('\t', "    ");
        } else {
            text.retain(|ch| !ch.is_control() || ch.is_whitespace());
            text = text.split_whitespace().collect::<Vec<_>>().join(" ");
        }
        if !shell && status != ToolCallStatus::Completed {
            text.push_str("  ·  ");
            text.push_str(crate::acp::status_label(status));
        }
        if let Some(index) = self.tool_index(&id) {
            let entry = &mut self.entries[index];
            if entry.approval != Some(ManualApproval::Rejected) {
                entry.text = text;
            }
            entry.tool_status = Some(status);
            entry.shell = shell;
            entry.detail = Some(detail);
            entry.preview_start = preview_start;
            self.measured = self.measured.min(index);
            return;
        }
        self.register_action(self.entries.len(), live);
        self.entries.push(Entry {
            role: Role::Tool,
            text,
            rows: Vec::new(),
            code_rows: Vec::new(),
            table_rows: BTreeMap::new(),
            detail: Some(detail),
            detail_rows: Vec::new(),
            preview_start,
            shell,
            id: Some(id),
            expanded: false,
            tool_status: Some(status),
            approval: None,
            action_heading: false,
            action_visible: true,
            action_preview: false,
            end: 0,
        });
    }

    fn tool_index(&self, id: &str) -> Option<usize> {
        let start = self.replay_from.unwrap_or(0);
        self.entries[start..]
            .iter()
            .rposition(|entry| entry.id.as_deref() == Some(id))
            .map(|index| start + index)
    }

    pub fn show_pending_approval(&mut self, request: &PermissionRequest) {
        let index = self.tool_index(&request.tool_call_id).unwrap_or_else(|| {
            self.tool(
                request.tool_call_id.clone(),
                ToolDisplay {
                    title: request.title.clone(),
                    subject: String::new(),
                    status: ToolCallStatus::Pending,
                    detail: request.detail.clone(),
                    output: String::new(),
                    preview_output: false,
                    shell: request.shell,
                },
                true,
            );
            self.entries.len() - 1
        });
        self.entries[index].approval = Some(ManualApproval::Pending);
        self.output_pending |= !self.following;
        self.measured = self.measured.min(index);
    }

    pub fn resolve_approval(&mut self, id: &str, title: &str, decision: Option<ManualApproval>) {
        let Some(index) = self.tool_index(id) else {
            return;
        };
        let entry = &mut self.entries[index];
        if entry.approval != Some(ManualApproval::Pending) {
            return;
        }
        entry.approval = decision;
        if decision == Some(ManualApproval::Rejected) {
            entry.text = title.to_owned();
        }
        self.output_pending |= !self.following;
        self.measured = self.measured.min(index);
    }

    pub fn toggle_tools(&mut self) {
        self.set_tools_expanded(
            self.entries
                .iter()
                .any(|entry| entry.has_detail() && !entry.expanded),
        );
    }

    pub fn set_tools_expanded(&mut self, expanded: bool) {
        for entry in &mut self.entries {
            if entry.role == Role::Tool {
                entry.expanded = expanded;
            }
        }
        self.measured = 0;
    }

    pub fn toggle_disclosure_at(&mut self, row: u16) -> bool {
        let row = self.top.saturating_add(usize::from(row));
        let index = self.entries[..self.laid_out].partition_point(|entry| entry.end <= row);
        if index == self.laid_out {
            return false;
        }
        let start = self.entry_start(index);
        let entry = &mut self.entries[index];
        if entry.action_heading && row == start {
            self.actions.toggle(index);
            self.measured = self.measured.min(index);
            self.following = false;
            self.selected_prompt = None;
            return true;
        }
        if !entry.action_visible {
            return false;
        }
        let start = start + usize::from(entry.action_heading);
        let header_hit = match entry.role {
            Role::Tool => {
                entry.has_detail()
                    && row
                        < start
                            + entry.rows.len()
                            + if entry.expanded {
                                0
                            } else {
                                entry.detail_rows.len() + usize::from(entry.has_more_output())
                            }
            }
            Role::Thought => row == start,
            _ => false,
        };
        if !header_hit {
            return false;
        }
        entry.expanded = !entry.expanded;
        self.measured = self.measured.min(index);
        self.following = false;
        self.selected_prompt = None;
        true
    }

    /// Show or collapse the text of reasoning blocks; their headers stay visible.
    pub fn show_thoughts(&mut self, show: bool) {
        if self.show_thoughts != show {
            self.show_thoughts = show;
            for entry in &mut self.entries {
                if entry.role == Role::Thought {
                    entry.expanded = false;
                }
            }
            self.measured = 0;
        }
    }

    pub fn append(&mut self, role: Role, text: String) {
        self.append_chunk(role, text, false, false);
    }

    pub fn append_thought(&mut self, text: String, live: bool) {
        self.append_chunk(Role::Thought, text, live && self.show_thoughts, live);
    }

    pub fn show_details(&mut self, show: bool) {
        if self.actions.set_compact(!show) {
            self.measured = 0;
        }
    }

    pub fn details_shown(&self) -> bool {
        !self.actions.compact()
    }

    pub fn animating_actions(&self) -> bool {
        !self.replaying() && self.actions.animating()
    }

    pub fn finish_actions(&mut self) {
        if let Some(index) = self.actions.finish() {
            self.measured = self.measured.min(index);
        }
    }

    fn register_action(&mut self, index: usize, live: bool) {
        self.measured = self.measured.min(self.actions.push(index, live));
    }

    fn append_chunk(&mut self, role: Role, mut text: String, expand_new: bool, live: bool) {
        self.output_pending |= !self.following;
        let start = self.replay_from.unwrap_or(0);
        if let Some(index) = self.entries.len().checked_sub(1)
            && index >= start
            && self.entries[index].role == role
            && (role != Role::Thought || self.actions.can_extend(index))
        {
            let entry = &mut self.entries[index];
            text.retain(|ch| ch == '\n' || !ch.is_control());
            let new_thought =
                role == Role::Thought && entry.text.trim().is_empty() && !text.trim().is_empty();
            entry.text.push_str(&text);
            self.measured = self.measured.min(index);
            if new_thought {
                self.register_action(index, live);
            }
        } else {
            self.push_entry(role, text, expand_new, live);
        }
    }
}
