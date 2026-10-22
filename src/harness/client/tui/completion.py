"""Bounded workspace file discovery and matching for the composer."""

from __future__ import annotations

import os
from pathlib import Path


def workspace_files(root: Path) -> list[str]:
    """Discover up to 10,000 files for composer suggestions, without ignore rules."""
    paths: list[str] = []
    for directory, folders, files in os.walk(root):
        folders.sort()
        for name in sorted(files):
            paths.append(str((Path(directory) / name).relative_to(root)))
            if len(paths) >= 10_000:
                return sorted(paths)
    return sorted(paths)


def matching_files(paths: list[str], query: str) -> list[str]:
    needle = query.casefold()

    def matches(path: str) -> bool:
        characters = iter(path.casefold())
        return all(character in characters for character in needle)

    candidates = [path for path in paths if matches(path)]
    candidates.sort(
        key=lambda path: (
            not Path(path).name.casefold().startswith(needle),
            needle not in path.casefold(),
            len(path),
            path,
        )
    )
    return candidates[:12]
