"""``hrns`` command line: terminal app and headless runner."""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from typing import Annotated

from cyclopts import App, CycloptsError, Parameter

from .._version import __version__
from ..shared.errors import HrnsClientError, InteractionRequired
from ..shared.exit_codes import EXIT_FAILURE, EXIT_OK, EXIT_USAGE
from .common import (
    ClientIoOption,
    ConfigOption,
    ExcludeToolOption,
    RepeatedStringOption,
    ToolOption,
    _namespace,
    _require_server_extra,
)
from .config_cmd import (
    _config_get_command,
    _config_keys_command,
    _config_path_command,
    _config_show_command,
)
from .connect import (
    _command_connect,
    _command_login,
    _command_profiles,
    _command_run,
)
from .mcp_cmd import (
    _mcp_add,
    _mcp_auth,
    _mcp_get,
    _mcp_list,
    _mcp_logout,
    _mcp_remove,
    _mcp_set_enabled,
)

__all__ = ["main", "build_parser"]


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
    from ..shared.clean import clean

    return clean(assume_yes=yes, dry_run=dry_run, config=config, keys=keys)


def _stop_command(*, config: ConfigOption = None) -> int:
    from ..local_server import load_settings, stop

    if stop(load_settings(config, env_only=False).store):
        print("Stopped the local Harness server.")
    else:
        print("The local Harness server is already stopped.")
    return EXIT_OK


def _agent_command(
    *,
    config: ConfigOption = None,
    env_only: Annotated[
        bool,
        Parameter(help="Ignore the configuration file and use environment variables only."),
    ] = False,
    catalog: Annotated[bool, Parameter(help="Print the provider/model catalog.")] = False,
    cwd: Annotated[str, Parameter(help="Workspace root.")] = ".",
    local_endpoint: Annotated[bool, Parameter(show=False)] = False,
) -> int:
    _require_server_extra()
    if local_endpoint:
        from ..local_server import print_endpoint

        print_endpoint(config=config, env_only=env_only)
        return EXIT_OK
    from ..server.runtime import run as run_agent

    run_agent(config=config, env_only=env_only, catalog=catalog, cwd=cwd)
    return EXIT_OK


def _acp_command(
    *,
    model: Annotated[
        str | None, Parameter(help="Pydantic AI model id; defaults to HRNS_MODEL.")
    ] = None,
    cwd: Annotated[str, Parameter(help="Workspace root.")] = ".",
) -> int:
    _require_server_extra()
    from ..server.acp import run_acp_stdio
    from ..server.agent import build_agent

    run_acp_stdio(lambda _session: build_agent(model, cwd=cwd))
    return EXIT_OK


def _serve_command(
    *,
    model: Annotated[
        str | None, Parameter(help="Pydantic AI model id; defaults to HRNS_MODEL.")
    ] = None,
    cwd: Annotated[str, Parameter(help="Root directory allowed for session workspaces.")] = ".",
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

    from ..server import AcpServer
    from ..server.agent import build_agent
    from ..server.fastapi import _ACP_MESSAGE_LIMIT

    server = AcpServer[None]()
    workspace_root = Path(cwd).expanduser().resolve()
    if not workspace_root.is_dir():
        raise HrnsClientError("The server workspace root must be an existing directory.")
    server.add_agent(
        "agent",
        factory=lambda session: build_agent(model, cwd=session.cwd),
        workspace_root=workspace_root,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async with server:
            yield

    app = FastAPI(lifespan=lifespan)
    server.mount_acp(app)
    uvicorn.run(app, host=host, port=port, ws_max_size=_ACP_MESSAGE_LIMIT)
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
    app.command(_login_command, name="login", help="Sign in to a configured provider")
    app.command(_stop_command, name="stop", help="Stop the local server and cancel unfinished runs")
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
