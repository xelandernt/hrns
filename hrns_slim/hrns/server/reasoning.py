"""Verified reasoning effort settings for the built-in model resolver."""

from __future__ import annotations

import os
import re
from collections.abc import Collection, Mapping
from typing import TYPE_CHECKING

from pydantic_ai.models import known_model_names
from pydantic_ai.models.anthropic import AnthropicModelSettings
from pydantic_ai.models.bedrock import BedrockModelSettings
from pydantic_ai.models.openai import OpenAIChatModelSettings
from pydantic_ai.profiles.anthropic import AnthropicEffort, anthropic_model_profile
from pydantic_ai.settings import ModelSettings

from hrns.shared.config import EFFORT_OFF, THINKING_LEVELS

if TYPE_CHECKING:
    from pydantic_ai.models.google import GoogleModelSettings

_STANDARD = ("low", "medium", "high")
_EXTENDED = (*_STANDARD, "xhigh")
_MAX = (*_EXTENDED, "max")
_OPENAI_PROVIDERS = frozenset({"openai", "openai-chat", "openai-responses"})
_NATIVE_URLS = {
    **dict.fromkeys(_OPENAI_PROVIDERS, "https://api.openai.com/v1"),
    "anthropic": "https://api.anthropic.com",
    "deepseek": "https://api.deepseek.com",
    "google": "https://generativelanguage.googleapis.com",
}
_BASE_URL_ENV = {
    **dict.fromkeys(_OPENAI_PROVIDERS, "OPENAI_BASE_URL"),
    "anthropic": "ANTHROPIC_BASE_URL",
    "google": "GOOGLE_GEMINI_BASE_URL",
}
_OPENAI_MODELS = {
    # https://developers.openai.com/api/docs/models
    "gpt-6.2-astra": _MAX,
    "gpt-6.2-sol": _MAX,
    "gpt-6.1-sol": _MAX,
    "gpt-6.1-terra": (*_MAX, "none"),
    "gpt-6-astra": _MAX,
    "gpt-6-sol": (*_MAX, "none"),
    "gpt-6-luna": (*_MAX, "none"),
    "gpt-5.6": (*_MAX, "none"),
    "gpt-5.6-sol": (*_MAX, "none"),
    "gpt-5.6-terra": (*_MAX, "none"),
    "gpt-5.6-luna": (*_MAX, "none"),
    "gpt-5.5": (*_EXTENDED, "none"),
    "gpt-5.4": (*_EXTENDED, "none"),
    "gpt-5.4-mini": (*_EXTENDED, "none"),
    "gpt-5.4-nano": (*_EXTENDED, "none"),
    "gpt-5.3-codex": _EXTENDED,
    "gpt-5.2": (*_EXTENDED, "none"),
    "gpt-5.1": (*_STANDARD, "none"),
    "gpt-5": ("minimal", *_STANDARD),
    "gpt-5-mini": ("minimal", *_STANDARD),
    "gpt-5-nano": ("minimal", *_STANDARD),
    "o3": _STANDARD,
    "o3-mini": _STANDARD,
    "o4-mini": _STANDARD,
}
_GOOGLE_MODELS = {
    # https://aistudio.google.com/docs/thinking#thinking-levels
    "gemini-4-carbon": _STANDARD,
    "gemini-4-argon": _STANDARD,
    "gemini-4-hydrogen": _EXTENDED,
    "gemini-4-neon": _EXTENDED,
    "gemini-3.8-flash": _STANDARD,
    "gemini-3.7-flash": _STANDARD,
    "gemini-3.6-flash": ("minimal", *_STANDARD),
    "gemini-3.5-flash": ("minimal", *_STANDARD),
    "gemini-3.5-flash-lite": ("minimal", *_STANDARD),
    "gemini-3.1-pro-preview": _STANDARD,
    "gemini-3.1-flash-lite-image": ("minimal", "high"),
    "gemini-3-flash-preview": ("minimal", *_STANDARD),
    "gemini-3-pro-preview": ("low", "high"),
}
_CODEX_MODELS = {
    # https://github.com/openai/codex/blob/main/codex-rs/models-manager/models.json
    **{model: _MAX for model in _OPENAI_MODELS if model.startswith(("gpt-6", "gpt-5.6-"))},
    "gpt-5.5": _EXTENDED,
}
_CLAUDE_MODELS = frozenset(
    model.removeprefix("anthropic:")
    for model in known_model_names()
    if model.startswith("anthropic:")
) | {
    # SDK suggestions omit older models that still support effort on first-party routes.
    "claude-fable-5",
    "claude-fable-5-1",
    "claude-fable-5-2",
    "claude-mythos-5",
    "claude-mythos-5-1",
    "claude-mythos-5-2",
    "claude-opus-4-5",
    "claude-opus-4-6",
    "claude-opus-4-7",
    "claude-opus-5",
    "claude-sonnet-5",
}


def openai_efforts(levels: Collection[str]) -> dict[str, ModelSettings]:
    """Translate effort declarations on an OpenAI-shaped route without remapping levels."""
    settings: dict[str, ModelSettings] = {}
    for level in THINKING_LEVELS:
        if level in levels:
            settings[level] = OpenAIChatModelSettings(openai_reasoning_effort=level)
    if "none" in levels:
        settings[EFFORT_OFF] = OpenAIChatModelSettings(openai_reasoning_effort="none")
    return settings


def _claude_efforts(
    model: str, *, bedrock: bool, model_settings: Mapping[str, object]
) -> dict[str, ModelSettings]:
    # https://platform.claude.com/docs/en/build-with-claude/effort
    # https://docs.aws.amazon.com/bedrock/latest/userguide/claude-messages-adaptive-thinking.html
    profile = anthropic_model_profile(model) if model in _CLAUDE_MODELS else None
    if profile is None or not profile.get("anthropic_supports_effort"):
        return {}
    if bedrock and not profile.get("anthropic_supports_adaptive_thinking"):
        return {}
    levels: tuple[AnthropicEffort, ...] = ("low", "medium", "high")
    if profile.get("anthropic_supports_xhigh_effort"):
        levels += ("xhigh",)
    if model != "claude-opus-4-5":
        levels += ("max",)

    if bedrock:
        configured = model_settings.get("bedrock_additional_model_requests_fields")
        fields = dict(configured) if isinstance(configured, Mapping) else {}
        configured_output = fields.get("output_config")
        output = dict(configured_output) if isinstance(configured_output, Mapping) else {}
        output.pop("effort", None)
        settings: dict[str, ModelSettings] = {
            level: BedrockModelSettings(
                bedrock_additional_model_requests_fields={
                    **fields,
                    "thinking": {"type": "adaptive"},
                    "output_config": {**output, "effort": level},
                }
            )
            for level in levels
        }
        if not profile.get("thinking_always_enabled"):
            fields.pop("output_config", None)
            if output:
                fields["output_config"] = output
            settings[EFFORT_OFF] = BedrockModelSettings(
                bedrock_additional_model_requests_fields={
                    **fields,
                    "thinking": {"type": "disabled"},
                }
            )
    else:
        adaptive = profile.get("anthropic_supports_adaptive_thinking", False)
        settings = {
            level: (
                AnthropicModelSettings(thinking=True, anthropic_effort=level)
                if adaptive
                else AnthropicModelSettings(anthropic_effort=level)
            )
            for level in levels
        }
        if not profile.get("thinking_always_enabled"):
            settings[EFFORT_OFF] = AnthropicModelSettings(anthropic_thinking={"type": "disabled"})
    return settings


def native_efforts(
    provider: str,
    model_name: str,
    base_url: str,
    options: Mapping[str, object],
    model_settings: Mapping[str, object],
) -> dict[str, ModelSettings]:
    """Return the documented levels for the first-party API and model."""
    # An SDK client or HTTP endpoint can carry a different catalog under the same ids.
    if any(options.get(key) is not None for key in ("openai_client", "anthropic_client")):
        return {}
    env = _BASE_URL_ENV.get(provider)
    endpoint = base_url or options.get("base_url") or (os.getenv(env) if env else None)
    if endpoint and (
        not isinstance(endpoint, str) or endpoint.rstrip("/") != _NATIVE_URLS.get(provider)
    ):
        return {}
    if provider == "bedrock":
        if options.get("bedrock_client") or any(
            os.getenv(key) for key in ("AWS_ENDPOINT_URL_BEDROCK_RUNTIME", "AWS_ENDPOINT_URL")
        ):
            return {}
        match = re.fullmatch(
            r"(?:[a-z]+\.)?anthropic\.(claude-[\w-]+?)(?:-v\d+(?::\d+)?)?", model_name
        )
        if match is None:
            return {}
        model_name = match[1]
    model = re.sub(r"-(?:\d{4}-\d{2}-\d{2}|\d{8})$", "", model_name)
    if provider in _OPENAI_PROVIDERS:
        levels = _OPENAI_MODELS.get(model, ())
        return openai_efforts(set(levels) - {"max"} if provider == "openai-chat" else levels)
    if provider == "openai-codex":
        return openai_efforts(_CODEX_MODELS.get(model, ()))
    if provider == "deepseek" and model in {
        "deepseek-flash",
        "deepseek-v4-flash",
        "deepseek-v4-pro",
    }:
        # https://api-docs.deepseek.com/guides/thinking_mode/
        return openai_efforts(("low", "high", "max", "none"))
    if provider in {"anthropic", "bedrock"}:
        return _claude_efforts(model, bedrock=provider == "bedrock", model_settings=model_settings)
    if provider == "google":
        settings: dict[str, ModelSettings] = {}
        for level in _GOOGLE_MODELS.get(model, ()):
            google_settings: GoogleModelSettings = {
                "google_thinking_config": {
                    "include_thoughts": True,
                    "thinking_level": level.upper(),
                }
            }
            settings[level] = google_settings
        return settings
    return {}
