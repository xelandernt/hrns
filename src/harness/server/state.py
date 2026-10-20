"""Per-application state and FastAPI dependencies."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from fastapi import Request, WebSocket

from .auth import PrincipalIndex
from .catalog import CursorCodec
from .config import (
    DeploymentConfig,
    HarnessServerConfig,
    PrincipalConfig,
    ServerSettings,
)
from .worker import WorkerRegistry, WorkerSlots

__all__ = ["HarnessState", "get_state", "get_websocket_state"]


@dataclass
class HarnessState:
    """Per-application immutable catalog plus mutable capacity accounting."""

    config: HarnessServerConfig
    settings: ServerSettings
    store_path: str
    deployments: dict[str, DeploymentConfig]
    deployment_order: list[DeploymentConfig]
    principals: PrincipalIndex
    cursor_codec: CursorCodec
    slots: WorkerSlots
    registry: WorkerRegistry

    @classmethod
    def from_config(cls, config: HarnessServerConfig) -> "HarnessState":
        deployments = {deployment.id: deployment for deployment in config.deployments}
        return cls(
            config=config,
            settings=config.server,
            store_path=config.server.store_path,
            deployments=deployments,
            deployment_order=list(config.deployments),
            principals=PrincipalIndex(config.principals),
            cursor_codec=CursorCodec(),
            slots=WorkerSlots(
                config.server.max_worker_count,
                config.server.max_connections_per_principal,
            ),
            registry=WorkerRegistry(),
        )

    def deployment(self, agent_id: str) -> DeploymentConfig | None:
        return self.deployments.get(agent_id)

    def worker_store_path(self, agent_id: str) -> str:
        """Value handed to workers as ``HARNESS_STORE``.

        By default this is one SQLite database per immutable deployment revision:
        workers of the same revision (all connections) share it under SQLite WAL,
        while different runtime versions never contend for one schema. With
        ``server.store_url`` the same PostgreSQL DSN is supplied to every worker;
        rows remain scoped to ``(principal, deploymentId)`` inside the database.
        """

        if self.settings.store_url:
            return self.settings.store_url
        return str(Path(self.store_path) / f"{agent_id}.sqlite3")

    def allowed_deployments(self, principal: PrincipalConfig) -> list[DeploymentConfig]:
        allowed = set(principal.allowed_agent_ids)
        return [
            deployment
            for deployment in self.deployment_order
            if deployment.id in allowed
        ]


async def get_state(request: Request) -> HarnessState:
    """FastAPI dependency returning the application's shared server state."""

    return request.app.state.harness


async def get_websocket_state(websocket: WebSocket) -> HarnessState:
    """WebSocket counterpart of :func:`get_state`."""

    return websocket.app.state.harness
