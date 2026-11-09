"""The spec of an assignment, and how ``start_assignment`` resolves it.

A spec is the whole brief: the agent of the assignment sees nothing else of
the chat. ``resolve`` fills each value the spec leaves out from the
``assignments`` section, clamps each ceiling to its maximum and says so in a
note, and refuses a model or a repository that the section does not allow.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any

from joshua_shared.config import MCP_NAME_PATTERN, Assignments, parse_duration
from pydantic import BaseModel, ConfigDict, Field, field_validator

from joshua_assignments.repos import RepoError, normalize_repo, repo_allowed

MAX_GOAL_CHARS = 20000
MAX_REPOS = 20
MAX_TURNS = 10000
# The destination ``report_to`` names when the report goes to the person's
# own destination, ``assignments.people.<id>.notify``.
REPORT_TO_CONVERSATION = "conversation"
SLUG_CHARS = 40
MAX_TITLE = 120
# A title that ``title_of`` makes from the goal is at most this long.
TITLE_CHARS = 80
_MCP_NAME_RE = re.compile(MCP_NAME_PATTERN)
# The name of a saved assignment: lower case letters, digits, and hyphens,
# 2 to 60 characters, unique for a person.
SAVED_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,59}\Z")


class SpecRefused(Exception):
    """A spec that ``resolve`` refuses. ``reason`` is a fixed word for the caller."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message


class _Spec(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Budget(_Spec):
    """The ceilings of one assignment.

    ``five_hour_pct`` and ``seven_day_pct`` stop the assignment when the
    subscription window reaches that percent used. ``tokens`` caps the tokens
    of this assignment.
    """

    five_hour_pct: int | None = Field(default=None, ge=1, le=100)
    seven_day_pct: int | None = Field(default=None, ge=1, le=100)
    tokens: int | None = Field(default=None, gt=0)


class AssignmentSpec(_Spec):
    """What a caller gives ``start_assignment``."""

    goal: str = Field(min_length=1, max_length=MAX_GOAL_CHARS)
    title: str | None = Field(default=None, max_length=MAX_TITLE)
    model: str | None = None
    budget: Budget = Budget()
    max_turns: int | None = Field(default=None, gt=0, le=MAX_TURNS)
    working_time: str | None = None
    repos: list[str] = Field(default_factory=list, max_length=MAX_REPOS)
    tools: list[str] | None = None
    tasks_project: str | None = Field(default=None, max_length=200)
    report_to: str = Field(default=REPORT_TO_CONVERSATION, min_length=1, max_length=200)
    dry_run: bool = False

    @field_validator("goal")
    @classmethod
    def _goal_has_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("goal is empty")
        return value.strip()

    @field_validator("title")
    @classmethod
    def _title_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return " ".join(value.split()) or None

    @field_validator("working_time")
    @classmethod
    def _check_working_time(cls, value: str | None) -> str | None:
        if value is not None:
            check_duration(value)
        return value

    @field_validator("tools")
    @classmethod
    def _check_tools(cls, value: list[str] | None) -> list[str] | None:
        for name in value or []:
            if not _MCP_NAME_RE.match(name):
                raise ValueError("each tool is the name of an MCP server, such as gitlab")
        return value


def saved_name(raw: Any) -> str:
    """``raw`` as the name of a saved assignment, in lower case. Raises ValueError."""
    name = raw.strip().lower() if isinstance(raw, str) else ""
    if not SAVED_NAME_RE.match(name):
        raise ValueError(
            "the name of a saved assignment is 2 to 60 lower case letters, digits, "
            "and hyphens, such as docs-cleanup"
        )
    return name


def check_duration(value: str) -> int:
    """The seconds in ``value``. Raises ValueError when it is not more than zero."""
    seconds = parse_duration(value)
    if seconds <= 0:
        raise ValueError("a duration must be more than zero")
    return seconds


def first_error(exc: Exception) -> str:
    """The first error of a pydantic error, as ``location: message``, with no input value."""
    errors = getattr(exc, "errors", None)
    if not callable(errors):
        return str(exc)
    first = errors()[0]
    where = ".".join(str(part) for part in first.get("loc", ())) or "spec"
    return f"{where}: {first.get('msg', 'not valid')}"


@dataclass
class Resolved:
    """A spec with every value filled in, and the notes about what changed."""

    spec: dict[str, Any]
    model_id: str
    effort: str
    notes: list[str] = field(default_factory=list)


def clamp(name: str, value: int, maximum: int, notes: list[str]) -> int:
    """``value``, or ``maximum`` when ``value`` is above it. A clamp adds a note."""
    if value > maximum:
        notes.append(f"{name} {value} is above the maximum {maximum}, so it is {maximum}")
        return maximum
    return value


def check_repo(raw: Any, section: Assignments) -> str:
    """``raw`` as ``host/group/name`` when ``assignments.repos`` allows it.

    Raises SpecRefused ``invalid_repo`` or ``repo_not_allowed``. A repository
    of a spec and a repository that Joshua grants later both pass here.
    """
    try:
        repo = normalize_repo(raw if isinstance(raw, str) else "")
    except RepoError as exc:
        raise SpecRefused("invalid_repo", str(exc)) from None
    if not repo_allowed(repo, section.repos):
        raise SpecRefused("repo_not_allowed", f"the repository {repo} is not in assignments.repos")
    return repo


def resolve(spec: AssignmentSpec, section: Assignments) -> Resolved:
    """Fill, clamp, and check ``spec``. Raises SpecRefused for a value the section refuses."""
    notes: list[str] = []
    model = spec.model or section.default_model
    if model not in section.models:
        known = ", ".join(sorted(section.models))
        raise SpecRefused("unknown_model", f"the model {model!r} is not known; use one of {known}")
    repos: list[str] = []
    for raw in spec.repos:
        repo = check_repo(raw, section)
        if repo not in repos:
            repos.append(repo)
    limits = section.limits
    five = clamp(
        "budget.five_hour_pct",
        spec.budget.five_hour_pct or limits.five_hour_pct,
        limits.max_five_hour_pct,
        notes,
    )
    seven = clamp(
        "budget.seven_day_pct",
        spec.budget.seven_day_pct or limits.seven_day_pct,
        limits.max_seven_day_pct,
        notes,
    )
    persona = section.models[model]
    resolved = {
        "goal": spec.goal,
        "title": spec.title or title_of(spec.goal),
        "model": model,
        "budget": {"five_hour_pct": five, "seven_day_pct": seven, "tokens": spec.budget.tokens},
        "max_turns": spec.max_turns or section.default_max_turns,
        "working_time": spec.working_time or section.default_working_time,
        "repos": repos,
        "tools": spec.tools,
        "tasks_project": spec.tasks_project,
        "report_to": spec.report_to,
        "dry_run": spec.dry_run,
    }
    return Resolved(spec=resolved, model_id=persona.model, effort=persona.effort, notes=notes)


def _first_line(goal: str) -> str:
    return next((text.strip() for text in goal.splitlines() if text.strip()), "")


def _cut(words: list[str], limit: int, sep: str) -> str:
    """The first words of ``words``, joined by ``sep``, at most ``limit`` long."""
    out = ""
    for word in words:
        candidate = f"{out}{sep}{word}" if out else word
        if len(candidate) > limit:
            break
        out = candidate
    return out


def slug(goal: str) -> str:
    """The first line of ``goal`` with text, in kebab case, at most ``SLUG_CHARS`` long."""
    ascii_text = unicodedata.normalize("NFKD", _first_line(goal)).encode("ascii", "ignore").decode()
    words = re.findall(r"[a-z0-9]+", ascii_text.lower())
    return _cut(words, SLUG_CHARS, "-") or "assignment"


def title_of(goal: str) -> str:
    """The first sentence of ``goal``, at most ``TITLE_CHARS`` long, cut at a word."""
    line = _first_line(goal)
    sentence = re.split(r"(?<=[.!?])\s", line, maxsplit=1)[0].rstrip(".")
    words = sentence.split()
    title = _cut(words, TITLE_CHARS, " ")
    if not title and words:
        title = words[0][:TITLE_CHARS]
    return title or "Assignment"


def assignment_title(spec: dict[str, Any] | None) -> str:
    """The title of an assignment: ``title`` of its spec, else one from its goal."""
    spec = spec or {}
    return str(spec.get("title") or title_of(str(spec.get("goal") or "")))
