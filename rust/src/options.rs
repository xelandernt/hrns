use agent_client_protocol::v1::schema::{
    SessionConfigKind, SessionConfigOption, SessionConfigOptionCategory, SessionConfigOptionValue,
    SessionConfigSelectOptions,
};

pub fn summary(option: &SessionConfigOption) -> String {
    match &option.kind {
        SessionConfigKind::Select(select) => {
            let values = match &select.options {
                SessionConfigSelectOptions::Ungrouped(values) => values.iter().collect(),
                SessionConfigSelectOptions::Grouped(groups) => groups
                    .iter()
                    .flat_map(|group| group.options.iter())
                    .collect::<Vec<_>>(),
                _ => Vec::new(),
            };
            if values.is_empty() {
                format!("{} · {}", option.name, select.current_value)
            } else {
                let choices = values
                    .into_iter()
                    .map(|value| value.value.to_string())
                    .collect::<Vec<_>>()
                    .join(", ");
                format!("{} · {}\n{choices}", option.name, select.current_value)
            }
        }
        SessionConfigKind::Boolean(toggle) => format!("{} · {}", option.name, toggle.current_value),
        _ => option.name.clone(),
    }
}

pub fn value(option: &SessionConfigOption, input: &str) -> Option<SessionConfigOptionValue> {
    match &option.kind {
        SessionConfigKind::Select(select) => {
            let values = match &select.options {
                SessionConfigSelectOptions::Ungrouped(values) => values.iter().collect(),
                SessionConfigSelectOptions::Grouped(groups) => groups
                    .iter()
                    .flat_map(|group| group.options.iter())
                    .collect::<Vec<_>>(),
                _ => Vec::new(),
            };
            let known =
                values.is_empty() || values.iter().any(|value| value.value.to_string() == input);
            known.then(|| SessionConfigOptionValue::value_id(input.to_owned()))
        }
        SessionConfigKind::Boolean(_) => match input {
            "true" | "on" => Some(SessionConfigOptionValue::boolean(true)),
            "false" | "off" => Some(SessionConfigOptionValue::boolean(false)),
            _ => None,
        },
        _ => None,
    }
}

pub fn matches_category(
    option: &SessionConfigOption,
    category: &SessionConfigOptionCategory,
) -> bool {
    match &option.category {
        Some(actual) => actual == category,
        None => match category {
            SessionConfigOptionCategory::Model => option.id.0.as_ref() == "model",
            SessionConfigOptionCategory::ThoughtLevel => option.id.0.as_ref() == "effort",
            _ => false,
        },
    }
}
