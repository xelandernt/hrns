"""Status bar and its theme-aware formatting helpers."""

from __future__ import annotations

from typing import Any, NamedTuple

from rich.color import Color
from rich.style import Style
from rich.text import Text
from textual.timer import Timer
from textual.widgets import Static


def _parse_color(value: str | None) -> Color | None:
    if not value:
        return None
    try:
        return Color.parse(value)
    except Exception:  # a theme may carry a colour name Rich does not know
        return None


class _Palette(NamedTuple):
    accent: Color | None = None
    foreground: Color | None = None
    muted: Color | None = None
    success: Color | None = None
    warning: Color | None = None
    error: Color | None = None


def _palette(widget: Static) -> _Palette:
    """Theme colours the band needs, tolerant of partial or ANSI themes."""
    try:
        theme = widget.app.current_theme
    except Exception:
        return _Palette()
    variables = theme.variables or {}
    return _Palette(
        accent=_parse_color(theme.accent or theme.primary),
        foreground=_parse_color(theme.foreground),
        muted=_parse_color(variables.get("text-muted")),
        success=_parse_color(theme.success),
        warning=_parse_color(theme.warning),
        error=_parse_color(theme.error),
    )


def _mix(start: Color, end: Color, weight: float) -> Color:
    weight = max(0.0, min(1.0, weight))
    left = start.get_truecolor()
    right = end.get_truecolor()
    return Color.from_rgb(
        round(left.red + (right.red - left.red) * weight),
        round(left.green + (right.green - left.green) * weight),
        round(left.blue + (right.blue - left.blue) * weight),
    )


def _clip(text: Text, width: int) -> Text:
    if width <= 0:
        return Text()
    if len(text) <= width:
        return text
    head = text[: width - 1].copy() if width > 1 else Text()
    head.append("…", style="dim")
    return head


class StatusBar(Static):
    """One line of chrome: live activity on the left, agent state on the right.

    The right cluster is pinned to the edge, so it never shifts while the
    activity label changes width. While a turn runs the label carries a slow
    gradient sweep instead of a spinner, and the timer driving it only exists
    while something is actually happening.
    """

    SWEEP_INTERVAL = 0.09
    SWEEP_SPREAD = 8

    def __init__(self, **kwargs: Any) -> None:
        kwargs.setdefault("markup", False)
        super().__init__("", **kwargs)
        self._connected = True
        self._model = ""
        self._effort = ""
        self._mode = ""
        self._usage = ""
        self._cost = ""
        self._permission = ""
        self._client_io = ""
        self._activity = ""
        self._hint = ""
        self._context_line = ""
        self._busy = False
        self._phase = 0
        self._timer: Timer | None = None

    def show(
        self,
        *,
        connected: bool,
        model: str = "",
        effort: str = "",
        mode: str = "",
        usage: str = "",
        cost: str = "",
        usage_detail: str = "",
        busy: bool = False,
        activity: str = "",
        permission: str = "",
        client_io: str = "",
        hint: str = "",
        context: str = "",
    ) -> None:
        self._connected = connected
        self._model = model
        self._effort = effort
        self._mode = mode
        self._usage = usage
        self._cost = cost
        self.tooltip = usage_detail or None
        self._permission = permission
        self._client_io = client_io
        self._activity = activity
        self._hint = hint
        self._context_line = context
        self._set_busy(busy)
        self.refresh()

    def _set_busy(self, busy: bool) -> None:
        if busy == self._busy:
            return
        self._busy = busy
        if busy:
            self._phase = 0
            self._start_timer()
        else:
            self._stop_timer()

    def _start_timer(self) -> None:
        if self._timer is None and self.is_mounted:
            self._timer = self.set_interval(self.SWEEP_INTERVAL, self._tick)

    def _stop_timer(self) -> None:
        if self._timer is not None:
            self._timer.stop()
            self._timer = None

    def _tick(self) -> None:
        self._phase += 1
        self.refresh()

    def on_mount(self) -> None:
        if self._busy:
            self._start_timer()

    def on_unmount(self) -> None:
        self._stop_timer()

    # ---------------------------------------------------------------- render

    def render(self) -> Text:
        width = self.size.width  # content width: padding is already excluded
        if width <= 0:
            return Text()
        # Mid-turn the activity label is the point of the band, so it keeps a
        # reserved slice of the line and the right cluster gives way instead.
        reserve = min(len(self._activity or "working") + 2, 26) if self._busy else 0
        right = self._right(max(0, width - reserve))
        budget = width - len(right) - 2
        left = self._left(budget) if budget >= 8 else Text()
        left = _clip(left, budget)
        gap = " " * max(2, width - len(left) - len(right))
        return left + Text(gap) + right

    def _left(self, budget: int) -> Text:
        if not self._busy:
            if self._hint:
                return Text(self._hint, style="dim")
            return self._context_text()
        label = self._activity or "working"
        text = self._sweep(label)
        # The hint is an extra, never a truncated fragment; it appears only when
        # the whole phrase fits beside the activity label.
        if self._hint and len(label) + len(self._hint) + 3 <= budget:
            text.append(f"   {self._hint}", style="dim")
        return text

    def _context_text(self) -> Text:
        if not self._context_line:
            return Text()
        palette = _palette(self)
        color = palette.success if self._connected else palette.error
        text = Text()
        text.append("● ", style=Style(color=color))
        text.append(self._context_line, style=Style(color=palette.muted))
        return text

    def _right(self, budget: int) -> Text:
        palette = _palette(self)
        model = (
            Text(self._model, style=Style(color=palette.accent or palette.foreground, bold=True))
            if self._model
            else None
        )
        mode = Text(self._mode, style=Style(color=palette.foreground)) if self._mode else None
        effort = (
            Text(self._effort, style=Style(color=palette.muted or palette.foreground))
            if self._effort
            else None
        )
        permission = (
            Text(self._permission, style=Style(color=palette.warning, bold=True))
            if self._permission and self._permission != "ask"
            else None
        )
        client_io = (
            Text(f"io {self._client_io}", style=Style(color=palette.accent, bold=True))
            if self._client_io and self._client_io != "off"
            else None
        )
        usage = (
            Text(f"ctx {self._usage}", style=Style(color=palette.foreground))
            if self._usage
            else None
        )
        cost = (
            Text(self._cost, style=Style(color=palette.muted or palette.foreground))
            if self._cost
            else None
        )
        connection = (
            None
            if self._connected
            else Text("○ disconnected", style=Style(color=palette.error, bold=True))
        )
        fields: list[tuple[str, Text | None]] = [
            ("model", model),
            ("mode", mode),
            ("effort", effort),
            ("permission", permission),
            ("client_io", client_io),
            ("usage", usage),
            ("cost", cost),
            ("connection", connection),
        ]

        def assemble() -> Text:
            identity = Text(" · ", style="dim").join(
                [piece for _, piece in fields[:3] if piece is not None]
            )
            text = Text()
            for piece in [identity, *(piece for _, piece in fields[3:])]:
                if piece is None or not piece.plain:
                    continue
                if text.plain:
                    text.append("   ")
                text += piece
            return text

        # Ambiguity is resolved by dropping the least load-bearing field first;
        # the model name and the two usage figures are the last to go.
        for name in ("client_io", "permission", "effort", "mode", "cost", "usage", "model"):
            if len(assemble()) <= budget:
                break
            fields = [(label, None if label == name else piece) for label, piece in fields]
        return _clip(assemble(), budget)

    def _sweep(self, label: str) -> Text:
        palette = _palette(self)
        if palette.accent is None:
            return Text(label, style="bold")
        # The label stays readable at rest; the sweep is a travelling accent,
        # not a fade from invisible to visible.
        base = palette.muted or palette.foreground or palette.accent
        cycle = len(label) + self.SWEEP_SPREAD
        head = (self._phase % cycle) - self.SWEEP_SPREAD
        text = Text()
        for index, char in enumerate(label):
            weight = max(0.0, 1.0 - abs(index - head) / self.SWEEP_SPREAD) ** 1.6
            text.append(
                char, style=Style(color=_mix(base, palette.accent, weight), bold=weight > 0.8)
            )
        return text
