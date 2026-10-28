"""Build the ACP-client-backed terminal capability for one session."""

from __future__ import annotations

import asyncio
import contextlib
from datetime import timedelta

from pydantic_ai.capabilities import Toolset
from pydantic_ai.toolsets import FunctionToolset

from hrns.protocol import TERMINAL, HrnsError

from ...client_io import client_offers
from ...registry import BindingContext
from ...resources import CLIENT

__all__ = ["build_client_terminal"]


def build_client_terminal(binding: BindingContext) -> Toolset[object]:
    """Wrap the client-backed terminal toolset in the capability this binding contributes."""
    resources = binding.resources
    session = resources.require(CLIENT) if resources is not None else None
    if session is None or not client_offers(session.capabilities, TERMINAL):
        raise HrnsError(
            "CAPABILITY_UNAVAILABLE",
            f"Binding {binding.binding_id!r} needs a client that advertises terminal support, "
            "and the connected client does not.",
            details={"binding_id": binding.binding_id, "requires_client": [TERMINAL]},
        )

    async def run_command(command: str, args: list[str] | None = None) -> str:
        """Run a program in the connected ACP client's environment, and return its output.

        Args:
            command: Executable name or absolute path, without arguments.
            args: Separate program arguments. For shell syntax, explicitly invoke the
                client's shell with its command flag and the script as arguments.
        """
        create = asyncio.create_task(
            session.client.create_terminal(
                command=command,
                args=args,
                session_id=session.session_id,
                cwd=session.cwd or None,
            )
        )
        terminal_id: str | None = None
        cancelled = False
        try:
            # Await without cancelling the request: its response is needed for cleanup.
            await asyncio.wait([create])
            terminal_id = create.result().terminal_id
            await session.client.wait_for_terminal_exit(
                session_id=session.session_id, terminal_id=terminal_id
            )
            result = await session.client.terminal_output(
                session_id=session.session_id, terminal_id=terminal_id
            )
            parts = [result.output]
            if result.truncated:
                parts.append("[output truncated]")
            status = result.exit_status
            if status is not None and status.exit_code not in (None, 0):
                parts.append(f"[exited with code {status.exit_code}]")
            elif status is not None and status.signal is not None:
                parts.append(f"[terminated by signal {status.signal}]")
            return "\n".join(parts)
        except asyncio.CancelledError:
            cancelled = True
            raise
        finally:

            async def cleanup() -> None:
                nonlocal terminal_id
                if terminal_id is None:
                    with contextlib.suppress(Exception):
                        terminal_id = (await create).terminal_id
                if terminal_id is not None:
                    if cancelled:
                        with contextlib.suppress(Exception):
                            await session.client.kill_terminal(
                                session_id=session.session_id, terminal_id=terminal_id
                            )
                    with contextlib.suppress(Exception):
                        await session.client.release_terminal(
                            session_id=session.session_id, terminal_id=terminal_id
                        )

            await asyncio.shield(cleanup())

    toolset = FunctionToolset[object]()
    toolset.add_function(
        run_command,
        name="run_command",
        metadata={
            "code_arg_name": "command",
            "code_arg_language": "shell",
            "temporal": {"start_to_close_timeout": timedelta(hours=1)},
        },
    )
    return Toolset(toolset, id="hrns.client_terminal")
