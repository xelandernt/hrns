"""``hrns`` command line: terminal app, headless runner, catalog and bridge."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Annotated, Any, Literal

from cyclopts import App, CycloptsError, Parameter

from ..config import LocalSettings, McpOAuthConfig, McpServerSettings, default_local_config_path
from ..config.mcp import (
    MCP_NAME_PATTERN,
    build_client,
    build_token_store,
    clear_tokens,
    default_token_store_path,
    server_target,
)
from ._version import __version__
from .catalog import AgentDeployment
from .client import HrnsAcpClient
from .errors import HrnsClientError, InteractionRequired
from .headless import EXIT_FAILURE, EXIT_OK, EXIT_USAGE, run_headless
from .interactions import HeadlessInteractions
from .io import parse_client_io
from .launch import (
    ConnectionTarget,
    catalog_for,
    default_remote_cwd,
    local_cwd,
    resolve_agent,
    transport_for,
)
from .local import local_target
from .login import DeviceAuthorization, login, login_target
from .profiles import Profile, Profiles, default_profiles_path

__all__ = ["main", "build_parser"]


# --------------------------------------------------------------------- parser


ProfilesOption = Annotated[
    list[str] | None,
    Parameter(
        name="--profiles",
        metavar="PATH",
        negative_iterable=(),
        help="Load an explicit profile TOML file (repeatable). Also HRNS_PROFILES.",
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
            "agent runs commands in this terminal. Defaults to the profile's client_io."
        ),
    ),
]


def _namespace(**values: object) -> argparse.Namespace:
    return argparse.Namespace(**values)


def _profile_values(profiles: list[str] | None) -> list[str]:
    return profiles or []


def _target_options(
    *,
    profiles: ProfilesOption = None,
    stdio: bool = False,
    local: bool = False,
    local_config: Path | None = None,
    server: str | None = None,
    agent: str | None = None,
    profile: str | None = None,
    cwd: str | None = None,
    url: str | None = None,
    token: str | None = None,
) -> dict[str, object]:
    return {
        "profiles": _profile_values(profiles),
        "stdio": stdio,
        "local": local,
        "local_config": local_config,
        "server": server,
        "agent": agent,
        "profile": profile,
        "cwd": cwd,
        "url": url,
        "token": token,
    }


def _connect_command(
    *remainder: str,
    profiles: ProfilesOption = None,
    stdio: bool = False,
    local: bool = False,
    local_config: Path | None = None,
    server: str | None = None,
    agent: str | None = None,
    profile: str | None = None,
    cwd: str | None = None,
    url: str | None = None,
    token: str | None = None,
    client_io: ClientIoOption = None,
    session: str | None = None,
) -> int:
    args = _namespace(
        **_target_options(
            profiles=profiles,
            stdio=stdio,
            local=local,
            local_config=local_config,
            server=server,
            agent=agent,
            profile=profile,
            cwd=cwd,
            url=url,
            token=token,
        ),
        remainder=list(remainder),
        client_io=client_io,
        session=session,
    )
    return _command_connect(args)


def _run_command(
    *remainder: str,
    profiles: ProfilesOption = None,
    stdio: bool = False,
    local: bool = False,
    local_config: Path | None = None,
    server: str | None = None,
    agent: str | None = None,
    profile: str | None = None,
    cwd: str | None = None,
    url: str | None = None,
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
            profiles=profiles,
            stdio=stdio,
            local=local,
            local_config=local_config,
            server=server,
            agent=agent,
            profile=profile,
            cwd=cwd,
            url=url,
            token=token,
        ),
        remainder=list(remainder),
        client_io=client_io,
        json=json,
        allow_all=allow_all,
        deny_all=deny_all,
        answer=answer or [],
        resume=resume,
        timeout=timeout,
        attach=attach or [],
    )
    return asyncio.run(_command_run(args))


def _agents_list_command(
    *,
    profiles: ProfilesOption = None,
    server: str | None = None,
    url: str | None = None,
    token: str | None = None,
    limit: int = 50,
    cursor: str | None = None,
    all: bool = False,
    json: bool = False,
) -> int:
    return asyncio.run(
        _command_agents(
            _namespace(
                profiles=_profile_values(profiles),
                agents_command="list",
                server=server,
                url=url,
                token=token,
                limit=limit,
                cursor=cursor,
                all=all,
                json=json,
            )
        )
    )


def _agents_show_command(
    agent: str,
    *,
    profiles: ProfilesOption = None,
    server: str | None = None,
    url: str | None = None,
    token: str | None = None,
    json: bool = False,
) -> int:
    return asyncio.run(
        _command_agents(
            _namespace(
                profiles=_profile_values(profiles),
                agents_command="show",
                server=server,
                url=url,
                token=token,
                agent=agent,
                json=json,
            )
        )
    )


def _bridge_command(
    *,
    profiles: ProfilesOption = None,
    server: str | None = None,
    url: str | None = None,
    token: str | None = None,
    agent: str | None = None,
    remote_cwd: str | None = None,
    profile: str | None = None,
) -> int:
    return asyncio.run(
        _command_bridge(
            _namespace(
                profiles=_profile_values(profiles),
                server=server,
                url=url,
                token=token,
                agent=agent,
                remote_cwd=remote_cwd,
                profile=profile,
            )
        )
    )


def _profiles_list_command(*, profiles: ProfilesOption = None) -> int:
    return _command_profiles(
        _namespace(profiles=_profile_values(profiles), profiles_command="list")
    )


def _profiles_path_command(*, profiles: ProfilesOption = None) -> int:
    return _command_profiles(
        _namespace(profiles=_profile_values(profiles), profiles_command="path")
    )


def _profiles_add_command(
    name: str,
    *remainder: str,
    profiles: ProfilesOption = None,
    stdio: bool = False,
    server: str | None = None,
    agent: str | None = None,
    token: str | None = None,
    cwd: str | None = None,
    remote_cwd: str | None = None,
    default: bool = False,
) -> int:
    return _command_profiles(
        _namespace(
            profiles=_profile_values(profiles),
            profiles_command="add",
            name=name,
            remainder=list(remainder),
            stdio=stdio,
            server=server,
            agent=agent,
            token=token,
            cwd=cwd,
            remote_cwd=remote_cwd,
            default=default,
        )
    )


def _profiles_remove_command(name: str, *, profiles: ProfilesOption = None) -> int:
    return _command_profiles(
        _namespace(profiles=_profile_values(profiles), profiles_command="remove", name=name)
    )


def _login_command(provider: str | None = None, *, local_config: Path | None = None) -> int:
    return asyncio.run(_command_login(_namespace(provider=provider, local_config=local_config)))


def _clean_command(
    *,
    yes: bool = False,
    dry_run: bool = False,
    config: bool = False,
    keys: bool = False,
) -> int:
    from .clean import clean

    return clean(assume_yes=yes, dry_run=dry_run, config=config, keys=keys)


def _mcp_add_command(
    name: str,
    *command_words: str,
    local_config: Path | None = None,
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
    description: str = "",
    include_tools: ToolOption = None,
    exclude_tools: ExcludeToolOption = None,
    timeout: float = 10.0,
    disabled: bool = False,
    force: bool = False,
) -> int:
    return _mcp_add(
        _namespace(
            local_config=local_config,
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
            description=description,
            include_tools=include_tools or [],
            exclude_tools=exclude_tools or [],
            timeout=timeout,
            disabled=disabled,
            force=force,
            command_words=list(command_words),
        )
    )


def _mcp_list_command(*, local_config: Path | None = None, json: bool = False) -> int:
    return _mcp_list(_namespace(local_config=local_config, json=json))


def _mcp_get_command(name: str, *, local_config: Path | None = None, json: bool = False) -> int:
    return _mcp_get(_namespace(local_config=local_config, name=name, json=json))


def _mcp_remove_command(name: str, *, local_config: Path | None = None) -> int:
    return _mcp_remove(_namespace(local_config=local_config, name=name))


def _mcp_enable_command(name: str, *, local_config: Path | None = None) -> int:
    return _mcp_set_enabled(_namespace(local_config=local_config, name=name), True)


def _mcp_disable_command(name: str, *, local_config: Path | None = None) -> int:
    return _mcp_set_enabled(_namespace(local_config=local_config, name=name), False)


def _mcp_auth_command(name: str, *, local_config: Path | None = None) -> int:
    return asyncio.run(_mcp_auth(_namespace(local_config=local_config, name=name)))


def _mcp_logout_command(name: str, *, local_config: Path | None = None) -> int:
    return asyncio.run(_mcp_logout(_namespace(local_config=local_config, name=name)))


def _register_commands(app: App) -> None:
    app.command(_connect_command, name="connect", help="Open the full-screen terminal")
    app.command(_run_command, name="run", help="Run one prompt headlessly and print the result")
    app.command(_bridge_command, name="bridge", help="Bridge local stdio ACP to a hosted agent")
    app.command(
        _login_command, name="login", help="Sign in to a local provider with an account login"
    )
    app.command(
        _clean_command,
        name="clean",
        help="Delete local session history and, with --config, the local provider config",
    )

    agents = App(name="agents", help="Inspect the hosted deployment catalog")
    agents.command(_agents_list_command, name="list", help="List accessible agent deployments")
    agents.command(_agents_show_command, name="show", help="Show one deployment descriptor")
    app.command(agents)

    profiles = App(name="profiles", help="Manage saved connection profiles")
    profiles.command(_profiles_list_command, name="list", help="List saved profiles")
    profiles.command(_profiles_path_command, name="path", help="Print the user profile file path")
    profiles.command(_profiles_add_command, name="add", help="Add or replace a profile")
    profiles.command(_profiles_remove_command, name="remove", help="Remove a saved profile")
    app.command(profiles)

    mcp = App(name="mcp", help="Manage MCP servers for the local agent")

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


def _extract_global_profiles(argv: list[str]) -> list[str]:
    """Move legacy global --profiles options into commands that consume them."""
    values: list[str] = []
    while argv and argv[0] == "--profiles":
        argv.pop(0)
        if not argv:
            raise HrnsClientError("argument --profiles: expected one argument")
        values.append(argv.pop(0))
    if not values:
        return argv

    command = argv[0] if argv else "connect"
    if command not in {"connect", "run", "agents", "bridge", "profiles"}:
        return argv
    command_index = 1 if command in {"agents", "profiles"} and len(argv) > 1 else 0
    options = [item for value in values for item in ("--profiles", value)]
    argv[command_index + 1 : command_index + 1] = options
    return argv


def build_parser() -> App:
    parser = App(
        name="hrns",
        help="Keyboard-first ACP terminal, headless runner and stdio bridge.",
        version=f"hrns {__version__}",
        exit_on_error=False,
    )
    _register_commands(parser)
    return parser


# ------------------------------------------------------------------ helpers


def _profiles(args: argparse.Namespace) -> Profiles:
    return Profiles.load(extra_files=[Path(item) for item in getattr(args, "profiles", []) or []])


def _profile_from_args(profiles: Profiles, args: argparse.Namespace) -> Profile | None:
    """Pick the saved profile named by --profile/--server, or build an ad-hoc one."""
    name = getattr(args, "profile", None) or getattr(args, "server", None)
    if name:
        if name in profiles.profiles:
            profile = profiles.get(name)
            # --url/--token/--agent flags override the saved values.
            overrides = {
                "url": getattr(args, "url", None),
                "token": getattr(args, "token", None),
                "agent": getattr(args, "agent", None),
            }
            for field, value in overrides.items():
                if value:
                    setattr(profile, field, value)
            if getattr(args, "cwd", None) and profile.transport == "stdio":
                profile.cwd = args.cwd
            return profile
        if "://" in name:
            return Profile(
                name="cli-server",
                transport="server",
                url=name,
                token=getattr(args, "token", None) or "",
                agent=getattr(args, "agent", None) or "",
            )
        raise HrnsClientError(
            f"No profile named '{name}'. Saved: {', '.join(profiles.names) or '(none)'}. "
            "Run `hrns profiles list`."
        )
    if getattr(args, "url", None):
        return Profile(
            name="cli-server",
            transport="server",
            url=args.url,
            token=getattr(args, "token", None) or "",
            agent=getattr(args, "agent", None) or "",
        )
    profile = profiles.default_profile()
    return profile


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
    profiles: Profiles,
    args: argparse.Namespace,
    *,
    remainder: Sequence[str] = (),
    split_prompt: bool = True,
) -> ConnectionTarget:
    """Apply CLI precedence: explicit stdio > explicit remote > local model."""
    cwd_override = getattr(args, "cwd", None)
    explicit_remote = any(
        getattr(args, name, None) for name in ("server", "url", "profile", "agent", "token")
    )
    local_requested = bool(getattr(args, "local", False) or getattr(args, "local_config", None))
    if local_requested or not (getattr(args, "stdio", False) or explicit_remote):
        if local_requested and (getattr(args, "stdio", False) or explicit_remote):
            raise HrnsClientError(
                "--local/--local-config cannot be combined with another connection target."
            )
        command, _prompt = _split_stdio_remainder(remainder, split_prompt=split_prompt)
        if command:
            if local_requested:
                raise HrnsClientError("Local mode does not accept an agent command.")
            raise HrnsClientError(
                "A command was supplied without --stdio. Use `--stdio -- CMD ARGS`."
            )
        return local_target(getattr(args, "local_config", None), cwd_override)
    command, _prompt = _split_stdio_remainder(remainder, split_prompt=split_prompt)
    if getattr(args, "stdio", False):
        profile = _stdio_profile("cli", command, cwd_override)
        return ConnectionTarget(
            name="cli",
            kind="local",
            profile=profile,
            agent=None,
            cwd=local_cwd(profile, cwd_override),
        )
    if command:
        raise HrnsClientError("A command was supplied without --stdio. Use `--stdio -- CMD ARGS`.")
    profile = _profile_from_args(profiles, args)
    if profile is None:
        raise HrnsClientError(
            "No connection target. Use `--stdio -- CMD`, `--server PROFILE --agent AGENT`, or configure a local model."
        )
    if profile.transport == "stdio":
        if not profile.command:
            raise HrnsClientError(f"Profile '{profile.name}' has no command.")
        return ConnectionTarget(
            name=profile.name,
            kind="local",
            profile=profile,
            agent=None,
            cwd=local_cwd(profile, cwd_override),
        )
    agent = await resolve_agent(profile, getattr(args, "agent", None))
    cwd = default_remote_cwd(agent, cwd_override)
    return ConnectionTarget(name=profile.name, kind="remote", profile=profile, agent=agent, cwd=cwd)


def _answers(args: argparse.Namespace) -> dict[str, Any]:
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


async def _resolve_catalog_profile(args: argparse.Namespace) -> tuple[Profile, Any]:
    profiles = _profiles(args)
    profile = _profile_from_args(profiles, args)
    if profile is None:
        raise HrnsClientError("Pass --server PROFILE (or --url URL) to reach a catalog.")
    if profile.transport != "server":
        raise HrnsClientError(f"Profile '{profile.name}' is not a server profile.")
    return profile, catalog_for(profile)


# ----------------------------------------------------------------- commands


async def _command_run(args: argparse.Namespace) -> int:
    profiles = _profiles(args)
    if args.stdio:
        command, prompt_words = _split_stdio_remainder(args.remainder)
        target_remainder: list[str] = ["--", *command]
    else:
        # Local-model and remote targets take the remainder as the prompt; only
        # an explicit stdio launch has a command that needs separating.
        prompt_words = list(args.remainder)
        if prompt_words and prompt_words[0] == "--":
            prompt_words.pop(0)
        target_remainder = []
    prompt = " ".join(prompt_words).strip()
    if not prompt:
        raise HrnsClientError(
            'No prompt supplied. Usage: `hrns run --stdio -- CMD ARGS -- "your prompt"` or '
            '`hrns run --server team --agent research-assistant "your prompt"`.'
        )
    target = await _resolve_target(profiles, args, remainder=target_remainder)
    policy_name = "allow" if args.allow_all else "deny" if args.deny_all else "required"
    policy = HeadlessInteractions(policy_name, answers=_answers(args))
    client = HrnsAcpClient(
        lambda: transport_for(target),
        policy=policy,
        auth_method=target.profile.auth_method,
        client_io=parse_client_io(args.client_io or target.profile.client_io),
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


async def _command_agents(args: argparse.Namespace) -> int:
    _profile, catalog = await _resolve_catalog_profile(args)
    async with catalog:
        if args.agents_command == "show":
            deployment = await catalog.get_agent(args.agent)
            if args.json:
                print(json.dumps(deployment.raw, ensure_ascii=False, indent=2))
            else:
                _print_agent_detail(deployment)
            return EXIT_OK
        items: list[AgentDeployment] = []
        cursor = args.cursor
        pages = 0
        while True:
            page = await catalog.list_agents(cursor=cursor, limit=max(1, min(100, args.limit)))
            items.extend(page.items)
            cursor = page.next_cursor
            pages += 1
            if not cursor or not args.all or pages >= 20:
                break
        if args.json:
            print(
                json.dumps(
                    {"items": [item.raw for item in items], "nextCursor": cursor},
                    ensure_ascii=False,
                    indent=2,
                )
            )
        else:
            _print_agent_table(items)
        return EXIT_OK


def _bridge_remote_cwd(profile: Profile, override: str | None) -> str | None:
    """Explicit --remote-cwd wins; otherwise use the saved profile setting."""
    return override or profile.remote_cwd or None


async def _command_bridge(args: argparse.Namespace) -> int:
    profiles = _profiles(args)
    profile = _profile_from_args(profiles, args)
    if profile is None:
        raise HrnsClientError("Pass --server PROFILE (or --url URL) to bridge to a hosted agent.")
    if profile.transport != "server":
        raise HrnsClientError(f"Profile '{profile.name}' is not a server profile.")
    agent = await resolve_agent(profile, args.agent)
    catalog = catalog_for(profile)
    from .bridge import BridgeSession, run_bridge

    session = BridgeSession(
        url=catalog.websocket_url(agent.agent_id),
        token_ref=profile.token,
        remote_cwd=_bridge_remote_cwd(profile, args.remote_cwd),
        agent_id=agent.agent_id,
    )
    return await run_bridge(session)


def _command_profiles(args: argparse.Namespace) -> int:
    profiles = _profiles(args)
    if args.profiles_command == "list":
        if not profiles.profiles:
            print("No profiles saved. `hrns profiles add NAME --stdio -- CMD` creates one.")
            return EXIT_OK
        for name in sorted(profiles.profiles):
            profile = profiles.profiles[name]
            marker = "*" if profile.default_for_server else " "
            details = (
                profile.url + f" · {profile.agent}"
                if profile.is_remote
                else profile.describe_location()
            )
            print(f"{marker} {name:20} {profile.transport:7} {details}")
        if profiles.sources:
            print(f"\nLoaded from: {', '.join(str(path) for path in profiles.sources)}")
        return EXIT_OK
    if args.profiles_command == "path":
        print(profiles.sources[0] if profiles.sources else default_profiles_path())
        return EXIT_OK
    if args.profiles_command == "add":
        if not profiles.sources:
            profiles.sources.append(default_profiles_path())
        if args.stdio:
            command, _ = _split_stdio_remainder(args.remainder)
            profile = _stdio_profile(args.name, command, args.cwd)
        elif args.server:
            profile = Profile(
                name=args.name,
                transport="server",
                url=args.server,
                agent=args.agent or "",
                token=args.token or "",
                remote_cwd=args.remote_cwd or "",
            )
        else:
            raise HrnsClientError("`profiles add` needs --stdio (with `-- CMD`) or --server URL.")
        profiles.upsert(profile, set_default=args.default)
        path = profiles.save(profiles.sources[0])
        print(f"Saved profile '{profile.name}' to {path}")
        return EXIT_OK
    if args.profiles_command == "remove":
        if args.name not in profiles.profiles:
            raise HrnsClientError(f"No profile named '{args.name}'.")
        del profiles.profiles[args.name]
        path = profiles.save()
        print(f"Removed '{args.name}' from {path}")
        return EXIT_OK
    return EXIT_USAGE


async def _command_login(args: argparse.Namespace) -> int:
    target = login_target(args.provider, args.local_config)
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
            f'provider = "{result.provider}" in hrns.local.toml, or add and select it '
            "with /providers in the terminal.",
            flush=True,
        )
    return EXIT_OK


def _command_connect(args: argparse.Namespace) -> int:
    from .tui.app import run_terminal

    profiles = _profiles(args)
    preselect: ConnectionTarget | None = None
    status_message: str | None = None
    explicit = any(
        getattr(args, name, None)
        for name in ("stdio", "server", "url", "profile", "agent", "token", "local_config")
    ) or getattr(args, "local", False)
    try:
        preselect = asyncio.run(
            _resolve_target(profiles, args, remainder=args.remainder, split_prompt=False)
        )
    except HrnsClientError as exc:
        # Local mode is the default. If it is not configured yet, show the picker
        # with the local-model action selected instead of exiting.
        if explicit:
            raise
        status_message = str(exc)
    return run_terminal(
        profiles,
        initial_target=preselect,
        initial_session=args.session,
        status_message=status_message,
        client_io=parse_client_io(args.client_io) if args.client_io else None,
    )


# ------------------------------------------------------------------- output


def _print_agent_table(items: list[AgentDeployment]) -> None:
    if not items:
        print("No accessible agent deployments.")
        return
    id_width = max(len(item.agent_id) for item in items)
    print(f"{'ID':<{id_width}}  {'NAME':<28}  {'VERSION':<10}  WORKDIR")
    for item in items:
        name = (item.name + (" *" if item.recommended else ""))[:28]
        cwd = item.default_working_directory or (item.workspaces[0] if item.workspaces else "")
        print(f"{item.agent_id:<{id_width}}  {name:<28}  {item.version:<10}  {cwd}")


def _print_agent_detail(item: AgentDeployment) -> None:
    print(f"{item.label} ({item.agent_id})")
    if item.version:
        print(f"version: {item.version}")
    if item.recommended:
        print("recommended: yes")
    if item.description:
        print(f"\n{item.description}")
    if item.default_working_directory:
        print(f"\ndefault workspace: {item.default_working_directory}")
    for workspace in item.workspaces:
        print(f"workspace: {workspace}")


# ----------------------------------------------------------------------- mcp


def _mcp_config(args: argparse.Namespace) -> tuple[LocalSettings, Path]:
    """Load the local settings that own MCP servers, allowing a new file to be created."""
    explicit = getattr(args, "local_config", None)
    if explicit is not None:
        path = Path(explicit).expanduser().absolute()
        settings = (
            LocalSettings(config_file=path) if path.is_file() else LocalSettings(config_file=None)
        )
        return settings, path
    settings = LocalSettings()
    return settings, Path(settings.config_file or default_local_config_path())


def _mcp_pairs(values: Sequence[str], flag: str) -> dict[str, str]:
    pairs: dict[str, str] = {}
    for item in values:
        key, separator, value = item.partition("=")
        if not separator or not key.strip():
            raise HrnsClientError(f"{flag} expects KEY=VALUE, got '{item}'.")
        pairs[key.strip()] = value
    return pairs


def _mcp_server(name: str, args: argparse.Namespace) -> McpServerSettings:
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
        defer_loading=args.defer,
        description=args.description,
        include_tools=list(args.include_tools),
        exclude_tools=list(args.exclude_tools),
        timeout=args.timeout,
        oauth=oauth,
    )


def _mcp_server_or_error(settings: LocalSettings, name: str) -> McpServerSettings:
    try:
        return settings.mcp.servers[name]
    except KeyError:
        known = ", ".join(sorted(settings.mcp.servers)) or "(none)"
        raise HrnsClientError(f"No MCP server named '{name}'. Configured: {known}.") from None


def _mcp_list(args: argparse.Namespace) -> int:
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
    print(f"{'NAME':<{name_width}}  {'TRANSPORT':<9}  {'STATUS':<8}  MODE      TARGET")
    for name, server in sorted(servers.items()):
        status = "enabled" if server.enabled else "disabled"
        mode = "deferred" if server.defer_loading else "eager"
        print(
            f"{name:<{name_width}}  {server.transport:<9}  {status:<8}  {mode:<9} "
            f"{server_target(server)}"
        )
    print(f"\nConfig: {path}")
    store = settings.mcp.token_store_path or str(default_token_store_path())
    print(f"Tokens: {settings.mcp.token_store}:{store}")
    return EXIT_OK


def _mcp_get(args: argparse.Namespace) -> int:
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
    print(f"timeout: {server.timeout:g}s")
    print(f"config: {path}")
    return EXIT_OK


async def _mcp_auth(args: argparse.Namespace) -> int:
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


async def _mcp_logout(args: argparse.Namespace) -> int:
    settings, _path = _mcp_config(args)
    server = _mcp_server_or_error(settings, args.name)
    store = build_token_store(settings.mcp.token_store, settings.mcp.token_store_path)
    try:
        await clear_tokens(server, store)
    except Exception as exc:  # noqa: BLE001 - reported to the operator verbatim
        raise HrnsClientError(f"Cannot clear tokens for '{args.name}': {exc}") from exc
    print(f"Removed stored OAuth tokens for '{args.name}'.")
    return EXIT_OK


def _mcp_add(args: argparse.Namespace) -> int:
    settings, path = _mcp_config(args)
    if args.name in settings.mcp.servers and not args.force:
        raise HrnsClientError(
            f"MCP server '{args.name}' already exists; pass --force to replace it."
        )
    server = _mcp_server(args.name, args)
    settings.mcp.servers[args.name] = server
    settings.save(path, exclude_defaults=True)
    print(f"Saved MCP server '{args.name}' ({server.transport}) to {path}")
    if server.is_http and server.oauth is not False:
        print(f"Authorize it with: hrns mcp auth {args.name}")
    return EXIT_OK


def _mcp_remove(args: argparse.Namespace) -> int:
    settings, path = _mcp_config(args)
    _mcp_server_or_error(settings, args.name)
    del settings.mcp.servers[args.name]
    settings.save(path, exclude_defaults=True)
    print(f"Removed MCP server '{args.name}' from {path}")
    return EXIT_OK


def _mcp_set_enabled(args: argparse.Namespace, enabled: bool) -> int:
    settings, path = _mcp_config(args)
    server = _mcp_server_or_error(settings, args.name)
    server.enabled = enabled
    settings.save(path, exclude_defaults=True)
    print(f"{'Enabled' if enabled else 'Disabled'} MCP server '{args.name}'.")
    return EXIT_OK


# --------------------------------------------------------------------- main


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    tokens = list(sys.argv[1:] if argv is None else argv)
    try:
        tokens = _extract_global_profiles(tokens)
        if not tokens or tokens[0] == "--profiles":
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
