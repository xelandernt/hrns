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
use ratzilla::{CanvasBackend, WebRenderer};
use wasm_bindgen::{
    JsCast, JsValue, closure::Closure, convert::FromWasmAbi, prelude::wasm_bindgen,
};
use wasm_bindgen_futures::JsFuture;
use web_sys::{
    ClipboardEvent, Element, EventTarget, File, HtmlElement, HtmlInputElement, KeyboardEvent, Url,
    WheelEvent, window,
};
use ws_stream_wasm::{WsErr, WsMessage, WsMeta};

use crate::acp::{
    self, ClientIoMode, Connection, Event, Services, SessionConnection, SessionTarget,
};
use crate::app::{App, Key, LocalEffect, Startup};
use crate::attachments::{Attachment, MAX_FILE_BYTES};
use crate::socket;
use crate::transcript::ACTION_FRAME_MILLIS;

#[wasm_bindgen]
pub struct WebTerminal {
    display: Option<Display>,
    root: HtmlElement,
    app: Rc<RefCell<App>>,
    events: mpsc::UnboundedReceiver<Event>,
    requests: mpsc::UnboundedSender<Connection>,
    _input: InputBindings,
}

struct Display {
    terminal: Terminal<CanvasBackend>,
    canvas: Element,
    size: (u32, u32),
}

impl Drop for Display {
    fn drop(&mut self) {
        self.canvas.remove();
    }
}

#[wasm_bindgen]
impl WebTerminal {
    #[wasm_bindgen(constructor)]
    pub fn new(
        root: HtmlElement,
        input: HtmlInputElement,
        endpoint: Option<String>,
        cwd: Option<String>,
        session: Option<String>,
        theme: Option<String>,
    ) -> Result<WebTerminal, JsValue> {
        let (action_tx, action_rx) = mpsc::unbounded();
        let (event_tx, events) = mpsc::unbounded();
        let (requests, requests_rx) = mpsc::unbounded();
        let config = web_config(endpoint, cwd, session);
        let label = config
            .as_ref()
            .map(|(endpoint, _)| endpoint.host())
            .unwrap_or_else(|_| "Remote".into());
        let startup = Startup {
            client_io: ClientIoMode::Off,
            theme: theme.filter(|name| crate::theme::NAMES.contains(&name.as_str())),
        };
        let app = Rc::new(RefCell::new(App::new(
            label,
            action_tx,
            crate::preferences::load(),
            startup,
        )));
        let display = display_size(&root)
            .map(|size| Display::new(&root, size, Rc::clone(&app)))
            .transpose()
            .map_err(js_error)?;
        let input =
            bind_input(&root, input, Rc::clone(&app), requests.clone()).map_err(js_error)?;
        run_connection(config, action_rx, event_tx, requests_rx);
        Ok(Self {
            display,
            root,
            app,
            events,
            requests,
            _input: input,
        })
    }

    pub fn resize(&mut self) -> Result<(), JsValue> {
        let Some(size) = display_size(&self.root) else {
            self.display = None;
            return Ok(());
        };
        if self
            .display
            .as_ref()
            .is_none_or(|display| display.size != size)
        {
            self.display =
                Some(Display::new(&self.root, size, Rc::clone(&self.app)).map_err(js_error)?);
        }
        Ok(())
    }

    pub fn draw(&mut self) -> Result<(), JsValue> {
        let mut app = self.app.borrow_mut();
        for _ in 0..256 {
            let Ok(update) = self.events.try_recv() else {
                break;
            };
            app.event(update);
        }
        if let Some(effect) = app.take_local_effect() {
            apply_effect(&mut app, &self.requests, effect);
        }
        if let Some(display) = self.display.as_mut() {
            let animation_tick = js_sys::Date::now() as u128 / ACTION_FRAME_MILLIS;
            display
                .terminal
                .draw(|frame| app.draw(frame, animation_tick))
                .map_err(js_error)?;
        }
        Ok(())
    }
}

impl Drop for WebTerminal {
    fn drop(&mut self) {
        self.requests.close_channel();
    }
}

// Ratzilla's canvas uses a fixed 10 × 19 CSS pixel cell.
const CELL_WIDTH: i32 = 10;
const CELL_HEIGHT: i32 = 19;

fn display_size(root: &HtmlElement) -> Option<(u32, u32)> {
    let width = root.client_width();
    let height = root.client_height();
    (width >= CELL_WIDTH && height >= CELL_HEIGHT).then_some((
        width.min(i32::from(u16::MAX)) as u32,
        height.min(i32::from(u16::MAX)) as u32,
    ))
}

impl Display {
    fn new(
        root: &HtmlElement,
        size: (u32, u32),
        app: Rc<RefCell<App>>,
    ) -> Result<Self, Box<dyn std::error::Error>> {
        let backend = CanvasBackend::new_with_options(
            CanvasBackendOptions::new().grid_id(&root.id()).size(size),
        )?;
        let canvas = root
            .last_element_child()
            .ok_or_else(|| io::Error::other("Terminal canvas unavailable"))?;
        let mut display = Self {
            terminal: Terminal::new(backend)?,
            canvas,
            size,
        };
        display.terminal.on_mouse_event(move |event| {
            let mut app = app.borrow_mut();
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
        Ok(display)
    }
}

fn js_error(error: impl std::fmt::Display) -> JsValue {
    js_sys::Error::new(&error.to_string()).into()
}

fn apply_effect(app: &mut App, requests: &mpsc::UnboundedSender<Connection>, effect: LocalEffect) {
    match effect {
        LocalEffect::Reconnect => {
            let _ = requests.unbounded_send(Connection::Reconnect);
        }
        LocalEffect::Connect(target) => send_connect(app, requests, target),
        LocalEffect::Switch(target) => {
            let _ = requests.unbounded_send(Connection::Switch(target));
        }
        LocalEffect::SavePreferences(preferences) => crate::preferences::save(&preferences),
        LocalEffect::Copy(_) | LocalEffect::Attach(_) => {}
    }
}

fn run_connection(
    config: Result<(Url, SessionTarget), String>,
    mut action_rx: crate::acp::Actions,
    event_tx: crate::acp::Events,
    mut requests_rx: mpsc::UnboundedReceiver<Connection>,
) {
    wasm_bindgen_futures::spawn_local(async move {
        use futures::{FutureExt, select_biased};
        let (mut endpoint, target) = match config {
            Ok((endpoint, target)) => (Ok(endpoint), target),
            Err(error) => (
                Err(error),
                SessionTarget {
                    cwd: None,
                    session: None,
                },
            ),
        };
        let mut session = SessionConnection::new(target);
        loop {
            let request = {
                let (attempt_events, updates) = mpsc::unbounded();
                let mut updates = updates.fuse();
                let connection = connect_attempt(
                    endpoint.clone(),
                    session.begin_attempt(),
                    &mut action_rx,
                    attempt_events,
                )
                .fuse();
                futures::pin_mut!(connection);
                loop {
                    select_biased! {
                        request = requests_rx.next().fuse() => break Ok(request),
                        event = updates.select_next_some() => session.forward(event, &event_tx),
                        result = connection => break Err(result),
                    }
                }
            };
            let request = match request {
                Ok(request) => request,
                Err(result) => {
                    if session.finish(result, &event_tx) {
                        continue;
                    }
                    requests_rx.next().await
                }
            };
            match request {
                Some(Connection::Reconnect) => {}
                Some(Connection::Switch(next)) => session.switch(next, true),
                Some(Connection::Connect(next)) => match connection_target(&next) {
                    Ok((url, cwd)) => {
                        let _ = event_tx.unbounded_send(Event::ConnectionChanged(url.host()));
                        session.switch(
                            SessionTarget {
                                cwd: Some(cwd),
                                session: None,
                            },
                            false,
                        );
                        endpoint = Ok(url);
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
}

async fn connect_attempt(
    endpoint: Result<Url, String>,
    target: SessionTarget,
    actions: &mut crate::acp::Actions,
    events: crate::acp::Events,
) -> Result<(), String> {
    let endpoint = endpoint?;
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

fn web_config(
    endpoint: Option<String>,
    cwd: Option<String>,
    session: Option<String>,
) -> Result<(Url, SessionTarget), String> {
    let window = window().ok_or("Browser window unavailable")?;
    let page = window
        .location()
        .href()
        .map_err(|_| "Page URL unavailable")?;
    let page = Url::new(&page).map_err(|_| "Invalid page URL")?;
    let endpoint = match endpoint {
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
    let session = session.filter(|id| !id.is_empty());
    let cwd = cwd.or_else(|| session.is_none().then(|| "/".into()));
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

struct Listener<T: 'static> {
    target: EventTarget,
    event: &'static str,
    callback: Closure<dyn FnMut(T)>,
}

impl<T: FromWasmAbi + 'static> Listener<T> {
    fn new(
        target: impl Into<EventTarget>,
        event: &'static str,
        callback: impl FnMut(T) + 'static,
    ) -> io::Result<Self> {
        let target = target.into();
        let callback = Closure::<dyn FnMut(T)>::new(callback);
        target
            .add_event_listener_with_callback(event, callback.as_ref().unchecked_ref())
            .map_err(|_| io::Error::other(format!("Could not bind the {event} listener")))?;
        Ok(Self {
            target,
            event,
            callback,
        })
    }
}

impl<T: 'static> Drop for Listener<T> {
    fn drop(&mut self) {
        let _ = self.target.remove_event_listener_with_callback(
            self.event,
            self.callback.as_ref().unchecked_ref(),
        );
    }
}

struct InputBindings {
    _wheel: Listener<WheelEvent>,
    _change: Listener<web_sys::Event>,
    _keyboard: Listener<KeyboardEvent>,
    _paste: Listener<ClipboardEvent>,
}

fn bind_input(
    terminal: &HtmlElement,
    input: HtmlInputElement,
    app: Rc<RefCell<App>>,
    requests: mpsc::UnboundedSender<Connection>,
) -> io::Result<InputBindings> {
    let wheel = bind_wheel(terminal, Rc::clone(&app))?;
    let change = bind_file_chooser(&input, Rc::clone(&app))?;
    let keyboard = {
        let app = Rc::clone(&app);
        Listener::new(terminal.clone(), "keydown", move |event: KeyboardEvent| {
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
                    Some(LocalEffect::Attach(path)) if path.is_empty() => input.click(),
                    Some(LocalEffect::Attach(_)) => app.borrow_mut().event(Event::Warning(
                        "Use /attach to open the browser file chooser".into(),
                    )),
                    Some(effect) => apply_effect(&mut app.borrow_mut(), &requests, effect),
                    None => {}
                }
            }
        })?
    };
    let paste = Listener::new(terminal.clone(), "paste", move |event: ClipboardEvent| {
        if let Some(data) = event.clipboard_data()
            && let Ok(text) = data.get_data("text/plain")
        {
            event.prevent_default();
            app.borrow_mut().paste(&text);
        }
    })?;
    Ok(InputBindings {
        _wheel: wheel,
        _change: change,
        _keyboard: keyboard,
        _paste: paste,
    })
}

fn bind_wheel(terminal: &HtmlElement, app: Rc<RefCell<App>>) -> io::Result<Listener<WheelEvent>> {
    let mut remainder = 0.0;
    Listener::new(terminal.clone(), "wheel", move |event: WheelEvent| {
        if event.ctrl_key() || event.delta_y() == 0.0 {
            return;
        }
        // Ratzilla's canvas draws the terminal grid five CSS pixels inside the element.
        let x = f64::from(event.offset_x() - 5) / f64::from(CELL_WIDTH);
        let y = f64::from(event.offset_y() - 5) / f64::from(CELL_HEIGHT);
        if x < 0.0 || y < 0.0 {
            return;
        }
        let delta = match event.delta_mode() {
            WheelEvent::DOM_DELTA_LINE => event.delta_y(),
            WheelEvent::DOM_DELTA_PAGE => event.delta_y() * app.borrow().transcript_page_height(),
            _ => event.delta_y() / f64::from(CELL_HEIGHT),
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
    })
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

fn bind_file_chooser(
    input: &HtmlInputElement,
    app: Rc<RefCell<App>>,
) -> io::Result<Listener<web_sys::Event>> {
    let element = input.clone();
    Listener::new(input.clone(), "change", move |_| {
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
    })
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
            "n" => Some(Key::NewSession),
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
