"""Locally installed presentation providers with a mandatory readable fallback.

A remote peer may send a renderer identifier and versioned JSON data, never code. Only
locally installed ``harness.renderers`` entry points resolve to executable adapters. This
module deliberately imports no UI library: providers offer Textual factories that the TUI
asks for explicitly, while headless/client-core usage only sees identifiers and fallback
text.
"""

from __future__ import annotations

import importlib.metadata
import json
import re
from typing import Any

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
    """Resolve versioned presentation identifiers against locally installed providers.

    A provider is an object (or a callable returning one) with optional ``command_view``
    and ``tool_view`` factories. The two factory kinds are independent; an invocation asks
    for exactly one kind, so a missing tool factory can never turn a command into a tool.
    """

    def __init__(self) -> None:
        self._providers: dict[tuple[str, int], Any] = {}
        self._entry_points: dict[str, importlib.metadata.EntryPoint] = {}

    def register(self, renderer_id: str, version: int, provider: Any) -> None:
        key = (renderer_id, version)
        if key in self._providers:
            raise ValueError(f"Duplicate renderer provider: {renderer_id} v{version}")
        if not renderer_id or version < 1 or provider is None:
            raise ValueError("A renderer needs a non-empty id, version >= 1 and a provider")
        self._providers[key] = provider

    def discover(self) -> None:
        """Discover locally installed providers without importing them yet."""
        discovered: dict[str, importlib.metadata.EntryPoint] = {}
        for entry in importlib.metadata.entry_points(group="harness.renderers"):
            if entry.name in discovered:
                raise ValueError(f"Duplicate renderer entry point: {entry.name}")
            discovered[entry.name] = entry
        self._entry_points = discovered

    def provider(self, renderer_id: str, version: int) -> Any | None:
        key = (renderer_id, version)
        provider = self._providers.get(key)
        if provider is not None:
            return provider
        entry = self._entry_points.get(f"{renderer_id}@{version}")
        if entry is None:
            return None
        try:
            loaded = entry.load()
            is_provider = hasattr(loaded, "tool_view") or hasattr(loaded, "command_view")
            provider = loaded if is_provider else loaded() if callable(loaded) else None
        except Exception:
            return None
        if provider is None:
            return None
        self._providers[key] = provider
        return provider

    def factory(self, renderer_id: str, version: int, kind: str) -> Any | None:
        if kind not in {"command", "tool"}:
            raise ValueError(f"Unknown view kind: {kind}")
        provider = self.provider(renderer_id, version)
        if provider is None:
            return None
        factory = getattr(provider, f"{kind}_view", None)
        return factory if callable(factory) else None
