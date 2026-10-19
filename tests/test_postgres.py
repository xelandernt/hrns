"""Tests for the optional PostgreSQL session store.

The construction/import checks always run.  Real database integration is opt-in and
requires ``HARNESS_TEST_POSTGRES_DSN`` plus the ``postgres`` extra (``psycopg``)::

    HARNESS_TEST_POSTGRES_DSN=postgresql://user:pass@host/db \\
        uv run --extra postgres pytest -q tests/test_postgres.py

The integration tests use unique session ids and delete the sessions they create;
they do not drop shared tables.  Use a disposable database in CI.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest
import pytest_asyncio
from harness_protocol import HarnessError
from harness_runtime import postgres
from harness_runtime.postgres import PostgresSessionStore, is_postgres_dsn
from harness_runtime.store import SessionRecord, SessionStore

ROOT = Path(__file__).resolve().parents[1]
POSTGRES_DSN = os.environ.get("HARNESS_TEST_POSTGRES_DSN")

requires_postgres = pytest.mark.skipif(
    not POSTGRES_DSN,
    reason=(
        "set HARNESS_TEST_POSTGRES_DSN to run PostgreSQL integration tests "
        "(install the postgres extra with uv sync --all-packages --extra postgres)"
    ),
)


# --- driver/import/construction checks (no server required) --------------


def test_optional_import_reports_actionable_error_without_driver() -> None:
    """The module must import without psycopg and fail only on construction."""

    code = r"""
import sys
sys.modules["psycopg"] = None
from harness_runtime import postgres
assert postgres.PSYCOPG_AVAILABLE is False
try:
    postgres.PostgresSessionStore("postgresql://localhost/example")
except ImportError as exc:
    assert "harness-runtime[postgres]" in str(exc), str(exc)
else:
    raise SystemExit("PostgresSessionStore unexpectedly constructed without psycopg")
"""
    env = os.environ.copy()
    paths = [
        str(ROOT / "packages" / "harness-runtime" / "src"),
        str(ROOT / "packages" / "harness-protocol" / "src"),
    ]
    if env.get("PYTHONPATH"):
        paths.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(paths)
    result = subprocess.run(
        [sys.executable, "-c", code],
        check=False,
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr


def test_missing_driver_error_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(postgres, "psycopg", None)
    monkeypatch.setattr(postgres, "_pg_errors", None)
    monkeypatch.setattr(postgres, "_dict_row", None)
    with pytest.raises(ImportError, match="postgres"):
        PostgresSessionStore("postgresql://localhost/example")


@pytest.mark.skipif(not postgres.PSYCOPG_AVAILABLE, reason="requires the postgres extra")
async def test_construction_is_lazy_and_validates_dsn(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HARNESS_STORE", raising=False)
    with pytest.raises(ValueError, match="DSN"):
        PostgresSessionStore()
    with pytest.raises(ValueError, match="filesystem path"):
        PostgresSessionStore("/tmp/harness.sqlite3")

    store = PostgresSessionStore(
        "postgresql://postgres@127.0.0.1:1/example", connect_timeout=1
    )
    keyword_store = PostgresSessionStore(
        "host=127.0.0.1 port=1 dbname=example connect_timeout=1"
    )
    try:
        # Construction is lazy: no server contact until an operation/initialize().
        assert store._connection is None
        assert keyword_store._connection is None
        assert is_postgres_dsn("postgresql://example/db")
        assert is_postgres_dsn("POSTGRES://example/db")
        assert not is_postgres_dsn("var/harness-store")
        assert not is_postgres_dsn(None)
        with pytest.raises(RuntimeError, match="SQLite"):
            store._connect()
    finally:
        await store.close()
        await keyword_store.close()

    monkeypatch.setenv("HARNESS_STORE", "postgresql://postgres@127.0.0.1:1/example")
    from_env = PostgresSessionStore(connect_timeout=1)
    await from_env.close()


@pytest.mark.skipif(not postgres.PSYCOPG_AVAILABLE, reason="requires the postgres extra")
async def test_unreachable_server_fails_on_initialize_not_construction() -> None:
    psycopg = pytest.importorskip("psycopg")
    store = PostgresSessionStore(
        "postgresql://postgres@127.0.0.1:1/example", connect_timeout=1
    )
    try:
        with pytest.raises(psycopg.OperationalError):
            await store.initialize()
        assert store._connection is None
    finally:
        await store.close()


def test_public_api_matches_sqlite_store() -> None:
    """A subclass with silently inherited SQLite methods would be a defect."""

    assert issubclass(PostgresSessionStore, SessionStore)
    base_methods = {
        name: member
        for name, member in vars(SessionStore).items()
        if not name.startswith("_") and callable(member)
    }
    ours = {
        name: member
        for name, member in vars(PostgresSessionStore).items()
        if not name.startswith("_") and callable(member)
    }
    assert set(base_methods) == set(ours), (
        "PostgresSessionStore must override every public SessionStore method; "
        f"missing={sorted(set(base_methods) - set(ours))}, "
        f"extra={sorted(set(ours) - set(base_methods))}"
    )
    for name, base_member in base_methods.items():
        base_signature = inspect.signature(base_member)
        ours_signature = inspect.signature(ours[name])
        assert list(base_signature.parameters) == list(ours_signature.parameters), name
        assert [param.default for param in base_signature.parameters.values()] == [
            param.default for param in ours_signature.parameters.values()
        ], name


# --- opt-in PostgreSQL integration ---------------------------------------


@pytest_asyncio.fixture
async def pg_stores():
    """Two store instances (two connections) plus cleanup for created sessions."""

    stores = [PostgresSessionStore(POSTGRES_DSN) for _ in range(2)]
    await asyncio.gather(*(store.initialize() for store in stores))
    created: set[str] = set()
    try:
        yield stores, created
    finally:
        for session_id in created:
            try:
                await stores[0].delete_session(session_id)
            except Exception:  # pragma: no cover - cleanup is best effort
                pass
        await asyncio.gather(*(store.close() for store in stores))


def _record(
    session_id: str,
    *,
    principal: str = "alice",
    deployment_id: str = "deploy-1",
    cwd: str = "/tmp/workspace",
    workspace_managed: bool = False,
) -> SessionRecord:
    return SessionRecord(
        session_id=session_id,
        principal=principal,
        deployment_id=deployment_id,
        agent_id="example.agent",
        definition_version="1.0.0",
        title="PostgreSQL test",
        cwd=cwd,
        workspace_managed=workspace_managed,
    )


async def _create_session(
    store: PostgresSessionStore,
    created: set[str],
    *,
    initial_bindings: dict[str, dict[str, object]] | None = None,
    policy_revision: int = 3,
    **record_kwargs: object,
) -> str:
    session_id = str(record_kwargs.pop("session_id", f"s_{uuid.uuid4().hex}"))
    await store.create_session(
        _record(session_id, **record_kwargs),
        initial_bindings=initial_bindings
        or {"web": {"enabled": True, "config": {"max_results": 5}}},
        policy_revision=policy_revision,
    )
    created.add(session_id)
    return session_id


async def _raw_connection():
    psycopg = pytest.importorskip("psycopg")
    from psycopg.rows import dict_row

    return await psycopg.AsyncConnection.connect(
        POSTGRES_DSN, autocommit=True, row_factory=dict_row
    )


@requires_postgres
async def test_schema_is_migrated_and_idempotent(pg_stores) -> None:
    stores, _created = pg_stores
    connection = await _raw_connection()
    try:
        cursor = await connection.execute(
            "SELECT COALESCE(MAX(version), 0) AS version FROM harness_store_schema"
        )
        row = await cursor.fetchone()
        assert row["version"] == postgres.SCHEMA_VERSION
        for table in (
            "harness_sessions",
            "harness_session_control",
            "harness_configuration_revisions",
            "harness_configuration_transactions",
            "harness_turns",
            "harness_messages",
            "harness_workspace_records",
            "harness_transcript",
        ):
            cursor = await connection.execute(
                "SELECT to_regclass(%s) AS name", (table,)
            )
            row = await cursor.fetchone()
            assert row["name"] == table
    finally:
        await connection.close()
    # Re-running initialize on an existing store is a no-op, not a migration error.
    await stores[0].initialize()


@requires_postgres
async def test_session_lifecycle_ownership_and_record_aliases(pg_stores) -> None:
    stores, created = pg_stores
    store = stores[0]
    session_id = await _create_session(store, created)

    summary = await store.get_session(session_id)
    assert summary is not None
    assert summary.session_id == session_id
    assert summary.principal == "alice"
    assert summary.deployment_id == "deploy-1"
    assert summary.cwd == "/tmp/workspace"
    assert summary.workspace_managed is False
    assert summary.status == "open"
    assert summary.transcript == []

    managed_id = await _create_session(store, created, workspace_managed=True)
    managed = await store.get_session(managed_id)
    assert managed is not None and managed.workspace_managed is True
    managed_record = await store.load_record(managed_id)
    assert managed_record is not None and managed_record.workspace_managed is True

    with pytest.raises(HarnessError) as duplicate:
        await store.create_session(
            _record(session_id),
            initial_bindings={},
            policy_revision=1,
        )
    assert duplicate.value.code == "INVALID_CONFIGURATION"

    listed = await store.list_sessions("alice")
    assert session_id in {item.session_id for item in listed}
    assert session_id not in {item.session_id for item in await store.list_sessions("bob")}
    assert session_id in {
        item.session_id
        for item in await store.list_sessions("alice", deployment_id="deploy-1")
    }
    assert session_id not in {
        item.session_id
        for item in await store.list_sessions("alice", deployment_id="deploy-2")
    }
    records = await store.list_records(principal="alice", deployment_id="deploy-1")
    assert session_id in {item.session_id for item in records}
    assert all(item.transcript == [] for item in records if item.session_id == session_id)

    token_a = uuid.uuid4().hex
    token_b = uuid.uuid4().hex
    assert await store.acquire_control(session_id, "alice", token_a) is True
    assert await store.acquire_control(session_id, "alice", token_a) is True
    assert await store.acquire_control(session_id, "alice", token_b) is False
    await store.release_control(session_id, token_b)  # wrong token is a no-op
    assert await store.acquire_control(session_id, "alice", token_b) is False
    await store.release_control(session_id, token_a)
    assert await store.acquire_control(session_id, "alice", token_b) is True

    before_touch = (await store.get_session(session_id)).last_active_at
    await store.touch_session(session_id)
    assert (await store.get_session(session_id)).last_active_at >= before_touch

    await store.close_session(session_id)
    closed = await store.get_session(session_id)
    assert closed is not None and closed.status == "closed"
    assert await store.acquire_control(session_id, "alice", token_a) is True
    await store.release_control(session_id, token_a)

    loaded = await store.load_record(session_id)
    assert loaded is not None and loaded.session_id == session_id
    assert loaded.transcript == []

    assert await store.delete_record(session_id) is True
    assert await store.get_session(session_id) is None
    assert await store.delete_record(session_id) is False


@requires_postgres
async def test_stale_ownership_reclamation_is_host_aware(pg_stores) -> None:
    stores, created = pg_stores
    store = stores[0]
    session_id = await _create_session(store, created)
    connection = await _raw_connection()
    timestamp = int(time.time() * 1000)
    try:
        assert await store.acquire_control(session_id, "alice", "live-token") is True
        cursor = await connection.execute(
            "SELECT owner_pid, owner_host FROM harness_session_control "
            "WHERE session_id = %s",
            (session_id,),
        )
        row = await cursor.fetchone()
        assert row["owner_pid"] == os.getpid()
        owner_host = row["owner_host"]
        assert owner_host

        # A live local owner is not stolen even when stale_after is zero.
        assert (
            await store.acquire_control(session_id, "alice", "other-token", stale_after=0)
            is False
        )

        # Schema-v1-style rows without PID information can use the configured age
        # fallback when the per-call argument is omitted.
        await connection.execute(
            "UPDATE harness_session_control SET owner_token = 'orphan', owner_pid = NULL, "
            "owner_host = NULL, acquired_at = %s WHERE session_id = %s",
            (timestamp - 60_000, session_id),
        )
        store.control_stale_after = 30
        try:
            assert (
                await store.acquire_control(session_id, "alice", "other-token") is True
            )
        finally:
            store.control_stale_after = None

        # A dead process on this host is reclaimed immediately.  Overwrite the existing
        # synthetic row directly so the test does not depend on release-then-update order.
        await connection.execute(
            "UPDATE harness_session_control SET owner_token = 'dead-local', "
            "owner_pid = %s, owner_host = %s, acquired_at = %s WHERE session_id = %s",
            (2_147_483_647, owner_host, timestamp, session_id),
        )
        assert await store.acquire_control(session_id, "alice", "local-token") is True

        # A remote owner is never judged dead by this host's PID namespace.
        await connection.execute(
            "UPDATE harness_session_control SET owner_token = 'remote-owner', "
            "owner_pid = %s, owner_host = %s, acquired_at = %s WHERE session_id = %s",
            (2_147_483_647, "another-host.example", timestamp, session_id),
        )
        assert (
            await store.acquire_control(session_id, "alice", "remote-token", stale_after=0)
            is False
        )
        await store.release_control(session_id, "remote-owner")
    finally:
        await connection.close()


@requires_postgres
async def test_revisions_and_transaction_idempotency(pg_stores) -> None:
    stores, created = pg_stores
    store = stores[0]
    session_id = await _create_session(store, created)

    initial = await store.latest_revision(session_id)
    assert initial is not None
    assert (initial.revision, initial.source, initial.applied) == (1, "defaults", False)

    second = await store.append_revision(
        session_id,
        bindings={"web": {"enabled": False, "config": {}}},
        policy_revision=4,
        source="manual",
        request_id="append-1",
    )
    assert second.revision == 2 and second.source == "manual"
    assert (await store.latest_revision(session_id)).revision == 2

    await store.mark_revision_applied(session_id, 2)
    assert (await store.get_revision(session_id, 1)).applied is True
    assert (await store.get_revision(session_id, 2)).applied is True
    await store.mark_revision_applied(session_id, 1, applied=False)
    assert (await store.get_revision(session_id, 1)).applied is False
    assert (await store.get_revision(session_id, 2)).applied is True

    assert (await store.claim_transaction(session_id, "req-a", "hash-1")).status == "claimed"
    with pytest.raises(HarnessError) as in_progress:
        await store.claim_transaction(session_id, "req-a", "hash-1")
    assert in_progress.value.code == "REVISION_CONFLICT"
    with pytest.raises(HarnessError) as changed_payload:
        await store.claim_transaction(session_id, "req-a", "hash-2")
    assert changed_payload.value.code == "INVALID_CONFIGURATION"

    await store.fail_transaction(session_id, "req-a")
    assert (await store.claim_transaction(session_id, "req-a", "hash-1")).status == "claimed"

    built_revisions: list[int] = []

    def build_snapshot(revision: int) -> dict[str, object]:
        built_revisions.append(revision)
        return {"session_id": session_id, "selected_revision": revision, "bindings": {}}

    committed = await store.commit_transaction(
        session_id,
        "req-a",
        bindings={"web": {"enabled": True, "config": {"max_results": 9}}},
        policy_revision=5,
        snapshot_builder=build_snapshot,
    )
    assert committed.revision == 3
    assert built_revisions == [3]
    assert committed.bindings == {"web": {"enabled": True, "config": {"max_results": 9}}}

    claimed = await store.claim_transaction(session_id, "req-a", "hash-1")
    assert claimed.status == "committed" and claimed.revision == 3
    assert claimed.snapshot == {
        "session_id": session_id,
        "selected_revision": 3,
        "bindings": {},
    }

    # Idempotent re-commit returns the stored revision and does not allocate another.
    again = await store.commit_transaction(
        session_id,
        "req-a",
        bindings={"ignored": {"enabled": True, "config": {}}},
        policy_revision=99,
        snapshot_builder=None,
    )
    assert again.revision == 3
    assert (await store.latest_revision(session_id)).revision == 3

    # A lost commit response followed by a client-side failure must not erase the commit.
    await store.fail_transaction(session_id, "req-a")
    assert (await store.claim_transaction(session_id, "req-a", "hash-1")).status == "committed"

    # No-op transactions do not create a revision but still persist their snapshot.
    assert (await store.claim_transaction(session_id, "req-noop", "hash-3")).status == "claimed"
    noop = await store.commit_transaction(
        session_id,
        "req-noop",
        bindings=None,
        policy_revision=5,
        snapshot_builder=lambda revision: {"selected_revision": revision, "noop": True},
        create_revision=False,
    )
    assert noop.revision == 3
    noop_claim = await store.claim_transaction(session_id, "req-noop", "hash-3")
    assert noop_claim.status == "committed" and noop_claim.revision == 3
    assert noop_claim.snapshot == {"selected_revision": 3, "noop": True}
    assert (await store.latest_revision(session_id)).revision == 3

    with pytest.raises(HarnessError) as unclaimed:
        await store.commit_transaction(
            session_id,
            "never-claimed",
            bindings={},
            policy_revision=1,
            snapshot_builder=lambda revision: {},
        )
    assert unclaimed.value.code == "INVALID_CONFIGURATION"


@requires_postgres
async def test_turns_history_and_transcript_semantics(pg_stores) -> None:
    stores, created = pg_stores
    store = stores[0]
    session_id = await _create_session(store, created)

    first = await store.create_turn(
        session_id, "turn-1", active_revision=1, prompt={"kind": "text", "text": "hello"}
    )
    assert first.turn_index == 1 and first.status == "running"
    assert first.prompt == {"kind": "text", "text": "hello"}
    assert [turn.turn_id for turn in await store.list_turns(session_id)] == ["turn-1"]

    messages = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi"},
    ]
    await store.finalize_turn(
        session_id,
        "turn-1",
        status="completed",
        output={"text": "hi"},
        usage={"requests": 1, "input_tokens": 3},
        messages=messages,
    )
    stored_turn = await store.get_turn(session_id, "turn-1")
    assert stored_turn is not None
    assert stored_turn.status == "completed"
    assert stored_turn.output == {"text": "hi"}
    assert stored_turn.usage == {"requests": 1, "input_tokens": 3}
    assert stored_turn.finished_at is not None
    assert json.loads(await store.load_messages_json(session_id)) == messages
    assert await store.message_count(session_id) == 2

    await store.append_messages(
        session_id, "turn-1", [{"role": "user", "content": "one more"}]
    )
    assert await store.message_count(session_id) == 3
    assert len(json.loads(await store.load_messages_json(session_id))) == 3

    await store.create_turn(session_id, "turn-2", active_revision=1, prompt="crash me")
    interrupted = await store.mark_turn_interrupted(session_id)
    assert interrupted == ["turn-2"]
    interrupted_turn = await store.get_turn(session_id, "turn-2")
    assert interrupted_turn is not None
    assert interrupted_turn.status == "interrupted"
    assert interrupted_turn.finished_at is not None

    await store.create_turn(session_id, "turn-3", active_revision=1, prompt="again")
    assert [turn.turn_id for turn in await store.list_turns(session_id)] == [
        "turn-1",
        "turn-2",
        "turn-3",
    ]

    text_seq = await store.append_transcript(
        session_id,
        "turn-1",
        "text_delta",
        {"text": "Hello ", "part_index": 0, "kind": "text"},
    )
    await store.append_transcript_text(session_id, text_seq, "world", "turn-1")
    tool_seq = await store.append_transcript(
        session_id, "turn-2", "tool_call", {"tool_call_id": "call-1", "tool_name": "search"}
    )
    thinking_seq = await store.append_transcript(
        session_id, None, "thinking_delta", {"text": "considering"}
    )
    assert [text_seq, tool_seq, thinking_seq] == [1, 2, 3]

    transcript = await store.load_transcript(session_id)
    assert [event["seq"] for event in transcript] == [1, 2, 3]
    assert transcript[0]["data"]["text"] == "Hello world"
    assert transcript[1]["kind"] == "tool_call"
    assert transcript[2]["turn_id"] is None
    assert len(await store.load_transcript(session_id, limit=2)) == 2
    assert [event["seq"] for event in await store.load_transcript(session_id, turn_id="turn-1")] == [1]

    record = await store.load_record(session_id)
    assert record is not None
    assert [event["seq"] for event in record.transcript] == [1, 2, 3]


@requires_postgres
async def test_workspace_records_and_callbacks(pg_stores) -> None:
    from harness_runtime.workspaces import WorkspaceRecord
    from pydantic_ai.workspaces import WorkspaceRef

    stores, created = pg_stores
    store = stores[0]
    session_id = await _create_session(store, created)
    record = WorkspaceRecord(
        session_id=session_id,
        provider="test",
        created_at=1.0,
        last_used_at=2.0,
        ref=WorkspaceRef(provider="test", id="env-1"),
        mode="ephemeral",
    )
    await store.save_workspace_record(record)
    assert await store.get_workspace_record(session_id) == record
    assert record in await store.list_workspace_records()

    callbacks = store.workspace_record_callbacks()
    updated = WorkspaceRecord(
        session_id=session_id,
        provider="test",
        created_at=1.0,
        last_used_at=3.0,
        ref=None,
        mode="ephemeral",
    )
    await callbacks.save(updated)
    assert await callbacks.get(session_id) == updated
    assert updated in list(await callbacks.records())

    connection = await _raw_connection()
    try:
        await connection.execute(
            "UPDATE harness_workspace_records SET record = %s WHERE session_id = %s",
            ("{not-json", session_id),
        )
    finally:
        await connection.close()
    with pytest.raises(HarnessError) as unreadable:
        await store.get_workspace_record(session_id)
    assert unreadable.value.code == "WORKSPACE_RECORD_FAILED"
    with pytest.raises(HarnessError) as unreadable_list:
        await store.list_workspace_records()
    assert unreadable_list.value.code == "WORKSPACE_RECORD_FAILED"

    await store.delete_workspace_record(session_id)
    assert await store.get_workspace_record(session_id) is None

    # Deleting the session also clears its workspace identity, despite no foreign key.
    await store.save_workspace_record(record)
    await store.delete_session(session_id)
    connection = await _raw_connection()
    try:
        cursor = await connection.execute(
            "SELECT COUNT(*) AS count FROM harness_workspace_records "
            "WHERE session_id = %s",
            (session_id,),
        )
        row = await cursor.fetchone()
        assert row["count"] == 0
    finally:
        await connection.close()


@requires_postgres
async def test_workspace_manager_round_trip_through_store(pg_stores, tmp_path) -> None:
    from harness_runtime.workspaces import WorkspaceManager, local_workspace_provider

    stores, created = pg_stores
    store = stores[0]
    session_id = await _create_session(store, created)
    manager = WorkspaceManager(
        providers=[local_workspace_provider(tmp_path)],
        record_store=store.workspace_record_callbacks(),
    )
    lease = await manager.acquire(session_id, provider="local")
    persisted = await store.get_workspace_record(session_id)
    assert persisted is not None
    assert persisted.ref == lease.ref
    await manager.close(session_id)
    released = await store.get_workspace_record(session_id)
    assert released is not None and released.released_at is not None
    await manager.delete(session_id)
    assert await store.get_workspace_record(session_id) is None


@requires_postgres
async def test_delete_cascades_every_child_table(pg_stores) -> None:
    stores, created = pg_stores
    store = stores[0]
    session_id = await _create_session(store, created)
    await store.acquire_control(session_id, "alice", "owner")
    await store.append_revision(
        session_id,
        bindings={"web": {"enabled": False, "config": {}}},
        policy_revision=2,
        source="manual",
    )
    await store.claim_transaction(session_id, "delete-request", "hash")
    await store.create_turn(session_id, "delete-turn", active_revision=1, prompt="hello")
    await store.finalize_turn(
        session_id,
        "delete-turn",
        status="completed",
        messages=[{"role": "user", "content": "hello"}],
    )
    await store.append_transcript(session_id, "delete-turn", "text_delta", {"text": "go"})

    await store.delete_session(session_id)
    assert await store.get_session(session_id) is None
    connection = await _raw_connection()
    try:
        for table in (
            "harness_sessions",
            "harness_session_control",
            "harness_configuration_revisions",
            "harness_configuration_transactions",
            "harness_turns",
            "harness_messages",
            "harness_workspace_records",
            "harness_transcript",
        ):
            cursor = await connection.execute(
                f"SELECT COUNT(*) AS count FROM {table} WHERE session_id = %s",
                (session_id,),
            )
            row = await cursor.fetchone()
            assert row["count"] == 0, table
    finally:
        await connection.close()


@requires_postgres
async def test_multiworker_claims_and_allocations(pg_stores) -> None:
    stores, created = pg_stores
    store_a, store_b = stores
    session_id = await _create_session(store_a, created)

    revision_results = await asyncio.gather(
        *[
            (store_a if index % 2 == 0 else store_b).append_revision(
                session_id,
                bindings={"n": {"enabled": True, "config": {"index": index}}},
                policy_revision=index + 1,
                source="manual",
            )
            for index in range(4)
        ]
    )
    assert sorted(revision.revision for revision in revision_results) == [2, 3, 4, 5]
    assert (await store_a.latest_revision(session_id)).revision == 5

    turn_results = await asyncio.gather(
        *[
            (store_a if index % 2 == 0 else store_b).create_turn(
                session_id,
                f"concurrent-turn-{index}",
                active_revision=1,
                prompt=f"prompt-{index}",
            )
            for index in range(3)
        ]
    )
    assert sorted(turn.turn_index for turn in turn_results) == [1, 2, 3]

    token_a = uuid.uuid4().hex
    token_b = uuid.uuid4().hex
    controls = await asyncio.gather(
        store_a.acquire_control(session_id, "alice", token_a),
        store_b.acquire_control(session_id, "alice", token_b),
    )
    assert sorted(controls) == [False, True]
    winner = token_a if controls[0] else token_b
    await store_b.release_control(session_id, winner)

    claims = await asyncio.gather(
        store_a.claim_transaction(session_id, "shared-request", "same-hash"),
        store_b.claim_transaction(session_id, "shared-request", "same-hash"),
        return_exceptions=True,
    )
    claimed = [result for result in claims if not isinstance(result, BaseException)]
    conflicts = [result for result in claims if isinstance(result, HarnessError)]
    assert len(claimed) == 1 and claimed[0].status == "claimed"
    assert len(conflicts) == 1 and conflicts[0].code == "REVISION_CONFLICT"

    committed = await store_a.commit_transaction(
        session_id,
        "shared-request",
        bindings={"web": {"enabled": True, "config": {}}},
        policy_revision=1,
        snapshot_builder=lambda revision: {"selected_revision": revision},
    )
    assert committed.revision == 6
    for store in stores:
        claim = await store.claim_transaction(session_id, "shared-request", "same-hash")
        assert claim.status == "committed" and claim.revision == 6


@requires_postgres
async def test_missing_session_writes_are_explicit(pg_stores) -> None:
    stores, _created = pg_stores
    store = stores[0]
    missing = f"s_{uuid.uuid4().hex}"

    with pytest.raises(HarnessError) as acquire:
        await store.acquire_control(missing, "alice", "token")
    assert acquire.value.code == "INVALID_CONFIGURATION"
    with pytest.raises(HarnessError):
        await store.append_revision(
            missing, bindings={}, policy_revision=1, source="manual"
        )
    with pytest.raises(HarnessError):
        await store.claim_transaction(missing, "request", "hash")
    with pytest.raises(HarnessError):
        await store.create_turn(missing, "turn", active_revision=1, prompt="hello")
    with pytest.raises(HarnessError):
        await store.finalize_turn(missing, "turn", status="completed")
    assert await store.mark_turn_interrupted(missing) == []
    await store.mark_revision_applied(missing, 1)  # read-like no-op, as in SQLite
    await store.release_control(missing, "token")  # idempotent no-op
    await store.delete_session(missing)  # idempotent no-op


@requires_postgres
async def test_close_reconnects_for_later_operations(pg_stores) -> None:
    stores, created = pg_stores
    store = stores[0]
    session_id = await _create_session(store, created)
    await store.close()
    await store.close()  # idempotent
    summary = await store.get_session(session_id)
    assert summary is not None and summary.session_id == session_id


@requires_postgres
async def test_real_session_turn_persists_history_and_transcript(pg_stores) -> None:
    from harness_runtime import AgentDefinition, PluginRegistry, SessionManager
    from pydantic_ai import Agent
    from pydantic_ai.models.test import TestModel

    stores, created = pg_stores
    store = stores[0]

    def factory():
        return Agent(TestModel(), name="postgres-test")

    definition = AgentDefinition(
        agent_id="example.postgres.turn",
        name="PostgreSQL turn",
        factory=factory,
    )
    manager = SessionManager(PluginRegistry(), store=store)
    manager.register_definition(definition)
    try:
        async with manager.open_session(
            definition,
            principal="alice",
            deployment_id="deploy-1",
        ) as session:
            session_id = session.session_id
            created.add(session_id)
            result = await session.run("hello")
            assert result.output
            turns = await store.list_turns(session_id)
            assert [turn.status for turn in turns] == ["completed"]
            assert turns[0].prompt == "hello"
            first_message_count = await store.message_count(session_id)
            assert first_message_count > 0
            transcript = await store.load_transcript(session_id)
            assert any(row["kind"] == "text_delta" for row in transcript)

        resumed = await manager.open_saved_session(
            "alice", session_id, deployment_id="deploy-1"
        )
        try:
            assert await resumed.history()
            assert [turn.status for turn in await store.list_turns(session_id)] == [
                "completed"
            ]
            second = await resumed.run("again")
            assert second.output
            assert [turn.turn_index for turn in await store.list_turns(session_id)] == [1, 2]
            assert await store.message_count(session_id) > first_message_count
        finally:
            await resumed.close()
    finally:
        await manager.close()


@requires_postgres
async def test_session_manager_accepts_postgres_store(pg_stores) -> None:
    """The store must satisfy session.py's isinstance-based store coercion."""

    from harness_runtime import AgentDefinition, PluginRegistry, SessionManager

    stores, created = pg_stores
    store = stores[0]
    manager = SessionManager(PluginRegistry(), store=store)
    definition = AgentDefinition(agent_id="example.postgres", name="PostgreSQL example")
    try:
        async with manager.open_session(
            definition,
            principal="alice",
            deployment_id="deploy-1",
            title="Manager integration",
        ) as session:
            session_id = session.session_id
            created.add(session_id)
            summary = await store.get_session(session_id)
            assert summary is not None
            assert (summary.principal, summary.deployment_id) == ("alice", "deploy-1")
            assert summary.status == "open"

        closed = await store.get_session(session_id)
        assert closed is not None and closed.status == "closed"

        resumed = await manager.open_saved_session(
            "alice", session_id, deployment_id="deploy-1"
        )
        try:
            assert resumed.session_id == session_id
            assert resumed.snapshot.selected_revision == 1
        finally:
            await resumed.close()
    finally:
        await manager.close()
