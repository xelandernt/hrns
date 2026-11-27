"""Launch the native TUI for an existing resolved connection."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sysconfig

from ..shared.config import HrnsConfig
from ..shared.config.credentials import secret_value
from ..shared.config.settings import is_builtin_hrns
from ..shared.errors import HrnsClientError
from .launch import ConnectionTarget


def native_executable() -> str | None:
    """The native client installed in this environment, if any."""
    scripts = sysconfig.get_path("scripts")
    return shutil.which("hrns-tui", path=scripts) or shutil.which("hrns-tui")


def run_native_tui(
    target: ConnectionTarget,
    *,
    session: str | None,
    explicit_cwd: str | None,
    client_io: str = "all",
    theme: str | None = None,
    config: HrnsConfig,
) -> int:
    executable = native_executable()
    if executable is None:
        raise HrnsClientError("Install the hrns-tui wheel in this Python environment.")

    profile = target.profile
    builtin = is_builtin_hrns(profile)
    env = dict(os.environ)
    env.pop("HRNS_TUI_TOKEN", None)
    argv = [executable, "--client-io", client_io]
    effort = _effort_profile(config, target)
    if effort is not None:
        argv.extend(("--effort-profile", json.dumps(effort)))
    if theme:
        argv.extend(("--theme", theme))
    if config.config_file:
        argv.extend(("--mcp-config", str(config.path)))
    if session:
        argv.extend(("--session", session))
    if session is None or explicit_cwd is not None or (target.kind != "remote" and not builtin):
        argv.extend(("--cwd", target.cwd))
    cwd = None
    if target.kind == "remote":
        argv.extend(("--server", profile.url))
        token = secret_value(profile.token)
        if token:
            env["HRNS_TUI_TOKEN"] = token
    else:
        cwd = target.cwd
        if builtin:
            if profile.args[:2] == ["-m", "hrns.server.runtime"]:
                args = ["-m", "hrns.local_server", *profile.args[2:]]
            else:
                args = [*profile.args, "--local-endpoint"]
            argv.extend(("--local", profile.command, *args))
        else:
            argv.extend(("--stdio", profile.command, *profile.args))
        env.update({name: secret_value(reference) or "" for name, reference in profile.env.items()})

    if os.name == "posix":
        try:
            if cwd is None:
                os.execve(os.path.abspath(executable), argv, env)
            else:
                _exec_at(executable, argv, env, cwd)
        except OSError as exc:
            raise HrnsClientError(f"Could not launch the native TUI: {exc.strerror}") from exc
    try:
        return subprocess.run(argv, cwd=cwd, env=env, check=False).returncode
    except KeyboardInterrupt:
        return 130
    except OSError as exc:
        raise HrnsClientError(f"Could not launch the native TUI: {exc.strerror}") from exc


def _effort_profile(config: HrnsConfig, target: ConnectionTarget) -> dict[str, object] | None:
    """Resolve the named connection and its TOML source in the launcher's workspace."""
    name = target.profile_name
    builtin = is_builtin_hrns(target.profile)
    if name is None and not builtin:
        return None
    return {
        "path": str(config.path.resolve()),
        "profile": name,
        "builtin": builtin,
        "values": config.local_model_effort if name is None else target.profile.model_effort,
    }


def _exec_at(executable: str, argv: list[str], env: dict[str, str], cwd: str) -> None:
    """Set the child cwd for the SDK's stdio process launcher."""
    executable = os.path.abspath(executable)
    original_cwd = os.getcwd()
    os.chdir(cwd)
    try:
        os.execve(executable, argv, env)
    finally:
        os.chdir(original_cwd)
