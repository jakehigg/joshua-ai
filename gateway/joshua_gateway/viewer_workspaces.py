"""The charm beside the name of a workspace: working, waiting, or nothing.

A workspace carries one charm wherever its name shows, next to the two-person
mark of a shared one: Joshua is **working** for this workspace, or something
of it is **waiting** for a person. Waiting is a question of an assignment or
of a developer task, which the person answers. An unstarted draft is not a
question, so a workspace whose only open item is a draft carries no charm. A
workspace with nothing running and nothing waiting carries no charm. The charm
is read-only: it says what is happening and does nothing.

:func:`charms` is the one place that collects the sources and merges them, and
the workspaces routes (``viewer_api.workspaces``) put its answer on each
workspace as ``charm``. Working wins over waiting: Joshua is on it, so the
person has nothing to do yet.

The frontend reads the state and the one line of it, and no more
(``gateway/frontend/src/chat/WorkCharm.tsx``).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from joshua_gateway import viewer_assignments as va
from joshua_gateway import viewer_developer as vd

WORKING = "working"
WAITING = "waiting"

# What the charm says of a turn of the thread that has not finished.
TURN_REASON = "Joshua is answering in this workspace"

# The most characters of a line that comes from a source as text, such as the
# question of a developer job.
MAX_REASON = 200


@dataclass(frozen=True)
class Charm:
    """One charm: the state, and the line a person reads on hover or tap."""

    state: str
    reason: str


def charm_json(charm: Charm | None) -> dict[str, str] | None:
    """The charm as the routes send it, or None for no charm."""
    return None if charm is None else {"state": charm.state, "reason": charm.reason}


async def charms(
    person: str,
    rows: Sequence[Mapping[str, Any]],
    *,
    assignments: va.AssignmentsClient | None = None,
    developer: vd.DeveloperClient | None = None,
) -> dict[str, Charm]:
    """The charm of each workspace of ``rows``, by workspace id. A workspace
    with nothing running and nothing waiting is not in the answer.

    The sources are the turn of the thread, which core answers as
    ``turn_in_flight`` on each workspace, the assignments manager, which gives
    the runs and the drafts of this person, each with the workspace that
    started it, and the developer addon, which gives its jobs, each with the
    channel it reports to.

    A source that is not configured, or that does not answer, is left out
    instead of failing: the charm is then what the other sources say.
    """
    ids = {str(row.get("id")) for row in rows if row.get("id")}
    found: list[tuple[str, Charm]] = list(_from_turns(rows))
    if assignments is not None:
        found += await _from_assignments(person, assignments)
    if developer is not None:
        found += await _from_developer(developer)
    merged = _merge(found)
    return {workspace: charm for workspace, charm in merged.items() if workspace in ids}


def _from_turns(rows: Sequence[Mapping[str, Any]]) -> list[tuple[str, Charm]]:
    """A turn of the thread that has not finished: core says so on each row."""
    return [
        (str(row["id"]), Charm(WORKING, TURN_REASON))
        for row in rows
        if row.get("id") and row.get("turn_in_flight")
    ]


async def _from_assignments(person: str, client: va.AssignmentsClient) -> list[tuple[str, Charm]]:
    """The runs and the drafts of this person that belong to a workspace."""
    found: list[tuple[str, Charm]] = []
    for item in await client.open_rows(person):
        workspace = (item.workspace or "").strip()
        charm = _of_assignment(item)
        if workspace and charm is not None:
            found.append((workspace, charm))
    return found


def _of_assignment(item: va.Item) -> Charm | None:
    """The charm of one assignment, or None when it carries none."""
    if item.status == "running":
        return Charm(WORKING, f"assignment {item.id} is running")
    if item.status == "waiting":
        return Charm(WAITING, f"assignment {item.id} {_asked(item)}")
    # A draft is no charm: it asked nothing of the person, so nothing of it is
    # to clear. An assignment paused at the subscription limits is no charm
    # either: it waits for a window to reset, which needs no person, and
    # nothing of it is running.
    return None


def _asked(item: va.Item) -> str:
    """What a parked assignment asked for, as a person reads it."""
    kind = item.open_request.kind if item.open_request else ""
    return va.REQUEST_HEADINGS.get(kind, "has a question")


async def _from_developer(client: vd.DeveloperClient) -> list[tuple[str, Charm]]:
    """The jobs of the developer addon that have not ended, each on the
    workspace that dispatched it. A job that reports to another channel, or to
    none, belongs to no workspace and is left out."""
    found: list[tuple[str, Charm]] = []
    for job in await client.active_jobs():
        workspace = vd.workspace_of(job.notify_channel)
        if workspace:
            found.append((workspace, _of_job(job)))
    return found


def _of_job(job: vd.Job) -> Charm:
    """The charm of one developer job. A job that asked the person waits, with
    the question as the line of why; a job that works says so."""
    question = (job.question or "").strip()
    if question:
        return Charm(WAITING, question[:MAX_REASON])
    name = f"developer task {job.id}" if job.id else "a developer task"
    return Charm(WORKING, f"{name} is running")


def _merge(found: Iterable[tuple[str, Charm]]) -> dict[str, Charm]:
    """One charm for each workspace: working wins over waiting, and the first
    of the sources wins within a state."""
    out: dict[str, Charm] = {}
    for workspace, charm in found:
        old = out.get(workspace)
        if old is None or (old.state == WAITING and charm.state == WORKING):
            out[workspace] = charm
    return out
