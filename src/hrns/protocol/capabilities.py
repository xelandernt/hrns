"""Paths into ACP's ``clientCapabilities`` object.

A client capability is a boolean somewhere in the ``initialize`` payload: ``terminal`` at the
top level, ``fs.read_text_file`` one level down, and so on. Naming them as dotted paths keeps
requiring one a data change instead of a branch in the runtime, and lets a plugin state what it
needs in the vocabulary the protocol already uses.
"""

from __future__ import annotations

__all__ = [
    "FS_READ_TEXT_FILE",
    "FS_WRITE_TEXT_FILE",
    "TERMINAL",
    "acp_capability_spelling",
]

#: ``clientCapabilities.fs.readTextFile``: the agent may ask for file contents.
FS_READ_TEXT_FILE = "fs.read_text_file"
#: ``clientCapabilities.fs.writeTextFile``: the agent may ask the client to write a file.
FS_WRITE_TEXT_FILE = "fs.write_text_file"
#: ``clientCapabilities.terminal``: the agent may drive the client's ``terminal/*`` methods.
TERMINAL = "terminal"


def acp_capability_spelling(path: str) -> str:
    """The path as ACP spells it on the wire (``fs.read_text_file`` → ``fs.readTextFile``)."""

    def camel(segment: str) -> str:
        head, *rest = segment.split("_")
        return head + "".join(part.title() for part in rest)

    return ".".join(camel(segment) for segment in path.split("."))
