"""``hrns`` command line: terminal app, headless runner, catalog and bridge."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any, NoReturn

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
from .launch import (
    ConnectionTarget,
    catalog_for,
    default_remote_cwd,
    local_cwd,
    resolve_agent,
    transport_for,
)
from .local import local_target
from .profiles import Profile, Profiles, default_profiles_path

__all__ = ["main", "build_parser"]


# --------------------------------------------------------------------- parser


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:  # pragma: no cover - argparse boilerplate
        self.print_usage(sys.stderr)
        self.exit(EXIT_USAGE, f"hrns: {message}\n")


def build_parser() -> argparse.ArgumentParser:
    parser = _ArgumentParser(
        prog="hrns",
        description="Keyboard-first ACP terminal, headless runner and stdio bridge.",
    )
    parser.add_argument("--version", action="version", version=f"hrns {__version__}")
    parser.add_argument(
        "--profiles",
        action="append",
        default=[],
        metavar="PATH",
        help="Load an explicit profile TOML file (repeatable). Also HRNS_PROFILES.",
    )
    sub = parser.add_subparsers(dest="command")

    connect = sub.add_parser("connect", help="Open the full-screen terminal")
    _add_target_flags(connect, enable_stdio=True)
    connect.add_argument(
        "--session", metavar="SESSION_ID", help="Load this session after connecting"
    )
    connect.add_argument("remainder", nargs=argparse.REMAINDER, help=argparse.SUPPRESS)

    run = sub.add_parser("run", help="Run one prompt headlessly and print the result")
    _add_target_flags(run, enable_stdio=True)
    run.add_argument("--json", action="store_true", help="Print one JSON summary object")
    _add_interaction_flags(run)
    run.add_argument("--resume", metavar="SESSION_ID", help="Load and continue an existing session")
    run.add_argument("--timeout", type=float, metavar="SECONDS")
    run.add_argument(
        "--attach",
        action="append",
        default=[],
        metavar="PATH",
        help="Explicitly attach a regular file to this prompt (repeatable)",
    )
    run.add_argument("remainder", nargs=argparse.REMAINDER, help=argparse.SUPPRESS)

    agents = sub.add_parser("agents", help="Inspect the hosted deployment catalog")
    agents_sub = agents.add_subparsers(dest="agents_command", required=True)
    agents_list = agents_sub.add_parser("list", help="List accessible agent deployments")
    agents_list.add_argument("--server", metavar="PROFILE", help="Server profile name (or URL)")
    agents_list.add_argument("--url", help="Server base URL when no profile is saved")
    agents_list.add_argument(
        "--token", metavar="REF", help="Bearer token reference (env:NAME or keyring:service/user)"
    )
    agents_list.add_argument("--limit", type=int, default=50)
    agents_list.add_argument("--cursor")
    agents_list.add_argument("--all", action="store_true", help="Follow pagination to the end")
    agents_list.add_argument("--json", action="store_true")
    agents_show = agents_sub.add_parser("show", help="Show one deployment descriptor")
    agents_show.add_argument("--server", metavar="PROFILE", help="Server profile name (or URL)")
    agents_show.add_argument("--url")
    agents_show.add_argument("--token", metavar="REF")
    agents_show.add_argument("agent")
    agents_show.add_argument("--json", action="store_true")

    bridge = sub.add_parser("bridge", help="Bridge local stdio ACP to a hosted agent")
    bridge.add_argument("--server", metavar="PROFILE", help="Server profile name (or URL)")
    bridge.add_argument("--url", help="Server base URL when no profile is saved")
    bridge.add_argument("--token", metavar="REF")
    bridge.add_argument("--agent", required=False, metavar="AGENT")
    bridge.add_argument(
        "--remote-cwd", metavar="PATH", help="Replace the local cwd with this remote directory"
    )
    bridge.add_argument("--profile", metavar="NAME")

    profiles = sub.add_parser("profiles", help="Manage saved connection profiles")
    profiles_sub = profiles.add_subparsers(dest="profiles_command", required=True)
    profiles_sub.add_parser("list", help="List saved profiles")
    profiles_sub.add_parser("path", help="Print the user profile file path")
    add = profiles_sub.add_parser("add", help="Add or replace a profile")
    add.add_argument("name")
    add.add_argument(
        "--stdio", action="store_true", help="Define a local launch; put the command after --"
    )
    add.add_argument("--server", metavar="URL", help="Define a hosted server profile")
    add.add_argument("--agent", help="Default agent id for a server profile")
    add.add_argument(
        "--token", metavar="REF", help="Token secret reference (env:NAME or keyring:service/user)"
    )
    add.add_argument("--cwd")
    add.add_argument("--remote-cwd")
    add.add_argument("--default", action="store_true", help="Mark as the preferred profile")
    add.add_argument("remainder", nargs=argparse.REMAINDER, help=argparse.SUPPRESS)
    remove = profiles_sub.add_parser("remove", help="Remove a saved profile")
    remove.add_argument("name")

    clean = sub.add_parser("clean", help="Delete local session history")
    clean.add_argument("-y", "--yes", action="store_true", help="Skip the confirmation prompt")
    clean.add_argument(
        "-n", "--dry-run", action="store_true", help="Show what would be deleted, then exit"
    )

    mcp = sub.add_parser("mcp", help="Manage MCP servers for the local agent")
    mcp.add_argument(
        "--local-config",
        type=Path,
        metavar="PATH",
        default=argparse.SUPPRESS,
        help="Local settings TOML file that holds the MCP servers",
    )
    mcp_sub = mcp.add_subparsers(dest="mcp_command")
    mcp.set_defaults(mcp_parser=mcp)

    def mcp_parser(
        name: str, help_text: str, *, aliases: tuple[str, ...] = ()
    ) -> argparse.ArgumentParser:
        child = mcp_sub.add_parser(name, help=help_text, aliases=list(aliases))
        child.add_argument(
            "--local-config",
            type=Path,
            metavar="PATH",
            default=argparse.SUPPRESS,
            help=argparse.SUPPRESS,
        )
        return child

    mcp_add = mcp_parser("add", "Add or replace an MCP server")
    mcp_add.add_argument("name", metavar="NAME")
    mcp_add.add_argument("--url", metavar="URL", help="Streamable HTTP or SSE endpoint")
    mcp_add.add_argument(
        "--command",
        dest="stdio_command",
        metavar="CMD",
        help="stdio executable (or put it after --)",
    )
    mcp_add.add_argument("--env", action="append", default=[], metavar="KEY=VALUE")
    mcp_add.add_argument("--cwd", metavar="DIR")
    mcp_add.add_argument("--header", action="append", default=[], metavar="KEY=VALUE")
    mcp_add.add_argument("--oauth-client-id")
    mcp_add.add_argument("--oauth-client-secret")
    mcp_add.add_argument("--oauth-scope", action="append", default=[], metavar="SCOPE")
    mcp_add.add_argument("--oauth-callback-port", type=int)
    mcp_add.add_argument("--no-oauth", action="store_true", help="Never attempt OAuth")
    mcp_add.add_argument(
        "--defer",
        action="store_true",
        help="Load this server's tools on demand instead of putting them in every turn",
    )
    mcp_add.add_argument(
        "--tool", dest="include_tools", action="append", default=[], metavar="NAME"
    )
    mcp_add.add_argument(
        "--exclude-tool", dest="exclude_tools", action="append", default=[], metavar="NAME"
    )
    mcp_add.add_argument("--timeout", type=float, default=10.0, metavar="SECONDS")
    mcp_add.add_argument("--disabled", action="store_true", help="Save the server disabled")
    mcp_add.add_argument("--force", action="store_true", help="Replace an existing server")
    mcp_add.add_argument(
        "command_words",
        nargs="*",
        metavar="-- COMMAND ARGS",
        help="stdio command and arguments, after `--`",
    )

    mcp_list = mcp_parser("list", "List configured MCP servers")
    mcp_list.add_argument("--json", action="store_true")

    mcp_get = mcp_parser("get", "Show one configured MCP server")
    mcp_get.add_argument("name")
    mcp_get.add_argument("--json", action="store_true")

    mcp_remove = mcp_parser("remove", "Remove a configured MCP server")
    mcp_remove.add_argument("name")

    for verb, verb_help in (
        ("enable", "Enable a server for new sessions"),
        ("disable", "Disable a server without removing its configuration"),
    ):
        child = mcp_parser(verb, verb_help)
        child.add_argument("name")

    mcp_auth = mcp_parser("auth", "Authenticate with an MCP server using OAuth", aliases=("login",))
    mcp_auth.add_argument("name")

    mcp_logout = mcp_parser("logout", "Forget stored OAuth tokens for an MCP server")
    mcp_logout.add_argument("name")
    return parser


def _add_target_flags(parser: argparse.ArgumentParser, *, enable_stdio: bool) -> None:
    if enable_stdio:
        parser.add_argument(
            "--stdio", action="store_true", help="Launch a local ACP agent; put argv after --"
        )
    parser.add_argument(
        "--local", action="store_true", help="Run the configured local developer agent (default)"
    )
    parser.add_argument(
        "--local-config",
        type=Path,
        metavar="PATH",
        help="Local model settings TOML (implies --local)",
    )
    parser.add_argument("--server", metavar="PROFILE", help="Use a saved server profile (or a URL)")
    parser.add_argument("--agent", metavar="AGENT", help="Agent deployment id or catalog name")
    parser.add_argument("--profile", metavar="NAME", help="Use a saved profile by name")
    parser.add_argument("--cwd", metavar="DIR", help="Working directory override")
    parser.add_argument("--url", help="Server base URL when no profile is saved")
    parser.add_argument("--token", metavar="REF", help="Bearer token reference")


def _add_interaction_flags(parser: argparse.ArgumentParser) -> None:
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--allow-all", action="store_true", help="Explicitly approve tool requests")
    group.add_argument("--deny-all", action="store_true", help="Explicitly reject tool requests")
    parser.add_argument(
        "--answer",
        action="append",
        default=[],
        metavar="FIELD=VALUE",
        help="Answer for a structured elicitation field (JSON value accepted)",
    )


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


def _command_mcp(args: argparse.Namespace) -> int:
    if not getattr(args, "mcp_command", None):
        args.mcp_parser.print_help()
        return EXIT_OK
    if args.mcp_command == "add":
        return _mcp_add(args)
    if args.mcp_command == "list":
        return _mcp_list(args)
    if args.mcp_command == "get":
        return _mcp_get(args)
    if args.mcp_command == "remove":
        return _mcp_remove(args)
    if args.mcp_command == "enable":
        return _mcp_set_enabled(args, True)
    if args.mcp_command == "disable":
        return _mcp_set_enabled(args, False)
    if args.mcp_command in {"auth", "login"}:
        return asyncio.run(_mcp_auth(args))
    if args.mcp_command == "logout":
        return asyncio.run(_mcp_logout(args))
    return EXIT_USAGE


# --------------------------------------------------------------------- main


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    if not args.command:
        defaults = argparse.Namespace(
            profiles=args.profiles,
            stdio=False,
            local=False,
            local_config=None,
            server=None,
            agent=None,
            profile=None,
            cwd=None,
            url=None,
            token=None,
            remainder=[],
            session=None,
        )
        try:
            return _command_connect(defaults)
        except KeyboardInterrupt:
            return 130
    try:
        if args.command == "connect":
            return _command_connect(args)
        if args.command == "run":
            return asyncio.run(_command_run(args))
        if args.command == "agents":
            return asyncio.run(_command_agents(args))
        if args.command == "bridge":
            return asyncio.run(_command_bridge(args))
        if args.command == "profiles":
            return _command_profiles(args)
        if args.command == "mcp":
            return _command_mcp(args)
        if args.command == "clean":
            from .clean import clean

            return clean(assume_yes=args.yes, dry_run=args.dry_run)
    except KeyboardInterrupt:
        return 130
    except InteractionRequired as exc:
        print(f"hrns: interaction-required: {exc.message}", file=sys.stderr)
        return 3
    except (HrnsClientError, KeyError) as exc:
        message = exc.args[0] if isinstance(exc, KeyError) and exc.args else str(exc)
        print(f"hrns: error: {message}", file=sys.stderr)
        return EXIT_USAGE if isinstance(exc, KeyError) else EXIT_FAILURE
    except (ValueError, OSError) as exc:
        print(f"hrns: error: {exc}", file=sys.stderr)
        return EXIT_FAILURE
    parser.print_help()
    return EXIT_USAGE


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
