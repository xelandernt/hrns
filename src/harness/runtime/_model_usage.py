"""Normalize provider usage before Pydantic AI prices each response."""

from __future__ import annotations

from typing import Any


def normalize_model_usage(model: Any) -> Any:
    """Adapt DeepSeek's cache-hit field; leave other providers to Pydantic AI."""
    if isinstance(model, str):
        if not model.startswith("deepseek:"):
            return model
        from pydantic_ai.models import infer_model

        model = infer_model(model)
    if getattr(model, "system", None) != "deepseek":
        return model
    # OpenAI is an optional runtime extra, so import it only for this provider.
    from openai.types import CompletionUsage
    from openai.types.chat import ChatCompletion, ChatCompletionChunk
    from openai.types.completion_usage import PromptTokensDetails
    from pydantic_ai.models.openai import OpenAIChatModel, OpenAIStreamedResponse
    from pydantic_ai.providers.deepseek import DeepSeekProvider
    from pydantic_ai.usage import RequestUsage

    if type(model) is not OpenAIChatModel:
        return model

    def normalize(response: ChatCompletion | ChatCompletionChunk) -> None:
        usage: CompletionUsage | None = response.usage
        if usage is None:
            return
        extra = usage.model_extra or {}
        hits = extra.get("prompt_cache_hit_tokens")
        if isinstance(hits, int) and 0 <= hits <= usage.prompt_tokens:
            details = usage.prompt_tokens_details or PromptTokensDetails()
            usage.prompt_tokens_details = details.model_copy(update={"cached_tokens": hits})
        # Misses remain ordinary input tokens, never cache writes.

    class DeepSeekStreamedResponse(OpenAIStreamedResponse):
        def _map_usage(self, response: ChatCompletionChunk) -> RequestUsage:
            normalize(response)
            result = super()._map_usage(response)
            if response.usage and response.usage.model_extra:
                misses = response.usage.model_extra.get("prompt_cache_miss_tokens")
                if isinstance(misses, int) and misses >= 0:
                    result.details["cache_miss_tokens"] = misses
            return result

    class DeepSeekChatModel(OpenAIChatModel):
        @property
        def _streamed_response_cls(self) -> type[OpenAIStreamedResponse]:
            return DeepSeekStreamedResponse

        def _map_usage(self, response: ChatCompletion) -> RequestUsage:
            normalize(response)
            result = super()._map_usage(response)
            if response.usage and response.usage.model_extra:
                misses = response.usage.model_extra.get("prompt_cache_miss_tokens")
                if isinstance(misses, int) and misses >= 0:
                    result.details["cache_miss_tokens"] = misses
            return result

    return DeepSeekChatModel(
        model.model_name,
        provider=DeepSeekProvider(openai_client=model.client),
        settings=model.settings,
        profile=model.profile,
    )
