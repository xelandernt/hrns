#[cfg(not(target_family = "wasm"))]
use std::sync::Arc;

#[cfg(not(target_family = "wasm"))]
use agent_client_protocol::JsonRpcResponse;
use agent_client_protocol::v1::SessionMessage;
#[cfg(not(target_family = "wasm"))]
use agent_client_protocol::v1::schema::TerminalId;
use agent_client_protocol::v1::schema::{
    CreateElicitationRequest, CreateElicitationResponse, CreateTerminalRequest,
    CreateTerminalResponse, KillTerminalRequest, KillTerminalResponse, ReadTextFileRequest,
    ReadTextFileResponse, ReleaseTerminalRequest, ReleaseTerminalResponse,
    RequestPermissionRequest, RequestPermissionResponse, SessionNotification, StopReason,
    TerminalOutputRequest, TerminalOutputResponse, WaitForTerminalExitRequest,
    WaitForTerminalExitResponse, WriteTextFileRequest, WriteTextFileResponse,
};
use agent_client_protocol::{Dispatch, Error, Responder, util::MatchDispatch};
use futures::FutureExt;
use futures::channel::oneshot;

#[cfg(not(target_family = "wasm"))]
use crate::client_io::ClientIo;
use crate::elicitation;

#[cfg(not(target_family = "wasm"))]
use super::rpc::{exit_status, wait_for_terminal_exit, with_client_io, with_client_io_blocking};
use super::tools::text_update;
use super::{Event, PermissionChoice, PermissionRequest, SessionRunner};

impl SessionRunner<'_> {
    pub(super) async fn handle_update(&mut self, update: SessionMessage) -> Result<(), Error> {
        match update {
            SessionMessage::PromptResponse(result) => {
                let interrupted = match result {
                    Ok(response) => response.stop_reason == StopReason::Cancelled,
                    Err(error) => {
                        self.send_event(Event::Error(error.message));
                        true
                    }
                };
                self.send_event(Event::End { interrupted });
            }
            SessionMessage::Message(dispatch) => {
                self.dispatch_session_message(dispatch.into_raw()).await?;
            }
            _ => {}
        }
        Ok(())
    }

    async fn dispatch_session_message(&mut self, dispatch: Dispatch) -> Result<(), Error> {
        MatchDispatch::new(dispatch)
            .if_notification(async |notification: SessionNotification| {
                if let Some(event) = text_update(notification.update, &mut self.tools) {
                    self.send_event(event);
                }
                Ok(())
            })
            .await
            .if_request(async |request: RequestPermissionRequest, responder| {
                self.enqueue_permission(request, responder);
                Ok(())
            })
            .await
            .if_request(async |request: CreateElicitationRequest, responder| {
                self.enqueue_elicitation(request, responder)
            })
            .await
            .if_request(async |request: ReadTextFileRequest, responder| {
                self.handle_read_text_file(request, responder)
            })
            .await
            .if_request(async |request: WriteTextFileRequest, responder| {
                self.handle_write_text_file(request, responder)
            })
            .await
            .if_request(async |request: CreateTerminalRequest, responder| {
                self.handle_create_terminal(request, responder)
            })
            .await
            .if_request(async |request: TerminalOutputRequest, responder| {
                self.handle_terminal_output(request, responder)
            })
            .await
            .if_request(async |request: WaitForTerminalExitRequest, responder| {
                self.handle_wait_for_terminal_exit(request, responder)
            })
            .await
            .if_request(async |request: KillTerminalRequest, responder| {
                self.handle_kill_terminal(request, responder)
            })
            .await
            .if_request(async |request: ReleaseTerminalRequest, responder| {
                self.handle_release_terminal(request, responder)
            })
            .await
            .otherwise_ignore()
    }

    fn enqueue_permission(
        &mut self,
        request: RequestPermissionRequest,
        responder: Responder<RequestPermissionResponse>,
    ) {
        let (sender, receiver) = oneshot::channel();
        let tool_call_id = request.tool_call.tool_call_id.to_string();
        let mut state = self.tools.get(&tool_call_id).cloned().unwrap_or_default();
        state.apply(&request.tool_call);
        let mut display = state.display();
        let operation = if display.shell {
            "Run"
        } else {
            display.title.as_str()
        };
        let mut title = if display.subject.is_empty() {
            operation.to_owned()
        } else {
            format!("{operation} {}", display.subject)
        };
        title.retain(|ch| !ch.is_control());
        display.prepare_detail();
        self.send_event(Event::Permission(PermissionRequest {
            tool_call_id,
            title,
            kind: request.tool_call.fields.kind,
            detail: display.detail,
            shell: display.shell,
            options: request
                .options
                .iter()
                .map(|option| PermissionChoice {
                    name: option.name.clone(),
                    kind: option.kind,
                })
                .collect(),
            respond: sender,
        }));
        self.permissions
            .push(async move { (request, responder, receiver.await.ok().flatten()) }.boxed_local());
    }

    fn enqueue_elicitation(
        &mut self,
        request: CreateElicitationRequest,
        responder: Responder<CreateElicitationResponse>,
    ) -> Result<(), Error> {
        let Some(form) = elicitation::display(&request) else {
            return responder.respond(elicitation::decline());
        };
        let (sender, receiver) = oneshot::channel();
        self.send_event(Event::Elicitation {
            form,
            respond: sender,
        });
        self.elicitations
            .push(async move { (responder, receiver.await.ok().flatten()) }.boxed_local());
        Ok(())
    }

    #[cfg(not(target_family = "wasm"))]
    fn spawn_blocking_client_io<T, R>(
        &self,
        responder: Responder<R>,
        operation: impl FnOnce(&mut ClientIo) -> Result<T, String> + Send + 'static,
        response: impl FnOnce(T) -> R + Send + 'static,
    ) -> Result<(), Error>
    where
        T: Send + 'static,
        R: JsonRpcResponse + Send + 'static,
    {
        let io = Arc::clone(&self.io);
        self.session.connection().spawn(async move {
            let result = with_client_io_blocking(io, operation).await;
            let _ = match result {
                Ok(value) => responder.respond(response(value)),
                Err(error) => responder.respond_with_error(error),
            };
            Ok(())
        })
    }

    fn handle_read_text_file(
        &self,
        request: ReadTextFileRequest,
        responder: Responder<ReadTextFileResponse>,
    ) -> Result<(), Error> {
        #[cfg(not(target_family = "wasm"))]
        {
            self.spawn_blocking_client_io(
                responder,
                move |io| {
                    io.read(
                        &request.session_id.to_string(),
                        &request.path,
                        request.line,
                        request.limit,
                    )
                },
                ReadTextFileResponse::new,
            )
        }
        #[cfg(target_family = "wasm")]
        {
            let _ = request;
            responder.respond_with_error(Error::method_not_found())
        }
    }

    fn handle_write_text_file(
        &self,
        request: WriteTextFileRequest,
        responder: Responder<WriteTextFileResponse>,
    ) -> Result<(), Error> {
        #[cfg(not(target_family = "wasm"))]
        {
            self.spawn_blocking_client_io(
                responder,
                move |io| {
                    io.write(
                        &request.session_id.to_string(),
                        &request.path,
                        &request.content,
                    )
                },
                |()| WriteTextFileResponse::new(),
            )
        }
        #[cfg(target_family = "wasm")]
        {
            let _ = request;
            responder.respond_with_error(Error::method_not_found())
        }
    }

    fn handle_create_terminal(
        &self,
        request: CreateTerminalRequest,
        responder: Responder<CreateTerminalResponse>,
    ) -> Result<(), Error> {
        #[cfg(not(target_family = "wasm"))]
        {
            self.spawn_blocking_client_io(
                responder,
                move |io| {
                    let env = request
                        .env
                        .iter()
                        .map(|variable| (variable.name.clone(), variable.value.clone()))
                        .collect::<Vec<_>>();
                    io.create_terminal(
                        &request.session_id.to_string(),
                        &request.command,
                        &request.args,
                        &env,
                        request.cwd.as_deref(),
                        request.output_byte_limit,
                    )
                },
                |id| CreateTerminalResponse::new(TerminalId::new(id)),
            )
        }
        #[cfg(target_family = "wasm")]
        {
            let _ = request;
            responder.respond_with_error(Error::method_not_found())
        }
    }

    fn handle_terminal_output(
        &self,
        request: TerminalOutputRequest,
        responder: Responder<TerminalOutputResponse>,
    ) -> Result<(), Error> {
        #[cfg(not(target_family = "wasm"))]
        {
            let result = with_client_io(&self.io, |io| {
                io.terminal_output(
                    &request.session_id.to_string(),
                    &request.terminal_id.to_string(),
                )
            });
            match result {
                Ok((output, truncated, status)) => responder.respond(
                    TerminalOutputResponse::new(output, truncated)
                        .exit_status(status.map(exit_status)),
                ),
                Err(error) => responder.respond_with_error(error),
            }
        }
        #[cfg(target_family = "wasm")]
        {
            let _ = request;
            responder.respond_with_error(Error::method_not_found())
        }
    }

    fn handle_wait_for_terminal_exit(
        &self,
        request: WaitForTerminalExitRequest,
        responder: Responder<WaitForTerminalExitResponse>,
    ) -> Result<(), Error> {
        #[cfg(not(target_family = "wasm"))]
        {
            let io = Arc::clone(&self.io);
            self.session.connection().spawn(async move {
                let session_id = request.session_id.to_string();
                let terminal_id = request.terminal_id.to_string();
                let cancellation = responder.cancellation();
                let result = tokio::select! {
                    biased;
                    _ = cancellation.cancelled() => Err(Error::request_cancelled()),
                    result = wait_for_terminal_exit(&io, &session_id, &terminal_id) => result,
                };
                let _ = match result {
                    Ok(exit) => {
                        responder.respond(WaitForTerminalExitResponse::new(exit_status(exit)))
                    }
                    Err(error) => responder.respond_with_error(error),
                };
                Ok(())
            })
        }
        #[cfg(target_family = "wasm")]
        {
            let _ = request;
            responder.respond_with_error(Error::method_not_found())
        }
    }

    fn handle_kill_terminal(
        &self,
        request: KillTerminalRequest,
        responder: Responder<KillTerminalResponse>,
    ) -> Result<(), Error> {
        #[cfg(not(target_family = "wasm"))]
        {
            self.spawn_blocking_client_io(
                responder,
                move |io| {
                    io.kill_terminal(
                        &request.session_id.to_string(),
                        &request.terminal_id.to_string(),
                    )
                },
                |()| KillTerminalResponse::new(),
            )
        }
        #[cfg(target_family = "wasm")]
        {
            let _ = request;
            responder.respond_with_error(Error::method_not_found())
        }
    }

    fn handle_release_terminal(
        &self,
        request: ReleaseTerminalRequest,
        responder: Responder<ReleaseTerminalResponse>,
    ) -> Result<(), Error> {
        #[cfg(not(target_family = "wasm"))]
        {
            self.spawn_blocking_client_io(
                responder,
                move |io| {
                    io.release_terminal(
                        &request.session_id.to_string(),
                        &request.terminal_id.to_string(),
                    )
                },
                |()| ReleaseTerminalResponse::new(),
            )
        }
        #[cfg(target_family = "wasm")]
        {
            let _ = request;
            responder.respond_with_error(Error::method_not_found())
        }
    }
}
