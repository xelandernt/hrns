"""Client capabilities the connected ACP client offers, and the handle that uses them.

ACP negotiates capabilities in both directions. Everything else in this runtime acts in the
host workspace; a *client-origin* binding instead asks the connected client to do the work, so
its tools exist only while that client advertises what they need.

A binding names what it needs as paths into the ``clientCapabilities`` object
(``terminal``, ``fs.read_text_file``, ...). This module resolves those paths against whatever
the client actually advertised, so no capability has to be spelled out in code: a plugin that
requires a path the ACP schema already has needs no change here.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import get_args

from acp import Client, schema

from hrns.protocol import acp_capability_spelling

__all__ = [
    "ClientSession",
    "capability_path_known",
    "client_offers",
    "describe_missing",
    "missing_client_capability",
]


@dataclass(frozen=True, slots=True, kw_only=True)
class ClientSession:
    """The live connection handle for one session, as the client advertised it.

    ``client`` is the ACP connection the adapter speaks on. Passing it to a client-backed
    toolset routes that tool's I/O through the client instead of this host's disk or shell.
    """

    client: Client
    session_id: str
    cwd: str
    capabilities: schema.ClientCapabilities | None = None


def client_offers(capabilities: schema.ClientCapabilities | None, path: str) -> bool:
    """Whether the client advertised ``True`` at ``path`` during ``initialize``.

    Anything else — an absent path, an ``Optional`` left unset, a non-boolean value — counts as
    no. A capability is only in play when the peer said yes in so many words.
    """
    if capabilities is None or not path:
        return False
    current: object = capabilities
    for segment in path.split("."):
        current = getattr(current, segment, None)
        if current is None:
            return False
    return current is True


def missing_client_capability(
    capabilities: schema.ClientCapabilities | None,
    needs: Sequence[str],
) -> str | None:
    """The first path in ``needs`` the client did not advertise, if any."""
    for need in needs:
        if not client_offers(capabilities, need):
            return need
    return None


def capability_path_known(path: str) -> bool:
    """Whether ``path`` names a field of ACP's ``ClientCapabilities``.

    Checked when a plugin descriptor is registered, so a misspelled requirement fails at
    startup instead of leaving a binding permanently unavailable.
    """
    if not path:
        return False
    current: object = schema.ClientCapabilities
    for segment in path.split("."):
        fields = getattr(current, "model_fields", None)
        if not isinstance(fields, dict):
            return False
        field = fields.get(segment)
        if field is None:
            return False
        current = _without_optional(getattr(field, "annotation", None))
    return True


def _without_optional(annotation: object) -> object:
    for argument in get_args(annotation):
        if argument is not type(None):
            return argument
    return annotation


def describe_missing(needs: Sequence[str]) -> str:
    """Availability wording for the paths a client did not advertise."""
    return ", ".join(acp_capability_spelling(need) for need in needs)
