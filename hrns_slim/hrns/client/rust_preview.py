"""Launch the native TUI preview for an existing resolved connection."""

from __future__ import annotations

import os
import shutil
import subprocess
import sysconfig

from ..shared.errors import HrnsClientError
from .launch import ConnectionTarget
from .transport import secret_value


def run_rust_preview(target: ConnectionTarget) -> int:
    scripts = sysconfig.get_path("scripts")
    executable = shutil.which("hrns-tui", path=scripts) or shutil.which("hrns-tui")
    if executable is None:
        raise HrnsClientError("Install the hrns-tui preview wheel in this Python environment.")

    profile = target.profile
    env = dict(os.environ)
    env.pop("HRNS_TUI_TOKEN", None)
    argv = [executable, "--cwd", target.cwd]
    cwd = None
    if target.kind == "remote":
        argv.extend(("--server", profile.url))
        token = secret_value(profile.token)
        if token:
            env["HRNS_TUI_TOKEN"] = token
    else:
        cwd = target.cwd
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


def _exec_at(executable: str, argv: list[str], env: dict[str, str], cwd: str) -> None:
    """Set the child cwd for the SDK's stdio process launcher."""
    executable = os.path.abspath(executable)
    original_cwd = os.getcwd()
    os.chdir(cwd)
    try:
        os.execve(executable, argv, env)
    finally:
        os.chdir(original_cwd)
