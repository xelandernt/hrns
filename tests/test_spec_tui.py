"""CONCEPT §15 terminal regressions found during full spec verification.

Each test pins a confirmed mismatch between the Textual contract and the
previous terminal implementation. They use only the public TUI surface plus
the existing independent/extension ACP fixtures.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from harness_client.events import ConfigOptionsUpdated
from harness_client.profiles import Profile, Profiles
from harness_client.renderers import RendererRegistry
from harness_client.tui.app import HarnessApp
from harness_client.tui.screens import ConnectScreen
from harness_client.tui.state import build_form_fields, coerce_value, tool_detail_text
from harness_client.tui.widgets import ActivityPanel, ToolCard, TranscriptPane
from test_client_support import (
    EXTENSION_AGENT_SOURCE,
    INDEPENDENT_AGENT,
    write_extension_agent,
)
from test_client_ui import make_app, open_chat, pane_text, send_message, wait_for

# --------------------------------------------------------------- replay bounds


async def test_replay_keeps_agent_turns_after_their_user_prompts():
    """Loading a multi-turn session must not merge later answers into turn one."""
    app, _target = make_app([sys.executable, str(INDEPENDENT_AGENT)])
    async with app.run_test(size=(120, 40)) as pilot:
        screen = await open_chat(pilot, app)
        pane = screen.query_one("#transcript", TranscriptPane)
        pane.begin_replay()
        await pane.add_user("first question")
        await pane.append_agent_text("first answer")
        await pane.add_user("second question")
        await pane.append_agent_text("second answer")
        await pane.end_replay()

        blocks = list(pane.children)
        assert [type(block).__name__ for block in blocks] == [
            "MessageBlock",
            "MessageBlock",
            "MessageBlock",
            "MessageBlock",
        ]
        assert blocks[0]._text == "first question"
        assert blocks[1]._text == "first answer"
        assert "second answer" not in blocks[1]._text
        assert blocks[2]._text == "second question"
        assert blocks[3]._text == "second answer"
    await app.shutdown_client()


async def test_replay_coalesces_chunks_and_merges_tool_progress():
    app, _target = make_app([sys.executable, str(INDEPENDENT_AGENT)])
    async with app.run_test(size=(120, 40)) as pilot:
        screen = await open_chat(pilot, app)
        pane = screen.query_one("#transcript", TranscriptPane)
        pane.begin_replay()
        await pane.append_agent_text("first ")
        await pane.append_agent_text("answer")
        start = SimpleNamespace(
            session_id="replay",
            tool_call_id="tool-1",
            title="Delegated lookup",
            kind="other",
            status="pending",
            is_new=True,
            content=(),
            locations=(),
            raw_input={"query": "x"},
            raw_output=None,
            meta={},
        )
        update = SimpleNamespace(
            session_id="replay",
            tool_call_id="tool-1",
            title="",
            kind="",
            status="completed",
            is_new=False,
            content=(
                {"type": "content", "content": {"type": "text", "text": "tool done"}},
            ),
            locations=(),
            raw_input=None,
            raw_output=None,
            meta={},
        )
        await pane.start_tool(start)
        await pane.update_tool(update)
        entries = pane.replay_entries()
        assert [kind for kind, _payload in entries] == ["agent", "tool"]
        assert entries[0][1] == "first answer"
        assert entries[1][1]["status"] == "completed"
        assert "tool done" in entries[1][1]["detail"]

        await pane.end_replay()
        cards = [child for child in pane.children if isinstance(child, ToolCard)]
        assert len(cards) == 1
        assert "tool done" in str(cards[0]._detail.render())
    await app.shutdown_client()


# ----------------------------------------------------- generic/specialized tool


async def test_tool_card_has_initial_generic_details_and_keeps_unknown_content():
    event = SimpleNamespace(
        content=(
            {"type": "diff", "path": "a.py", "oldText": "old", "newText": "new"},
            {"type": "terminal", "terminalId": "term-1", "command": "pytest -q"},
            {"type": "future-content", "value": 7},
        ),
        raw_input={"path": "a.py"},
        raw_output=None,
        locations=(),
    )
    detail = tool_detail_text(event)
    assert "--- a.py" in detail and "+ new" in detail
    assert "terminal term-1" in detail and "$ pytest -q" in detail
    assert "future-content" in detail and '"value": 7' in detail

    app, _target = make_app([sys.executable, str(INDEPENDENT_AGENT)])
    async with app.run_test(size=(120, 40)) as pilot:
        await open_chat(pilot, app)
        card = ToolCard(
            {
                "toolCallId": "t1",
                "title": "Read file",
                "status": "completed",
                "detail": detail,
            }
        )
        # Generic details must render on the first snapshot, not only after an update.
        assert "future-content" in str(card._detail.render())
    await app.shutdown_client()


async def test_local_renderer_applies_with_safe_versioned_fallback():
    app, _target = make_app([sys.executable, str(INDEPENDENT_AGENT)])
    async with app.run_test(size=(120, 40)) as pilot:
        await open_chat(pilot, app)
        registry = RendererRegistry()
        registry.register("demo.diff", 1, lambda data: f"{data['path']}: +{data['added']}")
        card = ToolCard(
            {
                "toolCallId": "t2",
                "title": "Edit",
                "status": "completed",
                "detail": "generic fallback",
                "presentation": {
                    "rendererId": "demo.diff",
                    "schemaVersion": 1,
                    "data": {"path": "a.py", "added": 2},
                    "fallback": "peer fallback",
                },
            },
            renderer=registry,
        )
        assert "a.py: +2" in str(card._detail.render())

        unknown = ToolCard(
            {
                "toolCallId": "t3",
                "title": "Edit",
                "status": "completed",
                "detail": "generic fallback",
                "presentation": {
                    "rendererId": "demo.diff",
                    "schemaVersion": 99,
                    "data": {},
                    "fallback": "safe peer fallback",
                },
            },
            renderer=registry,
        )
        assert "safe peer fallback" in str(unknown._detail.render())
    await app.shutdown_client()


# ------------------------------------------------------------------ usage/plans


async def test_prompt_result_usage_reaches_activity_panel(tmp_path: Path):
    script = write_extension_agent(tmp_path)
    app, _target = make_app([sys.executable, str(script)])
    async with app.run_test(size=(120, 40)) as pilot:
        screen = await open_chat(pilot, app)
        activity = screen.query_one("#side-panel").activity
        await send_message(pilot, app, "tool hello")
        pane = screen.query_one("#transcript", TranscriptPane)
        await wait_for(
            pilot,
            lambda: "ext reply: tool hello" in pane_text(pane)
            and "42" in str(activity._usage.render()),
            message="prompt-result usage",
        )
        assert "42" in str(activity._usage.render())
    await app.shutdown_client()


async def test_new_session_clears_previous_plan_commands_and_usage(tmp_path: Path):
    script = write_extension_agent(tmp_path)
    app, _target = make_app([sys.executable, str(script)])
    async with app.run_test(size=(120, 40)) as pilot:
        screen = await open_chat(pilot, app)
        pane = screen.query_one("#transcript", TranscriptPane)
        activity = screen.query_one("#side-panel").activity
        await send_message(pilot, app, "plan the work")
        await wait_for(
            pilot,
            lambda: "ext reply: plan the work" in pane_text(pane)
            and "Inspect the workspace" in str(activity._plan.render())
            and "42" in str(activity._usage.render()),
            message="plan and usage",
        )
        first_id = screen.session.session_id

        await pilot.press("ctrl+n")
        await wait_for(
            pilot,
            lambda: screen.session.session_id != first_id
            and "No plan reported" in str(activity._plan.render())
            and "No usage reported" in str(activity._usage.render())
            and "No agent commands" in str(activity._commands.render()),
            message="session activity reset",
        )
        status = str(screen.query_one("#statusbar").render())
        assert "42 tokens" not in status
    await app.shutdown_client()


# -------------------------------------------------------------- subagent panel


def test_delegation_tool_is_visible_in_subagent_activity():
    activity = ActivityPanel()
    activity.note_tool(
        {
            "toolCallId": "delegate-1",
            "title": "delegate_task: research",
            "status": "in_progress",
            "kind": "other",
        }
    )
    assert "delegate_task" in str(activity._subagents.render())
    activity.note_tool(
        {"toolCallId": "delegate-1", "title": "", "status": "completed"}
    )
    rendered = str(activity._subagents.render())
    assert rendered.count("delegate_task") == 1
    assert "completed" in rendered

    activity.clear_subagents()
    assert "No subagent activity" in str(activity._subagents.render())


# --------------------------------------------------------------- keyboard/filter


async def test_connection_filter_accepts_q_without_quitting():
    profile = Profile(
        name="fixture",
        transport="stdio",
        command=sys.executable,
        args=[str(INDEPENDENT_AGENT)],
    )
    app = HarnessApp(Profiles({"fixture": profile}))
    async with app.run_test(size=(110, 36)) as pilot:
        await wait_for(pilot, lambda: isinstance(app.screen, ConnectScreen))
        picker = app.screen
        picker._filter.focus()
        await pilot.press("q")
        await pilot.pause(0.1)
        assert app.is_running
        assert picker._filter.value == "q"
    await app.shutdown_client()


async def test_reconnect_is_refused_while_a_turn_is_running():
    app, _target = make_app([sys.executable, str(INDEPENDENT_AGENT)])
    async with app.run_test(size=(120, 40)) as pilot:
        screen = await open_chat(pilot, app)
        original = screen.client
        screen._set_busy(True, detail="working…")
        await screen._reconnect()
        pane = screen.query_one("#transcript", TranscriptPane)
        assert screen.client is original
        assert "Stop the running turn before reconnecting" in pane_text(pane)
        screen._set_busy(False)
    await app.shutdown_client()


async def test_escape_cancels_the_running_turn():
    app, _target = make_app([sys.executable, str(INDEPENDENT_AGENT)])
    async with app.run_test(size=(120, 40)) as pilot:
        screen = await open_chat(pilot, app)
        screen.session.cancel = AsyncMock()
        screen._set_busy(True, detail="working…")
        await pilot.press("escape")
        await wait_for(pilot, lambda: screen.session.cancel.await_count == 1)
        pane = screen.query_one("#transcript", TranscriptPane)
        await wait_for(
            pilot,
            lambda: "Cancellation requested" in pane_text(pane),
            message="cancellation notice",
        )
        screen._set_busy(False)
    await app.shutdown_client()


# ------------------------------------------------------------ adaptive controls


async def test_session_hint_omits_unadvertised_actions(tmp_path: Path):
    source = EXTENSION_AGENT_SOURCE.replace(
        '{"list": {}, "delete": {}, "close": {}, "resume": {}}',
        '{"list": {}, "delete": {}, "close": {}}',
    )
    assert '"resume": {}' not in source
    script = tmp_path / "no_resume_agent.py"
    script.write_text(source, encoding="utf-8")
    app, _target = make_app([sys.executable, str(script)])
    async with app.run_test(size=(120, 40)) as pilot:
        screen = await open_chat(pilot, app)
        await pilot.press("f3")
        sessions = screen.query_one("#side-panel").sessions
        await wait_for(pilot, lambda: sessions._table.row_count == 1)
        await wait_for(pilot, lambda: "resume not advertised" in str(sessions._status.render()))
        hint = str(sessions._hint.render())
        assert "u resumes" not in hint
        assert "d deletes" in hint and "x closes" in hint
    await app.shutdown_client()


# --------------------------------------------------------------- attachments


async def test_attach_clear_removes_every_staged_file(tmp_path: Path):
    first = tmp_path / "first.md"
    second = tmp_path / "second.txt"
    first.write_text("one", encoding="utf-8")
    second.write_text("two", encoding="utf-8")
    app, _target = make_app([sys.executable, str(INDEPENDENT_AGENT)])
    async with app.run_test(size=(120, 40)) as pilot:
        screen = await open_chat(pilot, app)
        pane = screen.query_one("#transcript", TranscriptPane)
        await send_message(pilot, app, f"/attach {first}")
        await wait_for(pilot, lambda: len(screen._attachments) == 1)
        await send_message(pilot, app, f"/attach {second}")
        await wait_for(pilot, lambda: len(screen._attachments) == 2)
        await send_message(pilot, app, "/attach clear")
        await wait_for(
            pilot,
            lambda: screen._attachments == []
            and "Cleared 2 staged attachment" in pane_text(pane),
            message="attachments cleared",
        )
    await app.shutdown_client()


async def test_standard_config_panel_tracks_agent_option_updates(tmp_path: Path):
    script = write_extension_agent(tmp_path)
    app, _target = make_app(
        [sys.executable, str(script)], env={"FIXTURE_EXTENSION": "0"}
    )
    async with app.run_test(size=(120, 40)) as pilot:
        screen = await open_chat(pilot, app)
        panel = screen.query_one("#side-panel").capabilities
        await wait_for(pilot, lambda: panel._list.option_count == 2)
        options = (
            {
                "id": "model",
                "name": "Model",
                "type": "select",
                "currentValue": "test-deep",
                "options": [
                    {"value": "test-fast", "name": "Test Fast"},
                    {"value": "test-deep", "name": "Test Deep"},
                ],
            },
            {
                "id": "stream",
                "name": "Stream output",
                "type": "boolean",
                "currentValue": False,
            },
        )
        await screen._handle_event(
            ConfigOptionsUpdated(session_id=screen.session.session_id, options=options)
        )
        rows = [
            str(panel._list.get_option_at_index(index).prompt)
            for index in range(panel._list.option_count)
        ]
        assert any("Model: Test Deep" in row for row in rows)
        assert any("Stream output: off" in row for row in rows)
    await app.shutdown_client()


# ------------------------------------------------------------ capability detail


async def test_capability_detail_reports_defaults(tmp_path: Path):
    script = write_extension_agent(tmp_path)
    app, _target = make_app([sys.executable, str(script)])
    async with app.run_test(size=(120, 40)) as pilot:
        screen = await open_chat(pilot, app)
        panel = screen.query_one("#side-panel").capabilities
        await wait_for(pilot, lambda: panel._list.option_count == 3)
        detail = str(panel._detail.render())
        assert "default: on" in detail
    await app.shutdown_client()


# -------------------------------------------------------- structured elicitation


def test_array_elicitation_field_coerces_declared_item_types():
    fields = build_form_fields(
        {
            "type": "object",
            "properties": {
                "tags": {"type": "array", "items": {"type": "string"}},
                "counts": {"type": "array", "items": {"type": "integer"}},
            },
        }
    )
    by_name = {field["name"]: field for field in fields}
    assert coerce_value(by_name["tags"], "one, two") == ["one", "two"]
    assert coerce_value(by_name["counts"], "1, 2") == [1, 2]
