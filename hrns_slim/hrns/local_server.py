"""Discover or start the built-in daemon without importing the agent runtime."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from hashlib import sha256
from http.client import HTTPConnection, HTTPException
from pathlib import Path
from typing import Annotated

from cyclopts import App, Parameter
from platformdirs import user_runtime_path
from pydantic import BaseModel, Field, ValidationError

from .shared.config import HrnsConfig


class Endpoint(BaseModel):
    port: int = Field(ge=1, le=65535)
    token: str
    configuration: str


def runtime_directory(store: Path) -> Path:
    path = user_runtime_path("hrns") / _store_id(store)
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    return path


def _store_id(store: Path) -> str:
    return sha256(os.fsencode(store.resolve())).hexdigest()[:24]


def load_settings(config: Path | None, *, env_only: bool) -> HrnsConfig:
    if env_only:
        return HrnsConfig(config_file=None)
    return HrnsConfig() if config is None else HrnsConfig(config_file=config)


def configuration_id(settings: HrnsConfig, *, env_only: bool) -> str:
    return "environment" if env_only else str(settings.path.resolve())


def _running(path: Path) -> Endpoint | None:
    try:
        endpoint = Endpoint.model_validate_json((path / "endpoint.json").read_bytes())
    except (OSError, ValidationError):
        return None
    connection = HTTPConnection("127.0.0.1", endpoint.port, timeout=0.3)
    try:
        connection.request("GET", "/health", headers={"Authorization": f"Bearer {endpoint.token}"})
        return endpoint if connection.getresponse().status == 204 else None
    except (OSError, HTTPException):
        return None
    finally:
        connection.close()


def _find_or_start(settings: HrnsConfig, *, env_only: bool, setup: bool) -> Endpoint:
    directory = runtime_directory(settings.store)
    endpoint = _running(directory)
    if endpoint is None:
        args = ["--env-only"] if env_only else []
        if settings.config_file is not None:
            args.extend(["--config", str(settings.path)])
        if setup:
            args.append("--setup")
        options = (
            {"creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP}
            if os.name == "nt"
            else {"start_new_session": True}
        )
        fd = os.open(directory / "server.log", os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "ab") as log:
            child = subprocess.Popen(
                [sys.executable, "-m", "hrns.server.runtime", "--local-daemon", *args],
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                **options,
            )
        deadline = time.monotonic() + 20
        while endpoint is None and time.monotonic() < deadline:
            child.poll()
            time.sleep(0.1)
            endpoint = _running(directory)
    if endpoint is None:
        raise SystemExit(
            f"Could not start the local Harness server. See {directory / 'server.log'}"
        )
    if endpoint.configuration != configuration_id(settings, env_only=env_only):
        raise SystemExit(
            "The local Harness server uses a different configuration for this session store. "
            "Use the same configuration or select a separate store."
        )
    return endpoint


def print_endpoint(
    *,
    config: Path | None = None,
    env_only: bool = False,
    setup: bool = False,
    cwd: Annotated[str, Parameter(show=False)] = ".",
) -> None:
    """Print the endpoint for the native TUI. The TUI supplies its cwd over ACP."""
    settings = load_settings(config, env_only=env_only)
    endpoint = _find_or_start(settings, env_only=env_only, setup=setup)
    print(json.dumps({**endpoint.model_dump(), "store_id": _store_id(settings.store)}))


if __name__ == "__main__":
    App(name="hrns local server", default_command=print_endpoint)()
