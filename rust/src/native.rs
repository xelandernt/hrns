use std::env;
use std::io::{self, stdout};
use std::path::PathBuf;
use std::time::Duration;

use agent_client_protocol::{AcpAgent, AcpAgentConfig, Channel};
use crossterm::clipboard::CopyToClipboard;
use crossterm::event::{self, DisableBracketedPaste, EnableBracketedPaste};
use crossterm::event::{Event as TerminalEvent, KeyCode, KeyEvent, KeyEventKind, KeyModifiers};
use futures::channel::mpsc;
use tokio_tungstenite::tungstenite::Message;
use tokio_tungstenite::tungstenite::client::IntoClientRequest;

use crate::acp::{self, Actions, Event, Events, SessionTarget};
use crate::app::{App, Key, LocalEffect};
use crate::attachments;
use crate::socket;

const HELP: &str =
    "Usage: hrns-tui [--cwd PATH] [--session ID] [--server URL | --stdio COMMAND [ARGS...]]
       Native WebSocket authentication reads HRNS_TUI_TOKEN.
       With no transport option, starts the built-in hrns agent.";

enum Target {
    Remote(String),
    Stdio { command: String, args: Vec<String> },
}

struct Options {
    session: SessionTarget,
    target: Target,
}

impl Options {
    fn parse() -> Result<Option<Self>, String> {
        let mut argv = env::args().skip(1);
        let mut cwd = None;
        let mut target = None;
        let mut session = None;
        while let Some(arg) = argv.next() {
            match arg.as_str() {
                "--help" | "-h" => return Ok(None),
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
                _ => return Err(format!("Unknown or repeated option {arg:?}\n{HELP}")),
            }
        }
        let cwd = match cwd {
            Some(cwd) if matches!(target, Some(Target::Remote(_))) => Some(cwd),
            Some(cwd) => Some(std::path::absolute(cwd).map_err(|error| error.to_string())?),
            None if session.is_some() && matches!(target, Some(Target::Remote(_))) => None,
            None => Some(env::current_dir().map_err(|error| error.to_string())?),
        };
        let target = target.unwrap_or_else(|| Target::Stdio {
            command: "hrns".into(),
            args: vec!["agent".into()],
        });
        Ok(Some(Self {
            session: SessionTarget { cwd, session },
            target,
        }))
    }

    fn label(&self) -> String {
        let location = match &self.target {
            Target::Remote(url) => url
                .split_once("://")
                .and_then(|(_, rest)| rest.split(['/', '?', '#']).next())
                .and_then(|authority| authority.rsplit('@').next())
                .unwrap_or("remote"),
            Target::Stdio { .. } => "local",
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
    let (reconnect_tx, reconnect_rx) = mpsc::unbounded();
    let destination = options.label();
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
                    reconnect_rx,
                    stop_rx,
                ));
            });
        if let Err(error) = result {
            let _ = event_tx.unbounded_send(Event::Error(error));
            let _ = event_tx.unbounded_send(Event::Disconnected);
        }
    });
    let mut app = App::new(destination, action_tx);
    let result = (|| -> io::Result<()> {
        crossterm::execute!(stdout(), EnableBracketedPaste)?;
        let mut dirty = true;
        loop {
            for _ in 0..256 {
                let Ok(update) = event_rx.try_recv() else {
                    break;
                };
                app.event(update);
                dirty = true;
            }
            if dirty {
                terminal.draw(|frame| app.draw(frame))?;
                dirty = false;
            }
            if !event::poll(Duration::from_millis(16))? {
                continue;
            }
            match event::read()? {
                TerminalEvent::Key(key) if key.kind != KeyEventKind::Release => {
                    if key.code == KeyCode::Char('c')
                        && key.modifiers.contains(KeyModifiers::CONTROL)
                    {
                        break;
                    }
                    if let Some(key) = translate(key) {
                        app.key(key);
                        if let Some(effect) = app.take_local_effect() {
                            local_effect(effect, &mut app, &local_events, &reconnect_tx);
                        }
                        dirty = true;
                    }
                }
                TerminalEvent::Paste(text) => {
                    app.paste(&text);
                    dirty = true;
                }
                TerminalEvent::Resize(_, _) => dirty = true,
                _ => {}
            }
        }
        Ok(())
    })();
    let paste = crossterm::execute!(stdout(), DisableBracketedPaste);
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
    reconnect: &mpsc::UnboundedSender<()>,
) {
    match effect {
        LocalEffect::Reconnect => {
            let _ = reconnect.unbounded_send(());
        }
        LocalEffect::Copy(text) => {
            if crossterm::execute!(stdout(), CopyToClipboard::to_clipboard_from(text)).is_err() {
                app.event(Event::Error(
                    "Could not copy to the terminal clipboard".into(),
                ));
            }
        }
        LocalEffect::Attach(path) if path.is_empty() => {
            app.event(Event::Note("Use /attach <path> to select a file".into()));
        }
        LocalEffect::Attach(path) => {
            app.event(Event::LoadingAttachment);
            let events = events.clone();
            std::thread::spawn(move || {
                let expanded = path.strip_prefix("~/").and_then(|relative| {
                    env::var_os("HOME").map(|home| PathBuf::from(home).join(relative))
                });
                let path = expanded
                    .as_ref()
                    .and_then(|path| path.to_str())
                    .unwrap_or(&path);
                let attachment = attachments::read_native(path)
                    .map(Box::new)
                    .map_err(|error| format!("Could not attach file: {error}"));
                let _ = events.unbounded_send(Event::Attachment(attachment));
            });
        }
    }
}

async fn supervise(
    options: &Options,
    actions: &mut Actions,
    events: Events,
    mut reconnect: mpsc::UnboundedReceiver<()>,
    mut stop: mpsc::UnboundedReceiver<()>,
) {
    use futures::{FutureExt, StreamExt, select_biased};
    loop {
        select_biased! {
            _ = stop.next() => return,
            result = connect(options, actions, events.clone()).fuse() => finish(&events, result),
        }
        select_biased! {
            _ = stop.next() => return,
            request = reconnect.next() => if request.is_none() {
                return;
            },
        }
        let _ = events.unbounded_send(Event::Connecting);
    }
}

async fn connect(options: &Options, actions: &mut Actions, events: Events) -> Result<(), String> {
    match &options.target {
        Target::Stdio { command, args } => {
            let agent = AcpAgent::new(AcpAgentConfig::new(command.clone()).args(args.clone()));
            acp::connect(agent, options.session.clone(), actions, events)
                .await
                .map_err(|error| error.message)
        }
        Target::Remote(url) => {
            let mut request = url
                .as_str()
                .into_client_request()
                .map_err(|_| "Invalid ACP endpoint")?;
            let uri = request.uri();
            let loopback = matches!(uri.host(), Some("127.0.0.1" | "localhost" | "[::1]"));
            if uri.scheme_str() != Some("wss") && !loopback {
                return Err("Use wss:// for remote ACP connections".into());
            }
            if let Ok(token) = env::var("HRNS_TUI_TOKEN") {
                let mut header = format!("Bearer {token}")
                    .parse::<tokio_tungstenite::tungstenite::http::HeaderValue>()
                    .map_err(|_| "Invalid ACP bearer token")?;
                header.set_sensitive(true);
                request.headers_mut().insert("authorization", header);
            }
            let (ws, _) = tokio::time::timeout(
                Duration::from_secs(15),
                tokio_tungstenite::connect_async(request),
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
                acp::connect(transport, options.session.clone(), actions, events),
            );
            wire?;
            session.map_err(|error| error.message)
        }
    }
}

fn message_text(message: Message) -> Result<Option<String>, String> {
    match message {
        Message::Text(text) => Ok(Some(text.to_string())),
        Message::Binary(_) => Err("The ACP endpoint sent a binary WebSocket frame".into()),
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
        KeyCode::Left => Some(Key::Left),
        KeyCode::Right => Some(Key::Right),
        KeyCode::Home => Some(Key::Home),
        KeyCode::End => Some(Key::End),
        KeyCode::Up => Some(Key::HistoryUp),
        KeyCode::Down => Some(Key::HistoryDown),
        KeyCode::PageUp => Some(Key::ScrollUp),
        KeyCode::PageDown => Some(Key::ScrollDown),
        KeyCode::Char('r') if key.modifiers == KeyModifiers::CONTROL => Some(Key::Reconnect),
        KeyCode::Esc => Some(Key::Escape),
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
