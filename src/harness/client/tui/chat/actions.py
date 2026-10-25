"""Panel, help, palette, settings, and transcript-search actions."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from ...interactions import PERMISSION_MODES
from ..commands import LOCAL_COMMANDS, resolve_local_command
from ..composer import Composer
from ..dialogs import (
    ChoiceDialog,
    CommandPaletteDialog,
    HelpDialog,
    SettingsDialog,
    TranscriptSearchDialog,
)
from ..panels import SidePanel
from ..state import Settings
from ..transcript import TranscriptPane

if TYPE_CHECKING:
    from .screen import ChatScreen


class ChatActions:
    """Screen actions that open panels or dialogs rather than drive a turn."""

    def __init__(self, screen: ChatScreen) -> None:
        self.screen = screen

    def show_capabilities(self) -> None:
        self.show_panel("capabilities")

    def show_todos(self) -> None:
        self.show_panel("todos")

    def show_mcp(self) -> None:
        screen = self.screen
        self.show_panel("mcp")
        screen._refresh_mcp()

    def show_sessions(self) -> None:
        screen = self.screen
        self.show_panel("sessions")
        screen.refresh_sessions()

    def show_activity(self) -> None:
        self.show_panel("activity")

    def show_panel(self, tab: str, *, focus: bool = True) -> None:
        screen = self.screen
        panel = screen.query_one("#side-panel", SidePanel)
        screen._panel_open = True
        panel.display = True
        # TabbedContent resolves its private ContentTabs by immediate-child
        # lookup. Assigning ``active`` before compose has mounted it raises
        # NoMatches, so wait until the show/refresh cycle has run.
        if focus:
            screen.call_after_refresh(panel.activate, tab)
        else:
            screen.call_after_refresh(panel.select, tab)

    def close_panel(self) -> None:
        screen = self.screen
        panel = screen.query_one("#side-panel", SidePanel)
        screen._panel_open = False
        panel.display = False
        screen.query_one("#composer", Composer).focus()

    def toggle_panel(self) -> None:
        screen = self.screen
        if screen._panel_open:
            self.close_panel()
            return
        screen._panel_open = True
        screen.query_one("#side-panel", SidePanel).display = True

    def command_palette(self) -> None:
        screen = self.screen
        agent_commands = list(screen._commands)
        local_names = {
            name for command in LOCAL_COMMANDS for name in (command.name, *command.aliases)
        }
        agent_names = {str(command.get("name", "")) for command in agent_commands}
        collisions = local_names & agent_names

        def apply(result: str | None) -> None:
            if not result:
                return
            kind, _, name = result.partition(":")
            if kind == "local":
                command = resolve_local_command(name)
                composer = screen.query_one("#composer", Composer)
                if command is not None and command.takes_argument:
                    composer.load_text(f"/{command.name} ")
                    composer.focus()
                elif command is not None:
                    screen._dispatch_local_command(command.name)
            elif kind == "plugin":
                descriptor = screen.commands.find_plugin_command(name)
                if descriptor is not None:
                    composer = screen.query_one("#composer", Composer)
                    takes_arguments = bool(
                        descriptor.get("takesArguments") or descriptor.get("takes_arguments")
                    )
                    if takes_arguments:
                        composer.load_text(f"/{name} ")
                        composer.focus()
                    else:
                        screen._dispatch_plugin_command(
                            str(descriptor.get("bindingId") or descriptor.get("binding_id")),
                            str(descriptor.get("commandName") or descriptor.get("command_name")),
                        )
            elif kind == "agent":
                composer = screen.query_one("#composer", Composer)
                # Always route through /agent so collisions cannot hijack the
                # pick into a local command with the same name.
                composer.load_text(f"/agent {name} ")
                composer.focus()

        plugin_entries = [
            {
                "display": screen.commands.plugin_command_display(command),
                "description": str(command.get("description", "")),
            }
            for command in screen._plugin_commands
        ]
        screen.app.push_screen(
            CommandPaletteDialog(
                LOCAL_COMMANDS,
                agent_commands,
                collision_names=collisions,
                plugin_commands=plugin_entries,
            ),
            apply,
        )

    def help(self) -> None:
        screen = self.screen
        agent_lines = (
            "\n".join(
                f"- `/{command.get('name')}` — {command.get('description', '')}"
                for command in screen._commands
            )
            or "No agent commands advertised."
        )
        local_lines = []
        for command in LOCAL_COMMANDS:
            if command.hidden:
                continue
            aliases = (
                " (aliases: " + ", ".join(f"/{alias}" for alias in command.aliases) + ")"
                if command.aliases
                else ""
            )
            local_lines.append(f"- `/{command.name}` — {command.description}{aliases}")
        plugin_lines = [
            f"- `/{screen.commands.plugin_command_display(command)}` — {command.get('description', '')}"
            for command in screen._plugin_commands
        ]
        plugin_section = (
            "\n## Plugin commands\n\n" + "\n".join(plugin_lines) if plugin_lines else ""
        )
        body = f"""# Harness terminal

## Keys

| key | action |
| --- | --- |
| `Ctrl+Enter` | send the prompt (works from any panel or the transcript) |
| `Ctrl+K` | command palette (local and agent sections) |
| `F1` | this help |
| `F2` | plugins panel |
| `F3` | sessions panel |
| `F4` | activity panel |
| `Tab` / `Shift+Tab` inside the side panel | switch between plugins, sessions and activity |
| `Esc` inside the side panel | close it and return to the composer |
| `Up` / `Down` or `Tab` in a form | move between fields |
| `Left` / `Right` in a form | change a choice or toggle a switch |
| `Enter` in a form | save and close |
| `Esc` in a form | cancel without saving |
| `Ctrl+T` | focus the transcript for scrolling; `Esc` returns to the composer |
| `Ctrl+F` | find text in retained messages, tools, and reasoning |
| `Alt+Up` / `Alt+Down` | jump to the previous / next user message |
| `Ctrl+End` | jump to latest output and resume following |
| `Up` / `Down` at editor boundaries | recall prompts and restore the unfinished draft |
| `Esc` (idle) | focus the composer |
| `Ctrl+B` | show or hide the side panel |
| `Ctrl+N` | new session |
| `Ctrl+R` | reconnect and reload the session |
| `Esc` | stop the running turn |
| `Ctrl+Q` | quit |

## Local commands

{chr(10).join(local_lines)}
{plugin_section}

Type `@` to find and attach a local workspace file; select a staged file to remove it.
Submitting during a running turn steers the agent: the text reaches it at its next model
request instead of waiting for the turn to end. Prompts with attachments, and peers without
the steering extension, queue locally instead; use the queue's Edit and Remove controls to
revise those. Cancellation or errors pause the queue.
Drafts, prompt history, attachments, and queues stay with their session while this app is open.
Use `/` in the sessions panel to filter titles and workspaces. `Esc` closes the panel.

## Agent commands

{agent_lines}

Agent commands are invoked as `/<name>` or `/agent <name>` on collisions. The client never
invents commands the agent did not advertise.
"""
        screen.app.push_screen(HelpDialog(body))

    def show_permissions(self) -> None:
        screen = self.screen
        labels = {
            "ask": "Ask every time",
            "reads": "Allow reads, ask for the rest",
            "all": "Allow all tools",
        }
        descriptions = {
            "ask": "Show the approval bar for every tool request.",
            "reads": "Approve read, search and think calls automatically; ask before writes, commands and network access.",
            "all": "Approve every call whose agent offered an allow option. This is not a sandbox.",
        }
        choices = [
            {"value": mode, "name": labels[mode], "description": descriptions[mode]}
            for mode in PERMISSION_MODES
        ]

        def apply(mode: str | None) -> None:
            if mode not in PERMISSION_MODES:
                return
            screen.settings = replace(screen.settings, permission_mode=mode)
            screen.app.apply_settings(screen.settings)
            screen.chrome.apply_settings()
            screen._notice_soon(f"Approvals: {labels[mode]}.", "success")

        screen.app.push_screen(
            ChoiceDialog(
                "Tool approvals",
                "How should this client answer the agent's tool requests?",
                choices,
                current=screen.settings.permission_mode,
            ),
            apply,
        )

    def show_settings(self) -> None:
        screen = self.screen

        def apply(settings: Settings | None) -> None:
            if settings is not None:
                screen.settings = settings
                screen.app.apply_settings(settings)
                screen.chrome.apply_settings()

        screen.app.push_screen(
            SettingsDialog(
                screen.settings,
                can_persist=bool(screen.app.profiles.sources),
            ),
            apply,
        )

    def find_transcript_action(self) -> None:
        screen = self.screen
        screen._find_transcript()

    def find_transcript(self, query: str = "") -> None:
        screen = self.screen
        pane = screen.query_one("#transcript", TranscriptPane)
        entries = pane.search_entries()

        def reveal(index: int | None) -> None:
            if index is not None and entries[index][0].is_mounted:
                pane.reveal(entries[index][0])

        screen.app.push_screen(
            TranscriptSearchDialog([(role, text) for _, role, text in entries], query),
            reveal,
        )
