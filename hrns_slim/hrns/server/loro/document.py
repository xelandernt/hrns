"""Typed domain layer over one Loro session document.

Root schema (server-assigned IDs are strings):

``meta``          LoroMap   schema version, agent/session identity, cwd, timestamps
``lifecycle``     LoroMap   durable status, revision, active contribution, interruption
``settings``      LoroMap   model, setting selections, cost, todos, invoked skills
``inputs``        LoroList<LoroMap>   pending/consumed contributions, text in LoroText
``consumption``   LoroList<LoroMap>   immutable capture records ordered by server seq
``history``       LoroList<LoroMap>   lossless native ModelMessage JSON, append-only
``display``       LoroList<LoroMap>   display-only records, streaming text in LoroText
``interactions``  LoroMap<LoroMap>    questions/approvals keyed by stable request ID

Only the server writes ``meta``, ``lifecycle``, ``consumption``, ``history`` and
``display``. Admitted writers may edit ``inputs``, ``settings`` and answers under
``interactions``; that ownership split is a convention among trusted writers, not
an authorization boundary.
"""

from __future__ import annotations

import base64
import json
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import TypeVar
from uuid import uuid4

from loro import (
    DiffEvent,
    ExportMode,
    ImportStatus,
    LoroDoc,
    LoroList,
    LoroMap,
    LoroText,
    Subscription,
    ValueOrContainer,
)
from pydantic import TypeAdapter
from pydantic_ai.messages import ModelMessage, ModelMessagesTypeAdapter

from hrns.server.capabilities import TodoItem

from .models import (
    SCHEMA_VERSION,
    Author,
    Consumption,
    ConsumptionState,
    Contribution,
    ContributionState,
    DisplayKind,
    DisplayRecord,
    DocumentSettings,
    InteractionKind,
    InteractionRecord,
    InteractionState,
    Lifecycle,
    LifecycleStatus,
    SchemaVersionError,
    SessionMetadata,
)

_JSON_OBJECT = TypeAdapter(dict[str, object])
_JSON_BLOCKS = TypeAdapter(tuple[dict[str, object], ...])
_JSON_STRING_LIST = TypeAdapter(tuple[str, ...])
_JSON_TODOS = TypeAdapter(tuple[TodoItem, ...])
_CONTRIBUTION_STATE = TypeAdapter(ContributionState)
_CONSUMPTION_STATE = TypeAdapter(ConsumptionState)
_INTERACTION_STATE = TypeAdapter(InteractionState)
_INTERACTION_KIND = TypeAdapter(InteractionKind)
_DISPLAY_KIND = TypeAdapter(DisplayKind)
_LIFECYCLE_STATUS = TypeAdapter(LifecycleStatus)

T = TypeVar("T")


def now_ms() -> int:
    return int(time.time() * 1000)


def _literal(adapter: TypeAdapter[T], value: str | None, default: T) -> T:
    if value is None:
        return default
    try:
        return adapter.validate_python(value)
    except ValueError:
        return default


def _scalar(value: ValueOrContainer | None) -> object | None:
    return value.value if ValueOrContainer.is_value(value) else None


def _string(container: LoroMap, key: str) -> str | None:
    value = _scalar(container.get(key))
    return value if isinstance(value, str) else None


def _integer(container: LoroMap, key: str) -> int | None:
    value = _scalar(container.get(key))
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _boolean(container: LoroMap, key: str) -> bool | None:
    value = _scalar(container.get(key))
    return value if isinstance(value, bool) else None


def _json_object(container: LoroMap, key: str) -> dict[str, object]:
    text = _string(container, key)
    if text is None:
        return {}
    try:
        return _JSON_OBJECT.validate_json(text)
    except ValueError:
        return {}


def _child_map(container: LoroMap, key: str) -> LoroMap | None:
    value = container.get(key)
    if value is None or not ValueOrContainer.is_container(value):
        return None
    return value.container if isinstance(value.container, LoroMap) else None


def _child_text(container: LoroMap, key: str) -> LoroText | None:
    value = container.get(key)
    if value is None or not ValueOrContainer.is_container(value):
        return None
    return value.container if isinstance(value.container, LoroText) else None


def _item_map(entries: LoroList, index: int) -> LoroMap | None:
    value = entries.get(index)
    if value is None or not ValueOrContainer.is_container(value):
        return None
    return value.container if isinstance(value.container, LoroMap) else None


def _container_text(container: object) -> LoroText:
    if not isinstance(container, LoroText):
        raise TypeError("expected a text container")
    return container


def _container_map(container: object) -> LoroMap:
    if not isinstance(container, LoroMap):
        raise TypeError("expected a map container")
    return container


@dataclass(frozen=True)
class DisplaySlot:
    """A display record plus the text container its producer appends to."""

    record: DisplayRecord
    text: LoroText


class SessionDocument:
    """One session's authoritative Loro document."""

    def __init__(self, doc: LoroDoc) -> None:
        self._doc = doc
        self._meta = doc.get_map("meta")
        self._lifecycle = doc.get_map("lifecycle")
        self._settings = doc.get_map("settings")
        self._inputs = doc.get_list("inputs")
        self._consumption = doc.get_list("consumption")
        self._history = doc.get_list("history")
        self._display = doc.get_list("display")
        self._interactions = doc.get_map("interactions")
        self._display_cache: dict[str, tuple[LoroMap, LoroText | None]] = {}

    # -- construction ---------------------------------------------------------

    @classmethod
    def create(
        cls,
        *,
        agent_id: str,
        session_id: str,
        cwd: str,
        peer_id: int | None = None,
    ) -> SessionDocument:
        doc = LoroDoc()
        if peer_id is not None:
            doc.peer_id = peer_id
        document = cls(doc)
        stamp = now_ms()
        document._meta.insert("schema_version", SCHEMA_VERSION)
        document._meta.insert("agent_id", agent_id)
        document._meta.insert("session_id", session_id)
        document._meta.insert("cwd", cwd)
        document._meta.insert("created_at", stamp)
        document._meta.insert("updated_at", stamp)
        for name in ("received_seq", "consumption_seq", "display_seq"):
            document._meta.insert(name, 0)
        document._lifecycle.insert("status", "idle")
        document._lifecycle.insert("revision", 0)
        document._lifecycle.insert("resume_required", False)
        document._settings.insert("setting_selections", "{}")
        document._settings.insert("todos", "[]")
        document._settings.insert("skills", "[]")
        document.commit()
        return document

    @classmethod
    def restore(
        cls,
        snapshot: bytes,
        pending: bytes | None = None,
        *,
        peer_id: int | None = None,
    ) -> SessionDocument:
        doc = LoroDoc()
        if peer_id is not None:
            doc.peer_id = peer_id
        doc.import_(snapshot)
        if pending:
            doc.import_(pending)
        document = cls(doc)
        document.validate()
        return document

    def validate(self) -> SessionMetadata:
        version = _integer(self._meta, "schema_version")
        if version != SCHEMA_VERSION:
            raise SchemaVersionError(
                f"document schema version {version!r} is not supported (expected {SCHEMA_VERSION})"
            )
        return self.session_metadata()

    # -- identity and lifecycle ----------------------------------------------

    def session_metadata(self) -> SessionMetadata:
        return SessionMetadata(
            schema_version=_integer(self._meta, "schema_version") or 0,
            agent_id=_string(self._meta, "agent_id") or "",
            session_id=_string(self._meta, "session_id") or "",
            cwd=_string(self._meta, "cwd") or "",
            created_at=_integer(self._meta, "created_at") or 0,
            updated_at=_integer(self._meta, "updated_at") or 0,
        )

    def touch(self) -> None:
        self._meta.insert("updated_at", now_ms())

    def lifecycle(self) -> Lifecycle:
        return Lifecycle(
            status=_literal(_LIFECYCLE_STATUS, _string(self._lifecycle, "status"), "idle"),
            revision=_integer(self._lifecycle, "revision") or 0,
            active_contribution_id=_string(self._lifecycle, "active_contribution_id"),
            last_outcome=_string(self._lifecycle, "last_outcome"),
            resume_required=_boolean(self._lifecycle, "resume_required") or False,
        )

    def set_lifecycle_status(self, status: LifecycleStatus) -> None:
        self._lifecycle.insert("status", status)

    def set_active_contribution(self, contribution_id: str | None) -> None:
        if contribution_id is None:
            self._lifecycle.delete("active_contribution_id")
        else:
            self._lifecycle.insert("active_contribution_id", contribution_id)

    def set_last_outcome(self, outcome: str | None) -> None:
        if outcome is None:
            self._lifecycle.delete("last_outcome")
        else:
            self._lifecycle.insert("last_outcome", outcome)

    def set_resume_required(self, required: bool) -> None:
        self._lifecycle.insert("resume_required", required)

    def commit_lifecycle(self) -> Lifecycle:
        self._lifecycle.insert("revision", (_integer(self._lifecycle, "revision") or 0) + 1)
        self.touch()
        self.commit()
        return self.lifecycle()

    # -- contributions --------------------------------------------------------

    def _contribution_map(self, contribution_id: str) -> LoroMap | None:
        for index in range(len(self._inputs)):
            entry = _item_map(self._inputs, index)
            if entry is not None and _string(entry, "id") == contribution_id:
                return entry
        return None

    def _parse_contribution(self, entry: LoroMap) -> Contribution:
        author_json = _string(entry, "author")
        author = (
            Author.model_validate_json(author_json)
            if author_json is not None
            else Author(id="unknown")
        )
        try:
            blocks = _JSON_BLOCKS.validate_json(_string(entry, "blocks") or "[]")
        except ValueError:
            blocks = ()
        try:
            skills = _JSON_STRING_LIST.validate_json(_string(entry, "skills") or "[]")
        except ValueError:
            skills = ()
        body = _child_text(entry, "text")
        return Contribution(
            id=_string(entry, "id") or "",
            author=author,
            state=_literal(_CONTRIBUTION_STATE, _string(entry, "state"), "draft"),
            text=body.to_string() if body is not None else "",
            blocks=blocks,
            skills=skills,
            origin=_string(entry, "origin") or "python",
            created_at=_integer(entry, "created_at") or 0,
            received_seq=_integer(entry, "received_seq"),
            consumed_seq=_integer(entry, "consumed_seq"),
            capture_version=_string(entry, "capture_version"),
        )

    def contributions(self) -> list[Contribution]:
        return [
            self._parse_contribution(entry)
            for index in range(len(self._inputs))
            if (entry := _item_map(self._inputs, index)) is not None
        ]

    def contribution(self, contribution_id: str) -> Contribution | None:
        entry = self._contribution_map(contribution_id)
        return self._parse_contribution(entry) if entry is not None else None

    def create_contribution(
        self,
        *,
        author: Author,
        text: str = "",
        blocks: Sequence[dict[str, object]] = (),
        skills: Sequence[str] = (),
        origin: str = "python",
        state: ContributionState = "ready",
        contribution_id: str | None = None,
    ) -> Contribution:
        entry = _container_map(self._inputs.push_container(LoroMap()))
        identifier = contribution_id or uuid4().hex
        entry.insert("id", identifier)
        entry.insert("author", author.model_dump_json())
        entry.insert("state", state)
        entry.insert("origin", origin)
        entry.insert("created_at", now_ms())
        entry.insert("blocks", _JSON_BLOCKS.dump_json(tuple(blocks)).decode("utf-8"))
        entry.insert("skills", _JSON_STRING_LIST.dump_json(tuple(skills)).decode("utf-8"))
        body = _container_text(entry.insert_container("text", LoroText()))
        if text:
            body.insert(0, text)
        self.touch()
        self.commit()
        return self._parse_contribution(entry)

    def contribution_text(self, contribution_id: str) -> LoroText | None:
        entry = self._contribution_map(contribution_id)
        return _child_text(entry, "text") if entry is not None else None

    def set_contribution_text(self, contribution_id: str, text: str) -> bool:
        entry = self._contribution_map(contribution_id)
        if entry is None or _string(entry, "state") not in ("draft", "ready"):
            return False
        body = _child_text(entry, "text")
        if body is None:
            return False
        body.splice(0, body.len_unicode, text)
        self.touch()
        self.commit()
        return True

    def mark_ready(self, contribution_id: str) -> bool:
        entry = self._contribution_map(contribution_id)
        if entry is None or _string(entry, "state") != "draft":
            return False
        entry.insert("state", "ready")
        entry.insert("ready_at", now_ms())
        self._stamp_received(entry)
        self.touch()
        self.commit()
        return True

    def cancel_contribution(self, contribution_id: str) -> bool:
        entry = self._contribution_map(contribution_id)
        if entry is None or _string(entry, "state") not in ("draft", "ready"):
            return False
        entry.insert("state", "cancelled")
        self.touch()
        self.commit()
        return True

    def discard_inputs(self, states: Iterable[str]) -> list[str]:
        """Cancel every contribution in one of the given states and return their ids."""
        wanted = set(states)
        cancelled: list[str] = []
        for index in range(len(self._inputs)):
            entry = _item_map(self._inputs, index)
            if entry is None or _string(entry, "state") not in wanted:
                continue
            identifier = _string(entry, "id")
            if identifier is None:
                continue
            entry.insert("state", "cancelled")
            cancelled.append(identifier)
        if cancelled:
            self.touch()
            self.commit()
        return cancelled

    def _next_counter(self, name: str) -> int:
        value = (_integer(self._meta, name) or 0) + 1
        self._meta.insert(name, value)
        return value

    def _stamp_received(self, entry: LoroMap) -> int:
        existing = _integer(entry, "received_seq")
        if existing is not None:
            return existing
        value = self._next_counter("received_seq")
        entry.insert("received_seq", value)
        return value

    def stamp_ready(self) -> int:
        """Give every ready contribution a server arrival order, in list order."""
        stamped = 0
        for index in range(len(self._inputs)):
            entry = _item_map(self._inputs, index)
            if entry is None or _string(entry, "state") != "ready":
                continue
            if _integer(entry, "received_seq") is None:
                self._stamp_received(entry)
                stamped += 1
        if stamped:
            self.commit()
        return stamped

    def eligible(self) -> list[Contribution]:
        """Ready contributions in server arrival order, without freezing them."""
        self.stamp_ready()
        ready = [c for c in self.contributions() if c.state == "ready"]
        return sorted(ready, key=lambda c: (c.received_seq or 0, c.id))

    def capture_ready(self) -> list[Contribution]:
        """Freeze every integrated ready contribution at one boundary.

        Assigns monotonic consumption sequence numbers, writes immutable
        consumption records, and marks the contributions captured. Content is
        read once, so later text edits cannot reach this capture.
        """
        ready = self.eligible()
        if not ready:
            return []
        stamp = now_ms()
        version = self.version_token()
        captured: list[Contribution] = []
        for contribution in ready:
            entry = self._contribution_map(contribution.id)
            if entry is None:
                continue
            seq = self._next_counter("consumption_seq")
            entry.insert("state", "captured")
            entry.insert("consumed_seq", seq)
            entry.insert("capture_version", version)
            record = _container_map(self._consumption.push_container(LoroMap()))
            record.insert("seq", seq)
            record.insert("contribution_id", contribution.id)
            record.insert("author", contribution.author.model_dump_json())
            record.insert("captured_at", stamp)
            record.insert("version", version)
            record.insert("content", contribution.text)
            record.insert("state", "captured")
            captured.append(
                contribution.model_copy(
                    update={
                        "state": "captured",
                        "consumed_seq": seq,
                        "capture_version": version,
                    }
                )
            )
        self.touch()
        self.commit()
        return captured

    def consumption(self) -> list[Consumption]:
        records: list[Consumption] = []
        for index in range(len(self._consumption)):
            entry = _item_map(self._consumption, index)
            if entry is None:
                continue
            author_json = _string(entry, "author")
            author = (
                Author.model_validate_json(author_json)
                if author_json is not None
                else Author(id="unknown")
            )
            records.append(
                Consumption(
                    seq=_integer(entry, "seq") or 0,
                    contribution_id=_string(entry, "contribution_id") or "",
                    author=author,
                    captured_at=_integer(entry, "captured_at") or 0,
                    version=_string(entry, "version") or "",
                    content=_string(entry, "content") or "",
                    state=_literal(_CONSUMPTION_STATE, _string(entry, "state"), "captured"),
                    outcome=_string(entry, "outcome"),
                )
            )
        return sorted(records, key=lambda record: record.seq)

    def _consumption_map(self, seq: int) -> LoroMap | None:
        for index in range(len(self._consumption)):
            entry = _item_map(self._consumption, index)
            if entry is not None and _integer(entry, "seq") == seq:
                return entry
        return None

    def mark_consumption(
        self, seq: int, state: ConsumptionState, outcome: str | None = None
    ) -> bool:
        entry = self._consumption_map(seq)
        if entry is None:
            return False
        entry.insert("state", state)
        if outcome is not None:
            entry.insert("outcome", outcome)
        contribution_id = _string(entry, "contribution_id")
        if contribution_id is not None:
            source = self._contribution_map(contribution_id)
            if source is not None and _string(source, "state") == "captured":
                source.insert("state", "delivered" if state == "delivered" else "failed")
        self.touch()
        self.commit()
        return True

    # -- native history -------------------------------------------------------

    def history_messages(self) -> list[ModelMessage]:
        messages: list[ModelMessage] = []
        for index in range(len(self._history)):
            entry = _item_map(self._history, index)
            if entry is None:
                continue
            payload = _string(entry, "payload")
            if payload is not None:
                messages.extend(ModelMessagesTypeAdapter.validate_json(payload))
        return messages

    def append_history(self, messages: Sequence[ModelMessage]) -> None:
        if not messages:
            return
        for message in messages:
            entry = _container_map(self._history.push_container(LoroMap()))
            entry.insert("id", uuid4().hex)
            entry.insert("payload", ModelMessagesTypeAdapter.dump_json([message]).decode("utf-8"))
        self.touch()
        self.commit()

    @property
    def history_length(self) -> int:
        return len(self._history)

    def replace_history(self, messages: Sequence[ModelMessage]) -> None:
        """Rewrite history when a correction cannot be expressed as an append."""
        self._history.delete(0, len(self._history))
        self.append_history(messages)

    def title(self) -> str | None:
        """Derive the session title from the first user text in the document."""
        for record in self.display_records():
            if record.kind != "user":
                continue
            title = " ".join(record.text.split())
            if title:
                return title[:80]
        for contribution in self.contributions():
            title = " ".join(contribution.text.split())
            if title:
                return title[:80]
        return None

    # -- display --------------------------------------------------------------

    def start_display(
        self,
        kind: DisplayKind,
        payload: dict[str, object] | None = None,
        *,
        status: str = "",
        text: str = "",
    ) -> DisplaySlot:
        identifier = uuid4().hex
        seq = self._next_counter("display_seq")
        entry = _container_map(self._display.push_container(LoroMap()))
        entry.insert("id", identifier)
        entry.insert("kind", kind)
        entry.insert("seq", seq)
        entry.insert("status", status)
        entry.insert("payload", _JSON_OBJECT.dump_json(payload or {}).decode("utf-8"))
        body = _container_text(entry.insert_container("text", LoroText()))
        if text:
            body.insert(0, text)
        self._display_cache[identifier] = (entry, body)
        self.touch()
        self.commit()
        return DisplaySlot(record=self._display_record(entry), text=body)

    def _display_map(self, display_id: str) -> tuple[LoroMap, LoroText | None] | None:
        cached = self._display_cache.get(display_id)
        if cached is not None:
            return cached
        for index in range(len(self._display)):
            entry = _item_map(self._display, index)
            if entry is not None and _string(entry, "id") == display_id:
                found = (entry, _child_text(entry, "text"))
                self._display_cache[display_id] = found
                return found
        return None

    def _display_record(self, entry: LoroMap) -> DisplayRecord:
        body = _child_text(entry, "text")
        return DisplayRecord(
            id=_string(entry, "id") or "",
            kind=_literal(_DISPLAY_KIND, _string(entry, "kind"), "assistant"),
            seq=_integer(entry, "seq") or 0,
            status=_string(entry, "status") or "",
            text=body.to_string() if body is not None else "",
            payload=_json_object(entry, "payload"),
        )

    def display_text(self, display_id: str) -> LoroText | None:
        found = self._display_map(display_id)
        return found[1] if found is not None else None

    def display_record(self, display_id: str) -> DisplayRecord | None:
        found = self._display_map(display_id)
        return self._display_record(found[0]) if found is not None else None

    def set_display_status(self, display_id: str, status: str) -> DisplayRecord | None:
        found = self._display_map(display_id)
        if found is None:
            return None
        found[0].insert("status", status)
        self.touch()
        self.commit()
        return self._display_record(found[0])

    def set_display_payload(
        self, display_id: str, payload: dict[str, object]
    ) -> DisplayRecord | None:
        found = self._display_map(display_id)
        if found is None:
            return None
        found[0].insert("payload", _JSON_OBJECT.dump_json(payload).decode("utf-8"))
        self.touch()
        self.commit()
        return self._display_record(found[0])

    def display_records(self) -> list[DisplayRecord]:
        return [
            self._display_record(entry)
            for index in range(len(self._display))
            if (entry := _item_map(self._display, index)) is not None
        ]

    # -- settings -------------------------------------------------------------

    def settings(self) -> DocumentSettings:
        try:
            selections = _JSON_OBJECT.validate_json(
                _string(self._settings, "setting_selections") or "{}"
            )
        except ValueError:
            selections = {}
        try:
            todos = _JSON_TODOS.validate_json(_string(self._settings, "todos") or "[]")
        except ValueError:
            todos = ()
        try:
            skills = _JSON_STRING_LIST.validate_json(_string(self._settings, "skills") or "[]")
        except ValueError:
            skills = ()
        return DocumentSettings(
            model=_string(self._settings, "model"),
            setting_selections={
                str(key): str(value) for key, value in selections.items() if isinstance(value, str)
            },
            cost=_string(self._settings, "cost"),
            todos=todos,
            skills=skills,
        )

    def _write_setting(self, key: str, value: str | None) -> None:
        if value is None:
            if key in self._settings:
                self._settings.delete(key)
        else:
            self._settings.insert(key, value)
        self.touch()
        self.commit()

    def set_model(self, model: str | None) -> None:
        self._write_setting("model", model)

    def set_setting_selections(self, selections: dict[str, str]) -> None:
        self._write_setting("setting_selections", json.dumps(selections, sort_keys=True))

    def set_cost(self, cost: str | None) -> None:
        self._write_setting("cost", cost)

    def set_todos(self, items: Sequence[TodoItem]) -> None:
        self._write_setting("todos", _JSON_TODOS.dump_json(tuple(items)).decode("utf-8"))

    def set_skills(self, skills: Sequence[str]) -> None:
        self._write_setting("skills", _JSON_STRING_LIST.dump_json(tuple(skills)).decode("utf-8"))

    # -- interactions ---------------------------------------------------------

    def _interaction_map(self, interaction_id: str) -> LoroMap | None:
        return _child_map(self._interactions, interaction_id)

    def _parse_interaction(self, entry: LoroMap) -> InteractionRecord:
        response_json = _string(entry, "response")
        try:
            response = _JSON_OBJECT.validate_json(response_json) if response_json else None
        except ValueError:
            response = None
        return InteractionRecord(
            id=_string(entry, "id") or "",
            kind=_literal(_INTERACTION_KIND, _string(entry, "kind"), "approval"),
            state=_literal(_INTERACTION_STATE, _string(entry, "state"), "pending"),
            request=_json_object(entry, "request"),
            created_at=_integer(entry, "created_at") or 0,
            response=response,
            resolved_by=_string(entry, "resolved_by"),
            detail=_string(entry, "detail"),
        )

    def create_interaction(
        self,
        *,
        kind: InteractionKind,
        request: dict[str, object],
        interaction_id: str | None = None,
    ) -> InteractionRecord:
        identifier = interaction_id or uuid4().hex
        entry = _container_map(self._interactions.insert_container(identifier, LoroMap()))
        entry.insert("id", identifier)
        entry.insert("kind", kind)
        entry.insert("state", "pending")
        entry.insert("request", _JSON_OBJECT.dump_json(request).decode("utf-8"))
        entry.insert("created_at", now_ms())
        self.touch()
        self.commit()
        return self._parse_interaction(entry)

    def interaction(self, interaction_id: str) -> InteractionRecord | None:
        entry = self._interaction_map(interaction_id)
        return self._parse_interaction(entry) if entry is not None else None

    def interactions(self) -> list[InteractionRecord]:
        records: list[InteractionRecord] = []
        for key in self._interactions.keys():
            entry = self._interaction_map(key)
            if entry is not None:
                records.append(self._parse_interaction(entry))
        return sorted(records, key=lambda record: record.created_at)

    def resolve_interaction(
        self,
        interaction_id: str,
        response: dict[str, object],
        *,
        resolved_by: str,
    ) -> bool:
        """Record the first accepted answer; later answers change nothing."""
        entry = self._interaction_map(interaction_id)
        if entry is None or _string(entry, "state") != "pending":
            return False
        entry.insert("state", "resolved")
        entry.insert("response", _JSON_OBJECT.dump_json(response).decode("utf-8"))
        entry.insert("resolved_by", resolved_by)
        self.touch()
        self.commit()
        return True

    def cancel_interaction(self, interaction_id: str, *, state: str, detail: str) -> bool:
        entry = self._interaction_map(interaction_id)
        if entry is None or _string(entry, "state") != "pending":
            return False
        entry.insert("state", state)
        entry.insert("detail", detail)
        self.touch()
        self.commit()
        return True

    def cancel_pending_interactions(self, *, state: str, detail: str) -> list[str]:
        cancelled: list[str] = []
        for key in self._interactions.keys():
            entry = self._interaction_map(key)
            if entry is None or _string(entry, "state") != "pending":
                continue
            entry.insert("state", state)
            entry.insert("detail", detail)
            identifier = _string(entry, "id")
            if identifier is not None:
                cancelled.append(identifier)
        if cancelled:
            self.touch()
            self.commit()
        return cancelled

    # -- persistence and synchronization -------------------------------------

    def snapshot(self) -> bytes:
        return self._doc.export(ExportMode.Snapshot())

    def pending_updates(self) -> bytes | None:
        return self._doc.export_pending_updates() or None

    def version(self) -> bytes:
        return self._doc.oplog_vv.encode()

    def version_token(self) -> str:
        return base64.b64encode(self._doc.state_frontiers.encode()).decode("ascii")

    def frontiers(self) -> bytes:
        return self._doc.state_frontiers.encode()

    def import_update(self, data: bytes, *, origin: str) -> ImportStatus:
        status = self._doc.import_with(data, origin=origin)
        self._display_cache.clear()
        return status

    def import_snapshot(self, data: bytes, *, origin: str) -> ImportStatus:
        return self.import_update(data, origin=origin)

    def commit(self, *, origin: str = "server") -> None:
        self._doc.set_next_commit_origin(origin)
        self._doc.commit()

    def subscribe(self, callback: Callable[[DiffEvent], None]) -> Subscription:
        return self._doc.subscribe_root(callback)

    def subscribe_local_updates(self, callback: Callable[[bytes], bool]) -> Subscription:
        return self._doc.subscribe_local_update(callback)

    @property
    def doc(self) -> LoroDoc:
        return self._doc

    @property
    def schema_version(self) -> int:
        return _integer(self._meta, "schema_version") or 0
