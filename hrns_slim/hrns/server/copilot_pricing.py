"""Published GitHub Copilot rates for Gemini 4 Carbon."""

from datetime import UTC, date, datetime
from decimal import Decimal

from openai.types.chat import ChatCompletion, ChatCompletionChunk
from pydantic_ai.models.github_copilot import GitHubCopilotModel
from pydantic_ai.models.openai import OpenAIStreamedResponse
from pydantic_ai.usage import RequestUsage

_PROMOTION_END = date(2026, 12, 1)


def _add_carbon_estimate(usage: RequestUsage) -> RequestUsage:
    if usage.cost is not None or not usage.total_tokens:
        return usage
    # https://docs.github.com/en/copilot/reference/copilot-billing/models-and-pricing#footnotes
    cached = min(usage.cache_read_tokens, usage.input_tokens)
    uncached = usage.input_tokens - cached
    rates = (Decimal(2), Decimal("0.10"), Decimal(10))
    if datetime.now(UTC).date() >= _PROMOTION_END:
        rates = (Decimal(4), Decimal("0.20"), Decimal(20))
    input_rate, cached_rate, output_rate = rates
    usage.cost = (
        uncached * input_rate + cached * cached_rate + usage.output_tokens * output_rate
    ) / 1_000_000
    return usage


class _CarbonStream(OpenAIStreamedResponse):
    def _map_usage(self, response: ChatCompletionChunk) -> RequestUsage:
        return _add_carbon_estimate(super()._map_usage(response))


class CopilotCarbonModel(GitHubCopilotModel):
    """Apply GitHub's dated estimate before Pydantic AI aggregates request usage."""

    @property
    def _streamed_response_cls(self) -> type[OpenAIStreamedResponse]:
        return _CarbonStream

    def _map_usage(self, response: ChatCompletion) -> RequestUsage:
        return _add_carbon_estimate(super()._map_usage(response))
