use std::path::Path;

use serde::Deserialize;
use serde_json::{Map, Value, json};

pub const META_KEY: &str = "hrns/skills";

#[derive(Clone, Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct Skill {
    pub id: String,
    #[serde(default)]
    pub name: String,
    #[serde(default)]
    pub description: String,
    #[serde(default = "user_invocable")]
    pub user_invocable: bool,
    pub path: Option<String>,
}

const fn user_invocable() -> bool {
    true
}

impl Skill {
    pub fn label(&self) -> &str {
        if self.name.is_empty() {
            &self.id
        } else {
            &self.name
        }
    }

    pub fn directory(&self) -> Option<&str> {
        Path::new(self.path.as_deref()?)
            .parent()?
            .to_str()
            .filter(|directory| !directory.is_empty())
    }
}

pub struct Mention {
    pub id: String,
    pub start: usize,
    pub end: usize,
    pub text: String,
}

pub fn catalog(meta: &Map<String, Value>) -> Vec<Skill> {
    meta.get(META_KEY)
        .and_then(|value| serde_json::from_value(value.clone()).ok())
        .unwrap_or_default()
}

/// The `$id` references a draft names, in first-seen order.
pub fn mentions(text: &str, skills: &[Skill]) -> Vec<Mention> {
    let invocable: Vec<&Skill> = skills.iter().filter(|skill| skill.user_invocable).collect();
    let mut found = Vec::new();
    let mut seen: Vec<&str> = Vec::new();
    let mut characters = 0;
    let mut index = 0;
    while index < text.len() {
        let character = text[index..].chars().next().unwrap_or_default();
        if character == '$'
            && (index == 0
                || text[..index]
                    .chars()
                    .next_back()
                    .is_some_and(char::is_whitespace))
        {
            let rest = &text[index + 1..];
            let id: String = rest
                .chars()
                .take_while(|ch| ch.is_ascii_alphanumeric() || matches!(ch, '_' | '-'))
                .collect();
            if let Some(skill) = invocable.iter().find(|skill| skill.id == id)
                && !seen.contains(&skill.id.as_str())
            {
                seen.push(&skill.id);
                found.push(Mention {
                    id: skill.id.clone(),
                    start: characters,
                    end: characters + 1 + id.chars().count(),
                    text: format!("${id}"),
                });
            }
            characters += 1 + id.chars().count();
            index += 1 + id.len();
            continue;
        }
        characters += 1;
        index += character.len_utf8();
    }
    found
}

pub fn prompt_meta(text: &str, skills: &[Skill]) -> Option<Value> {
    let mentions = mentions(text, skills);
    if mentions.is_empty() {
        return None;
    }
    Some(Value::Array(
        mentions
            .into_iter()
            .map(|mention| {
                json!({
                    "id": mention.id,
                    "mention": {
                        "start": mention.start,
                        "end": mention.end,
                        "text": mention.text,
                    },
                })
            })
            .collect(),
    ))
}
