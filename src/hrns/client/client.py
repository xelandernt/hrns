"""The ACP client core: one connection, sessions, extension calls, events.

The official ACP SDK performs dispatch and type handling. This module owns
session lifecycle, event normalization, extension negotiation, and the
interaction policy boundary. No runtime or Pydantic AI imports live here.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from acp import RequestError
from acp.client.connection import ClientSideConnection
from acp.schema import (
    AcceptElicitationResponse,
    AllowedOutcome,
    BooleanConfigOptionCapabilities,
    CancelElicitationResponse,
    ClientCapabilities,
    ClientSessionCapabilities,
    DeclineElicitationResponse,
    DeniedOutcome,
    ElicitationCapabilities,
    FileSystemCapabilities,
    PermissionOption,
    PlanCapabilities,
    RequestPermissionResponse,
    SessionConfigOptionsCapabilities,
    ToolCallUpdate,
)

from hrns.protocol import (
    FS_READ_TEXT_FILE,
    FS_WRITE_TEXT_FILE,
    TERMINAL,
    CommandInvokeResponse,
    ConfigurationSnapshot,
    ToolPresentationSnapshot,
)

from .bus import EventBus
from .connection import ConnectionLifecycle
from .constants import (
    CLIENT_NAME,
    CONNECT_TIMEOUT,
)
from .errors import (
    ExtensionUnavailable,
    HrnsClientError,
    InteractionRejected,
    InteractionRequired,
    ProtocolError,
    describe_rpc_error,
)
from .events import (
    SessionsListed,
)
from .extension import (
    CapabilityListResult,
    CommandListResult,
    HrnsExtension,
    PluginListResult,
    advertisement_meta,
    strip_prefix,
)
from .extensions_api import ExtensionsApi
from .interactions import (
    AskUserResponse,
    InteractionPolicy,
    RequireInteractions,
    ask_user_payload,
    parse_ask_user_request,
)
from .io import ClientIo, ClientIoMode, ClientIoPolicy
from .payloads import elicitation_record, normalize_permission
from .session import SessionLifecycle, SessionProxy
from .transport import AgentTransport, ConnectionBundle
from .turn import TurnService
from .updates import dispatch_update

__all__ = ["HrnsAcpClient", "DEFAULT_INTERACTION_POLICY"]

DEFAULT_INTERACTION_POLICY: InteractionPolicy = RequireInteractions()


class HrnsAcpClient:
    """A single ACP connection to one agent.

    ``transport_factory`` is re-invoked for every (re)connect, so transports
    may be stateful (a process launch or a WebSocket).
    """

    def __init__(
        self,
        transport_factory: Callable[[], AgentTransport] | AgentTransport,
        *,
        policy: InteractionPolicy | None = None,
        advertise_extension: bool = True,
        session_new_meta: dict[str, Any] | None = None,
        connect_timeout: float = CONNECT_TIMEOUT,
        auth_method: str = "",
        client_io: ClientIoMode = "off",
    ) -> None:
        self.policy: InteractionPolicy = policy or DEFAULT_INTERACTION_POLICY
        self.advertise_extension = advertise_extension
        self.session_new_meta = dict(session_new_meta or {})
        self.connect_timeout = connect_timeout
        self.auth_method = auth_method.strip()
        self.io = ClientIo(ClientIoPolicy(mode=client_io))

        self.bus = EventBus()
        self.initialized: Any = None
        self.extension = HrnsExtension()
        self.capabilities: ClientCapabilities = self._build_capabilities()
        self.agent_name = CLIENT_NAME
        self.agent_version = ""
        self.agent_title = CLIENT_NAME
        self.auth_methods: list[Any] = []

        self._inflight_updates = 0
        self.sessions = SessionLifecycle(self)
        self.turns = TurnService(self)
        self.extensions = ExtensionsApi(
            request=self._ext_request,
            check_local_add=self.sessions.check_local_add,
            bus=self.bus,
            answer_questions=self._answer_questions,
        )
        self.connection_lifecycle = ConnectionLifecycle(self, transport_factory, connect_timeout)
        self._pending_interaction_error: HrnsClientError | None = None

    # ------------------------------------------------------------------ setup

    def _build_capabilities(self) -> ClientCapabilities:
        offered = self.io.policy.offered
        return ClientCapabilities(
            fs=FileSystemCapabilities(
                read_text_file=FS_READ_TEXT_FILE in offered,
                write_text_file=FS_WRITE_TEXT_FILE in offered,
            ),
            terminal=TERMINAL in offered,
            session=ClientSessionCapabilities(
                config_options=SessionConfigOptionsCapabilities(
                    boolean=BooleanConfigOptionCapabilities(),
                )
            ),
            plan=PlanCapabilities(),
            elicitation=ElicitationCapabilities(form={}, url={}),
        )

    def _initialize_meta(self) -> dict[str, Any]:
        meta = advertisement_meta(advertise=self.advertise_extension)
        return meta

    @property
    def bundle(self) -> ConnectionBundle:
        return self.connection_lifecycle.bundle

    @property
    def connection(self) -> ClientSideConnection:
        return self.bundle.conn

    @property
    def connected(self) -> bool:
        return self.connection_lifecycle.connected

    # --------------------------------------------------------------- lifecycle

    async def connect(self) -> Any:
        return await self.connection_lifecycle.connect()

    async def disconnect(self) -> None:
        await self.connection_lifecycle.disconnect()
        await self.io.close()

    def has_session_capability(self, name: str) -> bool:
        """Ask whether the agent advertised a session lifecycle feature."""
        return self.sessions.has_capability(name)

    @property
    def supports_load(self) -> bool:
        return self.sessions.supports_load

    async def new_session(
        self,
        cwd: str,
        *,
        additional_directories: list[str] | None = None,
        initial_configuration: list[dict[str, Any]] | None = None,
    ) -> SessionProxy:
        return await self.sessions.new_session(
            cwd,
            additional_directories=additional_directories,
            initial_configuration=initial_configuration,
        )

    async def load_session(
        self,
        session_id: str,
        cwd: str,
        *,
        additional_directories: list[str] | None = None,
        replay: bool = True,
    ) -> SessionProxy:
        return await self.sessions.load_session(
            session_id,
            cwd,
            additional_directories=additional_directories,
            replay=replay,
        )

    async def resume_session(
        self,
        session_id: str,
        cwd: str,
        *,
        additional_directories: list[str] | None = None,
    ) -> SessionProxy:
        return await self.sessions.resume_session(
            session_id,
            cwd,
            additional_directories=additional_directories,
        )

    async def list_sessions(
        self, cwd: str | None = None, cursor: str | None = None
    ) -> SessionsListed:
        return await self.sessions.list_sessions(cwd=cwd, cursor=cursor)

    async def close_session(self, session_id: str) -> None:
        await self.sessions.close_session(session_id)

    async def delete_session(self, session_id: str) -> None:
        await self.sessions.delete_session(session_id)

    async def authenticate(self, method_id: str) -> Any:
        try:
            return await self.connection.authenticate(method_id=method_id)
        except RequestError as exc:
            raise HrnsClientError(describe_rpc_error("authenticate", exc)) from exc

    # ------------------------------------------------------------- turn control

    async def prompt(
        self,
        session_id: str,
        prompt: str | list[Any],
        *,
        display_text: str | None = None,
    ) -> Any:
        return await self.turns.prompt(session_id, prompt, display_text=display_text)

    async def cancel(self, session_id: str) -> None:
        await self.turns.cancel(session_id)

    async def set_mode(self, session_id: str, mode_id: str) -> None:
        await self.turns.set_mode(session_id, mode_id)

    async def set_config_option(
        self, session_id: str, config_id: str, value: str | bool
    ) -> tuple[dict[str, Any], ...]:
        return await self.turns.set_config_option(session_id, config_id, value)

    # --------------------------------------------------------------- extension

    async def _ext_request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if not self.extension.supports(method):
            raise ExtensionUnavailable(method)
        try:
            result = await self.connection.ext_method(strip_prefix(method), params)
        except RequestError as exc:
            if exc.code == -32601:
                raise ExtensionUnavailable(method) from exc
            raise HrnsClientError(describe_rpc_error(method, exc)) from exc
        if not isinstance(result, dict):
            raise ProtocolError(f"{method} returned an unexpected response type")
        return result

    async def hrns_capabilities(
        self, session_id: str, *, cursor: str | None = None, limit: int | None = None
    ) -> CapabilityListResult:
        return await self.extensions.capabilities(session_id, cursor=cursor, limit=limit)

    async def hrns_plugins(
        self, *, cursor: str | None = None, limit: int | None = None
    ) -> PluginListResult:
        return await self.extensions.plugins(cursor=cursor, limit=limit)

    async def hrns_configuration(self, session_id: str) -> ConfigurationSnapshot:
        return await self.extensions.configuration(session_id)

    async def hrns_update_configuration(
        self,
        session_id: str,
        changes: list[dict[str, Any]] | dict[str, Any],
        *,
        expected_revision: int | None = None,
        request_id: str | None = None,
    ) -> ConfigurationSnapshot:
        return await self.extensions.update_configuration(
            session_id,
            changes,
            expected_revision=expected_revision,
            request_id=request_id,
        )

    async def hrns_commands(
        self, session_id: str, *, cursor: str | None = None, limit: int | None = None
    ) -> CommandListResult:
        return await self.extensions.commands(session_id, cursor=cursor, limit=limit)

    async def hrns_steer(self, session_id: str, text: str) -> bool:
        """Inject a follow-up into the turn that is currently running."""
        return await self.extensions.steer(session_id, text)

    async def hrns_invoke_command(
        self,
        session_id: str,
        binding_id: str,
        command_name: str,
        *,
        text: str | None = None,
        arguments: dict[str, Any] | None = None,
        expected_revision: int | None = None,
        request_id: str | None = None,
    ) -> CommandInvokeResponse:
        return await self.extensions.invoke_command(
            session_id,
            binding_id,
            command_name,
            text=text,
            arguments=arguments,
            expected_revision=expected_revision,
            request_id=request_id,
        )

    async def hrns_cancel_command(self, session_id: str, request_id: str) -> dict[str, Any]:
        return await self.extensions.cancel_command(session_id, request_id)

    async def hrns_presentations(
        self, session_id: str, tool_call_ids: list[str]
    ) -> tuple[ToolPresentationSnapshot, ...]:
        return await self.extensions.presentations(session_id, tool_call_ids)

    async def _raw_request(self, method: str, params: dict[str, Any]) -> Any:
        """Send one request through the SDK connection's public JSON-RPC layer."""
        conn = getattr(self.bundle.conn, "_conn", None)
        if conn is None or not hasattr(conn, "send_request"):
            raise ProtocolError(f"The installed ACP SDK cannot send '{method}' requests.")
        return await conn.send_request(method, params)

    async def drain_events(self, *, timeout: float = 1.0) -> None:
        """Let notification handlers and event consumers catch up with a prompt response."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        stable = 0
        while loop.time() < deadline:
            await asyncio.sleep(0)
            await self.bus.drain()
            if self._inflight_updates == 0:
                stable += 1
                if stable >= 3:
                    return
                await asyncio.sleep(0.01)
            else:
                stable = 0
        await self.bus.drain()

    # ------------------------------------------------------- client callbacks

    async def session_update(self, session_id: str, update: Any, **kwargs: Any) -> None:
        self._inflight_updates += 1
        try:
            dispatch_update(self.bus, session_id, update)
        finally:
            self._inflight_updates -= 1

    async def request_permission(
        self,
        session_id: str,
        tool_call: ToolCallUpdate,
        options: list[PermissionOption],
        **kwargs: Any,
    ) -> RequestPermissionResponse:
        record = normalize_permission(session_id, tool_call, options)
        try:
            decision = await self.policy.choose_permission(record)
        except (InteractionRequired, InteractionRejected) as exc:
            # Remember the policy failure so the turn fails with a clear error, and
            # tell the agent "cancelled" so it can wind down instead of waiting.
            self._pending_interaction_error = exc
            return RequestPermissionResponse(outcome=DeniedOutcome(outcome="cancelled"))
        if decision.option_id is None:
            return RequestPermissionResponse(outcome=DeniedOutcome(outcome="cancelled"))
        return RequestPermissionResponse(
            outcome=AllowedOutcome(outcome="selected", option_id=decision.option_id)
        )

    async def create_elicitation(self, message: str, mode: Any, **kwargs: Any) -> Any:
        record = elicitation_record(message, mode)
        try:
            answer = await self.policy.answer_elicitation(record)
        except (InteractionRequired, InteractionRejected) as exc:
            self._pending_interaction_error = exc
            return DeclineElicitationResponse(action="decline")
        if answer.action == "accept":
            return AcceptElicitationResponse(action="accept", content=answer.content or None)
        if answer.action == "cancel":
            return CancelElicitationResponse(action="cancel")
        return DeclineElicitationResponse(action="decline")

    async def complete_elicitation(self, elicitation_id: str, **kwargs: Any) -> None:
        return None

    async def _answer_questions(self, params: dict[str, Any]) -> dict[str, Any]:
        request = parse_ask_user_request(params)
        try:
            response = await self.policy.answer_questions(request)
        except (InteractionRequired, InteractionRejected) as exc:
            self._pending_interaction_error = exc
            return ask_user_payload(AskUserResponse(cancelled=True))
        return ask_user_payload(response)

    async def read_text_file(
        self,
        session_id: str,
        path: str,
        line: int | None = None,
        limit: int | None = None,
        **kwargs: Any,
    ) -> Any:
        return await self.io.read_text_file(session_id, path, line=line, limit=limit)

    async def write_text_file(self, session_id: str, path: str, content: str, **kwargs: Any) -> Any:
        return await self.io.write_text_file(session_id, path, content)

    async def create_terminal(
        self,
        session_id: str,
        command: str,
        args: list[str] | None = None,
        env: list[Any] | None = None,
        cwd: str | None = None,
        output_byte_limit: int | None = None,
        **kwargs: Any,
    ) -> Any:
        return await self.io.create_terminal(
            session_id,
            command,
            args=args,
            env=env,
            cwd=cwd,
            output_byte_limit=output_byte_limit,
        )

    async def terminal_output(self, session_id: str, terminal_id: str, **kwargs: Any) -> Any:
        return await self.io.terminal_output(session_id, terminal_id)

    async def wait_for_terminal_exit(self, session_id: str, terminal_id: str, **kwargs: Any) -> Any:
        return await self.io.wait_for_terminal_exit(session_id, terminal_id)

    async def kill_terminal(self, session_id: str, terminal_id: str, **kwargs: Any) -> Any:
        await self.io.kill_terminal(session_id, terminal_id)
        return None

    async def release_terminal(self, session_id: str, terminal_id: str, **kwargs: Any) -> Any:
        await self.io.release_terminal(session_id, terminal_id)
        return None

    async def ext_method(self, method: str, params: dict[str, Any]) -> Any:
        return await self.extensions.handle_ext_method(method, params)

    async def ext_notification(self, method: str, params: dict[str, Any]) -> None:
        await self.extensions.handle_notification(method, params)
