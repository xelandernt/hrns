"""Client identity, extension method names, and static protocol bounds."""

from __future__ import annotations

from acp import PROTOCOL_VERSION
from websockets.typing import Subprotocol

import hrns.protocol as _protocol
from hrns.protocol import PING_INTERVAL, PING_TIMEOUT, SUBPROTOCOL

from . import _version

#: Values shown to the agent in ``initialize``.
CLIENT_NAME = "hrns-client"
CLIENT_TITLE = "hrns terminal"
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
WS_SUBPROTOCOL: Subprotocol = Subprotocol(SUBPROTOCOL)

#: Retention bounds for the terminal UI.
UI_MAX_TRANSCRIPT_BLOCKS = 400
UI_MAX_TOOL_CONTENT_CHARS = 8000
UI_STREAM_COALESCE_SECONDS = 0.06

#: Bounded outbound buffering hint for the WebSocket transport.
WS_WRITE_LIMIT = 1024 * 1024

CONNECT_TIMEOUT = 20.0
DEFAULT_CATALOG_LIMIT = 50

CONFIG_DIR_NAME = "hrns"
CONFIG_FILE_NAME = "profiles.toml"
PROJECT_CONFIG_NAME = "hrns.toml"
CONFIG_ENV_OVERRIDE = "HRNS_PROFILES"
