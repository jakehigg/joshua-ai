"""Offline tests for the JSON routes of the journal.

``GET /api/v1/journal`` answers one month of the feed, newest day first, with
the months that hold an entry and the people of the instance.
``GET /api/v1/journal/<year>/<month>/<day>`` answers one day, with the nearest
day that holds an entry on each side.

The feed reads ``wiki/journal/YYYY/MM/DD/*.md`` from a temp data root. The
default month of an empty journal is today, so every test fixes the clock
(``_today``) and no test reads the real one.
"""

from __future__ import annotations

import shutil
import textwrap
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from joshua_gateway import viewer
from joshua_gateway.viewer_api import journal as journal_api
from joshua_shared import config
from starlette.testclient import TestClient

ALEX = ("alex", "alex-secret")
MIA = ("mia", "mia-secret")

# The day the clock says, for the month an empty journal falls back to.
TODAY = date(2027, 3, 14)

CONFIG = textwrap.dedent("""\
    name: Test
    timezone: America/New_York
    people:
      - id: alex
        name: Alex
      - id: mia
        name: Mia
        role: guest
    viewer:
      enabled: true
      users:
        alex: ${VIEWER_PW_ALEX:-}
        mia: ${VIEWER_PW_MIA:-}
    """)

DAY_PAGE = textwrap.dedent("""\
    ---
    date: 2026-09-04
    people:
    - alex
    - mia
    source: nightly
    ---

    Alex booked the flights to Lisbon for October.
    """)

ENTRY = textwrap.dedent("""\
    ---
    date: 2026-09-04
    people:
    - alex
    source: agent
    ---

    # Oatmeal for breakfast

    Alex had oatmeal, with the good honey.
    """)

OLDER = textwrap.dedent("""\
    ---
    date: 2026-08-20
    people:
    - mia
    source: agent
    ---

    Mia started the pottery class on Wednesdays.
    """)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _tree(data: Path) -> None:
    j = data / "wiki" / "journal"
    _write(j / "2026/09/04/2026-09-04.md", DAY_PAGE)
    _write(j / "2026/09/04/oatmeal-breakfast.md", ENTRY)
    _write(j / "2026/08/20/pottery-class.md", OLDER)


@pytest.fixture
def client(monkeypatch, tmp_path) -> TestClient:
    data = tmp_path / "data"
    _tree(data)
    cfg_path = tmp_path / "joshua.yaml"
    cfg_path.write_text(CONFIG)
    monkeypatch.setenv(config.CONFIG_ENV_VAR, str(cfg_path))
    monkeypatch.setenv("JOSHUA_DATA_DIR", str(data))
    monkeypatch.setenv("VIEWER_PW_ALEX", ALEX[1])
    monkeypatch.setenv("VIEWER_PW_MIA", MIA[1])
    # The clock never comes from the day the test runs.
    monkeypatch.setattr(journal_api, "_today", lambda: TODAY)
    return TestClient(viewer.build_app())


def _feed(client: TestClient, query: str = "", auth: tuple[str, str] = ALEX) -> Any:
    response = client.get("/api/v1/journal" + query, auth=auth)
    assert response.status_code == 200, response.text
    return response.json()


def _day(client: TestClient, path: str, auth: tuple[str, str] = ALEX) -> Any:
    response = client.get("/api/v1/journal/" + path, auth=auth)
    assert response.status_code == 200, response.text
    return response.json()


def _titles(body: dict[str, Any]) -> list[str]:
    return [entry["title"] for entry in body["entries"]]


# -- the feed ---------------------------------------------------------------


def test_the_feed_needs_auth(client) -> None:
    assert client.get("/api/v1/journal").status_code == 401
    assert client.get("/api/v1/journal/2026/09/04").status_code == 401


def test_the_default_month_is_the_newest_month_with_an_entry(client) -> None:
    body = _feed(client)
    assert body["month"] == "2026-09"
    assert body["available"] == ["2026-09", "2026-08"]
    assert _titles(body) == ["The day", "Oatmeal for breakfast"]
    assert body["person"] is None


def test_the_default_month_of_an_empty_journal_is_today(client, tmp_path) -> None:
    shutil.rmtree(tmp_path / "data" / "wiki" / "journal")
    body = _feed(client)
    assert body["month"] == "2027-03"
    assert body["available"] == []
    assert body["entries"] == []


def test_the_month_of_the_query_picks_the_month(client) -> None:
    body = _feed(client, "?month=2026-08")
    assert body["month"] == "2026-08"
    assert _titles(body) == ["pottery class"]


def test_a_month_with_no_entry_is_an_empty_month(client) -> None:
    body = _feed(client, "?month=2020-01")
    assert body["month"] == "2020-01"
    assert body["entries"] == []
    assert body["available"] == ["2026-09", "2026-08"]


def test_a_bad_month_falls_back_to_the_newest_one(client) -> None:
    assert _feed(client, "?month=2026-13")["month"] == "2026-09"
    assert _feed(client, "?month=sept")["month"] == "2026-09"


def test_the_person_filter_keeps_the_entries_naming_that_person(client) -> None:
    body = _feed(client, "?person=mia")
    assert body["person"] == "mia"
    # The newest month with an entry of mia is August, not September.
    assert body["month"] == "2026-08"
    assert body["available"] == ["2026-09", "2026-08"]
    assert _titles(body) == ["pottery class"]
    assert _titles(_feed(client, "?month=2026-09&person=mia")) == ["The day"]


def test_an_unknown_person_is_no_filter(client) -> None:
    body = _feed(client, "?person=nobody")
    assert body["person"] is None
    assert _titles(body) == ["The day", "Oatmeal for breakfast"]


def test_an_entry_carries_its_day_its_people_and_its_text(client) -> None:
    entry = _feed(client)["entries"][1]
    assert entry == {
        "day": "2026-09-04",
        "day_url": "/journal/2026/09/04",
        "slug": "oatmeal-breakfast",
        "title": "Oatmeal for breakfast",
        "source": "agent",
        "people": [{"id": "alex", "name": "Alex"}],
        "markdown": "Alex had oatmeal, with the good honey.\n",
        "edit_url": "/edit/journal/2026/09/04/oatmeal-breakfast.md",
    }


def test_the_people_of_the_instance_come_with_the_feed(client) -> None:
    assert _feed(client)["people"] == [
        {"id": "alex", "name": "Alex"},
        {"id": "mia", "name": "Mia"},
    ]


def test_a_member_may_edit_and_a_guest_may_not(client) -> None:
    assert _feed(client)["can_edit"] is True
    assert _feed(client, auth=MIA)["can_edit"] is False
    assert _day(client, "2026/09/04")["can_edit"] is True
    assert _day(client, "2026/09/04", auth=MIA)["can_edit"] is False


# -- one day ----------------------------------------------------------------


def test_a_day_holds_its_entries_the_nightly_page_first(client) -> None:
    body = _day(client, "2026/09/04")
    assert body["day"] == "2026-09-04"
    assert _titles(body) == ["The day", "Oatmeal for breakfast"]
    assert [entry["source"] for entry in body["entries"]] == ["nightly", "agent"]


def test_a_day_with_no_entry_answers_the_day_and_nothing_else(client) -> None:
    body = _day(client, "2026/09/01")
    assert body["day"] == "2026-09-01"
    assert body["entries"] == []
    assert body["older"] == {"day": "2026-08-20", "url": "/journal/2026/08/20"}
    assert body["newer"] == {"day": "2026-09-04", "url": "/journal/2026/09/04"}


def test_the_newest_day_has_no_newer_and_the_oldest_no_older(client) -> None:
    newest = _day(client, "2026/09/04")
    assert newest["older"] == {"day": "2026-08-20", "url": "/journal/2026/08/20"}
    assert newest["newer"] is None
    oldest = _day(client, "2026/08/20")
    assert oldest["older"] is None
    assert oldest["newer"] == {"day": "2026-09-04", "url": "/journal/2026/09/04"}


def test_a_date_that_is_not_a_date_is_404(client) -> None:
    response = client.get("/api/v1/journal/2026/02/31", auth=ALEX)
    assert response.status_code == 404
    assert response.json() == {"error": "not found"}
    assert client.get("/api/v1/journal/2026/13/01", auth=ALEX).status_code == 404


def test_the_answers_are_never_cached(client) -> None:
    for path in ("/api/v1/journal", "/api/v1/journal/2026/09/04"):
        response = client.get(path, auth=ALEX)
        assert response.headers["cache-control"] == "no-store", path
