use std::fs::{self, File, OpenOptions};
use std::io::{self, Write};
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex, Weak};

use base64::Engine;
use rmcp::transport::auth::{
    AuthError, CredentialRefreshGuard, CredentialStore, StoredCredentials,
};
use sha2::{Digest, Sha256};

use super::super::config::{CredentialSettings, StoreKind};

#[derive(Clone)]
pub(in crate::mcp) struct Store {
    backend: Backend,
    lock: PathBuf,
    active_lock: Arc<Mutex<Weak<File>>>,
}

#[derive(Clone)]
enum Backend {
    File(PathBuf),
    Keyring(String),
}

impl Store {
    pub(in crate::mcp) fn new(
        settings: &CredentialSettings,
        url: &reqwest::Url,
        client_id: &str,
    ) -> Result<Self, String> {
        let dirs = directories::BaseDirs::new().ok_or("User data directory is unavailable")?;
        let default_dir = dirs.data_dir().join("hrns/mcp-auth");
        let account = base64::engine::general_purpose::URL_SAFE_NO_PAD
            .encode(Sha256::digest(format!("{url}\n{client_id}")));
        let directory = match (settings.token_store, settings.token_store_path.trim()) {
            (StoreKind::Keyring, _) | (_, "") => default_dir,
            (_, "~") => dirs.home_dir().to_owned(),
            (_, path) if path.starts_with("~/") => dirs.home_dir().join(&path[2..]),
            (_, path) => PathBuf::from(path),
        }
        .join("native");
        let backend = match settings.token_store {
            StoreKind::File => Backend::File(directory.join(format!("{account}.json"))),
            StoreKind::Keyring => Backend::Keyring(account.clone()),
        };
        Ok(Self {
            backend,
            lock: directory.join(format!("{account}.lock")),
            active_lock: Arc::default(),
        })
    }
    pub(in crate::mcp) async fn sign_out(&self) -> Result<(), AuthError> {
        let _guard = self.acquire_refresh_guard().await?;
        self.clear().await
    }

    fn mutation_lock(&self) -> Result<Arc<File>, AuthError> {
        self.active_lock
            .lock()
            .ok()
            .and_then(|file| file.upgrade())
            .ok_or_else(|| store_error("credential mutation has no lock"))
    }
}

#[async_trait::async_trait]
impl CredentialStore for Store {
    async fn load(&self) -> Result<Option<StoredCredentials>, AuthError> {
        let backend = self.backend.clone();
        blocking(move || {
            let bytes = match backend {
                Backend::File(path) => match fs::read(path) {
                    Ok(bytes) => bytes,
                    Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(None),
                    Err(error) => return Err(store_error(error)),
                },
                Backend::Keyring(account) => match keyring_entry(&account)?.get_secret() {
                    Ok(bytes) => bytes,
                    Err(keyring::Error::NoEntry) => return Ok(None),
                    Err(error) => return Err(store_error(error)),
                },
            };
            serde_json::from_slice(&bytes)
                .map(Some)
                .map_err(store_error)
        })
        .await
    }

    async fn save(&self, credentials: StoredCredentials) -> Result<(), AuthError> {
        let backend = self.backend.clone();
        let guard = self.mutation_lock()?;
        blocking(move || {
            // The blocking task can finish after the caller is cancelled.
            let _guard = guard;
            let bytes = serde_json::to_vec(&credentials).map_err(store_error)?;
            match backend {
                Backend::File(path) => save_file(&path, &bytes).map_err(store_error),
                Backend::Keyring(account) => keyring_entry(&account)?
                    .set_secret(&bytes)
                    .map_err(keyring_save_error),
            }
        })
        .await
    }

    async fn clear(&self) -> Result<(), AuthError> {
        let backend = self.backend.clone();
        let guard = self.mutation_lock()?;
        blocking(move || {
            let _guard = guard;
            match backend {
                Backend::File(path) => match fs::remove_file(path) {
                    Ok(()) => Ok(()),
                    Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(()),
                    Err(error) => Err(store_error(error)),
                },
                Backend::Keyring(account) => match keyring_entry(&account)?.delete_credential() {
                    Ok(()) | Err(keyring::Error::NoEntry) => Ok(()),
                    Err(error) => Err(store_error(error)),
                },
            }
        })
        .await
    }

    async fn acquire_refresh_guard(&self) -> Result<Option<CredentialRefreshGuard>, AuthError> {
        let path = self.lock.clone();
        let file = blocking(move || open_lock(&path).map_err(store_error)).await?;
        // Nonblocking retries let a cancelled authorization stop waiting for another process.
        loop {
            match file.try_lock() {
                Ok(()) => {
                    let file = Arc::new(file);
                    *self.active_lock.lock().map_err(store_error)? = Arc::downgrade(&file);
                    return Ok(Some(CredentialRefreshGuard::new(file)));
                }
                Err(std::fs::TryLockError::WouldBlock) => {
                    tokio::time::sleep(std::time::Duration::from_millis(50)).await;
                }
                Err(std::fs::TryLockError::Error(error)) => return Err(store_error(error)),
            }
        }
    }
}

#[cfg(any(target_os = "linux", target_os = "macos", target_os = "windows"))]
fn keyring_entry(account: &str) -> Result<keyring::Entry, AuthError> {
    keyring::Entry::new("hrns.mcp.native", account).map_err(store_error)
}

#[cfg(not(any(target_os = "linux", target_os = "macos", target_os = "windows")))]
fn keyring_entry(_: &str) -> Result<keyring::Entry, AuthError> {
    Err(store_error(
        "the OS keyring backend is unavailable on this platform",
    ))
}

fn keyring_save_error(error: keyring::Error) -> AuthError {
    #[cfg(windows)]
    if let keyring::Error::TooLong(_, limit) = error {
        return store_error(format!(
            "Windows Credential Manager can store at most {limit} bytes per entry. Set token_store = \"file\" under [mcp] and sign in again."
        ));
    }
    store_error(error)
}

async fn blocking<T: Send + 'static>(
    action: impl FnOnce() -> Result<T, AuthError> + Send + 'static,
) -> Result<T, AuthError> {
    tokio::task::spawn_blocking(action)
        .await
        .map_err(store_error)?
}

fn store_error(error: impl std::fmt::Display) -> AuthError {
    AuthError::CredentialStoreError(error.to_string())
}

fn create_parent(path: &Path) -> io::Result<&Path> {
    let parent = path
        .parent()
        .ok_or_else(|| io::Error::other("Missing credentials directory"))?;
    let mut builder = fs::DirBuilder::new();
    builder.recursive(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::DirBuilderExt;
        builder.mode(0o700);
    }
    builder.create(parent)?;
    Ok(parent)
}

fn open_lock(path: &Path) -> io::Result<File> {
    create_parent(path)?;
    let mut options = OpenOptions::new();
    options.read(true).write(true).create(true).truncate(false);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.mode(0o600);
    }
    options.open(path)
}

fn save_file(path: &Path, bytes: &[u8]) -> io::Result<()> {
    let mut temporary = tempfile::NamedTempFile::new_in(create_parent(path)?)?;
    temporary.write_all(bytes)?;
    temporary.as_file().sync_all()?;
    temporary.persist(path)?;
    Ok(())
}
