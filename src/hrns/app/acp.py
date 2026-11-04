"""ACP adapter entry points built on pydantic-ai-harness."""

from __future__ import annotations

import asyncio
import warnings
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, Protocol, TypeAlias, runtime_checkable

import acp
from acp import schema
from pydantic_ai.agent import AbstractAgent
from pydantic_ai.models import KnownModelName, Model
from pydantic_ai.output import OutputDataT
from pydantic_ai.tools import AgentDepsT
from pydantic_ai_harness.experimental import HarnessExperimentalWarning

warnings.filterwarnings("ignore", category=HarnessExperimentalWarning)

from pydantic_ai_harness.experimental.acp import (  # noqa: E402
    InMemorySessionStore as _InMemorySessionStore,
)
from pydantic_ai_harness.experimental.acp import (  # noqa: E402
    PydanticAIACPAgent as _PydanticAIACPAgent,
)
from pydantic_ai_harness.experimental.acp import (  # noqa: E402
    SessionStore,
    StoredSession,
)

__all__ = [
    "ACPModels",
    "InMemorySessionStore",
    "ListableSessionStore",
    "PydanticAIACPAgent",
    "SessionStore",
    "SessionSummary",
    "create_acp_agent",
    "run_acp_stdio",
]

ACPModels: TypeAlias = Sequence[KnownModelName | str] | Literal["all"]
"""Model ids advertised through ACP's ``model`` session config option."""

McpServers: TypeAlias = (
    list[schema.HttpMcpServer | schema.SseMcpServer | schema.AcpMcpServer | schema.McpServerStdio]
    | None
)

_PAGE_SIZE = 100
"""Sessions returned per ``session/list`` page."""


@dataclass(frozen=True, slots=True)
class SessionSummary:
    """The metadata ``session/list`` reports for one stored session."""

    session_id: str
    cwd: str
    title: str | None = None
    updated_at: datetime | None = None


@runtime_checkable
class ListableSessionStore(SessionStore, Protocol):
    """A session store the ACP adapter can enumerate for ``session/list``.

    ``SessionStore`` only needs ``save`` and ``load`` because ``session/load`` addresses one
    session at a time. Listing needs the workspace a session belongs to, which is not part of
    ``StoredSession``: the adapter calls :meth:`remember` when a session is created or reopened,
    and :meth:`list_sessions` returns the matching summaries.
    """

    async def remember(self, session_id: str, *, cwd: str) -> None:
        """Record the workspace for a newly created or reopened session."""
        ...  # pragma: no cover - protocol stub

    async def list_sessions(
        self, *, cwd: str | None = None, cursor: str | None = None
    ) -> tuple[Sequence[SessionSummary], str | None]:
        """Return one page of summaries and the next page's cursor, if any.

        Raise :class:`ValueError` for an unknown or malformed cursor.
        """
        ...  # pragma: no cover - protocol stub


def _title_from_updates(updates: Sequence[object]) -> str | None:
    """Derive a session title from the first user text in its transcript."""
    for update in updates:
        if not isinstance(update, schema.UserMessageChunk):
            continue
        content = update.content
        if isinstance(content, schema.TextContentBlock):
            title = " ".join(content.text.split())
            if title:
                return title[:80]
    return None


def _cursor_offset(cursor: str | None) -> int:
    if cursor is None:
        return 0
    try:
        offset = int(cursor)
    except ValueError as exc:
        raise ValueError("cursor is not a valid session-list offset") from exc
    if offset < 0:
        raise ValueError("cursor is not a valid session-list offset")
    return offset


class InMemorySessionStore(_InMemorySessionStore):
    """Process-local store that also supports ``session/list``.

    Sessions are kept for the lifetime of the process. Implement
    :class:`ListableSessionStore` over durable storage to list sessions across restarts.
    """

    def __init__(self) -> None:
        super().__init__()
        self._summaries: dict[str, SessionSummary] = {}

    async def save(self, session_id: str, session: StoredSession) -> None:
        await super().save(session_id, session)
        previous = self._summaries.get(session_id)
        self._summaries[session_id] = SessionSummary(
            session_id=session_id,
            cwd=previous.cwd if previous is not None else "",
            title=_title_from_updates(session.updates)
            or (previous.title if previous is not None else None),
            updated_at=datetime.now(UTC),
        )

    async def remember(self, session_id: str, *, cwd: str) -> None:
        previous = self._summaries.get(session_id)
        self._summaries[session_id] = SessionSummary(
            session_id=session_id,
            cwd=cwd,
            title=previous.title if previous is not None else None,
            updated_at=previous.updated_at if previous is not None else datetime.now(UTC),
        )

    async def list_sessions(
        self, *, cwd: str | None = None, cursor: str | None = None
    ) -> tuple[Sequence[SessionSummary], str | None]:
        sessions = [
            summary
            for summary in self._summaries.values()
            if summary.cwd and (cwd is None or summary.cwd == cwd)
        ]
        sessions.sort(
            key=lambda summary: (
                -(summary.updated_at.timestamp() if summary.updated_at is not None else 0.0),
                summary.session_id,
            )
        )
        offset = _cursor_offset(cursor)
        page = tuple(sessions[offset : offset + _PAGE_SIZE])
        next_offset = offset + _PAGE_SIZE
        next_cursor = str(next_offset) if next_offset < len(sessions) else None
        return page, next_cursor


class PydanticAIACPAgent(_PydanticAIACPAgent[AgentDepsT, OutputDataT]):
    """The upstream ACP adapter with ``session/list`` for listable stores.

    When the configured store implements :class:`ListableSessionStore`, ``initialize``
    advertises ``session/list`` and the adapter reports each session's workspace, title, and
    last-activity time. With any other store, the upstream behavior is unchanged.
    """

    @property
    def _listable_store(self) -> ListableSessionStore | None:
        store = self._session_store
        return store if isinstance(store, ListableSessionStore) else None

    async def initialize(
        self,
        protocol_version: int,
        client_capabilities: schema.ClientCapabilities | None = None,
        client_info: schema.Implementation | None = None,
        **kwargs: object,
    ) -> schema.InitializeResponse:
        response = await super().initialize(
            protocol_version, client_capabilities, client_info, **kwargs
        )
        agent_capabilities = response.agent_capabilities
        session_capabilities = (
            agent_capabilities.session_capabilities if agent_capabilities is not None else None
        )
        if self._listable_store is not None and session_capabilities is not None:
            session_capabilities.list = schema.SessionListCapabilities()
        return response

    async def new_session(
        self,
        cwd: str,
        additional_directories: list[str] | None = None,
        mcp_servers: McpServers = None,
        **kwargs: object,
    ) -> schema.NewSessionResponse:
        response = await super().new_session(
            cwd,
            additional_directories=additional_directories,
            mcp_servers=mcp_servers,
            **kwargs,
        )
        store = self._listable_store
        if store is not None:
            await store.remember(response.session_id, cwd=cwd)
        return response

    async def load_session(
        self,
        cwd: str,
        session_id: str,
        mcp_servers: McpServers = None,
        additional_directories: list[str] | None = None,
        **kwargs: object,
    ) -> schema.LoadSessionResponse | None:
        response = await super().load_session(
            cwd,
            session_id,
            mcp_servers=mcp_servers,
            additional_directories=additional_directories,
            **kwargs,
        )
        store = self._listable_store
        if response is not None and store is not None:
            await store.remember(session_id, cwd=cwd)
        return response

    async def list_sessions(
        self,
        cwd: str | None = None,
        cursor: str | None = None,
        **kwargs: object,
    ) -> schema.ListSessionsResponse:
        store = self._listable_store
        if store is None:
            raise acp.RequestError.method_not_found("session/list")
        try:
            summaries, next_cursor = await store.list_sessions(cwd=cwd, cursor=cursor)
        except ValueError as exc:
            raise acp.RequestError.invalid_params({"cursor": cursor, "reason": str(exc)}) from exc
        return schema.ListSessionsResponse(
            sessions=[
                schema.SessionInfo(
                    session_id=summary.session_id,
                    cwd=summary.cwd,
                    title=summary.title,
                    updated_at=(
                        summary.updated_at.isoformat() if summary.updated_at is not None else None
                    ),
                )
                for summary in summaries
            ],
            next_cursor=next_cursor,
        )


def create_acp_agent(
    agent: AbstractAgent[None, str],
    *,
    session_store: SessionStore | None = None,
    models: ACPModels | None = None,
    model_resolver: Callable[[str], Model | str] | None = None,
) -> PydanticAIACPAgent[None, str]:
    """Wrap an agent in the upstream ACP adapter.

    ``session_store`` defaults to process-local memory. Pass a durable store if
    sessions should survive a restart; the adapter only needs ``save`` and
    ``load``, plus ``remember`` and ``list_sessions`` to advertise ``session/list``.
    Pass ``models`` to advertise ACP's ``model`` session config option; clients can
    then switch models without reconnecting. ``model_resolver`` maps an advertised
    id to a model when the id is not one Pydantic AI resolves itself.
    """
    return PydanticAIACPAgent(
        agent,
        name=agent.name or "hrns",
        session_store=session_store or InMemorySessionStore(),
        models=models,
        model_resolver=model_resolver,
    )


def run_acp_stdio(
    agent: AbstractAgent[None, str],
    *,
    session_store: SessionStore | None = None,
    models: ACPModels | None = None,
    model_resolver: Callable[[str], Model | str] | None = None,
) -> None:
    """Serve the agent over ACP stdio until the client disconnects."""
    adapter = create_acp_agent(
        agent,
        session_store=session_store,
        models=models,
        model_resolver=model_resolver,
    )
    asyncio.run(acp.run_agent(adapter, use_unstable_protocol=True))
