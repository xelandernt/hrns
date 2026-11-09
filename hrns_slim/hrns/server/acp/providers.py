"""Pluggable LLM-provider configuration for ACP's ``providers/*`` methods.

A client uses ``providers/list`` to discover which providers it may route, then
``providers/set`` to point one at its own gateway and ``providers/disable`` to stop
routing through it. The adapter owns none of that state: it forwards to a registry the
caller supplies, the same way it forwards session persistence to a ``SessionStore``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Protocol, runtime_checkable


@dataclass(frozen=True, kw_only=True)
class ProviderRoute:
    """The non-secret routing ``providers/list`` reports for one provider."""

    api_type: str
    base_url: str


@dataclass(frozen=True, kw_only=True)
class ProviderConfig:
    """One provider's client-supplied configuration.

    ``headers`` may carry secrets, so it is never echoed back by ``providers/list``.
    """

    route: ProviderRoute
    headers: dict[str, str] = field(default_factory=dict[str, str])


@dataclass(frozen=True, kw_only=True)
class ProviderDescriptor:
    """One provider as ``providers/list`` reports it."""

    provider_id: str
    supported: Sequence[str]
    required: bool = False
    # ``None`` means the client disabled this provider; it must not be used for LLM calls.
    current: ProviderRoute | None = None


class ProviderDisabled(ValueError):
    """Raised when a model is resolved through a provider the client disabled."""


@runtime_checkable
class ProviderRegistry(Protocol):
    """Where a resolver reads and writes client-managed provider routing.

    ``set_provider`` raises ``ValueError`` for configuration the registry rejects; the
    adapter translates that into ``invalid_params``. ``disable_provider`` treats an
    unknown id as success and raises ``ValueError`` for a ``required`` provider.
    Implementations do not need to translate their own errors into ACP errors.

    ``current_config`` returns the client's configuration for a provider, ``None`` when
    the client never configured it (so the caller keeps its own defaults), and raises
    :class:`ProviderDisabled` when the client disabled it.
    """

    def list_providers(self) -> Sequence[ProviderDescriptor]: ...

    def set_provider(
        self, provider_id: str, api_type: str, base_url: str, headers: Mapping[str, str]
    ) -> None: ...

    def disable_provider(self, provider_id: str) -> None: ...

    def current_config(self, provider_id: str) -> ProviderConfig | None: ...


class InMemoryProviderRegistry(ProviderRegistry):
    """Process-local registry seeded with the providers the agent already knows.

    Configuration lives for the life of the process only, as the ACP RFD requires; it is
    never written to disk and never persisted with a session.
    """

    def __init__(self, providers: Sequence[ProviderDescriptor] | None = None) -> None:
        self._providers: dict[str, ProviderDescriptor] = {
            provider.provider_id: provider for provider in providers or ()
        }
        self._configs: dict[str, ProviderConfig] = {}

    def list_providers(self) -> Sequence[ProviderDescriptor]:
        return tuple(self._providers.values())

    def set_provider(
        self, provider_id: str, api_type: str, base_url: str, headers: Mapping[str, str]
    ) -> None:
        provider = self._providers.get(provider_id)
        if provider is None:
            raise ValueError(f"provider {provider_id!r} is not configurable")
        route = ProviderRoute(api_type=api_type, base_url=base_url)
        if route.api_type not in provider.supported:
            raise ValueError(
                f"provider {provider_id!r} does not support api type {route.api_type!r}"
            )
        self._providers[provider_id] = replace(provider, current=route)
        self._configs[provider_id] = ProviderConfig(route=route, headers=dict(headers))

    def disable_provider(self, provider_id: str) -> None:
        provider = self._providers.get(provider_id)
        if provider is None:
            return
        if provider.required:
            raise ValueError(f"provider {provider_id!r} is required and cannot be disabled")
        self._providers[provider_id] = replace(provider, current=None)
        self._configs.pop(provider_id, None)

    def current_config(self, provider_id: str) -> ProviderConfig | None:
        provider = self._providers.get(provider_id)
        if provider is None:
            return None
        if provider.current is None:
            raise ProviderDisabled(f"Provider {provider_id!r} was disabled by the client.")
        return self._configs.get(provider_id)
