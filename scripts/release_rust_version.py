"""Translate a release tag for Python or stamp the Cargo release files."""

from __future__ import annotations

import re
import sys
from pathlib import Path

RUST = Path(__file__).resolve().parent.parent / "rust"
CRATE_VERSION = re.compile(r'(?m)^(name = "hrns-tui"\nversion = )"([^"]+)"$')

_NUMBER = r"(?:0|[1-9][0-9]*)"
_TAG = re.compile(
    rf"v?(?P<version>{_NUMBER}\.{_NUMBER}\.{_NUMBER})"
    rf"(?:-(?P<stage>alpha|beta|rc)\.(?P<serial>{_NUMBER}))?"
)
_PYTHON_STAGES = {"alpha": "a", "beta": "b", "rc": "rc"}


def versions(tag: str) -> tuple[str, str]:
    match = _TAG.fullmatch(tag)
    if match is None:
        raise SystemExit(f"expected X.Y.Z or X.Y.Z-(alpha|beta|rc).N, got {tag!r}")
    version = match["version"]
    stage = match["stage"]
    if stage is None:
        return version, version
    serial = match["serial"]
    return f"{version}-{stage}.{serial}", f"{version}{_PYTHON_STAGES[stage]}{serial}"


def stamp(version: str) -> None:
    files = {path: path.read_text() for path in (RUST / "Cargo.toml", RUST / "Cargo.lock")}
    matches = {path: CRATE_VERSION.findall(text) for path, text in files.items()}
    if any(len(items) != 1 for items in matches.values()):
        raise SystemExit("expected one hrns-tui version in both Cargo files")
    if len({items[0][1] for items in matches.values()}) != 1:
        raise SystemExit("Cargo.toml and Cargo.lock disagree on the hrns-tui version")

    for path, text in files.items():
        path.write_text(CRATE_VERSION.sub(lambda match: f'{match[1]}"{version}"', text))


if __name__ == "__main__":
    match sys.argv[1:]:
        case ["--python-version", tag]:
            print(versions(tag)[1])
        case [tag]:
            stamp(versions(tag)[0])
        case _:
            raise SystemExit("usage: release_rust_version.py [--python-version] RELEASE_TAG")
