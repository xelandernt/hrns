"""Harness extension requests and their payload validation."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from harness.protocol import (
    CommandInvokeResponse,
    ConfigurationSnapshot,
    ToolPresentationSnapshot,
)

from .errors import HarnessClientError, ProtocolError
from .extension import (
    CapabilityListResult,
    CommandListResult,
    PluginListResult,
    build_configuration_update,
    parse_capability_list,
    parse_command_list,
    parse_command_response,
    parse_configuration_snapshot,
    parse_plugin_list,
    parse_presentation_list,
    validate_changes,
)
from .payloads import checked_limit

Request = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]


class ExtensionsApi:
    """Owns the ``_harness`` request surface for one connected client."""

    def __init__(
        self,
        *,
        request: Request,
        check_local_add: Callable[[list[Any]], None],
    ) -> None:
        self._request = request
        self._check_local_add = check_local_add

    async def capabilities(
        self, session_id: str, *, cursor: str | None = None, limit: int | None = None
    ) -> CapabilityListResult:
        payload: dict[str, Any] = {"sessionId": session_id}
        if cursor:
            payload["cursor"] = cursor
        effective_limit = checked_limit(limit)
        if effective_limit is not None:
            payload["limit"] = effective_limit
        try:
            raw = await self._request("_harness/capabilities/list", payload)
            return parse_capability_list(raw)
        except (ValueError, TypeError) as exc:
            raise ProtocolError(f"capabilities/list returned malformed data: {exc}") from exc

    async def plugins(
        self, *, cursor: str | None = None, limit: int | None = None
    ) -> PluginListResult:
        payload: dict[str, Any] = {}
        if cursor:
            payload["cursor"] = cursor
        effective_limit = checked_limit(limit)
        if effective_limit is not None:
            payload["limit"] = effective_limit
        try:
            raw = await self._request("_harness/plugins/list", payload)
            return parse_plugin_list(raw)
        except (ValueError, TypeError) as exc:
            raise ProtocolError(f"plugins/list returned malformed data: {exc}") from exc

    async def configuration(self, session_id: str) -> ConfigurationSnapshot:
        try:
            raw = await self._request("_harness/configuration/get", {"sessionId": session_id})
            return parse_configuration_snapshot(raw)
        except (ValueError, TypeError) as exc:
            raise ProtocolError(f"configuration/get returned malformed data: {exc}") from exc

    async def update_configuration(
        self,
        session_id: str,
        changes: list[dict[str, Any]] | dict[str, Any],
        *,
        expected_revision: int | None = None,
        request_id: str | None = None,
    ) -> ConfigurationSnapshot:
        if expected_revision is None:
            current = await self.configuration(session_id)
            expected_revision = current.selected_revision
        request_id = request_id or f"client-{uuid.uuid4().hex}"
        try:
            validated = validate_changes(changes)
        except ValueError as exc:
            raise HarnessClientError(f"Invalid configuration change: {exc}") from exc
        self._check_local_add(validated)
        try:
            payload = build_configuration_update(
                session_id, request_id, expected_revision, validated
            )
        except ValueError as exc:
            raise HarnessClientError(f"Invalid configuration change: {exc}") from exc
        try:
            raw = await self._request("_harness/configuration/update", payload)
            return parse_configuration_snapshot(raw)
        except (ValueError, TypeError) as exc:
            raise ProtocolError(f"configuration/update returned malformed data: {exc}") from exc

    async def commands(
        self, session_id: str, *, cursor: str | None = None, limit: int | None = None
    ) -> CommandListResult:
        payload: dict[str, Any] = {"sessionId": session_id}
        if cursor:
            payload["cursor"] = cursor
        effective_limit = checked_limit(limit)
        if effective_limit is not None:
            payload["limit"] = effective_limit
        try:
            raw = await self._request("_harness/commands/list", payload)
            return parse_command_list(raw)
        except (ValueError, TypeError) as exc:
            raise ProtocolError(f"commands/list returned malformed data: {exc}") from exc

    async def steer(self, session_id: str, text: str) -> bool:
        """Inject a follow-up into the turn that is currently running."""
        raw = await self._request("_harness/session/steer", {"sessionId": session_id, "text": text})
        return bool(raw.get("steered"))

    async def invoke_command(
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
        if text is not None and arguments is not None:
            raise HarnessClientError(
                "Provide either raw command text or structured arguments, not both."
            )
        if expected_revision is None:
            expected_revision = (await self.commands(session_id)).selected_revision
        request_id = request_id or f"command-{uuid.uuid4().hex}"
        payload: dict[str, Any] = {
            "sessionId": session_id,
            "bindingId": binding_id,
            "commandName": command_name,
            "requestId": request_id,
            "expectedRevision": expected_revision,
        }
        if arguments is not None:
            payload["arguments"] = arguments
        else:
            payload["text"] = text or ""
        try:
            raw = await self._request("_harness/commands/invoke", payload)
            return parse_command_response(raw)
        except (ValueError, TypeError) as exc:
            raise ProtocolError(f"commands/invoke returned malformed data: {exc}") from exc

    async def cancel_command(self, session_id: str, request_id: str) -> dict[str, Any]:
        try:
            return await self._request(
                "_harness/commands/cancel",
                {"sessionId": session_id, "requestId": request_id},
            )
        except (ValueError, TypeError) as exc:
            raise ProtocolError(f"commands/cancel returned malformed data: {exc}") from exc

    async def presentations(
        self, session_id: str, tool_call_ids: list[str]
    ) -> tuple[ToolPresentationSnapshot, ...]:
        if not tool_call_ids:
            return ()
        payload = {"sessionId": session_id, "toolCallIds": tool_call_ids[:200]}
        try:
            raw = await self._request("_harness/presentation/get", payload)
            return tuple(parse_presentation_list(raw).snapshots)
        except (ValueError, TypeError) as exc:
            raise ProtocolError(f"presentation/get returned malformed data: {exc}") from exc
