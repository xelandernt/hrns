"""A measured window over retained transcript blocks."""

from __future__ import annotations

import asyncio
from bisect import bisect_right
from collections.abc import Callable
from dataclasses import dataclass

from textual import events
from textual.app import ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.widget import Widget
from textual.widgets import Static


@dataclass(eq=False)
class HistoryEntry:
    """Stable history identity, independent of its current widget instance."""

    block: Widget
    height: int = 1
    offset: int = 0
    wrapper: HistoryItem | None = None
    needs_restore: bool = False


class HistoryItem(Vertical):
    DEFAULT_CSS = "HistoryItem { height: auto; }"

    def __init__(self, block: Widget, resized: Callable[[], None]) -> None:
        super().__init__(block)
        self._resized = resized

    def on_resize(self, event: events.Resize) -> None:
        self._resized()


class HistoryPane(VerticalScroll):
    """Keep only a viewport and overscan mounted, with spacers for other history."""

    def __init__(
        self,
        *,
        id: str | None = None,
        name: str | None = None,
        classes: str | None = None,
        disabled: bool = False,
    ) -> None:
        super().__init__(id=id, name=name, classes=classes, disabled=disabled)
        self._blocks: list[HistoryEntry] = []
        self._history_lock = asyncio.Lock()
        self._before = Static("")
        self._after = Static("")
        self._before.styles.height = self._after.styles.height = 0
        self._visibility_pending = False
        self._visibility_revision = 0
        self._scroll_anchor: tuple[HistoryEntry, float] | None = None
        self._scroll_adjustment = 0.0
        self._compensating_scroll = False
        self._following = True

    def compose(self) -> ComposeResult:
        yield self._before
        yield self._after

    def restore_block(self, block: Widget) -> Widget:
        """Rebuild a previously unmounted block from its retained state."""
        raise NotImplementedError

    def _schedule_visibility(self) -> None:
        self._visibility_revision += 1
        if self.is_mounted and not self._visibility_pending:
            self._visibility_pending = True
            self.call_after_refresh(self._update_visible_blocks)

    def _anchor_position(self) -> float:
        if self._scroll_anchor is None:
            return 0.0
        entry, inset = self._scroll_anchor
        return entry.offset + min(inset, max(0, entry.height - 1))

    def _measure(self) -> None:
        if not self._following:
            self._capture_anchor()
        previous_anchor = self._anchor_position()
        offset = 0
        for entry in self._blocks:
            if entry.wrapper is not None and entry.wrapper.size.height:
                entry.height = entry.wrapper.size.height
            entry.offset = offset
            offset += entry.height
        self._scroll_adjustment += self._anchor_position() - previous_anchor

    def _capture_anchor(self) -> None:
        if self._scroll_anchor is None and self._blocks:
            index = max(0, bisect_right([e.offset for e in self._blocks], self.scroll_y) - 1)
            entry = self._blocks[index]
            self._scroll_anchor = (entry, self.scroll_y - entry.offset)

    def _restore_anchor(self) -> None:
        adjustment, self._scroll_adjustment = self._scroll_adjustment, 0.0
        if not adjustment or self._following:
            return
        # Translate the current position and destination by the measured change.
        # Ordinary scroll frames need no correction and must keep their animation.
        position = self.scroll_y + adjustment
        target = self.scroll_target_y + adjustment
        animated = self.app.animator.is_being_animated(self, "scroll_y")
        self._compensating_scroll = True
        try:
            self.scroll_to(y=position, animate=False, immediate=True, force=True)
            if animated:
                self.scroll_to(y=target, animate=True, immediate=True, force=True)
        finally:
            self._compensating_scroll = False

    async def _update_visible_blocks(self) -> None:
        async with self._history_lock:
            await self._update_window()

    async def _update_window(self) -> None:
        revision = self._visibility_revision
        try:
            following = self._following
            if following:
                self._scroll_anchor = None
                self._scroll_adjustment = 0.0
            self._measure()
            entries = self._blocks
            if not entries:
                return
            height = max(1, self.scrollable_content_region.height)
            total = entries[-1].offset + entries[-1].height
            position = max(0, total - height) if following else self.scroll_y
            if self._scroll_anchor is not None:
                position = self._anchor_position()
            offsets = [entry.offset for entry in entries]
            start = max(0, bisect_right(offsets, position - height) - 1)
            stop = min(len(entries), bisect_right(offsets, position + height * 2) + 1)
            with self.app.batch_update():
                hidden = [
                    entry
                    for index, entry in enumerate(entries)
                    if entry.wrapper is not None and not start <= index < stop
                ]
                wrappers = [entry.wrapper for entry in hidden if entry.wrapper is not None]
                if any(wrapper.has_focus_within for wrapper in wrappers):
                    self.focus(scroll_visible=False)
                if wrappers:
                    await self.remove_children(wrappers)
                for entry in hidden:
                    entry.wrapper = None
                    entry.needs_restore = True
                for index in range(start, stop):
                    entry = entries[index]
                    if entry.wrapper is not None:
                        continue
                    if entry.needs_restore:
                        entry.block = self.restore_block(entry.block)
                        entry.needs_restore = False
                    wrapper = HistoryItem(entry.block, self._schedule_visibility)
                    next_wrapper = next(
                        (e.wrapper for e in entries[index + 1 : stop] if e.wrapper is not None),
                        self._after,
                    )
                    entry.wrapper = wrapper
                    await self.mount(wrapper, before=next_wrapper)
                self._before.styles.height = entries[start].offset
                self._after.styles.height = (
                    total - entries[stop - 1].offset - entries[stop - 1].height
                )
            self.call_after_refresh(self._settle_window)
        finally:
            self._visibility_pending = False
            if revision != self._visibility_revision:
                self._schedule_visibility()

    def _settle_window(self) -> None:
        if not self.is_mounted:
            return
        self._measure()
        mounted = [entry for entry in self._blocks if entry.wrapper is not None]
        if mounted:
            self._before.styles.height = mounted[0].offset
            total = self._blocks[-1].offset + self._blocks[-1].height
            self._after.styles.height = total - mounted[-1].offset - mounted[-1].height
        # Apply after spacer layout, when max_scroll_y reflects the new measurements.
        self.call_after_refresh(self._restore_anchor)

    async def _append_history(self, block: Widget) -> HistoryEntry:
        async with self._history_lock:
            entry = HistoryEntry(block)
            self._blocks.append(entry)
            self._schedule_visibility()
            return entry

    def reveal_entry(self, entry: HistoryEntry) -> None:
        self._scroll_adjustment = 0.0
        self._scroll_anchor = (entry, 0)
        self._compensating_scroll = True
        try:
            self.scroll_to(y=entry.offset, animate=False, immediate=True, force=True)
        finally:
            self._compensating_scroll = False
        self._schedule_visibility()

    def on_resize(self, event: events.Resize) -> None:
        self._schedule_visibility()

    async def clear_history(self) -> None:
        async with self._history_lock:
            self._blocks.clear()
            self._scroll_anchor = None
            self._scroll_adjustment = 0.0
            await self.remove_children(HistoryItem)
            self._before.styles.height = self._after.styles.height = 0
