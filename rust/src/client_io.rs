//! Native client-side filesystem and terminal services for ACP.

use std::collections::HashMap;
use std::io::Read;
use std::path::{Component, Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};

pub const MAX_FILE_BYTES: u64 = 2 * 1024 * 1024;
pub const MAX_OUTPUT_BYTES: usize = 1024 * 1024;
pub const MAX_TERMINALS: usize = 8;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Mode {
    Off,
    Read,
    All,
}

impl Mode {
    pub fn reads(self) -> bool {
        matches!(self, Self::Read | Self::All)
    }

    pub fn writes(self) -> bool {
        matches!(self, Self::All)
    }

    pub fn terminal(self) -> bool {
        matches!(self, Self::All)
    }
}

#[derive(Clone, Debug)]
pub struct Exit {
    pub code: Option<i32>,
    pub signal: Option<String>,
}

struct Terminal {
    session: String,
    child: Child,
    pid: u32,
    output: Arc<Mutex<Vec<u8>>>,
    truncated: Arc<AtomicBool>,
    status: Option<Exit>,
}

pub struct ClientIo {
    pub mode: Mode,
    roots: HashMap<String, PathBuf>,
    terminals: HashMap<String, Terminal>,
    next_terminal: usize,
}

impl ClientIo {
    pub fn new(mode: Mode) -> Self {
        Self {
            mode,
            roots: HashMap::new(),
            terminals: HashMap::new(),
            next_terminal: 0,
        }
    }

    pub fn remember(&mut self, session: &str, cwd: &Path) {
        if self.mode == Mode::Off {
            return;
        }
        if let Ok(root) = cwd.canonicalize() {
            self.roots.insert(session.to_owned(), root);
        }
    }

    pub fn close(&mut self) {
        for terminal in self.terminals.values_mut() {
            terminate(terminal);
        }
        self.terminals.clear();
        self.roots.clear();
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
        let size = std::fs::metadata(&target)
            .map_err(|error| reason(path, error))?
            .len();
        if size > MAX_FILE_BYTES {
            return Err(format!(
                "{}: file is larger than {MAX_FILE_BYTES} bytes",
                path.display()
            ));
        }
        let content = std::fs::read_to_string(&target).map_err(|error| reason(path, error))?;
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
            .strip_prefix(&root)
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
            None => root,
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
        let output = Arc::new(Mutex::new(Vec::new()));
        let truncated = Arc::new(AtomicBool::new(false));
        for stream in [
            child.stdout.take().map(StreamKind::Out),
            child.stderr.take().map(StreamKind::Err),
        ]
        .into_iter()
        .flatten()
        {
            let buffer = Arc::clone(&output);
            let flag = Arc::clone(&truncated);
            std::thread::spawn(move || {
                let mut reader: Box<dyn Read + Send> = match stream {
                    StreamKind::Out(stream) => Box::new(stream),
                    StreamKind::Err(stream) => Box::new(stream),
                };
                let mut chunk = [0u8; 64 * 1024];
                loop {
                    match reader.read(&mut chunk) {
                        Ok(0) | Err(_) => break,
                        Ok(read) => {
                            let Ok(mut buffer) = buffer.lock() else {
                                break;
                            };
                            let room = limit.saturating_sub(buffer.len());
                            if room == 0 {
                                flag.store(true, Ordering::Relaxed);
                            } else {
                                buffer.extend_from_slice(&chunk[..read.min(room)]);
                                if read > room {
                                    flag.store(true, Ordering::Relaxed);
                                }
                            }
                        }
                    }
                }
            });
        }
        self.next_terminal = self.next_terminal.wrapping_add(1);
        let id = format!("term-{pid}-{}", self.next_terminal);
        self.terminals.insert(
            id.clone(),
            Terminal {
                session: session.to_owned(),
                child,
                pid,
                output,
                truncated,
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
        let output = terminal
            .output
            .lock()
            .map(|bytes| String::from_utf8_lossy(&bytes).into_owned())
            .unwrap_or_default();
        let truncated = terminal.truncated.load(Ordering::Relaxed);
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
        if let Some(mut terminal) = self.terminals.remove(id) {
            if terminal.session != session {
                self.terminals.insert(id.to_owned(), terminal);
                return Err("no such terminal".into());
            }
            terminate(&mut terminal);
        }
        Ok(())
    }

    fn root(&self, session: &str) -> Result<PathBuf, String> {
        self.roots
            .get(session)
            .cloned()
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
        if !resolved.starts_with(&root) {
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
        if !normalized.starts_with(&root) {
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
        if !ancestor.starts_with(&root)
            || !ancestor
                .canonicalize()
                .map_err(|error| reason(path, error))?
                .starts_with(&root)
        {
            return Err(format!(
                "{}: outside the session working directory",
                path.display()
            ));
        }
        std::fs::create_dir_all(parent).map_err(|error| reason(path, error))?;
        let parent = parent.canonicalize().map_err(|error| reason(path, error))?;
        if !parent.starts_with(&root) {
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
        let terminal = self
            .terminals
            .get_mut(id)
            .ok_or_else(|| format!("no such terminal {id}"))?;
        if terminal.session != session {
            return Err(format!("no such terminal {id}"));
        }
        Ok(terminal)
    }
}

enum StreamKind {
    Out(std::process::ChildStdout),
    Err(std::process::ChildStderr),
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
