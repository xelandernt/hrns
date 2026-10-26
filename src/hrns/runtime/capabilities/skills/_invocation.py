"""Explicit skill references: `$name` in the user's message forces a skill into the turn."""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from typing import Any

from pydantic_ai.messages import ModelMessage, ModelRequest, UserPromptPart

__all__ = ["explicit_skill_names", "latest_user_text"]

#: Agent Skills names: lowercase letters, digits, and hyphens, up to 64 characters.
_SKILL_REFERENCE = re.compile(r"(?<![\w$])\$([a-z0-9][a-z0-9-]{0,63})")


def explicit_skill_names(text: str, available: Iterable[str]) -> list[str]:
    """Skill names the text mentions as `$name`, in first-mention order.

    A token that matches no discovered skill is ignored, so prices, shell variables, and
    unfamiliar `$names` pass through untouched.
    """
    known = set(available)
    ordered: list[str] = []
    seen: set[str] = set()
    for match in _SKILL_REFERENCE.finditer(text):
        name = match.group(1)
        if name in known and name not in seen:
            seen.add(name)
            ordered.append(name)
    return ordered


def latest_user_text(
    prompt: str | Sequence[Any] | None,
    messages: Sequence[ModelMessage] = (),
) -> str:
    """The current turn's user text: the run prompt, else a trailing user message.

    Falling back only when the *last* message is a request avoids re-triggering a
    `$name` from an earlier turn on a continuation run.
    """
    text = _content_text(prompt)
    if text is not None:
        return text
    if not messages:
        return ""
    last = messages[-1]
    if not isinstance(last, ModelRequest):
        return ""
    for part in reversed(last.parts):
        if isinstance(part, UserPromptPart):
            found = _content_text(part.content)
            if found is not None:
                return found
    return ""


def _content_text(content: object) -> str | None:
    if isinstance(content, str):
        return content
    if isinstance(content, Sequence):
        chunks: list[str] = []
        for item in content:
            if isinstance(item, str):
                chunks.append(item)
            else:
                value = getattr(item, "text", None)
                if isinstance(value, str):
                    chunks.append(value)
        if chunks:
            return "\n".join(chunks)
    return None
