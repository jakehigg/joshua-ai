"""Unit tests of :mod:`joshua_gateway.viewer_workspaces`: the collecting and
the merging of the charm beside the name of a workspace.

These call :func:`charms` directly, with rows built in the test and an
assignments source that is a real :class:`AssignmentsClient` over a
:class:`httpx.MockTransport` (no real HTTP) or simply ``None``. The viewer
routes that proxy this through core are covered in
``test_viewer_api_workspaces.py``; here the point is the function itself.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
from joshua_gateway import viewer_assignments as va
from joshua_gateway import viewer_workspaces as vw

WS = "11111111-1111-1111-1111-111111111111"
OTHER = "22222222-2222-2222-2222-222222222222"


def row(workspace: str = WS, **fields: Any) -> dict[str, Any]:
    return {"id": workspace, **fields}


def assignment(**fields: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": 31,
        "title": "RCA: the receipts",
        "status": "running",
        "state": "in_progress",
        "workspace": WS,
    }
    return base | fields


def client_of(
    *, draft: list[dict[str, Any]] | None = None, in_progress: list[dict[str, Any]] | None = None
) -> va.AssignmentsClient:
    """An :class:`AssignmentsClient` whose manager is a fake in-process
    transport, with the given rows by state."""
    rows = {"draft": draft or [], "in_progress": in_progress or []}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"items": rows.get(request.url.params.get("state"), [])})

    return va.AssignmentsClient(
        "http://assignments", "viewer-token", transport=httpx.MockTransport(handler)
    )


def down_client() -> va.AssignmentsClient:
    """An :class:`AssignmentsClient` whose manager never answers."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route")

    return va.AssignmentsClient(
        "http://assignments", "viewer-token", transport=httpx.MockTransport(handler)
    )


def charms(rows: list[dict[str, Any]], assignments: va.AssignmentsClient | None) -> dict[str, Any]:
    return asyncio.run(vw.charms("alex", rows, assignments=assignments))


def waiting(**fields: Any) -> dict[str, Any]:
    """An assignment that asked the person a question."""
    return assignment(id=36, status="waiting", open_question="Which invoice?", **fields)


def test_working_wins_over_waiting_when_both_are_true_of_one_workspace() -> None:
    """A turn in flight (``turn_in_flight``) and an assignment with a
    question, both of the same workspace: the working charm wins."""
    rows = [row(turn_in_flight=True)]
    client = client_of(in_progress=[waiting()])
    marks = charms(rows, client)
    assert marks[WS] == vw.Charm(vw.WORKING, vw.TURN_REASON)


def test_working_wins_over_waiting_from_the_assignments_source_alone() -> None:
    """The same workspace with an assignment that asks (waiting) and a run
    (working), both from the assignments source: working still wins."""
    rows = [row()]
    client = client_of(in_progress=[waiting(), assignment()])
    marks = charms(rows, client)
    assert marks[WS] == vw.Charm(vw.WORKING, "assignment 31 is running")
    # The other order of the same two gives the same charm.
    client = client_of(in_progress=[assignment(), waiting()])
    assert charms(rows, client)[WS] == vw.Charm(vw.WORKING, "assignment 31 is running")


def test_an_assignment_that_asks_gives_the_waiting_charm() -> None:
    """A question of an assignment is what waiting means."""
    marks = charms([row()], client_of(in_progress=[waiting()]))
    assert marks[WS] == vw.Charm(vw.WAITING, "assignment 36 has a question")


def test_a_draft_alone_gives_no_charm() -> None:
    """An unstarted draft asks the person nothing, so the workspace of it
    carries no charm: a person could not clear one."""
    client = client_of(draft=[assignment(id=36, status="draft", state="draft")])
    assert charms([row()], client) == {}
    # With a turn in flight, the charm is that turn and nothing of the draft.
    assert charms([row(turn_in_flight=True)], client)[WS] == vw.Charm(vw.WORKING, vw.TURN_REASON)


def test_a_source_that_is_absent_leaves_the_other_sources() -> None:
    """``assignments=None``: the turn source alone still gives a state."""
    rows = [row(turn_in_flight=True)]
    marks = charms(rows, None)
    assert marks[WS] == vw.Charm(vw.WORKING, vw.TURN_REASON)


def test_a_source_that_raises_leaves_the_other_sources() -> None:
    """The manager behind the assignments source never answers: the turn
    source alone still gives a state."""
    rows = [row(turn_in_flight=True)]
    marks = charms(rows, down_client())
    assert marks[WS] == vw.Charm(vw.WORKING, vw.TURN_REASON)


def test_no_source_with_anything_to_report_gives_no_charm() -> None:
    """Nothing in flight and nothing waiting: the workspace carries no
    charm, and is left out of the answer entirely."""
    rows = [row()]
    assert charms(rows, client_of()) == {}
    assert charms(rows, None) == {}


def test_a_run_parked_at_the_limits_gives_no_charm() -> None:
    """An assignment paused at the subscription limits waits for a window to
    reset, which needs no person, so it carries no charm."""
    rows = [row()]
    client = client_of(in_progress=[assignment(status="paused_limits")])
    assert charms(rows, client) == {}
