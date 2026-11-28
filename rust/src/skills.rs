use std::collections::HashSet;
use std::ops::Range;
use std::path::Path;

use serde::Deserialize;
use serde_json::{Map, Value, json};

use crate::composer::prompt_tokens;

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

pub fn catalog(meta: &Map<String, Value>) -> Vec<Skill> {
    meta.get(META_KEY)
        .and_then(|value| serde_json::from_value(value.clone()).ok())
        .unwrap_or_default()
}

/// Byte ranges of user-invocable `$id` references in the draft.
pub fn mention_ranges<'a>(
    text: &'a str,
    skills: &'a [Skill],
) -> impl Iterator<Item = Range<usize>> + 'a {
    prompt_tokens(text).filter_map(move |token| {
        let word = text[token.clone()].strip_prefix('$')?;
        let length = word
            .find(|ch: char| !ch.is_ascii_alphanumeric() && !matches!(ch, '_' | '-'))
            .unwrap_or(word.len());
        let id = &word[..length];
        skills
            .iter()
            .any(|skill| skill.user_invocable && skill.id == id)
            .then_some(token.start..token.start + 1 + length)
    })
}

pub fn prompt_meta(text: &str, skills: &[Skill]) -> Option<Value> {
    let mut seen = HashSet::new();
    let mut previous_end = 0;
    let mut characters = 0;
    let mentions: Vec<_> = mention_ranges(text, skills)
        .filter_map(|range| {
            let mention = &text[range.clone()];
            let id = &mention[1..];
            if !seen.insert(id) {
                return None;
            }
            let start = characters + text[previous_end..range.start].chars().count();
            let end = start + mention.len();
            previous_end = range.end;
            characters = end;
            Some(json!({
                "id": id,
                "mention": { "start": start, "end": end, "text": mention },
            }))
        })
        .collect();
    (!mentions.is_empty()).then_some(Value::Array(mentions))
}
