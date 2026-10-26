"""Pure presentation helpers shared by the terminal widgets."""

from __future__ import annotations

from collections.abc import Mapping

from rich.segment import Segment
from rich.style import Style
from textual.strip import Strip

# One monochrome marker per tool call; status carries the meaning while it
# runs. The result body is shown as a short ``⎿`` preview beneath the header.
SPINNER_FRAMES = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")
# Collapsed cards stay a few rows tall; the rest is one click (or Enter) away.
TOOL_PREVIEW_LINES = 4
TOOL_OUTPUT_INDENT = "    ⎿  "

# Retained text per message block is bounded so a single very long streaming
# reply cannot grow the widget tree or make Markdown re-renders unbounded.
MAX_STREAM_MESSAGE_CHARS = 24_000


def bounded_user_text(text: str) -> str:
    """Keep a very large user prompt from creating an unbounded Static."""
    if len(text) <= MAX_STREAM_MESSAGE_CHARS:
        return text
    return "…" + text[-(MAX_STREAM_MESSAGE_CHARS - 1) :]


def truncate_text(text: str, width: int) -> str:
    """Clip `text` to `width` characters, marking the cut with an ellipsis."""
    if width <= 0:
        return ""
    if len(text) <= width:
        return text
    if width == 1:
        return "…"
    return text[: width - 1].rstrip() + "…"


def _restyle_spans(
    strip: Strip,
    spans: list[tuple[int, int, str]],
    styles: Mapping[str, Style],
) -> Strip:
    """Restyle character ranges of an already-rendered strip.

    ``Strip`` is immutable, so a new one is built by splitting segments at span
    boundaries. Characters keep their existing style; the span style is layered on
    top so cursor and selection styling still show through.
    """
    text = strip.text
    span_styles: list[Style | None] = [None] * len(text)
    for start, end, marker in spans:
        style = styles.get(marker)
        if style is None:
            continue
        for index in range(start, min(end, len(text))):
            span_styles[index] = style

    segments: list[Segment] = []
    position = 0
    for segment in strip._segments:
        chunk_text = segment.text
        run_start = 0
        for offset in range(1, len(chunk_text) + 1):
            ends_run = offset == len(chunk_text)
            if not ends_run and span_styles[position + offset] is span_styles[position + run_start]:
                continue
            span_style = span_styles[position + run_start]
            style = segment.style
            if span_style is not None:
                style = span_style if style is None else style + span_style
            segments.append(Segment(chunk_text[run_start:offset], style, segment.control))
            run_start = offset
        position += len(chunk_text)
    return Strip(segments, strip.cell_length)
