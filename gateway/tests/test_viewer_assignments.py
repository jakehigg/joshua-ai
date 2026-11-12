"""Offline tests for the assignments manager client and the data it builds.

A MockTransport stands in for the assignments manager, so no network is
needed. The answers follow the read API contract of the manager.

Every page of the section is the React app: ``test_viewer_api_assignments.py``
covers the JSON routes of the list and of one assignment, and
``test_viewer_api_saved.py`` those of the saved assignments. What stays here
is the client, the strings a person reads, and the definition as units.
"""

from __future__ import annotations

import textwrap
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from joshua_gateway import viewer
from joshua_gateway import viewer_assignments as am
from joshua_gateway import viewer_comments as vc
from joshua_shared import config
from starlette.testclient import TestClient

ALEX = ("alex", "alex-secret")
NOW = datetime(2026, 11, 9, 15, 0, tzinfo=UTC)

CONFIG = textwrap.dedent("""\
    name: Test
    timezone: UTC
    people:
      - id: alex
        name: Alex
    viewer:
      enabled: true
      users:
        alex: ${VIEWER_PW_ALEX:-}
    """)


def _iso(delta: timedelta) -> str:
    return (NOW - delta).isoformat()


def item(**fields: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": 7,
        "title": "Clean up the docs",
        "status": "running",
        "state": "in_progress",
        "model": "sonnet",
        "created_at": _iso(timedelta(hours=3)),
        "started_at": _iso(timedelta(hours=2)),
        "finished_at": None,
        "working_s": 4800,
        "usage": {"input": 10, "output": 20, "cache_read": 30, "cache_write": 40, "total": 12345},
        "open_question": None,
        "asked_at": None,
        "stopped_at_limits": None,
        "saved": None,
    }
    return base | fields


@pytest.fixture
def data(monkeypatch, tmp_path) -> None:
    root = tmp_path / "data"
    (root / "wiki").mkdir(parents=True)
    cfg_path = tmp_path / "joshua.yaml"
    cfg_path.write_text(CONFIG)
    monkeypatch.setenv(config.CONFIG_ENV_VAR, str(cfg_path))
    monkeypatch.setenv("JOSHUA_DATA_DIR", str(root))
    monkeypatch.setenv("VIEWER_PW_ALEX", ALEX[1])
    monkeypatch.delenv(am.ASSIGNMENTS_URL_ENV, raising=False)
    monkeypatch.delenv(am.VIEWER_TOKEN_ENV, raising=False)
    monkeypatch.setattr(config, "_cache", None)
    monkeypatch.setattr(config, "_cache_path", None)
    monkeypatch.setattr(config, "_cache_people_file_mtime", None)
    monkeypatch.setattr(viewer, "_now", lambda tz: NOW.astimezone(tz))


# -- the client ---------------------------------------------------------------


async def test_a_network_error_is_none() -> None:
    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    client = am.AssignmentsClient("http://assignments", "t", transport=httpx.MockTransport(fail))
    assert await client.list("alex", "draft") is None
    assert await client.detail("alex", 1) is None


async def test_a_body_that_is_not_json_is_none() -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(200, text="<html>"))
    client = am.AssignmentsClient("http://assignments", "t", transport=transport)
    assert await client.saved("alex") is None


async def test_a_row_of_another_person_is_not_found() -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(404, json={"error": "no"}))
    client = am.AssignmentsClient("http://assignments", "t", transport=transport)
    assert await client.saved_detail("alex", 3) is am.NOT_FOUND
    assert await client.detail("alex", 7) is am.NOT_FOUND
    # A list never names a row, so a 404 of the manager is no list.
    assert await client.saved("alex") is None


def test_from_env_needs_both_values() -> None:
    assert am.from_env({}) is None
    assert am.from_env({am.ASSIGNMENTS_URL_ENV: "http://assignments"}) is None
    assert am.from_env({am.VIEWER_TOKEN_ENV: "t"}) is None
    client = am.from_env({am.ASSIGNMENTS_URL_ENV: "http://assignments", am.VIEWER_TOKEN_ENV: "t"})
    assert isinstance(client, am.AssignmentsClient)


def test_readyz_reports_the_assignments(data, monkeypatch) -> None:
    client = TestClient(viewer.build_app())
    assert client.get("/readyz").json()["assignments"] is False
    monkeypatch.setenv(am.ASSIGNMENTS_URL_ENV, "http://assignments")
    monkeypatch.setenv(am.VIEWER_TOKEN_ENV, "t")
    assert client.get("/readyz").json()["assignments"] is True


# -- the small parts ---------------------------------------------------------


@pytest.mark.parametrize(
    ("seconds", "text"),
    [
        (-30, "just now"),
        (59, "just now"),
        (60 * 5, "5m ago"),
        (3600 * 2, "2h ago"),
        (3600 * 47, "47h ago"),
        (86400 * 3, "3d ago"),
        (86400 * 90, "3mo ago"),
    ],
)
def test_ago(seconds, text) -> None:
    assert vc.ago(seconds) == text


def test_duration() -> None:
    assert am.duration(-1) == "0s"
    assert am.duration(45) == "45s"
    assert am.duration(12 * 60) == "12m"
    assert am.duration(80 * 60) == "1h 20m"


def test_parse_state() -> None:
    assert am.parse_state("draft") == "draft"
    assert am.parse_state("queued") is None
    assert am.parse_state(None) is None
    assert am.parse_state("running") is None


def test_the_states_are_drafts_in_progress_and_completed() -> None:
    assert [label for _, label in am.STATES] == ["Drafts", "In progress", "Completed"]
    assert am.empty_text("draft") == "No draft."
    assert am.empty_text(None) == "No assignment here."


def test_a_status_of_the_store_reads_as_words() -> None:
    assert am.status_label("paused_limits") == "paused at limits"
    assert am.status_label("odd_<x>") == "odd <x>"


def test_a_stop_on_unknown_limits_says_so() -> None:
    text = am.limits_text({"reason": "unknown"}, tz=UTC, now=NOW)
    assert text == "Stopped because the subscription limits are not known."


def test_a_limits_stop_reads_the_window_and_the_reset(data) -> None:
    stop = {
        "window": "five_hour",
        "used_pct": 91.5,
        "ceiling": 90,
        "resets_at": (NOW + timedelta(hours=2)).isoformat(),
        "next_step": "fix the links",
    }
    text = am.limits_text(stop, tz=UTC, now=NOW)
    assert "Stopped at the 5-hour limit: 91.5% used, the ceiling is 90%." in text
    assert "The window resets 5:00 pm today." in text
    assert "Next step: fix the links" in text


def test_run_number_needs_two_runs() -> None:
    def run(number: int) -> dict[str, Any]:
        return {"id": number, "title": "T", "status": "done", "state": "completed"}

    one = am.Detail.model_validate(item() | {"lineage": [run(7)]})
    assert am.run_number(one) is None
    two = am.Detail.model_validate(item() | {"lineage": [run(4), run(7)]})
    assert am.run_number(two) == (2, 4)


def test_start_phrase_names_the_draft() -> None:
    assert am.start_phrase(9) == "Tell Joshua: start assignment 9"


def test_summary_line_takes_the_lede_then_the_next_step() -> None:
    assert am.summary_line(am.Item.model_validate(item(lede="  Done.  "))) == "Done."
    assert am.summary_line(am.Item.model_validate(item(next_step="fix"))) == "Next: fix"
    assert am.summary_line(am.Item.model_validate(item())) == ""


def test_text_lines_counts_the_lines_of_a_text() -> None:
    assert am.text_lines(None) == []
    assert am.text_lines("a\r\nb") == ["a", "b"]


# -- the definition as units ---------------------------------------------------

# The definition that the manager renders for {"title": "Clean up the docs",
# "goal": <the goal below>, "model": "sonnet", "budget": {"turns": 40},
# "repos": ["example/docs"], "dry_run": False}.
FULL_DEFINITION = (
    "title: Clean up the docs\ngoal: |-\n  # Clean up\n\n  Read each page\n"
    "  and fix the links.\n\n  - flat one\n  - parent\n    - nested one\n"
    "    - nested two\n  - last\n\n  1. first\n  2. second\n\n  > a quote\n\n"
    "  ```sh\n  make test\n  ```\n\n  | a | b |\n  |---|---|\n  | 1 | 2 |\n"
    "model: sonnet\nbudget:\n  turns: 40\nrepos:\n  - example/docs\ndry_run: false"
)


def _units(text: str) -> dict[int, am.Unit]:
    props, goal = am.definition_units(text)
    return {unit.first: unit for unit in [*props, *goal]}


def test_each_block_of_the_goal_is_a_unit_at_its_first_line() -> None:
    units = _units(FULL_DEFINITION)
    expected = {
        2: ("goal", 0),
        3: ("h", 0),
        5: ("p", 0),
        8: ("li", 0),
        9: ("li", 0),
        10: ("li", 1),
        11: ("li", 1),
        12: ("li", 0),
        14: ("li", 0),
        15: ("li", 0),
        17: ("quote", 0),
        19: ("code", 0),
        23: ("table", 0),
    }
    assert {line: (u.kind, u.depth) for line, u in units.items() if u.kind != "prop"} == expected
    # A unit says what its "+" names, and a nested item is a unit of its own.
    assert units[5].what == "the paragraph at line 5"
    assert units[10].markdown == "- nested one" and units[10].what == "the list item at line 10"
    assert units[19].markdown == "```sh\nmake test\n```"
    assert units[2].what == "the goal" and units[2].last == 2


def test_the_fields_of_a_definition_are_properties() -> None:
    props, goal = am.definition_units(FULL_DEFINITION)
    assert [unit.first for unit in props] == [1, 26, 27, 29, 31]
    assert [unit.label for unit in props] == ["title", "model", "budget", "repos", "dry run"]
    assert props[0].value == {"kind": "text", "text": "Clean up the docs"}
    assert props[2].value == {"kind": "tags", "tags": ["turns: 40"]}
    assert props[2].last == 28
    assert props[3].value == {"kind": "tags", "tags": ["example/docs"]}
    assert props[4].value == {"kind": "text", "text": "no"}
    assert props[0].what == "the field title"
    assert goal[0].kind == "goal"


def test_a_definition_with_no_block_scalar_goal_is_only_properties() -> None:
    props, goal = am.definition_units("goal: Clean up the docs.\ntools:\n  - a\n  - b")
    assert goal == []
    assert [unit.label for unit in props] == ["goal", "tools"]
    assert props[1].value == {"kind": "tags", "tags": ["a", "b"]}


def test_odd_values_of_a_definition() -> None:
    text = (
        'goal: ""\nnotes: null\nmeta: {}\nlist: []\nbad: [x\nmax_turns: 40\n'
        'repos:\n  - null\n  - {"a": 1}'
    )
    units = _units(text)
    for line in (1, 2, 3, 4):
        assert units[line].value == {"kind": "none"}, line
    assert units[5].value == {"kind": "code", "text": "bad: [x"}
    assert units[6].label == "max turns"
    assert units[7].value == {"kind": "tags", "tags": ["none", '{"a": 1}']}


def test_an_empty_definition_has_no_unit() -> None:
    assert am.definition_units("") == ([], [])
    assert am.text_lines("") == []


def test_the_definition_fits_a_phone() -> None:
    """No part of the definition is wider than a phone: the property rows go to
    one column, the text wraps, and a code block or a table scrolls in its own
    box."""
    css = (Path(viewer.__file__).parent / "viewer.css").read_text()
    ui = (Path(viewer.__file__).parent / "ui" / "joshua-ui.css").read_text()
    phone = css[css.index("@media (max-width: 560px)") :]
    phone = phone[: phone.index("\n}\n")]
    assert ".a-prop > .a-unit-body { grid-template-columns: minmax(0, 1fr);" in phone
    rule = css[css.index(".a-unit {") :]
    rule = rule[: rule.index("}")]
    assert "min-width: 0" in rule and "overflow-wrap: anywhere" in rule
    assert "minmax(0, 1fr)" in css[css.index(".a-prop > .a-unit-body {") :][:200]
    assert ".a-tags { display: flex; flex-wrap: wrap;" in css
    assert "overflow-x: auto" in ui[ui.index(".j-prose pre {") :][:300]
    assert "overflow-x: auto" in ui[ui.index(".j-prose table {") :][:200]
