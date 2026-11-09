"""The read API of the manager: plain JSON routes beside ``/mcp``, for the viewer.

``GET /v1/assignments``, ``GET /v1/assignments/{id}``, ``GET /v1/saved``, and
``GET /v1/saved/{id}`` read the store. A caller sends its own fleet token,
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
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from joshua_assignments.digest import lede, next_step
from joshua_assignments.manager import Manager, saved_ref
from joshua_assignments.settings import Settings
from joshua_assignments.spec import assignment_title
from joshua_assignments.store import STATES, USAGE_FIELDS, state, statuses_of

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


def detail(
    row: dict[str, Any], names: Mapping[int, str], checkpoints: list[dict[str, Any]]
) -> dict[str, Any]:
    """The whole view of one assignment. ``checkpoints`` are the newest first."""
    return {
        **item(row, names),
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


def saved_item(row: dict[str, Any]) -> dict[str, Any]:
    """The view of one saved assignment, with the count of its runs and its newest run."""
    last = row.get("last_run")
    return {
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
        return JSONResponse(detail(row, await self._names([row]), checkpoints))

    async def saved_list(self, request: Request) -> Response:
        person = _person(request)
        if person is None:
            return _error(400, "person is required: a person id")
        rows = await self.manager.store.list_saved(person)
        return JSONResponse({"items": [saved_item(row) for row in rows]})

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
        return JSONResponse({**saved_item(row), "runs_list": [item(run, names) for run in runs]})
