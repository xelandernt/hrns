"""Client identity, extension method names, and static protocol bounds."""

from __future__ import annotations

import os

import harness_protocol as _protocol
from acp import PROTOCOL_VERSION
from harness_protocol import PING_INTERVAL, PING_TIMEOUT, SUBPROTOCOL

from . import _version

#: Values shown to the agent in ``initialize``.
CLIENT_NAME = "harness-client"
CLIENT_TITLE = "Harness Terminal"
CLIENT_VERSION = _version.__version__

#: Protocol version we understand (stable v1 baseline).
PROTOCOL_VERSION = PROTOCOL_VERSION

#: Project extension wire names. ``ext_method`` adds the ACP ``_`` prefix;
#: these tuples carry the prefix to match ``ExtensionAdvertisement.methods``.
#: One source of truth with hosts and the server: the shared protocol package.
EXTENSION_METHODS = tuple(_protocol.EXTENSION_METHODS)
EXTENSION_VERSION = _protocol.EXTENSION_VERSION

#: 16 MiB frames, 30s ping/pong, versioned subprotocol from the shared contract.
MAX_MESSAGE_BYTES = _protocol.MAX_MESSAGE_BYTES
WS_PING_INTERVAL = float(PING_INTERVAL)
WS_PING_TIMEOUT = float(PING_TIMEOUT)
WS_SUBPROTOCOL = SUBPROTOCOL

#: Retention bounds for the terminal UI and logs.
UI_MAX_TRANSCRIPT_BLOCKS = 400
UI_MAX_TOOL_CONTENT_CHARS = 8000
UI_MAX_LOG_LINES = 500
UI_STREAM_COALESCE_SECONDS = 0.06

#: Bounded outbound buffering hint for the WebSocket transport.
WS_WRITE_LIMIT = 1024 * 1024

CONNECT_TIMEOUT = 20.0
DEFAULT_CATALOG_LIMIT = 50

CONFIG_DIR_NAME = "harness"
CONFIG_FILE_NAME = "profiles.toml"
PROJECT_CONFIG_NAME = "harness.toml"
CONFIG_ENV_OVERRIDE = "HARNESS_PROFILES"


def client_info_dict() -> dict[str, str]:
    """Name/title metadata used in the initialize request."""
    return {
        "name": CLIENT_NAME,
        "title": CLIENT_TITLE,
        "version": CLIENT_VERSION,
    }


def is_ci() -> bool:
    return bool(os.environ.get("CI"))
