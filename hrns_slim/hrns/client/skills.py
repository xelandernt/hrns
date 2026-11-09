"""Skill references a client resolves from a draft.

The agent owns the skills; this module only turns the ``$name`` tokens a person
typed into the structured reference the protocol carries. The body never travels
here: the server expands the id into the skill's instructions, so a large
``SKILL.md`` cannot be sent twice or drift from what the model would load itself.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

__all__ = ["SKILL_META_KEY", "SkillMention", "find_mentions", "from_session_meta"]

#: The per-block prompt ``_meta`` key, matching the adapter's reader.
SKILL_META_KEY = "hrns/skills"

#: A ``$skill`` token at the start of a word, the same rule the composer highlights.
_MENTION_PATTERN = re.compile(r"(?<!\S)\$([A-Za-z0-9][\w-]*)")


@dataclass(frozen=True, kw_only=True)
class SkillMention:
    """One ``$skill`` reference, with the span the composer rendered it at."""

    id: str
    start: int
    end: int
    text: str

    def to_meta(self) -> dict[str, object]:
        """The object the prompt block's ``_meta`` carries for this reference."""
        return {
            "id": self.id,
            "mention": {"start": self.start, "end": self.end, "text": self.text},
        }


def find_mentions(text: str, skills: Sequence[Mapping[str, object]]) -> tuple[SkillMention, ...]:
    """References the draft actually names, deduplicated by skill id.

    A name the catalog does not list stays literal text, which is what someone typing
    a shell variable meant. ``userInvocable: false`` keeps a skill out of the wire
    reference the same way it keeps it out of completion.
    """
    invocable = {
        str(skill["id"])
        for skill in skills
        if skill.get("id") and bool(skill.get("userInvocable", True))
    }
    mentions: list[SkillMention] = []
    seen: set[str] = set()
    for match in _MENTION_PATTERN.finditer(text):
        skill_id = match.group(1)
        if skill_id not in invocable or skill_id in seen:
            continue
        mentions.append(
            SkillMention(
                id=skill_id,
                start=match.start(),
                end=match.end(),
                text=match.group(0),
            )
        )
        seen.add(skill_id)
    return tuple(mentions)


def from_session_meta(meta: Mapping[str, object] | None) -> tuple[dict[str, object], ...]:
    """The skill catalog a ``session/new`` or ``session/load`` response carried."""
    if not meta:
        return ()
    entries = meta.get(SKILL_META_KEY)
    if not isinstance(entries, list):
        return ()
    return tuple(entry for entry in entries if isinstance(entry, dict))
