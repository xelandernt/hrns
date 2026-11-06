"""Wire details shared by the ACP adapter and the terminal client.

The capability itself is ``pydantic_ai_harness.ask_user.AskUser``. The only thing
both ends must agree on is a marker on the elicitation schema plus the naming of
the form properties; everything else is derived from the standard schema. This
module is deliberately free of runtime dependencies: the terminal client imports it.
"""

from __future__ import annotations

__all__ = ["ASK_USER_META", "choice_field", "text_field"]

#: ``_meta`` key that marks an elicitation as an ``ask_user_question`` request. The
#: value is not read: a client that knows the key renders the questions as tabs, and
#: one that ignores it still sees a usable, standard form.
ASK_USER_META = "hrns.ask_user"


def choice_field(index: int) -> str:
    """Form property carrying the labels picked for question ``index``."""
    return f"q{index}"


def text_field(index: int) -> str:
    """Form property carrying a typed custom answer for question ``index``."""
    return f"q{index}_text"
