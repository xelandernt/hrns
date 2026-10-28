"""Build the ACP-client-backed terminal capability for one session."""

from __future__ import annotations

from pydantic_ai.capabilities import Toolset
from pydantic_ai_harness.experimental.acp import AcpTerminalToolset

from hrns.protocol import TERMINAL, HrnsError

from ...client_io import client_offers
from ...registry import BindingContext
from ...resources import CLIENT

__all__ = ["build_client_terminal"]


def build_client_terminal(binding: BindingContext) -> Toolset[object]:
    """Wrap the client-backed terminal toolset in the capability this binding contributes."""
    resources = binding.resources
    session = resources.require(CLIENT) if resources is not None else None
    if session is None or not client_offers(session.capabilities, TERMINAL):
        raise HrnsError(
            "CAPABILITY_UNAVAILABLE",
            f"Binding {binding.binding_id!r} needs a client that advertises terminal support, "
            "and the connected client does not.",
            details={"binding_id": binding.binding_id, "requires_client": [TERMINAL]},
        )
    toolset = AcpTerminalToolset[object](
        client=session.client,
        session_id=session.session_id,
        cwd=session.cwd or None,
    )
    return Toolset(toolset, id="hrns.client_terminal")
