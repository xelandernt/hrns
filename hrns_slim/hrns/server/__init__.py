"""Server-owned agent sessions with ACP steering and optional AG-UI observation."""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .host import HrnsServer

from .acp import AcpServer, RegisteredAgent, Session, TurnHandle, TurnResult, run_acp_stdio
from .agent import build_agent
from .loro import Author, DocumentCheckpoint, InMemoryDocumentStore, SessionDocument
from .loro.sqlite import SQLiteDocumentStore

__all__ = [
    "AcpServer",
    "Author",
    "DocumentCheckpoint",
    "InMemoryDocumentStore",
    "SQLiteDocumentStore",
    "SessionDocument",
    "HrnsServer",
    "RegisteredAgent",
    "Session",
    "TurnHandle",
    "TurnResult",
    "build_agent",
    "run_acp_stdio",
]


def __getattr__(name: str) -> object:
    if name == "HrnsServer":
        from .host import HrnsServer

        return HrnsServer
    raise AttributeError(name)
