"""The JSON routes of the journal: one month of the feed, and one day.

The wiki tree shows the journal as ``YYYY/MM/DD`` folders, which is the wrong
shape for reading what happened, so the journal has a feed of its own:

- ``GET /api/v1/journal?month=YYYY-MM&person=<id>`` answers one month, newest
  day first, with the months that hold an entry and the people of the
  instance, so the app builds the month picker and the person filter. The
  default month is the newest month with an entry, and the current month in
  UTC when the journal is empty. An unknown ``person`` id is no filter.
- ``GET /api/v1/journal/{year}/{month}/{day}`` answers the entries of one day,
  with the nearest day that holds an entry on each side. A date that is not a
  date is 404.

``viewer_journal`` reads the files and selects them. This module answers only,
and the write of an entry is the editor (``viewer_api.edit``).
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

from joshua_shared import config
from joshua_shared.config import Person, is_member
from starlette.requests import Request
from starlette.responses import JSONResponse

from joshua_gateway import viewer_journal as journal
from joshua_gateway.files_mcp.server import data_root


def _answer(body: dict[str, Any], status: int = 200) -> JSONResponse:
    from joshua_gateway import viewer  # ``viewer`` imports this package

    return JSONResponse(body, status_code=status, headers=viewer._NO_STORE)


def _today() -> date:
    """Today in UTC: the month the feed shows when the journal holds nothing."""
    return datetime.now(UTC).date()


def _people() -> list[dict[str, str]]:
    """Every person of the instance, for the filter and the chips of an entry."""
    return [{"id": person.id, "name": person.name} for person in config.load().people]


def _month_name(month: tuple[int, int]) -> str:
    return f"{month[0]:04d}-{month[1]:02d}"


def entry_json(entry: journal.Entry, names: dict[str, str]) -> dict[str, Any]:
    """One entry: its day, its text as Markdown, and who it names. A person
    the config does not hold keeps their id as their name."""
    return {
        "day": entry.day.isoformat(),
        "day_url": entry.day_url,
        "slug": entry.slug,
        "title": entry.title,
        "source": entry.source,
        "people": [{"id": pid, "name": names.get(pid, pid)} for pid in entry.people],
        "markdown": entry.body,
        "edit_url": entry.edit_url,
    }


def _hop(day: date | None) -> dict[str, str] | None:
    """The nearest day on one side of a day, or None when there is none."""
    if day is None:
        return None
    return {"day": day.isoformat(), "url": journal.day_url(day)}


async def feed(request: Request, person: Person) -> JSONResponse:
    """The journal for one month, newest day first.

    ``month=YYYY-MM`` picks the month, ``person=<id>`` keeps the entries that
    name that person, and both are in the answer, so a bookmarked address
    shows the same thing.
    """
    entries = journal.load_entries(data_root())
    people = _people()
    person_id = (request.query_params.get("person") or "").strip() or None
    if person_id is not None and person_id not in {item["id"] for item in people}:
        person_id = None
    scoped = journal.for_person(entries, person_id)
    available = journal.months(scoped)
    month = journal.parse_month(request.query_params.get("month"))
    if month is None:
        today = _today()
        month = available[0] if available else (today.year, today.month)
    names = {item["id"]: item["name"] for item in people}
    return _answer(
        {
            "month": _month_name(month),
            "available": [_month_name(item) for item in available],
            "people": people,
            "person": person_id,
            "can_edit": is_member(person.role),
            "entries": [entry_json(entry, names) for entry in journal.for_month(scoped, month)],
        }
    )


async def day(request: Request, person: Person) -> JSONResponse:
    """One day of the journal, with the nearest older and newer day that
    holds an entry."""
    try:
        wanted = date(
            int(request.path_params["year"]),
            int(request.path_params["month"]),
            int(request.path_params["day"]),
        )
    except ValueError:
        return _answer({"error": "not found"}, 404)
    entries = journal.load_entries(data_root())
    all_days = journal.days(entries)  # newest first
    names = {item["id"]: item["name"] for item in _people()}
    return _answer(
        {
            "day": wanted.isoformat(),
            "can_edit": is_member(person.role),
            "entries": [entry_json(entry, names) for entry in journal.for_day(entries, wanted)],
            "older": _hop(next((item for item in all_days if item < wanted), None)),
            "newer": _hop(next((item for item in reversed(all_days) if item > wanted), None)),
        }
    )
