"""Host support types, policy, composed-agent bridge, and store selection."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic_ai import RunContext
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.messages import ModelResponse
from pydantic_ai.models import ModelRequestContext
from pydantic_ai.settings import ModelSettings

from hrns.protocol import EXTENSION_METHODS, HrnsError

from . import acp as acp_map
from .acp_store import SessionStore
from .composition import Composition
from .definitions import AgentDefinition
from .registry import PluginRegistry
from .resources import MODEL, ResourceBindings, ResourceSet

if TYPE_CHECKING:
    from .host import HrnsACPAgent

_EXTENSION_METHODS = tuple(EXTENSION_METHODS)


@dataclass(frozen=True, slots=True)
class HostSetup:
    """Everything the CLI (or a worker supervisor) tells the runtime builder."""

    definition: str
    store_path: str | None = None
    model: str | None = None
    models: tuple[str, ...] = ()
    resources: Mapping[str, str] = field(default_factory=dict)
    hosted: bool = False
    principal: str = "local"
    deployment_id: str = "local"
    allowed_cwds: tuple[str, ...] | None = None
    local_binding_creation: bool = True

    @property
    def advertise_local_binding_creation(self) -> bool:
        return self.local_binding_creation and not self.hosted

    def advertised_models(self) -> tuple[str, ...]:
        values = list(dict.fromkeys(model for model in self.models if model))
        if self.model and self.model not in values:
            values.insert(0, self.model)
        return tuple(values)

    def default_model(self) -> str | None:
        values = self.advertised_models()
        return self.model or (values[0] if values else None)


@dataclass
class HostBundle:
    """The pieces a host process serves: one definition and one durable store."""

    definition: AgentDefinition
    registry: PluginRegistry
    store: SessionStore
    close_store: bool = False
    model_resolver: Callable[[str], Any] | None = None


@dataclass(frozen=True, slots=True)
class HostPolicy:
    """Host-owned policy: identity, workspace bounds, and model selection."""

    principal: str = "local"
    deployment_id: str = "local"
    hosted: bool = False
    allowed_cwds: tuple[str, ...] | None = None
    allow_local_binding_creation: bool = True
    models: tuple[str, ...] = ()
    default_model: str | None = None
    thinking_models: Mapping[str, str] = field(default_factory=dict)
    default_effort: str | None = None
    model_settings: ModelSettings = field(default_factory=ModelSettings)
    resources: ResourceSet | ResourceBindings = field(default_factory=dict)
    default_cwd: str | None = None

    def __post_init__(self) -> None:
        roots = self.allowed_cwds
        if roots is not None:
            object.__setattr__(
                self, "allowed_cwds", tuple(acp_map.canonical_root(root) for root in roots)
            )
        if self.hosted and self.allowed_cwds is None:
            raise HrnsError(
                "DEPLOYMENT_UNAVAILABLE",
                "A hosted worker requires HRNS_ALLOWED_CWDS (a JSON list, possibly empty).",
            )

    @property
    def can_create_bindings(self) -> bool:
        return self.allow_local_binding_creation and not self.hosted

    def extension_methods(self) -> tuple[str, ...]:
        if self.can_create_bindings:
            return _EXTENSION_METHODS
        return tuple(method for method in _EXTENSION_METHODS if method != "_hrns/plugins/list")

    def effort_support(self, model: str | None) -> str | None:
        return self.thinking_models.get(model) if model else None

    def validate_cwd(self, cwd: Any) -> str:
        return acp_map.validate_requested_cwd(cwd, allowed=self.allowed_cwds, hosted=self.hosted)


@dataclass(frozen=True)
class SessionDeps:
    """Dependencies that let the adapter dispatch a run to this session's native agent."""

    session_id: str
    composition: Composition


@dataclass
class _UsageReporting(AbstractCapability[Any]):
    """Publish measured usage after each response, before tools continue the turn."""

    report: Callable[[ModelResponse], Awaitable[None]]

    async def after_model_request(
        self,
        ctx: RunContext[Any],
        *,
        request_context: ModelRequestContext,
        response: ModelResponse,
    ) -> ModelResponse:
        await self.report(response)
        return response


class _ComposedAgent:
    """A narrow agent-shaped dispatcher over one native agent per session.

    The upstream adapter owns one agent instance for a connection, while a session's
    definition factory must be constructed with that session's complete managed
    capabilities. The adapter already passes per-session ``deps`` to every run, so this
    wrapper selects the already-composed native agent from those deps. It does not run
    turns itself.
    """

    def __init__(
        self, *, name: str, report_usage: Callable[[str, ModelResponse], Awaitable[None]]
    ) -> None:
        self.name = name
        self._report_usage = report_usage
        self.output_type = str
        self.toolsets: list[Any] = []

    @staticmethod
    def _select(deps: Any) -> Any:
        if not isinstance(deps, SessionDeps):
            raise RuntimeError("ACP run without a composed session dependency")
        return deps.composition.agent

    def _capabilities(self, deps: SessionDeps) -> list[AbstractCapability[Any]]:
        async def report(response: ModelResponse) -> None:
            await self._report_usage(deps.session_id, response)

        return [*(deps.composition.config.capabilities or ()), _UsageReporting(report=report)]

    def run_stream_events(self, *args: Any, **kwargs: Any) -> Any:
        deps = kwargs.get("deps")
        composition = deps.composition if isinstance(deps, SessionDeps) else None
        target = composition.agent if composition is not None else self._select(deps)
        if isinstance(deps, SessionDeps) and composition is not None:
            kwargs["deps"] = composition.deps
            kwargs["capabilities"] = self._capabilities(deps)
            kwargs["toolsets"] = composition.config.toolsets
            kwargs["workspace"] = composition.config.workspace
        return target.run_stream_events(*args, **kwargs)

    async def run(self, *args: Any, **kwargs: Any) -> Any:
        deps = kwargs.get("deps")
        composition = deps.composition if isinstance(deps, SessionDeps) else None
        target = composition.agent if composition is not None else self._select(deps)
        if isinstance(deps, SessionDeps) and composition is not None:
            kwargs["deps"] = composition.deps
            kwargs["capabilities"] = self._capabilities(deps)
            kwargs["toolsets"] = composition.config.toolsets
            kwargs["workspace"] = composition.config.workspace
        return await target.run(*args, **kwargs)


def _iso_millis(value: int | None) -> str | None:
    if not isinstance(value, int):
        return None
    try:
        return datetime.fromtimestamp(value / 1000, tz=timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):  # pragma: no cover - defensive
        return None


def _prompt_text(prompt: Sequence[Any]) -> str:
    parts: list[str] = []
    for block in prompt:
        text = getattr(block, "text", None)
        if isinstance(text, str) and text:
            parts.append(text)
            continue
        if isinstance(block, Mapping):
            value = block.get("text")
            if isinstance(value, str) and value:
                parts.append(value)
    return " ".join(" ".join(parts).split())[:80]


def build_store(configured: str | Path | None) -> SessionStore:
    """Select SQLite or PostgreSQL from a path/DSN without an implicit fallback."""
    from .postgres import PostgresSessionStore, is_postgres_dsn
    from .store import SQLiteSessionStore

    value = str(configured) if configured is not None else None
    if value and is_postgres_dsn(value):
        return PostgresSessionStore(value)
    return SQLiteSessionStore(value) if value else SQLiteSessionStore()


def build_agent(bundle: HostBundle, setup: HostSetup | None = None) -> HrnsACPAgent:
    """Compose the ACP agent and host policy from a runtime bundle."""
    from .host import HrnsACPAgent
    from .local import thinking_support

    models = setup.advertised_models() if setup else ()
    resources = ResourceSet(setup.resources if setup else None)
    if setup and setup.model:
        resources.bind_if_absent(MODEL, setup.model)
    policy = HostPolicy(
        principal=setup.principal if setup else "local",
        deployment_id=setup.deployment_id if setup else "local",
        hosted=setup.hosted if setup else False,
        allowed_cwds=setup.allowed_cwds if setup else None,
        allow_local_binding_creation=setup.advertise_local_binding_creation if setup else True,
        models=models,
        default_model=(setup.default_model() if setup else None),
        thinking_models=thinking_support(models),
        resources=resources,
    )
    return HrnsACPAgent(bundle, policy)
