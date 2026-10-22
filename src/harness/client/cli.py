"""``harness`` command line: terminal app, headless runner, catalog and bridge."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any, NoReturn

from ._version import __version__
from .catalog import AgentDeployment
from .client import HarnessAcpClient
from .errors import HarnessClientError, InteractionRequired, SecretMissing
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
        self.exit(EXIT_USAGE, f"harness: {message}\n")


def build_parser() -> argparse.ArgumentParser:
    parser = _ArgumentParser(
        prog="harness",
        description="Keyboard-first ACP terminal, headless runner and stdio bridge.",
    )
    parser.add_argument("--version", action="version", version=f"harness {__version__}")
    parser.add_argument(
        "--profiles",
        action="append",
        default=[],
        metavar="PATH",
        help="Load an explicit profile TOML file (repeatable). Also HARNESS_PROFILES.",
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
        raise HarnessClientError(
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
        raise HarnessClientError(
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
            raise HarnessClientError(
                "--local/--local-config cannot be combined with another connection target."
            )
        command, _prompt = _split_stdio_remainder(remainder, split_prompt=split_prompt)
        if command:
            if local_requested:
                raise HarnessClientError("Local mode does not accept an agent command.")
            raise HarnessClientError(
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
        raise HarnessClientError(
            "A command was supplied without --stdio. Use `--stdio -- CMD ARGS`."
        )
    profile = _profile_from_args(profiles, args)
    if profile is None:
        raise HarnessClientError(
            "No connection target. Use `--stdio -- CMD`, `--server PROFILE --agent AGENT`, or configure a local model."
        )
    if profile.transport == "stdio":
        if not profile.command:
            raise HarnessClientError(f"Profile '{profile.name}' has no command.")
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
            raise HarnessClientError(f"--answer expects FIELD=VALUE, got '{item}'")
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
        raise HarnessClientError("Pass --server PROFILE (or --url URL) to reach a catalog.")
    if profile.transport != "server":
        raise HarnessClientError(f"Profile '{profile.name}' is not a server profile.")
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
        raise HarnessClientError(
            'No prompt supplied. Usage: `hrns run --stdio -- CMD ARGS -- "your prompt"` or '
            '`hrns run --server team --agent research-assistant "your prompt"`.'
        )
    target = await _resolve_target(profiles, args, remainder=target_remainder)
    policy_name = "allow" if args.allow_all else "deny" if args.deny_all else "required"
    policy = HeadlessInteractions(policy_name, answers=_answers(args))
    client = HarnessAcpClient(
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
        raise HarnessClientError(
            "Pass --server PROFILE (or --url URL) to bridge to a hosted agent."
        )
    if profile.transport != "server":
        raise HarnessClientError(f"Profile '{profile.name}' is not a server profile.")
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
            raise HarnessClientError(
                "`profiles add` needs --stdio (with `-- CMD`) or --server URL."
            )
        profiles.upsert(profile, set_default=args.default)
        path = profiles.save(profiles.sources[0])
        print(f"Saved profile '{profile.name}' to {path}")
        return EXIT_OK
    if args.profiles_command == "remove":
        if args.name not in profiles.profiles:
            raise HarnessClientError(f"No profile named '{args.name}'.")
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
    except HarnessClientError as exc:
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
        if args.command == "clean":
            from .clean import clean

            return clean(assume_yes=args.yes, dry_run=args.dry_run)
    except KeyboardInterrupt:
        return 130
    except (HarnessClientError, SecretMissing, KeyError) as exc:
        message = exc.args[0] if isinstance(exc, KeyError) and exc.args else str(exc)
        print(f"harness: error: {message}", file=sys.stderr)
        return EXIT_USAGE if isinstance(exc, KeyError) else EXIT_FAILURE
    except InteractionRequired as exc:
        print(f"harness: interaction-required: {exc.message}", file=sys.stderr)
        return 3
    parser.print_help()
    return EXIT_USAGE


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
