"""Built-in ACP entry point backed by the configured Pydantic AI model."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Annotated

from acp import schema
from cyclopts import App, Parameter
from pydantic_ai.agent import AbstractAgent
from pydantic_ai.settings import ModelSettings

from ..local_server import configuration_id, load_settings
from .acp import (
    AcpAttachmentConfig,
    AcpServer,
    AcpSession,
    SessionContext,
    acp_mcp_toolsets,
    run_acp_stdio,
)
from .acp.store import SessionStoreError
from .agent import build_agent
from .catalog import _LIVE_CATALOG_PROVIDERS
from .local_sessions import LocalSessionStore
from .model_resolver import RuntimeModelResolver, _thinking_support

__all__ = ["RuntimeModelResolver", "main"]

logger = logging.getLogger(__name__)


def _print_catalog(resolver: RuntimeModelResolver) -> None:
    providers = []
    for name, entry in resolver.settings.providers.items():
        provider = entry.provider_id(name)
        models = resolver.provider_models(name)
        thinking = {
            model: support
            for model in models
            if (support := _thinking_support(f"{provider}:{model}")) is not None
        }
        providers.append(
            {
                "name": name,
                "models": models,
                "thinking": thinking,
                "login": provider in {"github-copilot", "openai-codex"},
            }
        )
    print(json.dumps({"providers": providers}, indent=2))


def _attachment_config(session: AcpSession) -> AcpAttachmentConfig[None]:
    """Turn the client's MCP declarations into Pydantic AI toolsets."""
    return AcpAttachmentConfig(toolsets=acp_mcp_toolsets(session))


def run(
    *,
    config: Annotated[Path | None, Parameter(help="Path to the hrns configuration file.")] = None,
    env_only: Annotated[
        bool,
        Parameter(help="Ignore the configuration file and use environment variables only."),
    ] = False,
    catalog: Annotated[bool, Parameter(help="Print the provider/model catalog.")] = False,
    cwd: Annotated[str, Parameter(help="Workspace root.")] = ".",
    setup: Annotated[bool, Parameter(show=False)] = False,
    local_daemon: Annotated[bool, Parameter(show=False)] = False,
) -> None:
    """Run the configured agent as an ACP stdio server."""
    settings = load_settings(config, env_only=env_only)

    resolver = RuntimeModelResolver(settings)
    if any(
        entry.provider_id(name) in _LIVE_CATALOG_PROVIDERS
        for name, entry in settings.providers.items()
    ):
        asyncio.run(resolver.load_provider_catalogs())
    if catalog:
        _print_catalog(resolver)
        return
    store = LocalSessionStore(settings.store)
    scope: Path | None = None
    startup_override = {"HRNS_PROVIDER", "HRNS_MODEL"}.intersection(
        key.upper() for key in os.environ
    )
    if not env_only:
        scope = settings.path.resolve()
        try:
            resolver.last_provider_models = store.provider_models(scope)
            if not startup_override:
                resolver.startup_model = store.startup_model(scope)
        except SessionStoreError as exc:
            logger.warning("Could not load the last selected model: %s", exc)

    async def remember_model(model: str) -> None:
        if scope is not None:
            await store.remember_startup_model(scope, model)
            resolver.last_provider_models[model.partition(":")[0]] = model
            if not startup_override:
                resolver.startup_model = model

    models = resolver.list_models()
    if not models and env_only and not setup:
        raise SystemExit("No models are configured. Add a provider and model first.")

    def factory(session: SessionContext) -> AbstractAgent[None, str]:
        # Resolve the selected catalog entry at prompt time, including after local setup.
        return build_agent(
            cwd=session.cwd if local_daemon or cwd == "." else cwd,
            skills=settings.skills,
            model_settings=ModelSettings(
                **{
                    key: value
                    for key, value in settings.model_settings.items()
                    if key != "thinking"
                }
            ),
            _allow_unconfigured=True,
        )

    model_memory = remember_model if scope is not None else None
    mcp_capabilities = schema.McpCapabilities(acp=True, http=True, sse=True)
    if local_daemon:
        from .local_daemon import serve

        server = AcpServer[None]()
        registration = server.add_agent(
            "agent",
            factory=factory,
            model_resolver=resolver,
            session_store=store,
            attachment_config=_attachment_config,
            mcp_capabilities=mcp_capabilities,
        )

        def adapter():
            connection = registration.connection()
            connection._provider_editor = None if env_only else resolver.provider_editor()
            connection._remember_model = model_memory
            return connection

        serve(
            server,
            adapter,
            store.path,
            configuration_id(settings, env_only=env_only),
            has_pending_runs=registration._has_pending_runs,
        )
        return

    run_acp_stdio(
        factory,
        model_resolver=resolver,
        session_store=store,
        attachment_config=_attachment_config,
        mcp_capabilities=mcp_capabilities,
        _provider_editor=None if env_only else resolver.provider_editor(),
        _remember_model=model_memory,
    )


app = App(
    name="hrns agent",
    help="Run the configured agent as an ACP stdio server.",
    default_command=run,
)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
