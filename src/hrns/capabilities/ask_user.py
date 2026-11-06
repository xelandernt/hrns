"""The harness `AskUser`, deferred by default so a protocol adapter can answer it."""

from __future__ import annotations

from dataclasses import dataclass

from pydantic_ai.tools import AgentDepsT
from pydantic_ai_harness.ask_user import Answerer
from pydantic_ai_harness.ask_user import AskUser as _AskUser

__all__ = ["AskUser"]


@dataclass(kw_only=True)
class AskUser(_AskUser[AgentDepsT]):
    """The harness `AskUser` with the tool deferred instead of answered in-process.

    Deferred is the only mode that works when the human sits behind a protocol:
    the tool raises ``CallDeferred`` and the host, such as ``PydanticAIACPAgent``,
    puts the questions to the client. Pass ``answerer=`` only for a run with a local
    human, such as a CLI or a test; doing that while mounted over ACP would answer on
    the server and the client would never see the question.
    """

    answerer: Answerer | None = None
