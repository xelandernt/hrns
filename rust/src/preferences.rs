use base64::{Engine, engine::general_purpose::URL_SAFE_NO_PAD};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};

#[cfg(target_family = "wasm")]
const WASM_KEY: &str = "hrns.tui.preferences.v1";

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub enum ApprovalMode {
    #[default]
    Ask,
    Reads,
    All,
}

pub struct ApprovalKey(String);

impl ApprovalKey {
    pub fn new(scope: &str, session: &str) -> Self {
        let mut digest = Sha256::new();
        digest.update((scope.len() as u64).to_le_bytes());
        digest.update(scope);
        digest.update(session);
        Self(URL_SAFE_NO_PAD.encode(digest.finalize()))
    }

    pub fn load(&self) -> ApprovalMode {
        self.read()
            .as_deref()
            .and_then(ApprovalMode::parse)
            .unwrap_or_default()
    }

    #[cfg(not(target_family = "wasm"))]
    fn read(&self) -> Option<String> {
        std::fs::read_to_string(self.path()?).ok()
    }

    #[cfg(not(target_family = "wasm"))]
    pub fn save(&self, mode: ApprovalMode) -> Result<(), String> {
        use std::io::Write;

        let path = self.path().ok_or("Missing device state directory")?;
        let dir = path.parent().ok_or("Missing approval state directory")?;
        std::fs::create_dir_all(dir).map_err(|error| error.to_string())?;
        let mut file = tempfile::NamedTempFile::new_in(dir).map_err(|error| error.to_string())?;
        file.write_all(mode.label().as_bytes())
            .map_err(|error| error.to_string())?;
        file.persist(path).map_err(|error| error.to_string())?;
        Ok(())
    }

    #[cfg(not(target_family = "wasm"))]
    fn path(&self) -> Option<std::path::PathBuf> {
        let mut directory = device_state_path()?.into_os_string();
        directory.push(".approvals");
        Some(std::path::PathBuf::from(directory).join(&self.0))
    }

    #[cfg(target_family = "wasm")]
    fn read(&self) -> Option<String> {
        storage()?.get_item(&self.storage_key()).ok().flatten()
    }

    #[cfg(target_family = "wasm")]
    pub fn save(&self, mode: ApprovalMode) -> Result<(), String> {
        storage()
            .ok_or("Browser storage unavailable")?
            .set_item(&self.storage_key(), mode.label())
            .map_err(|_| "Browser storage rejected the approval mode".into())
    }

    #[cfg(target_family = "wasm")]
    fn storage_key(&self) -> String {
        format!("hrns.tui.approvals.v1.{}", self.0)
    }
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

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(default)]
pub struct Preferences {
    pub sidebar: bool,
    pub sidebar_width: u16,
    pub show_thoughts: bool,
    pub theme: Option<String>,
}

impl Default for Preferences {
    fn default() -> Self {
        Self {
            sidebar: false,
            sidebar_width: 0,
            show_thoughts: true,
            theme: None,
        }
    }
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
    device_state_path().unwrap_or_else(|| std::env::temp_dir().join("hrns").join("tui.json"))
}

#[cfg(not(target_family = "wasm"))]
fn device_state_path() -> Option<std::path::PathBuf> {
    use std::path::PathBuf;

    if let Some(path) = std::env::var_os("HRNS_TUI_STATE") {
        return Some(PathBuf::from(path));
    }
    let base = std::env::var_os("XDG_STATE_HOME")
        .map(PathBuf::from)
        .or_else(|| {
            std::env::var_os("HOME").map(|home| PathBuf::from(home).join(".local/state"))
        })?;
    Some(base.join("hrns").join("tui.json"))
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
