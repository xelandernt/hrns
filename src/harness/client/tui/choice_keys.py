"""Shared Left/Right handling for enumerated terminal controls.

Textual's own widgets only react to Up/Down (or Enter) on most pick lists.
The terminal treats Left/Right as "previous/next" everywhere: pick lists move
the highlight. Text inputs and other widgets that consume the arrow keys
themselves never reach these helpers.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, TypeVar

from textual.widgets import ListView, OptionList

T = TypeVar("T")

_DIRECTIONS = {"left": -1, "right": 1}


def choice_direction(key: str) -> int | None:
    """Return -1 for Left, 1 for Right, and None for any other key."""
    return _DIRECTIONS.get(key)


def stepped_value(values: Sequence[T], current: T | None, direction: int) -> T | None:
    """Return the value one step from ``current``, wrapping at the ends."""
    if not values:
        return None
    for index, value in enumerate(values):
        if value == current:
            break
    else:
        index = -1 if direction > 0 else 0
    return values[(index + direction) % len(values)]


def handle_choice_arrow(widget: Any, key: str) -> bool:
    """Apply Left/Right to ``widget`` and report whether the key was consumed."""
    direction = choice_direction(key)
    if direction is None or widget is None or getattr(widget, "disabled", False):
        return False
    if isinstance(widget, OptionList):
        if direction < 0:
            widget.action_cursor_up()
        else:
            widget.action_cursor_down()
        return True
    if isinstance(widget, ListView):
        if direction < 0:
            widget.action_cursor_up()
        else:
            widget.action_cursor_down()
        return True
    return False
