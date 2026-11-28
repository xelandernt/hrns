mod acp;
mod app;
mod attachments;
mod composer;
mod elicitation;
mod mcp_types;
mod options;
mod preferences;
mod skills;
mod socket;
mod theme;
mod tool_input;
mod transcript;

#[cfg(not(target_family = "wasm"))]
mod client_io;
#[cfg(not(target_family = "wasm"))]
mod config_file;
#[cfg(not(target_family = "wasm"))]
mod effort;
#[cfg(not(target_family = "wasm"))]
mod mcp;
#[cfg(not(target_family = "wasm"))]
mod native;
#[cfg(target_family = "wasm")]
mod web;

#[cfg(not(target_family = "wasm"))]
pub fn run() -> Result<(), Box<dyn std::error::Error>> {
    native::run()?;
    Ok(())
}

#[cfg(target_family = "wasm")]
pub use web::WebTerminal;
