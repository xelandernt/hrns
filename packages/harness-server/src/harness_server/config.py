"""TOML deployment configuration for ``harness-server``.

A deployment record is an **immutable revision**: ``id`` names one exact
installed environment (definition version, resolved dependencies, lockfile,
worker argv).  A new environment gets a new ``id``; ids are never reused.
Principals hold bearer-token *hashes* and the immutable agent revisions they
may use.

Configuration is validated eagerly at load time so a server either starts
with a coherent deployment catalog or fails with an actionable message.
"""

from __future__ import annotations

import hashlib
import os
import re
import tomllib
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

__all__ = [
    "ConfigError",
    "finalize_config",
    "DeploymentConfig",
    "HarnessServerConfig",
    "PrincipalConfig",
    "ServerSettings",
    "load_config",
    "sha256_hex",
]

_ID_PATTERN = r"^[a-z0-9][a-z0-9._-]{0,127}$"
_ENV_KEY_PATTERN = r"^[A-Z_][A-Z0-9_]*$"
_HEX64_PATTERN = r"^[0-9a-f]{64}$"


class ConfigError(Exception):
    """Raised when a deployment configuration file is unusable."""


def sha256_hex(value: str) -> str:
    """Return the lowercase SHA-256 hex digest of ``value`` (UTF-8)."""

    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class ServerSettings(BaseModel):
    """Process-level server settings (bind address, limits, timeouts)."""

    model_config = ConfigDict(extra="forbid")

    host: str = "127.0.0.1"
    port: int = Field(default=8080, ge=1, le=65535)
    #: Enforce WSS/HTTPS for non-loopback clients.  Loopback development
    #: clients may use plain ``ws``/``http`` even when this is true.
    require_tls: bool = True
    #: Addresses whose ``X-Forwarded-Proto`` header is honoured when the
    #: server runs behind an external TLS terminator.  ``["*"]`` trusts all.
    forwarded_allow_ips: list[str] = Field(default_factory=lambda: ["127.0.0.1"])
    store_path: str = "var/harness-store"
    #: Optional PostgreSQL URI DSN shared by deployment workers. When set, it is
    #: passed as ``HARNESS_STORE`` instead of a per-revision SQLite file. The worker
    #: environment must install ``harness-runtime[postgres]``; this server never
    #: imports or validates the driver itself.  Keyword DSNs are rejected because the
    #: generic worker selector only recognises URI prefixes.
    store_url: str | None = None
    log_level: Literal["debug", "info", "warning", "error", "critical"] = "info"
    root_path: str = ""

    # Capacity (see docs/SERVER.md "Limits").
    max_worker_count: int = Field(default=32, ge=1)
    max_connections_per_principal: int = Field(default=4, ge=1)

    #: Host environment variables copied into each worker.  Everything not
    #: listed here is withheld, so server secrets never reach workers by
    #: accident.
    worker_env_passthrough: list[str] = Field(
        default_factory=lambda: ["PATH", "HOME", "TMPDIR"]
    )

    #: Time given to a worker to exit after its stdin is closed (EOF).
    worker_term_grace_seconds: float = Field(default=10.0, gt=0)
    #: Time between SIGTERM and SIGKILL.
    worker_kill_grace_seconds: float = Field(default=5.0, gt=0)
    #: Frames queued toward a slow consumer before stall handling starts.
    outbound_queue_limit: int = Field(default=1024, ge=8)
    #: Time a full outbound queue may remain stuck before disconnecting.
    outbound_stall_timeout_seconds: float = Field(default=30.0, gt=0)
    #: Bytes of stderr retained per worker for diagnostics (never forwarded).
    stderr_tail_bytes: int = Field(default=65536, ge=0, le=4 * 1024 * 1024)

    #: Optional in-process TLS.  External TLS termination behind a proxy is
    #: the recommended deployment; these are for single-binary deployments.
    ssl_certfile: str | None = None
    ssl_keyfile: str | None = None

    @field_validator("store_path")
    @classmethod
    def _store_path_not_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("server.store_path must not be empty")
        return value

    @field_validator("store_url")
    @classmethod
    def _store_url_not_empty(cls, value: str | None) -> str | None:
        if value is None:
            return None
        resolved = value.strip()
        if not resolved:
            raise ValueError("server.store_url must be a non-empty DSN when set")
        # Workers select PostgreSQL only for a URI DSN.  A libpq keyword DSN
        # (``host=... dbname=...``) would otherwise be treated as a SQLite
        # filename by harness-runtime, silently splitting session storage.
        if not resolved.lower().startswith(("postgres://", "postgresql://")):
            raise ValueError(
                "server.store_url must be a PostgreSQL URI DSN "
                "(postgresql://user:password@host/database); libpq keyword DSNs are "
                "not selected by HARNESS_STORE"
            )
        return resolved

    @field_validator("worker_env_passthrough")
    @classmethod
    def _passthrough_names(cls, value: list[str]) -> list[str]:
        for name in value:
            if not re.fullmatch(_ENV_KEY_PATTERN, name):
                raise ValueError(f"invalid environment variable name {name!r}")
        return value

    @model_validator(mode="after")
    def _tls_pair(self) -> "ServerSettings":
        if bool(self.ssl_certfile) != bool(self.ssl_keyfile):
            raise ValueError("server.ssl_certfile and server.ssl_keyfile must be set together")
        return self


class DeploymentConfig(BaseModel):
    """One immutable deployed agent revision and its worker launch recipe."""

    model_config = ConfigDict(extra="forbid")

    #: Immutable revision identity; assigned when the environment is built.
    id: str = Field(pattern=_ID_PATTERN)
    name: str = Field(min_length=1, max_length=128)
    version: str = Field(min_length=1, max_length=64)
    description: str = ""
    #: Recommended revision for new sessions. Older/newer revisions stay addressable.
    recommended: bool = False

    #: Worker argv, executed directly (never through a shell).
    command: list[str] = Field(min_length=1)
    #: Worker working directory; relative paths resolve against the config.
    cwd: str | None = None

    #: Workspace roots this deployment offers.  Sent to the worker as
    #: ``HARNESS_ALLOWED_CWDS``; the host validates session cwd against it.
    allowed_cwds: list[str] = Field(default_factory=list)
    default_cwd: str | None = None

    #: Deployment resource bindings: literal values configured by the host
    #: operator (never client supplied) plus names read from the server's
    #: own environment at spawn time.
    env: dict[str, str] = Field(default_factory=dict)
    env_from_environment: list[str] = Field(default_factory=list)

    #: Resolved lockfile metadata for this revision.
    lockfile_path: str | None = None
    lockfile_sha256: str | None = Field(default=None, pattern=_HEX64_PATTERN)

    @field_validator("command")
    @classmethod
    def _argv(cls, value: list[str]) -> list[str]:
        if not value[0].strip():
            raise ValueError("command[0] must be a program name or path")
        for part in value:
            if "\x00" in part:
                raise ValueError("command argv entries must not contain NUL bytes")
        return value

    @field_validator("allowed_cwds")
    @classmethod
    def _absolute_roots(cls, value: list[str]) -> list[str]:
        seen: set[str] = set()
        for root in value:
            if not os.path.isabs(root):
                raise ValueError(f"allowed_cwds entries must be absolute paths: {root!r}")
            normalized = os.path.normpath(root)
            if normalized != root:
                raise ValueError(
                    f"allowed_cwds entries must be normalized absolute paths: {root!r}"
                )
            if normalized in seen:
                raise ValueError(f"duplicate allowed_cwds entry: {root!r}")
            seen.add(normalized)
        return value

    @field_validator("env")
    @classmethod
    def _resource_env(cls, value: dict[str, str]) -> dict[str, str]:
        for key in value:
            if not re.fullmatch(_ENV_KEY_PATTERN, key):
                raise ValueError(f"invalid resource environment variable name: {key!r}")
            if key.startswith("HARNESS_"):
                raise ValueError(
                    f"resource environment {key!r} uses the reserved HARNESS_ namespace"
                )
        return value

    @field_validator("env_from_environment")
    @classmethod
    def _referenced_env(cls, value: list[str]) -> list[str]:
        for name in value:
            if not re.fullmatch(_ENV_KEY_PATTERN, name):
                raise ValueError(f"invalid environment variable reference: {name!r}")
            if name.startswith("HARNESS_"):
                raise ValueError(
                    f"environment reference {name!r} uses the reserved HARNESS_ namespace"
                )
        return value

    @model_validator(mode="after")
    def _default_within_roots(self) -> "DeploymentConfig":
        if self.default_cwd is not None:
            if not os.path.isabs(self.default_cwd):
                raise ValueError("deployment.default_cwd must be an absolute path")
            if self.default_cwd not in self.allowed_cwds:
                raise ValueError("deployment.default_cwd must be listed in allowed_cwds")
        if self.lockfile_sha256 is not None and self.lockfile_path is None:
            raise ValueError("deployment.lockfile_sha256 requires deployment.lockfile_path")
        return self


class PrincipalConfig(BaseModel):
    """A calling principal: token hashes and the deployments it may use."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=_ID_PATTERN)
    #: SHA-256 hex digests of bearer tokens.  Plaintext tokens never appear
    #: in server configuration.
    token_hashes: list[str] = Field(min_length=1)
    allowed_agent_ids: list[str] = Field(min_length=1)

    @field_validator("token_hashes")
    @classmethod
    def _hashes(cls, value: list[str]) -> list[str]:
        for digest in value:
            if not re.fullmatch(_HEX64_PATTERN, digest):
                raise ValueError(
                    "principal.token_hashes entries must be lowercase SHA-256 hex digests"
                )
        if len(set(value)) != len(value):
            raise ValueError("duplicate token hash in principal")
        return value

    @field_validator("allowed_agent_ids")
    @classmethod
    def _agents(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise ValueError("duplicate allowed_agent_ids entry in principal")
        for agent_id in value:
            if not re.fullmatch(_ID_PATTERN, agent_id):
                raise ValueError(f"invalid agent id in allowed_agent_ids: {agent_id!r}")
        return value


class HarnessServerConfig(BaseModel):
    """Fully validated server configuration."""

    model_config = ConfigDict(extra="forbid")

    server: ServerSettings = Field(default_factory=ServerSettings)
    deployments: list[DeploymentConfig] = Field(min_length=1)
    principals: list[PrincipalConfig] = Field(min_length=1)

    @model_validator(mode="after")
    def _cross_references(self) -> "HarnessServerConfig":
        deployment_ids = [d.id for d in self.deployments]
        if len(set(deployment_ids)) != len(deployment_ids):
            dupes = sorted({i for i in deployment_ids if deployment_ids.count(i) > 1})
            raise ValueError(f"duplicate deployment ids: {', '.join(dupes)}")

        recommended_names = [d.name for d in self.deployments if d.recommended]
        if len(set(recommended_names)) != len(recommended_names):
            dupes = sorted(
                {name for name in recommended_names if recommended_names.count(name) > 1}
            )
            raise ValueError(
                "multiple revisions are recommended for the same agent name: "
                + ", ".join(dupes)
            )

        principal_ids = [p.id for p in self.principals]
        if len(set(principal_ids)) != len(principal_ids):
            dupes = sorted({i for i in principal_ids if principal_ids.count(i) > 1})
            raise ValueError(f"duplicate principal ids: {', '.join(dupes)}")

        hash_owner: dict[str, str] = {}
        for principal in self.principals:
            for digest in principal.token_hashes:
                if digest in hash_owner:
                    raise ValueError(
                        "the same token hash is assigned to multiple principals "
                        f"({hash_owner[digest]!r} and {principal.id!r})"
                    )
                hash_owner[digest] = principal.id

        known = set(deployment_ids)
        for principal in self.principals:
            missing = [a for a in principal.allowed_agent_ids if a not in known]
            if missing:
                raise ValueError(
                    f"principal {principal.id!r} allows unknown deployments: "
                    + ", ".join(missing)
                )
        return self


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ConfigError(f"cannot read configuration {path}: {exc}") from exc
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise ConfigError(f"configuration {path} is not UTF-8: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML in {path}: {exc}") from exc
    if not isinstance(data, dict):  # pragma: no cover - TOML root is a table
        raise ConfigError(f"configuration {path} must be a TOML table")
    return data


def _resolve_against(base: Path, value: str) -> str:
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = base / candidate
    return str(candidate.resolve())


def _verify_lockfile(deployment: DeploymentConfig, config_dir: Path) -> None:
    if deployment.lockfile_path is None:
        return
    path = Path(_resolve_against(config_dir, deployment.lockfile_path))
    try:
        content = path.read_bytes()
    except OSError as exc:
        raise ConfigError(
            f"deployment {deployment.id!r}: lockfile {path} is not readable: {exc}"
        ) from exc
    digest = hashlib.sha256(content).hexdigest()
    if deployment.lockfile_sha256 is not None and digest != deployment.lockfile_sha256:
        raise ConfigError(
            f"deployment {deployment.id!r}: lockfile digest mismatch "
            f"(expected {deployment.lockfile_sha256}, got {digest})"
        )
    deployment.lockfile_path = str(path)


def _verify_absolute_command(deployment: DeploymentConfig) -> None:
    program = deployment.command[0]
    if os.path.isabs(program) and not os.access(program, os.X_OK):
        raise ConfigError(
            f"deployment {deployment.id!r}: command is not an executable file: {program}"
        )


def finalize_config(config: HarnessServerConfig, base: Path) -> HarnessServerConfig:
    """Resolve filesystem paths and run load-time IO checks."""

    config.server.store_path = _resolve_against(base, config.server.store_path)
    if config.server.ssl_certfile is not None:
        config.server.ssl_certfile = _resolve_against(base, config.server.ssl_certfile)
    if config.server.ssl_keyfile is not None:
        config.server.ssl_keyfile = _resolve_against(base, config.server.ssl_keyfile)
    for deployment in config.deployments:
        if deployment.cwd is not None:
            deployment.cwd = _resolve_against(base, deployment.cwd)
        _verify_lockfile(deployment, base)
        _verify_absolute_command(deployment)

    if not config.server.forwarded_allow_ips:
        raise ConfigError("server.forwarded_allow_ips must contain at least one entry")
    return config


def load_config(path: str | os.PathLike[str]) -> HarnessServerConfig:
    """Parse, validate and normalize a deployment TOML file.

    Relative paths inside the file (``store_path``, worker ``cwd``, lockfile)
    resolve against the configuration file's directory and are rewritten to
    absolute paths on the returned model.
    """

    config_path = Path(path).expanduser().resolve()
    data = _read_toml(config_path)
    base = config_path.parent

    # Accept both a ``[[deployment]]`` array and a ``[deployment.<id>]``
    # mapping keyed by id, which is convenient for hand-maintained files.
    raw_deployments = data.get("deployment")
    if isinstance(raw_deployments, dict):
        normalized: list[dict[str, Any]] = []
        for key, value in raw_deployments.items():
            if not isinstance(value, dict):
                raise ConfigError(f"deployment.{key} must be a TOML table")
            item = dict(value)
            item.setdefault("id", key)
            normalized.append(item)
        data["deployment"] = normalized
    elif raw_deployments is None:
        data["deployment"] = []

    raw = {
        "server": data.get("server", {}),
        "deployments": data.get("deployment", []),
        "principals": data.get("principal", []),
    }
    if not isinstance(raw["server"], dict):
        raise ConfigError("server must be a TOML table")
    if not isinstance(raw["deployments"], list):
        raise ConfigError("deployment must be an array of tables ([[deployment]])")
    if not isinstance(raw["principals"], list):
        raise ConfigError("principal must be an array of tables ([[principal]])")

    # A ``[deployment.lockfile]`` table with ``path``/``sha256`` is accepted as
    # a nested spelling of the flat ``lockfile_path``/``lockfile_sha256``
    # fields, which keeps hand-written files readable.
    for item in raw["deployments"]:
        if not isinstance(item, dict):  # pragma: no cover - pydantic reports it
            continue
        lockfile = item.pop("lockfile", None)
        if lockfile is None:
            continue
        if not isinstance(lockfile, dict):
            raise ConfigError("deployment.lockfile must be a TOML table")
        unknown = set(lockfile) - {"path", "sha256"}
        if unknown:
            raise ConfigError(
                "deployment.lockfile accepts only path and sha256; got "
                + ", ".join(sorted(unknown))
            )
        if "path" in lockfile:
            item.setdefault("lockfile_path", lockfile["path"])
        if "sha256" in lockfile:
            item.setdefault("lockfile_sha256", lockfile["sha256"])

    try:
        config = HarnessServerConfig.model_validate(raw)
    except ValueError as exc:
        raise ConfigError(f"invalid configuration in {config_path}: {exc}") from exc

    return finalize_config(config, base)
