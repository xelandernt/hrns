"""Local, agent, and plugin command routing."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

from ...errors import ExtensionUnavailable, HrnsClientError
from ..commands import resolve_local_command
from ..composer import Composer
from ..transcript import TranscriptPane

if TYPE_CHECKING:
    from ...client import SessionProxy
    from .screen import ChatScreen


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

    async def run_local_command(self, name: str, rest: str = "") -> None:
        screen = self.screen
        composer = screen.query_one("#composer", Composer)
        composer.clear_composer()
        if name == "attach":
            await screen.prompts.stage_attachment(rest)
        elif name == "connect":
            await screen.app.choose_agent(screen)
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
        elif name == "plugins":
            screen.action_show_capabilities()
            screen.refresh_capabilities()
        elif name == "mcp":
            screen.action_show_mcp()
        elif name == "models":
            await screen.models.show_models()
        elif name == "providers":
            screen.models.open_providers()
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
        """Slash invocation: dispatch without creating a model turn or tool call."""
        screen = self.screen
        if not screen.client.extension.supports("_hrns/commands/invoke"):
            await screen._notice("This agent does not negotiate hrns plugin commands.", "warning")
            return
        if screen.busy:
            await screen._notice(
                "Stop the running turn before running a plugin command.", "warning"
            )
            return
        composer = screen.query_one("#composer", Composer)
        composer.clear_composer()
        try:
            await self.invoke_plugin_command(binding_id, command_name, text=arguments)
        except (ExtensionUnavailable, HrnsClientError):
            return

    async def invoke_plugin_command(
        self,
        binding_id: str,
        command_name: str,
        *,
        text: str | None = None,
        arguments: dict[str, Any] | None = None,
        request_id: str | None = None,
    ) -> Any:
        screen = self.screen
        if not screen.client.extension.supports("_hrns/commands/invoke"):
            raise ExtensionUnavailable("_hrns/commands/invoke")
        session = screen.session
        request_id = request_id or f"ui-{uuid.uuid4().hex}"
        try:
            response = await session.invoke_command(
                binding_id,
                command_name,
                text=text,
                arguments=arguments,
                expected_revision=screen._plugin_revision,
                request_id=request_id,
            )
        except (ExtensionUnavailable, HrnsClientError) as exc:
            await screen._notice(f"Command failed: {exc}", "error")
            raise
        if screen.session is not session:
            # The user switched sessions while the handler ran; stale output must not be
            # presented in, or start a turn for, a different conversation.
            return response
        result = response.result
        fallback = (
            result.presentation.fallback
            if result.presentation is not None
            else "Command completed."
        )
        await screen.query_one("#transcript", TranscriptPane).add_command(
            {
                "sessionId": session.session_id,
                "bindingId": response.binding_id,
                "commandName": response.command_name,
                "requestId": request_id,
                "fallback": fallback,
                "presentation": (
                    result.presentation.to_wire() if result.presentation is not None else None
                ),
            }
        )
        action = result.conversation
        if action is None:
            return response
        key = (session.session_id, request_id)
        if key in screen._consumed_command_actions:
            return response
        screen._consumed_command_actions.add(key)
        if len(screen._consumed_command_actions) > 200:
            screen._consumed_command_actions.clear()
            screen._consumed_command_actions.add(key)
        if getattr(action, "type", "") != "start_turn":
            await screen._notice(
                f"Command '/{binding_id}:{command_name}' requested an unsupported "
                "conversation action.",
                "warning",
            )
            return response
        await screen.prompts.start_prompt(
            action.prompt,
            attachments=[],
            from_queue=True,
        )
        return response

    async def send_agent_command(self, name: str, arguments: str) -> None:
        screen = self.screen
        if not name:
            return
        prompt = f"/{name}" + (f" {arguments}" if arguments else "")
        composer = screen.query_one("#composer", Composer)
        composer.clear_composer()
        await screen.prompts.start_prompt(prompt)
