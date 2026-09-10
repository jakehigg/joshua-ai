"""The set of taught skills a turn is matched against.

A command skill is matched by phrase, and a phrase match needs no database: it
is a comparison between the turn and a few dozen short strings. The registry
holds the parsed skills in memory so the match stays off the hot path, and the
indexer replaces the whole set on each pass over the ``files`` source.

The whole set is replaced at once, and never edited in place, so a turn that
reads the registry while a pass is running sees the set from before the pass or
the set from after it, and never a half-written one.
"""

from __future__ import annotations

from collections.abc import Iterable

from joshua_shared.log import get_logger

from joshua_core.memory.skills import Skill

logger = get_logger("memory.skill_registry")


class SkillRegistry:
    """Every parsed skill on the volume, replaced whole on each index pass."""

    def __init__(self) -> None:
        self._skills: tuple[Skill, ...] = ()
        self._loaded = False

    def replace(self, skills: Iterable[Skill]) -> None:
        """Swap in a new set. One assignment, so a reader never sees a partial set."""
        new = tuple(skills)
        commands = sum(1 for s in new if s.fires_from_phrase)
        semantic = sum(1 for s in new if s.fires_from_meaning)
        logger.info(
            {
                "message": "skill registry reloaded",
                "total": len(new),
                "phrase": commands,
                "semantic": semantic,
                "conventions": len(new) - commands - semantic,
            }
        )
        self._skills = new
        self._loaded = True

    @property
    def loaded(self) -> bool:
        """False until the first index pass has run. A turn before that fires nothing."""
        return self._loaded

    def all(self) -> tuple[Skill, ...]:
        return self._skills

    def phrase_skills(self) -> tuple[Skill, ...]:
        return tuple(s for s in self._skills if s.fires_from_phrase)

    def semantic_paths(self) -> frozenset[str]:
        """The paths of the skills that asked for the vector match.

        The provider uses this to keep the vector search to the pages that
        opted in, so a phrase page can never fire from meaning alone.
        """
        return frozenset(s.path for s in self._skills if s.fires_from_meaning)

    def by_path(self, path: str) -> Skill | None:
        for skill in self._skills:
            if skill.path == path:
                return skill
        return None
