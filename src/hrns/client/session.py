"""One conversation on a connected client, with cached display state."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from acp import RequestError
from acp.schema import AcpMcpServer, HttpMcpServer, McpServerStdio, SseMcpServer

from ..shared.errors import HrnsClientError, ProtocolError, describe_rpc_error
from .events import SessionsListed
from .payloads import absolute_path

if TYPE_CHECKING:
    from .client import HrnsAcpClient

McpServerDeclaration = AcpMcpServer | HttpMcpServer | McpServerStdio | SseMcpServer


class SessionProxy:
    """One conversation on a connected client, with cached display state."""

    def __init__(self, client: HrnsAcpClient, session_id: str, cwd: str) -> None:
        self.client = client
        self.session_id = session_id
        self.cwd = cwd
        # The agent may ask this client for file I/O and terminal commands from here on.
        client.io.remember(session_id, cwd)
        self.config_options: tuple[dict[str, Any], ...] = ()
        self.modes: dict[str, Any] | None = None
        self.current_mode_id: str | None = None
        self._unsubscribe: list[Callable[[], None]] = []

    def apply_state(self, modes: Any, config_options: Any) -> None:
        if modes is not None:
            self.modes = modes.model_dump(mode="json", by_alias=True, exclude_none=True)
            self.current_mode_id = modes.current_mode_id
        if config_options is not None:
            self.config_options = tuple(
                option.model_dump(mode="json", by_alias=True, exclude_none=True)
                for option in config_options
            )

    def remember(self, options: tuple[dict[str, Any], ...]) -> None:
        self.config_options = options

    # commands ---------------------------------------------------------------

    async def prompt(self, text: str, *, display_text: str | None = None) -> Any:
        return await self.client.prompt(self.session_id, text, display_text=display_text)

    async def prompt_blocks(self, blocks: list[Any], *, display_text: str) -> Any:
        return await self.client.prompt(self.session_id, blocks, display_text=display_text)

    async def cancel(self) -> None:
        await self.client.cancel(self.session_id)

    async def set_config_option(
        self, config_id: str, value: str | bool
    ) -> tuple[dict[str, Any], ...]:
        options = await self.client.set_config_option(self.session_id, config_id, value)
        self.remember(options)
        return options

    async def set_mode(self, mode_id: str) -> None:
        await self.client.set_mode(self.session_id, mode_id)
        self.current_mode_id = mode_id

    async def close(self) -> None:
        await self.client.close_session(self.session_id)

    async def delete(self) -> None:
        await self.client.delete_session(self.session_id)


class SessionLifecycle:
    """Owns session create/load/resume/list/close/delete for one client."""

    def __init__(self, client: HrnsAcpClient) -> None:
        self.client = client

    def has_capability(self, name: str) -> bool:
        """Ask whether the agent advertised a session lifecycle feature."""
        client = self.client
        if client.initialized is None:
            return False
        caps = getattr(client.initialized, "agent_capabilities", None)
        sessions = getattr(caps, "session_capabilities", None)
        return bool(getattr(sessions, name, None))

    @property
    def supports_load(self) -> bool:
        client = self.client
        if client.initialized is None:
            return False
        caps = getattr(client.initialized, "agent_capabilities", None)
        return bool(getattr(caps, "load_session", False))

    def check_additional_directories(self, directories: list[str] | None) -> None:
        if not directories:
            return
        for directory in directories:
            absolute_path(directory, label="additional directory")
        if not self.has_capability("additional_directories"):
            raise ProtocolError(
                "This agent does not advertise additionalDirectories support; "
                "start the session inside its workspace instead."
            )

    async def new_session(
        self,
        cwd: str,
        *,
        additional_directories: list[str] | None = None,
    ) -> SessionProxy:
        client = self.client
        absolute_path(cwd, label="working directory")
        self.check_additional_directories(additional_directories)
        mcp_servers: list[McpServerDeclaration] = list(await client.mcp_declarations())
        try:
            response = await client.connection.new_session(
                cwd=cwd,
                additional_directories=additional_directories,
                mcp_servers=mcp_servers,
            )
        except RequestError as exc:
            raise HrnsClientError(describe_rpc_error("session/new", exc)) from exc
        session = SessionProxy(client, response.session_id, cwd)
        session.apply_state(response.modes, response.config_options)
        return session

    async def load_session(
        self,
        session_id: str,
        cwd: str,
        *,
        additional_directories: list[str] | None = None,
        replay: bool = True,
    ) -> SessionProxy:
        client = self.client
        absolute_path(cwd, label="working directory")
        self.check_additional_directories(additional_directories)
        if not self.supports_load:
            raise ProtocolError(
                "This agent does not advertise session/load; reopening the conversation is unavailable."
            )
        if not replay:
            raise ProtocolError("session/load always replays history in ACP v1.")
        mcp_servers: list[McpServerDeclaration] = list(await client.mcp_declarations())
        client.begin_session_load(session_id)
        try:
            response = await client.connection.load_session(
                cwd=cwd,
                session_id=session_id,
                mcp_servers=mcp_servers,
                additional_directories=additional_directories,
            )
        except RequestError as exc:
            client.abort_session_load(session_id)
            raise HrnsClientError(describe_rpc_error("session/load", exc)) from exc
        except BaseException:
            client.abort_session_load(session_id)
            raise
        client.complete_session_load(session_id)
        session = SessionProxy(client, session_id, cwd)
        if response is not None:
            session.apply_state(response.modes, response.config_options)
        return session

    async def resume_session(
        self,
        session_id: str,
        cwd: str,
        *,
        additional_directories: list[str] | None = None,
    ) -> SessionProxy:
        client = self.client
        absolute_path(cwd, label="working directory")
        self.check_additional_directories(additional_directories)
        if not self.has_capability("resume"):
            raise ProtocolError("This agent does not advertise session/resume; use session/load.")
        mcp_servers: list[McpServerDeclaration] = list(await client.mcp_declarations())
        try:
            response = await client.connection.resume_session(
                session_id=session_id,
                cwd=cwd,
                mcp_servers=mcp_servers,
                additional_directories=additional_directories,
            )
        except RequestError as exc:
            raise HrnsClientError(describe_rpc_error("session/resume", exc)) from exc
        session = SessionProxy(client, session_id, cwd)
        session.apply_state(response.modes, response.config_options)
        return session

    async def list_sessions(
        self, cwd: str | None = None, cursor: str | None = None
    ) -> SessionsListed:
        client = self.client
        if cwd is not None:
            absolute_path(cwd, label="working directory filter")
        if not self.has_capability("list"):
            raise ProtocolError("This agent does not advertise session/list.")
        try:
            response = await client.connection.list_sessions(cwd=cwd, cursor=cursor)
        except RequestError as exc:
            raise HrnsClientError(describe_rpc_error("session/list", exc)) from exc
        sessions = tuple(
            item.model_dump(mode="json", by_alias=True, exclude_none=True)
            for item in response.sessions
        )
        result = SessionsListed(sessions=sessions, next_cursor=response.next_cursor)
        client.bus.publish(result)
        return result

    async def close_session(self, session_id: str) -> None:
        client = self.client
        if not self.has_capability("close"):
            raise ProtocolError(
                "This agent does not advertise session/close; use session/cancel for "
                "portable turn cleanup."
            )
        try:
            await client.connection.close_session(session_id=session_id)
        except RequestError as exc:
            raise HrnsClientError(describe_rpc_error("session/close", exc)) from exc

    async def delete_session(self, session_id: str) -> None:
        client = self.client
        if not self.has_capability("delete"):
            raise ProtocolError("This agent does not advertise session/delete.")
        try:
            await client.connection.delete_session(session_id=session_id)
        except RequestError as exc:
            # ACP says clients SHOULD treat -32002 as a successful delete: the session is
            # already gone, which is the outcome the caller asked for.
            if exc.code != -32002:
                raise HrnsClientError(describe_rpc_error("session/delete", exc)) from exc
