# Release maintenance

## Reasoning effort

Review the reasoning-effort rules before each release and when upgrading Pydantic AI
or its provider SDKs. Update the code when a provider changes its supported levels,
model IDs, catalog format, or request format.

| Code | What to review |
| --- | --- |
| [Native provider rules](hrns_slim/hrns/server/reasoning.py) | Check the OpenAI, Codex, and Gemini model tables, the DeepSeek levels, and the Claude rules for Anthropic and Bedrock. Check model aliases, endpoint differences, and whether the route can actually disable thinking. |
| [Copilot catalog handling](hrns_slim/hrns/server/runtime.py) | Copilot supplies its own levels in `capabilities.supports.reasoning_effort`. Update the parser if the catalog changes. Both context tiers use the same model entry. |
| [Shared effort values](hrns_slim/hrns/shared/config/settings.py) | Update `EffortLevel` and the [configuration reference](docs/configuration.md#thinking) if Harness adds an effort value. `THINKING_LEVELS` derives from `EffortLevel`. |

Use the provider's documented levels for the exact model and endpoint. Check how the
Pydantic AI version in `uv.lock` serializes each provider setting. Its unified
`thinking` setting can map multiple choices to the same value. A route whose levels
cannot be verified uses the provider default.

- [OpenAI model reference](https://developers.openai.com/api/docs/models) and [Codex model catalog](https://github.com/openai/codex/blob/main/codex-rs/models-manager/models.json)
- [Gemini thinking levels](https://aistudio.google.com/docs/thinking#understanding-thinking-levels) and [DeepSeek thinking mode](https://api-docs.deepseek.com/guides/thinking_mode/)
- [Anthropic effort](https://platform.claude.com/docs/en/build-with-claude/effort) and [Bedrock adaptive thinking](https://docs.aws.amazon.com/bedrock/latest/userguide/claude-messages-adaptive-thinking.html)
- [Pydantic AI thinking](https://pydantic.dev/docs/ai/capabilities/thinking/) and [GitHub Copilot in Pydantic AI](https://pydantic.dev/docs/ai/models/github-copilot/#thinking)

Run `just lint` and `just build`. For changed routes, check that an advertised effort
reaches the provider with the intended value and that an unsupported effort from ACP
or `[model_settings].thinking` fails before inference. Check that the TUI skips an
unsupported saved preference. If catalog or routing behavior changes, check that a
mounted ACP client receives the updated choices after a route change. Record which
checks used a live provider and which used captured requests or mock metadata.
