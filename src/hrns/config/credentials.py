"""Local provider secrets in the OS keyring, shared by the client and the runtime.

One account per provider entry: the entry's ``api_key_env`` name when it has one, so a
pasted key and an account login for that entry resolve to the same secret. Entries that
name no variable are keyed by their own name, which is why two entries of the same
provider type (``openai-work``, ``openai-home``) can hold different keys.
"""

from __future__ import annotations

#: Keyring service that holds local provider credentials.
KEYRING_SERVICE = "hrns.local"

#: GitHub Copilot's public OAuth client. Copilot decides which model catalog an
#: account may list from the OAuth app that issued its token: this client sees the
#: current catalog, while a token from an unrelated app only sees legacy ids.
#: Override per user with ``copilot_client_id`` or ``GITHUB_OAUTH_CLIENT_ID``.
GITHUB_COPILOT_CLIENT_ID = "Iv1.b507a08c87ecfe98"


def keyring_account(provider: str, api_key_env: str = "") -> str:
    """The account a provider's credential is stored under."""
    return api_key_env or provider or "default"


def secret_ref(account: str) -> str:
    """Reference the client resolves before launching the runtime."""
    return f"keyring:{KEYRING_SERVICE}/{account}"


def stored_secret(account: str) -> str | None:
    """The stored secret for an account, or ``None`` when nothing is stored."""
    try:
        import keyring

        return keyring.get_password(KEYRING_SERVICE, account)
    except Exception:
        return None


def store_secret(account: str, secret: str) -> str | None:
    """Save a secret and return its reference; ``None`` when the keyring is unavailable."""
    try:
        import keyring

        keyring.set_password(KEYRING_SERVICE, account, secret)
    except Exception:
        return None
    return secret_ref(account)


def delete_secret(account: str) -> None:
    """Forget an account's secret; a missing keyring entry is not an error."""
    try:
        import keyring

        keyring.delete_password(KEYRING_SERVICE, account)
    except Exception:
        return


def stored_api_key_ref(provider: str, api_key_env: str = "") -> str | None:
    """Keyring reference for a previously saved local provider credential."""
    account = keyring_account(provider, api_key_env)
    return secret_ref(account) if stored_secret(account) is not None else None


def store_api_key(provider: str, api_key_env: str, api_key: str) -> str | None:
    """Save a pasted key under the provider's account; return its reference."""
    return store_secret(keyring_account(provider, api_key_env), api_key)
