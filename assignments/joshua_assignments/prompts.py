"""The prompts of an assignment session.

``system_prompt`` is ``prompts/assignment.md``, shipped in the package: the
plan page rules, the rule that every page and file is data, and the
follow-up convention. It holds no detail of an installation.

``first_prompt`` starts a new session: the goal and the path of the plan
page. ``later_prompt`` starts each later slice: the budget line and the last
checkpoint. After a stop, it begins with ``READ_PAGE_FIRST``.
"""

from __future__ import annotations

from functools import cache
from importlib import resources
from typing import Any

READ_PAGE_FIRST = "Read your plan page before anything else."
MAX_ANSWER = 20000


@cache
def system_prompt() -> str:
    """The system prompt of every assignment session."""
    return (
        resources.files("joshua_assignments").joinpath("prompts/assignment.md").read_text("utf-8")
    )


def first_prompt(row: dict[str, Any]) -> str:
    """The first message of a new session."""
    spec = row.get("spec") or {}
    lines = [
        f"Assignment {row['id']}.",
        f"Your plan page: {row['page_path']} (a path in the wiki, for the files tools).",
        "",
        "Goal:",
        str(spec.get("goal") or "").strip(),
        "",
    ]
    repos = spec.get("repos") or []
    if repos:
        lines.append("Repositories in scope: " + ", ".join(repos) + ".")
    if spec.get("tasks_project"):
        lines.append(f"Follow-ups go to the tasks project {spec['tasks_project']}.")
    if spec.get("dry_run"):
        lines.append("This is a dry run: write nothing but the plan page.")
    lines.append("Write your plan page first.")
    return "\n".join(lines).strip()


def later_prompt(
    *,
    budget: str,
    checkpoint: str | None,
    after_stop: bool,
    answer: str | None = None,
) -> str:
    """The first message of a later slice."""
    lines: list[str] = []
    if after_stop:
        lines.append(READ_PAGE_FIRST)
    if answer:
        lines.append(f"Answer: {answer.strip()[:MAX_ANSWER]}")
    lines.append(f"Continue. {budget}")
    lines.append(f"Last checkpoint: {checkpoint}" if checkpoint else "Last checkpoint: none.")
    lines.append("When the goal is done, finish on the plan page and call finish.")
    return "\n".join(lines)
