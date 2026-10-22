"""The bundled Ask User capability: upstream ``AskUser`` plus a host answerer."""

from __future__ import annotations

from typing import Any

from pydantic_ai_harness.ask_user import AskUser

from harness.protocol import HarnessError

from .config import AskUserConfig

__all__ = ["build_ask_user"]


def build_ask_user(config: AskUserConfig, binding: Any) -> AskUser:
    """Bind the upstream capability to the host's ``user_answers`` resource."""
    resources = getattr(binding, "resources", None)
    answerer = resources.get("user_answers") if resources is not None else None
    if answerer is None:
        raise HarnessError(
            "RESOURCE_UNAVAILABLE",
            "The Ask User capability requires a 'user_answers' resource.",
            details={"resource": "user_answers", "kind": "service"},
        )
    return AskUser(answerer=answerer, timeout=config.timeout)
