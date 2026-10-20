"""Shared Left/Right handling for enumerated terminal controls.

Textual's own widgets only react to Up/Down (or Enter) on most choice controls.
The terminal treats Left/Right as "previous/next value" everywhere: binary
controls toggle, longer value lists step and wrap, and pick lists move the
highlight. Text inputs and other widgets that consume the arrow keys themselves
never reach these helpers.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, TypeVar

from textual.widgets import Checkbox, ListView, OptionList, Select

T = TypeVar("T")

_DIRECTIONS = {"left": -1, "right": 1}


def choice_direction(key: str) -> int | None:
    """Return -1 for Left, 1 for Right, and None for any other key."""
    return _DIRECTIONS.get(key)


def stepped_value(values: Sequence[T], current: T | None, direction: int) -> T | None:
    """Return the value one step from ``current``, wrapping at the ends."""
    if not values:
        return None
    try:
        index = values.index(current)  # type: ignore[arg-type]
    except ValueError:
        index = -1 if direction > 0 else 0
    return values[(index + direction) % len(values)]


def _step_select(select: Select, direction: int) -> bool:
    # Select exposes no public ordered view of its choices; ``_options`` is the
    # stable list used by its own value watcher in the pinned Textual range.
    values = [value for _prompt, value in select._options]
    value = stepped_value(values, select.value, direction)
    if value is None:
        return False
    select.value = value
    return True


def handle_choice_arrow(widget: Any, key: str) -> bool:
    """Apply Left/Right to ``widget`` and report whether the key was consumed."""
    direction = choice_direction(key)
    if direction is None or widget is None or getattr(widget, "disabled", False):
        return False
    if isinstance(widget, Checkbox):
        widget.toggle()
        return True
    if isinstance(widget, Select):
        return _step_select(widget, direction)
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
