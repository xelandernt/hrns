"""The single object the adapter asks about models, settings, and provider routing.

ACP has no model methods: models and model settings are session config options, and
providers are three methods that reroute them. ``ModelResolver`` is where those meet. The
adapter asks it what models exist, how one resolves for a run, which option values a
client may pick, and how those values become ``ModelSettings``; the resolver owns the
state behind all of it, so a ``providers/set`` it accepts changes what the next
``resolve`` builds and can narrow what the next ``list_options`` advertises.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from pydantic_ai.models import Model
from pydantic_ai.settings import ModelSettings

from .providers import InMemoryProviderRegistry, ProviderConfig, ProviderDescriptor

__all__ = [
    "ModelChoice",
    "ModelResolver",
    "ProviderModelResolver",
    "ResolvedModel",
    "SettingChoice",
    "SettingOption",
]


@dataclass(frozen=True, kw_only=True)
class ModelChoice:
    """One value of the ``model`` session config option.

    ``provider_id`` names the provider this model is built through. Every model has one —
    Pydantic AI cannot construct a model without a provider — so the resolver must name
    it; the adapter reports it as ``_meta.providerId`` so a client can group models by
    provider. The id may name a provider that ``list_providers()`` does not expose for
    configuration.
    """

    model_id: str
    provider_id: str
    name: str | None = None


@dataclass(frozen=True, kw_only=True)
class ResolvedModel:
    """The model a resolver picked for one run, plus the settings that choice requires.

    A resolver returns this instead of a bare model because client-configured provider
    routing carries headers, which reach a request as model settings rather than as
    provider constructor arguments. ``settings`` are merged over the agent's own.
    """

    model: Model | str
    settings: ModelSettings | None = None


@dataclass(frozen=True, kw_only=True)
class SettingChoice:
    """One value a setting option offers.

    ``is_default`` marks the value a session starts with. Exactly one choice of an option
    carries it, so the default is an element of the list rather than a second copy of one:
    there is no way to describe a default the client cannot select.

    ``meta`` is reported as the value's ``_meta``. A client that understands a key can
    render it richer (a range, a unit) while a client that does not still shows ``name``.
    """

    value: str
    name: str
    description: str | None = None
    is_default: bool = False
    meta: Mapping[str, object] = field(default_factory=dict[str, object])


@dataclass(frozen=True, kw_only=True)
class SettingOption:
    """A session config option other than the model: any model setting a client may change.

    The resolver advertises these and turns the client's choices back into
    ``ModelSettings``. An option is a public contract, not a mirror of ``ModelSettings``:
    ``id`` is stable across runs, ``choices`` are exactly the values a client may set, and
    the option must never expose a credential-bearing setting such as ``extra_headers`` or
    a provider key. The adapter sends the whole option to the client and merges the delta
    from ``ModelResolver.resolve_settings`` into every request.

    ``category`` follows ACP's reserved names when one fits (``thought_level``,
    ``model_config``, ``mode``); ``_``-prefixed names are free for custom use. Mark exactly
    one choice with ``is_default``; ``default`` reads it back, so the value a client starts
    from always belongs to the list it renders.
    """

    id: str
    name: str
    choices: Sequence[SettingChoice]
    category: str | None = None
    description: str | None = None
    meta: Mapping[str, object] = field(default_factory=dict[str, object])

    def __post_init__(self) -> None:
        """Freeze the choices and reject a list with no unambiguous default."""
        object.__setattr__(self, "choices", tuple(self.choices))
        values = [choice.value for choice in self.choices]
        if not values:
            raise ValueError(f"setting option {self.id!r} offers no choices")
        if len(set(values)) != len(values):
            raise ValueError(f"setting option {self.id!r} repeats a choice value")
        if sum(choice.is_default for choice in self.choices) != 1:
            raise ValueError(
                f"setting option {self.id!r} must mark exactly one choice as the default"
            )

    @property
    def default(self) -> str:
        """The value the session uses until the client selects one."""
        return next(choice.value for choice in self.choices if choice.is_default)


class ModelResolver(Protocol):
    """Models, their settings, and client-managed provider routing.

    ``list_models`` feeds the ``model`` session config option and validates what a client
    may select. ``resolve`` turns that selection into a runnable model; it must consult
    the same provider state that ``set_provider`` writes, or a configured route would be a
    lie. ``list_options`` and ``resolve_settings`` are the other half of that surface:
    the first advertises the session settings this deployment allows for a model, the
    second turns a session's setting selections into the settings delta every request carries.

    ``resolve_settings`` runs before each model request, so it must stay cheap: no model
    construction, no network. It returns a delta, never a complete ``ModelSettings`` — the
    adapter merges it over the model's and agent's settings, so a resolver cannot drop
    routing headers or agent defaults, and must raise ``ValueError`` for a selection it
    cannot apply.

    The three provider methods back ``providers/list``, ``providers/set``, and
    ``providers/disable``: ``set_provider`` raises ``ValueError`` for a provider or API
    type it does not accept, and ``disable_provider`` raises it for a ``required``
    provider. ``resolve`` raises ``ProviderDisabled`` when the model's provider is
    disabled.
    """

    def list_models(self) -> Sequence[ModelChoice]: ...

    def resolve(self, model_id: str) -> ResolvedModel: ...

    def list_options(self, model_id: str) -> Sequence[SettingOption]: ...

    def resolve_settings(
        self, model_id: str, setting_selections: Mapping[str, str]
    ) -> ModelSettings: ...

    def list_providers(self) -> Sequence[ProviderDescriptor]: ...

    def set_provider(
        self,
        provider_id: str,
        api_type: str,
        base_url: str,
        headers: Mapping[str, str],
    ) -> None: ...

    def disable_provider(self, provider_id: str) -> None: ...


class ProviderModelResolver(InMemoryProviderRegistry, ModelResolver, ABC):
    """Resolves every model through the provider it is advertised with.

    A model cannot exist without a provider, so this base class makes that link
    structural rather than trust: ``resolve`` looks the model up in ``list_models``,
    reads that choice's provider config, and hands both to ``build_model``. A subclass
    implements the catalog and the model construction, and cannot build a model through
    some other provider; ``current_config`` raises ``ProviderDisabled`` when the client
    disabled the provider, before any model is built.
    """

    def resolve(self, model_id: str) -> ResolvedModel:
        choice = next(
            (choice for choice in self.list_models() if choice.model_id == model_id), None
        )
        if choice is None:
            raise ValueError(f"Model {model_id!r} is not advertised.")
        return self.build_model(model_id, self.current_config(choice.provider_id))

    @abstractmethod
    def list_models(self) -> Sequence[ModelChoice]: ...

    def list_options(self, model_id: str) -> Sequence[SettingOption]:
        """Advertise no settings beyond the model; a resolver with knobs overrides this."""
        return ()

    def resolve_settings(
        self, model_id: str, setting_selections: Mapping[str, str]
    ) -> ModelSettings:
        """Return no settings delta; a resolver that advertises options overrides this."""
        return ModelSettings()

    @abstractmethod
    def build_model(self, model_id: str, config: ProviderConfig | None) -> ResolvedModel:
        """Build ``model_id`` through its provider.

        ``config`` is the client's routing for that provider, or ``None`` when the client
        never configured one and the resolver's own default stands.
        """
