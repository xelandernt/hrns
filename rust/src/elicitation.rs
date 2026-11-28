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
    Text(TextKind),
    Choice {
        options: Vec<OptionItem>,
        multiple: bool,
    },
    Boolean,
}

#[derive(Clone, Copy, Debug)]
pub enum TextKind {
    String,
    Integer,
    Number,
}

#[derive(Clone, Debug)]
pub struct Field {
    pub key: String,
    pub title: String,
    pub description: Option<String>,
    pub kind: FieldKind,
    pub required: bool,
    pub custom_key: Option<String>,
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
    pub choice: usize,
    pub editing_custom: bool,
    pub error: Option<String>,
}

impl FormState {
    pub fn new(form: Form) -> Self {
        Self {
            form,
            index: 0,
            draft: String::new(),
            answers: BTreeMap::new(),
            choice: 0,
            editing_custom: false,
            error: None,
        }
    }

    pub fn current(&self) -> Option<&Field> {
        self.form.fields.get(self.index)
    }

    pub fn enter(&mut self) -> Option<BTreeMap<String, ElicitationContentValue>> {
        let Some(field) = self.current() else {
            return self.submit();
        };
        if self.editing_custom && self.draft.trim().is_empty() {
            self.error = Some("Type your answer before continuing.".into());
            return None;
        }
        match &field.kind {
            FieldKind::Choice { options, .. }
                if field.custom_key.is_some()
                    && self.choice == options.len()
                    && !self.editing_custom =>
            {
                self.editing_custom = true;
                self.load_text();
                return None;
            }
            FieldKind::Choice {
                options,
                multiple: false,
            } => {
                if let Some(option) = options.get(self.choice) {
                    let key = field.key.clone();
                    let value = ElicitationContentValue::String(option.value.clone());
                    self.clear_custom();
                    self.answers.insert(key, value);
                }
            }
            FieldKind::Boolean => {
                self.answers.insert(
                    field.key.clone(),
                    ElicitationContentValue::Boolean(self.choice == 0),
                );
            }
            _ => {}
        }
        if !self.commit_text() {
            return None;
        }
        if let Some(field) = self.current().filter(|field| self.missing(field)) {
            self.error = Some(format!("Answer {} before continuing.", field.title));
            return None;
        }
        if self.form.fields.len() <= 1 {
            self.submit()
        } else {
            self.open(self.index + 1);
            None
        }
    }

    pub fn insert(&mut self, text: &str) {
        if self.editing_custom
            || self
                .current()
                .is_some_and(|field| matches!(field.kind, FieldKind::Text(_)))
        {
            self.draft
                .extend(text.chars().filter(|character| !character.is_control()));
            self.error = None;
        }
    }

    pub fn backspace(&mut self) {
        self.draft.pop();
        self.error = None;
    }

    pub fn move_field(&mut self, forward: bool) {
        if !self.commit_text() {
            return;
        }
        if forward && self.form.fields.len() <= 1 {
            return;
        }
        let index = if forward {
            (self.index + 1).min(self.form.fields.len())
        } else {
            self.index.saturating_sub(1)
        };
        self.open(index);
    }

    pub fn move_choice(&mut self, forward: bool) {
        if self.editing_custom {
            return;
        }
        let count = self.current().map_or(0, |field| match &field.kind {
            FieldKind::Choice { options, .. } => {
                options.len() + usize::from(field.custom_key.is_some())
            }
            FieldKind::Boolean => 2,
            FieldKind::Text(_) => 0,
        });
        self.choice = if forward {
            (self.choice + 1).min(count.saturating_sub(1))
        } else {
            self.choice.saturating_sub(1)
        };
        self.error = None;
    }

    pub fn choice_key(&mut self, character: char) -> bool {
        if self.editing_custom {
            return false;
        }
        let Some(field) = self.current() else {
            return false;
        };
        match &field.kind {
            FieldKind::Boolean => {
                self.choice = match character {
                    'y' | 'Y' => 0,
                    'n' | 'N' => 1,
                    _ => return false,
                };
                true
            }
            FieldKind::Choice { options, multiple } => {
                let multiple = *multiple;
                if character != ' ' {
                    let Some(index) = character
                        .to_digit(10)
                        .and_then(|digit| digit.checked_sub(1))
                    else {
                        return false;
                    };
                    let index = index as usize;
                    if index >= options.len() + usize::from(field.custom_key.is_some()) {
                        return false;
                    }
                    self.choice = index;
                }
                if multiple {
                    self.toggle_choice();
                }
                self.error = None;
                true
            }
            FieldKind::Text(_) => false,
        }
    }

    fn toggle_choice(&mut self) {
        let Some(field) = self.current() else {
            return;
        };
        let FieldKind::Choice {
            options,
            multiple: true,
        } = &field.kind
        else {
            return;
        };
        let Some(option) = options.get(self.choice) else {
            return;
        };
        let key = field.key.clone();
        let value = option.value.clone();
        self.clear_custom();
        let entry = self
            .answers
            .entry(key.clone())
            .or_insert_with(|| ElicitationContentValue::StringArray(Vec::new()));
        if let ElicitationContentValue::StringArray(values) = entry {
            if let Some(index) = values.iter().position(|item| *item == value) {
                values.remove(index);
            } else {
                values.push(value);
            }
            if values.is_empty() {
                self.answers.remove(&key);
            }
        }
    }

    fn clear_custom(&mut self) {
        if let Some(key) = self.current().and_then(|field| field.custom_key.clone()) {
            self.answers.remove(&key);
        }
    }

    fn missing(&self, field: &Field) -> bool {
        (field.required || field.custom_key.is_some()) && self.answer(field).is_none()
    }

    pub fn answer(&self, field: &Field) -> Option<&ElicitationContentValue> {
        field
            .custom_key
            .as_ref()
            .and_then(|key| self.answers.get(key))
            .or_else(|| self.answers.get(&field.key))
    }

    fn submit(&mut self) -> Option<BTreeMap<String, ElicitationContentValue>> {
        if let Some(index) = self
            .form
            .fields
            .iter()
            .position(|field| self.missing(field))
        {
            self.open(index);
            self.error = Some(format!(
                "Answer {} before submitting.",
                self.form.fields[index].title
            ));
            return None;
        }
        Some(std::mem::take(&mut self.answers))
    }

    fn commit_text(&mut self) -> bool {
        let Some(field) = self.current() else {
            return true;
        };
        let (key, kind) = match &field.kind {
            _ if self.editing_custom => match &field.custom_key {
                Some(key) => (key.clone(), TextKind::String),
                None => return true,
            },
            FieldKind::Text(kind) => (field.key.clone(), *kind),
            _ => return true,
        };
        let text = self.draft.trim();
        if text.is_empty() {
            self.answers.remove(&key);
            return true;
        }
        let value = match kind {
            TextKind::String => Some(ElicitationContentValue::String(text.into())),
            TextKind::Integer => text
                .parse::<i64>()
                .ok()
                .map(ElicitationContentValue::Integer),
            TextKind::Number => text
                .parse::<f64>()
                .ok()
                .filter(|number| number.is_finite())
                .map(ElicitationContentValue::Number),
        };
        let Some(value) = value else {
            self.error = Some("Enter a valid number.".into());
            return false;
        };
        if self.editing_custom {
            let choice_key = field.key.clone();
            self.answers.remove(&choice_key);
        }
        self.answers.insert(key, value);
        true
    }

    fn open(&mut self, index: usize) {
        self.index = index;
        self.error = None;
        self.editing_custom = false;
        self.choice = self.current().map_or(0, |field| {
            if let FieldKind::Choice { options, .. } = &field.kind
                && field
                    .custom_key
                    .as_ref()
                    .is_some_and(|key| self.answers.contains_key(key))
            {
                return options.len();
            }
            match (&field.kind, self.answer(field)) {
                (
                    FieldKind::Choice { options, .. },
                    Some(ElicitationContentValue::String(value)),
                ) => options
                    .iter()
                    .position(|option| &option.value == value)
                    .unwrap_or(options.len()),
                (FieldKind::Boolean, Some(ElicitationContentValue::Boolean(false))) => 1,
                _ => 0,
            }
        });
        self.load_text();
    }

    fn load_text(&mut self) {
        self.draft = self
            .current()
            .and_then(|field| {
                let key = if self.editing_custom {
                    field.custom_key.as_ref()?
                } else {
                    &field.key
                };
                match self.answers.get(key)? {
                    ElicitationContentValue::String(value) => Some(value.clone()),
                    ElicitationContentValue::Integer(value) => Some(value.to_string()),
                    ElicitationContentValue::Number(value) => Some(value.to_string()),
                    _ => None,
                }
            })
            .unwrap_or_default();
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
        let companions = fields
            .iter()
            .filter_map(|field| {
                let choice = field.key.strip_suffix("_text")?;
                matches!(field.kind, FieldKind::Text(TextKind::String))
                    .then(|| (choice.to_owned(), field.key.clone()))
            })
            .collect::<Vec<_>>();
        for (choice, key) in companions {
            if let Some(field) = fields
                .iter_mut()
                .find(|field| field.key == choice && matches!(field.kind, FieldKind::Choice { .. }))
            {
                field.custom_key = Some(key.clone());
                fields.retain(|field| field.key != key);
            }
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
                _ => FieldKind::Text(TextKind::String),
            }
        }
        ElicitationPropertySchema::Integer(schema) => {
            if let Some(name) = &schema.title {
                name.clone_into(&mut title);
            }
            description.clone_from(&schema.description);
            FieldKind::Text(TextKind::Integer)
        }
        ElicitationPropertySchema::Number(schema) => {
            if let Some(name) = &schema.title {
                name.clone_into(&mut title);
            }
            description.clone_from(&schema.description);
            FieldKind::Text(TextKind::Number)
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
            FieldKind::Text(TextKind::String)
        }
    };
    Field {
        key: key.to_owned(),
        title,
        description,
        kind,
        required,
        custom_key: None,
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
