"""Taught skills — parsing for ``wiki/skills/<slug>.md``.

A taught skill is a Markdown file a member writes to the shared wiki. The
frontmatter holds a display ``name`` and a list of trigger phrases; the body is
the instructions the agent follows when a phrase fires. The indexer turns each
trigger into one ``kb_chunk`` row with ``kind = 'skill'`` (see
``memory/indexer.py``), and the taught-skill provider matches a turn against
those rows (see ``engine/taught_skills.py``).

This module is dependency-light (stdlib only) so the offline parser tests import
it without psycopg.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from joshua_shared.log import get_logger

logger = get_logger("memory.skills")

# The kind stored on every trigger row. It is filtered out of ordinary recall.
SKILL_KIND = "skill"

# A skill file is ``wiki/skills/<slug>.md`` at that exact depth. A file deeper in
# the tree (``wiki/skills/sub/x.md``) is an ordinary wiki page — the slug pattern
# forbids a slash, so a deeper path never matches.
_SKILL_PREFIX = "wiki/skills/"
_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")

# One trigger phrase is capped at this many characters; a file keeps at most this
# many phrases. A phrase past the cap is dropped with one warning.
MAX_TRIGGER_CHARS = 200
MAX_TRIGGERS = 20


@dataclass(frozen=True)
class Skill:
    """One parsed taught skill.

    ``name`` is the display name (empty when the frontmatter omits it; the
    indexer then falls back to the slug). ``triggers`` is the cleaned tuple of
    phrases — empty means the skill is off. ``instructions`` is the body.
    """

    name: str
    triggers: tuple[str, ...]
    instructions: str


def is_skill_path(uri: str) -> bool:
    """True for a taught-skill file ``wiki/skills/<slug>.md`` at that exact depth."""
    if not uri.startswith(_SKILL_PREFIX):
        return False
    rest = uri[len(_SKILL_PREFIX) :]
    if not rest.endswith(".md"):
        return False
    return bool(_SLUG.match(rest[:-3]))


def skill_slug(uri: str) -> str:
    """The ``<slug>`` of a skill path, or ``""`` when the path is not a skill."""
    if not is_skill_path(uri):
        return ""
    return uri[len(_SKILL_PREFIX) : -len(".md")]


def _parse_trigger_list(raw: str) -> list[str] | None:
    """Read the frontmatter ``triggers`` value as a list of phrases, or None.

    The value is a raw string such as ``["movie time", "let's watch a movie"]``.
    A value that is not a bracketed list does not parse as a list and returns
    None. An empty list ``[]`` returns an empty list — a valid, switched-off
    skill.
    """
    raw = raw.strip()
    if not (raw.startswith("[") and raw.endswith("]")):
        return None
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        parsed = None
    if isinstance(parsed, list):
        return [str(x) for x in parsed]
    inner = raw[1:-1].strip()
    if not inner:
        return []
    return [part.strip().strip("'\"") for part in inner.split(",")]


def clean_triggers(triggers: Iterable[str], *, path: str = "") -> tuple[str, ...]:
    """Strip, lower-case, drop empties and duplicates, cap length and count.

    Logs one warning that names ``path`` when the count cap drops a phrase.
    """
    out: list[str] = []
    seen: set[str] = set()
    for raw in triggers:
        phrase = raw.strip().lower()
        if not phrase:
            continue
        phrase = phrase[:MAX_TRIGGER_CHARS]
        if phrase in seen:
            continue
        seen.add(phrase)
        out.append(phrase)
    if len(out) > MAX_TRIGGERS:
        logger.warning(
            {
                "message": "taught skill trigger cap; dropping phrases",
                "path": path,
                "kept": MAX_TRIGGERS,
                "dropped": len(out) - MAX_TRIGGERS,
            }
        )
        out = out[:MAX_TRIGGERS]
    return tuple(out)


def parse_skill(frontmatter: Mapping[str, str], body: str, *, path: str = "") -> Skill | None:
    """Parse a taught skill, or None when the file is not a usable skill.

    Returns None when the frontmatter is missing, when ``triggers`` does not
    parse as a list, or when the body is empty. ``path`` names the file in the
    trigger-cap warning.
    """
    if not frontmatter:
        return None
    instructions = (body or "").strip()
    if not instructions:
        return None
    if "triggers" not in frontmatter:
        return None
    parsed = _parse_trigger_list(frontmatter["triggers"])
    if parsed is None:
        return None
    triggers = clean_triggers(parsed, path=path)
    name = (frontmatter.get("name") or "").strip()
    return Skill(name=name, triggers=triggers, instructions=instructions)
