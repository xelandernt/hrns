"""Translate ``ask_user_question`` questions to and from ACP form elicitation.

A question becomes one form property (a titled single-select or multi-select) plus a
companion string for a typed answer. The titles, descriptions, options and
multi-select flag are all standard form schema; the only addition is the
:data:`hrns.shared.ASK_USER_META` marker, which tells a client that understands it to
render tabs instead of a flat form.
"""

from __future__ import annotations

from collections.abc import Sequence

from acp import schema
from pydantic_ai_harness.ask_user import (
    DECLINED,
    AskUserAnswer,
    AskUserRequest,
    AskUserResponse,
    Question,
    ask_user_result,
)

from hrns.shared import ASK_USER_META, choice_field, text_field

__all__ = [
    "ASK_USER_UNAVAILABLE",
    "ask_user_message",
    "ask_user_response",
    "ask_user_tool_result",
    "build_ask_user_schema",
]

#: Tool result when the connected client cannot render an elicitation at all.
ASK_USER_UNAVAILABLE = (
    "The client cannot show questions to the user. Continue without the answer: make a "
    "reasonable choice and say which one you made."
)


def _enum_options(question: Question) -> list[schema.EnumOption]:
    return [
        schema.EnumOption(
            const=option.label,
            title=option.label,
            description=option.description or None,
        )
        for option in question.options
    ]


def build_ask_user_schema(questions: Sequence[Question]) -> schema.ElicitationSchema:
    """Build the form the client renders for one ``ask_user_question`` call."""
    properties: dict[
        str, schema.ElicitationStringPropertySchema | schema.ElicitationMultiSelectPropertySchema
    ] = {}

    for index, question in enumerate(questions):
        choice = choice_field(index)
        text = text_field(index)

        if question.multi_select:
            properties[choice] = schema.ElicitationMultiSelectPropertySchema(
                title=question.header,
                description=question.question,
                items=schema.TitledMultiSelectItems(any_of=_enum_options(question)),
            )
        else:
            properties[choice] = schema.ElicitationStringPropertySchema(
                title=question.header,
                description=question.question,
                one_of=_enum_options(question),
            )
        properties[text] = schema.ElicitationStringPropertySchema(
            title=f"{question.header} (own answer)",
            description="Type your own answer instead of choosing an option.",
        )

    return schema.ElicitationSchema(
        title="Questions from the agent",
        description="Answer each question; the agent is waiting for the result.",
        properties=properties,
        field_meta={ASK_USER_META: {}},
    )


def ask_user_message(questions: Sequence[Question]) -> str:
    """A one-line summary for clients that render the message above the form."""
    if len(questions) == 1:
        return questions[0].question
    return f"The agent has {len(questions)} questions for you."


def _selected(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,) if value else ()
    if isinstance(value, list):
        return tuple(item for item in value if isinstance(item, str))
    return ()


def ask_user_response(
    response: schema.AcceptElicitationResponse
    | schema.DeclineElicitationResponse
    | schema.CancelElicitationResponse
    | schema.OtherElicitationResponse,
    request: AskUserRequest,
) -> AskUserResponse:
    """Turn a client's elicitation response into a harness answer.

    A cancel or decline becomes a cancelled response. An accept that carries nothing
    is treated the same way, so an empty form never reads as an answer.
    """
    if not isinstance(response, schema.AcceptElicitationResponse):
        return AskUserResponse(cancelled=True)

    content = response.content or {}
    answers: list[AskUserAnswer] = []
    for index, question in enumerate(request.questions):
        text = content.get(text_field(index))
        custom = text.strip() if isinstance(text, str) and text.strip() else None
        selected = () if custom else _selected(content.get(choice_field(index)))
        if custom is None and not selected:
            continue
        answers.append(
            AskUserAnswer(header=question.header, selected=selected, custom_answer=custom)
        )
    if len(answers) != len(request.questions):
        return AskUserResponse(cancelled=True)
    return AskUserResponse(answers=tuple(answers))


def ask_user_tool_result(
    request: AskUserRequest,
    response: AskUserResponse,
) -> object:
    """Render the model-facing tool result, falling back to a decline rather than raising."""
    try:
        return ask_user_result(request, response)
    except ValueError:
        return DECLINED
