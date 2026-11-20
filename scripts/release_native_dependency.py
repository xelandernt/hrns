"""Give the published ``hrns`` wrapper a version-matched native client requirement.

The native client ships as ``hrns-tui``, a Maturin binary wheel that exists only for
the four platforms CI builds. The source tree cannot depend on it: ``uv lock`` has no
index entry for it, and its version comes from the Rust crate rather than from
``uv-dynamic-versioning``. The publish workflow therefore patches the wrapper metadata
after ``just check`` has validated the pristine tree, rebuilds ``dist/``, and checks the
result with ``--verify``.

``hrns-slim`` is deliberately left alone so ``hrns-slim[server]`` and
``hrns-slim[client]`` never install a binary wheel.
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
WRAPPER = ROOT / "pyproject.toml"

#: Platforms CI builds, as ``(sys_platform, platform_machine)`` marker pairs. Windows
#: reports ``AMD64`` from ``platform.machine()``; some resolvers simulate ``x86_64``.
PLATFORMS = (
    ("linux", ("x86_64",)),
    ("darwin", ("arm64",)),
    ("darwin", ("x86_64",)),
    ("win32", ("AMD64", "x86_64")),
)


def _clause(sys_platform: str, machines: tuple[str, ...]) -> str:
    alternatives = " or ".join(f'platform_machine == "{machine}"' for machine in machines)
    if len(machines) > 1:
        alternatives = f"({alternatives})"
    return f'(sys_platform == "{sys_platform}" and {alternatives})'


MARKERS = " or ".join(_clause(*platform) for platform in PLATFORMS)
#: ``{{ version }}`` is expanded by the hatch metadata hook to the wheel's own version.
NATIVE_REQUIREMENT = f"hrns-tui=={{{{ version }}}}; {MARKERS}"

_HOOK_HEADER = "[tool.hatch.metadata.hooks.uv-dynamic-versioning]"


def _dependency_lines(text: str) -> tuple[int, int]:
    """The line span of the hook's ``dependencies`` array, inline or multi-line."""
    lines = text.splitlines(keepends=True)
    header = next(
        (index for index, line in enumerate(lines) if line.strip() == _HOOK_HEADER),
        None,
    )
    if header is None:
        raise SystemExit(f"pyproject.toml has no {_HOOK_HEADER} section")
    start = header + 1
    while start < len(lines) and not lines[start].strip():
        start += 1
    if start == len(lines) or not lines[start].lstrip().startswith("dependencies"):
        raise SystemExit("the metadata hook has no dependencies array")
    depth = 0
    for index in range(start, len(lines)):
        # Requirement strings can contain brackets, but they stay balanced.
        depth += lines[index].count("[") - lines[index].count("]")
        if depth == 0:
            return start, index + 1
    raise SystemExit("the dependencies array is not closed")


def _source_block(native: bool) -> str:
    """The ``dependencies`` statement, without the section header."""
    if not native:
        return 'dependencies = ["hrns-slim[client,server]=={{ version }}"]\n'
    return (
        "dependencies = [\n"
        '    "hrns-slim[client,server]=={{ version }}",\n'
        f"    '{NATIVE_REQUIREMENT}',\n"
        "]\n"
    )


def _write_source(native: bool) -> int:
    text = WRAPPER.read_text()
    lines = text.splitlines(keepends=True)
    start, end = _dependency_lines(text)
    block = _source_block(native).splitlines(keepends=True)
    updated = "".join([*lines[:start], *block, *lines[end:]])
    state = "carries" if native else "omits"
    if updated == text:
        print(f"pyproject.toml already {state} the native dependency")
    else:
        WRAPPER.write_text(updated)
        print(f"{'patched' if native else 'reverted'} {WRAPPER.relative_to(ROOT)}")
    dependency_array = tomllib.loads(WRAPPER.read_text())["tool"]["hatch"]["metadata"]["hooks"][
        "uv-dynamic-versioning"
    ]["dependencies"]
    if (NATIVE_REQUIREMENT in dependency_array) is not native:
        raise SystemExit("the metadata hook did not reach the requested state")
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


def _check_metadata(label: str, text: str, *, native: bool) -> None:
    """Assert the wrapper declares ``hrns-tui`` and the slim package does not."""
    message = _headers(text)
    requirements: list[str] = message.get_all("Requires-Dist") or []
    pinned = [item for item in requirements if item.startswith("hrns-tui==")]
    if not native:
        _require(not pinned, f"{label} must not require hrns-tui")
        return
    _require(bool(pinned), f"{label} is missing the hrns-tui requirement")
    version = message["Version"]
    _require(version is not None, f"{label} has no Version")
    requirement = pinned[0]
    _require(
        requirement.startswith(f"hrns-tui=={version}"),
        f"{label} does not pin hrns-tui to its own version: {requirement}",
    )
    # Hatchling re-serializes the marker with single quotes.
    normalized = requirement.replace('"', "'")
    for sys_platform, machines in PLATFORMS:
        _require(
            f"sys_platform == '{sys_platform}'" in normalized
            and all(f"platform_machine == '{machine}'" in normalized for machine in machines),
            f"{label} does not allow {sys_platform}/{machines}: {requirement}",
        )
    _require(
        any(item.startswith("hrns-slim[client,server]==") for item in requirements),
        f"{label} lost its hrns-slim requirement",
    )


def verify(directory: pathlib.Path) -> int:
    """Check the built distributions in ``directory``."""
    wheels = sorted(directory.glob("*.whl"))
    sdists = sorted(directory.glob("*.tar.gz"))
    _require(bool(wheels), f"no wheels in {directory}")
    _require(bool(sdists), f"no source distributions in {directory}")
    for path in wheels + sdists:
        text = _wheel_metadata(path) if path.suffix == ".whl" else _sdist_metadata(path)
        native = path.name.startswith("hrns-")
        _check_metadata(path.name, text, native=native)
        print(f"{path.name}: {'hrns-tui pinned' if native else 'no native dependency'}")
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
        help="restore the development dependency array",
    )
    group.add_argument(
        "--verify",
        metavar="DIRECTORY",
        type=pathlib.Path,
        help="check built distributions instead of editing pyproject.toml",
    )
    arguments = parser.parse_args(argv)
    if arguments.verify is not None:
        return verify(arguments.verify)
    return _write_source(native=not arguments.revert)


if __name__ == "__main__":
    sys.exit(main())
