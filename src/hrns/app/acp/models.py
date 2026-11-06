"""The single object the adapter asks about models and client-managed provider routing.

ACP has no model methods: models are a session config option, and providers are three
methods that reroute them. ``ModelResolver`` is where those meet. The adapter asks it what
models exist, how one resolves for a run, and which providers a client may reroute; the
resolver owns the state behind all three, so a ``providers/set`` it accepts changes what
the next ``resolve`` builds.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from pydantic_ai.models import Model
from pydantic_ai.settings import ModelSettings

from .providers import InMemoryProviderRegistry, ProviderConfig, ProviderDescriptor

__all__ = ["ModelChoice", "ModelResolver", "ProviderModelResolver", "ResolvedModel"]


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


class ModelResolver(Protocol):
    """Models, their resolution, and client-managed provider routing.

    ``list_models`` feeds the ``model`` session config option and validates what a client
    may select. ``resolve`` turns that selection into a runnable model; it must consult
    the same provider state that ``set_provider`` writes, or a configured route would be a
    lie. The three provider methods back ``providers/list``, ``providers/set``, and
    ``providers/disable``: ``set_provider`` raises ``ValueError`` for a provider or API
    type it does not accept, and ``disable_provider`` raises it for a ``required``
    provider. ``resolve`` raises ``ProviderDisabled`` when the model's provider is
    disabled.
    """

    def list_models(self) -> Sequence[ModelChoice]: ...

    def resolve(self, model_id: str) -> ResolvedModel: ...

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

    @abstractmethod
    def build_model(self, model_id: str, config: ProviderConfig | None) -> ResolvedModel:
        """Build ``model_id`` through its provider.

        ``config`` is the client's routing for that provider, or ``None`` when the client
        never configured one and the resolver's own default stands.
        """
