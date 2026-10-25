"""One conversation on a connected client, with cached display state."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from acp import RequestError

from harness.protocol import (
    CommandInvokeResponse,
    ConfigurationSnapshot,
    ToolPresentationSnapshot,
)

from .errors import HarnessClientError, ProtocolError, describe_rpc_error
from .events import (
    CapabilitiesListed,
    PluginCommandsListed,
    PluginListed,
    SessionsListed,
)
from .extension import (
    CapabilityListResult,
    CommandListResult,
    PluginListResult,
    validate_changes,
)
from .payloads import absolute_path

if TYPE_CHECKING:
    from .client import HarnessAcpClient


class SessionProxy:
    """One conversation on a connected client, with cached display state."""

    def __init__(self, client: HarnessAcpClient, session_id: str, cwd: str) -> None:
        self.client = client
        self.session_id = session_id
        self.cwd = cwd
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

    async def rename(self, title: str) -> str:
        """Set a persistent manual name for this session."""
        from harness.protocol.models import SessionTitleUpdate

        request = SessionTitleUpdate(session_id=self.session_id, title=title)
        result = await self.client._ext_request("_harness/session/rename", request.to_wire())
        return SessionTitleUpdate.model_validate(result).title

    async def steer(self, text: str) -> bool:
        """Deliver a follow-up into the running turn; false when it has finished."""
        return await self.client.harness_steer(self.session_id, text)

    async def configuration(self) -> ConfigurationSnapshot:
        snapshot = await self.client.harness_configuration(self.session_id)
        return snapshot

    async def update_configuration(
        self,
        changes: list[dict[str, Any]] | dict[str, Any],
        *,
        expected_revision: int | None = None,
        request_id: str | None = None,
    ) -> ConfigurationSnapshot:
        snapshot = await self.client.harness_update_configuration(
            self.session_id,
            changes,
            expected_revision=expected_revision,
            request_id=request_id,
        )
        return snapshot

    async def list_capabilities(
        self, *, cursor: str | None = None, limit: int | None = None
    ) -> CapabilityListResult:
        result = await self.client.harness_capabilities(self.session_id, cursor=cursor, limit=limit)
        self.client.bus.publish(
            CapabilitiesListed(
                session_id=self.session_id,
                bindings=tuple(
                    binding.model_dump(mode="json", by_alias=True) for binding in result.bindings
                ),
                selected_revision=result.selected_revision,
                policy_revision=result.policy_revision,
                next_cursor=result.next_cursor,
            )
        )
        return result

    async def list_plugins(
        self, *, cursor: str | None = None, limit: int | None = None
    ) -> PluginListResult:
        result = await self.client.harness_plugins(cursor=cursor, limit=limit)
        self.client.bus.publish(
            PluginListed(plugins=result.plugins, next_cursor=result.next_cursor)
        )
        return result

    async def list_commands(self, *, cursor: str | None = None) -> CommandListResult:
        result = await self.client.harness_commands(self.session_id, cursor=cursor)
        self.client.bus.publish(
            PluginCommandsListed(
                session_id=self.session_id,
                commands=tuple(result.commands),
                selected_revision=result.selected_revision,
                supported_operations=tuple(result.supported_operations),
            )
        )
        return result

    async def invoke_command(
        self,
        binding_id: str,
        command_name: str,
        *,
        text: str | None = None,
        arguments: dict[str, Any] | None = None,
        expected_revision: int | None = None,
        request_id: str | None = None,
    ) -> CommandInvokeResponse:
        return await self.client.harness_invoke_command(
            self.session_id,
            binding_id,
            command_name,
            text=text,
            arguments=arguments,
            expected_revision=expected_revision,
            request_id=request_id,
        )

    async def cancel_command(self, request_id: str) -> dict[str, Any]:
        return await self.client.harness_cancel_command(self.session_id, request_id)

    async def presentations(self, tool_call_ids: list[str]) -> tuple[ToolPresentationSnapshot, ...]:
        return await self.client.harness_presentations(self.session_id, tool_call_ids)

    async def close(self) -> None:
        await self.client.close_session(self.session_id)

    async def delete(self) -> None:
        await self.client.delete_session(self.session_id)


def _deep_merge(base: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
    """Merge metadata dictionaries without losing sibling keys."""
    result = dict(base)
    for key, value in extra.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


class SessionLifecycle:
    """Owns session create/load/resume/list/close/delete for one client."""

    def __init__(self, client: HarnessAcpClient) -> None:
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

    def check_local_add(self, changes: list[Any]) -> None:
        client = self.client
        if not any(getattr(change, "type", None) == "add" for change in changes):
            return
        if not client.extension.local_binding_creation:
            raise HarnessClientError(
                "The host did not advertise local binding creation; 'add' changes are "
                "unavailable. Enable an existing offered binding instead."
            )

    async def new_session(
        self,
        cwd: str,
        *,
        additional_directories: list[str] | None = None,
        initial_configuration: list[dict[str, Any]] | None = None,
    ) -> SessionProxy:
        client = self.client
        absolute_path(cwd, label="working directory")
        self.check_additional_directories(additional_directories)
        meta: dict[str, Any] = {}
        if initial_configuration:
            try:
                validated = validate_changes(initial_configuration)
            except ValueError as exc:
                raise HarnessClientError(f"Invalid initial configuration: {exc}") from exc
            self.check_local_add(validated)
            meta = {
                "harness": {
                    "initialConfiguration": [
                        change.model_dump(mode="json", by_alias=True, exclude_none=True)
                        for change in validated
                    ]
                }
            }
        if client.session_new_meta:
            meta = _deep_merge(meta, client.session_new_meta)
        try:
            response = await client.connection.new_session(
                cwd=cwd,
                additional_directories=additional_directories,
                mcp_servers=[],
                **(meta or {}),
            )
        except RequestError as exc:
            raise HarnessClientError(describe_rpc_error("session/new", exc)) from exc
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
        try:
            response = await client.connection.load_session(
                cwd=cwd,
                session_id=session_id,
                mcp_servers=[],
                additional_directories=additional_directories,
            )
        except RequestError as exc:
            raise HarnessClientError(describe_rpc_error("session/load", exc)) from exc
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
        try:
            response = await client.connection.resume_session(
                session_id=session_id,
                cwd=cwd,
                mcp_servers=[],
                additional_directories=additional_directories,
            )
        except RequestError as exc:
            raise HarnessClientError(describe_rpc_error("session/resume", exc)) from exc
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
            raise HarnessClientError(describe_rpc_error("session/list", exc)) from exc
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
            raise HarnessClientError(describe_rpc_error("session/close", exc)) from exc

    async def delete_session(self, session_id: str) -> None:
        client = self.client
        if not self.has_capability("delete"):
            raise ProtocolError("This agent does not advertise session/delete.")
        # ACP SDK 0.11.1 exposes the request model but not a convenience method.
        try:
            result = await client._raw_request("session/delete", {"sessionId": session_id})
        except RequestError as exc:
            raise HarnessClientError(describe_rpc_error("session/delete", exc)) from exc
        if result is not None and not isinstance(result, dict):
            raise ProtocolError("session/delete returned an unexpected response")
