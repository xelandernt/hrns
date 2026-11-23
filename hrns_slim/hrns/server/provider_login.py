"""Local provider sign-in using the runtime's provider implementations."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from typing import Literal

from pydantic import TypeAdapter, ValidationError
from pydantic_ai.providers.openai_codex import (
    OpenAICodexCredentials,
    OpenAICodexCredentialSource,
)

from hrns.shared.config import GITHUB_COPILOT_CLIENT_ID, ProviderEntry
from hrns.shared.config.credentials import (
    KEYRING_SERVICE,
    keyring_secret,
    provider_credential_account,
    store_secret,
    stored_secret,
)

LoginMethod = Literal["api-key", "copilot", "codex", "aws", "aws-sso"]
_CODEX_CREDENTIALS = TypeAdapter(OpenAICodexCredentials)


def login_methods(provider_type: str) -> tuple[LoginMethod, ...]:
    match provider_type:
        case "github-copilot":
            return ("copilot",)
        case "openai-codex":
            return ("codex",)
        case "bedrock":
            return ("aws", "aws-sso")
        case _:
            return ("api-key",)


def login_account(name: str, entry: ProviderEntry) -> str:
    return provider_credential_account(name, entry.provider_id(name), entry.base_url)


def _save_secret(account: str, secret: str) -> None:
    if store_secret(account, secret) is None:
        raise ProviderLoginError("The OS keyring could not save the credential.")


@dataclass(frozen=True)
class CodexKeyring(OpenAICodexCredentialSource):
    account: str

    async def load(self) -> OpenAICodexCredentials:
        # Refresh tokens can rotate in another process.
        raw = await asyncio.to_thread(keyring_secret, KEYRING_SERVICE, self.account, fresh=True)
        if raw is None:
            raise ProviderLoginError("Sign in to Codex with /login.")
        try:
            return _CODEX_CREDENTIALS.validate_json(raw)
        except ValidationError:
            raise ProviderLoginError(
                "The saved Codex credentials are invalid. Run /login."
            ) from None

    async def save(self, credentials: OpenAICodexCredentials) -> None:
        await asyncio.to_thread(_save_secret, self.account, json.dumps(asdict(credentials)))


class ProviderLoginError(ValueError):
    """A sign-in error safe to display to the user."""


class ProviderLogin:
    """One cancellable sign-in attempt per local runtime."""

    def __init__(
        self, *, refresh_copilot_models: Callable[[str], Awaitable[str | None]] | None = None
    ) -> None:
        self._refresh_copilot_models = refresh_copilot_models
        self._task: asyncio.Task[str | None] | None = None
        self._challenge: dict[str, object] = {}

    @property
    def active(self) -> bool:
        return self._task is not None

    async def start(
        self, name: str, entry: ProviderEntry, method: LoginMethod, key: str | None
    ) -> dict[str, object]:
        if method not in login_methods(entry.provider_id(name)):
            raise ProviderLoginError("This sign-in method is unavailable for the provider.")
        if self._task is not None:
            raise ProviderLoginError("Finish or cancel the current sign-in first.")
        account = login_account(name, entry)
        match method:
            case "api-key":
                if not key or not key.strip():
                    raise ProviderLoginError("Enter an API key.")
                await asyncio.to_thread(_save_secret, account, key.strip())
                return {"done": True}
            case "copilot":
                from pydantic_ai.providers.github_copilot import GitHubCopilotOAuthFlow

                if entry.base_url:
                    raise ProviderLoginError(
                        "Remove the custom endpoint to sign in to GitHub Copilot."
                    )
                client_id = (
                    entry.copilot_client_id
                    or os.getenv("GITHUB_OAUTH_CLIENT_ID")
                    or GITHUB_COPILOT_CLIENT_ID
                )
                flow = GitHubCopilotOAuthFlow(client_id=client_id)

                async def copilot() -> str | None:
                    device = await flow.start()
                    self._challenge = {"url": device.verification_uri, "code": device.user_code}
                    credentials = await flow.wait_for_authorization()
                    await asyncio.to_thread(_save_secret, account, credentials.access_token)
                    if self._refresh_copilot_models is not None:
                        return await self._refresh_copilot_models(name)
                    return None

                self._task = asyncio.create_task(copilot())
                return {}
            case "codex":
                from pydantic_ai.providers.openai_codex import OpenAICodexOAuthFlow

                codex_flow = OpenAICodexOAuthFlow()

                async def codex() -> None:
                    credentials = await asyncio.wait_for(
                        codex_flow.exchange_code_from_callback(), timeout=300
                    )
                    await CodexKeyring(account).save(credentials)

                self._task = asyncio.create_task(codex())
                return {"url": codex_flow.authorization_url()}
            case "aws" | "aws-sso":
                self._task = asyncio.create_task(_aws_login(entry, sso=method == "aws-sso"))
                return {}

    async def status(self) -> dict[str, object]:
        task = self._task
        if task is None:
            raise ProviderLoginError("No sign-in is in progress.")
        await asyncio.wait((task,), timeout=0.5)
        if not task.done():
            return {"done": False, **self._challenge}
        if self._task is not task:
            return {"done": False}
        self._task = None
        self._challenge = {}
        try:
            warning = task.result()
        except ProviderLoginError:
            raise
        except Exception:
            raise ProviderLoginError(
                "Sign-in failed or expired. Run /login to try again."
            ) from None
        return {"done": True, "warning": warning}

    async def cancel(self) -> None:
        task, self._task = self._task, None
        self._challenge = {}
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


def codex_source(name: str, entry: ProviderEntry) -> CodexKeyring | None:
    account = login_account(name, entry)
    return CodexKeyring(account) if stored_secret(account) is not None else None


async def _aws_login(entry: ProviderEntry, *, sso: bool) -> None:
    from boto3 import Session
    from botocore.exceptions import BotoCoreError, ClientError

    profile = entry.options.get("profile_name")
    region = entry.options.get("region_name")
    profile_name = profile if isinstance(profile, str) else None
    region_name = region if isinstance(region, str) else None

    def discover() -> None:
        session = Session(profile_name=profile_name, region_name=region_name)
        credentials = session.get_credentials()
        if credentials is None:
            raise ProviderLoginError("boto3 could not find AWS credentials.")
        credentials.get_frozen_credentials()

    if sso:
        args = ["aws", "sso", "login"]
        if profile_name:
            args.extend(("--profile", profile_name))
        try:
            process = await asyncio.create_subprocess_exec(
                *args,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except FileNotFoundError:
            raise ProviderLoginError("Install AWS CLI v2 to start an AWS SSO login.") from None
        try:
            if await asyncio.wait_for(process.wait(), timeout=600):
                raise ProviderLoginError("AWS SSO login failed. Check the selected AWS profile.")
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()

    try:
        await asyncio.to_thread(discover)
    except (BotoCoreError, ClientError):
        raise ProviderLoginError(
            "AWS credentials are unavailable. Check your AWS profile or use AWS SSO."
        ) from None
