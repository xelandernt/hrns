//! Agent-initiated structured forms, shared by both frontends.

use std::collections::BTreeMap;

use agent_client_protocol::v1::schema::{
    CreateElicitationRequest, CreateElicitationResponse, ElicitationAcceptAction,
    ElicitationAction, ElicitationContentValue, ElicitationMode, ElicitationPropertySchema,
    ElicitationSchema, MultiSelectItems,
};

#[derive(Clone, Debug)]
pub struct OptionItem {
    pub value: String,
    pub label: String,
    pub detail: Option<String>,
}

#[derive(Clone, Debug)]
pub enum FieldKind {
    Text {
        numeric: bool,
    },
    Choice {
        options: Vec<OptionItem>,
        multiple: bool,
    },
    Boolean,
}

#[derive(Clone, Debug)]
pub struct Field {
    pub key: String,
    pub title: String,
    pub description: Option<String>,
    pub kind: FieldKind,
    pub required: bool,
}

#[derive(Clone, Debug, Default)]
pub struct Form {
    pub message: String,
    pub fields: Vec<Field>,
}

pub struct FormState {
    pub form: Form,
    pub index: usize,
    pub draft: String,
    pub answers: BTreeMap<String, ElicitationContentValue>,
    selected: Vec<Vec<bool>>,
    pub error: Option<String>,
}

impl FormState {
    pub fn new(form: Form) -> Self {
        let selected = form
            .fields
            .iter()
            .map(|field| match &field.kind {
                FieldKind::Choice { options, .. } => vec![false; options.len()],
                _ => Vec::new(),
            })
            .collect();
        let mut state = Self {
            form,
            index: 0,
            draft: String::new(),
            answers: BTreeMap::new(),
            selected,
            error: None,
        };
        state.load_current();
        state
    }

    pub fn current(&self) -> Option<&Field> {
        self.form.fields.get(self.index)
    }

    pub fn is_boolean(&self) -> bool {
        matches!(
            self.current().map(|field| &field.kind),
            Some(FieldKind::Boolean)
        )
    }

    /// Commit the current row and either advance or finish the form.
    pub fn enter(&mut self) -> Option<BTreeMap<String, ElicitationContentValue>> {
        if self.index >= self.form.fields.len() {
            return self.submit();
        }
        if !matches!(
            self.current().map(|field| &field.kind),
            Some(FieldKind::Choice { .. } | FieldKind::Boolean)
        ) {
            self.commit_draft();
        }
        self.advance();
        if self.index >= self.form.fields.len() {
            self.submit()
        } else {
            None
        }
    }

    pub fn insert(&mut self, text: &str) {
        let numeric = matches!(
            self.current().map(|field| &field.kind),
            Some(FieldKind::Text { numeric: true })
        );
        if matches!(
            self.current().map(|field| &field.kind),
            Some(FieldKind::Text { .. })
        ) {
            for character in text.chars() {
                let allowed = if numeric {
                    character.is_ascii_digit()
                        || character == '.'
                        || character == '-'
                        || character == '+'
                } else {
                    !character.is_control() || character == '\n'
                };
                if allowed {
                    self.draft.push(character);
                }
            }
        }
        self.error = None;
    }

    pub fn backspace(&mut self) {
        self.draft.pop();
        self.error = None;
    }

    pub fn move_field(&mut self, forward: bool) {
        self.commit_draft();
        if forward {
            self.index = (self.index + 1).min(self.form.fields.len());
        } else {
            self.index = self.index.saturating_sub(1);
        }
        self.load_current();
        self.error = None;
    }

    /// Apply a digit pressed while the current field is a choice.
    /// Returns true when the key was consumed.
    pub fn choose(&mut self, digit: usize) -> bool {
        let Some(field) = self.current() else {
            return false;
        };
        let FieldKind::Choice { options, multiple } = field.kind.clone() else {
            return false;
        };
        if digit == 0 || digit > options.len() {
            return false;
        }
        let choice = digit - 1;
        let field_key = field.key.clone();
        if multiple {
            if let Some(row) = self.selected.get_mut(self.index) {
                row[choice] = !row[choice];
            }
            self.answers.remove(&field_key);
            let picked = self
                .selected
                .get(self.index)
                .map(|row| {
                    row.iter()
                        .enumerate()
                        .filter(|(_, active)| **active)
                        .map(|(index, _)| options[index].value.clone())
                        .collect::<Vec<_>>()
                })
                .unwrap_or_default();
            if !picked.is_empty() {
                self.answers
                    .insert(field_key, ElicitationContentValue::StringArray(picked));
            }
        } else {
            self.answers.insert(
                field_key,
                ElicitationContentValue::String(options[choice].value.clone()),
            );
            self.advance();
        }
        self.error = None;
        true
    }

    pub fn choose_boolean(&mut self, value: bool) -> bool {
        let Some(field) = self.current() else {
            return false;
        };
        if !matches!(field.kind, FieldKind::Boolean) {
            return false;
        }
        let key = field.key.clone();
        self.answers
            .insert(key, ElicitationContentValue::Boolean(value));
        self.advance();
        true
    }

    pub fn submit(&mut self) -> Option<BTreeMap<String, ElicitationContentValue>> {
        self.commit_draft();
        let missing = self
            .form
            .fields
            .iter()
            .filter(|field| field.required && !self.answers.contains_key(&field.key))
            .map(|field| field.title.clone())
            .collect::<Vec<_>>();
        if !missing.is_empty() {
            self.error = Some(format!("Answer {}", missing.join(", ")));
            return None;
        }
        if let Some(field) = self.current() {
            let title = field.title.clone();
            self.error = Some(format!("Answer {title}"));
            return None;
        }
        Some(std::mem::take(&mut self.answers))
    }

    fn advance(&mut self) {
        self.index = (self.index + 1).min(self.form.fields.len());
        self.load_current();
    }

    fn commit_draft(&mut self) {
        if self.draft.is_empty() {
            return;
        }
        let Some(field) = self.current() else {
            return;
        };
        if !matches!(field.kind, FieldKind::Text { .. }) {
            return;
        }
        let key = field.key.clone();
        let numeric = matches!(field.kind, FieldKind::Text { numeric: true });
        let draft = self.draft.trim().to_owned();
        if let Ok(value) = draft.parse::<i64>()
            && numeric
        {
            self.answers
                .insert(key, ElicitationContentValue::Integer(value));
        } else if let Ok(value) = draft.parse::<f64>() {
            if numeric {
                self.answers
                    .insert(key, ElicitationContentValue::Number(value));
            } else {
                self.answers
                    .insert(key, ElicitationContentValue::String(draft));
            }
        } else {
            self.answers
                .insert(key, ElicitationContentValue::String(draft));
        }
        self.draft.clear();
    }

    fn load_current(&mut self) {
        self.draft.clear();
        let Some(field) = self.current().cloned() else {
            return;
        };
        let key = field.key.clone();
        if let Some(ElicitationContentValue::String(text)) = self.answers.get(&key) {
            self.draft = text.clone();
        }
        if let Some(ElicitationContentValue::Integer(value)) = self.answers.get(&key) {
            self.draft = value.to_string();
        }
        if let Some(ElicitationContentValue::Number(value)) = self.answers.get(&key) {
            self.draft = value.to_string();
        }
        if let (
            FieldKind::Choice { options, .. },
            Some(ElicitationContentValue::StringArray(picked)),
        ) = (&field.kind, self.answers.get(&key))
            && let Some(row) = self.selected.get_mut(self.index)
        {
            for (index, option) in options.iter().enumerate() {
                row[index] = picked.contains(&option.value);
            }
        }
    }

    pub fn selections(&self) -> &[bool] {
        self.selected
            .get(self.index)
            .map(Vec::as_slice)
            .unwrap_or_default()
    }
}

impl Form {
    pub fn from_schema(message: String, schema: &ElicitationSchema) -> Self {
        let mut fields = Vec::new();
        for (key, property) in &schema.properties {
            let required = schema
                .required
                .as_ref()
                .is_some_and(|required| required.contains(key));
            fields.push(field_from_schema(key, property, required));
        }
        Self { message, fields }
    }
}

fn field_from_schema(key: &str, property: &ElicitationPropertySchema, required: bool) -> Field {
    let mut title = key.to_owned();
    let mut description = None;
    let kind = match property {
        ElicitationPropertySchema::String(schema) => {
            if let Some(name) = &schema.title {
                name.clone_into(&mut title);
            }
            description.clone_from(&schema.description);
            let options = schema
                .one_of
                .as_ref()
                .map(|options| {
                    options
                        .iter()
                        .map(|option| OptionItem {
                            value: option.value.clone(),
                            label: option.title.clone(),
                            detail: option.description.clone(),
                        })
                        .collect::<Vec<_>>()
                })
                .or_else(|| {
                    schema.enum_values.as_ref().map(|values| {
                        values
                            .iter()
                            .map(|value| OptionItem {
                                value: value.clone(),
                                label: value.clone(),
                                detail: None,
                            })
                            .collect()
                    })
                });
            match options {
                Some(options) if !options.is_empty() => FieldKind::Choice {
                    options,
                    multiple: false,
                },
                _ => FieldKind::Text { numeric: false },
            }
        }
        ElicitationPropertySchema::Integer(schema) => {
            if let Some(name) = &schema.title {
                name.clone_into(&mut title);
            }
            description.clone_from(&schema.description);
            FieldKind::Text { numeric: true }
        }
        ElicitationPropertySchema::Number(schema) => {
            if let Some(name) = &schema.title {
                name.clone_into(&mut title);
            }
            description.clone_from(&schema.description);
            FieldKind::Text { numeric: true }
        }
        ElicitationPropertySchema::Boolean(schema) => {
            if let Some(name) = &schema.title {
                name.clone_into(&mut title);
            }
            description.clone_from(&schema.description);
            FieldKind::Boolean
        }
        ElicitationPropertySchema::Array(schema) => {
            if let Some(name) = &schema.title {
                name.clone_into(&mut title);
            }
            description.clone_from(&schema.description);
            let options = match &schema.items {
                MultiSelectItems::Titled(items) => items
                    .options
                    .iter()
                    .map(|option| OptionItem {
                        value: option.value.clone(),
                        label: option.title.clone(),
                        detail: option.description.clone(),
                    })
                    .collect::<Vec<_>>(),
                MultiSelectItems::String(items) => items
                    .values
                    .iter()
                    .map(|value| OptionItem {
                        value: value.clone(),
                        label: value.clone(),
                        detail: None,
                    })
                    .collect(),
                _ => Vec::new(),
            };
            FieldKind::Choice {
                options,
                multiple: true,
            }
        }
        _ => {
            description = Some("This field needs a newer client".into());
            FieldKind::Text { numeric: false }
        }
    };
    Field {
        key: key.to_owned(),
        title,
        description,
        kind,
        required,
    }
}

pub fn accept(answers: BTreeMap<String, ElicitationContentValue>) -> CreateElicitationResponse {
    CreateElicitationResponse::new(ElicitationAction::Accept(
        ElicitationAcceptAction::new().content(answers),
    ))
}

pub fn decline() -> CreateElicitationResponse {
    CreateElicitationResponse::new(ElicitationAction::Decline)
}

/// Split a request into a displayable form, if the client can render its mode.
pub fn display(request: &CreateElicitationRequest) -> Option<Form> {
    match &request.mode {
        ElicitationMode::Form(form) => Some(Form::from_schema(
            request.message.clone(),
            &form.requested_schema,
        )),
        _ => None,
    }
}
