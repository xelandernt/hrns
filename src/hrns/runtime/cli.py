"""Command-line bootstrap and stdio serving for the ACP host."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import importlib
import inspect
import json
import logging
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from functools import reduce
from pathlib import Path
from typing import Any

from acp import schema
from acp.agent.router import build_agent_router
from acp.connection import Connection
from acp.core import DEFAULT_STDIO_BUFFER_LIMIT_BYTES
from acp.meta import AGENT_METHODS, CLIENT_METHODS
from acp.schema import DeleteSessionRequest
from acp.stdio import stdio_streams
from acp.utils import normalize_result, notify_model, request_model, request_optional_model

from hrns.protocol import HrnsError

from . import acp as acp_map
from .definitions import AgentDefinition, DefinitionManifest, manifest_from_file
from .host import HrnsACPAgent
from .host_support import HostBundle, HostSetup, build_agent, build_store
from .registry import PluginRegistry

_LOG = logging.getLogger("hrns.runtime.host")


def definition_from_manifest(manifest: DefinitionManifest) -> AgentDefinition:
    """Build a runtime definition from a parsed manifest envelope."""
    return manifest.to_definition()


_REGISTRY_HOOK_NAMES = ("build_registry", "registry")


async def _await_result(awaitable: Any) -> Any:
    return await awaitable


def _coerce_definition(target: Any, *, source: str) -> AgentDefinition:
    if callable(target) and not isinstance(target, AgentDefinition | DefinitionManifest):
        target = target()
        target = asyncio.run(_await_result(target)) if inspect.isawaitable(target) else target
    if isinstance(target, DefinitionManifest):
        return definition_from_manifest(target)
    if isinstance(target, AgentDefinition):
        return target
    if isinstance(target, Mapping):
        return AgentDefinition.model_validate(dict(target))
    raise HrnsError(
        "DEPLOYMENT_UNAVAILABLE",
        f"Definition source {source!r} produced {type(target).__name__}, not an AgentDefinition.",
        {"source": source},
    )


def _registry_from_module(module: Any) -> PluginRegistry | None:
    for name in _REGISTRY_HOOK_NAMES:
        value = getattr(module, name, None)
        if value is None:
            continue
        if isinstance(value, PluginRegistry):
            return value
        if callable(value):
            try:
                candidate = value()
            except TypeError:
                continue
            except Exception:
                _LOG.exception("definition registry hook %r failed", name)
                continue
            if isinstance(candidate, PluginRegistry):
                return candidate
    return None


def _load_definition_source(source: str) -> tuple[AgentDefinition, PluginRegistry | None]:
    path = Path(source).expanduser()
    if path.exists() and path.is_file():
        return definition_from_manifest(manifest_from_file(path)), None
    module_name, separator, attribute = source.partition(":")
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise HrnsError(
            "DEPLOYMENT_UNAVAILABLE",
            f"Cannot import definition module {module_name!r}: {exc}",
            {"source": source},
        ) from exc
    if separator:
        try:
            target: Any = reduce(getattr, attribute.split("."), module)
        except AttributeError as exc:
            raise HrnsError(
                "DEPLOYMENT_UNAVAILABLE",
                f"Definition module {module_name!r} has no attribute {attribute!r}.",
                {"source": source},
            ) from exc
    else:
        target = getattr(module, "definition", None)
        if target is None:
            raise HrnsError(
                "DEPLOYMENT_UNAVAILABLE",
                f"{module_name!r} defines no 'definition' attribute; use module:attribute.",
                {"source": source},
            )
    return _coerce_definition(target, source=source), _registry_from_module(module)


def load_definition(source: str) -> AgentDefinition:
    """Load an agent definition from an import reference or a manifest file."""
    return _load_definition_source(source)[0]


def build_local_bundle(setup: HostSetup) -> HostBundle:
    """The built-in composition: installed plugins + durable store + one definition."""
    definition, hook_registry = _load_definition_source(setup.definition)
    registry = hook_registry or PluginRegistry()
    registry.discover()
    definition = definition.resolve(registry)
    store = build_store(setup.store_path)
    return HostBundle(
        definition=definition,
        registry=registry,
        store=store,
        close_store=True,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


class _UsageError(Exception):
    pass


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hrns-host",
        description=(
            "Serve a hrns agent definition over ACP v1 on stdio. Diagnostics go to "
            "stderr; stdout carries newline-delimited JSON-RPC only."
        ),
    )
    parser.add_argument(
        "definition",
        nargs="?",
        help="agent definition import reference (package.module:attribute) or manifest path",
    )
    parser.add_argument(
        "--definition",
        dest="definition_flag",
        help="definition source (alternative to the positional)",
    )
    parser.add_argument("--manifest", help="YAML/JSON agent definition manifest path")
    parser.add_argument(
        "--store",
        help="SQLite session store path or PostgreSQL DSN (enables load/list/delete)",
    )
    parser.add_argument("--model", help="default model id for this deployment")
    parser.add_argument("--models", help="comma-separated model ids offered to ACP clients")
    parser.add_argument(
        "--resource",
        action="append",
        default=[],
        metavar="NAME=VALUE",
        help="named definition resource value (repeatable)",
    )
    parser.add_argument(
        "--allowed-cwd",
        action="append",
        dest="allowed_cwds",
        help="local workspace root (repeatable)",
    )
    parser.add_argument("--principal", help="local principal id (default: local)")
    parser.add_argument("--deployment-id", help="local deployment id (default: local)")
    parser.add_argument("--runtime-module", help="module exposing build_host_bundle(setup)")
    parser.add_argument(
        "--hosted", action="store_true", help="hosted worker: identity comes from HRNS_* env"
    )
    parser.add_argument(
        "--accept-client-capabilities",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="allow offered client plugins to execute through the ACP client (default: off)",
    )
    parser.add_argument("--log-level", default="warning")
    return parser


def _env_flag(name: str) -> bool:
    value = os.environ.get(name, "").strip().lower()
    return value not in ("", "0", "false", "no", "off")


def _require_env(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise _UsageError(f"{name} must be set by the server in hosted mode")
    return value


def setup_from_args(args: argparse.Namespace) -> HostSetup:
    """Resolve CLI arguments + the trusted hosted environment into a :class:`HostSetup`."""
    hosted = bool(args.hosted) or _env_flag("HRNS_HOSTED")
    sources = [
        source for source in (args.definition, args.definition_flag, args.manifest) if source
    ]
    if not sources:
        raise _UsageError("an agent definition import reference or manifest path is required")
    if len(sources) > 1:
        raise _UsageError("provide exactly one definition source")
    definition = sources[0]
    resources: dict[str, str] = {}
    for item in args.resource:
        name, separator, value = item.partition("=")
        if not separator or not name.strip():
            raise _UsageError(f"--resource expects NAME=VALUE, got {item!r}")
        resources[name.strip()] = value
    models = tuple(model.strip() for model in (args.models or "").split(",") if model.strip())
    if hosted:
        supplied = {
            "--principal": args.principal,
            "--deployment-id": args.deployment_id,
            "--allowed-cwd": args.allowed_cwds,
            "--store": args.store,
        }
        conflicting = [flag for flag, value in supplied.items() if value]
        if conflicting:
            raise _UsageError(
                ", ".join(conflicting)
                + " cannot be set in hosted mode; the server supplies HRNS_PRINCIPAL, "
                "HRNS_DEPLOYMENT_ID, HRNS_ALLOWED_CWDS and HRNS_STORE"
            )
        principal = _require_env("HRNS_PRINCIPAL")
        deployment_id = _require_env("HRNS_DEPLOYMENT_ID")
        store_path = _require_env("HRNS_STORE")
        raw_allowed = _require_env("HRNS_ALLOWED_CWDS")
        try:
            allowed = json.loads(raw_allowed)
        except json.JSONDecodeError as exc:
            raise _UsageError("HRNS_ALLOWED_CWDS must be a JSON array of path strings") from exc
        if not isinstance(allowed, list) or not all(isinstance(item, str) for item in allowed):
            raise _UsageError("HRNS_ALLOWED_CWDS must be a JSON array of path strings")
        allowed_cwds: tuple[str, ...] | None = tuple(allowed)
        local_binding_creation = False
    else:
        principal = args.principal or "local"
        deployment_id = args.deployment_id or "local"
        store_path = args.store
        allowed_cwds = tuple(args.allowed_cwds) if args.allowed_cwds else None
        local_binding_creation = True
    return HostSetup(
        definition=definition,
        store_path=store_path,
        model=args.model,
        models=models,
        resources=resources,
        hosted=hosted,
        principal=principal,
        deployment_id=deployment_id,
        allowed_cwds=allowed_cwds,
        local_binding_creation=local_binding_creation,
        accepts_client_capabilities=args.accept_client_capabilities,
    )


def resolve_builder(module_name: str | None) -> Callable[[HostSetup], HostBundle]:
    if not module_name:
        return build_local_bundle
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise HrnsError(
            "DEPLOYMENT_UNAVAILABLE",
            f"Cannot import runtime module {module_name!r}: {exc}",
            {"module": module_name},
        ) from exc
    builder = getattr(module, "build_host_bundle", None)
    if not callable(builder):
        raise HrnsError(
            "DEPLOYMENT_UNAVAILABLE",
            f"Runtime module {module_name!r} exposes no build_host_bundle(setup) function.",
            {"module": module_name},
        )
    return builder


def build_router(agent: Any, *, use_unstable_protocol: bool = True) -> Any:
    """Build the SDK agent router plus the standard session-delete route."""
    router = build_agent_router(agent, use_unstable_protocol=use_unstable_protocol)
    router.route_request(
        AGENT_METHODS["session_delete"],
        DeleteSessionRequest,
        agent,
        "delete_session",
        adapt_result=normalize_result,
    )
    return router


class _AgentClientChannel:
    """The client-facing half of the SDK connection, over our router."""

    def __init__(self, connection: Connection) -> None:
        self._conn = connection

    async def session_update(
        self, session_id: str, update: acp_map.SessionUpdate, **kwargs: Any
    ) -> None:
        notification = schema.SessionNotification(
            session_id=session_id, update=update, field_meta=kwargs or None
        )
        await notify_model(self._conn, CLIENT_METHODS["session_update"], notification)

    async def request_permission(
        self,
        session_id: str,
        tool_call: schema.ToolCallUpdate,
        options: list[schema.PermissionOption],
        **kwargs: Any,
    ) -> schema.RequestPermissionResponse:
        request = schema.RequestPermissionRequest(
            options=options,
            session_id=session_id,
            tool_call=tool_call,
            field_meta=kwargs or None,
        )
        return await request_model(
            self._conn,
            CLIENT_METHODS["session_request_permission"],
            request,
            schema.RequestPermissionResponse,
        )

    async def read_text_file(
        self,
        session_id: str,
        path: str,
        line: int | None = None,
        limit: int | None = None,
        **kwargs: object,
    ) -> schema.ReadTextFileResponse:
        return await request_model(
            self._conn,
            CLIENT_METHODS["fs_read_text_file"],
            schema.ReadTextFileRequest(
                session_id=session_id, path=path, line=line, limit=limit, field_meta=kwargs or None
            ),
            schema.ReadTextFileResponse,
        )

    async def write_text_file(
        self, session_id: str, path: str, content: str, **kwargs: object
    ) -> schema.WriteTextFileResponse | None:
        return await request_optional_model(
            self._conn,
            CLIENT_METHODS["fs_write_text_file"],
            schema.WriteTextFileRequest(
                session_id=session_id, path=path, content=content, field_meta=kwargs or None
            ),
            schema.WriteTextFileResponse,
        )

    async def create_terminal(
        self,
        session_id: str,
        command: str,
        args: list[str] | None = None,
        env: list[schema.EnvVariable] | None = None,
        cwd: str | None = None,
        output_byte_limit: int | None = None,
        **kwargs: object,
    ) -> schema.CreateTerminalResponse:
        return await request_model(
            self._conn,
            CLIENT_METHODS["terminal_create"],
            schema.CreateTerminalRequest(
                session_id=session_id,
                command=command,
                args=args,
                env=env,
                cwd=cwd,
                output_byte_limit=output_byte_limit,
                field_meta=kwargs or None,
            ),
            schema.CreateTerminalResponse,
        )

    async def terminal_output(
        self, session_id: str, terminal_id: str, **kwargs: object
    ) -> schema.TerminalOutputResponse:
        return await request_model(
            self._conn,
            CLIENT_METHODS["terminal_output"],
            schema.TerminalOutputRequest(
                session_id=session_id, terminal_id=terminal_id, field_meta=kwargs or None
            ),
            schema.TerminalOutputResponse,
        )

    async def wait_for_terminal_exit(
        self, session_id: str, terminal_id: str, **kwargs: object
    ) -> schema.WaitForTerminalExitResponse:
        return await request_model(
            self._conn,
            CLIENT_METHODS["terminal_wait_for_exit"],
            schema.WaitForTerminalExitRequest(
                session_id=session_id, terminal_id=terminal_id, field_meta=kwargs or None
            ),
            schema.WaitForTerminalExitResponse,
        )

    async def kill_terminal(
        self, session_id: str, terminal_id: str, **kwargs: object
    ) -> schema.KillTerminalResponse | None:
        return await request_optional_model(
            self._conn,
            CLIENT_METHODS["terminal_kill"],
            schema.KillTerminalRequest(
                session_id=session_id, terminal_id=terminal_id, field_meta=kwargs or None
            ),
            schema.KillTerminalResponse,
        )

    async def release_terminal(
        self, session_id: str, terminal_id: str, **kwargs: object
    ) -> schema.ReleaseTerminalResponse | None:
        return await request_optional_model(
            self._conn,
            CLIENT_METHODS["terminal_release"],
            schema.ReleaseTerminalRequest(
                session_id=session_id, terminal_id=terminal_id, field_meta=kwargs or None
            ),
            schema.ReleaseTerminalResponse,
        )

    async def ext_method(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        return await self._conn.send_request(f"_{method}", params)

    async def ext_notification(self, method: str, params: dict[str, Any]) -> None:
        await self._conn.send_notification(f"_{method}", params)


async def serve_agent(
    agent: HrnsACPAgent,
    *,
    input_stream: Any = None,
    output_stream: Any = None,
    use_unstable_protocol: bool = True,
) -> None:
    """Serve an agent over stdio (newline-delimited ACP JSON); diagnostics on stderr."""
    await agent.store.backend.initialize()
    if input_stream is None and output_stream is None:
        reader, writer = await stdio_streams(limit=DEFAULT_STDIO_BUFFER_LIMIT_BYTES)
    else:
        writer, reader = input_stream, output_stream
    router = build_router(agent, use_unstable_protocol=use_unstable_protocol)
    connection = Connection(router, writer, reader, listening=False)
    agent.on_connect(_AgentClientChannel(connection))
    try:
        await connection.main_loop()
    finally:
        with contextlib.suppress(Exception):
            await asyncio.shield(connection.close())


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        stream=sys.stderr,
        level=getattr(logging, str(args.log_level).upper(), logging.WARNING),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        setup = setup_from_args(args)
    except _UsageError as exc:
        parser.error(str(exc))
    try:
        bundle = resolve_builder(args.runtime_module)(setup)
        agent = build_agent(bundle, setup)
    except HrnsError as exc:
        print(f"hrns-host: {exc.message}", file=sys.stderr)
        return 2
    try:
        asyncio.run(_serve_until_closed(agent))
    except KeyboardInterrupt:  # pragma: no cover
        return 130
    return 0


async def _serve_until_closed(agent: HrnsACPAgent) -> None:
    try:
        await agent.store.backend.initialize()
        await serve_agent(agent)
    finally:
        await agent.shutdown()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
