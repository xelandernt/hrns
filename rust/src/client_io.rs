//! Native client-side filesystem and terminal services for ACP.

use std::collections::{HashMap, VecDeque};
use std::io::Read;
use std::path::{Component, Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::{Arc, Mutex};

use crate::acp::ClientIoMode;

pub const MAX_FILE_BYTES: u64 = 2 * 1024 * 1024;
pub const MAX_OUTPUT_BYTES: usize = 1024 * 1024;
pub const MAX_TERMINALS: usize = 8;

#[derive(Clone, Debug)]
pub struct Exit {
    pub code: Option<i32>,
    pub signal: Option<String>,
}

struct Terminal {
    child: Child,
    pid: u32,
    output: Arc<Mutex<TerminalOutput>>,
    status: Option<Exit>,
}

struct TerminalOutput {
    bytes: VecDeque<u8>,
    truncated: bool,
    limit: usize,
}

impl TerminalOutput {
    fn append(&mut self, chunk: &[u8]) {
        let overflow = (self.bytes.len() + chunk.len()).saturating_sub(self.limit);
        let discard = overflow.min(self.bytes.len());
        self.bytes.drain(..discard);
        self.bytes.extend(&chunk[overflow - discard..]);
        self.truncated |= overflow > 0;
    }

    fn snapshot(&mut self) -> (String, bool) {
        let bytes = self.bytes.make_contiguous();
        let start = if self.truncated {
            // A byte limit can bisect the first retained UTF-8 character.
            bytes
                .iter()
                .take_while(|byte| **byte & 0b1100_0000 == 0b1000_0000)
                .count()
        } else {
            0
        };
        let mut text = String::from_utf8_lossy(&bytes[start..]).into_owned();
        // Replacement characters for invalid UTF-8 can increase the byte count.
        let mut overflow = text.len().saturating_sub(self.limit);
        while !text.is_char_boundary(overflow) {
            overflow += 1;
        }
        text.drain(..overflow);
        (text, self.truncated || overflow > 0)
    }
}

pub struct ClientIo {
    mode: ClientIoMode,
    session: String,
    root: Option<PathBuf>,
    terminals: HashMap<String, Terminal>,
    next_terminal: usize,
}

impl ClientIo {
    pub fn new(mode: ClientIoMode, session: String, cwd: &Path) -> Self {
        let root = if mode.reads() {
            cwd.canonicalize().ok()
        } else {
            None
        };
        Self {
            mode,
            session,
            root,
            terminals: HashMap::new(),
            next_terminal: 0,
        }
    }

    pub fn read(
        &self,
        session: &str,
        path: &Path,
        line: Option<u32>,
        limit: Option<u32>,
    ) -> Result<String, String> {
        if !self.mode.reads() {
            return Err("fs/read_text_file is not advertised".into());
        }
        let target = self.resolve(session, path)?;
        let metadata = target.metadata().map_err(|error| reason(path, error))?;
        if !metadata.is_file() {
            return Err(format!("{}: expected a regular file", path.display()));
        }
        let too_large = || {
            format!(
                "{}: file is larger than {MAX_FILE_BYTES} bytes",
                path.display()
            )
        };
        if metadata.len() > MAX_FILE_BYTES {
            return Err(too_large());
        }
        let mut content = String::new();
        std::fs::File::open(&target)
            .map_err(|error| reason(path, error))?
            .take(MAX_FILE_BYTES + 1)
            .read_to_string(&mut content)
            .map_err(|error| reason(path, error))?;
        if content.len() as u64 > MAX_FILE_BYTES {
            return Err(too_large());
        }
        Ok(match (line, limit) {
            (None, None) => content,
            _ => window(&content, line, limit),
        })
    }

    pub fn write(&self, session: &str, path: &Path, content: &str) -> Result<(), String> {
        if !self.mode.writes() {
            return Err("fs/write_text_file is not advertised".into());
        }
        let target = self.resolve_for_write(session, path)?;
        let root = self.root(session)?;
        let relative = target
            .strip_prefix(root)
            .map_err(|_| format!("{}: outside the session working directory", path.display()))?;
        if read_only(relative) {
            return Err(format!(
                "{}: version-control, credential, and key paths are read-only",
                path.display()
            ));
        }
        if let Some(parent) = target.parent() {
            std::fs::create_dir_all(parent).map_err(|error| reason(path, error))?;
        }
        std::fs::write(&target, content).map_err(|error| reason(path, error))
    }

    pub fn create_terminal(
        &mut self,
        session: &str,
        command: &str,
        args: &[String],
        env: &[(String, String)],
        cwd: Option<&Path>,
        output_limit: Option<u64>,
    ) -> Result<String, String> {
        if !self.mode.terminal() {
            return Err("terminal/create is not advertised".into());
        }
        if self.terminals.len() >= MAX_TERMINALS {
            return Err(format!(
                "too many terminals are open (limit {MAX_TERMINALS})"
            ));
        }
        let root = self.root(session)?;
        let working = match cwd {
            Some(path) if path.is_absolute() => normalize(path),
            Some(path) => normalize(&root.join(path)),
            None => root.to_path_buf(),
        };
        let limit = output_limit
            .and_then(|limit| usize::try_from(limit).ok())
            .unwrap_or(MAX_OUTPUT_BYTES)
            .min(MAX_OUTPUT_BYTES);
        let mut process = Command::new(command);
        process
            .args(args)
            .current_dir(&working)
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());
        for (name, value) in env {
            process.env(name, value);
        }
        #[cfg(unix)]
        {
            use std::os::unix::process::CommandExt;
            process.process_group(0);
        }
        let mut child = process
            .spawn()
            .map_err(|error| format!("{command}: {error}"))?;
        let pid = child.id();
        let output = Arc::new(Mutex::new(TerminalOutput {
            bytes: VecDeque::new(),
            truncated: false,
            limit,
        }));
        capture_output(child.stdout.take(), &output);
        capture_output(child.stderr.take(), &output);
        self.next_terminal = self.next_terminal.wrapping_add(1);
        let id = format!("term-{pid}-{}", self.next_terminal);
        self.terminals.insert(
            id.clone(),
            Terminal {
                child,
                pid,
                output,
                status: None,
            },
        );
        Ok(id)
    }

    pub fn terminal_output(
        &mut self,
        session: &str,
        id: &str,
    ) -> Result<(String, bool, Option<Exit>), String> {
        if !self.mode.terminal() {
            return Err("terminal/output is not advertised".into());
        }
        let terminal = self.terminal(session, id)?;
        let status = refresh_status(terminal);
        let (output, truncated) = terminal
            .output
            .lock()
            .map(|mut output| output.snapshot())
            .unwrap_or_default();
        Ok((output, truncated, status))
    }

    pub fn wait_for_exit(&mut self, session: &str, id: &str) -> Result<Option<Exit>, String> {
        if !self.mode.terminal() {
            return Err("terminal/wait_for_exit is not advertised".into());
        }
        let terminal = self.terminal(session, id)?;
        Ok(refresh_status(terminal))
    }

    pub fn kill_terminal(&mut self, session: &str, id: &str) -> Result<(), String> {
        if !self.mode.terminal() {
            return Err("terminal/kill is not advertised".into());
        }
        let terminal = self.terminal(session, id)?;
        terminate(terminal);
        Ok(())
    }

    pub fn release_terminal(&mut self, session: &str, id: &str) -> Result<(), String> {
        if !self.mode.terminal() {
            return Err("terminal/release is not advertised".into());
        }
        if self.session != session {
            return if self.terminals.contains_key(id) {
                Err("no such terminal".into())
            } else {
                Ok(())
            };
        }
        if let Some(mut terminal) = self.terminals.remove(id) {
            terminate(&mut terminal);
        }
        Ok(())
    }

    fn root(&self, session: &str) -> Result<&Path, String> {
        self.root
            .as_deref()
            .filter(|_| self.session == session)
            .ok_or_else(|| format!("no working directory is known for session {session}"))
    }

    fn resolve(&self, session: &str, path: &Path) -> Result<PathBuf, String> {
        let root = self.root(session)?;
        let candidate = if path.is_absolute() {
            path.to_path_buf()
        } else {
            root.join(path)
        };
        let resolved = candidate
            .canonicalize()
            .map_err(|error| reason(path, error))?;
        if !resolved.starts_with(root) {
            return Err(format!(
                "{}: outside the session working directory",
                path.display()
            ));
        }
        Ok(resolved)
    }

    fn resolve_for_write(&self, session: &str, path: &Path) -> Result<PathBuf, String> {
        let root = self.root(session)?;
        let candidate = if path.is_absolute() {
            path.to_path_buf()
        } else {
            root.join(path)
        };
        let normalized = normalize(&candidate);
        if !normalized.starts_with(root) {
            return Err(format!(
                "{}: outside the session working directory",
                path.display()
            ));
        }
        if normalized.exists() {
            return normalized
                .canonicalize()
                .map_err(|error| reason(path, error));
        }
        let parent = normalized
            .parent()
            .ok_or_else(|| format!("{}: invalid path", path.display()))?;
        let mut ancestor = parent;
        while !ancestor.exists() {
            ancestor = ancestor
                .parent()
                .ok_or_else(|| format!("{}: invalid path", path.display()))?;
        }
        if !ancestor.starts_with(root)
            || !ancestor
                .canonicalize()
                .map_err(|error| reason(path, error))?
                .starts_with(root)
        {
            return Err(format!(
                "{}: outside the session working directory",
                path.display()
            ));
        }
        std::fs::create_dir_all(parent).map_err(|error| reason(path, error))?;
        let parent = parent.canonicalize().map_err(|error| reason(path, error))?;
        if !parent.starts_with(root) {
            return Err(format!(
                "{}: outside the session working directory",
                path.display()
            ));
        }
        Ok(parent.join(
            normalized
                .file_name()
                .ok_or_else(|| format!("{}: invalid path", path.display()))?,
        ))
    }

    fn terminal(&mut self, session: &str, id: &str) -> Result<&mut Terminal, String> {
        self.terminals
            .get_mut(id)
            .filter(|_| self.session == session)
            .ok_or_else(|| format!("no such terminal {id}"))
    }
}

impl Drop for ClientIo {
    fn drop(&mut self) {
        for terminal in self.terminals.values_mut() {
            terminate(terminal);
        }
    }
}

fn capture_output(reader: Option<impl Read + Send + 'static>, output: &Arc<Mutex<TerminalOutput>>) {
    let Some(mut reader) = reader else { return };
    let output = Arc::clone(output);
    std::thread::spawn(move || {
        let mut chunk = [0u8; 64 * 1024];
        loop {
            let read = match reader.read(&mut chunk) {
                Ok(0) => break,
                Ok(read) => read,
                Err(error) if error.kind() == std::io::ErrorKind::Interrupted => continue,
                Err(_) => break,
            };
            let Ok(mut output) = output.lock() else { break };
            output.append(&chunk[..read]);
        }
    });
}

fn refresh_status(terminal: &mut Terminal) -> Option<Exit> {
    if terminal.status.is_none()
        && let Ok(Some(status)) = terminal.child.try_wait()
    {
        terminal.status = Some(exit_of(status));
    }
    terminal.status.clone()
}

#[cfg(unix)]
fn terminate(terminal: &mut Terminal) {
    if terminal.status.is_some() {
        return;
    }
    unsafe {
        let _ = libc::killpg(terminal.pid as libc::pid_t, libc::SIGKILL);
    }
    let _ = terminal.child.kill();
    let _ = terminal.child.wait();
    terminal.status = Some(Exit {
        code: None,
        signal: Some("SIGKILL".into()),
    });
}

#[cfg(not(unix))]
fn terminate(terminal: &mut Terminal) {
    let _ = terminal.child.kill();
    let _ = terminal.child.wait();
    terminal.status = Some(Exit {
        code: None,
        signal: None,
    });
}

fn exit_of(status: std::process::ExitStatus) -> Exit {
    #[cfg(unix)]
    {
        use std::os::unix::process::ExitStatusExt;
        if let Some(signal) = status.signal() {
            return Exit {
                code: None,
                signal: Some(signal.to_string()),
            };
        }
    }
    Exit {
        code: status.code(),
        signal: None,
    }
}

fn normalize(path: &Path) -> PathBuf {
    let mut normalized = PathBuf::new();
    for component in path.components() {
        match component {
            Component::CurDir => {}
            Component::ParentDir => {
                normalized.pop();
            }
            other => normalized.push(other.as_os_str()),
        }
    }
    normalized
}

fn window(content: &str, line: Option<u32>, limit: Option<u32>) -> String {
    let start = line.unwrap_or(1).saturating_sub(1) as usize;
    let stop = limit.map(|limit| start.saturating_add(limit as usize));
    content
        .split_inclusive('\n')
        .skip(start)
        .take(stop.map_or(usize::MAX, |stop| stop.saturating_sub(start)))
        .collect()
}

fn read_only(relative: &Path) -> bool {
    const PROTECTED: [&str; 3] = [".git", ".hrns", ".pydantic-ai-harness"];
    if relative
        .components()
        .any(|component| PROTECTED.contains(&component.as_os_str().to_string_lossy().as_ref()))
    {
        return true;
    }
    let Some(name) = relative.file_name().and_then(|name| name.to_str()) else {
        return false;
    };
    let lower = name.to_ascii_lowercase();
    name.starts_with(".env")
        || lower.starts_with("secrets")
        || name.ends_with(".pem")
        || name.ends_with(".key")
}

fn reason(path: &Path, error: std::io::Error) -> String {
    format!("{}: {}", path.display(), error)
}
