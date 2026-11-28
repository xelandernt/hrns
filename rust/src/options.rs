use agent_client_protocol::v1::schema::{
    SessionConfigKind, SessionConfigOption, SessionConfigOptionCategory, SessionConfigOptionValue,
    SessionConfigSelectOption, SessionConfigSelectOptions,
};

pub fn summary(option: &SessionConfigOption) -> String {
    match &option.kind {
        SessionConfigKind::Select(select) => {
            let values = choices(option);
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
        SessionConfigKind::Select(_) => {
            let values = choices(option);
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

pub fn permissions_option(options: &[SessionConfigOption]) -> Option<&SessionConfigOption> {
    options.iter().find(|option| {
        option.id.0.as_ref() == "permissions" && matches!(option.kind, SessionConfigKind::Select(_))
    })
}

pub fn permissions_value(options: &[SessionConfigOption]) -> Option<&str> {
    let option = permissions_option(options)?;
    let SessionConfigKind::Select(select) = &option.kind else {
        return None;
    };
    Some(select.current_value.0.as_ref())
}

pub fn selected_model(options: &[SessionConfigOption]) -> Option<&SessionConfigSelectOption> {
    let option = options
        .iter()
        .find(|option| matches_category(option, &SessionConfigOptionCategory::Model))?;
    let SessionConfigKind::Select(select) = &option.kind else {
        return None;
    };
    choices(option)
        .into_iter()
        .find(|choice| choice.value == select.current_value)
}

pub fn model_provider(choice: &SessionConfigSelectOption) -> Option<&str> {
    choice.meta.as_ref()?.get("providerId")?.as_str()
}

pub fn choices(option: &SessionConfigOption) -> Vec<&SessionConfigSelectOption> {
    let SessionConfigKind::Select(select) = &option.kind else {
        return Vec::new();
    };
    match &select.options {
        SessionConfigSelectOptions::Ungrouped(values) => values.iter().collect(),
        SessionConfigSelectOptions::Grouped(groups) => groups
            .iter()
            .flat_map(|group| group.options.iter())
            .collect(),
        _ => Vec::new(),
    }
}
