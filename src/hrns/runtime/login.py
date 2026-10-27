"""Account logins for local providers: OAuth device flow, then the OS keyring.

The runtime owns these flows because Pydantic AI ships them; the client only starts
``hrns-local --login PROVIDER`` and renders the events it prints.
"""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass


class LocalLoginError(ValueError):
    """A device login that could not produce a usable token."""


@dataclass(frozen=True, slots=True)
class DeviceAuthorization:
    """The code a user types at ``verification_uri`` while the runtime polls."""

    user_code: str
    verification_uri: str
    expires_in: int


@dataclass(frozen=True, slots=True)
class LoginOutcome:
    """A completed login, ready to report and store."""

    token: str
    models: tuple[str, ...] = ()


#: hrns's GitHub OAuth app: device flow enabled, no scopes requested.
#: Override with the environment variable below to use a different app.
GITHUB_OAUTH_CLIENT_ID = "Ov23li5tNkRrbPNDx71L"
CLIENT_ID_ENV = "GITHUB_OAUTH_CLIENT_ID"

#: provider -> flow that returns a token once the user approves the device code.
DeviceFlow = Callable[[Callable[[DeviceAuthorization], None]], Awaitable[LoginOutcome]]


def github_client_id() -> str:
    return os.environ.get(CLIENT_ID_ENV) or GITHUB_OAUTH_CLIENT_ID


async def _github_copilot_flow(announce: Callable[[DeviceAuthorization], None]) -> LoginOutcome:
    from pydantic_ai.providers.github_copilot import GitHubCopilotOAuthFlow

    flow = GitHubCopilotOAuthFlow(client_id=github_client_id())
    try:
        authorization = await flow.start()
    except Exception as exc:  # noqa: BLE001 - user-facing message, not a stack trace
        raise LocalLoginError(f"GitHub refused the device-code request: {exc}") from exc
    announce(
        DeviceAuthorization(
            user_code=authorization.user_code,
            verification_uri=authorization.verification_uri,
            expires_in=authorization.expires_in,
        )
    )
    try:
        credentials = await flow.wait_for_authorization()
    except Exception as exc:  # noqa: BLE001 - expiry, denial, or transport failure
        raise LocalLoginError(f"GitHub login did not complete: {exc}") from exc
    return LoginOutcome(
        token=credentials.access_token,
        models=await _copilot_models(credentials.access_token),
    )


async def _copilot_models(token: str) -> tuple[str, ...]:
    """Model ids this account may use on Chat Completions; empty when it cannot be inspected.

    GitHub authorization alone does not prove Copilot access, so a rejected token is
    worth reporting at login time instead of at the first prompt.
    """
    from pydantic_ai.providers.github_copilot import GitHubCopilotProvider

    try:
        async with GitHubCopilotProvider(api_key=token) as provider:
            listing = await provider.client.models.list()
    except Exception as exc:  # noqa: BLE001 - reported below when it looks like entitlement
        if _auth_failure(exc):
            raise LocalLoginError(
                "GitHub authorized the login, but Copilot rejected the token. Check that "
                "this account has an active Copilot subscription."
            ) from exc
        return ()
    return tuple(item.id for item in listing.data if _serves_chat(item))


def _auth_failure(exc: BaseException) -> bool:
    """Whether a catalog request failed because the token, not the network, was refused."""
    from openai import AuthenticationError, PermissionDeniedError

    if isinstance(exc, (AuthenticationError, PermissionDeniedError)):
        return True
    text = str(exc).lower()
    return "unauthorized" in text or "forbidden" in text


def _serves_chat(item: object) -> bool:
    """Whether a catalog entry is reachable through Chat Completions.

    Copilot lists the models its Responses API serves too, and those fail at the first
    request. An entry without the field is kept: hiding a working id is worse.
    """
    extra_object = getattr(item, "model_extra", None)
    extra: dict[str, object] = extra_object if isinstance(extra_object, dict) else {}
    endpoints = extra.get("supported_endpoints")
    if not isinstance(endpoints, list):
        return True
    return any(
        isinstance(endpoint, str) and "chat/completions" in endpoint for endpoint in endpoints
    )


_FLOWS: dict[str, DeviceFlow] = {
    "github-copilot": _github_copilot_flow,
}


def login_providers() -> tuple[str, ...]:
    """Provider ids that accept an account login instead of a pasted key."""
    return tuple(_FLOWS)


def supports_login(provider: str) -> bool:
    return provider in _FLOWS


async def device_login(
    provider: str,
    *,
    on_authorization: Callable[[DeviceAuthorization], None],
) -> LoginOutcome:
    """Run ``provider``'s device flow; ``announce`` runs once the code is available."""
    flow = _FLOWS.get(provider)
    if flow is None:
        known = ", ".join(login_providers())
        raise LocalLoginError(
            f"{provider!r} does not support an account login. Providers with one: {known}."
        )
    return await flow(on_authorization)
