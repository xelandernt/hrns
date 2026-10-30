"""hrns extension requests and their payload validation."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from acp import RequestError

from hrns.protocol import (
    CommandCatalogChanged,
    CommandInvokeResponse,
    ConfigurationSnapshot,
    PresentationUpdated,
    QueueEditRequest,
    QueueRemoveRequest,
    QueueSnapshot,
    QueueState,
    QueueUpdate,
    QueueUpdateProtocolError,
    ToolPresentationSnapshot,
)

from .bus import EventBus
from .errors import HrnsClientError, ProtocolError
from .events import (
    ConfigurationChanged,
    PluginCommandsListed,
    QueueChanged,
    ToolPresentationUpdated,
)
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
    parse_queue_snapshot,
    parse_queue_update,
    validate_changes,
)
from .payloads import checked_limit

Request = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]


class ExtensionsApi:
    """Owns the ``_hrns`` request surface for one connected client."""

    def __init__(
        self,
        *,
        request: Request,
        check_local_add: Callable[[list[Any]], None],
        bus: EventBus,
        answer_questions: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]],
    ) -> None:
        self._request = request
        self._check_local_add = check_local_add
        self._bus = bus
        self._answer_questions = answer_questions
        self._queues: dict[str, QueueState] = {}

    def _queue(self, session_id: str) -> QueueState:
        return self._queues.setdefault(session_id, QueueState(session_id))

    def _publish_queue(self, session_id: str) -> None:
        state = self._queue(session_id)
        items = state.ordered()
        revision = max(
            state.snapshot_revision,
            max((item.revision for item in items), default=0),
        )
        self._bus.publish(
            QueueChanged(
                session_id=session_id,
                revision=revision,
                items=items,
            )
        )

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
            raw = await self._request("_hrns/capabilities/list", payload)
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
            raw = await self._request("_hrns/plugins/list", payload)
            return parse_plugin_list(raw)
        except (ValueError, TypeError) as exc:
            raise ProtocolError(f"plugins/list returned malformed data: {exc}") from exc

    async def configuration(self, session_id: str) -> ConfigurationSnapshot:
        try:
            raw = await self._request("_hrns/configuration/get", {"sessionId": session_id})
            return parse_configuration_snapshot(raw)
        except (ValueError, TypeError) as exc:
            raise ProtocolError(f"configuration/get returned malformed data: {exc}") from exc

    async def queue(self, session_id: str) -> QueueSnapshot:
        try:
            raw = await self._request("_hrns/queue/list", {"sessionId": session_id})
            snapshot = parse_queue_snapshot(raw)
            self._queue(session_id).merge_snapshot(snapshot)
        except QueueUpdateProtocolError:
            raise
        except (ValueError, TypeError) as exc:
            raise ProtocolError(f"queue/list returned malformed data: {exc}") from exc
        self._publish_queue(session_id)
        return snapshot

    async def edit_queue_prompt(
        self,
        session_id: str,
        prompt_id: str,
        prompt: list[Any],
        *,
        expected_revision: int,
        mutation_id: str | None = None,
    ) -> QueueUpdate:
        content = [
            dump(mode="json", by_alias=True, exclude_none=True)
            if callable(dump := getattr(block, "model_dump", None))
            else block
            for block in prompt
        ]
        request = QueueEditRequest(
            session_id=session_id,
            prompt_id=prompt_id,
            prompt=content,
            expected_revision=expected_revision,
            mutation_id=mutation_id or f"queue-edit-{uuid.uuid4().hex}",
        )
        try:
            raw = await self._request("_hrns/queue/edit", request.to_wire())
            update = parse_queue_update(raw)
            self._queue(session_id).accept(update)
        except QueueUpdateProtocolError:
            raise
        except (ValueError, TypeError) as exc:
            raise ProtocolError(f"queue/edit returned malformed data: {exc}") from exc
        self._publish_queue(session_id)
        return update

    async def remove_queue_prompt(
        self,
        session_id: str,
        prompt_id: str,
        *,
        expected_revision: int,
        mutation_id: str | None = None,
    ) -> QueueUpdate:
        request = QueueRemoveRequest(
            session_id=session_id,
            prompt_id=prompt_id,
            expected_revision=expected_revision,
            mutation_id=mutation_id or f"queue-remove-{uuid.uuid4().hex}",
        )
        try:
            raw = await self._request("_hrns/queue/remove", request.to_wire())
            update = parse_queue_update(raw)
            self._queue(session_id).accept(update)
        except QueueUpdateProtocolError:
            raise
        except (ValueError, TypeError) as exc:
            raise ProtocolError(f"queue/remove returned malformed data: {exc}") from exc
        self._publish_queue(session_id)
        return update

    async def retry_queue_prompt(
        self,
        session_id: str,
        prompt_id: str,
        *,
        mutation_id: str | None = None,
    ) -> QueueUpdate:
        payload = {
            "sessionId": session_id,
            "promptId": prompt_id,
            "mutationId": mutation_id or f"queue-retry-{uuid.uuid4().hex}",
        }
        try:
            raw = await self._request("_hrns/queue/retry", payload)
            update = parse_queue_update(raw)
            self._queue(session_id).accept(update)
        except QueueUpdateProtocolError:
            raise
        except (ValueError, TypeError) as exc:
            raise ProtocolError(f"queue/retry returned malformed data: {exc}") from exc
        self._publish_queue(session_id)
        return update

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
            raise HrnsClientError(f"Invalid configuration change: {exc}") from exc
        self._check_local_add(validated)
        try:
            payload = build_configuration_update(
                session_id, request_id, expected_revision, validated
            )
        except ValueError as exc:
            raise HrnsClientError(f"Invalid configuration change: {exc}") from exc
        try:
            raw = await self._request("_hrns/configuration/update", payload)
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
            raw = await self._request("_hrns/commands/list", payload)
            return parse_command_list(raw)
        except (ValueError, TypeError) as exc:
            raise ProtocolError(f"commands/list returned malformed data: {exc}") from exc

    async def steer(self, session_id: str, text: str) -> bool:
        """Inject a follow-up into the turn that is currently running."""
        raw = await self._request("_hrns/session/steer", {"sessionId": session_id, "text": text})
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
            raise HrnsClientError(
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
            raw = await self._request("_hrns/commands/invoke", payload)
            return parse_command_response(raw)
        except (ValueError, TypeError) as exc:
            raise ProtocolError(f"commands/invoke returned malformed data: {exc}") from exc

    async def cancel_command(self, session_id: str, request_id: str) -> dict[str, Any]:
        try:
            return await self._request(
                "_hrns/commands/cancel",
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
            raw = await self._request("_hrns/presentation/get", payload)
            return tuple(parse_presentation_list(raw).snapshots)
        except (ValueError, TypeError) as exc:
            raise ProtocolError(f"presentation/get returned malformed data: {exc}") from exc

    async def handle_ext_method(self, method: str, params: dict[str, Any]) -> Any:
        name = method.lstrip("_")
        if name == "hrns/ask_user":
            return await self._answer_questions(params)
        if method in {"hrns/configuration/changed", "_hrns/configuration/changed"}:
            snapshot = parse_configuration_snapshot(params)
            self._bus.publish(
                ConfigurationChanged(session_id=snapshot.session_id, snapshot=snapshot)
            )
            return {}
        raise RequestError.method_not_found(f"_{method.lstrip('_')}")

    async def handle_notification(self, method: str, params: dict[str, Any]) -> None:
        name = method.lstrip("_")
        if name == "hrns/configuration/changed":
            try:
                snapshot = parse_configuration_snapshot(params)
            except Exception:
                return
            self._bus.publish(
                ConfigurationChanged(session_id=snapshot.session_id, snapshot=snapshot)
            )
            return
        if name == "hrns/commands/changed":
            try:
                changed = CommandCatalogChanged.model_validate(params)
            except Exception:
                return
            self._bus.publish(
                PluginCommandsListed(
                    session_id=changed.session_id,
                    commands=tuple(changed.commands),
                    selected_revision=changed.selected_revision,
                    supported_operations=tuple(changed.supported_operations),
                )
            )
            return
        if name == "hrns/presentation/updated":
            try:
                updated = PresentationUpdated.model_validate(params)
            except Exception:
                return
            self._bus.publish(ToolPresentationUpdated(snapshots=tuple(updated.snapshots)))
            return
        if name == "hrns/queue/changed":
            try:
                update = parse_queue_update(params)
                accepted = self._queue(update.item.session_id).accept(update)
            except QueueUpdateProtocolError:
                raise
            except (ValueError, TypeError) as exc:
                raise QueueUpdateProtocolError(f"queue notification is malformed: {exc}") from exc
            if accepted:
                self._publish_queue(update.item.session_id)
            return
        return None
