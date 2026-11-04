"""Mount ACP on a FastAPI app.

Run with:

    DEEPSEEK_API_KEY=... uvicorn examples.fastapi_app:app --reload

Sessions default to ``deepseek-flash`` and can switch to ``deepseek-pro`` over
ACP. Provider keys stay server-side (read from ``DEEPSEEK_API_KEY``); the
client never sends an API key with a prompt.
"""

from __future__ import annotations

from fastapi import FastAPI, WebSocket
from pydantic_ai.agent import AbstractAgent, Agent
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.deepseek import DeepSeekProvider

from hrns.app.acp import InMemorySessionStore
from hrns.app.fastapi import mount_acp

# ACP model switching is opt-in: the first id is each session's default. ACP
# advertises string ids, so keep the prebuilt models in a mapping and resolve a
# client's selection to the matching model with ``model_resolver`` below. The
# DeepSeek provider reads ``DEEPSEEK_API_KEY`` from the environment.
_MODELS = {
    "deepseek-flash": OpenAIChatModel(
        "deepseek-flash", provider=DeepSeekProvider(api_key="sk-2fe24f1f4b3f420184e6faaa0930f3bf")
    ),
    "deepseek-pro": OpenAIChatModel(
        "deepseek-v4-pro", provider=DeepSeekProvider(api_key="sk-2fe24f1f4b3f420184e6faaa0930f3bf")
    ),
}


def resolve_model(model_id: str) -> OpenAIChatModel | str:
    # Unknown ids fall back to Pydantic AI's own resolver via the unchanged string.
    return _MODELS.get(model_id, model_id)


async def authenticate(connection: WebSocket) -> str | None:
    """Return the caller's identity, or ``None`` to refuse the connection."""
    token = connection.headers.get("authorization")
    if token == "Bearer example-token":
        return "example-user"
    return None


def agent_factory(principal: str | None) -> AbstractAgent[None, str]:
    # Use ``principal`` to bind identity into the agent's tools or deps.
    return Agent()


# ACP session stores keyed by principal. A user reconnecting with the same
# identity can load their own sessions; another user's store is never consulted.
# This is process-local: use shared durable storage when running several workers.
_session_stores: dict[str, InMemorySessionStore] = {}


def session_store_factory(principal: str | None) -> InMemorySessionStore:
    key = principal or "anonymous"
    return _session_stores.setdefault(key, InMemorySessionStore())


app = FastAPI(title="hrns example")

# One fresh ACP adapter per authenticated WebSocket connection. Sessions are
# saved into that principal's store, so ``session/load`` cannot cross users.
mount_acp(
    app,
    agent_factory,
    path="/acp",
    authenticate=authenticate,
    session_store_factory=session_store_factory,
    models=list(_MODELS),
    model_resolver=resolve_model,
)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("examples.fastapi_app:app", host="0.0.0.0", port=8000)
