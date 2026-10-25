"""Prompt submission, queue, drafts, attachments, and suggestions."""

from __future__ import annotations

import asyncio
import contextlib
import re
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ...attachments import attachment_summary, build_prompt_blocks, validate_attachment
from ...errors import ExtensionUnavailable, HarnessClientError
from ..commands import LOCAL_COMMANDS, command_suggestion_text
from ..completion import FileIndex, index_workspace, matching_skills
from ..composer import AttachmentBar, CommandSuggestions, Composer, PromptQueue
from ..transcript import TranscriptPane

if TYPE_CHECKING:
    from ..screens import ChatScreen


class PromptWorkflow:
    """Owns everything between the composer and a running turn."""

    def __init__(self, screen: ChatScreen) -> None:
        self.screen = screen

    # ----------------------------------------------------------- suggestions

    def text_changed(self, event: Any) -> None:
        screen = self.screen
        composer = screen.query_one("#composer", Composer)
        if getattr(event, "text_area", None) is composer:
            self.sync_command_suggestions()

    def selection_changed(self, event: Any) -> None:
        screen = self.screen
        composer = screen.query_one("#composer", Composer)
        if getattr(event, "text_area", None) is composer and composer.has_focus:
            self.sync_command_suggestions()

    async def index_workspace(self) -> None:
        screen = self.screen
        screen._workspace_files, screen._file_index = await asyncio.to_thread(
            index_workspace, screen._workspace_root
        )
        self.sync_mention_catalog()
        self.sync_command_suggestions()

    def sync_mention_catalog(self) -> None:
        """Tell the composer which ``$skill`` / ``@path`` references are real."""
        screen = self.screen
        with contextlib.suppress(Exception):
            screen.query_one("#composer", Composer).set_mention_catalog(
                skills=[
                    str(skill.get("name", ""))
                    for skill in screen._skills
                    if bool(skill.get("user_invocable", True))
                ],
                files=screen._workspace_files,
            )

    def sync_skill_suggestions(self) -> bool:
        screen = self.screen
        composer = screen.query_one("#composer", Composer)
        row, column = composer.cursor_location
        line = composer.document.get_line(row)[:column]
        match = re.search(r"(?<!\S)\$([A-Za-z0-9-]*)$", line)
        if match is None or not composer.has_focus:
            return False
        items = [
            {
                "kind": "skill",
                "name": str(skill.get("name", "")),
                "label": (
                    f"${skill.get('name', '')}   {str(skill.get('description', ''))[:80]}"
                ).rstrip(),
                "start": (row, match.start()),
                "end": (row, column),
            }
            for skill in matching_skills(
                (skill for skill in screen._skills if bool(skill.get("user_invocable", True))),
                match.group(1),
            )
            if skill.get("name")
        ]
        if not items:
            if match.group(1):
                self.hide_command_suggestions()
            return False
        signature = tuple(item["name"] for item in items)
        if signature != screen._suggestion_signature:
            screen._suggestion_index = 0
            screen._suggestion_signature = signature
        screen._suggestion_items = items
        self.render_command_suggestions()
        composer.suggestions_open = True
        return True

    def sync_file_suggestions(self) -> None:
        screen = self.screen
        composer = screen.query_one("#composer", Composer)
        row, column = composer.cursor_location
        line = composer.document.get_line(row)[:column]
        match = re.search(r"(?<!\S)@([^\s@]*)$", line)
        if match is None or not composer.has_focus:
            self.hide_command_suggestions()
            return
        items = [
            {
                "kind": "file",
                "name": path,
                "label": f"@{path}",
                "start": (row, match.start()),
                "end": (row, column),
            }
            for path in screen._file_index.matching(match.group(1))
        ]
        if not items:
            self.hide_command_suggestions()
            return
        signature = tuple(item["name"] for item in items)
        if signature != screen._suggestion_signature:
            screen._suggestion_index = 0
            screen._suggestion_signature = signature
        screen._suggestion_items = items
        self.render_command_suggestions()
        composer.suggestions_open = True

    def sync_command_suggestions(self) -> None:
        screen = self.screen
        composer = screen.query_one("#composer", Composer)
        raw = composer.text
        if not raw.startswith("/") or "\n" in raw:
            if not self.sync_skill_suggestions():
                self.sync_file_suggestions()
            return
        body = raw[1:]
        if " " in body or "\t" in body:
            self.hide_command_suggestions()
            return
        needle = body.strip().lower()
        local = [
            command for command in LOCAL_COMMANDS if not command.hidden and command.matches(needle)
        ]
        if needle:
            local.sort(
                key=lambda command: (
                    0
                    if needle
                    in {command.name.lower(), *(alias.lower() for alias in command.aliases)}
                    else 1
                )
            )
        local_names = {
            name for command in LOCAL_COMMANDS for name in (command.name, *command.aliases)
        }
        agent = [
            command
            for command in screen._commands
            if needle in str(command.get("name", "")).lower()
            or needle in str(command.get("description", "")).lower()
        ]
        if needle:
            agent.sort(
                key=lambda command: 0 if needle == str(command.get("name", "")).lower() else 1
            )
        plugins = []
        for command in screen._plugin_commands:
            display = screen.commands.plugin_command_display(command)
            if (
                not needle
                or needle in display.lower()
                or needle in str(command.get("description", "")).lower()
            ):
                plugins.append(command)
        if needle:
            plugins.sort(
                key=lambda command: (
                    0 if needle == screen.commands.plugin_command_display(command).lower() else 1
                )
            )

        items: list[dict[str, Any]] = []
        for command in local:
            items.append(
                {
                    "kind": "local",
                    "name": command.name,
                    "label": command_suggestion_text(command),
                    "command": command,
                }
            )
        for command in agent[:100]:
            name = str(command.get("name", ""))
            if not name:
                continue
            description = str(command.get("description", ""))
            collision = name in local_names
            label = f"/{name}   {description}".rstrip()
            if collision:
                label += "   (agent command; use /agent)"
            items.append(
                {
                    "kind": "agent",
                    "name": name,
                    "label": label,
                    "collision": collision,
                }
            )
        for command in plugins[:100]:
            display = screen.commands.plugin_command_display(command)
            description = str(command.get("description", ""))
            hint = str(command.get("argumentsHint") or command.get("arguments_hint") or "")
            label = f"/{display}   {description}".rstrip()
            if hint:
                label += f"   ({hint})"
            items.append(
                {
                    "kind": "plugin",
                    "name": display,
                    "bindingId": command.get("bindingId") or command.get("binding_id"),
                    "commandName": command.get("commandName") or command.get("command_name"),
                    "label": label,
                }
            )
        if not items:
            self.hide_command_suggestions()
            return

        signature = tuple(f"{item['kind']}:{item['name']}" for item in items)
        if signature != screen._suggestion_signature:
            screen._suggestion_index = 0
            screen._suggestion_signature = signature
        else:
            screen._suggestion_index = min(screen._suggestion_index, len(items) - 1)
        screen._suggestion_items = items
        self.render_command_suggestions()
        composer.suggestions_open = True

    def render_command_suggestions(self) -> None:
        screen = self.screen
        widget = screen.query_one("#command-suggestions", CommandSuggestions)
        entries = [
            (str(item["label"]), f"{item['kind']}:{item['name']}")
            for item in screen._suggestion_items
        ]
        widget.show_entries(entries, screen._suggestion_index)
        if entries:
            widget.highlighted = screen._suggestion_index

    def hide_command_suggestions(self) -> None:
        screen = self.screen
        screen._suggestion_items = []
        screen._suggestion_signature = ()
        screen._suggestion_index = 0
        with contextlib.suppress(Exception):
            screen.query_one("#command-suggestions", CommandSuggestions).hide_entries()
        with contextlib.suppress(Exception):
            screen.query_one("#composer", Composer).suggestions_open = False

    def handle_suggestion_key(self, key: str) -> None:
        screen = self.screen
        if not screen._suggestion_items:
            return
        if key == "escape":
            self.hide_command_suggestions()
            return
        if key in {"up", "down"}:
            step = -1 if key == "up" else 1
            screen._suggestion_index = (screen._suggestion_index + step) % len(
                screen._suggestion_items
            )
            self.render_command_suggestions()
            return
        if key in {"enter", "tab"}:
            self.accept_command_suggestion(complete_only=key == "tab")

    def accept_command_suggestion(self, *, complete_only: bool) -> None:
        screen = self.screen
        if not (0 <= screen._suggestion_index < len(screen._suggestion_items)):
            return
        item = screen._suggestion_items[screen._suggestion_index]
        composer = screen.query_one("#composer", Composer)
        self.hide_command_suggestions()
        if item["kind"] == "file":
            screen._complete_file(item)
            return
        if item["kind"] == "skill":
            composer.replace(f"${item['name']} ", item["start"], item["end"])
            composer.focus()
            return
        if item["kind"] == "local":
            command = item["command"]
            if complete_only or command.takes_argument:
                composer.load_text(f"/{command.name} ")
                composer.focus()
                return
            composer.clear_composer()
            composer.focus()
            asyncio.create_task(screen.commands.run_local_command(command.name))
            return
        if item["kind"] == "plugin":
            descriptor = next(
                (
                    command
                    for command in screen._plugin_commands
                    if screen.commands.plugin_command_display(command) == item["name"]
                ),
                {},
            )
            takes_arguments = bool(
                descriptor.get("takesArguments", descriptor.get("takes_arguments"))
            )
            if complete_only or takes_arguments:
                composer.load_text(f"/{item['name']} ")
                composer.focus()
                return
            composer.clear_composer()
            composer.focus()
            asyncio.create_task(
                screen.commands.run_plugin_command(
                    str(item["bindingId"]), str(item["commandName"]), ""
                )
            )
            return
        prefix = "/agent " if item.get("collision") else "/"
        composer.load_text(f"{prefix}{item['name']} ")
        composer.focus()

    async def complete_file(self, item: dict[str, Any]) -> None:
        screen = self.screen
        path = str(screen._workspace_root / item["name"])
        if await self.stage_attachment(path):
            composer = screen.query_one("#composer", Composer)
            composer.replace(f"@{item['name']} ", item["start"], item["end"])
            composer.focus()

    # ------------------------------------------------------------- submission

    async def submitted(self, text: str) -> None:
        screen = self.screen
        self.hide_command_suggestions()
        text = text.strip()
        if not text:
            return
        if text.startswith("/"):
            await screen.commands.handle_command(text)
            return
        await self.start_prompt(text)

    async def start_prompt(
        self, text: str, *, attachments: list[str] | None = None, from_queue: bool = False
    ) -> bool:
        screen = self.screen
        if not screen.connected:
            await screen._notice("Not connected. Press Ctrl+R to reconnect.", "error")
            return False
        if screen.session.client is not screen.client:
            await screen._notice(
                "This session is not attached to the current connection. "
                "Press Ctrl+N for a new session or reload it after reconnecting.",
                "warning",
            )
            screen.query_one("#composer", Composer).focus()
            return False
        if screen._loading_session or screen._replay_target is not None:
            await screen._notice(
                "Wait for the session to finish loading; the prompt was kept in the composer.",
                "warning",
            )
            screen.query_one("#composer", Composer).focus()
            return False
        if screen.busy:
            if not screen._attachments and await self.steer(text):
                return True
            if len(screen._queued_prompts) >= 20:
                await screen._notice(
                    "The queue is full (20 follow-ups). Edit or remove an item.", "warning"
                )
                return False
            screen._queued_prompts.append((text, list(screen._attachments)))
            self.remember_prompt(text)
            screen._attachments.clear()
            screen.query_one("#composer", Composer).clear_composer()
            self.refresh_queue()
            return True
        attachments = list(screen._attachments) if attachments is None else attachments
        try:
            blocks = build_prompt_blocks(text, attachments) if attachments else None
        except HarnessClientError as exc:
            await screen._notice(exc.message, "error")
            if not from_queue:
                screen.query_one("#composer", Composer).load_text(text)
            return False
        composer = screen.query_one("#composer", Composer)
        if not from_queue:
            self.remember_prompt(text)
            composer.clear_composer()
        pane = screen.query_one("#transcript", TranscriptPane)
        await pane.finish_message()
        display = text
        if attachments:
            names = ", ".join(item["name"] for item in attachment_summary(attachments))
            display = f"{text}\n\nattached: {names}"
        await pane.add_user(display)
        if not from_queue:
            screen._attachments.clear()
            self.refresh_queue()
        screen._set_busy(True)
        screen.app.start_turn(screen, text, blocks)
        return True

    def remember_prompt(self, text: str) -> None:
        screen = self.screen
        history = screen._prompt_history.setdefault(screen.session.session_id, [])
        if not history or history[-1] != text:
            history.append(text)
            del history[:-100]
        screen.query_one("#composer", Composer).prompt_history = history

    async def steer(self, text: str) -> bool:
        """Deliver a follow-up into the running turn instead of waiting for it to end.

        The host injects it before its next model request, so the agent can act on it
        after the tool call it is currently running. Peers without the extension, or a
        turn that just finished, fall back to the local queue.
        """
        screen = self.screen
        if not screen.client.extension.supports("_harness/session/steer"):
            return False
        try:
            if not await screen.session.steer(text):
                return False
        except HarnessClientError:
            return False
        pane = screen.query_one("#transcript", TranscriptPane)
        await pane.finish_message()
        await pane.add_user(text)
        self.remember_prompt(text)
        screen.query_one("#composer", Composer).clear_composer()
        return True

    # ---------------------------------------------------------------- drafts

    def save_draft(self) -> None:
        screen = self.screen
        screen._drafts[screen.session.session_id] = (
            screen.query_one("#composer", Composer).text,
            list(screen._attachments),
            list(screen._queued_prompts),
        )

    def restore_draft(self) -> None:
        screen = self.screen
        if screen.target.location().get("kind") == "local":
            root = Path(screen.session.cwd).resolve()
            if root.is_dir() and root != screen._workspace_root:
                screen._workspace_root = root
                screen._workspace_files = []
                screen._file_index = FileIndex(())
                screen._index_workspace()
        text, attachments, queue = screen._drafts.get(screen.session.session_id, ("", [], []))
        composer = screen.query_one("#composer", Composer)
        composer.clear_composer()
        composer.load_text(text)
        composer.prompt_history = screen._prompt_history.setdefault(screen.session.session_id, [])
        screen._attachments = list(attachments)
        screen._queued_prompts = list(queue)
        screen._queue_paused = bool(queue)
        self.refresh_queue()

    def refresh_queue(self) -> None:
        screen = self.screen
        screen.query_one(PromptQueue).show_prompts(
            screen._queued_prompts, paused=screen._queue_paused
        )
        screen.query_one(AttachmentBar).show_files(attachment_summary(screen._attachments))

    def attachment_removed(self, index: int) -> None:
        screen = self.screen
        if 0 <= index < len(screen._attachments):
            screen._attachments.pop(index)
            self.refresh_queue()
            screen.query_one("#composer", Composer).focus()

    async def queue_action(self, action: str, index: int) -> None:
        screen = self.screen
        if action == "resume":
            screen._queue_paused = False
            self.refresh_queue()
            await self.send_next_queued()
        elif 0 <= index < len(screen._queued_prompts):
            if action == "edit":
                composer = screen.query_one("#composer", Composer)
                if composer.text.strip() or screen._attachments:
                    await screen._notice(
                        "Send or clear the current draft before editing a queued message.",
                        "warning",
                    )
                    return
                text, screen._attachments = screen._queued_prompts.pop(index)
                composer.load_text(text)
                composer.focus()
            elif action == "remove":
                screen._queued_prompts.pop(index)
            self.refresh_queue()

    async def send_next_queued(self) -> None:
        screen = self.screen
        if (
            screen not in screen.app.screen_stack
            or screen.busy
            or screen._queue_paused
            or not screen._queued_prompts
            or not screen.connected
            or screen._loading_session
        ):
            return
        text, attachments = screen._queued_prompts[0]
        if await self.start_prompt(text, attachments=attachments, from_queue=True):
            screen._queued_prompts.pop(0)
        else:
            screen._queue_paused = True
        self.refresh_queue()

    # ---------------------------------------------------------------- skills

    async def show_skills(self, query: str = "") -> None:
        screen = self.screen
        await self.refresh_skills()
        available = [skill for skill in screen._skills if bool(skill.get("user_invocable", True))]
        if not available:
            await screen._notice(
                "No skills discovered. Add skill packages under `.agents/skills`, or enable "
                "the skills plugin in /plugins and set its directories.",
                "info",
            )
            return
        composer = screen.query_one("#composer", Composer)
        composer.load_text(f"${query.strip().replace(' ', '-')}")
        composer.focus()
        self.sync_skill_suggestions()

    async def refresh_skills(self) -> None:
        """Fetch the runtime's skill catalog for the `$` picker; discovery is host-side."""
        screen = self.screen
        if screen._skills_loading:
            return
        screen._skills_loading = True
        try:
            binding_id = await self.skills_binding_id()
            screen._skills = (
                [] if binding_id is None else await self.fetch_skill_catalog(binding_id)
            )
            self.sync_mention_catalog()
        finally:
            screen._skills_loading = False

    async def skills_binding_id(self) -> str | None:
        screen = self.screen
        for binding in screen._bindings_raw:
            plugin_id = str(binding.get("pluginId") or binding.get("plugin_id") or "")
            binding_id = str(binding.get("bindingId") or binding.get("binding_id") or "")
            if plugin_id == "harness.skills" and binding_id and binding.get("enabled", True):
                return binding_id
        # `capabilities/list` may not be negotiated; the catalog command name is unique.
        for command in screen._plugin_commands:
            name = str(command.get("commandName") or command.get("command_name") or "")
            binding_id = str(command.get("bindingId") or command.get("binding_id") or "")
            if name == "catalog" and binding_id:
                return binding_id
        return None

    async def fetch_skill_catalog(self, binding_id: str) -> list[dict[str, Any]]:
        screen = self.screen
        if not screen.client.extension.supports("_harness/commands/invoke"):
            return []
        if screen.commands.find_plugin_command(f"{binding_id}:catalog") is None:
            return []
        try:
            response = await screen.session.invoke_command(
                binding_id,
                "catalog",
                expected_revision=screen._plugin_revision,
                request_id=f"ui-{uuid.uuid4().hex}",
            )
        except (ExtensionUnavailable, HarnessClientError):
            return []
        presentation = response.result.presentation
        data = presentation.data if presentation is not None else {}
        skills = data.get("skills") if isinstance(data, dict) else None
        if not isinstance(skills, list):
            return []
        return [skill for skill in skills if isinstance(skill, dict)]

    async def stage_attachment(self, argument: str) -> bool:
        """Stage or clear local files for the next prompt (explicit user action only)."""
        screen = self.screen
        argument = argument.strip()
        if argument.lower() in {"", "list"}:
            if not screen._attachments:
                await screen._notice("No files staged. Use /attach <path>.", "info")
            else:
                await screen._notice(
                    "Staged: " + ", ".join(Path(item).name for item in screen._attachments),
                    "info",
                )
            return False
        if argument.lower() in {"clear", "none"}:
            count = len(screen._attachments)
            screen._attachments.clear()
            self.refresh_queue()
            await screen._notice(f"Cleared {count} staged attachment(s).", "info")
            return False
        capabilities = getattr(
            getattr(screen.client.initialized, "agent_capabilities", None),
            "prompt_capabilities",
            None,
        )
        if not bool(getattr(capabilities, "embedded_context", False)):
            await screen._notice(
                "This agent did not advertise embedded-context prompt support; "
                "attachments cannot be sent. Paste the relevant text instead.",
                "warning",
            )
            return False
        try:
            candidate = Path(argument).expanduser()
            path = validate_attachment(
                candidate if candidate.is_absolute() else screen._workspace_root / candidate
            )
        except HarnessClientError as exc:
            await screen._notice(exc.message, "error")
            return False
        if str(path) in screen._attachments:
            await screen._notice(f"{path.name} is already staged.", "info")
        else:
            screen._attachments.append(str(path))
            await screen._notice(
                f"Staged {path.name}; {len(screen._attachments)} file(s) will be attached "
                "to the next message. Use /attach clear to remove them.",
                "success",
            )
        composer = screen.query_one("#composer", Composer)
        self.refresh_queue()
        composer.focus()
        return True
