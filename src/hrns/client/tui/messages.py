"""Textual messages exchanged between widgets and screens."""

from __future__ import annotations

from textual.message import Message

from ..interactions import AskUserResponse


class Submitted(Message):
    """The composer wants to submit its text."""

    def __init__(self, text: str) -> None:
        super().__init__()
        self.text = text


class CommandSuggestionKey(Message):
    """A navigation key consumed by the inline command picker."""

    def __init__(self, key: str) -> None:
        super().__init__()
        self.key = key


class QueueAction(Message):
    def __init__(self, action: str, index: int = 0) -> None:
        super().__init__()
        self.action = action
        self.index = index


class AttachmentRemoved(Message):
    def __init__(self, index: int) -> None:
        super().__init__()
        self.index = index


class OutputPending(Message):
    def __init__(self, pending: bool) -> None:
        super().__init__()
        self.pending = pending


class ApprovalDecided(Message):
    """The local user answered a pending tool approval."""

    def __init__(self, option_id: str | None) -> None:
        super().__init__()
        self.option_id = option_id


class AskUserAnswered(Message):
    """The local user finished or declined an agent question."""

    def __init__(self, response: AskUserResponse) -> None:
        super().__init__()
        self.response = response
