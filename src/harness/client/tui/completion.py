"""Bounded workspace file discovery and matching for the composer."""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any


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


def matching_skills(skills: Iterable[Mapping[str, Any]], query: str) -> list[Mapping[str, Any]]:
    """Skills whose name or description matches `query`, best first.

    Names win over descriptions, and a name prefix wins over a loose subsequence match, so
    typing `$re` surfaces `review` before `code-review`.
    """
    needle = query.casefold().strip()

    def rank(skill: Mapping[str, Any]) -> tuple[int, int, str] | None:
        name = str(skill.get("name", "")).casefold()
        description = str(skill.get("description", "")).casefold()
        if not needle:
            return (0, len(name), name)
        if name.startswith(needle):
            return (0, len(name), name)
        characters = iter(name)
        if all(character in characters for character in needle):
            return (1, len(name), name)
        if needle in description:
            return (2, len(name), name)
        return None

    ranked: list[tuple[tuple[int, int, str], Mapping[str, Any]]] = []
    for skill in skills:
        place = rank(skill)
        if place is not None:
            ranked.append((place, skill))
    ranked.sort(key=lambda item: item[0])
    return [skill for _, skill in ranked[:12]]
