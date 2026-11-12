"""Mount authenticated document synchronization and session lifecycle routes.

Creation and discovery are HTTP so a TypeScript-only client can start or join a
session without an ACP connection. Live participation is one WebSocket per
replica. Authentication and store authorization run before content is exposed.
"""

from __future__ import annotations

import base64
from typing import TYPE_CHECKING

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel
from starlette.requests import HTTPConnection
from starlette.responses import Response

from hrns.server._types import PrincipalT

from .registration import LoroSession
from .store import SessionAccess
from .sync import SyncTransport

if TYPE_CHECKING:
    from hrns.server.acp.host import AcpServer
    from hrns.server.fastapi import Authenticator


class CreateSessionRequest(BaseModel):
    cwd: str


class CreateSessionResponse(BaseModel):
    sessionId: str
    cwd: str


class _WebSocketTransport:
    def __init__(self, websocket: WebSocket) -> None:
        self._websocket = websocket

    async def send(self, data: bytes) -> None:
        await self._websocket.send_bytes(data)

    async def receive(self) -> bytes | None:
        message = await self._websocket.receive()
        if message["type"] == "websocket.disconnect":
            return None
        data = message.get("bytes")
        return data if isinstance(data, bytes) else None

    async def close(self, code: int = 1000, message: str = "") -> None:
        try:
            await self._websocket.close(code=code, reason=message)
        except Exception:
            pass


def mount_loro_routes(
    server: AcpServer[PrincipalT],
    app: FastAPI,
    *,
    prefix: str,
    authenticate: Authenticator[PrincipalT] | None = None,
) -> None:
    base = prefix.rstrip("/") + "/{agent_id}/sessions"

    async def principal_of(connection: HTTPConnection) -> PrincipalT | None:
        principal = await authenticate(connection) if authenticate is not None else None
        if authenticate is not None and principal is None:
            raise HTTPException(401, "Not authenticated")
        return principal

    async def session_of(
        agent_id: str,
        session_id: str,
        operation: SessionAccess,
        principal: PrincipalT | None,
    ) -> LoroSession:
        registration = server._loro.get(agent_id)
        if registration is None or not server._running:
            raise HTTPException(404, "Session not found")
        try:
            return await registration.get(session_id, principal, operation)
        except PermissionError as exc:
            raise HTTPException(403, "Not authorized") from exc
        except KeyError as exc:
            raise HTTPException(404, "Session not found") from exc

    @app.post(base, description="Create a native session and its Loro document.")
    async def create_session(
        connection: HTTPConnection, agent_id: str, body: CreateSessionRequest
    ) -> CreateSessionResponse:
        principal = await principal_of(connection)
        registration = server._loro.get(agent_id)
        if registration is None or not server._running:
            raise HTTPException(404, "Session not found")
        try:
            session = await registration.create(body.cwd, principal)
        except PermissionError as exc:
            raise HTTPException(403, "Not authorized") from exc
        return CreateSessionResponse(sessionId=session.session_id, cwd=session.cwd)

    @app.get(base, description="List stored sessions for this agent.")
    async def list_sessions(
        connection: HTTPConnection,
        agent_id: str,
        cwd: str | None = None,
        cursor: str | None = None,
    ) -> dict[str, object]:
        principal = await principal_of(connection)
        registration = server._loro.get(agent_id)
        if registration is None or not server._running:
            raise HTTPException(404, "Session not found")
        if registration.list is None:
            raise HTTPException(405, "Listing is not supported by this store")
        try:
            summaries, next_cursor = await registration.list(principal, cwd, cursor)
        except PermissionError as exc:
            raise HTTPException(403, "Not authorized") from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {
            "sessions": [
                {
                    "sessionId": summary.session_id,
                    "cwd": summary.cwd,
                    "title": summary.title,
                    "updatedAt": (
                        summary.updated_at.isoformat() if summary.updated_at is not None else None
                    ),
                }
                for summary in summaries
            ],
            "nextCursor": next_cursor,
        }

    @app.get(base + "/{session_id}", description="Read session metadata and live status.")
    async def read_session(
        connection: HTTPConnection, agent_id: str, session_id: str
    ) -> dict[str, object]:
        principal = await principal_of(connection)
        session = await session_of(agent_id, session_id, "loro/snapshot", principal)
        return {
            "sessionId": session.session_id,
            "cwd": session.cwd,
            "status": session.status(),
            "snapshot": base64.b64encode(session.snapshot()).decode("ascii"),
        }

    @app.get(base + "/{session_id}/snapshot", description="Export the current Loro snapshot.")
    async def read_snapshot(connection: HTTPConnection, agent_id: str, session_id: str) -> Response:
        principal = await principal_of(connection)
        session = await session_of(agent_id, session_id, "loro/snapshot", principal)
        return Response(content=session.snapshot(), media_type="application/octet-stream")

    @app.delete(base + "/{session_id}", description="Delete a live or stored session.")
    async def delete_session(
        connection: HTTPConnection, agent_id: str, session_id: str
    ) -> dict[str, object]:
        principal = await principal_of(connection)
        session = await session_of(agent_id, session_id, "session/delete", principal)
        registration = server._loro.get(agent_id)
        if registration is None or registration.delete is None:
            raise HTTPException(405, "Deletion is not supported by this store")
        try:
            await registration.delete(session.session_id, principal)
        except PermissionError as exc:
            raise HTTPException(403, "Not authorized") from exc
        except KeyError as exc:
            raise HTTPException(404, "Session not found") from exc
        return {"deleted": session.session_id}

    @app.websocket(base + "/{session_id}/sync")
    async def sync(websocket: WebSocket, agent_id: str, session_id: str) -> None:
        try:
            principal = await principal_of(websocket)
        except HTTPException:
            await websocket.close(code=1008)
            return
        try:
            session = await session_of(agent_id, session_id, "loro/sync", principal)
        except HTTPException:
            await websocket.close(code=1008)
            return
        await websocket.accept()
        transport: SyncTransport = _WebSocketTransport(websocket)
        try:
            await session.sync(transport)
        except WebSocketDisconnect:
            pass
        finally:
            await transport.close()


__all__ = ["mount_loro_routes"]
