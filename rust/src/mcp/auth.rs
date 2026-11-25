use std::path::Path;
use std::process::Stdio;
use std::time::Duration;

use tokio::process::Command;

pub(super) async fn stored_token(
    name: &str,
    config: &Path,
    url: &str,
    timeout: Duration,
) -> Option<String> {
    let output = tokio::time::timeout(
        timeout,
        command("_native-token", name, config)
            .stderr(Stdio::null())
            .output(),
    )
    .await
    .ok()?
    .ok()?;
    if !output.status.success() {
        return None;
    }
    let (stored_url, token) = serde_json::from_slice::<(String, String)>(&output.stdout).ok()?;
    (stored_url == url && !token.is_empty()).then_some(token)
}

pub(super) async fn sign_in(name: &str, config: &Path) -> Result<(), String> {
    let output = command("auth", name, config)
        .stdout(Stdio::null())
        .output()
        .await
        .map_err(|error| format!("Could not launch MCP sign-in: {error}"))?;
    if output.status.success() {
        return Ok(());
    }
    let diagnostic = super::startup_error("MCP sign-in failed".into(), output.stderr);
    Err(diagnostic)
}

fn command(operation: &str, name: &str, config: &Path) -> Command {
    let executable = std::env::current_exe()
        .ok()
        .map(|path| path.with_file_name(if cfg!(windows) { "hrns.exe" } else { "hrns" }))
        .filter(|path| path.is_file())
        .unwrap_or_else(|| "hrns".into());
    let mut command = Command::new(executable);
    command
        .kill_on_drop(true)
        .stdin(Stdio::null())
        .args(["mcp", operation, "--config"])
        .arg(config)
        .arg("--")
        .arg(name);
    command
}
