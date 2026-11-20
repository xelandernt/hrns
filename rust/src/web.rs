use std::cell::RefCell;
use std::io;
use std::path::PathBuf;
use std::rc::Rc;

use agent_client_protocol::Channel;
use futures::{StreamExt, channel::mpsc};
use ratatui::Terminal;
use ratzilla::{DomBackend, WebRenderer};
use wasm_bindgen::{JsCast, closure::Closure};
use wasm_bindgen_futures::JsFuture;
use web_sys::{ClipboardEvent, HtmlElement, KeyboardEvent, Url, window};
use ws_stream_wasm::{WsErr, WsMessage, WsMeta};

use crate::acp::{self, Event, SessionTarget};
use crate::app::{App, Key};
use crate::socket;

pub fn run() -> Result<(), Box<dyn std::error::Error>> {
    let (action_tx, action_rx) = mpsc::unbounded();
    let (event_tx, mut event_rx) = mpsc::unbounded();
    let config = web_config();
    let label = config
        .as_ref()
        .map(|(endpoint, _)| endpoint.host())
        .unwrap_or_else(|_| "Remote".into());
    let app = Rc::new(RefCell::new(App::new(label, action_tx)));
    let terminal = Terminal::new(DomBackend::new_by_id("terminal")?)?;
    bind_input(Rc::clone(&app))?;
    terminal.draw_web(move |frame| {
        let mut app = app.borrow_mut();
        for _ in 0..256 {
            let Ok(update) = event_rx.try_recv() else {
                break;
            };
            app.event(update);
        }
        app.draw(frame);
    });

    wasm_bindgen_futures::spawn_local(async move {
        let result = async {
            let (endpoint, target) = config?;
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
                acp::connect(transport, target, action_rx, event_tx.clone()),
            );
            wire?;
            session.map_err(|error| error.message)
        }
        .await;
        if let Err(error) = result {
            let _ = event_tx.unbounded_send(Event::Error(error));
        }
        let _ = event_tx.unbounded_send(Event::Disconnected);
    });

    Ok(())
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

fn message_text(message: WsMessage) -> Result<Option<String>, String> {
    match message {
        WsMessage::Text(text) => Ok(Some(text)),
        WsMessage::Binary(_) => Err("The ACP endpoint sent a binary WebSocket frame".into()),
    }
}

fn bind_input(app: Rc<RefCell<App>>) -> io::Result<()> {
    let terminal = window()
        .and_then(|window| window.document())
        .and_then(|document| document.get_element_by_id("terminal"))
        .and_then(|element| element.dyn_into::<HtmlElement>().ok())
        .ok_or_else(|| io::Error::other("Terminal element unavailable"))?;
    let keyboard = {
        let app = Rc::clone(&app);
        Closure::<dyn FnMut(KeyboardEvent)>::new(move |event: KeyboardEvent| {
            if event.is_composing() || event.ctrl_key() || event.alt_key() || event.meta_key() {
                return;
            }
            if let Some(key) = translate(&event) {
                event.prevent_default();
                let copy = {
                    let mut app = app.borrow_mut();
                    app.key(key);
                    app.take_clipboard()
                };
                if let Some(text) = copy
                    && let Some(window) = window()
                {
                    let write = window.navigator().clipboard().write_text(&text);
                    let app = Rc::clone(&app);
                    wasm_bindgen_futures::spawn_local(async move {
                        if JsFuture::from(write).await.is_err() {
                            app.borrow_mut().event(Event::Error(
                                "Could not copy to the browser clipboard".into(),
                            ));
                        }
                    });
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

fn translate(event: &KeyboardEvent) -> Option<Key> {
    let key = event.key();
    match key.as_str() {
        "Enter" if event.shift_key() => Some(Key::Newline),
        "Enter" => Some(Key::Enter),
        "Backspace" => Some(Key::Backspace),
        "Delete" => Some(Key::Delete),
        "ArrowLeft" => Some(Key::Left),
        "ArrowRight" => Some(Key::Right),
        "Home" => Some(Key::Home),
        "End" => Some(Key::End),
        "ArrowUp" => Some(Key::HistoryUp),
        "ArrowDown" => Some(Key::HistoryDown),
        "PageUp" => Some(Key::ScrollUp),
        "PageDown" => Some(Key::ScrollDown),
        "Escape" => Some(Key::Escape),
        text => {
            let mut chars = text.chars();
            let character = chars.next()?;
            chars.next().is_none().then_some(Key::Text(character))
        }
    }
}
