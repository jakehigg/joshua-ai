"""The read API of the manager: plain JSON routes beside ``/mcp``, for the viewer.

``GET /v1/assignments``, ``GET /v1/assignments/{id}``, ``GET /v1/saved``, and
``GET /v1/saved/{id}`` read the store. The comment routes
(``/v1/assignments/{id}/comments`` and ``/v1/saved/{id}/comments``) read the
comments on a definition, add one, and delete one: the only writes here. A
comment is written as the person that ``person`` names, on a row of that
person only, and only its author deletes it, while it is open. A caller sends its own fleet token,
``Authorization: Bearer <token>``. The bearer must be the
``JOSHUA_TOKEN_<NAME>`` of an identity in ``assignments.read_callers``
(default ``viewer``): the middleware of ``server`` checks it, and a missing
or wrong bearer gets 401. A fleet token is never a bearer of ``/mcp``, and a
bearer of ``/mcp`` is never one here.

The caller names the person in the ``person`` query value, as the gateway
names the person to core. A missing or bad ``person`` is 400. A row of
another person is 404, the same as a row that does not exist. A list holds
only the rows of that person. No answer holds a token or a bearer.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from joshua_shared.fleet_auth import identify_bearer
from joshua_shared.log import get_logger
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from joshua_assignments import definition as definition_mod
from joshua_assignments.comments import (
    MAX_OPEN_COMMENTS,
    CommentRefused,
    comment_view,
    is_open,
    new_comment,
)
from joshua_assignments.digest import lede, next_step
from joshua_assignments.manager import Manager, saved_ref
from joshua_assignments.settings import Settings
from joshua_assignments.spec import assignment_title
from joshua_assignments.store import STATES, USAGE_FIELDS, Parent, state, statuses_of

logger = get_logger("joshua_assignments.read_api")

READ_PREFIX = "/v1/"
DEFAULT_LIMIT = 50
MAX_LIMIT = 200
MAX_CHECKPOINTS = 200
MAX_RUNS = 50
_PERSON_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}\Z")


def read_caller(settings: Settings, callers: list[str], authorization: str | None) -> str | None:
    """The identity in ``callers`` whose fleet token ``authorization`` holds, or None."""
    return identify_bearer(authorization, settings.read_tokens_of(callers))


def _iso(value: Any) -> Any:
    return value.isoformat() if hasattr(value, "isoformat") else value


def item(row: dict[str, Any], names: Mapping[int, str]) -> dict[str, Any]:
    """The short view of one assignment."""
    spec = row.get("spec") or {}
    usage = {name: int(row.get(name) or 0) for name in USAGE_FIELDS}
    return {
        "id": int(row["id"]),
        "title": assignment_title(spec),
        "status": row["status"],
        "state": state(row["status"]),
        "model": str(spec.get("model") or row.get("model") or ""),
        "created_at": _iso(row.get("created_at")),
        "started_at": _iso(row.get("started_at")),
        "finished_at": _iso(row.get("finished_at")),
        "working_s": int(row.get("working_s") or 0),
        "turns": int(row.get("turns") or 0),
        "usage": {**usage, "total": sum(usage.values())},
        "outputs_count": len(row.get("outputs") or []),
        "lede": lede(row.get("report")),
        "next_step": next_step(row.get("plan")),
        "open_question": row.get("open_question"),
        "asked_at": _iso(row.get("asked_at")),
        "stopped_at_limits": row.get("stopped_at_limits"),
        "saved": saved_ref(row, names),
    }


def comments_part(spec: Any, rows: list[dict[str, Any]]) -> dict[str, Any]:
    """``definition``, ``comments``, and ``open_comments`` of a detail.

    ``definition`` is the spec as the text that the line numbers count."""
    text = definition_mod.render(spec or {})
    lines = text.split("\n") if text else []
    return {
        "definition": text,
        "comments": [comment_view(row, lines) for row in rows],
        "open_comments": sum(1 for row in rows if is_open(row)),
    }


def detail(
    row: dict[str, Any],
    names: Mapping[int, str],
    checkpoints: list[dict[str, Any]],
    comments: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """The whole view of one assignment. ``checkpoints`` are the newest first,
    ``comments`` the oldest first."""
    return {
        **item(row, names),
        **comments_part(row.get("spec"), comments or []),
        "spec": row.get("spec") or {},
        "plan": row.get("plan"),
        "plan_at": _iso(row.get("plan_at")),
        "report": row.get("report"),
        "report_at": _iso(row.get("report_at")),
        "outputs": row.get("outputs") or [],
        "checkpoints": [{"at": _iso(c.get("at")), "note": c.get("note")} for c in checkpoints],
        "summary": row.get("summary"),
        "follow_ups": row.get("follow_ups"),
        "answer": row.get("answer"),
        "error": row.get("error"),
    }


def saved_item(row: dict[str, Any], open_comments: int = 0) -> dict[str, Any]:
    """The view of one saved assignment, with the count of its runs and its newest run."""
    last = row.get("last_run")
    return {
        "open_comments": int(open_comments),
        "id": int(row["id"]),
        "name": row["name"],
        "title": assignment_title(row.get("spec")),
        "spec": row.get("spec") or {},
        "created_at": _iso(row.get("created_at")),
        "updated_at": _iso(row.get("updated_at")),
        "runs": int(row.get("runs") or 0),
        "last_run": (
            {
                "id": int(last["id"]),
                "status": last["status"],
                "finished_at": _iso(last.get("finished_at")),
            }
            if last
            else None
        ),
    }


def _error(status: int, text: str) -> JSONResponse:
    return JSONResponse({"error": text}, status_code=status)


def _person(request: Request) -> str | None:
    person = request.query_params.get("person", "")
    return person if _PERSON_RE.match(person) else None


def _not_found() -> JSONResponse:
    return _error(404, "not found")


class ReadApi:
    """The four routes, over the store of ``manager``."""

    def __init__(self, manager: Manager) -> None:
        self.manager = manager

    async def _names(self, rows: list[dict[str, Any]]) -> dict[int, str]:
        ids = {int(row["saved_id"]) for row in rows if row.get("saved_id") is not None}
        return await self.manager.store.saved_names(ids)

    async def assignments(self, request: Request) -> Response:
        person = _person(request)
        if person is None:
            return _error(400, "person is required: a person id")
        state_name = request.query_params.get("state")
        if state_name is not None and state_name not in STATES:
            return _error(400, f"state is one of {', '.join(STATES)}")
        raw_limit = request.query_params.get("limit", str(DEFAULT_LIMIT))
        try:
            limit = max(1, min(int(raw_limit), MAX_LIMIT))
        except ValueError:
            return _error(400, f"limit is a number from 1 to {MAX_LIMIT}")
        statuses = statuses_of(state_name) if state_name is not None else None
        rows = await self.manager.store.recent(person, limit, None, statuses)
        names = await self._names(rows)
        return JSONResponse({"items": [item(row, names) for row in rows]})

    async def assignment(self, request: Request) -> Response:
        person = _person(request)
        if person is None:
            return _error(400, "person is required: a person id")
        store = self.manager.store
        row = await store.get(int(request.path_params["assignment_id"]))
        if row is None or row["person_id"] != person:
            return _not_found()
        checkpoints = await store.checkpoints(row["id"], MAX_CHECKPOINTS)
        comments = await store.comments(person, Parent("assignment", int(row["id"])))
        return JSONResponse(detail(row, await self._names([row]), checkpoints, comments))

    async def saved_list(self, request: Request) -> Response:
        person = _person(request)
        if person is None:
            return _error(400, "person is required: a person id")
        store = self.manager.store
        rows = await store.list_saved(person)
        counts = await store.open_comment_counts(person, [int(row["id"]) for row in rows])
        items = [saved_item(row, counts.get(int(row["id"]), 0)) for row in rows]
        return JSONResponse({"items": items})

    async def saved(self, request: Request) -> Response:
        person = _person(request)
        if person is None:
            return _error(400, "person is required: a person id")
        store = self.manager.store
        row = await store.get_saved(int(request.path_params["saved_id"]))
        if row is None or row["person_id"] != person:
            return _not_found()
        runs = await store.recent(person, MAX_RUNS, saved_id=row["id"])
        names = {int(row["id"]): row["name"]}
        comments = await store.comments(person, Parent("saved", int(row["id"])))
        part = comments_part(row.get("spec"), comments)
        return JSONResponse(
            {
                **saved_item(row, part["open_comments"]),
                **part,
                "runs_list": [item(run, names) for run in runs],
            }
        )

    # --- comments -----------------------------------------------------------

    async def _parent(
        self, request: Request, kind: str
    ) -> tuple[str, Parent, list[str]] | JSONResponse:
        """The person, the parent, and the lines of its definition, or the error.

        A row of another person is 404, as a row that does not exist."""
        person = _person(request)
        if person is None:
            return _error(400, "person is required: a person id")
        store = self.manager.store
        if kind == "saved":
            parent = Parent("saved", int(request.path_params["saved_id"]))
            row = await store.get_saved(parent.id)
        else:
            parent = Parent("assignment", int(request.path_params["assignment_id"]))
            row = await store.get(parent.id)
        if row is None or row["person_id"] != person:
            return _not_found()
        return person, parent, definition_mod.lines(row.get("spec") or {})

    async def _comments(self, request: Request, kind: str) -> Response:
        found = await self._parent(request, kind)
        if isinstance(found, JSONResponse):
            return found
        person, parent, lines = found
        store = self.manager.store
        if request.method == "GET":
            rows = await store.comments(person, parent)
            return JSONResponse({"items": [comment_view(row, lines) for row in rows]})
        try:
            raw = await request.json()
        except ValueError:
            return _error(400, "the body is JSON")
        try:
            fields = new_comment(raw, lines)
        except CommentRefused as exc:
            return _error(400, str(exc))
        if len(await store.comments(person, parent, open_only=True)) >= MAX_OPEN_COMMENTS:
            return _error(409, f"a definition has at most {MAX_OPEN_COMMENTS} open comments")
        row = await store.add_comment(person, parent, **fields)
        logger.info(
            {
                "message": "comment stored",
                "comment_id": row["id"],
                "person": person,
                parent.column: parent.id,
            }
        )
        return JSONResponse(comment_view(row, lines), status_code=201)

    async def _delete_comment(self, request: Request, kind: str) -> Response:
        found = await self._parent(request, kind)
        if isinstance(found, JSONResponse):
            return found
        person, parent, _ = found
        store = self.manager.store
        comment_id = int(request.path_params["comment_id"])
        row = await store.get_comment(person, comment_id)
        if row is None or row.get(parent.column) != parent.id:
            return _not_found()
        if not is_open(row):
            return _error(409, "a resolved comment stays")
        if not await store.delete_comment(person, parent, comment_id):
            return _not_found()
        logger.info({"message": "comment deleted", "comment_id": comment_id, "person": person})
        return Response(status_code=204)

    async def assignment_comments(self, request: Request) -> Response:
        return await self._comments(request, "assignment")

    async def saved_comments(self, request: Request) -> Response:
        return await self._comments(request, "saved")

    async def assignment_comment(self, request: Request) -> Response:
        return await self._delete_comment(request, "assignment")

    async def saved_comment(self, request: Request) -> Response:
        return await self._delete_comment(request, "saved")
