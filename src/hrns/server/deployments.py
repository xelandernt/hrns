"""Immutable deployment revisions for agents hosted by an embedded server.

Revisions are immutable: hiding, unregistering or re-registering a deployment
never mutates a revision that a running session already holds.  Adding a
revision while the server is mounted is supported, so deployment changes do
not require a restart.  Hiding or unregistering an agent never deletes retained
revisions, which keeps hidden historical revisions loadable for sessions that
pin them.  New sessions resolve the latest visible revision, while pinned
sessions resolve their exact retained revision.  A missing historical revision
is a distinct error condition from an unknown agent: the former names an exact
version that is no longer registered, while the latter has no registered
revision at all.

The registry uses a plain :class:`threading.Lock` so it is safe to call from
synchronous registration code as well as from event-loop callbacks; reads never
need to be awaited.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

from .models import RegisteredAgent

__all__ = [
    "DeploymentConflict",
    "DeploymentRegistry",
    "DeploymentRevision",
    "MissingDeployment",
]


def _now_ms() -> int:
    """Return the current epoch time in milliseconds."""

    return time.time_ns() // 1_000_000


def _require_identifier(value: str, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")


class DeploymentConflict(ValueError):
    """Raised when a registry mutation conflicts with an existing revision."""


class MissingDeployment(LookupError):
    """Raised when a requested revision is not registered.

    ``version`` is ``None`` when the lookup asked for the latest revision of an
    unknown agent, and an exact version string when a specific historical
    revision is missing.  Callers can therefore distinguish an unknown agent
    from a vanished pinned revision.
    """

    def __init__(self, agent_id: str, version: str | None = None) -> None:
        self.agent_id = agent_id
        self.version = version
        if version is None:
            message = f"no visible deployment is registered for agent {agent_id!r}"
        else:
            message = f"deployment {agent_id!r} version {version!r} is not registered"
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class DeploymentRevision:
    """One immutable registration of an agent at a specific definition version."""

    agent: RegisteredAgent
    visible: bool
    registered_at: int
    retained: bool = True

    @property
    def agent_id(self) -> str:
        return self.agent.id

    @property
    def version(self) -> str:
        version = self.agent.definition.definition_version
        if version is None:
            raise ValueError(f"agent {self.agent.id!r} has no definition version")
        return version


class DeploymentRegistry:
    """Thread-safe registry of immutable deployment revisions.

    Revisions are keyed by ``(agent_id, version)`` and additionally kept in a
    per-agent insertion-order list.  Older revisions stay available so that
    sessions pinned to them keep loading, while :meth:`latest` and
    :meth:`visible` expose only the newest visible revision of each agent.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._revisions: dict[tuple[str, str], DeploymentRevision] = {}
        self._order: dict[str, list[DeploymentRevision]] = {}

    def register(
        self,
        agent: RegisteredAgent,
        *,
        visible: bool = True,
        replace: bool = False,
        retained: bool = True,
        registered_at: int | None = None,
    ) -> DeploymentRevision:
        """Add a revision, or update an existing one when ``replace`` is true.

        New revisions are appended to the agent's insertion order; older
        revisions are never removed by registering a newer version.  Replacing
        an existing ``(agent_id, version)`` keeps its original ``registered_at``
        and order, but refreshes the agent metadata and visibility.  Retained
        revisions stay retained across a replacement.
        """

        _require_identifier(agent.id, "agent.id")
        version = agent.definition.definition_version
        if version is None:
            raise ValueError("agent.definition.definition_version must be a non-empty string")
        _require_identifier(version, "agent.definition.definition_version")
        key = (agent.id, version)

        with self._lock:
            existing = self._revisions.get(key)
            if existing is not None:
                if not replace:
                    raise DeploymentConflict(
                        f"deployment {agent.id!r} version {version!r} is already registered"
                    )
                updated = DeploymentRevision(
                    agent=agent,
                    visible=visible,
                    registered_at=existing.registered_at,
                    retained=existing.retained or retained,
                )
                return self._store_locked(updated)

            revision = DeploymentRevision(
                agent=agent,
                visible=visible,
                registered_at=registered_at if registered_at is not None else _now_ms(),
                retained=retained,
            )
            self._revisions[key] = revision
            self._order.setdefault(agent.id, []).append(revision)
            return revision

    def get(self, agent_id: str, version: str) -> DeploymentRevision | None:
        """Return an exact revision, including hidden ones."""

        with self._lock:
            return self._revisions.get((agent_id, version))

    def latest(
        self,
        agent_id: str,
        *,
        include_hidden: bool = False,
    ) -> DeploymentRevision | None:
        """Return the most recently registered revision matching visibility."""

        with self._lock:
            return self._latest_locked(agent_id, include_hidden=include_hidden)

    def resolve(
        self,
        agent_id: str,
        *,
        version: str | None = None,
        include_hidden: bool = True,
    ) -> DeploymentRevision | None:
        """Resolve an exact version when given, otherwise the latest revision."""

        with self._lock:
            if version is not None:
                revision = self._revisions.get((agent_id, version))
                if revision is None or (not include_hidden and not revision.visible):
                    return None
                return revision
            return self._latest_locked(agent_id, include_hidden=include_hidden)

    def require(
        self,
        agent_id: str,
        *,
        version: str | None = None,
        include_hidden: bool = True,
    ) -> DeploymentRevision:
        """Like :meth:`resolve`, but raise :class:`MissingDeployment` on absence."""

        revision = self.resolve(agent_id, version=version, include_hidden=include_hidden)
        if revision is None:
            raise MissingDeployment(agent_id, version)
        return revision

    def agent_ids(self) -> tuple[str, ...]:
        """Every agent ID with at least one registered revision, in registration order."""
        with self._lock:
            return tuple(self._order)

    def revisions(self, agent_id: str) -> tuple[DeploymentRevision, ...]:
        """Return every retained revision of an agent, oldest first."""

        with self._lock:
            ordered = self._order.get(agent_id)
            return tuple(ordered) if ordered is not None else ()

    def visible(
        self,
        agent_ids: frozenset[str] | None = None,
    ) -> tuple[RegisteredAgent, ...]:
        """Return the latest visible agent per ID, in first-registration order."""

        with self._lock:
            agents: list[RegisteredAgent] = []
            for agent_id, ordered in self._order.items():
                if agent_ids is not None and agent_id not in agent_ids:
                    continue
                for revision in reversed(ordered):
                    if revision.visible:
                        agents.append(revision.agent)
                        break
            return tuple(agents)

    def set_visible(
        self,
        agent_id: str,
        version: str,
        visible: bool,
    ) -> DeploymentRevision:
        """Hide or show one revision without deleting it."""

        key = (agent_id, version)
        with self._lock:
            revision = self._revisions.get(key)
            if revision is None:
                raise MissingDeployment(agent_id, version)
            if revision.visible == visible:
                return revision
            updated = DeploymentRevision(
                agent=revision.agent,
                visible=visible,
                registered_at=revision.registered_at,
                retained=revision.retained,
            )
            return self._store_locked(updated)

    def unregister(self, agent_id: str) -> int:
        """Hide every revision of an agent, returning how many were hidden.

        Revisions stay retained and loadable; nothing is deleted.
        """

        with self._lock:
            ordered = self._order.get(agent_id)
            if not ordered:
                return 0
            hidden = [
                DeploymentRevision(
                    agent=revision.agent,
                    visible=False,
                    registered_at=revision.registered_at,
                    retained=revision.retained,
                )
                for revision in ordered
            ]
            self._order[agent_id] = hidden
            for revision in hidden:
                self._revisions[(agent_id, revision.version)] = revision
            return len(hidden)

    def remove(self, agent_id: str, version: str) -> bool:
        """Delete a non-retained revision, returning whether it existed.

        Retained revisions back pinned sessions, so attempting to remove one
        raises :class:`DeploymentConflict` rather than breaking those sessions.
        """

        key = (agent_id, version)
        with self._lock:
            revision = self._revisions.get(key)
            if revision is None:
                return False
            if revision.retained:
                raise DeploymentConflict(
                    f"deployment {agent_id!r} version {version!r} is retained and cannot be removed"
                )
            del self._revisions[key]
            ordered = self._order.get(agent_id)
            if ordered is not None:
                remaining = [item for item in ordered if item.version != version]
                if remaining:
                    self._order[agent_id] = remaining
                else:
                    del self._order[agent_id]
            return True

    def mark_referenced(self, agent_id: str, version: str) -> DeploymentRevision:
        """Retain a revision so a pinned session protects it from removal."""

        key = (agent_id, version)
        with self._lock:
            revision = self._revisions.get(key)
            if revision is None:
                raise MissingDeployment(agent_id, version)
            if revision.retained:
                return revision
            updated = DeploymentRevision(
                agent=revision.agent,
                visible=revision.visible,
                registered_at=revision.registered_at,
                retained=True,
            )
            return self._store_locked(updated)

    def missing(self, agent_id: str, version: str) -> bool:
        """Return whether an exact revision is absent from the registry."""

        with self._lock:
            return (agent_id, version) not in self._revisions

    def _latest_locked(
        self,
        agent_id: str,
        *,
        include_hidden: bool,
    ) -> DeploymentRevision | None:
        ordered = self._order.get(agent_id)
        if not ordered:
            return None
        for revision in reversed(ordered):
            if include_hidden or revision.visible:
                return revision
        return None

    def _store_locked(self, revision: DeploymentRevision) -> DeploymentRevision:
        self._revisions[(revision.agent_id, revision.version)] = revision
        ordered = self._order[revision.agent_id]
        for index, item in enumerate(ordered):
            if item.version == revision.version:
                ordered[index] = revision
                break
        return revision
