"""The `snapshots` memory source: one document per item of a fresh snapshot.

The rows come from a fake `snapshot_result` reader, and the clock is fixed, so
no test reads the date of the day it runs.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from joshua_core.memory.indexer import _kind_from_uri
from joshua_core.memory.sources.snapshots import (
    SnapshotsSource,
    item_uri,
    render_item,
    split_items,
)
from joshua_shared.config import Snapshot

NOW = datetime(2026, 3, 14, 15, 0, tzinfo=UTC)
TZ = "America/New_York"


def _snap(**kw) -> Snapshot:
    base = {"name": "open-tasks", "server": "tasks", "tool": "list_tasks", "every": "5m"}
    return Snapshot(**{**base, **kw})


def _row(text: str, *, name: str = "open-tasks", age: timedelta = timedelta(0), failures=0):
    return {
        "name": name,
        "person_id": None,
        "fetched_at": NOW - age,
        "ok": failures == 0,
        "text": text,
        "sha256": "x",
        "failures": failures,
        "last_error": None,
    }


class Rows:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows

    async def list_snapshot_results(self) -> list[dict]:
        return self.rows


async def _docs(snaps: list[Snapshot], rows: list[dict]):
    source = SnapshotsSource(Rows(rows), snaps, timezone=TZ, clock=lambda: NOW)
    return [doc async for doc in source.list_documents()]


# --- item splitting ----------------------------------------------------------


def test_a_json_list_is_the_item_list() -> None:
    text = json.dumps([{"title": "Change the filter", "due": "2026-03-15"}, "Call Alex"])
    assert split_items(text, None) == ["title: Change the filter\ndue: 2026-03-15", "Call Alex"]


def test_the_items_path_finds_the_list() -> None:
    text = json.dumps({"result": {"tasks": [{"title": "A"}, {"title": "B"}]}, "count": 2})
    assert split_items(text, "result.tasks") == ["title: A", "title: B"]


def test_one_object_is_one_item() -> None:
    assert split_items(json.dumps({"title": "Backup check", "note": ""}), None) == [
        "title: Backup check"
    ]


def test_plain_text_is_one_item() -> None:
    assert split_items("Backup check\nat noon", None) == ["Backup check\nat noon"]


def test_an_empty_list_has_no_item() -> None:
    assert split_items("[]", None) == []


def test_an_object_skips_empty_values_and_keeps_nested_compact() -> None:
    rendered = render_item({"a": None, "b": [], "c": {"d": 1}, "e": "  two   words "})
    assert rendered == 'c: {"d":1}\ne: two words'


# --- documents ---------------------------------------------------------------


async def test_each_item_is_one_document_with_a_header() -> None:
    text = json.dumps([{"title": "Change the filter"}, {"title": "Backup check"}])
    docs = await _docs([_snap()], [_row(text)])
    assert [d.text for d in docs] == [
        "open-tasks as of 2026-03-14 11:00 EDT\ntitle: Change the filter",
        "open-tasks as of 2026-03-14 11:00 EDT\ntitle: Backup check",
    ]
    assert {d.title for d in docs} == {"open-tasks"}
    assert {d.source for d in docs} == {"snapshots"}
    assert docs[0].doc_date == NOW.date()


async def test_the_uri_is_stable_for_the_same_item() -> None:
    first = await _docs([_snap()], [_row(json.dumps(["A", "B"]))])
    later = await _docs([_snap()], [_row(json.dumps(["B", "C"]), age=timedelta(minutes=1))])
    assert first[1].uri == later[0].uri == item_uri("open-tasks", "B")
    assert first[1].rev == later[0].rev
    assert first[1].uri.startswith("snapshot/open-tasks/")
    assert len(first[1].uri.rsplit("/", 1)[1]) == 12


async def test_the_kind_of_a_snapshot_row_is_snapshot() -> None:
    docs = await _docs([_snap()], [_row("one item")])
    assert _kind_from_uri(docs[0].uri) == "snapshot"


async def test_a_person_scope_is_on_every_row() -> None:
    docs = await _docs([_snap(scope="alex")], [_row(json.dumps(["A", "B"]))])
    assert {d.person_id for d in docs} == {"alex"}


async def test_a_shared_scope_has_no_person() -> None:
    docs = await _docs([_snap()], [_row("A")])
    assert docs[0].person_id is None


async def test_a_stale_snapshot_lists_nothing() -> None:
    snap = _snap()  # max_age: 3 x 5m is below 15m, so 15m
    assert await _docs([snap], [_row("A", age=timedelta(minutes=14))])
    assert await _docs([snap], [_row("A", age=timedelta(minutes=16))]) == []


async def test_an_explicit_max_age_wins() -> None:
    snap = _snap(max_age="1h")
    assert await _docs([snap], [_row("A", age=timedelta(minutes=50))])


async def test_a_snapshot_that_failed_three_times_lists_nothing() -> None:
    assert await _docs([_snap()], [_row("A", failures=2)])
    assert await _docs([_snap()], [_row("A", failures=3)]) == []


async def test_a_row_with_no_text_or_no_config_lists_nothing() -> None:
    no_text = {**_row("A"), "text": None}
    assert await _docs([_snap()], [no_text]) == []
    assert await _docs([_snap()], [_row("A", name="removed")]) == []


async def test_fetch_finds_one_document() -> None:
    source = SnapshotsSource(Rows([_row("A")]), [_snap()], timezone=TZ, clock=lambda: NOW)
    doc = await source.fetch(item_uri("open-tasks", "A"))
    assert doc is not None and doc.text.endswith("\nA")
    assert await source.fetch("snapshot/open-tasks/000000000000") is None
