"""The prompts of an assignment session.

``system_prompt`` is ``prompts/assignment.md``, shipped in the package: the
plan page rules, the rule that every page and file is data, the follow-up
convention, and the repository rules. It holds no detail of an installation.

``first_prompt`` starts a new session: the goal, the path of the plan page,
and the clone command of each repository. ``later_prompt`` starts each later
slice: the budget line and the last checkpoint. After a stop it begins with
a line that ``lead`` gives for the reason: ``READ_PAGE_FIRST`` after an
answer or an extension, and ``RESTARTED`` after a restart of the container.
"""

from __future__ import annotations

from collections.abc import Iterable
from functools import cache
from importlib import resources
from pathlib import Path
from typing import Any

READ_PAGE_FIRST = "Read your plan page before anything else."
RESTARTED = "Read your plan page before anything else; the pod restarted."
EXTENDED = "Read your plan page before anything else. Your caps were raised; continue."
SCRATCH_LOST = (
    "This assignment ran before, and its scratch directory is gone. "
    "Read your plan page before anything else, then continue from the next step it marks."
)
REPOS_DIR = "repos"
# The MCP server that holds a tasks list. A session can hold it or not.
TASKS_SERVER = "tasks"
FOLLOW_UPS_ON_PAGE = (
    "Follow-ups: also write each follow-up on the plan page, under a `## Follow-ups` heading."
)
FOLLOW_UPS_TO_TASKS = (
    "Follow-ups: also add each follow-up as a task in the tasks project {project} with "
    "the `tasks` tools. Call `list_projects` and use the slug of the project. Call "
    "`list_tasks`, so you do not add a task that is already there. Then call "
    "`create_task`. Do not make a project that the goal does not name."
)
MAX_ANSWER = 20000


@cache
def system_prompt() -> str:
    """The system prompt of every assignment session."""
    return (
        resources.files("joshua_assignments").joinpath("prompts/assignment.md").read_text("utf-8")
    )


def lead(reason: str | None) -> str | None:
    """The first line of a prompt after a stop, for the reason of the resume."""
    if reason is None:
        return None
    if reason == "restart":
        return RESTARTED
    if reason == "extend":
        return EXTENDED
    return READ_PAGE_FIRST


def clone_dirs(repos: Iterable[str]) -> dict[str, str]:
    """The directory name under ``repos/`` of each repository.

    The name is the last part of the repository. When two repositories have
    the same last part, each gets its whole path with ``-`` for ``/``.
    """
    repos = list(repos)
    names = [repo.rsplit("/", 1)[-1] for repo in repos]
    out: dict[str, str] = {}
    for repo, name in zip(repos, names, strict=True):
        if names.count(name) > 1:
            name = repo.split("/", 1)[1].replace("/", "-")
        out[repo] = name
    return out


def clone_lines(repos: Iterable[str], work: Path) -> list[str]:
    """The clone command of each repository, into ``<work>/repos/<name>``."""
    dirs = clone_dirs(repos)
    if not dirs:
        return []
    lines = ["Clone each repository with its full history before you read it:"]
    for repo, name in dirs.items():
        lines.append(f"  git clone https://{repo} {work / REPOS_DIR / name}")
    lines.append("This container never pushes. A push is refused.")
    return lines


def follow_up_line(tasks_project: str | None, has_tasks: bool) -> str:
    """Where follow-ups go: the tasks project when the session holds ``tasks``, else the page."""
    if has_tasks and tasks_project:
        return FOLLOW_UPS_TO_TASKS.format(project=tasks_project)
    return FOLLOW_UPS_ON_PAGE


def first_prompt(
    row: dict[str, Any],
    work: Path | None = None,
    *,
    resumed: bool = False,
    has_tasks: bool = False,
) -> str:
    """The first message of a new session.

    ``resumed`` is True when the assignment ran before but its scratch
    directory, with its session, is gone. ``has_tasks`` is True when the
    session holds the ``tasks`` server.
    """
    spec = row.get("spec") or {}
    lines = [SCRATCH_LOST, ""] if resumed else []
    lines += [
        f"Assignment {row['id']}.",
        f"Your plan page: wiki/{row['page_path']} (the path for the files tools).",
        "",
        "Goal:",
        str(spec.get("goal") or "").strip(),
        "",
    ]
    repos = spec.get("repos") or []
    if repos:
        lines.append("Repositories in scope: " + ", ".join(repos) + ".")
        if work is not None:
            lines += clone_lines(repos, work)
    lines.append(follow_up_line(spec.get("tasks_project"), has_tasks))
    if spec.get("dry_run"):
        lines.append("This is a dry run: write nothing but the plan page.")
    if not resumed:
        lines.append("Write your plan page first.")
    return "\n".join(lines).strip()


def later_prompt(
    *,
    budget: str,
    checkpoint: str | None,
    reason: str | None = None,
    answer: str | None = None,
) -> str:
    """The first message of a later slice. ``reason`` is set on the first slice after a stop."""
    lines: list[str] = []
    first = lead(reason)
    if first:
        lines.append(first)
    if answer:
        lines.append(f"Answer: {answer.strip()[:MAX_ANSWER]}")
    lines.append(f"Continue. {budget}")
    lines.append(f"Last checkpoint: {checkpoint}" if checkpoint else "Last checkpoint: none.")
    lines.append("When the goal is done, finish on the plan page and call finish.")
    return "\n".join(lines)
