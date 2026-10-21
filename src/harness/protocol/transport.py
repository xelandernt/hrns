"""The project WebSocket transport is distinct from ACP's standard stdio transport."""

import base64
import hashlib
import json
import math
from typing import Any, Sequence, TypeVar

from .errors import HarnessError

EXTENSION_VERSION = 1
SUBPROTOCOL = "harness.acp.v1"
MAX_MESSAGE_BYTES = 16 * 1024 * 1024
PING_INTERVAL = 30
PING_TIMEOUT = 30
EXTENSION_METHODS = (
    "_harness/capabilities/list",
    "_harness/plugins/list",
    "_harness/configuration/get",
    "_harness/configuration/update",
    "_harness/configuration/changed",
)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON field: {key}")
        result[key] = value
    return result


def _invalid_constant(value: str) -> None:
    raise ValueError(f"Invalid JSON constant: {value}")


def _finite_float(value: str) -> float:
    """Parse a JSON number, rejecting overflow to a non-finite float.

    ``json.loads`` maps literals such as ``1e999`` to ``inf`` without invoking
    ``parse_constant``; that would let a non-representable number onto the
    version-one wire contract, which requires finite JSON numbers.
    """

    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"Invalid non-finite JSON number: {value}")
    return number


def decode_frame(raw: str | bytes) -> dict[str, Any]:
    try:
        encoded = raw.encode("utf-8") if isinstance(raw, str) else raw
    except UnicodeEncodeError as exc:
        raise HarnessError("INVALID_FRAME", "Expected UTF-8 text") from exc
    if len(encoded) > MAX_MESSAGE_BYTES:
        raise HarnessError("MESSAGE_TOO_LARGE", "ACP messages may not exceed 16 MiB")
    try:
        value = json.loads(
            raw,
            object_pairs_hook=_unique_object,
            parse_constant=_invalid_constant,
            parse_float=_finite_float,
        )
    except (ValueError, UnicodeDecodeError, RecursionError) as exc:
        raise HarnessError("INVALID_FRAME", "Expected a UTF-8 JSON-RPC object") from exc
    if not isinstance(value, dict) or value.get("jsonrpc") != "2.0":
        raise HarnessError(
            "INVALID_FRAME", "Expected one JSON-RPC 2.0 object; batching is unsupported"
        )
    if "id" in value and value["id"] is not None and type(value["id"]) not in (int, str):
        raise HarnessError("INVALID_FRAME", "Expected a string or integer JSON-RPC id")
    if "method" in value:
        if not isinstance(value["method"], str) or not value["method"]:
            raise HarnessError("INVALID_FRAME", "Expected a JSON-RPC method name")
        if "result" in value or "error" in value:
            raise HarnessError("INVALID_FRAME", "Expected a request or a response, not both")
        if "params" in value and not isinstance(value["params"], (dict, list)):
            raise HarnessError("INVALID_FRAME", "Expected structured JSON-RPC params")
    elif "id" not in value or ("result" in value) == ("error" in value):
        raise HarnessError(
            "INVALID_FRAME", "Expected a JSON-RPC method or exactly one response result"
        )
    elif "error" in value:
        error = value["error"]
        if (
            not isinstance(error, dict)
            or type(error.get("code")) is not int
            or not isinstance(error.get("message"), str)
        ):
            raise HarnessError("INVALID_FRAME", "Expected a JSON-RPC error code and message")
    return value


def encode_frame(value: dict[str, Any]) -> str:
    raw = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    decode_frame(raw)
    return raw


def canonical_hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
        ).encode()
    ).hexdigest()


T = TypeVar("T")


def paginate(
    items: Sequence[T], cursor: str | None = None, limit: int = 50
) -> tuple[list[T], str | None]:
    if not 1 <= limit <= 100:
        raise HarnessError("INVALID_CONFIGURATION", "limit must be between 1 and 100")
    start = 0
    if cursor:
        try:
            decoded = json.loads(base64.urlsafe_b64decode(cursor.encode()))
            if (
                decoded.get("v") != 1
                or type(decoded.get("offset")) is not int
                or decoded["offset"] < 0
            ):
                raise ValueError("Invalid cursor")
            start = decoded["offset"]
        except (ValueError, TypeError, AttributeError) as exc:
            raise HarnessError("INVALID_CONFIGURATION", "Invalid pagination cursor") from exc
    end = start + limit
    next_cursor = None
    if end < len(items):
        next_cursor = base64.urlsafe_b64encode(
            json.dumps({"v": 1, "offset": end}).encode()
        ).decode()
    return list(items[start:end]), next_cursor
