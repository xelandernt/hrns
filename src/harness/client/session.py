"""One conversation on a connected client, with cached display state."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from harness.protocol import (
    CommandInvokeResponse,
    ConfigurationSnapshot,
    ToolPresentationSnapshot,
)

from .events import CapabilitiesListed, PluginCommandsListed, PluginListed
from .extension import CapabilityListResult, CommandListResult, PluginListResult

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
