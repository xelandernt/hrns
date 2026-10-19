"""Extra Textual Pilot tests: UX robustness gaps beyond test_client_ui.py.

Covers: collision-safe palette selection, collision notices on typed slash
commands, /model without options, prompts blocked during session replay,
transcript/stream bounds, session resume from the sessions panel, global
submit and focus keys, and the settings theme choice.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import harness_client.tui.widgets as widgets
from harness_client.tui.dialogs import CommandPaletteDialog, HelpDialog, SettingsDialog
from harness_client.tui.state import Settings
from harness_client.tui.widgets import Composer, ReasoningBlock, TranscriptPane
from test_client_support import EXTENSION_AGENT_SOURCE, INDEPENDENT_AGENT, write_extension_agent
from test_client_ui import make_app, open_chat, pane_text, send_message, wait_for
from textual.widgets import OptionList

# ------------------------------------------------------------- command palette


async def test_palette_agent_pick_is_collision_safe(tmp_path: Path):
    """Selecting an agent command must never invert into a same-named local command."""
    script = write_extension_agent(tmp_path)
    app, _target = make_app([sys.executable, str(script)])
    async with app.run_test(size=(120, 40)) as pilot:
        screen = await open_chat(pilot, app)
        await send_message(pilot, app, "plan the work")
        await wait_for(pilot, lambda: len(screen._commands) == 3, message="agent commands")
        await pilot.press("ctrl+k")
        await wait_for(pilot, lambda: any(isinstance(s, CommandPaletteDialog) for s in app.screen_stack))
        palette = next(s for s in app.screen_stack if isinstance(s, CommandPaletteDialog))
        option_list = palette.query_one("#command-list", OptionList)
        index = next(
            i
            for i in range(option_list.option_count)
            if option_list.get_option_at_index(i).id == "agent:help"
        )
        option_list.highlighted = index
        await pilot.press("enter")
        composer = screen.query_one("#composer", Composer)
        assert composer.text == "/agent help ", composer.text
        await pilot.press("ctrl+j")
        await wait_for(
            pilot,
            lambda: "ext reply: /help" in pane_text(screen.query_one("#transcript", TranscriptPane))
            and not screen.busy,
            message="agent-run help",
        )
        # The local HelpDialog must not have opened for the agent command.
        assert not any(isinstance(s, HelpDialog) for s in app.screen_stack)
    await app.shutdown_client()


async def test_slash_collision_runs_local_and_directs_to_agent_form(tmp_path: Path):
    script = write_extension_agent(tmp_path)
    app, _target = make_app([sys.executable, str(script)])
    async with app.run_test(size=(120, 40)) as pilot:
        screen = await open_chat(pilot, app)
        await send_message(pilot, app, "plan the work")
        await wait_for(pilot, lambda: len(screen._commands) == 3, message="agent commands")
        await send_message(pilot, app, "/help")
        await wait_for(pilot, lambda: any(isinstance(s, HelpDialog) for s in app.screen_stack))
        await pilot.press("escape")
        await wait_for(pilot, lambda: not any(isinstance(s, HelpDialog) for s in app.screen_stack))
        pane = screen.query_one("#transcript", TranscriptPane)
        await wait_for(
            pilot, lambda: "Use /agent help" in pane_text(pane), message="collision notice"
        )
    await app.shutdown_client()


# --------------------------------------------------------------- model command


async def test_model_command_without_options_shows_notice():
    """/model explains itself when the agent offers neither models nor modes."""
    app, _target = make_app([sys.executable, str(INDEPENDENT_AGENT)])
    async with app.run_test(size=(120, 40)) as pilot:
        screen = await open_chat(pilot, app)
        await send_message(pilot, app, "/model")
        pane = screen.query_one("#transcript", TranscriptPane)
        await wait_for(
            pilot, lambda: "neither a model config option" in pane_text(pane), message="model notice"
        )
        assert not any(type(s).__name__ == "ChoiceDialog" for s in app.screen_stack)
        assert not screen.busy
    await app.shutdown_client()


# ------------------------------------------------------------- replay guarding


async def test_prompt_is_blocked_while_session_replays():
    """A prompt sent during a load/replay is kept, not swallowed by the swap."""
    app, _target = make_app([sys.executable, str(INDEPENDENT_AGENT)])
    async with app.run_test(size=(120, 40)) as pilot:
        screen = await open_chat(pilot, app)
        composer = screen.query_one("#composer", Composer)
        pane = screen.query_one("#transcript", TranscriptPane)
        screen._loading_session = True
        composer.load_text("early prompt")
        await pilot.press("ctrl+j")
        await pilot.pause(0.2)
        await wait_for(
            pilot, lambda: "finish loading" in pane_text(pane), message="loading guard notice"
        )
        assert not screen.busy
        assert composer.text == "early prompt"
        screen._loading_session = False
        await pilot.press("ctrl+j")
        await wait_for(
            pilot,
            lambda: "Independent reply: early prompt" in pane_text(pane) and not screen.busy,
            message="retained prompt sent after load",
        )
    await app.shutdown_client()


# ---------------------------------------------------------------- bounded panes


async def test_transcript_bound_drops_oldest_and_prunes_tool_registry(monkeypatch: Any):
    monkeypatch.setattr(widgets, "UI_MAX_TRANSCRIPT_BLOCKS", 8)
    app, _target = make_app([sys.executable, str(INDEPENDENT_AGENT)])
    async with app.run_test(size=(120, 40)) as pilot:
        screen = await open_chat(pilot, app)
        pane = screen.query_one("#transcript", TranscriptPane)
        await pane.add_tool_snapshot({"toolCallId": "t-old", "title": "Old tool", "status": "completed"})
        for index in range(40):
            await pane.add_user(f"filler {index}")
        assert len(pane._blocks) <= 8
        assert "t-old" not in pane._tools, "evicted tool card kept registry entry"
    await app.shutdown_client()


async def test_read_only_extension_does_not_offer_edits(tmp_path: Path):
    source = EXTENSION_AGENT_SOURCE.replace(', "_harness/configuration/update"', "")
    script = tmp_path / "read_only_agent.py"
    script.write_text(source, encoding="utf-8")
    app, _target = make_app([sys.executable, str(script)])
    async with app.run_test(size=(120, 40)) as pilot:
        screen = await open_chat(pilot, app)
        panel = screen.query_one("#side-panel").capabilities
        await wait_for(pilot, lambda: panel._list.option_count == 3, message="read-only catalog")
        assert "read-only" in str(panel._status.render())
        assert panel.can_write is False
        await pilot.press("f2")
        await wait_for(pilot, lambda: panel._list.has_focus, message="panel focus")
        await pilot.press("enter")
        await wait_for(
            pilot,
            lambda: "read-only capability catalog" in str(panel._status.render()),
            message="read-only toggle guard",
        )
        assert panel.binding("web")["enabled"] is True
    await app.shutdown_client()


async def test_streaming_message_text_is_bounded():
    """A single streaming reply cannot grow unbounded; the trim stays visible."""
    app, _target = make_app([sys.executable, str(INDEPENDENT_AGENT)])
    async with app.run_test(size=(120, 40)) as pilot:
        await open_chat(pilot, app)
        limit = widgets.MAX_STREAM_MESSAGE_CHARS
        block = widgets.MessageBlock("agent", "")
        block.append_text("a" * (limit + 500))
        assert len(block._text) == limit
        # 500 characters overflow, plus one content character replaced by the "…" marker.
        assert block._dropped == 501
        assert block._text.startswith("…"), "trim origin must be marked"
        block.append_text("b" * 100)
        assert block._dropped == 601
        assert len(block._text) == limit
        block.flush()  # marker + capped text; must not raise

        thought = ReasoningBlock()
        thought.append_text("z" * (limit + 10))
        assert len(thought._text) <= limit
    thought.finish()


# ------------------------------------------------------------ session resume


async def test_sessions_panel_resume_switches_session(tmp_path: Path):
    script = write_extension_agent(tmp_path)
    app, _target = make_app([sys.executable, str(script)])
    async with app.run_test(size=(120, 40)) as pilot:
        screen = await open_chat(pilot, app)
        await send_message(pilot, app, "remember this")
        pane = screen.query_one("#transcript", TranscriptPane)
        await wait_for(
            pilot, lambda: "ext reply: remember this" in pane_text(pane) and not screen.busy
        )
        first_id = screen.session.session_id
        await pilot.press("ctrl+n")
        await wait_for(pilot, lambda: screen.session.session_id != first_id and not screen.busy)
        second_id = screen.session.session_id

        await pilot.press("f3")
        sessions = screen.query_one("#side-panel").sessions
        await wait_for(pilot, lambda: sessions._table.row_count == 2, message="session rows")
        await wait_for(pilot, lambda: "resume not advertised" not in str(sessions._status.render()))
        sessions._table.focus()
        sessions._table.move_cursor(row=0)
        await pilot.press("u")
        await wait_for(pilot, lambda: screen.session.session_id == first_id, message="resumed session")
        assert screen.session.session_id != second_id
        await wait_for(
            pilot, lambda: f"Loaded session {first_id}" in pane_text(pane), message="load notice"
        )
        status = str(screen.query_one("#statusbar").render())
        assert first_id in status
    await app.shutdown_client()


async def test_sessions_panel_resume_without_load_or_cache_is_explained(tmp_path: Path):
    source = EXTENSION_AGENT_SOURCE.replace(
        '{"list": {}, "delete": {}, "close": {}, "resume": {}}',
        '{"list": {}, "delete": {}, "close": {}}',
    ).replace('"loadSession": True,', '"loadSession": False,')
    assert '"resume": {}' not in source
    assert '"loadSession": True' not in source
    script = tmp_path / "no_load_or_resume_agent.py"
    script.write_text(source, encoding="utf-8")
    app, _target = make_app([sys.executable, str(script)])
    async with app.run_test(size=(120, 40)) as pilot:
        screen = await open_chat(pilot, app)
        await pilot.press("f3")
        sessions = screen.query_one("#side-panel").sessions
        await wait_for(pilot, lambda: sessions._table.row_count == 1, message="session row")
        await wait_for(
            pilot, lambda: "resume not advertised" in str(sessions._status.render()), message="status note"
        )
        sessions._table.focus()
        sessions._table.move_cursor(row=0)
        await pilot.press("u")
        pane = screen.query_one("#transcript", TranscriptPane)
        await pilot.pause(0.2)
        await wait_for(
            pilot,
            lambda: "no verified cache" in pane_text(pane),
            message="resume guard notice",
        )
        assert screen.busy is False
    await app.shutdown_client()


# ------------------------------------------------- global submit + focus keys


async def test_ctrl_t_focus_and_global_submit_work_away_from_composer():
    app, _target = make_app([sys.executable, str(INDEPENDENT_AGENT)])
    async with app.run_test(size=(120, 40)) as pilot:
        screen = await open_chat(pilot, app)
        pane = screen.query_one("#transcript", TranscriptPane)
        composer = screen.query_one("#composer", Composer)
        await pilot.press("ctrl+t")
        await pilot.pause(0.1)
        assert pane.has_focus
        await pilot.press("escape")
        await pilot.pause(0.1)
        assert composer.has_focus
        # From the transcript, Ctrl+Enter still submits the composer's text.
        await pilot.press("ctrl+t")
        composer.load_text("far send")
        await pilot.press("ctrl+enter")
        await wait_for(
            pilot,
            lambda: "Independent reply: far send" in pane_text(pane) and not screen.busy,
            message="submit from transcript",
        )
    await app.shutdown_client()


# ------------------------------------------------------------------- settings


async def test_settings_theme_choice_is_applied():
    app, _target = make_app([sys.executable, str(INDEPENDENT_AGENT)])
    async with app.run_test(size=(100, 30)) as pilot:
        screen = await open_chat(pilot, app)
        picked: list[Settings] = []

        def callback(value: Any) -> None:
            if value is not None:
                picked.append(value)

        from harness_client.tui.dialogs import SettingsDialog as _SD

        app.push_screen(SettingsDialog(screen.settings, can_persist=False), callback)
        await wait_for(pilot, lambda: any(isinstance(s, _SD) for s in app.screen_stack))
        dialog = next(s for s in app.screen_stack if isinstance(s, _SD))
        assert dialog._theme.value == screen.settings.theme
        dialog._theme.value = "textual-dark"
        await pilot.click("#settings-apply")
        await wait_for(pilot, lambda: bool(picked), message="settings applied")
        assert picked[0].auto_expand_tools == screen.settings.auto_expand_tools
        assert picked[0].theme == "textual-dark"
    await app.shutdown_client()
