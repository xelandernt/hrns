//! Argument layouts for the built-in coding agent's tools.

use std::borrow::Cow;

use serde_json::Value;

pub struct ToolInput {
    pub title: &'static str,
    pub summary: String,
    pub detail: String,
}

pub fn decode(value: &Value) -> Cow<'_, Value> {
    if let Some(text) = value.as_str()
        && let Ok(parsed @ Value::Object(_)) = serde_json::from_str(text)
    {
        Cow::Owned(parsed)
    } else {
        Cow::Borrowed(value)
    }
}

pub fn present(name: &str, input: &Value, has_diff: bool) -> Option<ToolInput> {
    let (title, summary, detail) = match name {
        "read_file" => {
            let path = text(input, "path")?;
            let summary = [
                Some(path.to_owned()),
                input
                    .get("offset")
                    .filter(|v| !v.is_null())
                    .map(|v| format!("offset {v}")),
                input
                    .get("limit")
                    .filter(|v| !v.is_null())
                    .map(|v| format!("limit {v}")),
            ]
            .into_iter()
            .flatten()
            .collect::<Vec<_>>()
            .join(" · ");
            (
                "Read",
                summary,
                fields(
                    input,
                    &[("path", "Path"), ("offset", "Offset"), ("limit", "Limit")],
                ),
            )
        }
        "list_files" => {
            let path = text(input, "path").unwrap_or(".");
            let glob = text(input, "glob");
            let summary = glob.map_or_else(|| path.to_owned(), |glob| format!("{path} · {glob}"));
            (
                "List",
                summary,
                fields(input, &[("path", "Path"), ("glob", "Glob")]),
            )
        }
        "grep" => {
            let path = text(input, "path").unwrap_or(".");
            let pattern = text(input, "pattern")?;
            let filters = [
                ("pattern", "Pattern"),
                ("path", "Path"),
                ("glob", "Glob"),
                ("file_type", "File type"),
                ("ignore_case", "Ignore case"),
                ("literal", "Literal"),
                ("context", "Context lines"),
            ];
            (
                "Search",
                format!("{pattern:?} in {path}"),
                fields(input, &filters),
            )
        }
        "write_file" | "edit_file" => {
            let path = text(input, "path")?;
            let (title, summary) = if name == "write_file" {
                let count = text(input, "content")?.lines().count();
                (
                    "Write",
                    format!("{path} · {}", count_label(count, "line", "lines")),
                )
            } else {
                let count = input
                    .get("replacements")
                    .and_then(Value::as_array)
                    .map_or(1, Vec::len);
                (
                    "Edit",
                    format!(
                        "{path} · {}",
                        count_label(count, "replacement", "replacements")
                    ),
                )
            };
            let mut detail = fields(
                input,
                &[("path", "Path"), ("expected_hash", "Expected hash")],
            );
            if !has_diff {
                append_file_input(&mut detail, input, name == "write_file");
            }
            (title, summary, detail)
        }
        "apply_changes" => {
            let changes = input.get("changes")?.as_array()?;
            let mut detail = String::new();
            for change in changes {
                let write = text(change, "kind") == Some("write");
                let path = text(change, "path").unwrap_or("Unknown path");
                let title = if write { "Write" } else { "Edit" };
                add(&mut detail, &format!("{title} {path}"));
                add(
                    &mut detail,
                    &fields(change, &[("expected_hash", "Expected hash")]),
                );
                append_file_input(&mut detail, change, write);
            }
            let first_path = changes.first().and_then(|change| text(change, "path"));
            let count = count_label(changes.len(), "file change", "file changes");
            let summary =
                first_path.map_or_else(|| count.clone(), |path| format!("{count} · {path}"));
            ("Apply", summary, detail)
        }
        "delegate_task" => {
            let agent = text(input, "agent_name")?;
            let task = text(input, "task")?;
            (
                "Delegate",
                format!("{agent} · {task}"),
                fields(input, &[("agent_name", "Agent"), ("task", "Task")]),
            )
        }
        "ask_user_question" => {
            let questions = input.get("questions")?.as_array()?;
            let mut detail = String::new();
            for question in questions {
                let title = text(question, "header").unwrap_or("Question");
                let question_text = text(question, "question").unwrap_or_default();
                let select = if question.get("multi_select").and_then(Value::as_bool) == Some(true)
                {
                    "multiple choices"
                } else {
                    "one choice"
                };
                add(&mut detail, &format!("{title} · {select}\n{question_text}"));
                if let Some(options) = question.get("options").and_then(Value::as_array) {
                    for option in options {
                        let label = text(option, "label").unwrap_or_default();
                        let line = text(option, "description").map_or_else(
                            || format!("  • {label}"),
                            |description| format!("  • {label} · {description}"),
                        );
                        add(&mut detail, &line);
                    }
                }
            }
            let count = count_label(questions.len(), "question", "questions");
            let first = questions
                .first()
                .and_then(|question| text(question, "header"));
            let summary =
                first.map_or_else(|| count.clone(), |header| format!("{count} · {header}"));
            ("Ask", summary, detail)
        }
        "read_todos" => ("Read", "todos".into(), String::new()),
        "write_todos" => {
            let items = input.get("items")?.as_array()?;
            let mut detail = String::new();
            for item in items {
                let content = text(item, "content").unwrap_or_default();
                let status = match text(item, "status") {
                    Some("completed") => "✓",
                    Some("in_progress") => "◐",
                    _ => "○",
                };
                let priority = text(item, "priority").unwrap_or("medium");
                add(
                    &mut detail,
                    &format!("{status} {content} · {priority} priority"),
                );
            }
            let summary = if items.is_empty() {
                "todos · clear list".into()
            } else {
                count_label(items.len(), "todo", "todos")
            };
            ("Update", summary, detail)
        }
        "load_capability" => {
            let id = text(input, "id")?;
            (
                "Load",
                format!("capability {id}"),
                fields(input, &[("id", "Capability")]),
            )
        }
        "read_skill_resource" | "run_skill_script" => {
            let skill = text(input, "skill_name")?;
            let script = name == "run_skill_script";
            let (title, field, label) = if script {
                ("Run skill script", "script_name", "Script")
            } else {
                ("Read skill resource", "resource_name", "Resource")
            };
            let target = text(input, field)?;
            (
                title,
                format!("{skill} / {target}"),
                fields(
                    input,
                    &[
                        ("skill_name", "Skill"),
                        (field, label),
                        ("args", "Arguments"),
                    ],
                ),
            )
        }
        "search_tools" => {
            let queries = input.get("queries")?.as_array()?;
            let detail = queries
                .iter()
                .filter_map(Value::as_str)
                .collect::<Vec<_>>()
                .join("\n");
            (
                "Find",
                format!("tools · {}", detail.lines().next().unwrap_or("")),
                detail,
            )
        }
        _ => return None,
    };
    Some(ToolInput {
        title,
        summary: preview(&summary),
        detail,
    })
}

pub fn text<'a>(input: &'a Value, key: &str) -> Option<&'a str> {
    input.get(key)?.as_str()
}

pub fn fields(input: &Value, keys: &[(&str, &str)]) -> String {
    keys.iter()
        .filter_map(|(key, label)| {
            let field = input.get(key).filter(|field| !field.is_null())?;
            Some(format!("{label}: {}", pretty_value(field)))
        })
        .collect::<Vec<_>>()
        .join("\n")
}

pub fn pretty_value(value: &Value) -> String {
    value.as_str().map(str::to_owned).unwrap_or_else(|| {
        serde_json::to_string_pretty(value).unwrap_or_else(|_| value.to_string())
    })
}

pub fn preview(text: &str) -> String {
    let mut chars = text
        .lines()
        .map(str::trim)
        .collect::<Vec<_>>()
        .join(" ")
        .chars()
        .collect::<Vec<_>>();
    if chars.len() > 110 {
        chars.truncate(109);
        chars.push('…');
    }
    chars.into_iter().collect()
}

fn count_label(count: usize, singular: &str, plural: &str) -> String {
    format!("{count} {}", if count == 1 { singular } else { plural })
}

fn append_file_input(detail: &mut String, input: &Value, write: bool) {
    if write {
        if let Some(content) = text(input, "content") {
            add(detail, "Contents");
            add(detail, content);
        }
        return;
    }
    if let Some(replacements) = input.get("replacements").and_then(Value::as_array) {
        for (index, replacement) in replacements.iter().enumerate() {
            add(detail, &format!("Replacement {}", index + 1));
            append_replacement(detail, replacement);
        }
    } else {
        append_replacement(detail, input);
    }
}

fn append_replacement(detail: &mut String, input: &Value) {
    for (field, prefix) in [("old_text", "- "), ("new_text", "+ ")] {
        if let Some(value) = text(input, field) {
            for line in value.lines() {
                add(detail, &format!("{prefix}{line}"));
            }
        }
    }
}

fn add(detail: &mut String, line: &str) {
    if line.is_empty() {
        return;
    }
    if !detail.is_empty() {
        detail.push('\n');
    }
    detail.push_str(line);
}
