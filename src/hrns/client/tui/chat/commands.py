"""Local, agent, and plugin command routing."""

from __future__ import annotations

import shlex
from typing import TYPE_CHECKING, Any

from ...errors import ExtensionUnavailable, HrnsClientError
from ...launch import remote_target
from ..commands import resolve_local_command
from ..composer import Composer
from ..transcript import TranscriptPane

if TYPE_CHECKING:
    from ...client import SessionProxy
    from .screen import ChatScreen

_CONNECT_USAGE = "Usage: /connect [wss://host/acp] [--token REF] [--cwd PATH]"


def _parse_connect_args(rest: str) -> tuple[str, str, str]:
    """Parse ``URL [--token REF] [--cwd PATH]`` for the `/connect` command."""
    try:
        words = shlex.split(rest)
    except ValueError as exc:
        raise HrnsClientError(f"Could not parse /connect arguments: {exc}") from exc
    url = ""
    token = ""
    cwd = ""
    index = 0
    while index < len(words):
        word = words[index]
        key, equals, inline = word.partition("=")
        if key in {"--token", "--cwd"}:
            if equals:
                value = inline
                index += 1
            else:
                if index + 1 >= len(words):
                    raise HrnsClientError(f"{key} needs a value. {_CONNECT_USAGE}")
                value = words[index + 1]
                index += 2
            if key == "--token":
                token = value
            else:
                cwd = value
            continue
        if word.startswith("-") or url:
            raise HrnsClientError(_CONNECT_USAGE)
        url = word
        index += 1
    if not url:
        raise HrnsClientError(_CONNECT_USAGE)
    return url, token, cwd


class CommandWorkflow:
    """Owns slash-command dispatch and plugin command invocation."""

    def __init__(self, screen: ChatScreen) -> None:
        self.screen = screen

    async def handle_command(self, text: str) -> None:
        screen = self.screen
        body = text[1:].strip()
        if not body:
            screen.action_command_palette()
            return
        name, _, rest = body.partition(" ")
        name = name.strip()
        rest = rest.strip()
        local = resolve_local_command(name)
        agent_names = {str(command.get("name", "")) for command in screen._commands}
        if ":" in name:
            descriptor = self.find_plugin_command(name)
            if descriptor is None:
                await screen._notice(
                    f"Unknown plugin command '/{name}'. Press Ctrl+K to see available commands.",
                    "warning",
                )
                return
            await self.run_plugin_command(
                str(descriptor.get("bindingId") or descriptor.get("binding_id")),
                str(descriptor.get("commandName") or descriptor.get("command_name")),
                rest,
            )
            return
        if name == "agent":
            if not rest:
                await screen._notice("Usage: /agent <agent-command> [arguments]", "warning")
                return
            agent_name, _, arguments = rest.partition(" ")
            await self.send_agent_command(agent_name, arguments)
            return
        if local is not None:
            await self.run_local_command(local.name, rest)
            if local.name in agent_names:
                await screen._notice(
                    f"Local /{local.name} ran. Use /agent {local.name} for the agent's "
                    f"'{local.name}' command.",
                    "info",
                )
            return
        if name in agent_names:
            await self.send_agent_command(name, rest)
            return
        await screen._notice(
            f"Unknown command '/{name}'. Press Ctrl+K to see local and agent commands.",
            "warning",
        )
        composer = screen.query_one("#composer", Composer)
        composer.load_text(text)

    async def connect(self, rest: str) -> None:
        """Open the picker, or connect directly when a URL is supplied."""
        screen = self.screen
        if not rest:
            await screen.app.choose_agent(screen)
            return
        try:
            url, token, cwd = _parse_connect_args(rest)
            target = remote_target(url, token=token, cwd=cwd)
        except HrnsClientError as exc:
            await screen._notice(exc.message, "error")
            return
        await screen.app.switch_agent(screen, target)

    async def run_local_command(self, name: str, rest: str = "") -> None:
        screen = self.screen
        composer = screen.query_one("#composer", Composer)
        composer.clear_composer()
        if name == "attach":
            await screen.prompts.stage_attachment(rest)
        elif name == "connect":
            await self.connect(rest)
        elif name == "rename":
            await screen.sessions.rename(rest)
        elif name == "sessions":
            screen.action_show_sessions()
        elif name == "find":
            screen._find_transcript(rest)
        elif name == "copy":
            response = screen.query_one("#transcript", TranscriptPane).latest_response()
            if response is None:
                screen.app.notify("No completed assistant response to copy")
            else:
                screen.app.copy_to_clipboard(response._text)
                screen.app.notify("Response copied")
        elif name == "skills":
            await screen.prompts.show_skills(rest)
        elif name == "todo":
            screen.action_show_todos()
        elif name == "models":
            if rest:
                await screen.models.set_model(rest)
            else:
                await screen.models.show_models()
        elif name == "providers":
            await screen.models.show_providers()
        elif name == "effort":
            await screen.models.show_effort()
        elif name == "permissions":
            screen._show_permissions()
        elif name == "settings":
            screen._show_settings()
        elif name == "help":
            screen.action_help()

    @staticmethod
    def plugin_command_display(command: dict[str, Any]) -> str:
        binding = str(command.get("bindingId") or command.get("binding_id") or "")
        name = str(command.get("commandName") or command.get("command_name") or "")
        return f"{binding}:{name}" if binding else name

    def find_plugin_command(self, display: str) -> dict[str, Any] | None:
        # A binding id may itself contain colons; the displayed name ends at the final one.
        for command in self.screen._plugin_commands:
            if self.plugin_command_display(command) == display:
                return command
        return None

    def make_view_context(self, session: SessionProxy) -> Any:
        screen = self.screen
        from ..extensions import ViewContext

        async def invoke(
            binding_id: str,
            command_name: str,
            arguments: dict[str, Any] | None,
            text: str | None,
        ) -> Any:
            if screen.session is not session:
                raise HrnsClientError(
                    "This widget belongs to a conversation that is no longer open."
                )
            if self.find_plugin_command(f"{binding_id}:{command_name}") is None:
                raise HrnsClientError(
                    f"Command '/{binding_id}:{command_name}' is no longer available."
                )
            return await self.invoke_plugin_command(
                binding_id,
                command_name,
                text=text,
                arguments=arguments,
            )

        return ViewContext(session_id=session.session_id, invoke=invoke)

    async def run_plugin_command(self, binding_id: str, command_name: str, arguments: str) -> None:
        await self.screen._notice("Plugin commands are not available in standard ACP.", "warning")

    async def invoke_plugin_command(
        self,
        binding_id: str,
        command_name: str,
        *,
        text: str | None = None,
        arguments: dict[str, Any] | None = None,
        request_id: str | None = None,
    ) -> Any:
        raise ExtensionUnavailable("plugin commands")

    async def send_agent_command(self, name: str, arguments: str) -> None:
        screen = self.screen
        if not name:
            return
        prompt = f"/{name}" + (f" {arguments}" if arguments else "")
        composer = screen.query_one("#composer", Composer)
        composer.clear_composer()
        await screen.prompts.start_prompt(prompt)
