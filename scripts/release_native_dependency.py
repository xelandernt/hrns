"""Add the version-matched native client to the published slim client extra.

The development workspace builds the Rust crate through its local uv source.
After validating that workspace, the release patches the slim metadata and
builds the distributions with a pin to the published native wheels.
"""

from __future__ import annotations

import argparse
import email.parser
import email.policy
import pathlib
import sys
import tarfile
import tomllib
import zipfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
SLIM = ROOT / "hrns_slim" / "pyproject.toml"
NATIVE_REQUIREMENT = "hrns-tui=={{ version }}"
_HOOK_HEADER = "[tool.hatch.metadata.hooks.uv-dynamic-versioning.optional-dependencies]"
_CLIENT_HEADER = "client = [\n"
_NATIVE_LINE = f'    "{NATIVE_REQUIREMENT}",\n'


def _write_source(native: bool) -> int:
    text = SLIM.read_text()
    before, header, extras = text.partition(f"{_HOOK_HEADER}\n")
    client_prefix, client, dependencies = extras.partition(_CLIENT_HEADER)
    if not header or not client:
        raise SystemExit("the slim metadata hook has no client extra")
    dependencies = dependencies.removeprefix(_NATIVE_LINE)
    if native:
        dependencies = _NATIVE_LINE + dependencies
    updated = before + header + client_prefix + client + dependencies
    dependency_array = tomllib.loads(updated)["tool"]["hatch"]["metadata"]["hooks"][
        "uv-dynamic-versioning"
    ]["optional-dependencies"]["client"]
    if (NATIVE_REQUIREMENT in dependency_array) is not native:
        raise SystemExit("the client extra did not reach the requested state")
    if updated != text:
        SLIM.write_text(updated)
    operation = "patched" if native else "restored"
    print(f"{operation} {SLIM.relative_to(ROOT)}")
    return 0


def _headers(text: str) -> email.message.Message:
    return email.parser.Parser(policy=email.policy.compat32).parsestr(text)


def _wheel_metadata(path: pathlib.Path) -> str:
    with zipfile.ZipFile(path) as archive:
        name = next(
            (item for item in archive.namelist() if item.endswith(".dist-info/METADATA")),
            None,
        )
        if name is None:
            raise SystemExit(f"{path.name} has no METADATA")
        return archive.read(name).decode("utf-8")


def _sdist_metadata(path: pathlib.Path) -> str:
    with tarfile.open(path) as archive:
        member = next(
            (item for item in archive.getmembers() if item.name.endswith("PKG-INFO")),
            None,
        )
        if member is None:
            raise SystemExit(f"{path.name} has no PKG-INFO")
        extracted = archive.extractfile(member)
        if extracted is None:
            raise SystemExit(f"cannot read PKG-INFO from {path.name}")
        return extracted.read().decode("utf-8")


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise SystemExit(f"metadata check failed: {message}")


def _check_metadata(label: str, text: str) -> str:
    message = _headers(text)
    name = message["Name"]
    version = message["Version"]
    _require(version is not None, f"{label} has no Version")
    requirements = [item.replace('"', "'") for item in message.get_all("Requires-Dist", [])]
    native = [item for item in requirements if item.startswith("hrns-tui")]
    if name == "hrns-slim":
        client = [item for item in requirements if "extra == 'client'" in item]
        _require(
            client == native == [f"hrns-tui=={version}; extra == 'client'"],
            f"{label} must assign only the matching native wheel to the client extra: {client}",
        )
        _require(
            "headless" in message.get_all("Provides-Extra", []),
            f"{label} has no headless extra",
        )
    elif name == "hrns":
        _require(not native, f"{label} must select hrns-tui through the client extra")
        _require(
            f"hrns-slim[client,headless,server]=={version}" in requirements,
            f"{label} lost its version-matched hrns-slim requirement",
        )
    else:
        raise SystemExit(f"unexpected distribution in {label}: {name}")
    return name


def verify(directory: pathlib.Path) -> int:
    """Check both artifact types for both Python distributions."""
    for pattern, read in (("*.whl", _wheel_metadata), ("*.tar.gz", _sdist_metadata)):
        names = set()
        for path in sorted(directory.glob(pattern)):
            names.add(_check_metadata(path.name, read(path)))
            print(f"{path.name}: verified")
        _require(names == {"hrns", "hrns-slim"}, f"expected both {pattern} packages in {directory}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--revert",
        action="store_true",
        help="restore the development client extra",
    )
    group.add_argument(
        "--verify",
        metavar="DIRECTORY",
        type=pathlib.Path,
        help="check built distributions instead of editing hrns_slim/pyproject.toml",
    )
    arguments = parser.parse_args(argv)
    if arguments.verify is not None:
        return verify(arguments.verify)
    return _write_source(native=not arguments.revert)


if __name__ == "__main__":
    sys.exit(main())
