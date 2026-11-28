"""Connection target resolution and ``connect`` / ``run`` / ``profiles`` / ``login`` handlers."""

from __future__ import annotations

import json
from collections.abc import Sequence
from types import SimpleNamespace
from typing import Any

from ..client.launch import ConnectionTarget, local_cwd, profile_target, transport_for
from ..client.runtime import agent_target
from ..shared.config import BUILTIN_CONNECTION, HrnsConfig, Profile, default_config_path
from ..shared.errors import HrnsClientError
from ..shared.exit_codes import EXIT_OK, EXIT_USAGE
from ..shared.urls import check_websocket_url
from .common import _config, _require_headless_extra, _require_server_extra


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


def _resolve_target(
    config: HrnsConfig,
    args: SimpleNamespace,
    *,
    remainder: Sequence[str] = (),
    split_prompt: bool = True,
    allow_local_setup: bool = False,
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
        return profile_target(profile, cwd_override, saved=False)
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
        return agent_target(
            getattr(args, "config", None), cwd_override, allow_setup=allow_local_setup
        )
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
        return agent_target(getattr(args, "config", None), cwd, allow_setup=True)
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
    _require_headless_extra()
    from ..client.client import HrnsAcpClient
    from ..client.headless import run_headless
    from ..client.interactions import HeadlessInteractions
    from ..client.io import parse_client_io

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
    target = _resolve_target(config, args, remainder=target_remainder)
    policy_name = "allow" if args.allow_all else "deny" if args.deny_all else "required"
    policy = HeadlessInteractions(policy_name, answers=_answers(args))
    client_io = args.client_io or target.profile.client_io
    if client_io is None and args.session and args.cwd is None:
        client_io = "off"
    from ..client.mcp_provider import ClientMcpProvider

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
    _require_server_extra()
    from getpass import getpass

    from ..server.provider_login import ProviderLogin, login_methods

    settings = _config(args)
    name = args.provider or settings.provider
    if not name:
        raise HrnsClientError(
            "Name a configured provider, for example `hrns login github-copilot`."
        )
    entry = settings.require_entry(name)
    methods = login_methods(entry.provider_id(name))
    method = methods[0]
    if len(methods) > 1:
        choices = " · ".join(f"{index + 1} {option}" for index, option in enumerate(methods))
        response = input(f"{choices} [1] ").strip() or "1"
        if response not in {str(index + 1) for index in range(len(methods))}:
            raise HrnsClientError("Choose one of the available sign-in methods.")
        method = methods[int(response) - 1]
    key = getpass("API key: ") if method == "api-key" else None
    login = ProviderLogin()
    previous_url = None
    try:
        progress = await login.start(name, entry, method, key)
        while not progress.get("done"):
            if (url := progress.get("url")) and url != previous_url:
                print(f"Open {url}", flush=True)
                if code := progress.get("code"):
                    print(f"Enter device code {code}", flush=True)
                previous_url = url
            progress = await login.status()
    finally:
        await login.cancel()
    print(f"Signed in to {name}.")
    return EXIT_OK


def _initial_connection(
    config: HrnsConfig, args: SimpleNamespace
) -> tuple[ConnectionTarget | None, str | None]:
    explicit = any(getattr(args, name, None) for name in ("stdio", "profile", "server")) or getattr(
        args, "local", False
    )
    command_words, _ = _split_stdio_remainder(args.remainder, split_prompt=False)
    try:
        if not explicit and not command_words:
            # A bare launch reopens the connection used last; the picker is the fallback.
            remembered = _remembered_target(config, args)
            if remembered is not None:
                return remembered, None
        return (
            _resolve_target(
                config, args, remainder=args.remainder, split_prompt=False, allow_local_setup=True
            ),
            None,
        )
    except HrnsClientError as exc:
        # A failed implicit target leaves the picker available.
        if explicit:
            raise
        return None, exc.message


def _command_connect(args: SimpleNamespace) -> int:
    config = _config(args)
    from ..client.native import native_executable, run_native_tui

    if native_executable() is None:
        raise HrnsClientError(
            "The native client is not installed. Install it with "
            "`pip install 'hrns-slim[client]>0.0.28'`, or run `just install` in a source "
            "checkout."
        )
    target, reason = _initial_connection(config, args)
    if target is None:
        raise HrnsClientError(reason or "The native client needs a connection target.")
    # Record the identity before exec. A profile or the built-in runtime reopens on a
    # bare launch; an ad-hoc command line has no document entry to reopen.
    config.remember_connection(config.connection_identity(target.profile))
    default_client_io = "off" if target.kind == "remote" else config.tui.client_io
    client_io = args.client_io or target.profile.client_io or default_client_io
    return run_native_tui(
        target,
        session=args.session,
        explicit_cwd=args.cwd,
        client_io=client_io,
        theme=config.tui.theme,
        config=config,
    )
