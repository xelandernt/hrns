"""Policies for approvals and structured elicitation.

Headless operation must never silently approve an interaction. The default
policy (:class:`RequireInteractions`) raises :class:`InteractionRequired`,
which the runner reports while retaining the session. Interactive clients
implement the same protocol with dialogs; scripted automation can opt into
explicit allow/deny behavior.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from .errors import InteractionRejected, InteractionRequired

__all__ = [
    "AskUserAnswer",
    "AskUserOption",
    "AskUserQuestion",
    "AskUserRequest",
    "AskUserResponse",
    "ElicitationAnswer",
    "ElicitationRequest",
    "HeadlessInteractions",
    "InteractionPolicy",
    "PERMISSION_MODES",
    "PermissionDecision",
    "PermissionRequest",
    "READ_ONLY_KINDS",
    "RequireInteractions",
    "ask_user_payload",
    "auto_permission",
    "parse_ask_user_request",
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
    option_id = option.get("optionId") or ""
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
        return [item for item in required if isinstance(item, str)]

    @property
    def properties(self) -> dict[str, Any]:
        props = self.schema.get("properties") or {}
        return props if isinstance(props, dict) else {}


@dataclass(frozen=True, slots=True)
class ElicitationAnswer:
    action: str = "decline"  # "accept" | "decline" | "cancel"
    content: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class AskUserOption:
    """One choice offered by an agent question."""

    label: str
    description: str = ""


@dataclass(frozen=True, slots=True)
class AskUserQuestion:
    """One multiple-choice question the agent asks mid-run."""

    header: str
    question: str
    options: tuple[AskUserOption, ...] = ()
    multi_select: bool = False


@dataclass(frozen=True, slots=True)
class AskUserRequest:
    """A normalized ``_hrns/ask_user`` request from the agent."""

    request_id: str
    questions: tuple[AskUserQuestion, ...] = ()
    session_id: str | None = None

    def __post_init__(self) -> None:
        if not self.questions:
            raise ValueError("an ask-user request needs at least one question")


@dataclass(frozen=True, slots=True)
class AskUserAnswer:
    """What the user picked for one question."""

    header: str
    selected: tuple[str, ...] = ()
    custom_answer: str | None = None


@dataclass(frozen=True, slots=True)
class AskUserResponse:
    """The user's answers, or a cancellation when the user declined."""

    answers: tuple[AskUserAnswer, ...] = ()
    cancelled: bool = False


def _mapping(value: Any, what: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{what} must be an object")
    return value


def _text(value: Any, what: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{what} must be a non-empty string")
    return value


def parse_ask_user_request(params: Mapping[str, Any]) -> AskUserRequest:
    """Parse the payload of an agent-initiated ``_hrns/ask_user`` request."""
    payload = _mapping(params, "ask-user parameters")
    raw_questions = payload.get("questions")
    if not isinstance(raw_questions, list) or not raw_questions:
        raise ValueError("ask-user parameters need a non-empty questions list")
    questions: list[AskUserQuestion] = []
    for index, raw in enumerate(raw_questions):
        item = _mapping(raw, f"question {index}")
        options: list[AskUserOption] = []
        raw_options = item.get("options")
        if not isinstance(raw_options, list) or not raw_options:
            raise ValueError(f"question {index} needs a non-empty options list")
        for option_index, raw_option in enumerate(raw_options):
            option = _mapping(raw_option, f"question {index} option {option_index}")
            description = option.get("description")
            options.append(
                AskUserOption(
                    label=_text(
                        option.get("label"), f"question {index} option {option_index} label"
                    ),
                    description=description if isinstance(description, str) else "",
                )
            )
        questions.append(
            AskUserQuestion(
                header=_text(item.get("header"), f"question {index} header"),
                question=_text(item.get("question"), f"question {index} text"),
                options=tuple(options),
                multi_select=bool(item.get("multiSelect", item.get("multi_select", False))),
            )
        )
    request_id = payload.get("requestId") or payload.get("request_id") or ""
    session_id = payload.get("sessionId") or payload.get("session_id")
    return AskUserRequest(
        request_id=str(request_id),
        questions=tuple(questions),
        session_id=session_id if isinstance(session_id, str) else None,
    )


def ask_user_payload(response: AskUserResponse) -> dict[str, Any]:
    """Serialize an :class:`AskUserResponse` for the runtime."""
    if response.cancelled:
        return {"cancelled": True, "answers": []}
    answers: list[dict[str, Any]] = []
    for answer in response.answers:
        item: dict[str, Any] = {"header": answer.header, "selected": list(answer.selected)}
        if answer.custom_answer is not None:
            item["customAnswer"] = answer.custom_answer
        answers.append(item)
    return {"cancelled": False, "answers": answers}


@runtime_checkable
class InteractionPolicy(Protocol):
    """Implementations decide how an agent's questions are answered."""

    async def choose_permission(self, request: PermissionRequest) -> PermissionDecision: ...

    async def answer_elicitation(self, request: ElicitationRequest) -> ElicitationAnswer: ...

    async def answer_questions(self, request: AskUserRequest) -> AskUserResponse: ...


class RequireInteractions:
    """Default: refuse to make a decision the user asked to be present for."""

    async def choose_permission(self, request: PermissionRequest) -> PermissionDecision:
        raise InteractionRequired("permission", request.summary(), session_id=request.session_id)

    async def answer_elicitation(self, request: ElicitationRequest) -> ElicitationAnswer:
        raise InteractionRequired("elicitation", request.message, session_id=request.session_id)

    async def answer_questions(self, request: AskUserRequest) -> AskUserResponse:
        raise InteractionRequired(
            "question",
            request.questions[0].question,
            session_id=request.session_id,
        )


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

    async def answer_questions(self, request: AskUserRequest) -> AskUserResponse:
        if self.policy == "required":
            raise InteractionRequired(
                "question", request.questions[0].question, session_id=request.session_id
            )
        if self.policy == "deny":
            return AskUserResponse(cancelled=True)
        answers: list[AskUserAnswer] = []
        missing: list[str] = []
        for question in request.questions:
            if question.header not in self.answers:
                missing.append(question.header)
                continue
            value = self.answers[question.header]
            if isinstance(value, list | tuple):
                selected = tuple(str(item) for item in value)
            else:
                selected = tuple(part.strip() for part in str(value).split(",") if part.strip())
            if not selected:
                missing.append(question.header)
                continue
            answers.append(AskUserAnswer(header=question.header, selected=selected))
        if missing:
            raise InteractionRejected(
                "The agent asked a question that needs an answer for "
                + ", ".join(missing)
                + "; pass it with --answer HEADER=LABEL or disable the ask_user capability."
            )
        return AskUserResponse(answers=tuple(answers))
