"""The `snapshots` memory source: one document per item of a fresh snapshot.

The rows come from a fake `snapshot_result` reader, and the clock is fixed, so
no test reads the date of the day it runs.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from joshua_core.memory import embed as embed_module
from joshua_core.memory.indexer import Indexer, _kind_from_uri
from joshua_core.memory.sources.snapshots import (
    SnapshotsSource,
    item_body,
    item_uri,
    label,
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


def test_one_text_part_per_element_is_the_item_list() -> None:
    # An MCP server can give a list as one text part per element, and the
    # gateway joins the parts with a new line.
    parts = [
        json.dumps({"name": "Filter", "status": "low"}, indent=2),
        json.dumps({"name": "Batteries"}),
    ]
    assert split_items("\n".join(parts), None) == ["name: Filter\nstatus: low", "name: Batteries"]


def test_a_cut_last_element_is_left_out() -> None:
    text = json.dumps({"name": "Filter"}) + "\n" + json.dumps({"name": "Batteries"})[:12]
    assert split_items(text, None) == ["name: Filter"]


def test_fields_keep_only_those_keys_in_that_order() -> None:
    text = json.dumps([{"id": 7, "status": "low", "name": "Filter", "noise": "x"}])
    assert split_items(text, None, ["name", "status", "missing"]) == ["name: Filter\nstatus: low"]


def test_an_empty_list_has_no_item() -> None:
    assert split_items("[]", None) == []


def test_an_object_skips_empty_values_and_keeps_nested_compact() -> None:
    rendered = render_item({"a": None, "b": [], "c": {"d": 1}, "e": "  two   words "})
    assert rendered == 'c: {"d":1}\ne: two words'


# --- documents ---------------------------------------------------------------


async def test_each_item_is_one_document_with_a_name_line() -> None:
    text = json.dumps([{"title": "Change the filter"}, {"title": "Backup check"}])
    docs = await _docs([_snap()], [_row(text)])
    assert [d.text for d in docs] == [
        "open-tasks:\ntitle: Change the filter",
        "open-tasks:\ntitle: Backup check",
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
    assert doc is not None and doc.text == "open-tasks:\nA"
    assert await source.fetch("snapshot/open-tasks/000000000000") is None


async def test_an_unchanged_item_is_the_same_document_on_the_next_fetch() -> None:
    first = await _docs([_snap()], [_row("A", age=timedelta(minutes=10))])
    later = await _docs([_snap()], [_row("A")])
    assert [(d.uri, d.rev, d.text) for d in first] == [(d.uri, d.rev, d.text) for d in later]


async def test_an_unchanged_item_is_not_embedded_again(monkeypatch) -> None:
    """Two fetches of the same item: the second index pass embeds nothing."""
    calls = {"n": 0}

    def fake_embed(text: str, model: str = "") -> list[float]:
        calls["n"] += 1
        return [0.0] * 384

    monkeypatch.setattr(embed_module, "embed", fake_embed)
    rows = Rows([_row("A", age=timedelta(minutes=10))])
    source = SnapshotsSource(rows, [_snap()], timezone=TZ, clock=lambda: NOW)
    store = IndexStore()
    indexer = Indexer(store, {"snapshots": source}, embed_model="m", chunk_chars=1600)
    await indexer.reindex(source="snapshots")
    assert (len(store.replaced), calls["n"]) == (1, 1)
    rows.rows = [_row("A")]
    await indexer.reindex(source="snapshots")
    assert (len(store.replaced), calls["n"]) == (1, 1)
    assert store.deleted == []


def test_label_and_item_body() -> None:
    assert label("open-tasks", NOW, TZ) == "open-tasks as of 2026-03-14 11:00 EDT"
    assert item_body("open-tasks:\ntitle: A") == "title: A"
    assert item_body("no name line") == "no name line"


class IndexStore:
    """The index state of the `snapshots` source, in memory."""

    def __init__(self) -> None:
        self.state: dict = {}
        self.replaced: list = []
        self.deleted: list = []

    async def kb_purge_sources(self, keep):
        return 0

    async def kb_index_state(self, source):
        return dict(self.state)

    async def kb_replace_item(self, *, source, person_id, path, file_sha256, chunks, **kw):
        self.replaced.append(path)
        self.state[(person_id, path)] = file_sha256
        return len(list(chunks))

    async def kb_delete_item(self, source, person_id, path):
        self.deleted.append(path)
        self.state.pop((person_id, path), None)
        return 1
