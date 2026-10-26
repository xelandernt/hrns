"""MCP server configuration and client helpers shared by CLI and runtime.

The models are plain Pydantic so the client can read and write ``hrns.local.toml``
without importing the runtime. Everything that touches FastMCP (transports, OAuth,
token storage) imports lazily so ``hrns mcp`` stays cheap to start.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from platformdirs import user_data_path
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

__all__ = [
    "MCP_NAME_PATTERN",
    "McpOAuthConfig",
    "McpServerConfig",
    "McpServerSettings",
    "McpSettings",
    "build_client",
    "build_token_store",
    "build_transport",
    "clear_tokens",
    "default_token_store_path",
    "expand_env",
    "expand_env_map",
    "has_tokens",
    "server_name",
    "server_target",
]

MCP_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
"""Server names become binding ids, so keep them short and shell-friendly."""

_ENV_REFERENCE = re.compile(r"\{env:([A-Za-z_][A-Za-z0-9_]*)\}")


class McpOAuthConfig(BaseModel):
    """Pre-registered OAuth client details; DCR is used when these are absent."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    client_id: str = ""
    client_secret: str = ""
    scopes: list[str] = Field(default_factory=list)
    callback_port: int | None = Field(default=None, ge=1, le=65535)


class McpServerSettings(BaseModel):
    """One MCP server as it is written in ``[mcp.servers.<name>]``.

    Exactly one of ``command`` (stdio) or ``url`` (streamable HTTP or SSE) is set.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    command: str = ""
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    cwd: str = ""
    url: str = ""
    headers: dict[str, str] = Field(default_factory=dict)
    enabled: bool = True
    defer_loading: bool = False
    include_tools: list[str] = Field(default_factory=list)
    exclude_tools: list[str] = Field(default_factory=list)
    timeout: float = Field(default=10.0, gt=0)
    oauth: McpOAuthConfig | bool = True
    """``true``/object: use OAuth (and dynamic registration) for an HTTP server.
    ``false``: never attempt OAuth; tokens must arrive through ``headers``."""

    @model_validator(mode="after")
    def _one_transport(self) -> McpServerSettings:
        if bool(self.command) == bool(self.url):
            raise ValueError("set exactly one of command (stdio) or url (HTTP)")
        if self.url:
            parsed = urlsplit(self.url)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                raise ValueError("url must be an http(s) URL")
            if self.args:
                raise ValueError("args only apply to a stdio command")
            if self.cwd:
                raise ValueError("cwd only applies to a stdio command")
        if self.include_tools and self.exclude_tools:
            raise ValueError("use include_tools or exclude_tools, not both")
        return self

    @property
    def is_http(self) -> bool:
        return bool(self.url)

    @property
    def transport(self) -> Literal["stdio", "http"]:
        return "http" if self.is_http else "stdio"

    def oauth_config(self) -> McpOAuthConfig | None:
        """Normalized OAuth settings, or ``None`` when OAuth is disabled."""
        if self.oauth is False or not self.is_http:
            return None
        if self.oauth is True:
            return McpOAuthConfig()
        return self.oauth


class McpServerConfig(McpServerSettings):
    """The runtime view of one server, including where its OAuth tokens live."""

    token_store: Literal["file", "keyring"] = "file"
    token_store_path: str = ""


class McpSettings(BaseModel):
    """The ``[mcp]`` table: global token storage plus named servers."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    token_store: Literal["file", "keyring"] = "file"
    token_store_path: str = ""
    servers: dict[str, McpServerSettings] = Field(default_factory=dict)

    @field_validator("servers")
    @classmethod
    def _valid_names(cls, value: dict[str, McpServerSettings]) -> dict[str, McpServerSettings]:
        for name in value:
            if not MCP_NAME_PATTERN.fullmatch(name):
                raise ValueError(
                    f"invalid MCP server name {name!r}: use letters, digits, '_' or '-' "
                    "and start with a letter or digit"
                )
        return value


def default_token_store_path() -> Path:
    """Where OAuth tokens are kept when no path is configured."""
    return user_data_path("hrns") / "mcp-auth"


def server_name(binding_id: str) -> str:
    """Recover the configured name from its binding id (``mcp.<name>``)."""
    return binding_id.removeprefix("mcp.")


def server_target(server: McpServerSettings) -> str:
    """A short human-readable destination for lists and status rows."""
    if server.is_http:
        return server.url
    return " ".join([server.command, *server.args]).strip()


def _expand(value: str) -> str:
    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        try:
            return os.environ[name]
        except KeyError:
            raise ValueError(f"environment variable {name} is referenced but not set") from None

    return _ENV_REFERENCE.sub(replace, value)


def expand_env(value: str) -> str:
    """Expand ``{env:NAME}`` references in one configuration value."""
    return _expand(value)


def expand_env_map(values: dict[str, str]) -> dict[str, str]:
    """Expand ``{env:NAME}`` references in every value of a map."""
    return {key: _expand(value) for key, value in values.items()}


def build_token_store(
    kind: Literal["file", "keyring"] = "file",
    path: str | Path = "",
) -> Any:
    """Create the FastMCP token store, defaulting to private JSON files on disk."""
    if kind == "keyring":
        from key_value.aio.stores.keyring import KeyringStore

        return KeyringStore(service_name="hrns.mcp")

    from key_value.aio.stores.filetree import FileTreeStore

    directory = Path(path).expanduser() if path else default_token_store_path()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        directory.chmod(0o700)
    except OSError:  # pragma: no cover - best effort on exotic filesystems
        pass
    return FileTreeStore(data_directory=directory)


def _oauth(server: McpServerSettings, store: Any, *, interactive: bool) -> Any:
    from fastmcp.client.auth import OAuth

    config = server.oauth_config()
    if config is None:
        return None

    if interactive:
        return OAuth(
            mcp_url=server.url,
            scopes=config.scopes or None,
            client_id=config.client_id or None,
            client_secret=config.client_secret or None,
            callback_port=config.callback_port,
            token_storage=store,
        )

    class _NoBrowserOAuth(OAuth):
        """Runtime variant: never open a browser; ask the user to run ``hrns mcp auth``."""

        async def redirect_handler(self, authorization_url: str) -> None:
            raise RuntimeError("this MCP server needs authentication; run `hrns mcp auth <name>`")

    return _NoBrowserOAuth(
        mcp_url=server.url,
        scopes=config.scopes or None,
        client_id=config.client_id or None,
        client_secret=config.client_secret or None,
        callback_port=config.callback_port,
        token_storage=store,
    )


def build_transport(
    server: McpServerSettings,
    *,
    store: Any = None,
    interactive: bool = False,
) -> Any:
    """Build the FastMCP transport for one server, with headers and auth applied."""
    if server.is_http:
        from fastmcp.client.transports import SSETransport, StreamableHttpTransport
        from fastmcp.mcp_config import infer_transport_type_from_url

        headers = expand_env_map(server.headers) or None
        auth = _oauth(server, store, interactive=interactive)
        if infer_transport_type_from_url(server.url) == "sse":
            return SSETransport(url=server.url, headers=headers, auth=auth)
        return StreamableHttpTransport(url=server.url, headers=headers, auth=auth)

    from fastmcp.client.transports import StdioTransport

    return StdioTransport(
        command=server.command,
        args=list(server.args),
        env=expand_env_map(server.env) or None,
        cwd=server.cwd or None,
    )


def build_client(
    server: McpServerSettings,
    *,
    store: Any = None,
    interactive: bool = False,
) -> Any:
    """Build (but do not connect) a FastMCP client for status checks and OAuth."""
    from fastmcp import Client

    transport = build_transport(server, store=store, interactive=interactive)
    return Client(transport, timeout=server.timeout, init_timeout=server.timeout)


async def has_tokens(server: McpServerSettings, store: Any) -> bool | None:
    """Whether OAuth tokens are stored for this server; ``None`` when OAuth is off."""
    if server.oauth_config() is None:
        return None
    from fastmcp.client.auth.oauth import TokenStorageAdapter

    adapter = TokenStorageAdapter(async_key_value=store, server_url=server.url)
    try:
        return await adapter.get_tokens() is not None
    except Exception:
        return False


async def clear_tokens(server: McpServerSettings, store: Any) -> None:
    """Forget any OAuth tokens and registration cached for this server."""
    from fastmcp.client.auth.oauth import TokenStorageAdapter

    adapter = TokenStorageAdapter(async_key_value=store, server_url=server.url)
    await adapter.clear()
