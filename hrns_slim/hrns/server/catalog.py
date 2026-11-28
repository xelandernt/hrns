"""Live model catalog schemas for GitHub Copilot and OpenAI Codex."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Literal

from pydantic import BaseModel

_COPILOT_URL = "https://api.githubcopilot.com"
_COPILOT_VERSION_HEADER = "X-GitHub-Api-Version"
_COPILOT_DEFAULT_VERSION = "2025-04-01"
_COPILOT_EXTENDED_VERSION = "2026-08-01"
_COPILOT_LONG_SUFFIX = "@long-context"
_CopilotEndpoint = Literal["/chat/completions", "/responses"]
# Codex catalog compatibility level, based on minimal_client_version in its bundled catalog.
_CODEX_CATALOG_CLIENT_VERSION = "0.166.0"
_LIVE_CATALOG_PROVIDERS = {"github-copilot", "openai-codex"}


def _context_size_label(size: int) -> str:
    if size >= 1_000_000:
        return f"{size / 1_000_000:g}M"
    if size >= 1_000:
        return f"{size / 1_000:g}K"
    return str(size)


class _CopilotLimits(BaseModel):
    max_context_window_tokens: int | None = None
    max_output_tokens: int | None = None


class _CopilotSupports(BaseModel):
    reasoning_effort: list[str] | None = None


class _CopilotCapabilities(BaseModel):
    limits: _CopilotLimits | None = None
    supports: _CopilotSupports | None = None


class _CopilotPriceTier(BaseModel):
    max_prompt_tokens: int | None = None
    context_max: int | None = None

    def prompt_limit(self) -> int:
        return self.max_prompt_tokens or self.context_max or 0


class _CopilotTokenPrices(BaseModel):
    default: _CopilotPriceTier | None = None
    long_context: _CopilotPriceTier | None = None


class _CopilotBilling(BaseModel):
    token_prices: _CopilotTokenPrices | None = None


class _CopilotModel(BaseModel):
    id: str
    supported_endpoints: list[str] = []
    capabilities: _CopilotCapabilities | None = None
    billing: _CopilotBilling | None = None

    def reasoning_efforts(self) -> Sequence[str]:
        supports = self.capabilities.supports if self.capabilities else None
        return (supports.reasoning_effort or ()) if supports else ()

    def tier_prompt_limits(self) -> tuple[int, int] | None:
        prices = self.billing.token_prices if self.billing else None
        if prices and prices.default and prices.long_context:
            default, extended = prices.default.prompt_limit(), prices.long_context.prompt_limit()
            if 0 < default < extended:
                return default, extended
        return None

    def context_window(self, *, extended: bool = False) -> int | None:
        limits = self.capabilities.limits if self.capabilities else None
        size = limits.max_context_window_tokens if limits else None
        if tiers := self.tier_prompt_limits():
            default_prompt, extended_prompt = tiers
            prompt = extended_prompt if extended else default_prompt
            output = max(limits.max_output_tokens or 0, 0) if limits else 0
            tier_window = prompt + output
            return min(tier_window, size) if size and size > 0 else tier_window
        return size if size is not None and size > 0 else None

    def endpoint(self) -> _CopilotEndpoint | None:
        if "/chat/completions" in self.supported_endpoints:
            return "/chat/completions"
        if "/responses" in self.supported_endpoints:
            return "/responses"
        return None


class _CopilotCatalog(BaseModel):
    data: list[_CopilotModel]


class _CodexCatalogModel(BaseModel):
    slug: str
    visibility: str
    priority: int


class _CodexCatalog(BaseModel):
    models: list[_CodexCatalogModel]
