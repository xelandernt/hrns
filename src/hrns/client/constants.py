"""Client identity and static bounds."""

from __future__ import annotations

from acp import PROTOCOL_VERSION

from . import _version

#: Values shown to the agent in ``initialize``.
CLIENT_NAME = "hrns-client"
CLIENT_TITLE = "hrns terminal"
CLIENT_VERSION = _version.__version__

#: Stable ACP protocol version.
PROTOCOL_VERSION = PROTOCOL_VERSION

#: Retention bounds for the terminal UI.
UI_MAX_TRANSCRIPT_BLOCKS = 400
UI_MAX_TOOL_CONTENT_CHARS = 8000
UI_STREAM_COALESCE_SECONDS = 0.06

CONNECT_TIMEOUT = 20.0
MAX_MESSAGE_BYTES = 16 * 1024 * 1024

#: WebSocket transport bounds.
WS_PING_INTERVAL = 30.0
WS_PING_TIMEOUT = 30.0
WS_WRITE_LIMIT = 1024 * 1024

CONFIG_DIR_NAME = "hrns"
CONFIG_FILE_NAME = "profiles.toml"
PROJECT_CONFIG_NAME = "hrns.toml"
CONFIG_ENV_OVERRIDE = "HRNS_PROFILES"

#: Key in the profile file's ``[tui]`` table naming the connection to reopen.
LAST_CONNECTION = "last_connection"

#: Identity of the built-in local runtime; every other identity is a profile name.
LOCAL_CONNECTION = "local"
