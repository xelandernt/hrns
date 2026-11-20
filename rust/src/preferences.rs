use serde::{Deserialize, Serialize};

#[cfg(target_family = "wasm")]
const WASM_KEY: &str = "hrns.tui.preferences.v1";

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ApprovalMode {
    #[default]
    Ask,
    Reads,
    All,
}

impl ApprovalMode {
    pub fn parse(value: &str) -> Option<Self> {
        match value {
            "ask" => Some(Self::Ask),
            "reads" => Some(Self::Reads),
            "all" => Some(Self::All),
            _ => None,
        }
    }

    pub fn label(self) -> &'static str {
        match self {
            Self::Ask => "ask",
            Self::Reads => "reads",
            Self::All => "all",
        }
    }
}

#[derive(Clone, Debug, Default, Serialize, Deserialize)]
#[serde(default)]
pub struct Preferences {
    pub sidebar: bool,
    pub sidebar_width: u16,
    pub expand_tools: bool,
    pub show_thoughts: bool,
    pub theme: Option<String>,
    pub permission_mode: Option<ApprovalMode>,
}

impl Preferences {
    pub fn sidebar_width(&self, available: u16) -> u16 {
        let width = if self.sidebar_width == 0 {
            40
        } else {
            self.sidebar_width
        };
        width.clamp(24, 76).min(available / 2)
    }
}

#[cfg(not(target_family = "wasm"))]
pub fn load() -> Preferences {
    std::fs::read_to_string(path())
        .ok()
        .and_then(|text| serde_json::from_str(&text).ok())
        .unwrap_or_default()
}

#[cfg(not(target_family = "wasm"))]
pub fn save(preferences: &Preferences) {
    let path = path();
    let Some(parent) = path.parent() else {
        return;
    };
    if std::fs::create_dir_all(parent).is_err() {
        return;
    }
    if let Ok(text) = serde_json::to_string_pretty(preferences) {
        let _ = std::fs::write(path, text);
    }
}

#[cfg(not(target_family = "wasm"))]
fn path() -> std::path::PathBuf {
    use std::path::PathBuf;

    if let Some(path) = std::env::var_os("HRNS_TUI_STATE") {
        return PathBuf::from(path);
    }
    let base = std::env::var_os("XDG_STATE_HOME")
        .map(PathBuf::from)
        .or_else(|| std::env::var_os("HOME").map(|home| PathBuf::from(home).join(".local/state")))
        .unwrap_or_else(std::env::temp_dir);
    base.join("hrns").join("tui.json")
}

#[cfg(target_family = "wasm")]
pub fn load() -> Preferences {
    storage()
        .and_then(|storage| storage.get_item(WASM_KEY).ok().flatten())
        .and_then(|text| serde_json::from_str(&text).ok())
        .unwrap_or_default()
}

#[cfg(target_family = "wasm")]
pub fn save(preferences: &Preferences) {
    let Some(storage) = storage() else {
        return;
    };
    if let Ok(text) = serde_json::to_string(preferences) {
        let _ = storage.set_item(WASM_KEY, &text);
    }
}

#[cfg(target_family = "wasm")]
fn storage() -> Option<web_sys::Storage> {
    web_sys::window()?.local_storage().ok().flatten()
}
