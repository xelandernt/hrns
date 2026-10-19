"""Textual Pilot tests: streaming, dialogs, capabilities, sessions, recovery."""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any, Callable

from harness_client.launch import ConnectionTarget, local_cwd
from harness_client.profiles import Profile, Profiles
from harness_client.tui.app import HarnessApp
from harness_client.tui.dialogs import (
    ChoiceDialog,
    CommandPaletteDialog,
    ConfirmDialog,
    ElicitationDialog,
    FormDialog,
    HelpDialog,
    PermissionDialog,
)
from harness_client.tui.screens import ChatScreen, ConnectScreen
from harness_client.tui.widgets import MessageBlock, ToolCard, TranscriptPane
from test_client_support import INDEPENDENT_AGENT, write_extension_agent
from textual.widgets import Input, OptionList, Static


async def wait_for(
    pilot: Any,
    predicate: Callable[[], bool],
    *,
    timeout: float = 8.0,
    interval: float = 0.03,
    message: str = "condition",
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        await pilot.pause(interval)
        if predicate():
            return
    raise AssertionError(f"Timed out waiting for {message}")


def make_app(
    command: list[str],
    *,
    env: dict[str, str] | None = None,
    resize: tuple[int, int] = (120, 40),
) -> tuple[HarnessApp, ConnectionTarget]:
    profile = Profile(
        name="fixture",
        transport="stdio",
        command=command[0],
        args=command[1:],
        env=env or {},
    )
    profiles = Profiles({"fixture": profile})
    target = ConnectionTarget(
        name="fixture", kind="local", profile=profile, agent=None, cwd=local_cwd(profile)
    )
    return HarnessApp(profiles, initial_target=target), target


def pane_text(pane: TranscriptPane) -> str:
    parts: list[str] = []
    for child in pane.children:
        if isinstance(child, MessageBlock):
            parts.append(child._text)
        elif isinstance(child, ToolCard):
            parts.append(str(child.data.get("title", "")))
            parts.append(str(child.data.get("detail", "")))
        elif isinstance(child, Static):
            parts.append(str(child.render()))
        else:
            parts.append(str(getattr(child, "_text", "")))
    return "\n".join(parts)


async def open_chat(pilot: Any, app: HarnessApp) -> ChatScreen:
    await wait_for(pilot, lambda: isinstance(app.screen, ChatScreen), message="chat screen")
    await pilot.pause(0.4)
    return app.screen


async def send_message(pilot: Any, app: HarnessApp, text: str) -> None:
    screen = app.screen
    composer = screen.query_one("#composer")
    composer.load_text(text)
    await pilot.press("ctrl+j")
    await pilot.pause(0.05)


# ------------------------------------------------------------------ streaming


async def test_ui_streams_markdown_and_tool_cards():
    app, _target = make_app([sys.executable, str(INDEPENDENT_AGENT)])
    async with app.run_test(size=(120, 40)) as pilot:
        screen = await open_chat(pilot, app)
        await send_message(pilot, app, "tool hello")
        pane = screen.query_one("#transcript", TranscriptPane)
        await wait_for(
            pilot,
            lambda: not screen.busy and "Independent reply: tool hello" in pane_text(pane),
            message="streamed reply",
        )
        assert screen.query_one("#composer").text == ""
        assert any(isinstance(child, ToolCard) for child in pane.children)
        tool = next(child for child in pane.children if isinstance(child, ToolCard))
        assert tool.data["title"] == "Independent lookup"
        assert tool.data["status"] == "completed"
        assert "Lookup complete" in tool.data["detail"]
        topbar = str(screen.query_one("#topbar").render())
        assert "local" in topbar and "connected" in topbar
        status = str(screen.query_one("#statusbar").render())
        assert "independent-1" in status
        assert "working" not in status
    await app.shutdown_client()


async def test_ui_permission_dialog_requires_a_choice():
    app, _target = make_app([sys.executable, str(INDEPENDENT_AGENT)])
    async with app.run_test(size=(120, 40)) as pilot:
        screen = await open_chat(pilot, app)
        await send_message(pilot, app, "permission please")
        await wait_for(
            pilot, lambda: any(isinstance(s, PermissionDialog) for s in app.screen_stack)
        )
        dialog = next(s for s in app.screen_stack if isinstance(s, PermissionDialog))
        options = dialog.query_one("#permission-options", OptionList)
        labels = [
            options.get_option_at_index(index).prompt
            for index in range(options.option_count)
        ]
        assert any("Allow this operation" in str(label) for label in labels)
        assert any("Reject this operation" in str(label) for label in labels)
        await pilot.press("1")
        pane = screen.query_one("#transcript", TranscriptPane)
        await wait_for(
            pilot, lambda: "Approved" in pane_text(pane) and not screen.busy, message="approval"
        )
        assert not any(isinstance(s, PermissionDialog) for s in app.screen_stack)
    await app.shutdown_client()


async def test_ui_elicitation_form_returns_structured_answer(tmp_path: Path):
    script = write_extension_agent(tmp_path)
    app, _target = make_app([sys.executable, str(script)])
    async with app.run_test(size=(120, 40)) as pilot:
        screen = await open_chat(pilot, app)
        await send_message(pilot, app, "elicit now")
        await wait_for(
            pilot, lambda: any(isinstance(s, ElicitationDialog) for s in app.screen_stack)
        )
        dialog = next(s for s in app.screen_stack if isinstance(s, ElicitationDialog))
        inputs = list(dialog.query(Input))
        assert inputs, "form has inputs"
        inputs[0].value = "Ada"
        await pilot.click("#elicit-accept")
        pane = screen.query_one("#transcript", TranscriptPane)
        await wait_for(
            pilot,
            lambda: "elicited name=Ada count=2" in pane_text(pane) and not screen.busy,
            message="elicitation answer",
        )
    await app.shutdown_client()


# ---------------------------------------------------------------- capabilities


async def test_ui_capabilities_selected_active_and_policy(tmp_path: Path):
    script = write_extension_agent(tmp_path)
    app, _target = make_app([sys.executable, str(script)])
    async with app.run_test(size=(120, 40)) as pilot:
        screen = await open_chat(pilot, app)
        panel = screen.query_one("#side-panel").capabilities
        await wait_for(pilot, lambda: panel._list.option_count == 3, message="capability list")
        rows = [
            str(panel._list.get_option_at_index(index).prompt)
            for index in range(panel._list.option_count)
        ]
        assert rows[0].startswith("Web search") and "on" in rows[0]
        assert any("locked" in row for row in rows)

        await pilot.press("f2")
        await wait_for(pilot, lambda: panel._list.has_focus, message="panel focus")
        panel._list.highlighted = 0
        await pilot.press("enter")
        await wait_for(
            pilot,
            lambda: panel.binding("web") is not None and panel.binding("web")["enabled"] is False,
            message="capability toggle",
        )
        detail = panel._detail.render()
        assert "selected: off" in str(detail)
        assert "next turn" in str(detail)

        # Locked capability: the UI explains why instead of toggling.
        panel._list.highlighted = 2
        await pilot.press("enter")
        await wait_for(
            pilot, lambda: "locked" in str(panel._status.render()).lower(), message="locked reason"
        )

        # Edit fields through the schema-driven form.
        panel._list.highlighted = 0
        await pilot.press("e")
        await wait_for(pilot, lambda: any(isinstance(s, FormDialog) for s in app.screen_stack))
        form = next(s for s in app.screen_stack if isinstance(s, FormDialog))
        form_inputs = list(form.query(Input))
        assert form_inputs, "max_results input"
        form_inputs[0].value = "9"
        await pilot.click("#form-accept")
        await wait_for(
            pilot,
            lambda: panel.binding("web") is not None
            and panel.binding("web")["config"].get("max_results") == 9,
            message="configured capability",
        )
    await app.shutdown_client()


async def test_ui_adds_a_registered_local_plugin_binding(tmp_path: Path):
    script = write_extension_agent(tmp_path)
    app, _target = make_app([sys.executable, str(script)])
    async with app.run_test(size=(120, 40)) as pilot:
        screen = await open_chat(pilot, app)
        panel = screen.query_one("#side-panel").capabilities
        await wait_for(pilot, lambda: panel._list.option_count == 3, message="capability list")

        await pilot.press("f2")
        await wait_for(pilot, lambda: panel._list.has_focus, message="panel focus")
        await pilot.press("a")
        await wait_for(
            pilot,
            lambda: any(isinstance(s, ChoiceDialog) for s in app.screen_stack),
            message="plugin chooser",
        )
        await pilot.press("enter")
        await wait_for(
            pilot,
            lambda: any(isinstance(s, FormDialog) for s in app.screen_stack),
            message="add form",
        )
        form = next(s for s in app.screen_stack if isinstance(s, FormDialog))
        binding_input = next(
            widget for widget in form.query(Input) if widget.id != "form-cancel"
        )
        binding_input.value = "custom-one"
        await pilot.click("#form-accept")
        await wait_for(
            pilot,
            lambda: panel.binding("custom-one") is not None,
            message="added capability",
        )
        assert panel.binding("custom-one")["pluginId"] == "example.custom"
        assert panel.binding("custom-one")["enabled"] is True
    await app.shutdown_client()


async def test_ui_standard_configuration_when_extension_absent(tmp_path: Path):
    script = write_extension_agent(tmp_path)
    app, _target = make_app([sys.executable, str(script)], env={"FIXTURE_EXTENSION": "0"})
    async with app.run_test(size=(120, 40)) as pilot:
        screen = await open_chat(pilot, app)
        panel = screen.query_one("#side-panel").capabilities
        await wait_for(pilot, lambda: panel._list.option_count == 2, message="standard options")
        assert not panel.extension_mode
        assert "does not advertise the richer capability extension" in str(
            panel._status.render()
        )
        rows = [
            str(panel._list.get_option_at_index(index).prompt)
            for index in range(panel._list.option_count)
        ]
        assert any(row.startswith("Model: Test Fast") for row in rows)
        await pilot.press("f2")
        panel._list.highlighted = 0
        await pilot.press("enter")
        await wait_for(pilot, lambda: any(isinstance(s, ChoiceDialog) for s in app.screen_stack))
        choice = next(s for s in app.screen_stack if isinstance(s, ChoiceDialog))
        options = choice.query_one("#choice-list", OptionList)
        options.highlighted = 1
        await pilot.press("enter")
        await wait_for(
            pilot,
            lambda: next(
                (item for item in screen.session.config_options if item["id"] == "model"), {}
            ).get("currentValue")
            == "test-deep",
            message="model change",
        )
        panel._list.highlighted = 1
        await pilot.press("enter")
        await wait_for(
            pilot,
            lambda: next(
                (item for item in screen.session.config_options if item["id"] == "stream"), {}
            ).get("currentValue")
            is False,
            message="boolean toggle",
        )
    await app.shutdown_client()


# --------------------------------------------------------------------- sessions


async def test_ui_sessions_load_replay_and_delete(tmp_path: Path):
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
        await wait_for(
            pilot, lambda: screen.session.session_id != first_id and not screen.busy
        )
        second_id = screen.session.session_id
        await wait_for(pilot, lambda: "ext reply" not in pane_text(pane), message="cleared")

        await pilot.press("f3")
        sessions = screen.query_one("#side-panel").sessions
        await wait_for(pilot, lambda: sessions._table.row_count == 2, message="session rows")
        sessions._table.focus()
        sessions._table.move_cursor(row=0)
        await pilot.press("enter")
        await wait_for(
            pilot,
            lambda: screen.session.session_id == first_id
            and "ext reply: remember this" in pane_text(pane),
            message="session replay",
        )

        await pilot.press("f3")
        await wait_for(pilot, lambda: sessions._table.row_count == 2)
        sessions._table.focus()
        sessions._table.move_cursor(row=1)
        await pilot.press("d")
        await wait_for(pilot, lambda: any(isinstance(s, ConfirmDialog) for s in app.screen_stack))
        await pilot.press("y")
        await wait_for(pilot, lambda: sessions._table.row_count == 1, message="deleted session")
        assert screen.session.session_id != second_id
    await app.shutdown_client()


# ----------------------------------------------------------- commands & layout


async def test_ui_command_palette_separates_local_and_agent_commands(tmp_path: Path):
    script = write_extension_agent(tmp_path)
    app, _target = make_app([sys.executable, str(script)])
    async with app.run_test(size=(120, 40)) as pilot:
        screen = await open_chat(pilot, app)
        await send_message(pilot, app, "plan the work")
        pane = screen.query_one("#transcript", TranscriptPane)
        await wait_for(
            pilot, lambda: "ext reply: plan the work" in pane_text(pane) and not screen.busy
        )
        await wait_for(pilot, lambda: len(screen._commands) == 3, message="agent commands")
        await pilot.press("ctrl+k")
        await wait_for(
            pilot, lambda: any(isinstance(s, CommandPaletteDialog) for s in app.screen_stack)
        )
        palette = next(s for s in app.screen_stack if isinstance(s, CommandPaletteDialog))
        option_list = palette.query_one("#command-list", OptionList)
        labels = [
            str(option_list.get_option_at_index(index).prompt)
            for index in range(option_list.option_count)
        ]
        assert any("Local commands" in label for label in labels)
        assert any("Agent commands" in label for label in labels)
        assert any("research" in label for label in labels)
        assert any("use /agent NAME on collision" in label for label in labels)
        await pilot.press("escape")
        await wait_for(
            pilot, lambda: not any(isinstance(s, CommandPaletteDialog) for s in app.screen_stack)
        )

        # Local /help wins over the colliding agent command.
        await send_message(pilot, app, "/help")
        await wait_for(pilot, lambda: any(isinstance(s, HelpDialog) for s in app.screen_stack))
        await pilot.press("escape")
        await wait_for(
            pilot, lambda: not any(isinstance(s, HelpDialog) for s in app.screen_stack)
        )

        await send_message(pilot, app, "/agent research topic")
        await wait_for(
            pilot,
            lambda: "ext reply: /research topic" in pane_text(pane) and not screen.busy,
            message="agent command",
        )
    await app.shutdown_client()


async def test_ui_narrow_terminal_collapses_and_toggles_panel():
    app, _target = make_app([sys.executable, str(INDEPENDENT_AGENT)])
    async with app.run_test(size=(72, 26)) as pilot:
        screen = await open_chat(pilot, app)
        panel = screen.query_one("#side-panel")
        assert panel.display is False
        assert screen.has_class("narrow")
        await pilot.resize_terminal(120, 40)
        await wait_for(pilot, lambda: panel.display is True, message="wide panel")
        await pilot.press("ctrl+b")
        await wait_for(pilot, lambda: panel.display is False, message="manual hide")
        await pilot.press("ctrl+b")
        await wait_for(pilot, lambda: panel.display is True)
        await pilot.resize_terminal(60, 24)
        await wait_for(pilot, lambda: screen.has_class("narrow"), message="narrow class")
    await app.shutdown_client()


async def test_ui_picker_lists_profiles_and_filters():
    profile = Profile(name="fixture", transport="stdio", command=sys.executable, args=[str(INDEPENDENT_AGENT)])
    profiles = Profiles({"fixture": profile})
    app = HarnessApp(profiles)
    async with app.run_test(size=(110, 36)) as pilot:
        await wait_for(pilot, lambda: isinstance(app.screen, ConnectScreen))
        picker = app.screen
        assert picker._list.index == 0
        assert any(entry.kind == "local" and entry.profile.name == "fixture" for entry in picker.entries)
        await pilot.press("slash")
        picker._filter.value = "zzz"
        await pilot.pause(0.1)
        assert len(picker._visible) == 0
        picker._filter.value = "fixture"
        await pilot.pause(0.1)
        assert len(picker._visible) == 1
        await pilot.press("enter")
        await wait_for(pilot, lambda: isinstance(app.screen, ChatScreen), message="connect from picker")
    await app.shutdown_client()


async def test_ui_stages_explicit_attachment_then_sends_content(tmp_path):
    context = tmp_path / "context.md"
    context.write_text("# Local context\n\nBounded content.", encoding="utf-8")
    app, _target = make_app([sys.executable, str(INDEPENDENT_AGENT)])
    async with app.run_test(size=(120, 40)) as pilot:
        screen = await open_chat(pilot, app)
        await send_message(pilot, app, f"/attach {context}")
        await wait_for(
            pilot,
            lambda: screen._attachments == [str(context.resolve())],
            message="attachment staged",
        )
        pane = screen.query_one("#transcript", TranscriptPane)
        assert "Staged context.md" in pane_text(pane)

        await send_message(pilot, app, "use the attached context")
        await wait_for(
            pilot,
            lambda: not screen.busy and "Independent reply: use the attached context" in pane_text(pane),
            message="prompt with attachment",
        )
        assert screen._attachments == []
        assert "attached: context.md" in pane_text(pane)

        # A directory is never silently treated as a file attachment.
        await send_message(pilot, app, f"/attach {tmp_path}")
        await wait_for(
            pilot,
            lambda: "only regular files" in pane_text(pane),
            message="directory rejection",
        )
    await app.shutdown_client()


async def test_ui_does_not_stage_attachment_without_embedded_context(tmp_path: Path):
    context = tmp_path / "context.txt"
    context.write_text("not supported by this peer")
    script = write_extension_agent(tmp_path)
    app, _target = make_app([sys.executable, str(script)])
    async with app.run_test(size=(120, 40)) as pilot:
        screen = await open_chat(pilot, app)
        pane = screen.query_one("#transcript", TranscriptPane)
        await send_message(pilot, app, f"/attach {context}")
        await wait_for(
            pilot,
            lambda: "did not advertise embedded-context" in pane_text(pane),
            message="attachment capability rejection",
        )
        assert screen._attachments == []
    await app.shutdown_client()


async def test_ui_disconnect_is_recoverable():
    app, _target = make_app([sys.executable, str(INDEPENDENT_AGENT)])
    async with app.run_test(size=(120, 40)) as pilot:
        screen = await open_chat(pilot, app)
        await send_message(pilot, app, "disconnect")
        pane = screen.query_one("#transcript", TranscriptPane)
        await wait_for(pilot, lambda: screen.connected is False, message="connection lost")
        assert "Connection lost" in pane_text(pane)
        assert isinstance(app.screen, ChatScreen)
        await pilot.press("ctrl+r")
        await wait_for(pilot, lambda: screen.connected is True, message="reconnect")
        status = str(screen.query_one("#statusbar").render())
        assert "connected" in status
    await app.shutdown_client()
