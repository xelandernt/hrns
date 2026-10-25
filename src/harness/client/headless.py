"""Headless (`hrns run`) execution.

The runner streams assistant text to stdout, reports tool activity on
stderr, and prints either a JSON summary (``--json``) or a final status
line. Interactions never default to yes: without an explicit policy the run
fails with ``interaction-required`` while retaining the session.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, TextIO

from .attachments import attachment_summary, build_prompt_blocks
from .client import HarnessAcpClient
from .constants import CLIENT_VERSION
from .errors import HarnessClientError, InteractionRequired
from .events import (
    AgentTextChunk,
    CommandsUpdated,
    ConfigOptionsUpdated,
    PlanChanged,
    ToolEvent,
    TurnCompleted,
)
from .session import SessionProxy

__all__ = ["EXIT_INTERACTION_REQUIRED", "RunOutcome", "HeadlessRun", "run_headless"]

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_USAGE = 2
EXIT_INTERACTION_REQUIRED = 3


@dataclass
class RunOutcome:
    ok: bool
    session_id: str | None
    text: str = ""
    thoughts: str = ""
    stop_reason: str = ""
    tools: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, Any] | None = None
    error: dict[str, str] | None = None
    exit_code: int = EXIT_OK
    cwd: str = ""
    agent: dict[str, str] = field(default_factory=dict)
    duration_ms: int = 0
    interactions: list[dict[str, Any]] = field(default_factory=list)
    attachments: list[dict[str, Any]] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "ok": self.ok,
            "sessionId": self.session_id,
            "cwd": self.cwd,
            "agent": self.agent,
            "stopReason": self.stop_reason or None,
            "text": self.text,
            "tools": self.tools,
            "usage": self.usage,
            "durationMs": self.duration_ms,
            "client": {"name": "harness-client", "version": CLIENT_VERSION},
        }
        if self.thoughts:
            payload["thoughts"] = self.thoughts
        if self.error:
            payload["error"] = self.error
        if self.interactions:
            payload["interactions"] = self.interactions
        if self.attachments:
            payload["attachments"] = self.attachments
        return payload


class HeadlessRun:
    """Observes one turn and renders it for a non-interactive caller."""

    def __init__(
        self,
        client: HarnessAcpClient,
        *,
        json_output: bool = False,
        stdout: TextIO | None = None,
        stderr: TextIO | None = None,
        stream: bool = True,
    ) -> None:
        self.client = client
        self.json_output = json_output
        self.stdout = stdout or sys.stdout
        self.stderr = stderr or sys.stderr
        self.stream = stream and not json_output
        self.outcome = RunOutcome(ok=False, session_id=None)
        self._text_parts: list[str] = []
        self._thought_parts: list[str] = []
        self._tools: dict[str, dict[str, Any]] = {}
        self._started = time.monotonic()
        self._subscribers: list[Callable[[], None]] = []
        self._interaction_detected = False
        self._interaction_details: dict[str, Any] = {}

    def attach(self) -> None:
        self._subscribers.append(self.client.bus.subscribe(self._on_event))

    def detach(self) -> None:
        for unsubscribe in self._subscribers:
            unsubscribe()
        self._subscribers.clear()

    def _emit(self, text: str) -> None:
        if text:
            self.stdout.write(text)
            self.stdout.flush()

    def _status(self, text: str) -> None:
        self.stderr.write(f"{text}\n")
        self.stderr.flush()

    def _on_event(self, event: Any) -> None:
        if isinstance(event, AgentTextChunk):
            if event.thought:
                self._thought_parts.append(event.text)
                return
            self._text_parts.append(event.text)
            if self.stream:
                self._emit(event.text)
            return
        if isinstance(event, ToolEvent):
            entry = self._tools.setdefault(
                event.tool_call_id,
                {
                    "toolCallId": event.tool_call_id,
                    "title": event.title,
                    "kind": event.kind,
                    "status": event.status,
                    "detail": "",
                },
            )
            if event.title:
                entry["title"] = event.title
            if event.kind:
                entry["kind"] = event.kind
            if event.status:
                entry["status"] = event.status
            detail = _tool_detail(event)
            if detail:
                entry["detail"] = detail
            if event.is_new and self.stream:
                self._status(f"tool: {entry['title']} ({entry['status'] or 'pending'})")
            return
        if isinstance(event, TurnCompleted):
            self.outcome.stop_reason = event.stop_reason
            self.outcome.usage = event.usage
            return
        if isinstance(event, PlanChanged):
            if event.entries and self.stream:
                self._status("plan: " + " · ".join(entry[0] for entry in event.entries))
            return
        if isinstance(event, ConfigOptionsUpdated) or isinstance(event, CommandsUpdated):
            return

    def note_interaction(self, exc: InteractionRequired) -> None:
        self._interaction_detected = True
        self._interaction_details = {"kind": exc.kind, "summary": exc.message}
        self.outcome.interactions.append(self._interaction_details)

    def finish(
        self, *, session: SessionProxy | None, error: HarnessClientError | None = None
    ) -> RunOutcome:
        self.outcome.duration_ms = int((time.monotonic() - self._started) * 1000)
        self.outcome.session_id = session.session_id if session else self.outcome.session_id
        if session is not None:
            self.outcome.cwd = session.cwd
        self.outcome.agent = {
            "name": self.client.agent_title,
            "version": self.client.agent_version,
        }
        self.outcome.text = "".join(self._text_parts)
        self.outcome.thoughts = "".join(self._thought_parts)
        self.outcome.tools = list(self._tools.values())
        if error is not None:
            self.outcome.ok = False
            self.outcome.error = {
                "code": "interaction-required"
                if isinstance(error, InteractionRequired)
                else "run-failed",
                "message": error.message,
            }
            self.outcome.exit_code = (
                EXIT_INTERACTION_REQUIRED
                if isinstance(error, InteractionRequired)
                else EXIT_FAILURE
            )
        elif self._interaction_detected:
            self.outcome.ok = False
            self.outcome.error = {
                "code": "interaction-required",
                "message": "The agent requested an interaction but no explicit handler is configured.",
            }
            self.outcome.exit_code = EXIT_INTERACTION_REQUIRED
        else:
            self.outcome.ok = True
            self.outcome.exit_code = EXIT_OK
        return self.outcome

    def print_outcome(self) -> None:
        if self.json_output:
            self.stdout.write(
                json.dumps(self.outcome.to_json(), ensure_ascii=False, indent=2) + "\n"
            )
            self.stdout.flush()
            return
        if self.stream and not self.outcome.text.endswith("\n"):
            self._emit("\n")
        if self.outcome.error:
            self._status(f"harness: {self.outcome.error['code']}: {self.outcome.error['message']}")
        elif self.stream:
            stop = self.outcome.stop_reason or "end_turn"
            session = self.outcome.session_id or "?"
            self._status(f"harness: {stop} · session {session}")


def _tool_detail(event: ToolEvent) -> str:
    parts: list[str] = []
    for block in event.content:
        kind = block.get("type")
        if kind == "content":
            content = block.get("content") or {}
            if content.get("type") == "text":
                parts.append(str(content.get("text", "")))
        elif kind == "diff":
            parts.append(f"diff {block.get('path', '')}")
        elif kind == "terminal":
            parts.append(f"terminal {block.get('terminalId', '')}")
    if event.raw_output is not None:
        try:
            rendered = json.dumps(event.raw_output, ensure_ascii=False, default=str)
        except Exception:
            rendered = str(event.raw_output)
        if len(rendered) > 800:
            rendered = rendered[:800] + "…"
        parts.append(rendered)
    detail = "\n".join(part for part in parts if part)
    return detail[:8000]


async def run_headless(
    client: HarnessAcpClient,
    *,
    prompt: str,
    cwd: str,
    session_id: str | None = None,
    json_output: bool = False,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    connect: bool = True,
    timeout: float | None = None,
    attachments: list[str] | None = None,
) -> RunOutcome:
    """Connect, open/load a session, run one prompt, and return the outcome."""
    runner = HeadlessRun(client, json_output=json_output, stdout=stdout, stderr=stderr)
    session: SessionProxy | None = None
    attached = False
    error: HarnessClientError | None = None
    session_command = ""
    try:
        # Validate local attachments before opening a process or creating a
        # session, so a bad path cannot leave an empty remote session behind.
        blocks = build_prompt_blocks(prompt, attachments or ())
        runner.outcome.attachments = attachment_summary(attachments or ())
        if connect and not client.connected:
            await client.connect()
        if session_id:
            # Authoritative replay is the only safe recovery path for a client
            # with no verified local cache. Never fall back to resume.
            session = await client.load_session(session_id, cwd)
            session_command = "session/load"
            # Drain the authoritative replay before observing the new turn, so
            # headless output contains only the prompt being run now.
            await client.drain_events()
        else:
            session = await client.new_session(cwd)
        runner.outcome.session_id = session.session_id
        runner.outcome.cwd = cwd
        runner.attach()
        attached = True
        if timeout:
            await asyncio.wait_for(
                session.prompt_blocks(blocks, display_text=prompt), timeout=timeout
            )
        else:
            await session.prompt_blocks(blocks, display_text=prompt)
    except InteractionRequired as exc:
        runner.note_interaction(exc)
        error = exc
    except asyncio.TimeoutError:
        if session is not None:
            try:
                await asyncio.shield(session.cancel())
            except Exception:
                pass
        error = HarnessClientError(
            f"The turn did not finish within {timeout}s; cancellation was requested. "
            "Inspect the session before retrying."
        )
    except HarnessClientError as exc:
        error = exc
    except (KeyboardInterrupt, asyncio.CancelledError):
        if session is not None:
            try:
                await asyncio.shield(session.cancel())
            except Exception:
                pass
        error = HarnessClientError("Cancelled by the user.")
    finally:
        if attached:
            runner.detach()
    outcome = runner.finish(session=session, error=error)
    if session_command:
        outcome.interactions.append({"sessionCommand": session_command})
    runner.print_outcome()
    return outcome
