"""Typed screen base shared by the full-screen views."""

from __future__ import annotations

from typing import TYPE_CHECKING

from textual.screen import Screen

if TYPE_CHECKING:
    from .app import HrnsApp


class _HrnsScreen(Screen[None]):
    """Screen base whose ``app`` is the concrete hrns app for type checking.

    Textual types ``app`` as the generic ``App[object]``; at runtime this class adds no
    behavior, so the real Textual property remains in effect.
    """

    if TYPE_CHECKING:

        @property
        def app(self) -> HrnsApp: ...
