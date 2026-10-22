"""Agent-initiated questions carried to the connected ACP client.

The runtime owns the tool schema and validation through the upstream ``AskUser``
capability. This module only moves one validated request over ACP and turns the
client's reply back into the upstream response type.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

__all__ = ["ASK_USER_METHOD", "QuestionChannel"]

#: Agent-to-client extension method; ``AgentSideConnection.ext_method`` adds the underscore.
ASK_USER_METHOD = "harness/ask_user"


def request_payload(request: Any) -> dict[str, Any]:
    """Serialize an upstream ``AskUserRequest`` for the client."""
    return {
        "requestId": request.id,
        "questions": [
            {
                "header": question.header,
                "question": question.question,
                "multiSelect": question.multi_select,
                "options": [
                    {
                        "label": option.label,
                        "description": option.description or "",
                    }
                    for option in question.options
                ],
            }
            for question in request.questions
        ],
    }


def response_from_payload(payload: Any) -> Any:
    """Parse a client reply into an upstream ``AskUserResponse``."""
    from pydantic_ai_harness.ask_user import AskUserAnswer, AskUserResponse

    if not isinstance(payload, Mapping):
        raise ValueError("ask-user reply must be an object")
    if payload.get("cancelled") is True:
        return AskUserResponse(cancelled=True)
    raw_answers = payload.get("answers")
    if not isinstance(raw_answers, list):
        raise ValueError("ask-user reply must carry an answers list")
    answers: list[AskUserAnswer] = []
    for item in raw_answers:
        if not isinstance(item, Mapping):
            raise ValueError("each ask-user answer must be an object")
        header = item.get("header")
        if not isinstance(header, str) or not header:
            raise ValueError("each ask-user answer needs a header")
        raw_selected = item.get("selected") or []
        if not isinstance(raw_selected, list) or not all(
            isinstance(label, str) for label in raw_selected
        ):
            raise ValueError(f"answer for {header!r} has invalid selected labels")
        custom = item.get("customAnswer")
        if custom is not None and not isinstance(custom, str):
            raise ValueError(f"answer for {header!r} has an invalid custom answer")
        answers.append(
            AskUserAnswer(
                header=header,
                selected=tuple(raw_selected),
                custom_answer=custom,
            )
        )
    return AskUserResponse(answers=tuple(answers))


class QuestionChannel:
    """The ``user_answers`` resource: an answerer that asks the connected client."""

    def __init__(self) -> None:
        self._connection: Any | None = None

    def bind(self, connection: Any) -> None:
        """Point the channel at the live ACP connection (called from ``on_connect``)."""
        self._connection = connection

    async def __call__(self, request: Any) -> Any:
        from pydantic_ai_harness.ask_user import AskUserResponse

        connection = self._connection
        if connection is None:
            return AskUserResponse(cancelled=True)
        payload = await connection.ext_method(ASK_USER_METHOD, request_payload(request))
        return response_from_payload(payload)
