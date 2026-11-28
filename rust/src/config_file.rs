use std::fs::{self, OpenOptions};
use std::io::{self, Write};
use std::path::Path;

use tempfile::NamedTempFile;
use toml_edit::DocumentMut;

pub fn edit(
    path: &Path,
    update: impl FnOnce(&mut DocumentMut) -> io::Result<()>,
) -> io::Result<()> {
    let parent = path
        .parent()
        .ok_or_else(|| io::Error::other("Missing configuration directory"))?;
    let file_name = path
        .file_name()
        .ok_or_else(|| io::Error::other("Missing configuration filename"))?;
    fs::create_dir_all(parent)?;
    let mut lock_name = file_name.to_os_string();
    lock_name.push(".lock");
    let lock = OpenOptions::new()
        .write(true)
        .create(true)
        .truncate(false)
        .open(parent.join(lock_name))?;
    lock.lock()?;
    let mut document = match fs::read_to_string(path) {
        Ok(text) => text.parse::<DocumentMut>().map_err(io::Error::other)?,
        Err(error) if error.kind() == io::ErrorKind::NotFound => DocumentMut::new(),
        Err(error) => return Err(error),
    };
    update(&mut document)?;
    let mut temp = NamedTempFile::new_in(parent)?;
    temp.write_all(document.to_string().as_bytes())?;
    temp.as_file().sync_all()?;
    temp.persist(path).map_err(|error| error.error)?;
    Ok(())
}
