"""``hrns`` command line: terminal app and headless runner."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Annotated, Any, Literal

import tomli_w
from cyclopts import App, CycloptsError, Parameter

from ._version import __version__
from .client.launch import ConnectionTarget, local_cwd, profile_target, transport_for
from .client.runtime import agent_target
from .shared.config import (
    BUILTIN_CONNECTION,
    KEYRING_SERVICE,
    HrnsConfig,
    McpOAuthConfig,
    McpServerSettings,
    Profile,
    default_config_path,
    keyring_account,
    project_config_path,
    stored_api_key_ref,
)
from .shared.config.mcp import (
    MCP_NAME_PATTERN,
    build_client,
    build_token_store,
    clear_tokens,
    default_token_store_path,
    server_target,
)
from .shared.errors import HrnsClientError, InteractionRequired
from .shared.exit_codes import EXIT_FAILURE, EXIT_OK, EXIT_USAGE
from .shared.urls import check_websocket_url

__all__ = ["main", "build_parser"]


# --------------------------------------------------------------------- parser


ConfigOption = Annotated[
    Path | None,
    Parameter(
        name=["--config", "--config-file"],
        metavar="PATH",
        help=(
            "Read one configuration file instead of merging the user and project "
            "documents. Also HRNS_CONFIG_FILE."
        ),
    ),
]
RepeatedStringOption = Annotated[list[str] | None, Parameter(negative_iterable=())]
ToolOption = Annotated[
    list[str] | None,
    Parameter(name="--tool", metavar="NAME", negative_iterable=()),
]
ExcludeToolOption = Annotated[
    list[str] | None,
    Parameter(name="--exclude-tool", metavar="NAME", negative_iterable=()),
]
ClientIoOption = Annotated[
    Literal["off", "read", "all"] | None,
    Parameter(
        name="--client-io",
        help=(
            "Advertise client-side capabilities to the agent: 'off' offers nothing, 'read' "
            "serves fs/read_text_file, and 'all' also serves writes and terminal/* so the "
            "agent runs commands in this terminal. Defaults to 'all' unless a profile or "
            "TUI setting overrides it."
        ),
    ),
]


def _namespace(**values: object) -> SimpleNamespace:
    return SimpleNamespace(**values)


def _target_options(
    *,
    config: ConfigOption = None,
    stdio: bool = False,
    local: bool = False,
    profile: str | None = None,
    cwd: str | None = None,
    server: str | None = None,
    token: str | None = None,
) -> dict[str, object]:
    return {
        "config": config,
        "stdio": stdio,
        "local": local,
        "profile": profile,
        "cwd": cwd,
        "server": server,
        "token": token,
    }


def _connect_command(
    *remainder: str,
    config: ConfigOption = None,
    stdio: bool = False,
    local: bool = False,
    profile: str | None = None,
    cwd: str | None = None,
    server: str | None = None,
    token: str | None = None,
    client_io: ClientIoOption = None,
    session: str | None = None,
) -> int:
    args = _namespace(
        **_target_options(
            config=config,
            stdio=stdio,
            local=local,
            profile=profile,
            cwd=cwd,
            server=server,
            token=token,
        ),
        remainder=list(remainder),
        client_io=client_io,
        session=session,
    )
    return _command_connect(args)


def _run_command(
    *remainder: str,
    config: ConfigOption = None,
    stdio: bool = False,
    local: bool = False,
    profile: str | None = None,
    cwd: str | None = None,
    server: str | None = None,
    token: str | None = None,
    client_io: ClientIoOption = None,
    json: bool = False,
    allow_all: bool = False,
    deny_all: bool = False,
    answer: RepeatedStringOption = None,
    resume: str | None = None,
    timeout: float | None = None,
    attach: RepeatedStringOption = None,
) -> int:
    if allow_all and deny_all:
        raise CycloptsError("--allow-all cannot be combined with --deny-all.")
    args = _namespace(
        **_target_options(
            config=config,
            stdio=stdio,
            local=local,
            profile=profile,
            cwd=cwd,
            server=server,
            token=token,
        ),
        remainder=list(remainder),
        client_io=client_io,
        json=json,
        allow_all=allow_all,
        deny_all=deny_all,
        answer=answer or [],
        resume=resume,
        session=None,
        timeout=timeout,
        attach=attach or [],
    )
    return asyncio.run(_command_run(args))


def _profiles_list_command(*, config: ConfigOption = None) -> int:
    return _command_profiles(_namespace(config=config, profiles_command="list"))


def _profiles_path_command(*, config: ConfigOption = None) -> int:
    return _command_profiles(_namespace(config=config, profiles_command="path"))


def _profiles_add_command(
    name: str,
    *remainder: str,
    config: ConfigOption = None,
    stdio: bool = False,
    server: str | None = None,
    token: str | None = None,
    cwd: str | None = None,
    default: bool = False,
) -> int:
    return _command_profiles(
        _namespace(
            config=config,
            profiles_command="add",
            name=name,
            remainder=list(remainder),
            stdio=stdio,
            server=server,
            token=token,
            cwd=cwd,
            default=default,
        )
    )


def _profiles_remove_command(name: str, *, config: ConfigOption = None) -> int:
    return _command_profiles(_namespace(config=config, profiles_command="remove", name=name))


def _login_command(provider: str | None = None, *, config: ConfigOption = None) -> int:
    return asyncio.run(_command_login(_namespace(provider=provider, config=config)))


def _clean_command(
    *,
    yes: bool = False,
    dry_run: bool = False,
    config: bool = False,
    keys: bool = False,
) -> int:
    from .shared.clean import clean

    return clean(assume_yes=yes, dry_run=dry_run, config=config, keys=keys)


def _agent_command(
    *,
    config: ConfigOption = None,
    env_only: Annotated[
        bool,
        Parameter(help="Ignore the configuration file and use environment variables only."),
    ] = False,
    catalog: Annotated[bool, Parameter(help="Print the provider/model catalog.")] = False,
    login: Annotated[bool, Parameter(help="Account login (not supported).")] = False,
    cwd: Annotated[str, Parameter(help="Workspace root.")] = ".",
) -> int:
    _require_server_extra()
    from .server.runtime import run as run_agent

    run_agent(config=config, env_only=env_only, catalog=catalog, login=login, cwd=cwd)
    return EXIT_OK


def _acp_command(
    *,
    model: Annotated[
        str | None, Parameter(help="Pydantic AI model id; defaults to HRNS_MODEL.")
    ] = None,
    cwd: Annotated[str, Parameter(help="Workspace root.")] = ".",
) -> int:
    _require_server_extra()
    from .server.acp import run_acp_stdio
    from .server.agent import build_agent

    run_acp_stdio(lambda: build_agent(model, cwd=cwd))
    return EXIT_OK


def _serve_command(
    *,
    model: Annotated[
        str | None, Parameter(help="Pydantic AI model id; defaults to HRNS_MODEL.")
    ] = None,
    cwd: Annotated[str, Parameter(help="Workspace root.")] = ".",
    host: str = "127.0.0.1",
    port: int = 8000,
) -> int:
    _require_server_extra()
    if importlib.util.find_spec("uvicorn") is None or importlib.util.find_spec("fastapi") is None:
        raise HrnsClientError(
            "Serving over WebSocket needs the server extra. "
            "Install it with `pip install 'hrns-slim[server]'`."
        )
    from contextlib import asynccontextmanager

    import uvicorn
    from fastapi import FastAPI

    from .server import AcpServer
    from .server.agent import build_agent

    server = AcpServer()
    server.add_agent("agent", factory=lambda: build_agent(model, cwd=cwd))

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async with server:
            yield

    app = FastAPI(lifespan=lifespan)
    server.mount_acp(app)
    uvicorn.run(app, host=host, port=port)
    return EXIT_OK


def _mcp_add_command(
    name: str,
    *command_words: str,
    config: ConfigOption = None,
    url: str | None = None,
    stdio_command: Annotated[str | None, Parameter(name="--command", metavar="CMD")] = None,
    env: RepeatedStringOption = None,
    cwd: str | None = None,
    header: RepeatedStringOption = None,
    oauth_client_id: str | None = None,
    oauth_client_secret: str | None = None,
    oauth_scope: RepeatedStringOption = None,
    oauth_callback_port: int | None = None,
    no_oauth: bool = False,
    defer: bool = False,
    export: bool = False,
    always_allow: bool = False,
    description: str = "",
    include_tools: ToolOption = None,
    exclude_tools: ExcludeToolOption = None,
    timeout: float = 10.0,
    disabled: bool = False,
    force: bool = False,
) -> int:
    return _mcp_add(
        _namespace(
            config=config,
            name=name,
            url=url,
            stdio_command=stdio_command,
            env=env or [],
            cwd=cwd,
            header=header or [],
            oauth_client_id=oauth_client_id,
            oauth_client_secret=oauth_client_secret,
            oauth_scope=oauth_scope or [],
            oauth_callback_port=oauth_callback_port,
            no_oauth=no_oauth,
            defer=defer,
            export=export,
            always_allow=always_allow,
            description=description,
            include_tools=include_tools or [],
            exclude_tools=exclude_tools or [],
            timeout=timeout,
            disabled=disabled,
            force=force,
            command_words=list(command_words),
        )
    )


def _mcp_list_command(*, config: ConfigOption = None, json: bool = False) -> int:
    return _mcp_list(_namespace(config=config, json=json))


def _mcp_get_command(name: str, *, config: ConfigOption = None, json: bool = False) -> int:
    return _mcp_get(_namespace(config=config, name=name, json=json))


def _mcp_remove_command(name: str, *, config: ConfigOption = None) -> int:
    return _mcp_remove(_namespace(config=config, name=name))


def _mcp_enable_command(name: str, *, config: ConfigOption = None) -> int:
    return _mcp_set_enabled(_namespace(config=config, name=name), True)


def _mcp_disable_command(name: str, *, config: ConfigOption = None) -> int:
    return _mcp_set_enabled(_namespace(config=config, name=name), False)


def _mcp_auth_command(name: str, *, config: ConfigOption = None) -> int:
    return asyncio.run(_mcp_auth(_namespace(config=config, name=name)))


def _mcp_logout_command(name: str, *, config: ConfigOption = None) -> int:
    return asyncio.run(_mcp_logout(_namespace(config=config, name=name)))


def _register_commands(app: App) -> None:
    app.command(_connect_command, name="connect", help="Open the full-screen terminal")
    app.command(_run_command, name="run", help="Run one prompt headlessly and print the result")
    app.command(_agent_command, name="agent", help="Run the configured agent over ACP stdio")
    app.command(_acp_command, name="acp", help="Run an agent over ACP stdio")
    app.command(_serve_command, name="serve", help="Serve the agent over WebSocket and FastAPI")
    app.command(_login_command, name="login", help="Sign in to a provider with an account login")
    app.command(
        _clean_command,
        name="clean",
        help="Delete session history and, with --config, the provider config",
    )

    profiles = App(name="profiles", help="Manage saved connection profiles")
    profiles.command(_profiles_list_command, name="list", help="List saved profiles")
    profiles.command(_profiles_path_command, name="path", help="Print the configuration file path")
    profiles.command(_profiles_add_command, name="add", help="Add or replace a profile")
    profiles.command(_profiles_remove_command, name="remove", help="Remove a saved profile")
    app.command(profiles)

    config_app = App(name="config", help="Show and inspect the effective configuration")

    def config_help() -> int:
        config_app.help_print()
        return EXIT_OK

    config_app.default(config_help)
    config_app.command(_config_show_command, name="show", help="Print the effective configuration")
    config_app.command(
        _config_path_command, name="path", help="Print the source files, one per line"
    )
    config_app.command(
        _config_get_command, name="get", help="Print one configuration value by dotted key"
    )
    config_app.command(
        _config_keys_command, name="keys", help="List stored API keys and where they live"
    )
    app.command(config_app)

    mcp = App(name="mcp", help="Manage MCP servers for the built-in agent")

    def mcp_help() -> int:
        mcp.help_print()
        return EXIT_OK

    mcp.default(mcp_help)
    mcp.command(_mcp_add_command, name="add", help="Add or replace an MCP server")
    mcp.command(_mcp_list_command, name="list", help="List configured MCP servers")
    mcp.command(_mcp_get_command, name="get", help="Show one configured MCP server")
    mcp.command(_mcp_remove_command, name="remove", help="Remove a configured MCP server")
    mcp.command(_mcp_enable_command, name="enable", help="Enable a server for new sessions")
    mcp.command(
        _mcp_disable_command,
        name="disable",
        help="Disable a server without removing its configuration",
    )
    mcp.command(
        _mcp_auth_command,
        name="auth",
        alias="login",
        help="Authenticate with an MCP server using OAuth",
    )
    mcp.command(
        _mcp_logout_command, name="logout", help="Forget stored OAuth tokens for an MCP server"
    )
    app.command(mcp)


#: Leading options that describe a connection, so `hrns --server …` opens the terminal.
_CONNECT_DEFAULT_OPTIONS = frozenset(
    {
        "--config",
        "--config-file",
        "--stdio",
        "--local",
        "--profile",
        "--cwd",
        "--server",
        "--token",
        "--client-io",
        "--session",
    }
)


def build_parser() -> App:
    parser = App(
        name="hrns",
        help="Keyboard-first ACP terminal and headless runner.",
        version=f"hrns {__version__}",
        exit_on_error=False,
    )
    _register_commands(parser)
    return parser


# ------------------------------------------------------------------ helpers


def _config(args: SimpleNamespace) -> HrnsConfig:
    """Read the configuration; no explicit file means the user and project merge."""
    path = getattr(args, "config", None)
    return HrnsConfig(config_file=path) if path is not None else HrnsConfig()


def _require_client_extra() -> None:
    """Fail with an install hint instead of an ImportError from a missing extra."""
    if all(
        importlib.util.find_spec(name) is not None
        for name in ("textual", "acp", "websockets", "fastmcp")
    ):
        return
    raise HrnsClientError(
        "This command needs the client extra. Install it with `pip install 'hrns-slim[client]'`."
    )


def _require_server_extra() -> None:
    """Fail with an install hint instead of an ImportError from a missing extra."""
    if all(
        importlib.util.find_spec(name) is not None
        for name in ("pydantic_ai", "pydantic_ai_harness", "acp", "mcp", "fastmcp")
    ):
        return
    raise HrnsClientError(
        "This command needs the server extra. Install it with `pip install 'hrns-slim[server]'`."
    )


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
    project = project_config_path().expanduser().absolute()
    user = default_config_path().expanduser().absolute()
    return (
        _ConfigSource(path=project, kind="project", exists=project.is_file()),
        _ConfigSource(path=user, kind="user", exists=user.is_file()),
    )


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
        account = keyring_account(name, entry.api_key_env)
        ref = stored_api_key_ref(name, entry.api_key_env)
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
                "keyringRef": ref or f"keyring:{KEYRING_SERVICE}/{account}",
                "keyringStored": ref is not None,
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


def _profile_from_args(config: HrnsConfig, args: SimpleNamespace) -> Profile | None:
    """Pick the saved profile named by --profile, or the default."""
    name = getattr(args, "profile", None)
    if name:
        if name not in config.profile:
            available = ", ".join(sorted(config.profile)) or "(none)"
            raise HrnsClientError(
                f"No profile named '{name}'. Saved: {available}. Run `hrns profiles list`."
            )
        profile = config.get_profile(name)
        cwd = getattr(args, "cwd", None)
        return profile.model_copy(update={"cwd": cwd}) if cwd else profile
    return config.default_profile()


def _split_stdio_remainder(
    remainder: Sequence[str], *, split_prompt: bool = True
) -> tuple[list[str], list[str]]:
    """Split `run -- CMD ARGS -- PROMPT`; `connect -- CMD ARGS` has no prompt."""
    items = list(remainder)
    if items and items[0] == "--":
        items.pop(0)
    if split_prompt and "--" in items:
        index = items.index("--")
        return items[:index], items[index + 1 :]
    return items, []


def _stdio_profile(name: str, command: list[str], cwd: str | None) -> Profile:
    if not command:
        raise HrnsClientError(
            "--stdio needs a command after `--`, e.g. `hrns connect --stdio -- my-agent --acp`."
        )
    return Profile(
        name=name,
        transport="stdio",
        command=command[0],
        args=command[1:],
        cwd=cwd or "",
    )


async def _resolve_target(
    config: HrnsConfig,
    args: SimpleNamespace,
    *,
    remainder: Sequence[str] = (),
    split_prompt: bool = True,
) -> ConnectionTarget:
    """Apply CLI precedence: explicit server or stdio > local model."""
    cwd_override = getattr(args, "cwd", None)
    local_requested = bool(getattr(args, "local", False))
    stdio_requested = bool(getattr(args, "stdio", False))
    profile_name = getattr(args, "profile", None)
    server_url = (getattr(args, "server", None) or "").strip()
    token = (getattr(args, "token", None) or "").strip()
    if server_url:
        if local_requested or stdio_requested or profile_name:
            raise HrnsClientError("--server cannot be combined with --local, --stdio or --profile.")
        command, _prompt = _split_stdio_remainder(remainder, split_prompt=split_prompt)
        if command:
            raise HrnsClientError(
                "A command was supplied without --stdio. Use `--stdio -- CMD ARGS`."
            )
        profile = Profile(
            name="remote",
            transport="server",
            url=server_url,
            token=token,
            remote_cwd=cwd_override or "",
        )
        return profile_target(profile, cwd_override)
    if local_requested or not (stdio_requested or profile_name):
        if local_requested and (stdio_requested or profile_name):
            raise HrnsClientError("--local cannot be combined with another connection target.")
        command, _prompt = _split_stdio_remainder(remainder, split_prompt=split_prompt)
        if command:
            if local_requested:
                raise HrnsClientError("Local mode does not accept an agent command.")
            raise HrnsClientError(
                "A command was supplied without --stdio. Use `--stdio -- CMD ARGS`."
            )
        return agent_target(getattr(args, "config", None), cwd_override)
    command, _prompt = _split_stdio_remainder(remainder, split_prompt=split_prompt)
    if stdio_requested:
        profile = _stdio_profile("cli", command, cwd_override)
        return ConnectionTarget(
            name="cli",
            kind="local",
            profile=profile,
            cwd=local_cwd(profile, cwd_override),
        )
    if command:
        raise HrnsClientError("A command was supplied without --stdio. Use `--stdio -- CMD ARGS`.")
    profile = _profile_from_args(config, args)
    if profile is None:
        raise HrnsClientError(
            "No connection target. Use `--server URL`, `--stdio -- CMD` or configure a local model."
        )
    if token:
        profile = profile.model_copy(update={"token": token})
    return profile_target(profile, cwd_override)


def _remembered_target(config: HrnsConfig, args: SimpleNamespace) -> ConnectionTarget | None:
    """The connection this terminal used last, when it can still be resolved.

    A remembered profile that is no longer saved is forgotten: keeping the name would
    make every launch fail into the picker instead of starting clean.
    """
    identity = config.last_connection
    if not identity:
        return None
    cwd = getattr(args, "cwd", None)
    if identity == BUILTIN_CONNECTION:
        return agent_target(getattr(args, "config", None), cwd)
    profile = config.profile.get(identity)
    if profile is None:
        config.forget_connection()
        raise HrnsClientError(
            f"The last connection '{identity}' is no longer saved. Choose a connection."
        )
    return profile_target(profile, cwd)


def _answers(args: SimpleNamespace) -> dict[str, Any]:
    answers: dict[str, Any] = {}
    for item in getattr(args, "answer", []) or []:
        if "=" not in item:
            raise HrnsClientError(f"--answer expects FIELD=VALUE, got '{item}'")
        field, _, raw = item.partition("=")
        try:
            value: Any = json.loads(raw)
        except json.JSONDecodeError:
            value = raw
        answers[field.strip()] = value
    return answers


async def _command_run(args: SimpleNamespace) -> int:
    _require_client_extra()
    from .client.client import HrnsAcpClient
    from .client.headless import run_headless
    from .client.interactions import HeadlessInteractions
    from .client.io import parse_client_io

    config = _config(args)
    if args.stdio:
        command, prompt_words = _split_stdio_remainder(args.remainder)
        target_remainder: list[str] = ["--", *command]
    else:
        # Local-model targets take the remainder as the prompt; only an explicit
        # stdio launch has a command that needs separating.
        prompt_words = list(args.remainder)
        if prompt_words and prompt_words[0] == "--":
            prompt_words.pop(0)
        target_remainder = []
    prompt = " ".join(prompt_words).strip()
    if not prompt:
        raise HrnsClientError(
            'No prompt supplied. Usage: `hrns run --stdio -- CMD ARGS -- "your prompt"`, '
            '`hrns run --server wss://host/acp "your prompt"` or '
            '`hrns run --profile NAME "your prompt"`.'
        )
    target = await _resolve_target(config, args, remainder=target_remainder)
    policy_name = "allow" if args.allow_all else "deny" if args.deny_all else "required"
    policy = HeadlessInteractions(policy_name, answers=_answers(args))
    client_io = args.client_io or target.profile.client_io
    if client_io is None and args.session and args.cwd is None:
        client_io = "off"
    from .client.mcp_provider import ClientMcpProvider

    client = HrnsAcpClient(
        lambda: transport_for(target),
        policy=policy,
        auth_method=target.profile.auth_method,
        client_io=parse_client_io(client_io or "all"),
        mcp=ClientMcpProvider(config.mcp),
    )
    try:
        outcome = await run_headless(
            client,
            prompt=prompt,
            cwd=target.cwd,
            session_id=args.resume,
            json_output=args.json,
            timeout=args.timeout,
            attachments=args.attach,
        )
    finally:
        await client.disconnect()
    return outcome.exit_code


def _command_profiles(args: SimpleNamespace) -> int:
    config = _config(args)
    if args.profiles_command == "list":
        if not config.profile:
            print("No profiles saved. `hrns profiles add NAME --stdio -- CMD` creates one.")
            return EXIT_OK
        for name in sorted(config.profile):
            profile = config.profile[name]
            print(f"  {name:20} {profile.transport:7} {profile.describe_location()}")
        print(f"\nConfig: {config.path}")
        return EXIT_OK
    if args.profiles_command == "path":
        print(config.config_file or default_config_path())
        return EXIT_OK
    if args.profiles_command == "add":
        if args.server:
            if args.stdio:
                raise HrnsClientError("`profiles add` takes --stdio or --server, not both.")
            profile = Profile(
                name=args.name,
                transport="server",
                url=check_websocket_url(args.server),
                token=args.token or "",
                remote_cwd=args.cwd or "",
            )
            profile_target(profile)
        elif args.stdio:
            command, _ = _split_stdio_remainder(args.remainder)
            profile = _stdio_profile(args.name, command, args.cwd)
        else:
            raise HrnsClientError("`profiles add` needs --stdio (with `-- CMD`) or --server URL.")
        if args.default:
            for existing in config.profile.values():
                existing.default = False
            profile.default = True
        config.profile[profile.name] = profile
        path = config.save()
        print(f"Saved profile '{profile.name}' to {path}")
        return EXIT_OK
    if args.profiles_command == "remove":
        if args.name not in config.profile:
            raise HrnsClientError(f"No profile named '{args.name}'.")
        del config.profile[args.name]
        path = config.save()
        print(f"Removed '{args.name}' from {path}")
        return EXIT_OK
    return EXIT_USAGE


async def _command_login(args: SimpleNamespace) -> int:
    _require_client_extra()
    _require_server_extra()
    from .client.login import DeviceAuthorization, login, login_target

    target = login_target(args.provider, args.config)
    print(f"Signing in to {target.provider} (account {target.account})", flush=True)

    def announce(authorization: DeviceAuthorization) -> None:
        minutes = max(1, authorization.expires_in // 60)
        print(
            f"Open {authorization.verification_uri} and enter code: {authorization.user_code}",
            flush=True,
        )
        print(f"The code expires in about {minutes} min. Waiting for approval…", flush=True)

    result = await login(target, on_authorization=announce)
    print(f"Signed in. Token stored in the OS keyring as {result.reference}.", flush=True)
    if result.models:
        print("Models this account can use:\n  " + "\n  ".join(result.models), flush=True)
    if not target.configured:
        print(
            f"Saved for {result.provider}, which is not the active provider yet. Set "
            f'provider = "{result.provider}" in {default_config_path()}, or add and select '
            "it with /providers in the terminal.",
            flush=True,
        )
    return EXIT_OK


def _command_connect(args: SimpleNamespace) -> int:
    _require_client_extra()
    from .client.io import parse_client_io
    from .client.tui.app import run_terminal

    config = _config(args)
    preselect: ConnectionTarget | None = None
    status_message: str | None = None
    explicit = any(getattr(args, name, None) for name in ("stdio", "profile", "server")) or getattr(
        args, "local", False
    )
    command_words, _ = _split_stdio_remainder(args.remainder, split_prompt=False)
    try:
        if not explicit and not command_words:
            # A bare launch reopens the connection used last; the picker is the fallback.
            preselect = _remembered_target(config, args)
        if preselect is None:
            preselect = asyncio.run(
                _resolve_target(config, args, remainder=args.remainder, split_prompt=False)
            )
    except HrnsClientError as exc:
        # Local mode is the default. If it is not configured yet, show the picker
        # with the local-model action selected instead of exiting.
        if explicit:
            raise
        status_message = str(exc)
    return run_terminal(
        config,
        initial_target=preselect,
        initial_session=args.session,
        status_message=status_message,
        client_io=parse_client_io(args.client_io) if args.client_io else None,
    )


# ------------------------------------------------------------------- output


def _mcp_config(args: SimpleNamespace) -> tuple[HrnsConfig, Path]:
    """Load the configuration that owns MCP servers, allowing a new file to be created."""
    explicit = getattr(args, "config", None)
    if explicit is not None:
        path = Path(explicit).expanduser().absolute()
        settings = HrnsConfig(config_file=path) if path.is_file() else HrnsConfig(config_file=None)
        return settings, path
    settings = HrnsConfig()
    return settings, default_config_path()


def _mcp_pairs(values: Sequence[str], flag: str) -> dict[str, str]:
    pairs: dict[str, str] = {}
    for item in values:
        key, separator, value = item.partition("=")
        if not separator or not key.strip():
            raise HrnsClientError(f"{flag} expects KEY=VALUE, got '{item}'.")
        pairs[key.strip()] = value
    return pairs


def _mcp_server(name: str, args: SimpleNamespace) -> McpServerSettings:
    if not MCP_NAME_PATTERN.fullmatch(name):
        raise HrnsClientError(
            f"Invalid MCP server name '{name}': use letters, digits, '_' or '-' "
            "and start with a letter or digit."
        )
    command = list(args.command_words)
    if args.url and (args.stdio_command or command):
        raise HrnsClientError("Pass either --url or a stdio command, not both.")
    if args.stdio_command and command:
        raise HrnsClientError("Pass the command either with --command or after `--`, not both.")

    if args.url:
        url = args.url
        executable = ""
        arguments: list[str] = []
    elif command:
        url = ""
        executable = command[0]
        arguments = command[1:]
    elif args.stdio_command:
        url = ""
        executable = args.stdio_command
        arguments = []
    else:
        raise HrnsClientError(
            "An MCP server needs a transport: `hrns mcp add NAME -- npx -y PKG` "
            "or `hrns mcp add NAME --url https://example.com/mcp`."
        )

    oauth_flags = (
        args.oauth_client_id
        or args.oauth_client_secret
        or args.oauth_scope
        or args.oauth_callback_port is not None
    )
    if args.no_oauth and oauth_flags:
        raise HrnsClientError("--no-oauth cannot be combined with OAuth client options.")
    if args.always_allow and not args.export:
        raise HrnsClientError("--always-allow only applies with --export.")
    if oauth_flags:
        oauth: McpOAuthConfig | bool = McpOAuthConfig(
            client_id=args.oauth_client_id or "",
            client_secret=args.oauth_client_secret or "",
            scopes=list(args.oauth_scope),
            callback_port=args.oauth_callback_port,
        )
    else:
        oauth = False if args.no_oauth else True

    return McpServerSettings(
        command=executable,
        args=arguments,
        env=_mcp_pairs(args.env, "--env"),
        cwd=args.cwd or "",
        url=url,
        headers=_mcp_pairs(args.header, "--header"),
        enabled=not args.disabled,
        export=getattr(args, "export", False),
        always_allow=getattr(args, "always_allow", False),
        defer_loading=args.defer,
        description=args.description,
        include_tools=list(args.include_tools),
        exclude_tools=list(args.exclude_tools),
        timeout=args.timeout,
        oauth=oauth,
    )


def _mcp_server_or_error(settings: HrnsConfig, name: str) -> McpServerSettings:
    try:
        return settings.mcp.servers[name]
    except KeyError:
        known = ", ".join(sorted(settings.mcp.servers)) or "(none)"
        raise HrnsClientError(f"No MCP server named '{name}'. Configured: {known}.") from None


def _mcp_list(args: SimpleNamespace) -> int:
    settings, path = _mcp_config(args)
    servers = settings.mcp.servers
    if args.json:
        print(
            json.dumps(
                {
                    "configFile": str(path),
                    "tokenStore": settings.mcp.token_store,
                    "tokenStorePath": settings.mcp.token_store_path
                    or str(default_token_store_path()),
                    "servers": {
                        name: server.model_dump(mode="json")
                        for name, server in sorted(servers.items())
                    },
                },
                indent=2,
            )
        )
        return EXIT_OK
    if not servers:
        print("No MCP servers configured.")
        print("Add one with: hrns mcp add NAME -- npx -y PACKAGE")
        return EXIT_OK
    name_width = max(len(name) for name in servers)
    print(
        f"{'NAME':<{name_width}}  {'TRANSPORT':<9}  {'STATUS':<8}  {'MODE':<9}  {'ACP':<6}  TARGET"
    )
    for name, server in sorted(servers.items()):
        status = "enabled" if server.enabled else "disabled"
        mode = "deferred" if server.defer_loading else "eager"
        if not server.export:
            acp = "-"
        elif server.always_allow:
            acp = "always"
        else:
            acp = "export"
        print(
            f"{name:<{name_width}}  {server.transport:<9}  {status:<8}  {mode:<9}  "
            f"{acp:<6}  {server_target(server)}"
        )
    print(f"\nConfig: {path}")
    store = settings.mcp.token_store_path or str(default_token_store_path())
    print(f"Tokens: {settings.mcp.token_store}:{store}")
    return EXIT_OK


def _mcp_get(args: SimpleNamespace) -> int:
    settings, path = _mcp_config(args)
    server = _mcp_server_or_error(settings, args.name)
    if args.json:
        print(json.dumps(server.model_dump(mode="json"), indent=2))
        return EXIT_OK
    print(f"{args.name} ({server.transport}, {'enabled' if server.enabled else 'disabled'})")
    print(f"target: {server_target(server)}")
    if server.description:
        print(f"description: {server.description}")
    if server.cwd:
        print(f"cwd: {server.cwd}")
    if server.env:
        print("env: " + ", ".join(sorted(server.env)))
    if server.headers:
        print("headers: " + ", ".join(sorted(server.headers)))
    print(f"tools: {'deferred' if server.defer_loading else 'eager'}")
    if server.include_tools:
        print("include: " + ", ".join(server.include_tools))
    if server.exclude_tools:
        print("exclude: " + ", ".join(server.exclude_tools))
    if server.is_http:
        print(f"oauth: {'disabled' if server.oauth is False else 'enabled'}")
    if server.export:
        consent = "always allowed" if server.always_allow else "ask at session start"
        print(f"export over ACP: yes ({consent})")
    else:
        print("export over ACP: no")
    print(f"timeout: {server.timeout:g}s")
    print(f"config: {path}")
    return EXIT_OK


async def _mcp_auth(args: SimpleNamespace) -> int:
    settings, _path = _mcp_config(args)
    server = _mcp_server_or_error(settings, args.name)
    if not server.is_http:
        raise HrnsClientError("OAuth only applies to HTTP MCP servers; stdio servers use env vars.")
    if server.oauth_config() is None:
        raise HrnsClientError(
            f"OAuth is disabled for '{args.name}'. Pass credentials with --header instead."
        )
    store = build_token_store(settings.mcp.token_store, settings.mcp.token_store_path)
    print(f"Authorizing '{args.name}' (a browser opens only if the server needs OAuth)…")
    try:
        client = build_client(server, store=store, interactive=True)
        async with client:
            tools = await client.list_tools()
    except Exception as exc:  # noqa: BLE001 - reported to the operator verbatim
        raise HrnsClientError(f"Authentication failed: {exc}") from exc
    location = settings.mcp.token_store_path or str(default_token_store_path())
    print(f"Authenticated '{args.name}': {len(tools)} tool(s). Tokens: {location}")
    return EXIT_OK


async def _mcp_logout(args: SimpleNamespace) -> int:
    settings, _path = _mcp_config(args)
    server = _mcp_server_or_error(settings, args.name)
    store = build_token_store(settings.mcp.token_store, settings.mcp.token_store_path)
    try:
        await clear_tokens(server, store)
    except Exception as exc:  # noqa: BLE001 - reported to the operator verbatim
        raise HrnsClientError(f"Cannot clear tokens for '{args.name}': {exc}") from exc
    print(f"Removed stored OAuth tokens for '{args.name}'.")
    return EXIT_OK


def _mcp_add(args: SimpleNamespace) -> int:
    settings, path = _mcp_config(args)
    if args.name in settings.mcp.servers and not args.force:
        raise HrnsClientError(
            f"MCP server '{args.name}' already exists; pass --force to replace it."
        )
    server = _mcp_server(args.name, args)
    settings.mcp.servers[args.name] = server
    settings.save(path)
    print(f"Saved MCP server '{args.name}' ({server.transport}) to {path}")
    if server.is_http and server.oauth is not False:
        print(f"Authorize it with: hrns mcp auth {args.name}")
    return EXIT_OK


def _mcp_remove(args: SimpleNamespace) -> int:
    settings, path = _mcp_config(args)
    _mcp_server_or_error(settings, args.name)
    del settings.mcp.servers[args.name]
    settings.save(path)
    print(f"Removed MCP server '{args.name}' from {path}")
    return EXIT_OK


def _mcp_set_enabled(args: SimpleNamespace, enabled: bool) -> int:
    settings, path = _mcp_config(args)
    server = _mcp_server_or_error(settings, args.name)
    server.enabled = enabled
    settings.save(path)
    print(f"{'Enabled' if enabled else 'Disabled'} MCP server '{args.name}'.")
    return EXIT_OK


# --------------------------------------------------------------------- main


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    tokens = list(sys.argv[1:] if argv is None else argv)
    try:
        if not tokens or tokens[0] in _CONNECT_DEFAULT_OPTIONS:
            tokens = ["connect", *tokens]
        result = parser(
            tokens,
            exit_on_error=False,
            result_action="return_value",
            print_error=False,
        )
        return int(result or EXIT_OK)
    except KeyboardInterrupt:
        return 130
    except InteractionRequired as exc:
        print(f"hrns: interaction-required: {exc.message}", file=sys.stderr)
        return 3
    except (CycloptsError, HrnsClientError, KeyError) as exc:
        message = exc.args[0] if isinstance(exc, KeyError) and exc.args else str(exc)
        print(f"hrns: error: {message}", file=sys.stderr)
        return EXIT_USAGE if isinstance(exc, (CycloptsError, KeyError)) else EXIT_FAILURE
    except (ValueError, OSError) as exc:
        print(f"hrns: error: {exc}", file=sys.stderr)
        return EXIT_FAILURE


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
