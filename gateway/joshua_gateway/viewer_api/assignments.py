"""The JSON routes of the assignments: the list, one assignment, its comments.

- ``GET /api/v1/assignments?state=<state>`` gives the assignments of the
  person who signed in, one state at a time. With no ``state``, the answer
  holds those in progress when there is one, else the drafts when there is
  one, else the completed ones.
- ``GET /api/v1/assignments/{id}`` gives one assignment: its numbers, its
  definition as properties and blocks, its plan with the progress of its
  task boxes, its report when there is one, and the comments on each of the
  three texts.
- ``POST /api/v1/assignments/{id}/comments`` adds one comment of the person
  on a line of the definition, the plan, or the report (``target``).
- ``POST /api/v1/assignments/{id}/comments/{cid}/delete`` removes their own
  open comment.

The manager is reached only when ``ASSIGNMENTS_URL`` and
``JOSHUA_TOKEN_VIEWER`` are set (``viewer_assignments.from_env``). With
either one missing, a read answers 200 with ``configured`` false, and the
page says that the manager is not configured and selects no state. A call
that fails answers 200 with ``available`` false. A row of another person is
404, as the manager answers it.

Every string a person reads is derived here, never in the page: the duration
of a run, the phrase of its start, the label of its status, the text of a
notice, and what an empty list says. ``refresh_seconds`` is the time between
two reads of an assignment in progress, so the page polls instead of
reloading itself.

The answers hold data: the React app renders the list and the assignment
(``src/pages/Assignments.tsx``, ``src/pages/Assignment.tsx`` and
``src/assignments/``). The saved assignments still render on the server
(``viewer.saved_list`` and ``viewer.saved_page``).
"""

from __future__ import annotations

import os
from datetime import datetime, tzinfo
from typing import Any

from joshua_shared import config
from joshua_shared.config import Person
from joshua_shared.log import get_logger
from starlette.requests import Request
from starlette.responses import JSONResponse

from joshua_gateway import viewer_assignments as am
from joshua_gateway import viewer_blocks as vb
from joshua_gateway import viewer_comments as vc
from joshua_gateway import viewer_report as rp
from joshua_gateway.viewer_api.edit import (
    NOT_JSON,
    _answer,
    _bad,
    _csrf_token,
    _csrf_valid,
    _field,
    _fields,
    _origin_ok,
    _refused,
)
from joshua_gateway.viewer_usage import exact, short

logger = get_logger("viewer")

# The texts of an assignment that take a comment, and the start of the id of
# a unit of each: the definition keeps ``L``, as a document does.
COMMENT_TARGETS = ("definition", "plan", "report")
TARGET_PREFIX = {"definition": "L", **am.TEXT_PREFIX}

# Where the page goes after a comment is deleted, by its target.
TARGET_SECTION = {"definition": "definition", "plan": "plan", "report": "report"}

# What a comment on a target that is not one of the three says.
BAD_TARGET = "a bad target"

# With no ``state``, the list takes the first of these that holds a row.
PICK_ORDER = ("in_progress", "draft", "completed")


def comments_scope(assignment_id: int) -> str:
    """The CSRF scope of the comments of one assignment."""
    return f"comments:/assignments/{assignment_id}"


def _not_found() -> JSONResponse:
    return _answer({"error": "not found"}, 404)


def _clock() -> tuple[str, tzinfo, datetime]:
    """The timezone of the instance, its zone, and the time now in it."""
    from joshua_gateway import viewer  # ``viewer`` imports this package

    tz, now = viewer._clock()
    return config.load().timezone, tz, now


# -- the strings a person reads ---------------------------------------------


def when_phrase(item: am.Item, *, now: datetime) -> str:
    """When a run started, created, or ended: ``started 2h ago``."""
    for label, text in (
        ("ended", item.finished_at),
        ("started", item.started_at),
        ("created", item.created_at),
    ):
        when = vc.moment(text)
        if when is not None:
            return f"{label} {vc.ago((now - when).total_seconds())}"
    return ""


def ago(text: str | None, *, now: datetime) -> str:
    """One time of the manager, relative: ``5m ago``, or "" when it is none."""
    when = vc.moment(text)
    return "" if when is None else vc.ago((now - when).total_seconds())


def cost_text(item: am.Item) -> str:
    """The tokens of a run, short: ``12.3k tokens``. A draft has no cost."""
    return "" if item.state == "draft" else f"{short(item.usage.total)} tokens"


# -- the list ---------------------------------------------------------------


def _row(item: am.Item, *, now: datetime) -> dict[str, Any]:
    """One row of the list, with every string it shows."""
    return {
        "id": item.id,
        "url": f"/assignments/{item.id}",
        "title": item.title,
        "state": item.state,
        "status": item.status,
        "status_label": am.status_label(item.status),
        "when": when_phrase(item, now=now),
        "duration": am.duration(item.working_s),
        "cost": cost_text(item),
        "turns": item.turns,
        "summary_line": am.summary_line(item),
        "asks": bool(item.open_question),
        "start_phrase": am.start_phrase(item.id) if item.state == "draft" else "",
    }


async def assignments(request: Request, person: Person) -> JSONResponse:
    """The assignments of the person who signed in, one state at a time.

    With no ``state``, the answer holds those in progress when there is one,
    else the drafts when there is one, else the completed ones. A manager
    that is not configured and one that does not answer each give 200 with
    no items and no state, so the page says so and selects no pill.
    """
    client = am.from_env(os.environ)
    timezone, _tz, now = _clock()
    state = am.parse_state(request.query_params.get("state"))
    body = None
    if client is None:
        state = None
    elif state is None:
        for state in PICK_ORDER:
            body = await client.list(person.id, state)
            if body is None:
                state = None
                break
            if body.items:
                break
    else:
        body = await client.list(person.id, state)
        if body is None:
            state = None
    return _answer(
        {
            "configured": client is not None,
            "available": body is not None,
            "state": state,
            "states": [{"key": key, "label": label} for key, label in am.STATES],
            "items": [_row(item, now=now) for item in (body.items if body else [])],
            "empty": am.empty_text(state),
            "timezone": timezone,
            "now": now.isoformat(),
        }
    )


# -- one assignment ---------------------------------------------------------


def _comment(comment: am.Comment, person_id: str) -> dict[str, Any]:
    """One comment, as the gutter of the page reads it. The manager shows a
    person their own comments only, so the author is the person."""
    return {
        "id": str(comment.id),
        "target": comment.target,
        "line_no": comment.line_no,
        "line_text": comment.line_text,
        "line_now": comment.line_now,
        "stale": comment.stale,
        "body": comment.body,
        "author": person_id,
        "created_at": comment.created_at,
        "resolved_at": comment.resolved_at,
        "resolution": comment.resolution,
    }


def _comments(item: am.Detail, person_id: str) -> dict[str, list[dict[str, Any]]]:
    """The comments of the assignment, by the text each one is on."""
    return {
        target: [_comment(c, person_id) for c in item.comments if c.target == target]
        for target in COMMENT_TARGETS
    }


def _unit(unit: am.Unit) -> dict[str, Any]:
    """One commentable part of the definition: a property, the head of the
    goal, or one block of the goal."""
    return {
        "id": f"L{unit.first}",
        "first_line": unit.first,
        "last_line": unit.last,
        "kind": unit.kind,
        "label": unit.label,
        "value": unit.value,
        "markdown": unit.markdown,
        "depth": unit.depth,
        "what": unit.what,
    }


def _goal_offset(text: str) -> int:
    """The line of the ``goal:`` key of the definition, or 0 when the goal is
    not a block scalar."""
    for item in vb.fields(text):
        if item.key == "goal" and vb.goal_offset(item) is not None:
            return item.first
    return 0


def _definition(item: am.Detail) -> dict[str, Any]:
    """The definition as a document: the fields as properties, then the goal
    as blocks. A manager that gives no ``definition`` text gives the spec as
    YAML, and then no line takes a comment."""
    text = item.definition
    if text is None:
        return {"units": [], "lines": am.spec_yaml(item.spec).split("\n"), "goal_offset": 0}
    props, goal = am.definition_units(text)
    return {
        "units": [_unit(unit) for unit in [*props, *goal]],
        "lines": am.text_lines(text),
        "goal_offset": _goal_offset(text),
    }


def _blocks(text: str | None, prefix: str) -> list[dict[str, Any]]:
    """The blocks of the plan or the report, each with its lines of the text."""
    lines = am.text_lines(text)
    return [
        {
            "id": f"{prefix}{block.first}",
            "kind": block.kind,
            "depth": min(block.depth, vb.MAX_DEPTH),
            "markdown": vb.block_markdown(lines, block),
            "first_line": block.first,
            "last_line": block.last,
        }
        for block in vb.blocks(text or "")
    ]


def _plan(item: am.Detail, *, now: datetime) -> dict[str, Any]:
    """The plan, its blocks, and the progress of its task boxes."""
    progress = rp.plan_progress(item.plan)
    return {
        "markdown": item.plan or "",
        "lines": am.text_lines(item.plan),
        "blocks": _blocks(item.plan, TARGET_PREFIX["plan"]),
        "updated": ago(item.plan_at, now=now),
        "progress": {
            "done": progress.done,
            "total": progress.total,
            "next": progress.next_step,
            "percent": round(progress.percent, 1),
        },
    }


def _section(key: str, section: rp.Section) -> dict[str, Any]:
    return {
        "key": key,
        "title": section.title,
        "body": section.body,
        "line": section.line,
        "start": section.start,
        "end": section.end,
    }


def _report(item: am.Detail, *, now: datetime) -> dict[str, Any] | None:
    """The report, its blocks, and its sections, or None when there is none.

    The showcase of the report is the next task: the page shows the blocks
    until then.
    """
    if not (item.report and item.report.strip()):
        return None
    report = rp.parse(item.report)
    return {
        "markdown": item.report,
        "lines": am.text_lines(item.report),
        "blocks": _blocks(item.report, TARGET_PREFIX["report"]),
        "updated": ago(item.report_at, now=now),
        "title": report.title,
        "lede": report.lede,
        "structured": report.structured,
        "sections": {key: _section(key, part) for key, part in report.sections.items()},
    }


def _notice(item: am.Detail, *, tz: tzinfo, now: datetime) -> dict[str, Any] | None:
    """What the assignment waits for: a request, a question, or the limits."""
    request = item.open_request
    asked = ago(item.asked_at, now=now)
    if request is not None and request.kind in am.REQUEST_HEADINGS:
        return {
            "kind": "request",
            "heading": f"The assignment {am.REQUEST_HEADINGS[request.kind]}",
            "when": asked,
            "ref": request.ref,
            "text": f"Reason: {request.reason}" if request.reason.strip() else "",
            "foot": "Joshua grants it, provides it, or refuses it.",
        }
    if item.open_question:
        return {
            "kind": "question",
            "heading": "The assignment asks",
            "when": asked,
            "ref": "",
            "text": item.open_question,
            "foot": "Answer it in a conversation with Joshua.",
        }
    if item.stopped_at_limits:
        return {
            "kind": "limits",
            "heading": "Paused at the subscription limits",
            "when": "",
            "ref": "",
            "text": am.limits_text(item.stopped_at_limits, tz=tz, now=now),
            "foot": "",
        }
    return None


def _item(item: am.Detail, *, tz: tzinfo, now: datetime) -> dict[str, Any]:
    """The assignment itself: its marks, its numbers, and its progress."""
    found = am.run_number(item)
    return {
        "id": item.id,
        "title": item.title,
        "state": item.state,
        "status": item.status,
        "status_label": am.status_label(item.status),
        "when": when_phrase(item, now=now),
        "model": item.model,
        "usage": item.usage.model_dump(),
        "cost": cost_text(item),
        "cost_title": f"{exact(item.usage.total)} tokens: {am.breakdown(item.usage)}",
        "duration": am.duration(item.working_s),
        "turns": item.turns,
        "started_at": item.started_at,
        "finished_at": item.finished_at,
        "error": item.error or "",
        "open_comments": item.open_comments,
        "start_phrase": am.start_phrase(item.id) if item.state == "draft" else "",
        "draft_line": am.DRAFT_LINE if item.state == "draft" else "",
        "notice": _notice(item, tz=tz, now=now),
        "checkpoints": [
            {"at": point.at, "when": ago(point.at, now=now), "note": point.note}
            for point in item.checkpoints
        ],
        "outputs": [
            {"kind": out.kind, "ref": out.ref, "title": out.title, "when": ago(out.at, now=now)}
            for out in item.outputs
        ],
        "open_request": (
            None
            if item.open_request is None
            else {
                "kind": item.open_request.kind,
                "ref": item.open_request.ref,
                "reason": item.open_request.reason,
            }
        ),
        "lineage": [
            {
                "id": run.id,
                "url": f"/assignments/{run.id}",
                "title": run.title,
                "status": run.status,
                "status_label": am.status_label(run.status),
                "when": ago(run.finished_at or run.created_at, now=now),
                "current": run.id == item.id,
            }
            for run in item.lineage
        ],
        "run_number": None if found is None else {"run": found[0], "root": found[1]},
        "saved_ref": None if item.saved is None else {"id": item.saved.id, "name": item.saved.name},
    }


def _blank(configured: bool, *, timezone: str, now: datetime, **more: Any) -> dict[str, Any]:
    """The answer of a page with no assignment to show."""
    return {
        "configured": configured,
        "available": False,
        "not_found": False,
        "item": None,
        "definition": None,
        "plan": None,
        "report": None,
        "comments": {target: [] for target in COMMENT_TARGETS},
        "csrf": None,
        "refresh_seconds": None,
        "timezone": timezone,
        "now": now.isoformat(),
        **more,
    }


async def assignment(request: Request, person: Person) -> JSONResponse:
    """One assignment of the person who signed in, as the page reads it.

    A row of another person is 404, as the manager answers it. A manager
    that is not configured and one that does not answer each give 200 with
    no item, and the page says so.
    """
    client = am.from_env(os.environ)
    timezone, tz, now = _clock()
    if client is None:
        return _answer(_blank(False, timezone=timezone, now=now))
    item = await client.detail(person.id, request.path_params["id"])
    if item is am.NOT_FOUND:
        return _answer(_blank(True, timezone=timezone, now=now, not_found=True), 404)
    if not isinstance(item, am.Detail):
        return _answer(_blank(True, timezone=timezone, now=now))
    # A manager that gives no definition text counts no line, so no text of
    # this assignment takes a comment.
    csrf = None if item.definition is None else _csrf_token(person.id, comments_scope(item.id))
    return _answer(
        {
            "configured": True,
            "available": True,
            "not_found": False,
            "item": _item(item, tz=tz, now=now),
            "definition": _definition(item),
            "plan": _plan(item, now=now),
            "report": _report(item, now=now),
            "comments": _comments(item, person.id),
            "csrf": csrf,
            "refresh_seconds": am.REFRESH_S if item.state == "in_progress" else None,
            "timezone": timezone,
            "now": now.isoformat(),
        }
    )


# -- the comments -----------------------------------------------------------


def _line_no(fields: dict[str, Any]) -> int:
    """The ``line_no`` of the body, or 0 when it is not a number. The manager
    refuses a line that its text does not hold."""
    raw = fields.get("line_no")
    if isinstance(raw, bool):
        return 0
    try:
        return int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def _checked(request: Request, person: Person, fields: dict[str, Any]) -> JSONResponse | str:
    """The target of a write, or the refusal of the checks of every write."""
    assignment_id = int(request.path_params["id"])
    if not _csrf_valid(person.id, comments_scope(assignment_id), _field(fields, "csrf")):
        return _refused(403)
    if not _origin_ok(request):
        return _refused(403)
    target = _field(fields, "target") or "definition"
    return target if target in COMMENT_TARGETS else _bad(BAD_TARGET)


def _written(written: am.Written | None, *, anchor: str, delete: bool) -> JSONResponse:
    """The answer of the manager, as the page reads it. A row of another
    person is 404, and a refusal is 400 with what the manager said."""
    if written is not None and written.status == 404:
        return _not_found()
    if written is None or not written.ok:
        reason = written.error if written is not None else "the manager did not answer"
        return _bad(reason or "The manager refused the comment.")
    logger.info({"message": "viewer comment", "kind": "assignment", "delete": delete})
    return _answer({"ok": True, "anchor": anchor})


async def add_comment(request: Request, person: Person) -> JSONResponse:
    """Send one comment of the person on a line of a text of the assignment.

    ``target`` names the text: the definition, the plan, or the report. The
    manager stores it as the person who signed in, on their own row only.
    """
    client = am.from_env(os.environ)
    if client is None:
        return _not_found()
    fields = await _fields(request)
    if fields is None:
        return _bad(NOT_JSON)
    target = _checked(request, person, fields)
    if isinstance(target, JSONResponse):
        return target
    line_no = _line_no(fields)
    written = await client.add_comment(
        person.id,
        "assignment",
        int(request.path_params["id"]),
        line_no=line_no,
        line_text=_field(fields, "line_text"),
        body=_field(fields, "body"),
        target=target,
    )
    return _written(written, anchor=f"{TARGET_PREFIX[target]}{line_no}", delete=False)


async def delete_comment(request: Request, person: Person) -> JSONResponse:
    """Remove one open comment of the person from the manager."""
    client = am.from_env(os.environ)
    if client is None:
        return _not_found()
    fields = await _fields(request)
    if fields is None:
        return _bad(NOT_JSON)
    target = _checked(request, person, fields)
    if isinstance(target, JSONResponse):
        return target
    written = await client.delete_comment(
        person.id,
        "assignment",
        int(request.path_params["id"]),
        int(request.path_params["cid"]),
    )
    return _written(written, anchor=TARGET_SECTION[target], delete=True)
