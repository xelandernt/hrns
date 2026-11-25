use std::cell::RefCell;
use std::io;
use std::path::PathBuf;
use std::rc::Rc;

use agent_client_protocol::Channel;
use futures::{StreamExt, channel::mpsc};
use ratatui::Terminal;
use ratatui::layout::Position;
use ratzilla::backend::canvas::CanvasBackendOptions;
use ratzilla::event::{MouseButton, MouseEventKind};
use ratzilla::{CanvasBackend, CellSized, WebRenderer};
use wasm_bindgen::{JsCast, closure::Closure};
use wasm_bindgen_futures::JsFuture;
use web_sys::{
    ClipboardEvent, File, HtmlElement, HtmlInputElement, KeyboardEvent, Url, WheelEvent, window,
};
use ws_stream_wasm::{WsErr, WsMessage, WsMeta};

use crate::acp::{self, ClientIoMode, Connection, Event, Services, SessionTarget};
use crate::app::{App, Key, LocalEffect, Startup};
use crate::attachments::{Attachment, MAX_FILE_BYTES};
use crate::socket;

pub fn run() -> Result<(), Box<dyn std::error::Error>> {
    let (action_tx, mut action_rx) = mpsc::unbounded();
    let (event_tx, mut event_rx) = mpsc::unbounded();
    let (requests_tx, mut requests_rx) = mpsc::unbounded();
    let config = web_config();
    let label = config
        .as_ref()
        .map(|(endpoint, _)| endpoint.host())
        .unwrap_or_else(|_| "Remote".into());
    let startup = startup_config();
    let app = Rc::new(RefCell::new(App::new(
        label,
        action_tx,
        crate::preferences::load(),
        startup,
    )));
    let backend = CanvasBackend::new_with_options(CanvasBackendOptions::new().grid_id("terminal"))?;
    let cell_size = backend.cell_size_css_px();
    let mut terminal = Terminal::new(backend)?;
    let pointer_app = Rc::clone(&app);
    terminal.on_mouse_event(move |event| {
        let mut app = pointer_app.borrow_mut();
        let position = Position::new(event.col, event.row);
        match event.kind {
            MouseEventKind::Moved => {
                app.pointer_drag(position);
                app.pointer_move(position);
            }
            MouseEventKind::ButtonDown(MouseButton::Left) => {
                app.pointer_down(position);
            }
            MouseEventKind::ButtonUp(MouseButton::Left) => {
                app.pointer_up(position);
            }
            _ => {}
        }
    })?;
    bind_input(Rc::clone(&app), requests_tx.clone(), cell_size)?;
    terminal.draw_web(move |frame| {
        let mut app = app.borrow_mut();
        for _ in 0..256 {
            let Ok(update) = event_rx.try_recv() else {
                break;
            };
            app.event(update);
        }
        if let Some(effect) = app.take_local_effect() {
            match effect {
                LocalEffect::Reconnect => {
                    let _ = requests_tx.unbounded_send(Connection::Reconnect);
                }
                LocalEffect::Connect(target) => {
                    send_connect(&mut app, &requests_tx, target);
                }
                LocalEffect::Switch(target) => {
                    let _ = requests_tx.unbounded_send(Connection::Switch(target));
                }
                LocalEffect::SavePreferences(preferences) => {
                    crate::preferences::save(&preferences);
                }
                LocalEffect::Copy(_) | LocalEffect::Attach(_) => {}
            }
        }
        app.draw(frame);
    });

    wasm_bindgen_futures::spawn_local(async move {
        use futures::{FutureExt, select_biased};
        let mut config = config;
        let mut target =
            config
                .as_ref()
                .map(|(_, target)| target.clone())
                .unwrap_or(SessionTarget {
                    cwd: None,
                    session: None,
                });
        loop {
            let request = select_biased! {
                request = requests_rx.next().fuse() => request,
                result = connect_attempt(
                    config.clone(),
                    target.clone(),
                    &mut action_rx,
                    event_tx.clone(),
                ).fuse() => {
                    if let Err(error) = result {
                        let _ = event_tx.unbounded_send(Event::Error(error));
                    }
                    let _ = event_tx.unbounded_send(Event::Disconnected);
                    requests_rx.next().await
                }
            };
            match request {
                Some(Connection::Reconnect) => {}
                Some(Connection::Switch(next)) => target = next,
                Some(Connection::Connect(next)) => match connection_target(&next) {
                    Ok((endpoint, cwd)) => {
                        let _ = event_tx.unbounded_send(Event::ConnectionChanged(endpoint.host()));
                        target = SessionTarget {
                            cwd: Some(cwd),
                            session: None,
                        };
                        config = Ok((endpoint, target.clone()));
                    }
                    Err(error) => {
                        let _ = event_tx.unbounded_send(Event::Error(error));
                    }
                },
                None => return,
            }
            let _ = event_tx.unbounded_send(Event::Connecting);
        }
    });

    Ok(())
}

async fn connect_attempt(
    config: Result<(Url, SessionTarget), String>,
    target: SessionTarget,
    actions: &mut crate::acp::Actions,
    events: crate::acp::Events,
) -> Result<(), String> {
    let (endpoint, _) = config?;
    let (_, ws) = WsMeta::connect(endpoint.href(), None)
        .await
        .map_err(|_| "Could not open the remote ACP WebSocket".to_owned())?;
    let (transport, bridge) = Channel::duplex();
    let (wire, session) = futures::join!(
        socket::pump(
            ws.map(Ok::<_, WsErr>),
            bridge,
            WsMessage::Text,
            message_text
        ),
        acp::connect(
            transport,
            target,
            actions,
            events,
            Services {
                client_io: ClientIoMode::Off,
                approval_scope: format!("websocket:{}", endpoint.href()),
                remote: true,
                ..Services::default()
            },
        ),
    );
    wire?;
    session.map_err(|error| acp::request_error(&error))
}

fn startup_config() -> Startup {
    let Some(window) = window() else {
        return Startup::default();
    };
    let Ok(search) = window.location().search() else {
        return Startup::default();
    };
    let query = web_sys::UrlSearchParams::new_with_str(&search).ok();
    let theme = query
        .as_ref()
        .and_then(|query| query.get("theme"))
        .filter(|name| crate::theme::NAMES.contains(&name.as_str()));
    Startup {
        client_io: ClientIoMode::Off,
        theme,
    }
}

fn web_config() -> Result<(Url, SessionTarget), String> {
    let window = window().ok_or("Browser window unavailable")?;
    let page = window
        .location()
        .href()
        .map_err(|_| "Page URL unavailable")?;
    let page = Url::new(&page).map_err(|_| "Invalid page URL")?;
    let query = page.search_params();
    let endpoint = match query.get("endpoint") {
        Some(endpoint) => Url::new(&endpoint).map_err(|_| "Invalid ACP endpoint")?,
        None => {
            let endpoint = Url::new_with_base("/acp/agent", &page.href())
                .map_err(|_| "Could not construct an ACP endpoint")?;
            endpoint.set_protocol(if page.protocol() == "https:" {
                "wss:"
            } else {
                "ws:"
            });
            endpoint
        }
    };
    let endpoint = validate_endpoint(endpoint)?;
    endpoint.set_hash("");
    let session = query.get("session").filter(|id| !id.is_empty());
    let cwd = query
        .get("cwd")
        .or_else(|| session.is_none().then(|| "/".into()));
    if cwd.as_ref().is_some_and(|path| !path.starts_with('/')) {
        return Err("Enter an absolute remote workspace path".into());
    }
    Ok((
        endpoint,
        SessionTarget {
            cwd: cwd.map(PathBuf::from),
            session,
        },
    ))
}

fn send_connect(
    app: &mut App,
    requests: &mpsc::UnboundedSender<Connection>,
    target: acp::ConnectTarget,
) {
    match connection_target(&target) {
        Ok(_) => {
            let _ = requests.unbounded_send(Connection::Connect(target));
        }
        Err(error) => app.event(Event::Error(error)),
    }
}

fn connection_target(next: &acp::ConnectTarget) -> Result<(Url, PathBuf), String> {
    let endpoint = Url::new(&next.endpoint)
        .map_err(|_| "Enter a secure ACP WebSocket URL".to_owned())
        .and_then(validate_endpoint)?;
    let cwd = next.cwd.clone().unwrap_or_else(|| PathBuf::from("/"));
    // Browser paths are remote POSIX paths, including on Windows.
    if !cwd.to_string_lossy().starts_with('/') {
        return Err("Enter an absolute remote workspace path".into());
    }
    if !endpoint.search().is_empty() || !endpoint.hash().is_empty() {
        return Err("Enter an ACP endpoint without a query or fragment".into());
    }
    Ok((endpoint, cwd))
}

fn validate_endpoint(endpoint: Url) -> Result<Url, String> {
    let supported = match endpoint.protocol().as_str() {
        "wss:" => true,
        "ws:" => matches!(
            endpoint.hostname().as_str(),
            "localhost" | "127.0.0.1" | "[::1]"
        ),
        _ => false,
    };
    if !supported || !endpoint.username().is_empty() || !endpoint.password().is_empty() {
        return Err("Use a wss:// endpoint without embedded credentials (ws:// is allowed for loopback development)".into());
    }
    Ok(endpoint)
}

fn message_text(message: WsMessage) -> Result<Option<String>, String> {
    match message {
        WsMessage::Text(text) => Ok(Some(text)),
        WsMessage::Binary(_) => Err("The ACP endpoint sent a binary WebSocket frame".into()),
    }
}

fn bind_input(
    app: Rc<RefCell<App>>,
    requests: mpsc::UnboundedSender<Connection>,
    cell_size: (f32, f32),
) -> io::Result<()> {
    let terminal = window()
        .and_then(|window| window.document())
        .and_then(|document| document.get_element_by_id("terminal"))
        .and_then(|element| element.dyn_into::<HtmlElement>().ok())
        .ok_or_else(|| io::Error::other("Terminal element unavailable"))?;
    bind_wheel(&terminal, Rc::clone(&app), cell_size)?;
    let input = bind_file_chooser(Rc::clone(&app))?;
    let keyboard = {
        let app = Rc::clone(&app);
        let reconnect = requests.clone();
        Closure::<dyn FnMut(KeyboardEvent)>::new(move |event: KeyboardEvent| {
            if event.is_composing() {
                return;
            }
            if let Some(key) = translate(&event) {
                event.prevent_default();
                let effect = {
                    let mut app = app.borrow_mut();
                    app.key(key);
                    app.take_local_effect()
                };
                match effect {
                    Some(LocalEffect::Copy(text)) => copy(&app, &text),
                    Some(LocalEffect::Reconnect) => {
                        let _ = reconnect.unbounded_send(Connection::Reconnect);
                    }
                    Some(LocalEffect::Connect(target)) => {
                        send_connect(&mut app.borrow_mut(), &reconnect, target);
                    }
                    Some(LocalEffect::Switch(target)) => {
                        let _ = reconnect.unbounded_send(Connection::Switch(target));
                    }
                    Some(LocalEffect::SavePreferences(preferences)) => {
                        crate::preferences::save(&preferences);
                    }
                    Some(LocalEffect::Attach(path)) if path.is_empty() => input.click(),
                    Some(LocalEffect::Attach(_)) => app.borrow_mut().event(Event::Warning(
                        "Use /attach to open the browser file chooser".into(),
                    )),
                    None => {}
                }
            }
        })
    };
    let paste = Closure::<dyn FnMut(ClipboardEvent)>::new(move |event: ClipboardEvent| {
        if let Some(data) = event.clipboard_data() {
            if let Ok(text) = data.get_data("text/plain") {
                event.prevent_default();
                app.borrow_mut().paste(&text);
            }
        }
    });
    terminal
        .add_event_listener_with_callback("keydown", keyboard.as_ref().unchecked_ref())
        .map_err(|_| io::Error::other("Could not bind terminal keyboard input"))?;
    keyboard.forget();
    terminal
        .add_event_listener_with_callback("paste", paste.as_ref().unchecked_ref())
        .map_err(|_| io::Error::other("Could not bind terminal clipboard input"))?;
    paste.forget();
    let _ = terminal.focus();
    Ok(())
}

fn bind_wheel(
    terminal: &HtmlElement,
    app: Rc<RefCell<App>>,
    (width, height): (f32, f32),
) -> io::Result<()> {
    let canvas = terminal
        .query_selector("canvas")
        .ok()
        .flatten()
        .ok_or_else(|| io::Error::other("Terminal canvas unavailable"))?;
    let mut remainder = 0.0;
    let wheel = Closure::<dyn FnMut(WheelEvent)>::new(move |event: WheelEvent| {
        if event.ctrl_key() || event.delta_y() == 0.0 {
            return;
        }
        // Ratzilla's canvas draws the terminal grid five CSS pixels inside the element.
        let x = f64::from(event.offset_x() - 5) / f64::from(width);
        let y = f64::from(event.offset_y() - 5) / f64::from(height);
        if x < 0.0 || y < 0.0 {
            return;
        }
        let delta = match event.delta_mode() {
            WheelEvent::DOM_DELTA_LINE => event.delta_y(),
            WheelEvent::DOM_DELTA_PAGE => event.delta_y() * app.borrow().transcript_page_height(),
            _ => event.delta_y() / f64::from(height),
        };
        remainder += delta;
        let rows = remainder.trunc() as i32;
        if rows == 0 {
            return;
        }
        remainder -= f64::from(rows);
        if app
            .borrow_mut()
            .pointer_scroll(Position::new(x as u16, y as u16), rows)
        {
            event.prevent_default();
        }
    });
    canvas
        .add_event_listener_with_callback("wheel", wheel.as_ref().unchecked_ref())
        .map_err(|_| io::Error::other("Could not bind the terminal wheel"))?;
    wheel.forget();
    Ok(())
}

fn copy(app: &Rc<RefCell<App>>, text: &str) {
    let Some(window) = window() else {
        return;
    };
    let write = window.navigator().clipboard().write_text(text);
    let app = Rc::clone(app);
    wasm_bindgen_futures::spawn_local(async move {
        if JsFuture::from(write).await.is_err() {
            app.borrow_mut().event(Event::Error(
                "Could not copy to the browser clipboard".into(),
            ));
        }
    });
}

fn bind_file_chooser(app: Rc<RefCell<App>>) -> io::Result<HtmlInputElement> {
    let input = window()
        .and_then(|window| window.document())
        .and_then(|document| document.get_element_by_id("attachment"))
        .and_then(|element| element.dyn_into::<HtmlInputElement>().ok())
        .ok_or_else(|| io::Error::other("File chooser unavailable"))?;
    let element = input.clone();
    let change = Closure::<dyn FnMut()>::new(move || {
        let Some(file) = element.files().and_then(|files| files.get(0)) else {
            return;
        };
        element.set_value("");
        app.borrow_mut().event(Event::LoadingAttachment);
        let app = Rc::clone(&app);
        wasm_bindgen_futures::spawn_local(async move {
            let attachment = read_file(file).await.map(Box::new);
            app.borrow_mut().event(Event::Attachment(attachment));
        });
    });
    input
        .add_event_listener_with_callback("change", change.as_ref().unchecked_ref())
        .map_err(|_| io::Error::other("Could not bind file chooser"))?;
    change.forget();
    Ok(input)
}

async fn read_file(file: File) -> Result<Attachment, String> {
    if file.size() > MAX_FILE_BYTES as f64 {
        return Err("Attachment exceeds the 8 MiB file limit".into());
    }
    let data = JsFuture::from(file.array_buffer())
        .await
        .map_err(|_| "Could not read the selected file")?;
    let data = js_sys::Uint8Array::new(&data).to_vec();
    let name = file.name();
    let path = format!("/uploads/{name}");
    Attachment::new(name, &path, Some(file.type_()), data)
}

fn translate(event: &KeyboardEvent) -> Option<Key> {
    let key = event.key();
    if event.meta_key() || (event.ctrl_key() && event.alt_key()) {
        return None;
    }
    if event.alt_key() {
        return match key.as_str() {
            "ArrowUp" => Some(Key::PreviousPrompt),
            "ArrowDown" => Some(Key::NextPrompt),
            _ => None,
        };
    }
    if event.ctrl_key() {
        return match key.as_str() {
            "r" => Some(Key::Reconnect),
            "o" => Some(Key::ToggleTools),
            "b" => Some(Key::ToggleSidebar),
            "g" => Some(Key::Agents),
            "End" => Some(Key::LatestOutput),
            _ => None,
        };
    }
    match key.as_str() {
        "Enter" if event.shift_key() => Some(Key::Newline),
        "Enter" => Some(Key::Enter),
        "Backspace" => Some(Key::Backspace),
        "Delete" => Some(Key::Delete),
        "ArrowLeft" if event.shift_key() => Some(Key::EditQueuedPrompt),
        "ArrowLeft" => Some(Key::Left),
        "ArrowRight" => Some(Key::Right),
        "Home" => Some(Key::Home),
        "End" => Some(Key::End),
        "ArrowUp" => Some(Key::HistoryUp),
        "ArrowDown" => Some(Key::HistoryDown),
        "PageUp" => Some(Key::ScrollUp),
        "PageDown" => Some(Key::ScrollDown),
        "Escape" => Some(Key::Escape),
        "Tab" if event.shift_key() => Some(Key::BackTab),
        "Tab" => Some(Key::Tab),
        text => {
            let mut chars = text.chars();
            let character = chars.next()?;
            chars.next().is_none().then_some(Key::Text(character))
        }
    }
}
