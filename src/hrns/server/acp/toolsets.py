"""Filesystem and shell tools that route through the ACP client."""

from __future__ import annotations

import asyncio
import contextlib
import os.path
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Protocol

import anyio
from acp import Client, schema
from pydantic_ai import ModelRetry
from pydantic_ai.capabilities import Toolset
from pydantic_ai.tools import AgentDepsT, RunContext
from pydantic_ai.toolsets import FunctionToolset, ToolsetTool
from pydantic_ai.toolsets.wrapper import WrapperToolset
from pydantic_ai.workspaces import LocalWorkspaceBackend, Workspace
from pydantic_ai_harness.filesystem import FileSystem, FileSystemToolset

from .session import AcpSession


class _WorkspaceFileWriter(Protocol):
    def write_file(self, path: str, content: str, *, workspace: Workspace) -> Awaitable[str]: ...


class _FallbackFileWriter(_WorkspaceFileWriter):
    """Writes with the local ``FileSystem`` toolset, bounded by the session's ``cwd``."""

    def __init__(self, cwd: str) -> None:
        self._cwd = cwd
        toolset = FileSystem[None](root_dir=cwd).get_toolset()
        assert isinstance(toolset, FileSystemToolset)
        self._toolset = toolset

    async def write_file(self, path: str, content: str, *, workspace: Workspace) -> str:
        target = workspace if workspace.attached else LocalWorkspaceBackend(self._cwd)
        return await self._toolset.write_file(path, content, workspace=target)


class AcpFileSystemToolset(FunctionToolset[AgentDepsT]):
    """``read_file``/``write_file`` tools backed by the ACP client connection."""

    def __init__(
        self,
        *,
        client: Client,
        session_id: str,
        cwd: str | None = None,
        local_writer: _WorkspaceFileWriter | None = None,
    ) -> None:
        super().__init__()
        self._client = client
        self._session_id = session_id
        self._cwd = cwd
        self._local_writer = local_writer
        self.add_function(self.read_file, name="read_file")
        self.add_function(self.write_file, name="write_file")

    def _absolute(self, path: str) -> str:
        if self._cwd is None or os.path.isabs(path):
            return path
        return os.path.normpath(os.path.join(self._cwd, path))

    async def read_file(self, path: str) -> str:
        """Read a text file's contents through the editor.

        Args:
            path: Path to the file; resolved against the session workspace when relative.
        """
        response = await self._client.read_text_file(
            path=self._absolute(path), session_id=self._session_id
        )
        return response.content

    async def write_file(self, ctx: RunContext[AgentDepsT], path: str, content: str) -> str:
        """Write a text file's full contents through the editor.

        Args:
            ctx: The current agent run context.
            path: Path to the file; resolved against the session workspace when relative.
            content: The complete new contents of the file.
        """
        path = self._absolute(path)
        if self._local_writer is not None:
            return await self._local_writer.write_file(path, content, workspace=ctx.workspace)
        await self._client.write_text_file(content=content, path=path, session_id=self._session_id)
        return f"Wrote {path} ({len(content)} characters)."


def acp_filesystem(session: AcpSession) -> Toolset[None] | None:
    """Build an ACP-client-backed filesystem capability for ``session``, or ``None``."""
    capabilities = session.client_capabilities
    fs = capabilities.fs if capabilities is not None else None
    if fs is None or not fs.read_text_file:
        return None
    local_writer = None if fs.write_text_file else _FallbackFileWriter(session.cwd)
    return Toolset(
        AcpFileSystemToolset[None](
            client=session.client,
            session_id=session.session_id,
            cwd=session.cwd,
            local_writer=local_writer,
        )
    )


def _format_terminal_output(result: schema.TerminalOutputResponse) -> str:
    """Render a terminal's captured output plus notes for truncation or a non-zero exit."""
    parts = [result.output]
    if result.truncated:
        parts.append("[output truncated]")
    status = result.exit_status
    if status is not None and status.exit_code not in (None, 0):
        parts.append(f"[exited with code {status.exit_code}]")
    elif status is not None and status.signal is not None:
        parts.append(f"[terminated by signal {status.signal}]")
    return "\n".join(parts)


class AcpTerminalToolset(FunctionToolset[AgentDepsT]):
    """A ``run_command`` tool backed by the ACP client's terminal lifecycle."""

    def __init__(self, *, client: Client, session_id: str, cwd: str | None = None) -> None:
        super().__init__()
        self._client = client
        self._session_id = session_id
        self._cwd = cwd
        self.add_function(
            self.run_command,
            name="run_command",
            metadata={
                "code_arg_name": "command",
                "code_arg_language": "shell",
                "temporal": {"start_to_close_timeout": timedelta(hours=1)},
            },
        )

    async def run_command(self, command: str) -> str:
        """Run a shell command in the editor's terminal and return its captured output.

        Args:
            command: The shell command line to execute.
        """
        create = asyncio.ensure_future(
            self._client.create_terminal(
                command=command, session_id=self._session_id, cwd=self._cwd
            )
        )
        terminal_id: str | None = None
        try:
            await asyncio.wait([create])
            terminal_id = create.result().terminal_id
            await self._client.wait_for_terminal_exit(
                session_id=self._session_id, terminal_id=terminal_id
            )
            result = await self._client.terminal_output(
                session_id=self._session_id, terminal_id=terminal_id
            )
            return _format_terminal_output(result)
        except asyncio.CancelledError:
            with anyio.CancelScope(shield=True):
                if terminal_id is None:
                    with contextlib.suppress(Exception):
                        terminal_id = (await create).terminal_id
                if terminal_id is not None:
                    with contextlib.suppress(Exception):
                        await self._client.kill_terminal(
                            session_id=self._session_id, terminal_id=terminal_id
                        )
            raise
        finally:
            if terminal_id is not None:
                with anyio.CancelScope(shield=True), contextlib.suppress(Exception):
                    await self._client.release_terminal(
                        session_id=self._session_id, terminal_id=terminal_id
                    )


def acp_terminal(session: AcpSession) -> Toolset[None] | None:
    """Build an ACP-client-backed terminal capability for ``session``, or ``None``."""
    capabilities = session.client_capabilities
    if capabilities is None or not capabilities.terminal:
        return None
    return Toolset(
        AcpTerminalToolset[None](
            client=session.client, session_id=session.session_id, cwd=session.cwd
        )
    )


@dataclass
class AttachmentToolset(WrapperToolset[AgentDepsT]):
    """Refuse dispatch against resources from a departed ACP attachment."""

    current: Callable[[], bool]

    async def call_tool(
        self,
        name: str,
        tool_args: dict[str, object],
        ctx: RunContext[AgentDepsT],
        tool: ToolsetTool[AgentDepsT],
    ) -> object:
        if not self.current():
            raise ModelRetry(
                "The client that provided this tool disconnected; execution was not dispatched."
            )
        try:
            return await self.wrapped.call_tool(name, tool_args, ctx, tool)
        except (ConnectionError, EOFError) as exc:
            raise ModelRetry(
                "Client disconnected during tool execution; its outcome is unknown. Do not automatically retry side effects."
            ) from exc
