#[cfg(not(target_family = "wasm"))]
fn main() -> Result<(), Box<dyn std::error::Error>> {
    hrns_tui::run()
}

#[cfg(target_family = "wasm")]
fn main() {}
