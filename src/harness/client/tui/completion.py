"""Bounded workspace file discovery and matching for the composer."""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping
from heapq import nsmallest
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


def _is_subsequence(needle: str, haystack: str) -> bool:
    """Whether ``needle``'s characters appear in ``haystack`` in order.

    ``str.find`` from the previous hit keeps this in C and avoids the regex
    backtracking blow-up a ``.*``-joined pattern hits on long needles.
    """
    position = 0
    for character in needle:
        position = haystack.find(character, position)
        if position < 0:
            return False
        position += 1
    return True


class FileIndex:
    """Case-folded workspace paths, prepared once per index refresh."""

    def __init__(self, paths: Iterable[str]) -> None:
        self._entries: tuple[tuple[str, str, int, str], ...] = tuple(
            (path.rsplit("/", 1)[-1].casefold(), path.casefold(), len(path), path) for path in paths
        )

    def __len__(self) -> int:
        return len(self._entries)

    def matching(self, query: str, *, limit: int = 12) -> list[str]:
        needle = query.casefold()
        if needle:
            candidates = [entry for entry in self._entries if _is_subsequence(needle, entry[1])]
        else:
            candidates = list(self._entries)

        def rank(entry: tuple[str, str, int, str]) -> tuple[bool, bool, int, str]:
            return (not entry[0].startswith(needle), needle not in entry[1], entry[2], entry[3])

        return [entry[3] for entry in nsmallest(limit, candidates, key=rank)]


def index_workspace(root: Path) -> tuple[list[str], FileIndex]:
    """Discover the workspace and prepare its match index off the UI thread."""
    paths = workspace_files(root)
    return paths, FileIndex(paths)


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
