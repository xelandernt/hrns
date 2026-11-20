mod acp;
mod app;
mod attachments;
mod composer;
mod elicitation;
mod options;
mod preferences;
mod skills;
mod socket;
mod theme;
mod transcript;

#[cfg(not(target_family = "wasm"))]
mod client_io;
#[cfg(not(target_family = "wasm"))]
mod native;
#[cfg(target_family = "wasm")]
mod web;

fn main() -> Result<(), Box<dyn std::error::Error>> {
    #[cfg(not(target_family = "wasm"))]
    native::run()?;
    #[cfg(target_family = "wasm")]
    web::run()?;
    Ok(())
}
