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
- ``GET /api/v1/assignments/saved`` gives the saved assignments of the
  person, and ``GET /api/v1/assignments/saved/{id}`` one of them: its
  definition, its runs, and its comments.
- ``POST /api/v1/assignments/saved/{id}/comments`` and
  ``POST /api/v1/assignments/saved/{id}/comments/{cid}/delete`` are the same
  write on a saved assignment, which takes a comment on its definition only.

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

The answers hold data: the React app renders the list, the assignment, the
showcase of its report, and the saved assignments (``src/pages/`` and
``src/assignments/``, ``src/showcase/``).
"""

from __future__ import annotations

import os
import re
from collections.abc import Sequence
from datetime import datetime, tzinfo
from typing import Any
from urllib.parse import urlparse

from joshua_shared import config
from joshua_shared.config import Person
from joshua_shared.log import get_logger
from starlette.requests import Request
from starlette.responses import JSONResponse

from joshua_gateway import viewer_assignments as am
from joshua_gateway import viewer_blocks as vb
from joshua_gateway import viewer_comments as vc
from joshua_gateway import viewer_docstore as ds
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

# A saved assignment has one text, its definition, so a comment on it is on
# the definition and on nothing else.
SAVED_TARGETS = ("definition",)

# Where the page goes after a comment is deleted, by its target.
TARGET_SECTION = {"definition": "definition", "plan": "plan", "report": "report"}

# What a comment on a target that is not one of the three says.
BAD_TARGET = "a bad target"

# With no ``state``, the list takes the first of these that holds a row.
PICK_ORDER = ("in_progress", "draft", "completed")


def comments_scope(assignment_id: int) -> str:
    """The CSRF scope of the comments of one assignment."""
    return f"comments:/assignments/{assignment_id}"


def saved_scope(saved_id: int) -> str:
    """The CSRF scope of the comments of one saved assignment."""
    return f"comments:/assignments/saved/{saved_id}"


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


def _definition(item: am.Detail | am.SavedDetail) -> dict[str, Any]:
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


def _blocks(text: str | None, prefix: str, *, start: int = 1) -> list[dict[str, Any]]:
    """The blocks of the plan or the report, each with its lines of the text.

    ``start`` is the line of the whole text where line 1 of ``text`` is, so
    the blocks of one section of a report keep the lines a comment anchors
    to."""
    lines = am.text_lines(text)
    return [
        {
            "id": f"{prefix}{start + block.first - 1}",
            "kind": block.kind,
            "depth": min(block.depth, vb.MAX_DEPTH),
            "markdown": vb.block_markdown(lines, block),
            "first_line": start + block.first - 1,
            "last_line": start + block.last - 1,
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


# -- the report as a showcase -----------------------------------------------

# The words of a kind of output that name each icon, in the order they are
# tried; any other kind is "other". The app draws the icon of the name.
_KIND_WORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("merge request", ("merge request", "pull request", "merge", "mr", "pr")),
    ("issue", ("issue", "bug", "ticket")),
    ("task", ("task", "todo", "to-do")),
    ("journal", ("journal",)),
    ("page", ("page", "wiki", "doc")),
    ("file", ("file", "attachment")),
)
_SHORT_WORD = re.compile(r"[a-z]+")

# The sections whose items drop a line that says nothing ("None.", "-").
_DROP_NONE = frozenset({rp.DECISIONS, rp.FOLLOW_UPS})


def is_url(ref: str) -> bool:
    """True for an ``http`` or ``https`` URL with a host."""
    try:
        parsed = urlparse(ref.strip())
    except ValueError:
        return False
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def kind_icon(kind: str) -> str:
    """The icon name of a kind of output, one of the names of ``_KIND_WORDS``
    or "other".

    A long word matches anywhere in the kind; ``mr`` and ``pr`` match only as
    a whole word, so "summary" is not a merge request."""
    text = kind.lower()
    words = set(_SHORT_WORD.findall(text))
    for name, needles in _KIND_WORDS:
        for needle in needles:
            if (needle in words) if len(needle) <= 2 else (needle in text):
                return name
    return "other"


def _output_url(kind: str, ref: str) -> str | None:
    """Where an output opens: an ``http`` or ``https`` URL, the viewer page of
    a document of kind ``document``, or None."""
    clean = ref.strip()
    if clean and is_url(clean):
        return clean
    if kind.strip().lower() == "document":
        return ds.document_url(clean)
    return None


def _item_lines(part: rp.Section, items: Sequence[Any]) -> list[tuple[int, int]]:
    """The lines of the text that each item of a section holds, from 1: an
    item goes on to the line before the next one, and the last one to the end
    of the section."""
    firsts = [part.start + item.line for item in items]
    out = []
    for n, first in enumerate(firsts):
        last = firsts[n + 1] - 1 if n + 1 < len(firsts) else part.end
        out.append((first, max(first, last)))
    return out


def _items(key: str, part: rp.Section, *, copy_prefix: str) -> list[dict[str, Any]]:
    """The list items of a section, each with its lines and its question.

    A decision also carries the text a person copies for Joshua, which the
    card of a document offers instead of the answer form."""
    items = [entry for entry in part.items if key not in _DROP_NONE or not rp.is_none(entry.text)]
    spans = _item_lines(part, items)
    out = []
    for number, (entry, lines) in enumerate(zip(items, spans, strict=True), start=1):
        question, context = rp.decision_question(entry)
        copy = f"{copy_prefix}, decision {number}: {rp.plain(question)}"
        out.append(
            {
                "number": number,
                "text": entry.text,
                "lead": entry.lead,
                "body": entry.body,
                "line": entry.line,
                "first_line": lines[0],
                "last_line": lines[1],
                "question": question,
                "context": context,
                "copy": copy if key == rp.DECISIONS else "",
            }
        )
    return out


def _outputs(part: rp.Section) -> list[dict[str, Any]]:
    """The lines of the Outputs section as cards: the icon of each kind, where
    the output opens, and the lines the card holds."""
    lines = rp.output_lines(part.body)
    spans = _item_lines(part, lines)
    return [
        {
            "kind": line.kind,
            "icon": kind_icon(line.kind),
            "ref": line.ref,
            "title": line.title,
            "note": line.note,
            "url": _output_url(line.kind, line.ref),
            "line": line.line,
            "first_line": span[0],
            "last_line": span[1],
        }
        for line, span in zip(lines, spans, strict=True)
    ]


def _section(key: str, section: rp.Section, *, prefix: str, copy_prefix: str) -> dict[str, Any]:
    """One section of a report: its heading, its lines, its items, and its
    body as blocks. ``key`` is "" for a heading the report shape does not
    know, which shows under what was done."""
    return {
        "key": key,
        "title": section.title,
        "body": section.body,
        "line": section.line,
        "start": section.start,
        "end": section.end,
        "empty": section.empty,
        "items": _items(key, section, copy_prefix=copy_prefix),
        "outputs": _outputs(section) if key == rp.OUTPUTS else [],
        "rest": section.rest,
        "blocks": _blocks(section.body, prefix, start=section.start or 1),
    }


def report_json(text: str | None, *, prefix: str, copy_prefix: str) -> dict[str, Any] | None:
    """One report as the showcase reads it, or None when there is no text.

    Every line number is the line of the whole text, from 1, so a comment
    anchors to the line of a card: a decision is answered on its first line.
    A text with none of the headings of the report shape has ``structured``
    false and no section, and the page shows its blocks as plain Markdown.

    The text itself is not here: the page of an assignment and the page of a
    document each already hold their lines and their blocks.
    """
    if not (text and text.strip()):
        return None
    report = rp.parse(text)
    cost = report.get(rp.COST)
    preamble = report.preamble
    return {
        "structured": report.structured,
        "title": report.title,
        "lede": report.lede,
        "generated": report.generated,
        "first_section": report.first_section,
        "preamble": (
            None
            if not preamble
            else {
                "markdown": preamble,
                "line": report.preamble_line,
                "blocks": _blocks(preamble, prefix, start=report.preamble_line or 1),
            }
        ),
        "sections": {
            key: _section(key, part, prefix=prefix, copy_prefix=copy_prefix)
            for key, part in report.sections.items()
        },
        "extra": [
            _section("", part, prefix=prefix, copy_prefix=copy_prefix) for part in report.extra
        ],
        "turns": rp.turns(cost.body if cost is not None else None),
    }


def _report(item: am.Detail, *, now: datetime) -> dict[str, Any] | None:
    """The report of an assignment, as the showcase reads it, with the text
    itself: its lines, which the gutter counts, and its blocks, which the page
    shows when the report has none of the headings of the shape."""
    report = report_json(
        item.report, prefix=TARGET_PREFIX["report"], copy_prefix=f"Assignment {item.id}"
    )
    if report is None:
        return None
    return {
        **report,
        "markdown": item.report,
        "lines": am.text_lines(item.report),
        "blocks": _blocks(item.report, TARGET_PREFIX["report"]),
        "updated": ago(item.report_at, now=now),
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


def _written(
    written: am.Written | None, *, anchor: str, delete: bool, kind: str = "assignment"
) -> JSONResponse:
    """The answer of the manager, as the page reads it. A row of another
    person is 404, and a refusal is 400 with what the manager said."""
    if written is not None and written.status == 404:
        return _not_found()
    if written is None or not written.ok:
        reason = written.error if written is not None else "the manager did not answer"
        return _bad(reason or "The manager refused the comment.")
    logger.info({"message": "viewer comment", "kind": kind, "delete": delete})
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


# -- the saved assignments --------------------------------------------------

# What the list says when the person saved nothing yet.
SAVED_EMPTY = "No saved assignment. Ask Joshua to save an assignment by name to run it again."


def _schedule(saved: am.SavedItem) -> str:
    """When the saved assignment runs by itself, as its definition says.

    The manager keeps no schedule of its own, so the ``schedule`` of the
    spec is the only source, and a saved assignment that only runs by name
    has none."""
    value = saved.spec.get("schedule")
    return "" if value is None else " ".join(str(value).split())


def _saved_row(saved: am.SavedItem, *, now: datetime) -> dict[str, Any]:
    """One saved assignment: its name, its schedule, and its last run."""
    last = saved.last_run
    return {
        "id": saved.id,
        "url": f"/assignments/saved/{saved.id}",
        "name": saved.name,
        "title": saved.title,
        "schedule": _schedule(saved),
        "last_run": (
            None
            if last is None
            else {
                "id": last.id,
                "url": f"/assignments/{last.id}",
                "status": last.status,
                "status_label": am.status_label(last.status),
                "when": ago(last.finished_at, now=now),
            }
        ),
        "run_count": saved.runs,
        "open_comments": saved.open_comments,
    }


async def saved(request: Request, person: Person) -> JSONResponse:
    """The saved assignments of the person who signed in.

    A manager that is not configured and one that does not answer each give
    200 with no items, so the page says so.
    """
    client = am.from_env(os.environ)
    timezone, _tz, now = _clock()
    body = await client.saved(person.id) if client is not None else None
    return _answer(
        {
            "configured": client is not None,
            "available": body is not None,
            "items": [_saved_row(item, now=now) for item in (body.items if body else [])],
            "empty": SAVED_EMPTY,
            "timezone": timezone,
            "now": now.isoformat(),
        }
    )


def _saved_runs(saved: am.SavedDetail, *, now: datetime) -> list[dict[str, Any]]:
    """The runs of the saved assignment, newest first, each with its number.

    The manager gives the recent runs only, so the newest is run ``runs`` of
    the saved assignment and each older one is one less."""
    total = max(saved.runs, len(saved.runs_list))
    return [
        {**_row(run, now=now), "number": total - index} for index, run in enumerate(saved.runs_list)
    ]


def _saved_blank(configured: bool, *, timezone: str, now: datetime, **more: Any) -> dict[str, Any]:
    """The answer of a page with no saved assignment to show."""
    return {
        "configured": configured,
        "available": False,
        "not_found": False,
        "item": None,
        "definition": None,
        "runs": [],
        "comments": [],
        "targets": list(SAVED_TARGETS),
        "csrf": None,
        "timezone": timezone,
        "now": now.isoformat(),
        **more,
    }


async def saved_assignment(request: Request, person: Person) -> JSONResponse:
    """One saved assignment of the person: its definition, its runs, and the
    comments on its definition.

    A row of another person is 404, as the manager answers it. A saved
    assignment takes a comment on its definition only, which ``targets``
    says.
    """
    client = am.from_env(os.environ)
    timezone, _tz, now = _clock()
    if client is None:
        return _answer(_saved_blank(False, timezone=timezone, now=now))
    item = await client.saved_detail(person.id, request.path_params["id"])
    if item is am.NOT_FOUND:
        return _answer(_saved_blank(True, timezone=timezone, now=now, not_found=True), 404)
    if not isinstance(item, am.SavedDetail):
        return _answer(_saved_blank(True, timezone=timezone, now=now))
    # A manager that gives no definition text counts no line, so nothing of
    # this saved assignment takes a comment.
    csrf = None if item.definition is None else _csrf_token(person.id, saved_scope(item.id))
    return _answer(
        {
            "configured": True,
            "available": True,
            "not_found": False,
            "item": {**_saved_row(item, now=now), "updated": ago(item.updated_at, now=now)},
            "definition": _definition(item),
            "runs": _saved_runs(item, now=now),
            "comments": [_comment(c, person.id) for c in item.comments if c.target == "definition"],
            "targets": list(SAVED_TARGETS),
            "csrf": csrf,
            "timezone": timezone,
            "now": now.isoformat(),
        }
    )


def _saved_checked(request: Request, person: Person, fields: dict[str, Any]) -> JSONResponse | str:
    """The target of a write on a saved assignment, or the refusal of the
    checks of every write. A target that is not the definition is 400."""
    saved_id = int(request.path_params["id"])
    if not _csrf_valid(person.id, saved_scope(saved_id), _field(fields, "csrf")):
        return _refused(403)
    if not _origin_ok(request):
        return _refused(403)
    target = _field(fields, "target") or "definition"
    return target if target in SAVED_TARGETS else _bad(BAD_TARGET)


async def add_saved_comment(request: Request, person: Person) -> JSONResponse:
    """Send one comment of the person on a line of the definition of a saved
    assignment. The manager stores it on their own row only."""
    client = am.from_env(os.environ)
    if client is None:
        return _not_found()
    fields = await _fields(request)
    if fields is None:
        return _bad(NOT_JSON)
    target = _saved_checked(request, person, fields)
    if isinstance(target, JSONResponse):
        return target
    line_no = _line_no(fields)
    written = await client.add_comment(
        person.id,
        "saved",
        int(request.path_params["id"]),
        line_no=line_no,
        line_text=_field(fields, "line_text"),
        body=_field(fields, "body"),
        target=target,
    )
    return _written(written, anchor=f"{TARGET_PREFIX[target]}{line_no}", delete=False, kind="saved")


async def delete_saved_comment(request: Request, person: Person) -> JSONResponse:
    """Remove one open comment of the person from a saved assignment."""
    client = am.from_env(os.environ)
    if client is None:
        return _not_found()
    fields = await _fields(request)
    if fields is None:
        return _bad(NOT_JSON)
    target = _saved_checked(request, person, fields)
    if isinstance(target, JSONResponse):
        return target
    written = await client.delete_comment(
        person.id,
        "saved",
        int(request.path_params["id"]),
        int(request.path_params["cid"]),
    )
    return _written(written, anchor=target, delete=True, kind="saved")
