mod quit;

use std::env;
use std::io::{self, stdout};
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};

use agent_client_protocol::{AcpAgent, AcpAgentConfig, Channel};
use crossterm::clipboard::CopyToClipboard;
use crossterm::event::{
    self, DisableBracketedPaste, DisableFocusChange, DisableMouseCapture, EnableBracketedPaste,
    EnableFocusChange, EnableMouseCapture, MouseButton, MouseEventKind,
};
use crossterm::event::{Event as TerminalEvent, KeyCode, KeyEvent, KeyEventKind, KeyModifiers};
use crossterm::style::Print;
use futures::channel::mpsc;
use ratatui::layout::Position;
use tokio_tungstenite::tungstenite::Message;
use tokio_tungstenite::tungstenite::client::IntoClientRequest;
use tokio_tungstenite::tungstenite::protocol::{WebSocketConfig, frame::coding::CloseCode};

use crate::acp::{self, Actions, ClientIoMode, Connection, Event, Events, Services, SessionTarget};
use crate::app::{App, Key, LocalEffect, Startup};
use crate::attachments;
use crate::effort::EffortProfile;
use crate::socket;

const HELP: &str =
    "Usage: hrns-tui [--cwd PATH] [--session ID] [--server URL | --stdio COMMAND [ARGS...]]
       [--client-io off|read|all] [--theme NAME]
       [--mcp-config PATH]
       Native WebSocket authentication reads HRNS_TUI_TOKEN.
       With no transport option, starts the built-in hrns agent.";

/// Motion reporting needs SGR coordinates; RXVT encoding is deprecated and some terminals report
/// motion in it badly, so it is cleared after the capture modes are set. Windows consoles use the
/// platform mouse API instead of these sequences.
const MOUSE_ENCODING: &str = if cfg!(windows) { "" } else { "\x1b[?1015l" };

#[derive(Clone, PartialEq, Eq)]
enum Target {
    Remote(String),
    Local { command: String, args: Vec<String> },
    Stdio { command: String, args: Vec<String> },
}

impl Target {
    fn builtin() -> Self {
        Self::Local {
            command: "hrns".into(),
            args: vec!["agent".into(), "--local-endpoint".into()],
        }
    }
}

struct Options {
    session: SessionTarget,
    target: Target,
    client_io: ClientIoMode,
    theme: Option<String>,
    mcp_config: Option<PathBuf>,
    effort_profile: Option<EffortProfile>,
}

impl Options {
    fn parse() -> Result<Option<Self>, String> {
        let mut argv = env::args().skip(1);
        let mut cwd = None;
        let mut target = None;
        let mut session = None;
        let mut client_io = None;
        let mut theme = None;
        let mut mcp_config = None;
        let mut effort_profile = None;
        while let Some(arg) = argv.next() {
            match arg.as_str() {
                "--help" | "-h" => return Ok(None),
                "--client-io" if client_io.is_none() => {
                    client_io = Some(ClientIoMode::parse(
                        &argv.next().ok_or("--client-io requires a mode")?,
                    ));
                }
                "--theme" if theme.is_none() => {
                    theme = Some(argv.next().ok_or("--theme requires a name")?);
                }
                "--mcp-config" if mcp_config.is_none() => {
                    mcp_config = Some(PathBuf::from(
                        argv.next().ok_or("--mcp-config requires a path")?,
                    ));
                }
                "--effort-profile" if effort_profile.is_none() => {
                    let settings = argv.next().ok_or("--effort-profile requires settings")?;
                    effort_profile = Some(
                        serde_json::from_str(&settings)
                            .map_err(|_| "Invalid effort profile settings")?,
                    );
                }
                "--cwd" if cwd.is_none() => {
                    cwd = Some(PathBuf::from(argv.next().ok_or("--cwd requires a path")?));
                }
                "--session" if session.is_none() => {
                    session = Some(
                        argv.next()
                            .filter(|id| !id.is_empty())
                            .ok_or("--session requires an ID")?,
                    );
                }
                "--server" if target.is_none() => {
                    let url = argv.next().ok_or("--server requires a URL")?;
                    if !url.starts_with("ws://") && !url.starts_with("wss://") {
                        return Err("Expected a ws:// or wss:// endpoint".into());
                    }
                    target = Some(Target::Remote(url));
                }
                "--stdio" if target.is_none() => {
                    target = Some(Target::Stdio {
                        command: argv.next().ok_or("--stdio requires a command")?,
                        args: argv.by_ref().collect(),
                    });
                }
                "--local" if target.is_none() => {
                    target = Some(Target::Local {
                        command: argv.next().ok_or("--local requires a command")?,
                        args: argv.by_ref().collect(),
                    });
                }
                _ => return Err(format!("Unknown or repeated option {arg:?}\n{HELP}")),
            }
        }
        let cwd = match cwd {
            Some(cwd) if matches!(target, Some(Target::Remote(_))) => Some(cwd),
            Some(cwd) => Some(std::path::absolute(cwd).map_err(|error| error.to_string())?),
            None if session.is_some() && !matches!(target, Some(Target::Stdio { .. })) => None,
            None => Some(env::current_dir().map_err(|error| error.to_string())?),
        };
        let target = target.unwrap_or_else(Target::builtin);
        let mcp_config = mcp_config
            .or_else(|| env::var_os("HRNS_CONFIG_FILE").map(PathBuf::from))
            .map(std::path::absolute)
            .transpose()
            .map_err(|error| error.to_string())?;
        Ok(Some(Self {
            session: SessionTarget { cwd, session },
            target,
            client_io: client_io.unwrap_or(ClientIoMode::All),
            theme,
            mcp_config,
            effort_profile,
        }))
    }

    fn label(&self) -> String {
        let location = match &self.target {
            Target::Remote(url) => url
                .split_once("://")
                .and_then(|(_, rest)| rest.split(['/', '?', '#']).next())
                .and_then(|authority| authority.rsplit('@').next())
                .unwrap_or("remote"),
            Target::Local { .. } | Target::Stdio { .. } => "local",
        };
        location.to_owned()
    }
}

pub fn run() -> Result<(), Box<dyn std::error::Error>> {
    if env::args()
        .nth(1)
        .as_deref()
        .is_some_and(|arg| matches!(arg, "--version" | "-V"))
    {
        println!("hrns-tui {}", env!("CARGO_PKG_VERSION"));
        return Ok(());
    }
    let Some(options) = Options::parse()? else {
        println!("{HELP}");
        return Ok(());
    };

    if matches!(options.target, Target::Stdio { .. })
        && let Some(cwd) = &options.session.cwd
    {
        env::set_current_dir(cwd)?;
    }
    let (action_tx, mut action_rx) = mpsc::unbounded();
    let (event_tx, mut event_rx) = mpsc::unbounded();
    let local_events = event_tx.clone();
    let (requests_tx, requests_rx) = mpsc::unbounded();
    let destination = options.label();
    let startup_transport = options.target.clone();
    let startup = Startup {
        client_io: options.client_io,
        theme: options.theme.clone(),
    };
    let services = Services {
        client_io: options.client_io,
        mcp_config: options.mcp_config.clone(),
        effort_profile: options.effort_profile.clone(),
        ..Services::default()
    };
    let (shutdown, stop_rx) = mpsc::unbounded();
    let mut terminal = ratatui::try_init()?;
    let worker = std::thread::spawn(move || {
        let result = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .map_err(|error| error.to_string())
            .map(|runtime| {
                runtime.block_on(supervise(
                    &options,
                    &mut action_rx,
                    event_tx.clone(),
                    requests_rx,
                    stop_rx,
                    services,
                ));
            });
        if let Err(error) = result {
            let _ = event_tx.unbounded_send(Event::Error(error));
            let _ = event_tx.unbounded_send(Event::Disconnected);
        }
    });
    let mut app = App::new(destination, action_tx, crate::preferences::load(), startup);
    let result = (|| -> io::Result<()> {
        crossterm::execute!(
            stdout(),
            EnableBracketedPaste,
            EnableFocusChange,
            EnableMouseCapture,
            Print(MOUSE_ENCODING)
        )?;
        let mut dirty = true;
        let mut next_refresh = Instant::now() + Duration::from_secs(2);
        let mut quit = quit::Quit::default();
        loop {
            for _ in 0..256 {
                let Ok(update) = event_rx.try_recv() else {
                    break;
                };
                quit.event(&update, &app);
                app.event(update);
                dirty = true;
            }
            if quit.finished() {
                break;
            }
            dirty |= app.poll_file_completion();
            if Instant::now() >= next_refresh {
                app.refresh_agents();
                next_refresh = Instant::now() + Duration::from_secs(2);
            }
            if let Some(effect) = app.take_local_effect() {
                local_effect(
                    effect,
                    &mut app,
                    &local_events,
                    &requests_tx,
                    &startup_transport,
                );
                dirty = true;
            }
            if dirty {
                terminal.draw(|frame| {
                    app.draw(frame);
                    quit.draw(frame, &app.palette());
                })?;
                dirty = false;
            }
            if !event::poll(Duration::from_millis(16))? {
                continue;
            }
            match event::read()? {
                TerminalEvent::Key(key) if key.kind != KeyEventKind::Release => {
                    if quit.active() {
                        quit.key(key, &app);
                        dirty = true;
                        continue;
                    }
                    if matches!(key.code, KeyCode::Char('c' | 'q'))
                        && key.modifiers.contains(KeyModifiers::CONTROL)
                    {
                        quit.open(&app);
                        dirty = true;
                        continue;
                    }
                    if let Some(key) = translate(key) {
                        app.key(key);
                        if let Some(effect) = app.take_local_effect() {
                            local_effect(
                                effect,
                                &mut app,
                                &local_events,
                                &requests_tx,
                                &startup_transport,
                            );
                        }
                        dirty = true;
                    }
                }
                TerminalEvent::Paste(text) => {
                    if !quit.active() {
                        app.paste(&text);
                    }
                    dirty = true;
                }
                TerminalEvent::Mouse(mouse) if !quit.active() && mouse.modifiers.is_empty() => {
                    let position = Position::new(mouse.column, mouse.row);
                    dirty |= match mouse.kind {
                        MouseEventKind::ScrollUp => app.pointer_scroll(position, -3),
                        MouseEventKind::ScrollDown => app.pointer_scroll(position, 3),
                        MouseEventKind::Down(MouseButton::Left) => app.pointer_down(position),
                        MouseEventKind::Drag(MouseButton::Left) => {
                            app.pointer_drag(position) | app.pointer_move(position)
                        }
                        MouseEventKind::Up(MouseButton::Left) => {
                            app.pointer_up(position) | app.pointer_move(position)
                        }
                        MouseEventKind::Moved => app.pointer_move(position),
                        _ => false,
                    };
                }
                TerminalEvent::FocusGained => {
                    // Windows Terminal and ConPTY drop mouse modes on focus changes, as do some
                    // other terminals; re-assert them the way OpenCode's terminal does.
                    crossterm::execute!(stdout(), EnableMouseCapture, Print(MOUSE_ENCODING))?;
                }
                TerminalEvent::Resize(_, _) => dirty = true,
                _ => {}
            }
        }
        Ok(())
    })();
    let paste = crossterm::execute!(
        stdout(),
        DisableMouseCapture,
        DisableBracketedPaste,
        DisableFocusChange
    );
    let restore = ratatui::try_restore();
    let _ = shutdown.unbounded_send(());
    let _ = worker.join();
    result?;
    paste?;
    restore?;
    Ok(())
}

fn local_effect(
    effect: LocalEffect,
    app: &mut App,
    events: &Events,
    requests: &mpsc::UnboundedSender<Connection>,
    startup: &Target,
) {
    match effect {
        LocalEffect::Reconnect => {
            let _ = requests.unbounded_send(Connection::Reconnect);
        }
        LocalEffect::Connect(target) => match connection_target(startup, &target) {
            Ok((transport, cwd, _)) => {
                if matches!(transport, Target::Stdio { .. })
                    && let Err(error) = env::set_current_dir(cwd)
                {
                    app.event(Event::Error(format!(
                        "Could not open the local workspace: {error}"
                    )));
                    return;
                }
                let _ = requests.unbounded_send(Connection::Connect(target));
            }
            Err(error) => app.event(Event::Error(error)),
        },
        LocalEffect::Switch(target) => {
            let _ = requests.unbounded_send(Connection::Switch(target));
        }
        LocalEffect::SavePreferences(preferences) => crate::preferences::save(&preferences),
        LocalEffect::Copy(text) => {
            if let Err(error) = copy_to_clipboard(&text) {
                app.event(Event::Error(format!(
                    "Could not copy to the clipboard: {error}"
                )));
            }
        }
        LocalEffect::Attach(path) if path.is_empty() => {
            app.event(Event::Warning("Use /attach <path> to select a file".into()));
        }
        LocalEffect::Attach(path) => {
            app.event(Event::LoadingAttachment);
            let events = events.clone();
            let path = if let Some(relative) = path.strip_prefix("~/")
                && let Some(home) = env::var_os("HOME")
            {
                PathBuf::from(home).join(relative)
            } else {
                app.local_attachment_path(path.into())
            };
            std::thread::spawn(move || {
                let attachment = attachments::read_native(&path.to_string_lossy())
                    .map(Box::new)
                    .map_err(|error| format!("Could not attach file: {error}"));
                let _ = events.unbounded_send(Event::Attachment(attachment));
            });
        }
    }
}

fn copy_to_clipboard(text: &str) -> io::Result<()> {
    #[cfg(target_os = "macos")]
    if env::var_os("SSH_CONNECTION").is_none() {
        use std::io::Write;
        use std::process::{Command, Stdio};

        let mut child = Command::new("/usr/bin/pbcopy")
            .stdin(Stdio::piped())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()?;
        let written = child
            .stdin
            .take()
            .ok_or_else(|| io::Error::other("pbcopy input pipe unavailable"))
            .and_then(|mut stdin| stdin.write_all(text.as_bytes()));
        let status = child.wait()?;
        written?;
        return if status.success() {
            Ok(())
        } else {
            Err(io::Error::other(format!("pbcopy exited with {status}")))
        };
    }

    crossterm::execute!(stdout(), CopyToClipboard::to_clipboard_from(text))
}

async fn supervise(
    options: &Options,
    actions: &mut Actions,
    events: Events,
    mut requests: mpsc::UnboundedReceiver<Connection>,
    mut stop: mpsc::UnboundedReceiver<()>,
    services: Services,
) {
    use futures::{FutureExt, StreamExt, select_biased};
    let mut target = options.session.clone();
    let mut transport = options.target.clone();
    let mut rollback = None;
    let mut switch_error = None;
    loop {
        let mut ready = false;
        let request = {
            let (scoped_events, scoped_receiver) = mpsc::unbounded();
            let mut scoped_receiver = scoped_receiver.fuse();
            let connection = connect(
                &transport,
                &options.target,
                target.clone(),
                actions,
                scoped_events,
                services.clone(),
            )
            .fuse();
            futures::pin_mut!(connection);
            loop {
                select_biased! {
                    _ = stop.next() => return,
                    request = requests.next() => break Ok(request),
                    event = scoped_receiver.select_next_some() => {
                        target.remember(&event);
                        if matches!(&event, Event::Ready { .. }) {
                            ready = true;
                            rollback = None;
                        }
                        let _ = events.unbounded_send(event);
                        if ready && let Some(message) = switch_error.take() {
                            let _ = events.unbounded_send(Event::Warning(message));
                        }
                    }
                    result = connection => break Err(result),
                }
            }
        };
        let request = match request {
            Ok(request) => request,
            Err(Err(error)) if !ready && rollback.is_some() => {
                if let Some(previous) = rollback.take() {
                    target = previous;
                }
                switch_error = Some(format!("Could not switch sessions: {error}"));
                let _ = events.unbounded_send(Event::SwitchReplayReset);
                continue;
            }
            Err(result) => {
                finish(&events, result);
                if let Some(message) = switch_error.take() {
                    let _ = events.unbounded_send(Event::Warning(message));
                }
                select_biased! {
                    _ = stop.next() => return,
                    request = requests.next() => request,
                }
            }
        };
        let Some(request) = request else { return };
        if ready
            && matches!(request, Connection::Switch(_))
            && !matches!(transport, Target::Stdio { .. })
        {
            rollback = Some(target.clone());
        }
        apply(options, &mut transport, &mut target, request, &events);
    }
}

fn apply(
    options: &Options,
    transport: &mut Target,
    target: &mut SessionTarget,
    request: Connection,
    events: &Events,
) {
    match request {
        Connection::Reconnect => {}
        Connection::Switch(next) => *target = next,
        Connection::Connect(next) => match connection_target(&options.target, &next) {
            Ok((next_transport, cwd, label)) => {
                *transport = next_transport;
                *target = SessionTarget {
                    cwd: Some(cwd),
                    session: None,
                };
                let _ = events.unbounded_send(Event::ConnectionChanged(label));
            }
            Err(error) => {
                let _ = events.unbounded_send(Event::Error(error));
            }
        },
    }
    let _ = events.unbounded_send(Event::Connecting);
}

fn connection_target(
    startup: &Target,
    next: &acp::ConnectTarget,
) -> Result<(Target, PathBuf, String), String> {
    if next.endpoint == "local" {
        let cwd = next
            .cwd
            .as_ref()
            .map_or_else(env::current_dir, std::path::absolute)
            .map_err(|error| error.to_string())?;
        let target = match startup {
            target @ (Target::Local { .. } | Target::Stdio { .. }) => target.clone(),
            Target::Remote(_) => Target::builtin(),
        };
        return Ok((target, cwd, "local".into()));
    }
    let uri = next
        .endpoint
        .parse::<tokio_tungstenite::tungstenite::http::Uri>()
        .map_err(|_| "Invalid ACP endpoint")?;
    let host = uri.host().ok_or("Invalid ACP endpoint")?;
    let loopback = matches!(host, "127.0.0.1" | "localhost" | "[::1]");
    if !(uri.scheme_str() == Some("wss") || uri.scheme_str() == Some("ws") && loopback)
        || uri
            .authority()
            .is_some_and(|authority| authority.as_str().contains('@'))
        || uri.query().is_some()
        || next.endpoint.contains('#')
    {
        return Err("Use a wss:// ACP endpoint without credentials, query or fragment (ws:// works on loopback)".into());
    }
    let cwd = next.cwd.clone().unwrap_or_else(|| PathBuf::from("/"));
    if !cwd.to_string_lossy().starts_with('/') {
        return Err("Enter an absolute remote workspace path".into());
    }
    Ok((Target::Remote(next.endpoint.clone()), cwd, host.to_owned()))
}

async fn connect(
    transport: &Target,
    startup: &Target,
    target: SessionTarget,
    actions: &mut Actions,
    events: Events,
    mut services: Services,
) -> Result<(), String> {
    if transport != startup {
        services.effort_profile = None;
    }
    match transport {
        Target::Local { command, args } => {
            services.managed_sessions = true;
            let output = tokio::process::Command::new(command)
                .args(args)
                .kill_on_drop(true)
                .output()
                .await
                .map_err(|error| format!("Could not start the local Harness server: {error}"))?;
            if !output.status.success() {
                return Err(String::from_utf8_lossy(&output.stderr).trim().to_owned());
            }
            #[derive(serde::Deserialize)]
            struct Endpoint {
                port: u16,
                token: String,
                store_id: String,
            }
            let endpoint: Endpoint = serde_json::from_slice(&output.stdout)
                .map_err(|_| "Invalid local Harness endpoint")?;
            services.local_provider_management = true;
            services.approval_scope = format!("store:{}", endpoint.store_id);
            let url = format!("ws://127.0.0.1:{}/acp/agent", endpoint.port);
            connect_websocket(
                &url,
                Some(&endpoint.token),
                target,
                actions,
                events,
                services,
            )
            .await
        }
        Target::Stdio { command, args } => {
            let identity =
                serde_json::to_string(&(command, args)).map_err(|error| error.to_string())?;
            services.approval_scope = format!("stdio:{identity}");
            services.local_provider_management = matches!(
                args.as_slice(),
                [module_flag, module, ..] if module_flag == "-m" && module == "hrns.server.runtime"
            ) || (matches!(
                Path::new(command)
                    .file_name()
                    .and_then(|name| name.to_str()),
                Some("hrns" | "hrns.exe")
            ) && args
                .first()
                .is_some_and(|arg| arg == "agent"));
            let agent = AcpAgent::new(AcpAgentConfig::new(command.clone()).args(args.clone()));
            acp::connect(agent, target, actions, events, services)
                .await
                .map_err(|error| error.message)
        }
        Target::Remote(url) => {
            services.remote = true;
            services.managed_sessions = true;
            services.approval_scope = format!("websocket:{url}");
            let token = matches!(startup, Target::Remote(original) if original == url)
                .then(|| env::var("HRNS_TUI_TOKEN").ok())
                .flatten();
            connect_websocket(url, token.as_deref(), target, actions, events, services).await
        }
    }
}

async fn connect_websocket(
    url: &str,
    token: Option<&str>,
    target: SessionTarget,
    actions: &mut Actions,
    events: Events,
    services: Services,
) -> Result<(), String> {
    let mut request = url
        .into_client_request()
        .map_err(|_| "Invalid ACP endpoint")?;
    let uri = request.uri();
    let loopback = matches!(uri.host(), Some("127.0.0.1" | "localhost" | "[::1]"));
    if uri.scheme_str() != Some("wss") && !loopback {
        return Err("Use wss:// for remote ACP connections".into());
    }
    if let Some(token) = token {
        let mut header = format!("Bearer {token}")
            .parse::<tokio_tungstenite::tungstenite::http::HeaderValue>()
            .map_err(|_| "Invalid ACP bearer token")?;
        header.set_sensitive(true);
        request.headers_mut().insert("authorization", header);
    }
    // Match the Harness WebSocket bridge's message limit even for unfragmented frames.
    let config = WebSocketConfig::default().max_frame_size(Some(64 * 1024 * 1024));
    let (ws, _) = tokio::time::timeout(
        Duration::from_secs(15),
        tokio_tungstenite::connect_async_with_config(request, Some(config), false),
    )
    .await
    .map_err(|_| "Timed out connecting to the ACP endpoint")?
    .map_err(|error| match error {
        tokio_tungstenite::tungstenite::Error::Http(response) => {
            format!("WebSocket upgrade rejected ({})", response.status())
        }
        _ => "Could not open the ACP WebSocket".into(),
    })?;
    let (transport, bridge) = Channel::duplex();
    let (wire, session) = futures::join!(
        socket::pump(ws, bridge, |text| Message::Text(text.into()), message_text),
        acp::connect(transport, target, actions, events, services),
    );
    wire?;
    session.map_err(|error| acp::request_error(&error))
}

fn message_text(message: Message) -> Result<Option<String>, String> {
    match message {
        Message::Text(text) => Ok(Some(text.to_string())),
        Message::Binary(_) => Err("The ACP endpoint sent a binary WebSocket frame".into()),
        Message::Close(Some(frame)) if frame.code != CloseCode::Normal => Err(format!(
            "ACP WebSocket closed ({}) {}",
            frame.code, frame.reason
        )),
        _ => Ok(None),
    }
}

fn finish(events: &Events, result: Result<(), String>) {
    if let Err(error) = result {
        let _ = events.unbounded_send(Event::Error(error));
    }
    let _ = events.unbounded_send(Event::Disconnected);
}

fn translate(key: KeyEvent) -> Option<Key> {
    match key.code {
        KeyCode::Enter if key.modifiers.contains(KeyModifiers::SHIFT) => Some(Key::Newline),
        KeyCode::Enter => Some(Key::Enter),
        KeyCode::Backspace => Some(Key::Backspace),
        KeyCode::Delete => Some(Key::Delete),
        KeyCode::Left if key.modifiers == KeyModifiers::SHIFT => Some(Key::EditQueuedPrompt),
        KeyCode::Left => Some(Key::Left),
        KeyCode::Right => Some(Key::Right),
        KeyCode::Home => Some(Key::Home),
        KeyCode::End if key.modifiers == KeyModifiers::CONTROL => Some(Key::LatestOutput),
        KeyCode::End => Some(Key::End),
        KeyCode::Up if key.modifiers == KeyModifiers::ALT => Some(Key::PreviousPrompt),
        KeyCode::Down if key.modifiers == KeyModifiers::ALT => Some(Key::NextPrompt),
        KeyCode::Up => Some(Key::HistoryUp),
        KeyCode::Down => Some(Key::HistoryDown),
        KeyCode::PageUp => Some(Key::ScrollUp),
        KeyCode::PageDown => Some(Key::ScrollDown),
        KeyCode::Char('b') if key.modifiers == KeyModifiers::CONTROL => Some(Key::ToggleSidebar),
        KeyCode::Char('o') if key.modifiers == KeyModifiers::CONTROL => Some(Key::ToggleTools),
        KeyCode::Char('r') if key.modifiers == KeyModifiers::CONTROL => Some(Key::Reconnect),
        KeyCode::Char('g') if key.modifiers == KeyModifiers::CONTROL => Some(Key::Agents),
        KeyCode::Char('n') if key.modifiers == KeyModifiers::CONTROL => Some(Key::NewSession),
        KeyCode::Esc => Some(Key::Escape),
        KeyCode::BackTab => Some(Key::BackTab),
        KeyCode::Tab => Some(Key::Tab),
        KeyCode::Char(c)
            if !key
                .modifiers
                .intersects(KeyModifiers::CONTROL | KeyModifiers::ALT) =>
        {
            Some(Key::Text(c))
        }
        _ => None,
    }
}
