"""Persistence, retry identity, ownership, concurrency, and reconstruction."""

from __future__ import annotations

import asyncio
import sqlite3

import pytest
from harness_protocol import EnableChange, HarnessError
from harness_runtime import SessionStore
from harness_runtime.policy import changes_payload_hash
from test_runtime_support import MARKER, definition_with_echo, make_manager


def sqlite_scalar(path, query: str, params: tuple = ()):
    connection = sqlite3.connect(path)
    try:
        row = connection.execute(query, params).fetchone()
        return row[0] if row else None
    finally:
        connection.close()


def instructions_of(result: object) -> str:
    return "\n".join(
        str(message.instructions)
        for message in result.new_messages()  # type: ignore[attr-defined]
        if getattr(message, "instructions", None)
    )


async def test_sqlite_wal_and_expected_tables(tmp_path):
    path = tmp_path / "wal.db"
    store = SessionStore(path)
    await store.initialize()
    try:
        assert sqlite_scalar(path, "PRAGMA journal_mode") == "wal"
        tables = {
            row[0]
            for row in sqlite3.connect(path).execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert {
            "sessions",
            "session_control",
            "configuration_revisions",
            "configuration_transactions",
            "turns",
            "messages",
            "transcript",
        } <= tables
    finally:
        await store.close()


async def test_revisions_history_and_continuation_survive_reopen(tmp_path):
    manager = make_manager(tmp_path)
    definition = definition_with_echo()
    async with manager.open_session(definition) as session:
        session_id = session.session_id
        await session.run("first")
        await session.update_configuration(
            [EnableChange(binding_id="echo", enabled=False)], "persist-1", 1
        )
        history_before = await session.history()
        turns_before = await session._store.list_turns(session_id)
        conversation = session._conversation_id

    reopened_manager = make_manager(tmp_path)
    reopened_manager.register_definition(definition)
    session = await reopened_manager.open_saved_session(
        "local", session_id, resources={}
    )
    try:
        assert session.snapshot.selected_revision == 2
        assert session.snapshot.bindings["echo"].enabled is False
        assert session.snapshot.application_status == "applied"
        history_after = await session.history()
        assert len(history_after) == len(history_before)
        assert [type(message).__name__ for message in history_after] == [
            type(message).__name__ for message in history_before
        ]
        assert session._conversation_id == conversation
        turns = await session._store.list_turns(session_id)
        assert [turn.status for turn in turns] == [turn.status for turn in turns_before]
        result = await session.run("continued")
        assert result.output == "done"
        assert result.conversation_id == conversation
        assert MARKER not in instructions_of(result)
    finally:
        await session.close()


async def test_usage_accumulates_and_survives_reopen(tmp_path):
    manager = make_manager(tmp_path)
    definition = definition_with_echo()
    async with manager.open_session(definition) as session:
        await session.run("one")
        await session.run("two")
        usage = session.usage
        assert usage.requests >= 2
        session_id = session.session_id
    reopened = make_manager(tmp_path)
    reopened.register_definition(definition)
    session = await reopened.open_saved_session("local", session_id)
    try:
        assert session.usage.requests == usage.requests
        assert session.usage.input_tokens == usage.input_tokens
    finally:
        await session.close()


async def test_turn_records_store_outcome_usage_and_active_revision(tmp_path):
    manager = make_manager(tmp_path)
    definition = definition_with_echo()
    async with manager.open_session(definition) as session:
        await session.run("hello")
        turns = await session._store.list_turns(session.session_id)
        assert len(turns) == 1
        turn = turns[0]
        assert turn.status == "completed"
        assert turn.active_revision == 1
        assert turn.output == "done"
        assert turn.usage["requests"] >= 1
        assert turn.started_at <= turn.finished_at
        assert turn.prompt == "hello"


async def test_transcript_coalesces_text_and_records_tool_lifecycle(tmp_path):
    manager = make_manager(tmp_path)
    definition = definition_with_echo()
    async with manager.open_session(definition) as session:
        streamed = [event async for event in session.stream("hello")]
        transcript = await session.transcript()
        text_rows = [row for row in transcript if row["kind"] == "text_delta"]
        assert text_rows, transcript
        streamed_text = "".join(
            str(event.data["text"]) for event in streamed if event.type == "text_delta"
        )
        assert "".join(str(row["data"]["text"]) for row in text_rows) == streamed_text
        tool_calls = [row for row in transcript if row["kind"] == "tool_call"]
        tool_updates = [row for row in transcript if row["kind"] == "tool_update"]
        assert len(tool_calls) == 1
        assert tool_updates and tool_updates[0]["data"]["status"] == "completed"
        record = await session._store.load_record(session.session_id)
        assert [row["kind"] for row in record.transcript] == [
            row["kind"] for row in transcript
        ]


async def test_idempotent_retry_after_reopen_returns_committed_snapshot(tmp_path):
    manager = make_manager(tmp_path)
    definition = definition_with_echo()
    async with manager.open_session(definition) as session:
        session_id = session.session_id
        first = await session.update_configuration(
            [EnableChange(binding_id="echo", enabled=False)], "retry-persist", 1
        )
        assert first.selected_revision == 2

    reopened = make_manager(tmp_path)
    reopened.register_definition(definition)
    session = await reopened.open_saved_session("local", session_id)
    try:
        repeated = await session.update_configuration(
            [EnableChange(binding_id="echo", enabled=False)], "retry-persist", None
        )
        assert repeated.selected_revision == 2
        latest = await session._store.latest_revision(session_id)
        assert latest.revision == 2  # no extra revision from the retry
    finally:
        await session.close()


async def test_in_progress_transaction_returns_retryable_conflict(tmp_path):
    manager = make_manager(tmp_path)
    definition = definition_with_echo()
    change = [EnableChange(binding_id="echo", enabled=False)]
    payload_hash = changes_payload_hash(change)
    async with manager.open_session(definition) as session:
        claim = await session._store.claim_transaction(
            session.session_id, "concurrent-1", payload_hash
        )
        assert claim.status == "claimed"
        # Same payload while the claim is in progress: retryable revision conflict.
        with pytest.raises(HarnessError) as excinfo:
            await session.update_configuration(change, "concurrent-1", 1)
        assert excinfo.value.code == "REVISION_CONFLICT"
        # Different payload for the same request id is rejected by retry identity.
        with pytest.raises(HarnessError) as excinfo:
            await session.update_configuration(
                [EnableChange(binding_id="echo", enabled=True)], "concurrent-1", 1
            )
        assert excinfo.value.code == "INVALID_CONFIGURATION"
        await session._store.fail_transaction(session.session_id, "concurrent-1")
        # Same request id may be retried after a failed (non-committed) attempt.
        recovered = await session.update_configuration(change, "concurrent-1", 1)
        assert recovered.selected_revision == 2


async def test_ownership_denied_for_other_principal(tmp_path):
    manager = make_manager(tmp_path)
    definition = definition_with_echo()
    async with manager.open_session(definition) as session:
        session_id = session.session_id
    other = make_manager(tmp_path)
    other.register_definition(definition)
    with pytest.raises(HarnessError) as excinfo:
        await other.open_saved_session("intruder", session_id)
    assert excinfo.value.code == "POLICY_DENIED"
    assert excinfo.value.details["reason"] == "ownership"


async def test_missing_pinned_definition_is_deployment_unavailable(tmp_path):
    manager = make_manager(tmp_path)
    definition = definition_with_echo()
    async with manager.open_session(definition) as session:
        session_id = session.session_id
    fresh = make_manager(tmp_path)
    # Deliberately do not register the definition revision.
    with pytest.raises(HarnessError) as excinfo:
        await fresh.open_saved_session("local", session_id)
    assert excinfo.value.code == "DEPLOYMENT_UNAVAILABLE"
    assert excinfo.value.details["agent_id"] == definition.agent_id


async def test_control_claim_blocks_second_worker_until_close(tmp_path):
    manager = make_manager(tmp_path)
    definition = definition_with_echo()
    first = await manager.open_session(definition)
    try:
        other = make_manager(tmp_path)
        other.register_definition(definition)
        with pytest.raises(HarnessError) as excinfo:
            await other.open_saved_session("local", first.session_id)
        assert excinfo.value.code == "SESSION_BUSY"
        with pytest.raises(HarnessError) as excinfo:
            await manager.open_saved_session("local", first.session_id)
        assert excinfo.value.code == "SESSION_BUSY"
    finally:
        await first.close()
    second = await other.open_saved_session("local", first.session_id)
    await second.close()


async def test_duplicate_session_id_rejected(tmp_path):
    manager = make_manager(tmp_path)
    definition = definition_with_echo()
    async with manager.open_session(definition) as session:
        session_id = session.session_id
    # Reusing a closed explicit id is rejected by the store, not silently replaced.
    with pytest.raises(HarnessError) as excinfo:
        await manager.open_session(definition, session_id=session_id)
    assert excinfo.value.code == "INVALID_CONFIGURATION"


async def test_delete_removes_history_transcript_and_retry_records(tmp_path):
    path = tmp_path / "sessions.db"
    manager = make_manager(tmp_path)
    definition = definition_with_echo()
    async with manager.open_session(definition) as session:
        session_id = session.session_id
        await session.run("hello")
        await session.update_configuration(
            [EnableChange(binding_id="echo", enabled=False)], "delete-me", 1
        )
    await manager.delete_session("local", session_id)
    assert await manager._store.get_session(session_id) is None
    assert await manager._store.message_count(session_id) == 0
    assert await manager._store.load_transcript(session_id) == []
    assert sqlite_scalar(
        path, "SELECT COUNT(*) FROM configuration_transactions WHERE session_id = ?", (session_id,)
    ) == 0
    assert sqlite_scalar(
        path, "SELECT COUNT(*) FROM configuration_revisions WHERE session_id = ?", (session_id,)
    ) == 0


async def test_delete_requires_ownership(tmp_path):
    manager = make_manager(tmp_path)
    definition = definition_with_echo()
    async with manager.open_session(definition) as session:
        session_id = session.session_id
    with pytest.raises(HarnessError) as excinfo:
        await manager.delete_session("someone-else", session_id)
    assert excinfo.value.code == "POLICY_DENIED"


async def test_listing_filters_by_principal_and_records_include_cwd(tmp_path):
    manager = make_manager(tmp_path)
    definition = definition_with_echo()
    path = tmp_path / "workspace"
    path.mkdir()
    async with manager.open_session(definition, resources={"workspace": str(path)}) as session:
        session_id = session.session_id
    records = await manager._store.list_records(principal="local", deployment_id="local")
    assert [record.session_id for record in records] == [session_id]
    assert records[0].cwd == str(path)
    assert records[0].agent_id == definition.agent_id
    assert await manager._store.list_records(principal="other") == []
    loaded = await manager._store.load_record(session_id)
    assert loaded is not None and loaded.transcript == []


async def test_interrupted_turn_surfaces_on_reopen(tmp_path):
    manager = make_manager(tmp_path)
    definition = definition_with_echo()
    async with manager.open_session(definition) as session:
        session_id = session.session_id
        # Simulate a crash after the turn was accepted but before it finished.
        await session._store.create_turn(
            session_id, "t_crash", active_revision=1, prompt="orphaned"
        )
    reopened = make_manager(tmp_path)
    reopened.register_definition(definition)
    session = await reopened.open_saved_session("local", session_id)
    try:
        turns = await session._store.list_turns(session_id)
        assert turns[-1].status == "interrupted"
        transcript = await session.transcript()
        assert any(
            row["kind"] == "turn_interrupted" and row["turn_id"] == "t_crash"
            for row in transcript
        )
    finally:
        await session.close()


async def test_concurrent_sessions_on_shared_sqlite(tmp_path):
    definition = definition_with_echo()
    manager_a = make_manager(tmp_path)
    manager_b = make_manager(tmp_path)
    manager_b.register_definition(definition)

    async def run_one(manager, text):
        async with manager.open_session(definition) as session:
            result = await session.run(text)
            return session.session_id, result.output

    (session_a, output_a), (session_b, output_b) = await asyncio.gather(
        run_one(manager_a, "a"), run_one(manager_b, "b")
    )
    assert session_a != session_b
    assert output_a == output_b == "done"
    records = await manager_a._store.list_records(principal="local")
    assert {record.session_id for record in records} == {session_a, session_b}


async def test_uncommitted_transaction_records_do_not_leak_revisions(tmp_path):
    manager = make_manager(tmp_path)
    definition = definition_with_echo()
    async with manager.open_session(definition) as session:
        await session._store.claim_transaction(session.session_id, "abandoned", "hash")
        latest = await session._store.latest_revision(session.session_id)
        assert latest.revision == 1  # claimed but never committed
        # A stale in-progress row is not silently overwritten with a different payload.
        with pytest.raises(HarnessError) as excinfo:
            await session._store.claim_transaction(session.session_id, "abandoned", "other-hash")
        assert excinfo.value.code == "INVALID_CONFIGURATION"


async def test_missing_session_writes_are_structured_errors(tmp_path):
    store = SessionStore(tmp_path / "missing.db")
    await store.initialize()
    try:
        with pytest.raises(HarnessError) as revision:
            await store.append_revision(
                "missing", bindings={}, policy_revision=1, source="test"
            )
        assert revision.value.code == "INVALID_CONFIGURATION"
        with pytest.raises(HarnessError) as claim:
            await store.claim_transaction("missing", "request", "hash")
        assert claim.value.code == "INVALID_CONFIGURATION"
    finally:
        await store.close()


async def test_dead_owner_claim_is_reclaimed_by_new_worker(tmp_path):
    path = tmp_path / "control.db"
    store = SessionStore(path)
    await store.initialize()
    try:
        assert await store.acquire_control("s1", "local", "token-a") is True
        assert await store.acquire_control("s1", "local", "token-b") is False  # live owner
        connection = sqlite3.connect(path)
        connection.execute(
            "UPDATE session_control SET owner_pid = ? WHERE session_id = ?", (999_999_999, "s1")
        )
        connection.commit()
        connection.close()
        assert await store.acquire_control("s1", "local", "token-b") is True
        await store.release_control("s1", "token-b")
        assert await store.acquire_control("s1", "local", "token-c") is True
    finally:
        await store.close()
