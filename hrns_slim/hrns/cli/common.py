"""Shared CLI option types and extra-availability checks."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace
from typing import Annotated, Literal

from cyclopts import Parameter

from ..shared.config import HrnsConfig
from ..shared.errors import HrnsClientError

ConfigOption = Annotated[
    Path | None,
    Parameter(
        name=["--config", "--config-file"],
        metavar="PATH",
        help="Select a configuration file. Also HRNS_CONFIG_FILE.",
    ),
]
RepeatedStringOption = Annotated[list[str] | None, Parameter(negative_iterable=())]
ToolOption = Annotated[
    list[str] | None,
    Parameter(name="--tool", metavar="NAME", negative_iterable=()),
]
ExcludeToolOption = Annotated[
    list[str] | None,
    Parameter(name="--exclude-tool", metavar="NAME", negative_iterable=()),
]
ClientIoOption = Annotated[
    Literal["off", "read", "all"] | None,
    Parameter(
        name="--client-io",
        help=(
            "Advertise client-side capabilities to the agent: 'off' offers nothing, 'read' "
            "serves fs/read_text_file, and 'all' also serves writes and terminal/* so the "
            "agent runs commands in this terminal. Defaults to 'all' unless a profile or "
            "TUI setting overrides it."
        ),
    ),
]


def _namespace(**values: object) -> SimpleNamespace:
    return SimpleNamespace(**values)


def _config(args: SimpleNamespace) -> HrnsConfig:
    """Read the selected or default configuration."""
    path = getattr(args, "config", None)
    return HrnsConfig(config_file=path) if path is not None else HrnsConfig()


def _require_headless_extra() -> None:
    """Fail with an install hint instead of an ImportError from a missing extra."""
    if all(
        importlib.util.find_spec(name) is not None
        for name in ("acp", "websockets", "fastmcp", "mcp")
    ):
        return
    raise HrnsClientError(
        "This command needs the headless extra. Install it with `pip install 'hrns-slim[headless]>0.0.28'`."
    )


def _require_server_extra() -> None:
    """Fail with an install hint instead of an ImportError from a missing extra."""
    if all(
        importlib.util.find_spec(name) is not None
        for name in ("pydantic_ai", "pydantic_ai_harness", "acp", "mcp", "fastmcp")
    ):
        return
    raise HrnsClientError(
        "This command needs the server extra. Install it with `pip install 'hrns-slim[server]'`."
    )
