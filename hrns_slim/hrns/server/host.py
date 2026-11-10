"""Server with ACP steering and independent read-only AG-UI mounts."""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import FastAPI, HTTPException
from pydantic_ai.messages import ModelMessage
from starlette.requests import HTTPConnection
from starlette.responses import StreamingResponse

from .acp.host import AcpServer
from .ag_ui import Observation

if TYPE_CHECKING:
    from .fastapi import Authenticator


class HrnsServer(AcpServer):
    def _make_observer(self, session_id: str, history: list[ModelMessage]) -> Observation:
        return Observation(session_id, history)

    def mount_ag_ui(
        self, app: FastAPI, *, prefix: str = "/ag-ui", authenticate: Authenticator | None = None
    ) -> None:

        from .fastapi import authorize

        async def observation(
            connection: HTTPConnection, agent_id: str, session_id: str
        ) -> Observation:
            if not await authorize(connection, authenticate):
                raise HTTPException(401, "Not authenticated")
            if not self._running:
                raise HTTPException(503, "Server is not running")
            entry = self._registrations.get(agent_id)
            view = entry.views.get(session_id) if entry else None
            if view is None or not isinstance(view.observer, Observation):
                raise HTTPException(404, "Session not found")
            return view.observer

        path = prefix.rstrip("/") + "/{agent_id}/sessions/{session_id}"

        @app.get(path, description="Read-only AG-UI snapshot. Does not execute an agent.")
        async def snapshot(
            connection: HTTPConnection, agent_id: str, session_id: str
        ) -> dict[str, object]:
            publisher = await observation(connection, agent_id, session_id)
            return publisher.snapshot()

        @app.get(path + "/events", description="Read-only AG-UI SSE. Accepts no client mutations.")
        async def events(
            connection: HTTPConnection, agent_id: str, session_id: str
        ) -> StreamingResponse:
            publisher = await observation(connection, agent_id, session_id)

            async def stream():
                async with publisher.subscribe() as subscription:
                    async for event in subscription:
                        yield event

            return StreamingResponse(
                stream(),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )
