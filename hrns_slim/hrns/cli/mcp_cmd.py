"""``hrns mcp`` subcommand handlers."""

from __future__ import annotations

import importlib.util
import json
from collections.abc import Sequence
from pathlib import Path
from types import SimpleNamespace

from ..shared.config import HrnsConfig, McpOAuthConfig, McpServerSettings, default_config_path
from ..shared.config.mcp import (
    MCP_NAME_PATTERN,
    build_client,
    build_token_store,
    clear_tokens,
    default_token_store_path,
    server_target,
)
from ..shared.errors import HrnsClientError
from ..shared.exit_codes import EXIT_OK


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
        always_allow=getattr(args, "always_allow", False),
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
    print(f"{'NAME':<{name_width}}  {'TRANSPORT':<9}  {'STATUS':<8}  {'ACP':<6}  TARGET")
    for name, server in sorted(servers.items()):
        status = "enabled" if server.enabled else "disabled"
        acp = ("always" if server.always_allow else "ask") if server.enabled else "-"
        print(
            f"{name:<{name_width}}  {server.transport:<9}  {status:<8}  "
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
    if server.include_tools:
        print("include: " + ", ".join(server.include_tools))
    if server.exclude_tools:
        print("exclude: " + ", ".join(server.exclude_tools))
    if server.is_http:
        print(f"oauth: {'disabled' if server.oauth is False else 'enabled'}")
    if server.enabled:
        consent = "always allowed" if server.always_allow else "ask at session start"
        print(f"ACP consent: {consent}")
    print(f"timeout: {server.timeout:g}s")
    print(f"config: {path}")
    return EXIT_OK


def _require_python_mcp() -> None:
    if importlib.util.find_spec("fastmcp") is None:
        raise HrnsClientError(
            "Python MCP OAuth needs the headless or server extra. Install "
            "`hrns-slim[headless]>0.0.28`, or manage terminal sign-in in /mcp."
        )


async def _mcp_auth(args: SimpleNamespace) -> int:
    _require_python_mcp()
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
    _require_python_mcp()
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
