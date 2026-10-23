"""Bind the upstream Ask User capability to the host's answerer resource."""

from __future__ import annotations

from pydantic_ai_harness.ask_user import AskUser

from harness.protocol import HarnessError

from ...registry import BindingContext
from ...resources import ANSWERS
from .config import AskUserConfig

__all__ = ["build_ask_user"]


def build_ask_user(config: AskUserConfig, binding: BindingContext) -> AskUser:
    """Bind the upstream capability to the host's ``user_answers`` resource.

    The resource is a declared ``need`` of this plugin, so composition has already rejected a
    definition that neither declares nor receives it; ``require`` raises with the slot's own
    message for the hand-built case (a factory called outside a session), where the context
    carries no resources at all.
    """
    resources = binding.resources
    answerer = (
        resources.require(ANSWERS, context="The Ask User capability")
        if resources is not None
        else None
    )
    if answerer is None:
        raise HarnessError(
            "RESOURCE_UNAVAILABLE",
            "The Ask User capability requires the service resource 'user_answers'.",
            details={"resource": ANSWERS.name, "kind": ANSWERS.kind},
        )
    return AskUser(answerer=answerer, timeout=config.timeout)
