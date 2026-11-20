use std::fmt::Display;

use agent_client_protocol::{Channel, TransportFrame};
use futures::{FutureExt, Sink, SinkExt, Stream, StreamExt, select};

pub async fn pump<Message, Socket, E>(
    socket: Socket,
    mut channel: Channel,
    encode: impl Fn(String) -> Message,
    decode: impl Fn(Message) -> Result<Option<String>, String>,
) -> Result<(), String>
where
    Socket: Stream<Item = Result<Message, E>> + Sink<Message, Error = E> + Unpin,
    E: Display,
{
    let (mut send, mut receive) = socket.split();
    loop {
        select! {
            inbound = receive.next().fuse() => {
                let Some(inbound) = inbound else { return Ok(()) };
                if let Some(text) = decode(inbound.map_err(|e| e.to_string())?)? {
                    let frame = TransportFrame::parse_json(&text);
                    if channel.tx.unbounded_send(frame).is_err() {
                        return Ok(());
                    }
                }
            }
            outbound = channel.rx.next().fuse() => {
                let Some(frame) = outbound else { return Ok(()) };
                let text = frame.to_json().map_err(|e| e.to_string())?;
                send.send(encode(text)).await.map_err(|e| e.to_string())?;
            }
        }
    }
}
