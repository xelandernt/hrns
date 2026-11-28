"""``hrns config`` subcommand handlers."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Annotated, Any

import tomli_w
from cyclopts import Parameter

from ..shared.config import KEYRING_SERVICE, HrnsConfig, default_config_path
from ..shared.config.credentials import provider_credential_account, secret_ref, stored_secret
from ..shared.errors import HrnsClientError
from ..shared.exit_codes import EXIT_OK
from .common import ConfigOption, _config, _namespace


@dataclass(frozen=True)
class _ConfigSource:
    path: Path
    kind: str
    exists: bool


_SECRET_KEY = re.compile(r"(?i)(^|_)(api[_-]?key|token|secret|password|authorization|cookie)$")


def _redact(value: object, key: str = "") -> object:
    """Hide literal secrets while keeping ``env:``/``keyring:`` references visible."""
    if isinstance(value, dict):
        return {item: _redact(child, item) for item, child in value.items()}
    if isinstance(value, list):
        return [_redact(child, key) for child in value]
    if isinstance(value, str) and _SECRET_KEY.search(key.lower().replace("-", "_")):
        if value.startswith(("env:", "keyring:")) or "{env:" in value:
            return value
        return "***"
    return value


def _drop_none(value: object) -> object:
    """TOML has no null; drop absent optional fields before rendering."""
    if isinstance(value, dict):
        return {item: _drop_none(child) for item, child in value.items() if child is not None}
    if isinstance(value, list):
        return [_drop_none(child) for child in value if child is not None]
    return value


def _config_sources(settings: HrnsConfig, *, explicit: bool) -> tuple[_ConfigSource, ...]:
    """The documents this instance read, highest priority first."""
    if settings.config_file is not None:
        path = settings.config_file.expanduser().absolute()
        kind = "selected" if explicit else "environment"
        return (_ConfigSource(path=path, kind=kind, exists=path.is_file()),)
    user = default_config_path().expanduser().absolute()
    return (_ConfigSource(path=user, kind="user", exists=user.is_file()),)


def _config_show_command(
    *,
    config: ConfigOption = None,
    all: Annotated[bool, Parameter(help="Include values equal to their defaults.")] = False,
    json: Annotated[bool, Parameter(help="Print JSON instead of TOML.")] = False,
    reveal: Annotated[bool, Parameter(help="Print literal secrets instead of ***.")] = False,
) -> int:
    return _config_show(_namespace(config=config, all=all, json=json, reveal=reveal))


def _config_show(args: SimpleNamespace) -> int:
    settings = _config(args)
    sources = _config_sources(settings, explicit=args.config is not None)
    payload: dict[str, Any] = settings.model_dump(
        mode="json",
        exclude={"config_file", "api_key"},
        exclude_defaults=not args.all,
    )
    if not args.reveal:
        redacted = _redact(payload)
        if isinstance(redacted, dict):
            payload = redacted
    if args.json:
        print(
            json.dumps(
                {
                    "sources": [
                        {
                            "path": str(source.path),
                            "kind": source.kind,
                            "exists": source.exists,
                        }
                        for source in sources
                    ],
                    "writeTarget": str(settings.path),
                    "config": payload,
                },
                indent=2,
            )
        )
        return EXIT_OK
    print("Configuration")
    for source in sources:
        status = "exists" if source.exists else "missing"
        print(f"  source: {source.path} ({source.kind}, {status})")
    print(f"  write:  {settings.path}")
    print("  keys:   run `hrns config keys` for credential locations (values are never printed)")
    print()
    rendered = _drop_none(payload)
    if isinstance(rendered, dict):
        payload = rendered
    text = tomli_w.dumps(payload).rstrip()
    print(text or "# no non-default values")
    return EXIT_OK


def _config_path_command(*, config: ConfigOption = None) -> int:
    return _config_path(_namespace(config=config))


def _config_path(args: SimpleNamespace) -> int:
    settings = _config(args)
    sources = _config_sources(settings, explicit=args.config is not None)
    existing = [source.path for source in sources if source.exists]
    for path in existing or [settings.path]:
        print(path)
    return EXIT_OK


def _config_get_command(
    key: Annotated[str, Parameter(help="Dotted key, e.g. mcp.servers.playwright.url.")],
    *,
    config: ConfigOption = None,
    json: Annotated[bool, Parameter(help="Print the value as JSON.")] = False,
    reveal: Annotated[bool, Parameter(help="Print a literal secret instead of ***.")] = False,
) -> int:
    return _config_get(_namespace(config=config, key=key, json=json, reveal=reveal))


def _config_get(args: SimpleNamespace) -> int:
    if args.key == "api_key" or args.key.startswith("api_key."):
        raise HrnsClientError("api_key is a secret and is not printed")
    settings = _config(args)
    value: object = settings.model_dump(
        mode="json",
        exclude={"config_file", "api_key"},
    )
    if not args.reveal:
        value = _redact(value)
    for part in args.key.split("."):
        if isinstance(value, dict) and part in value:
            value = value[part]
        elif isinstance(value, list) and part.isdigit() and int(part) < len(value):
            value = value[int(part)]
        else:
            raise HrnsClientError(f"Unknown configuration key: {args.key}")
    if args.json or isinstance(value, (dict, list)):
        print(json.dumps(value, indent=2))
    elif value is None:
        print("null")
    elif isinstance(value, bool):
        print("true" if value else "false")
    else:
        print(value)
    return EXIT_OK


def _config_keys_command(
    *,
    config: ConfigOption = None,
    json: Annotated[bool, Parameter(help="Print JSON instead of a table.")] = False,
) -> int:
    return _config_keys(_namespace(config=config, json=json))


def _config_keys(args: SimpleNamespace) -> int:
    settings = _config(args)
    providers: list[dict[str, object]] = []
    for name, entry in sorted(settings.providers.items()):
        account = provider_credential_account(name, entry.provider_id(name), entry.base_url)
        ref = secret_ref(account)
        active = name == settings.provider
        agent_secret = settings.api_key.get_secret_value() if active and settings.api_key else None
        agent_kind = ""
        if agent_secret is not None:
            agent_kind = "reference" if agent_secret.startswith(("env:", "keyring:")) else "literal"
        providers.append(
            {
                "provider": name,
                "active": active,
                "apiKeyEnv": entry.api_key_env,
                "envSet": bool(entry.api_key_env and os.environ.get(entry.api_key_env)),
                "keyringService": KEYRING_SERVICE,
                "keyringAccount": account,
                "keyringRef": ref,
                "keyringStored": stored_secret(account) is not None,
                "hrnsApiKey": agent_secret is not None,
                "hrnsApiKeyKind": agent_kind,
            }
        )
    if args.json:
        print(
            json.dumps(
                {
                    "keyringService": KEYRING_SERVICE,
                    "providers": providers,
                },
                indent=2,
            )
        )
        return EXIT_OK
    if not providers:
        print("No providers configured.")
        return EXIT_OK
    print(f"Stored API keys · keyring service: {KEYRING_SERVICE}")
    for item in providers:
        active = " (active)" if item["active"] else ""
        print(f"\n{item['provider']}{active}")
        stored = "stored" if item["keyringStored"] else "not stored"
        print(f"  keyring: {item['keyringRef']} ({stored})")
        env_name = str(item["apiKeyEnv"] or "")
        if env_name:
            env_state = "set" if item["envSet"] else "not set"
            print(f"  env:     {env_name} ({env_state})")
        else:
            print("  env:     (no api_key_env configured)")
        if item["active"]:
            local = "set" if item["hrnsApiKey"] else "not set"
            kind = str(item["hrnsApiKeyKind"] or "")
            suffix = f", {kind}" if kind else ""
            print(f"  HRNS_API_KEY: {local}{suffix}")
    print("\nValues are never printed by this command.")
    return EXIT_OK
