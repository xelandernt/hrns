"""Turn execution and session config options for one connected client."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from acp import RequestError
from acp.schema import TextContentBlock

from ..shared.errors import (
    ConnectionLost,
    HrnsClientError,
    ProtocolError,
    TransportError,
    describe_rpc_error,
)
from .constants import MAX_MESSAGE_BYTES
from .events import ConfigOptionsUpdated, SessionExecutionState, TurnCompleted, TurnStarted
from .skills import SKILL_META_KEY, SkillMention

if TYPE_CHECKING:
    from .client import HrnsAcpClient

#: Text content maximum when the caller has not pre-validated an attachment.
_TOTAL_PROMPT_BYTES = MAX_MESSAGE_BYTES


def _with_skill_meta(blocks: list[Any], skills: tuple[SkillMention, ...]) -> list[Any]:
    """Attach the skill references to the prompt's first text block.

    ACP reserves ``_meta`` for exactly this kind of out-of-band payload, and the block
    is where it belongs: the text the person typed is unchanged, and the server reads
    the ids rather than parsing the text.
    """
    if not skills:
        return blocks
    payload = [mention.to_meta() for mention in skills]
    for index, block in enumerate(blocks):
        kind = block.get("type") if isinstance(block, dict) else getattr(block, "type", None)
        if kind != "text":
            continue
        if isinstance(block, dict):
            updated = dict(block)
            meta = dict(updated.get("_meta") or {})
            meta[SKILL_META_KEY] = payload
            updated["_meta"] = meta
        else:
            meta = dict(block.field_meta or {})
            meta[SKILL_META_KEY] = payload
            updated = block.model_copy(update={"field_meta": meta})
        return [*blocks[:index], updated, *blocks[index + 1 :]]
    return blocks


class TurnService:
    """Owns prompts, cancellation, modes, and config-option writes."""

    def __init__(self, client: HrnsAcpClient) -> None:
        self.client = client
        self._revisions: dict[str, int] = {}
        self._active: dict[str, str | None] = {}

    def reset(self) -> None:
        self._revisions.clear()
        self._active.clear()

    def forget_session(self, session_id: str) -> None:
        """A restored runtime may restart its revision sequence."""
        self._revisions.pop(session_id, None)
        self._active.pop(session_id, None)

    def is_running(self, session_id: str) -> bool:
        return self._active.get(session_id) is not None

    @property
    def server_lifecycle(self) -> bool:
        meta = getattr(self.client.initialized, "field_meta", None)
        return isinstance(meta, dict) and meta.get("hrns/session/state") is True

    def apply_server_state(self, state: dict[str, object]) -> None:
        session_id, revision = state.get("sessionId"), state.get("revision")
        status, turn_id = state.get("status"), state.get("activeTurnId")
        if not isinstance(session_id, str) or not isinstance(revision, int):
            return
        if status not in {"idle", "running", "waiting"}:
            return
        if turn_id is not None and not isinstance(turn_id, str):
            return
        if revision <= self._revisions.get(session_id, -1):
            return
        self._revisions[session_id] = revision
        previous = self._active.get(session_id)
        self._active[session_id] = turn_id
        if turn_id is not None and turn_id != previous:
            self.client.bus.publish(TurnStarted(session_id=session_id, prompt_text=""))
        elif turn_id is None and previous is not None:
            outcome = state.get("lastOutcome")
            usage = state.get("lastUsage")
            self.client.bus.publish(
                TurnCompleted(
                    session_id=session_id,
                    stop_reason=outcome if isinstance(outcome, str) else "end_turn",
                    usage=usage if isinstance(usage, dict) else None,
                )
            )
        self.client.bus.publish(
            SessionExecutionState(session_id=session_id, status=str(status), turn_id=turn_id)
        )

    def check_prompt_capabilities(self, blocks: list[Any]) -> None:
        """Never send a content kind the agent did not advertise."""
        client = self.client
        capabilities = getattr(
            getattr(client.initialized, "agent_capabilities", None),
            "prompt_capabilities",
            None,
        )
        allowed = {
            "image": bool(getattr(capabilities, "image", False)),
            "audio": bool(getattr(capabilities, "audio", False)),
            "resource": bool(getattr(capabilities, "embedded_context", False)),
        }
        for block in blocks:
            kind = block.get("type") if isinstance(block, dict) else getattr(block, "type", None)
            if kind in allowed and not allowed[kind]:
                label = "embedded context" if kind == "resource" else kind
                raise ProtocolError(
                    f"The agent did not advertise {label} prompt support; "
                    "attach text in the prompt instead."
                )

    async def prompt(
        self,
        session_id: str,
        prompt: str | list[Any],
        *,
        display_text: str | None = None,
        skills: Sequence[SkillMention] = (),
    ) -> Any:
        client = self.client
        client._pending_interaction_error = None
        blocks = (
            prompt if isinstance(prompt, list) else [TextContentBlock(type="text", text=prompt)]
        )
        blocks = _with_skill_meta(list(blocks), tuple(skills))
        self.check_prompt_capabilities(blocks)
        payloads: list[Any] = []
        for block in blocks:
            try:
                payloads.append(block.model_dump(mode="json", by_alias=True, exclude_none=True))
            except AttributeError:
                payloads.append(block)
        # Match the SDK's wire serialization (ASCII-escaped JSON, compact
        # separators) and include the JSON-RPC envelope so multi-byte text and
        # escaped characters cannot slip past the frame limit.
        envelope = {
            "jsonrpc": "2.0",
            "id": 2**63 - 1,
            "method": "session/prompt",
            "params": {"sessionId": session_id, "prompt": payloads},
        }
        total = len(
            json.dumps(envelope, ensure_ascii=True, separators=(",", ":"), default=str).encode(
                "utf-8"
            )
        )
        if total > _TOTAL_PROMPT_BYTES:
            raise TransportError(
                f"The prompt payload is {total:,} bytes; the transport accepts at most "
                f"{MAX_MESSAGE_BYTES:,} bytes. Attach a smaller file."
            )
        hint = (
            display_text
            if display_text is not None
            else (prompt if isinstance(prompt, str) else "")
        )
        if not self.server_lifecycle:
            client.bus.publish(TurnStarted(session_id=session_id, prompt_text=hint))
        try:
            response = await client.connection.prompt(session_id=session_id, prompt=blocks)
        except RequestError as exc:
            await client.drain_events()
            detail = describe_rpc_error("session/prompt", exc)
            client.bus.publish(
                TurnCompleted(session_id=session_id, stop_reason="error", detail=detail)
            )
            raise HrnsClientError(detail) from exc
        except asyncio.CancelledError:
            client.bus.publish(TurnCompleted(session_id=session_id, stop_reason="cancelled"))
            raise
        except (ConnectionError, asyncio.IncompleteReadError) as exc:
            await client.drain_events()
            detail = f"The connection was lost during the turn: {exc}"
            client.bus.publish(
                TurnCompleted(session_id=session_id, stop_reason="connection_lost", detail=detail)
            )
            raise ConnectionLost(detail) from exc
        await client.drain_events()
        if client._pending_interaction_error is not None:
            interaction = client._pending_interaction_error
            client._pending_interaction_error = None
            client.bus.publish(
                TurnCompleted(
                    session_id=session_id,
                    stop_reason="interaction_required",
                    detail=interaction.message,
                )
            )
            raise interaction
        usage = (
            response.usage.model_dump(mode="json", by_alias=True, exclude_none=True)
            if response.usage
            else None
        )
        if not self.server_lifecycle:
            client.bus.publish(
                TurnCompleted(session_id=session_id, stop_reason=response.stop_reason, usage=usage)
            )
        return response

    async def cancel(self, session_id: str) -> None:
        try:
            await self.client.connection.cancel(session_id=session_id)
        except Exception as exc:  # cancellation is best-effort at the wire level
            raise HrnsClientError(f"Cancellation could not be delivered: {exc}") from exc

    async def set_mode(self, session_id: str, mode_id: str) -> None:
        try:
            await self.client.connection.set_session_mode(session_id=session_id, mode_id=mode_id)
        except RequestError as exc:
            raise HrnsClientError(describe_rpc_error("session/set_mode", exc)) from exc

    async def set_config_option(
        self, session_id: str, config_id: str, value: str | bool
    ) -> tuple[dict[str, Any], ...]:
        client = self.client
        try:
            response = await client.connection.set_config_option(
                config_id=config_id, session_id=session_id, value=value
            )
        except RequestError as exc:
            raise HrnsClientError(describe_rpc_error("session/set_config_option", exc)) from exc
        options = tuple(
            option.model_dump(mode="json", by_alias=True, exclude_none=True)
            for option in response.config_options
        )
        client.bus.publish(ConfigOptionsUpdated(session_id=session_id, options=options))
        return options
