"""Locally installed presentation adapters with a mandatory readable fallback.

A remote peer may send a renderer identifier and versioned JSON data, never code. Only
locally installed `harness.renderers` entry points can resolve to executable code, and an
unknown or failing adapter always yields sanitized fallback text.
"""

from __future__ import annotations

import importlib.metadata
import json
import re
from collections.abc import Callable
from typing import Any

from harness.protocol import PresentationPayload

__all__ = ["RendererRegistry", "readable_text"]

_ANSI_ESCAPE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f\u202a-\u202e\u2066-\u2069]")


def readable_text(value: Any, *, limit: int = 64_000) -> str:
    """Bound and sanitize untrusted activity for terminal display."""
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, indent=2, ensure_ascii=False, default=str)
        except (ValueError, TypeError, RecursionError):
            text = "[Activity details could not be rendered]"
    text = _CONTROL.sub("", _ANSI_ESCAPE.sub("", text))
    return text if len(text) <= limit else text[:limit] + "\n… output truncated"


class RendererRegistry:
    """Resolve versioned presentation data against local adapters only."""

    def __init__(self) -> None:
        self._renderers: dict[tuple[str, int], Callable[[dict[str, Any]], str]] = {}
        self._entry_points: dict[str, importlib.metadata.EntryPoint] = {}

    def register(
        self, renderer_id: str, version: int, renderer: Callable[[dict[str, Any]], str]
    ) -> None:
        key = (renderer_id, version)
        if key in self._renderers:
            raise ValueError(f"Duplicate renderer: {renderer_id} v{version}")
        if not renderer_id or version < 1 or not callable(renderer):
            raise ValueError("A renderer needs a non-empty id, version >= 1 and a callable")
        self._renderers[key] = renderer

    def discover(self) -> None:
        """Discover locally installed adapters without importing them yet."""
        discovered: dict[str, importlib.metadata.EntryPoint] = {}
        for entry in importlib.metadata.entry_points(group="harness.renderers"):
            if entry.name in discovered:
                raise ValueError(f"Duplicate renderer entry point: {entry.name}")
            discovered[entry.name] = entry
        self._entry_points = discovered

    def render(self, value: PresentationPayload | dict[str, Any], *, fallback: str) -> str:
        try:
            presentation = (
                value
                if isinstance(value, PresentationPayload)
                else PresentationPayload.model_validate(value)
            )
        except (ValueError, TypeError):
            return readable_text(fallback)
        key = (presentation.renderer_id, presentation.schema_version)
        renderer = self._renderers.get(key)
        if renderer is None:
            # Entry point names encode an exact id@version; remote text is never
            # interpreted as an import path.
            entry = self._entry_points.get(f"{key[0]}@{key[1]}")
            if entry is not None:
                try:
                    loaded = entry.load()
                    if not callable(loaded):
                        raise TypeError("Renderer entry point must be callable")
                    self._renderers[key] = loaded
                    renderer = loaded
                except Exception:
                    return readable_text(presentation.fallback)
        if renderer is None:
            return readable_text(presentation.fallback)
        try:
            rendered = renderer(presentation.data)
            if not isinstance(rendered, str):
                raise TypeError("Renderer must return text")
            return readable_text(rendered)
        except Exception:
            return readable_text(presentation.fallback)
