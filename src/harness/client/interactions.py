"""Policies for approvals and structured elicitation.

Headless operation must never silently approve an interaction. The default
policy (:class:`RequireInteractions`) raises :class:`InteractionRequired`,
which the runner reports while retaining the session. Interactive clients
implement the same protocol with dialogs; scripted automation can opt into
explicit allow/deny behavior.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from .errors import InteractionRejected, InteractionRequired

__all__ = [
    "ElicitationAnswer",
    "ElicitationRequest",
    "HeadlessInteractions",
    "InteractionPolicy",
    "PERMISSION_MODES",
    "PermissionDecision",
    "PermissionRequest",
    "READ_ONLY_KINDS",
    "RequireInteractions",
    "auto_permission",
]

#: Interactive approval modes. ``ask`` surfaces every request, ``reads``
#: auto-answers read-only tool kinds, and ``all`` auto-approves any call whose
#: agent offered an allow option. None of these is a sandbox.
PERMISSION_MODES = ("ask", "reads", "all")

#: Tool kinds that cannot change the workspace or reach the network. ``fetch``
#: is deliberately absent: it crosses the network boundary.
READ_ONLY_KINDS = frozenset({"read", "search", "think"})


@dataclass(frozen=True, slots=True)
class PermissionRequest:
    """A normalized ``session/request_permission`` request."""

    session_id: str
    tool_call_id: str
    title: str
    kind: str
    status: str
    options: tuple[dict[str, str], ...] = ()
    raw_input: Any = None
    detail: str = ""

    def option(self, option_id: str) -> dict[str, str] | None:
        for option in self.options:
            if option.get("optionId") == option_id:
                return option
        return None

    def first_of_kind(self, *kinds: str) -> dict[str, str] | None:
        for option in self.options:
            if option.get("kind") in kinds:
                return option
        return None

    def summary(self) -> str:
        return self.title or self.detail or f"tool call {self.tool_call_id}"


@dataclass(frozen=True, slots=True)
class PermissionDecision:
    option_id: str | None = None  # None means "cancelled"
    note: str = ""

    @property
    def selected(self) -> bool:
        return self.option_id is not None


def auto_permission(mode: str, request: PermissionRequest) -> PermissionDecision | None:
    """Return the decision an interactive mode can make without the user.

    ``None`` means "ask". An unknown mode is treated as ``ask``. A request with
    no tool kind is never auto-approved by ``reads``: failing safe matters more
    than a quiet transcript.
    """
    if mode not in {"reads", "all"}:
        return None
    if mode == "reads" and request.kind not in READ_ONLY_KINDS:
        return None
    option = request.first_of_kind("allow_once", "allow_always")
    if option is None:
        return None
    option_id = str(option.get("optionId") or "")
    return PermissionDecision(option_id=option_id) if option_id else None


@dataclass(frozen=True, slots=True)
class ElicitationRequest:
    """A normalized form or URL elicitation request."""

    kind: str  # "form" | "url"
    message: str
    schema: dict[str, Any] = field(default_factory=dict)
    url: str = ""
    session_id: str | None = None
    tool_call_id: str | None = None
    request_id: str | None = None
    elicitation_id: str | None = None

    @property
    def required_fields(self) -> list[str]:
        required = self.schema.get("required") or []
        return [str(item) for item in required if isinstance(item, str)]

    @property
    def properties(self) -> dict[str, Any]:
        props = self.schema.get("properties") or {}
        return props if isinstance(props, dict) else {}


@dataclass(frozen=True, slots=True)
class ElicitationAnswer:
    action: str = "decline"  # "accept" | "decline" | "cancel"
    content: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class InteractionPolicy(Protocol):
    """Implementations decide how an agent's questions are answered."""

    async def choose_permission(self, request: PermissionRequest) -> PermissionDecision: ...

    async def answer_elicitation(self, request: ElicitationRequest) -> ElicitationAnswer: ...


class RequireInteractions:
    """Default: refuse to make a decision the user asked to be present for."""

    async def choose_permission(self, request: PermissionRequest) -> PermissionDecision:
        raise InteractionRequired("permission", request.summary(), session_id=request.session_id)

    async def answer_elicitation(self, request: ElicitationRequest) -> ElicitationAnswer:
        raise InteractionRequired("elicitation", request.message, session_id=request.session_id)


class HeadlessInteractions:
    """Explicit non-interactive behavior for scripted runs.

    ``policy="required"`` is the default and equals :class:`RequireInteractions`.
    ``"allow"`` picks the agent's allow option (or accepts a form using
    caller-supplied answers), ``"deny"`` rejects/declines. Both are *explicit*
    choices made on the command line, never silent defaults.
    """

    def __init__(
        self,
        policy: str = "required",
        *,
        answers: dict[str, Any] | None = None,
        accept_url: bool = True,
    ) -> None:
        if policy not in {"required", "allow", "deny"}:
            raise ValueError(f"Unknown interaction policy '{policy}'")
        self.policy = policy
        self.answers = dict(answers or {})
        self.accept_url = accept_url

    async def choose_permission(self, request: PermissionRequest) -> PermissionDecision:
        if self.policy == "required":
            raise InteractionRequired(
                "permission", request.summary(), session_id=request.session_id
            )
        if self.policy == "allow":
            option = request.first_of_kind("allow_once", "allow_always")
            if option is None:
                raise InteractionRejected(
                    f"The agent offered no allow option for {request.summary()}; "
                    "refusing to select an unrelated choice."
                )
        else:
            option = request.first_of_kind("reject_once", "reject_always")
            if option is None:
                raise InteractionRejected(
                    f"The agent offered no reject option for {request.summary()}; "
                    "refusing to select an unrelated choice."
                )
        return PermissionDecision(option_id=option.get("optionId", ""))

    async def answer_elicitation(self, request: ElicitationRequest) -> ElicitationAnswer:
        if self.policy == "required":
            raise InteractionRequired("elicitation", request.message, session_id=request.session_id)
        if self.policy == "deny":
            return ElicitationAnswer(action="decline")
        if request.kind == "url":
            return (
                ElicitationAnswer(action="accept")
                if self.accept_url
                else ElicitationAnswer(action="decline")
            )
        content: dict[str, Any] = {}
        for name, prop in request.properties.items():
            if name in self.answers:
                content[name] = self.answers[name]
            elif isinstance(prop, dict) and "default" in prop:
                content[name] = prop["default"]
        missing = [name for name in request.required_fields if name not in content]
        if missing:
            raise InteractionRejected(
                "The elicitation requires answers for "
                + ", ".join(missing)
                + "; pass them with --answer FIELD=VALUE."
            )
        return ElicitationAnswer(action="accept", content=content)
