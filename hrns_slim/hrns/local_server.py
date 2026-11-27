"""Discover or start the built-in daemon without importing the agent runtime."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from collections.abc import Generator
from contextlib import contextmanager
from hashlib import sha256
from http.client import HTTPConnection, HTTPException
from pathlib import Path
from tempfile import NamedTemporaryFile

import psutil
from cyclopts import App
from platformdirs import user_runtime_path
from pydantic import BaseModel, Field, FiniteFloat, ValidationError

from .shared.config import HrnsConfig
from .shared.errors import HrnsClientError


class Endpoint(BaseModel):
    port: int = Field(ge=1, le=65535)
    token: str
    configuration: str


class DaemonProcess(BaseModel):
    pid: int = Field(gt=0)
    created_at: FiniteFloat = Field(gt=0)


@contextmanager
def publish_metadata(path: Path, metadata: BaseModel) -> Generator[None]:
    """Publish daemon metadata atomically and remove it on graceful exit."""
    with NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(metadata.model_dump_json().encode())
            stream.close()
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
    try:
        yield
    finally:
        path.unlink(missing_ok=True)


@contextmanager
def publish_daemon_process(directory: Path) -> Generator[None]:
    process = psutil.Process()
    identity = DaemonProcess(pid=process.pid, created_at=process.create_time())
    with publish_metadata(directory / "process.json", identity):
        yield


def runtime_directory(store: Path) -> Path:
    path = user_runtime_path("hrns") / _store_id(store)
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.name == "posix":
        os.chmod(path, 0o700)
    return path


def _store_id(store: Path) -> str:
    return sha256(os.fsencode(store.resolve())).hexdigest()[:24]


@contextmanager
def try_store_lock(store: Path) -> Generator[bool]:
    """Hold the daemon's exclusive store lock for the lifetime of the context."""
    with (runtime_directory(store) / "server.lock").open("ab") as lock:
        try:
            if os.name == "nt":
                import msvcrt

                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            acquired = False
        except OSError:
            if os.name != "nt":
                raise
            acquired = False
        else:
            acquired = True
        yield acquired


def load_settings(config: Path | None, *, env_only: bool) -> HrnsConfig:
    if env_only:
        return HrnsConfig(config_file=None)
    return HrnsConfig() if config is None else HrnsConfig(config_file=config)


def configuration_id(settings: HrnsConfig, *, env_only: bool) -> str:
    return "environment" if env_only else str(settings.path.resolve())


def _read_endpoint(path: Path) -> Endpoint | None:
    try:
        return Endpoint.model_validate_json((path / "endpoint.json").read_bytes())
    except (OSError, ValidationError):
        return None


def _request(endpoint: Endpoint, method: str, path: str) -> int | None:
    connection = HTTPConnection("127.0.0.1", endpoint.port, timeout=0.3)
    try:
        connection.request(method, path, headers={"Authorization": f"Bearer {endpoint.token}"})
        return connection.getresponse().status
    except (OSError, HTTPException):
        return None
    finally:
        connection.close()


def _start(
    settings: HrnsConfig, directory: Path, *, env_only: bool, setup: bool
) -> subprocess.Popen[bytes]:
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
    fd = os.open(directory / "server.log", os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "wb") as log:
        return subprocess.Popen(
            [sys.executable, "-m", "hrns.server.runtime", "--local-daemon", *args],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            **options,
        )


def _healthy_endpoint(directory: Path, settings: HrnsConfig, *, env_only: bool) -> Endpoint | None:
    endpoint = _read_endpoint(directory)
    if endpoint is None or _request(endpoint, "GET", "/health") != 204:
        return None
    if endpoint.configuration != configuration_id(settings, env_only=env_only):
        raise SystemExit(
            "The local Harness server uses a different configuration for this session store. "
            "Use the same configuration or select a separate store."
        )
    return endpoint


def _find_or_start(settings: HrnsConfig, *, env_only: bool, setup: bool) -> Endpoint:
    directory = runtime_directory(settings.store)
    child: subprocess.Popen[bytes] | None = None
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        endpoint = _healthy_endpoint(directory, settings, env_only=env_only)
        if endpoint is not None:
            return endpoint
        if child is None or child.poll() == 0:
            # Wait for any retiring daemon to release its store before starting another.
            with try_store_lock(settings.store) as acquired:
                if acquired:
                    child = _start(settings, directory, env_only=env_only, setup=setup)
        elif child.returncode is not None:
            endpoint = _healthy_endpoint(directory, settings, env_only=env_only)
            if endpoint is not None:
                return endpoint
            break
        time.sleep(0.1)
    raise SystemExit(f"Could not start the local Harness server. See {directory / 'server.log'}")


def _read_daemon_process(path: Path) -> DaemonProcess | None:
    try:
        return DaemonProcess.model_validate_json(path.read_bytes())
    except (OSError, ValidationError):
        return None


def _force_stop(store: Path) -> bool:
    directory = runtime_directory(store)
    process_path = directory / "process.json"
    terminated: DaemonProcess | None = None
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        with try_store_lock(store) as acquired:
            if acquired:
                process_path.unlink(missing_ok=True)
                (directory / "endpoint.json").unlink(missing_ok=True)
                return terminated is not None
        identity = _read_daemon_process(process_path)
        if identity is not None:
            if terminated is not None and identity != terminated:
                return True  # A new daemon acquired the store lock.
            if terminated is None:
                try:
                    process = psutil.Process(identity.pid)
                    if process.create_time() == identity.created_at:
                        process.kill()
                        terminated = identity
                except psutil.NoSuchProcess:
                    pass  # The daemon may have exited before the signal was sent.
                except psutil.Error as exc:
                    raise HrnsClientError(
                        f"Could not terminate the local Harness server: {exc}"
                    ) from exc
        time.sleep(0.1)
    if terminated is None:
        raise HrnsClientError("Could not verify the local Harness server process for force stop.")
    raise HrnsClientError(
        "Timed out waiting for the terminated local Harness server to release its lock."
    )


def stop(store: Path, *, force: bool = False) -> bool:
    """Stop the local daemon for this store and wait for it to release the lock."""
    if force:
        return _force_stop(store)
    directory = runtime_directory(store)
    stopped: Endpoint | None = None
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        with try_store_lock(store) as acquired:
            if acquired:
                return stopped is not None
        endpoint = _read_endpoint(directory)
        if stopped is not None:
            if endpoint is not None and endpoint != stopped:
                return True  # A new daemon acquired the lock after the old one stopped.
        elif endpoint is not None:
            status = _request(endpoint, "POST", "/shutdown")
            if status == 202:
                stopped = endpoint
            elif status is not None:
                raise HrnsClientError(
                    f"The local Harness server rejected shutdown (HTTP {status})."
                )
        time.sleep(0.1)
    raise HrnsClientError("Timed out waiting for the local Harness server to stop.")


def print_endpoint(
    *,
    config: Path | None = None,
    env_only: bool = False,
    setup: bool = False,
) -> None:
    """Print the endpoint for the native TUI. The TUI supplies its cwd over ACP."""
    settings = load_settings(config, env_only=env_only)
    endpoint = _find_or_start(settings, env_only=env_only, setup=setup)
    print(json.dumps({**endpoint.model_dump(), "store_id": _store_id(settings.store)}))


if __name__ == "__main__":
    App(name="hrns local server", default_command=print_endpoint)()
