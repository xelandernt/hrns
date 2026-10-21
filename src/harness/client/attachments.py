"""Explicit, bounded file attachments for ACP prompts.

Attachments are content, not filesystem grants. The caller chooses each path; the
receiving agent never gains access to the client's working directory from an attachment,
and a hosted agent receives bytes rather than a local path it cannot read.

Text resources are embedded as UTF-8 text; everything else is embedded as base64 bytes.
Only regular files are accepted. The aggregate is bounded by the ACP message limit, with
headroom reserved for the rest of the JSON-RPC envelope.
"""

from __future__ import annotations

import base64
import mimetypes
import stat
from pathlib import Path
from typing import Any, Sequence

from acp import schema

from .errors import HarnessClientError

__all__ = [
    "MAX_ATTACHMENT_BYTES",
    "attachment_summary",
    "build_prompt_blocks",
    "validate_attachment",
]

#: Per-file limit; the client's prompt-size check still applies to the aggregate.
MAX_ATTACHMENT_BYTES = 8 * 1024 * 1024
_TEXTUAL_SUFFIXES = {
    ".json",
    ".jsonl",
    ".yaml",
    ".yml",
    ".toml",
    ".md",
    ".txt",
    ".csv",
    ".tsv",
    ".py",
    ".ts",
    ".tsx",
    ".js",
    ".jsx",
    ".css",
    ".html",
    ".xml",
    ".sql",
    ".sh",
    ".rs",
    ".go",
    ".java",
    ".kt",
    ".c",
    ".h",
    ".cpp",
    ".hpp",
    ".rb",
    ".php",
}


def _media_type(path: Path) -> str:
    guessed, _ = mimetypes.guess_type(path.name)
    if guessed:
        return guessed
    if path.suffix.lower() in _TEXTUAL_SUFFIXES:
        return "text/plain"
    return "application/octet-stream"


def _is_text(media_type: str, path: Path) -> bool:
    return (
        media_type.startswith("text/")
        or media_type in {"application/json", "application/xml", "application/yaml"}
        or path.suffix.lower() in _TEXTUAL_SUFFIXES
    )


def validate_attachment(raw: str | Path) -> Path:
    """Resolve and validate one explicit attachment without reading its content."""
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = path.resolve()
    try:
        info = path.stat()
    except OSError as exc:
        raise HarnessClientError(f"Cannot attach {path}: {exc}") from exc
    if not stat.S_ISREG(info.st_mode):
        raise HarnessClientError(f"Cannot attach {path}: only regular files are supported")
    if info.st_size > MAX_ATTACHMENT_BYTES:
        raise HarnessClientError(
            f"Cannot attach {path}: {info.st_size:,} bytes exceeds the "
            f"{MAX_ATTACHMENT_BYTES:,}-byte per-file limit"
        )
    return path


def _read_regular(path: Path) -> bytes:
    validate_attachment(path)
    try:
        return path.read_bytes()
    except OSError as exc:
        raise HarnessClientError(f"Cannot read {path}: {exc}") from exc


def build_prompt_blocks(prompt: str, attachments: Sequence[str] = ()) -> list[Any]:
    """Build ACP prompt blocks: the explicit user text followed by embedded resources."""
    if not prompt.strip():
        raise HarnessClientError("A prompt needs non-empty text; attachments do not replace it.")
    blocks: list[Any] = [schema.TextContentBlock(type="text", text=prompt)]
    total = len(prompt.encode("utf-8"))
    for raw in attachments:
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = path.resolve()
        content = _read_regular(path)
        total += len(content)
        # Base64 expands by 4/3; leave room for JSON-RPC fields and other blocks.
        if total * 4 // 3 > 12 * 1024 * 1024:
            raise HarnessClientError(
                "Attachments are too large for one ACP prompt (16 MiB frame limit); "
                "attach fewer or smaller files."
            )
        media_type = _media_type(path)
        uri = path.as_uri()
        if _is_text(media_type, path):
            try:
                text = content.decode("utf-8")
            except UnicodeDecodeError:
                text = None
            if text is not None:
                resource: Any = schema.TextResourceContents(
                    uri=uri, text=text, mime_type=media_type
                )
            else:
                resource = schema.BlobResourceContents(
                    uri=uri,
                    blob=base64.b64encode(content).decode("ascii"),
                    mime_type="application/octet-stream",
                )
        else:
            resource = schema.BlobResourceContents(
                uri=uri,
                blob=base64.b64encode(content).decode("ascii"),
                mime_type=media_type,
            )
        blocks.append(schema.EmbeddedResourceContentBlock(type="resource", resource=resource))
    return blocks


def attachment_summary(attachments: Sequence[str]) -> list[dict[str, Any]]:
    """Non-secret display metadata for the UI and JSON output."""
    result: list[dict[str, Any]] = []
    for raw in attachments:
        path = Path(raw).expanduser().resolve()
        try:
            size = path.stat().st_size
        except OSError:
            size = None
        result.append(
            {"name": path.name, "path": str(path), "size": size, "mimeType": _media_type(path)}
        )
    return result
