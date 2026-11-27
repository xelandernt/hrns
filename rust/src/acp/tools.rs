use std::borrow::Cow;
use std::collections::HashMap;

use agent_client_protocol::v1::schema::{
    ContentBlock, SessionUpdate, ToolCallContent, ToolCallLocation, ToolCallStatus, ToolKind,
};

use crate::tool_input;

use super::Event;

fn estimated_cost(amount: f64) -> String {
    format!("est. {amount:.6} USD")
}

pub(super) fn text_update(
    update: SessionUpdate,
    tools: &mut HashMap<String, ToolState>,
) -> Option<Event> {
    match update {
        SessionUpdate::UserMessageChunk(chunk) => {
            content_text(&chunk.content).map(|text| Event::UserChunk(text.into_owned()))
        }
        SessionUpdate::AgentMessageChunk(chunk) => {
            content_text(&chunk.content).map(|text| Event::Chunk(text.into_owned()))
        }
        SessionUpdate::AgentThoughtChunk(chunk) => {
            content_text(&chunk.content).map(|text| Event::Thought(text.into_owned()))
        }
        SessionUpdate::ConfigOptionUpdate(update) => Some(Event::Options(update.config_options)),
        SessionUpdate::AvailableCommandsUpdate(update) => Some(Event::AgentCommands(
            update
                .available_commands
                .into_iter()
                .map(|command| (command.name, command.description))
                .collect(),
        )),
        SessionUpdate::SessionInfoUpdate(update) => {
            if let Some(cost) = update
                .meta
                .as_ref()
                .and_then(|meta| meta.get("hrns/usage/cost_without_context"))
            {
                return Some(Event::Usage {
                    context: None,
                    cost: cost.as_f64().map(estimated_cost),
                });
            }
            if let Some(tokens) = update
                .meta
                .as_ref()
                .and_then(|meta| meta.get("hrns/usage/reasoning_tokens"))
            {
                Some(Event::ReasoningTokens(tokens.as_u64()))
            } else {
                Some(Event::SessionTitle(update.title.take()))
            }
        }
        SessionUpdate::CurrentModeUpdate(update) => {
            Some(Event::Mode(update.current_mode_id.to_string()))
        }
        SessionUpdate::Plan(plan) => Some(Event::Plan(plan.entries)),
        SessionUpdate::UsageUpdate(usage) => {
            let estimated = usage
                .meta
                .as_ref()
                .and_then(|meta| meta.get("hrns/usage/estimated_cost"))
                .and_then(serde_json::Value::as_bool)
                .unwrap_or(false);
            Some(Event::Usage {
                context: (usage.size > 0).then_some((usage.used, usage.size)),
                cost: usage.cost.map(|cost| {
                    if estimated && cost.currency == "USD" {
                        estimated_cost(cost.amount)
                    } else {
                        format!("{:.4} {}", cost.amount, cost.currency)
                    }
                }),
            })
        }
        SessionUpdate::ToolCall(call) => {
            let id = call.tool_call_id.to_string();
            let state = ToolState {
                title: call.title,
                status: call.status,
                kind: Some(call.kind),
                content: call.content,
                locations: call.locations,
                raw_input: call.raw_input,
                raw_output: call.raw_output,
            };
            let event = state.event(&id);
            tools.insert(id, state);
            Some(event)
        }
        SessionUpdate::ToolCallUpdate(update) => {
            let id = update.tool_call_id.to_string();
            let state = tools.entry(id.clone()).or_default();
            state.apply(&update);
            Some(state.event(&id))
        }
        _ => None,
    }
}

fn content_text(block: &ContentBlock) -> Option<Cow<'_, str>> {
    match block {
        ContentBlock::Text(text) => Some(text.text.as_str().into()),
        ContentBlock::Image(_) => Some("[image]".into()),
        ContentBlock::Audio(_) => Some("[audio]".into()),
        ContentBlock::ResourceLink(link) => Some(format!("[resource] {}", link.name).into()),
        ContentBlock::Resource(_) => Some("[embedded resource]".into()),
        _ => None,
    }
}

pub struct ToolDisplay {
    pub title: String,
    pub subject: String,
    pub status: ToolCallStatus,
    pub detail: String,
    pub output: String,
    pub preview_output: bool,
    pub shell: bool,
}

impl ToolDisplay {
    pub fn prepare_detail(&mut self) -> Option<usize> {
        for text in [&mut self.detail, &mut self.output] {
            text.retain(|ch| matches!(ch, '\n' | '\t') || !ch.is_control());
            *text = text.trim_end().replace('\t', "    ");
        }
        if self.output.is_empty() {
            return None;
        }
        if self.shell && self.detail.is_empty() {
            self.detail = std::mem::take(&mut self.output);
            return Some(0);
        }
        if !self.detail.is_empty() {
            self.detail.push('\n');
        }
        self.detail.push_str("Output\n");
        let start = self.detail.len();
        self.detail.push_str(&self.output);
        Some(start)
    }
}

#[derive(Clone, Default)]
pub(super) struct ToolState {
    title: String,
    status: ToolCallStatus,
    kind: Option<ToolKind>,
    content: Vec<ToolCallContent>,
    locations: Vec<ToolCallLocation>,
    raw_input: Option<serde_json::Value>,
    raw_output: Option<serde_json::Value>,
}

impl ToolState {
    pub(super) fn apply(&mut self, update: &agent_client_protocol::v1::schema::ToolCallUpdate) {
        if let Some(title) = update.fields.title.clone() {
            self.title = title;
        }
        if let Some(status) = update.fields.status {
            self.status = status;
        }
        if let Some(kind) = update.fields.kind {
            self.kind = Some(kind);
        }
        if let Some(locations) = &update.fields.locations {
            self.locations.clone_from(locations);
        }
        if let Some(content) = update.fields.content.clone() {
            self.content = content;
        }
        if let Some(raw_input) = update.fields.raw_input.clone() {
            self.raw_input = Some(raw_input);
        }
        if let Some(raw_output) = update.fields.raw_output.clone() {
            self.raw_output = Some(raw_output);
        }
    }

    fn event(&self, id: &str) -> Event {
        Event::Tool {
            id: id.to_owned(),
            display: self.display(),
        }
    }

    pub(super) fn display(&self) -> ToolDisplay {
        let decoded = self.raw_input.as_ref().map(tool_input::decode);
        let input = decoded.as_deref().unwrap_or(&serde_json::Value::Null);
        let has_diff = self
            .content
            .iter()
            .any(|part| matches!(part, ToolCallContent::Diff(_)));
        let presentation = tool_input::present(&self.title, input, has_diff);
        if presentation.is_none() && self.is_shell(input) {
            return self.shell_display(input);
        }
        let (title, subject, arguments) = if let Some(presentation) = presentation {
            (
                presentation.title.to_owned(),
                presentation.summary,
                presentation.detail,
            )
        } else {
            let (title, subject) = self.summary(input);
            let arguments = match input {
                serde_json::Value::Null => String::new(),
                serde_json::Value::Object(values) if values.is_empty() => String::new(),
                _ if input.get("truncated").and_then(serde_json::Value::as_bool) == Some(true) => {
                    tool_input::text(input, "preview").map_or_else(
                        || tool_input::pretty_value(input),
                        |preview| format!("… Input truncated\n{preview}"),
                    )
                }
                _ => tool_input::pretty_value(input),
            };
            (title, subject, arguments)
        };
        let (detail, output) = self.detail(arguments);
        ToolDisplay {
            title,
            subject,
            status: self.status,
            detail,
            output,
            preview_output: self.status == ToolCallStatus::Failed
                || matches!(self.kind, Some(ToolKind::Read | ToolKind::Search))
                || matches!(
                    self.title.as_str(),
                    "read_file"
                        | "read_text_file"
                        | "list_files"
                        | "list_directory"
                        | "glob"
                        | "grep"
                        | "search_files"
                        | "find_files"
                ),
            shell: false,
        }
    }

    fn is_shell(&self, input: &serde_json::Value) -> bool {
        matches!(
            self.title.as_str(),
            "shell" | "bash" | "terminal" | "execute" | "run_command"
        ) || (self.kind == Some(ToolKind::Execute)
            && (tool_input::text(input, "command").is_some() || self.title.starts_with("Run ")))
    }

    fn shell_display(&self, input: &serde_json::Value) -> ToolDisplay {
        let (_, fallback) = self.summary(input);
        let command = tool_input::text(input, "command").unwrap_or(&fallback);
        let mut arguments =
            tool_input::fields(input, &[("mode", "Mode"), ("timeout", "Timeout (seconds)")]);
        let summary = tool_input::preview(command);
        if summary != command {
            arguments = if arguments.is_empty() {
                format!("Command\n{command}")
            } else {
                format!("Command\n{command}\n{arguments}")
            };
        }
        let content: Vec<_> = self
            .content
            .iter()
            .filter_map(|part| match part {
                ToolCallContent::Content(content) => content_text(&content.content),
                ToolCallContent::Terminal(terminal) => {
                    Some(format!("Terminal {}", terminal.terminal_id).into())
                }
                _ => None,
            })
            .collect();
        let raw = self.raw_output.as_ref();
        let detail = if content.is_empty() {
            raw.map_or_else(String::new, tool_input::pretty_value)
        } else {
            content.join("\n")
        };
        let (output, exit_code) = shell_result(&detail);
        let status = if self.status == ToolCallStatus::Completed
            && exit_code.is_some_and(|code| code != 0)
        {
            ToolCallStatus::Failed
        } else {
            self.status
        };
        let title = match status {
            ToolCallStatus::Pending => "Queued",
            ToolCallStatus::InProgress => "Running",
            ToolCallStatus::Failed if exit_code.is_none() => "Failed",
            _ => "Ran",
        };
        let subject = match exit_code {
            Some(code) if code != 0 => format!("{summary}  (exit {code})"),
            _ => summary,
        };
        ToolDisplay {
            title: title.into(),
            subject,
            status,
            detail: arguments,
            output: output.to_owned(),
            preview_output: true,
            shell: true,
        }
    }

    fn summary(&self, arguments: &serde_json::Value) -> (String, String) {
        let verb = match self.kind {
            Some(ToolKind::Read) => "Read",
            Some(ToolKind::Edit) => "Edit",
            Some(ToolKind::Delete) => "Delete",
            Some(ToolKind::Move) => "Move",
            Some(ToolKind::Search) => "Search",
            Some(ToolKind::Execute) => "Run",
            Some(ToolKind::Think) => "Think",
            Some(ToolKind::Fetch) => "Fetch",
            Some(ToolKind::SwitchMode) => "Switch mode",
            _ => "",
        };
        let location = self.locations.first().map(|location| {
            let mut path = location.path.display().to_string();
            if let Some(line) = location.line {
                path.push_str(&format!(":{line}"));
            }
            if self.locations.len() > 1 {
                path.push_str(&format!(" (+{})", self.locations.len() - 1));
            }
            path
        });
        let input = [
            "command",
            "path",
            "file_path",
            "query",
            "pattern",
            "url",
            "target",
            "command_id",
        ]
        .into_iter()
        .find_map(|key| tool_input::text(arguments, key));
        let inferred = match self.title.as_str() {
            "read_file" | "read_text_file" => "Read",
            "write_file" | "write_text_file" | "edit_file" | "apply_patch" => "Edit",
            "list_files" | "list_directory" => "List",
            "grep" | "glob" | "search_files" | "find_files" => "Search",
            "shell" | "bash" | "terminal" | "execute" | "run_command" => "Run",
            _ => "",
        };
        let verb = if verb.is_empty() { inferred } else { verb };
        if verb.is_empty() {
            (
                if self.title.is_empty() {
                    "Tool call".into()
                } else {
                    self.title.clone()
                },
                location
                    .or_else(|| input.map(str::to_owned))
                    .unwrap_or_default(),
            )
        } else {
            let subject =
                if !self.title.is_empty() && inferred.is_empty() && self.title.contains(' ') {
                    let (leading, remainder) =
                        self.title.split_once(' ').unwrap_or((&self.title, ""));
                    if leading.eq_ignore_ascii_case(verb) {
                        remainder.to_owned()
                    } else {
                        self.title.clone()
                    }
                } else {
                    location
                        .or_else(|| input.map(str::to_owned))
                        .or_else(|| {
                            (inferred.is_empty()
                                && !self.title.is_empty()
                                && !self.title.eq_ignore_ascii_case(verb))
                            .then(|| self.title.clone())
                        })
                        .unwrap_or_default()
                };
            (verb.into(), subject)
        }
    }

    fn detail(&self, arguments: String) -> (String, String) {
        let mut changes = Vec::new();
        let mut output = Vec::new();
        for content in &self.content {
            match content {
                ToolCallContent::Content(content) => {
                    if let Some(text) = content_text(&content.content) {
                        output.push(text);
                    }
                }
                ToolCallContent::Diff(diff) => {
                    changes.push(diff.path.display().to_string());
                    if let Some(old) = &diff.old_text {
                        changes.extend(old.lines().map(|line| format!("- {line}")));
                    }
                    changes.extend(diff.new_text.lines().map(|line| format!("+ {line}")));
                }
                ToolCallContent::Terminal(terminal) => {
                    output.push(format!("Terminal {}", terminal.terminal_id).into());
                }
                _ => {}
            }
        }
        if output.is_empty()
            && let Some(raw) = self.raw_output.as_ref().filter(|v| !v.is_null())
        {
            output.push(tool_input::pretty_value(raw).into());
        }
        let detail = [
            (!arguments.is_empty()).then(|| format!("Arguments\n{arguments}")),
            (!changes.is_empty()).then(|| format!("Changes\n{}", changes.join("\n"))),
        ]
        .into_iter()
        .flatten()
        .collect::<Vec<_>>()
        .join("\n");
        (detail, output.join("\n"))
    }
}

// The workspace shell ends a completed result with its supervisor handles and exit record.
fn shell_result(text: &str) -> (&str, Option<i64>) {
    let text = text.trim_end();
    let Some((handles, exit)) = text.rsplit_once('\n') else {
        return (text, None);
    };
    let Some(exit) = serde_json::from_str::<serde_json::Value>(exit).ok() else {
        return (text, None);
    };
    let (output, trailer) = handles
        .rsplit_once("\nPID: ")
        .unwrap_or(("", handles.strip_prefix("PID: ").unwrap_or("")));
    let mut lines = trailer.lines();
    let pid = lines
        .next()
        .and_then(|line| line.split_once(" (supervisor; use `"))
        .and_then(|(pid, _)| pid.parse::<u64>().ok());
    if pid.is_some()
        && exit.get("pid").and_then(serde_json::Value::as_u64) == pid
        && lines
            .next()
            .is_some_and(|line| line.starts_with("Output: "))
        && lines
            .next()
            .is_some_and(|line| line.starts_with("Status: "))
        && lines.next().is_none()
        && let Some(code) = exit.get("exit_code").and_then(serde_json::Value::as_i64)
    {
        return (output, Some(code));
    }
    (text, None)
}

pub fn status_label(status: ToolCallStatus) -> &'static str {
    match status {
        ToolCallStatus::Pending => "pending",
        ToolCallStatus::InProgress => "running",
        ToolCallStatus::Completed => "done",
        ToolCallStatus::Failed => "failed",
        _ => "unknown",
    }
}
