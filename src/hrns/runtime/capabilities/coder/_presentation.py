"""Tool presentation payloads emitted by the bundled coding tools.

The model sees ``ToolReturn.return_value``; the terminal sees the ``hrns.presentation``
block in the return metadata. Keeping the two apart is what lets a view carry colors,
diffs, and syntax-shaped data without paying for it in the prompt.
"""

from __future__ import annotations

from typing import Any

from pydantic_ai.messages import ToolReturn

from hrns.protocol import PresentationPayload

from ._ids import RENDERER_IDS, renderer_id

__all__ = ["RENDERER_IDS", "input_metadata", "output_return", "renderer_id"]

_FALLBACK_CHARS = 8_000


def _fallback(text: str) -> str:
    return text if len(text) <= _FALLBACK_CHARS else text[:_FALLBACK_CHARS] + "\n… truncated"


def input_metadata(tool: str, *, pending: str) -> dict[str, Any]:
    """Tool-definition metadata that mounts the view as soon as the call starts."""
    payload = PresentationPayload(
        renderer_id=renderer_id(tool),
        schema_version=1,
        data={"tool": tool},
        fallback=pending,
    )
    return {"hrns": {"presentation": {"input": payload.to_wire()}}}


def output_return(
    text: str,
    tool: str,
    data: dict[str, Any],
    *,
    fallback: str | None = None,
) -> ToolReturn:
    """A model-facing result plus the matching UI payload."""
    payload = PresentationPayload(
        renderer_id=renderer_id(tool),
        schema_version=1,
        data=data,
        fallback=_fallback(fallback if fallback is not None else text),
    )
    return ToolReturn(text, metadata={"hrns": {"presentation": {"output": payload.to_wire()}}})
