"""The ``documents`` source: what it indexes, what it skips, who finds a
document, and how recall, injection, and the search route show one.

Every test is offline. A fake store stands in for pgvector, and embedding is a
constant vector.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from joshua_core.engine import injection
from joshua_core.engine.tools import ToolDeps
from joshua_core.engine.tools.recall import do_search_memory
from joshua_core.memory import embed as embed_module
from joshua_core.memory.indexer import Indexer
from joshua_core.memory.models import KbChunk
from joshua_core.memory.search import document_reader, document_visible, search
from joshua_core.memory.sources.documents import (
    SUMMARY_CHARS,
    DocumentsSource,
    document_pointer,
    first_paragraph,
    viewer_path,
)
from joshua_core.search_api import SEARCH_PATH, build_search_router
from joshua_core.store.models import Channel, Conversation
from joshua_shared.config import Memory, MemoryDocuments, MemorySearch, Person
from starlette.testclient import TestClient

AT = datetime(2026, 3, 14, 15, 0, tzinfo=UTC)
PEOPLE = [
    Person(id="alex", name="Alex"),
    Person(id="mia", name="Mia"),
    Person(id="sam", name="Sam", role="guest"),
    Person(id="robin", name="Robin", role="owner"),
]
SECRET_BODY = "The body sentence that must never reach the index."


@pytest.fixture(autouse=True)
def _const_embed(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    seen: list[str] = []

    def fake(text: str, model: str = "") -> list[float]:
        seen.append(text)
        return [0.1] * 384

    monkeypatch.setattr(embed_module, "embed", fake)
    return seen


def _write(root: Path, rel: str, text: str) -> Path:
    path = root / "documents" / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    os.utime(path, (AT.timestamp(), AT.timestamp()))
    return path


def _doc(title: str, *, extra: str = "", body: str = SECRET_BODY) -> str:
    return f"---\ntitle: {title}\n{extra}---\n\n{body}\n"


async def _list(source: DocumentsSource) -> dict[str, Any]:
    return {d.uri: d async for d in source.list_documents()}


# ── the source ───────────────────────────────────────────────────────────────


async def test_no_documents_root_yields_nothing(tmp_path: Path) -> None:
    source = DocumentsSource(tmp_path, members=["alex"])
    assert await _list(source) == {}
    assert await source.fetch("documents/alex/plan.md") is None


async def test_a_document_is_its_title_kind_status_and_summary(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "alex/party.md",
        _doc(
            "Party plan",
            extra='kind: plan\nstatus: active\nsummary: "Food, music, and the guest list."\n',
        ),
    )
    docs = await _list(DocumentsSource(tmp_path, members=["alex"]))
    doc = docs["documents/alex/party.md"]
    assert doc.source == "documents"
    assert doc.person_id == "alex"
    assert doc.title == "Party plan"
    assert doc.text == "Food, music, and the guest list."
    assert dict(doc.frontmatter) == {"kind": "plan", "status": "active"}
    assert SECRET_BODY not in doc.text


async def test_with_no_summary_the_first_paragraph_is_used(tmp_path: Path) -> None:
    body = "# Heading\n\nFirst paragraph\nover two lines.\n\nSecond paragraph."
    _write(tmp_path, "shared/meals.md", _doc("Meals", body=body))
    doc = (await _list(DocumentsSource(tmp_path, members=[])))["documents/shared/meals.md"]
    assert doc.person_id is None
    assert doc.text == "First paragraph over two lines."
    # The defaults of the conventions.
    assert dict(doc.frontmatter) == {"kind": "note", "status": "draft"}


async def test_first_paragraph_skips_headings_and_long_summaries_are_cut(
    tmp_path: Path,
) -> None:
    assert first_paragraph("## Only a heading\n\nText.") == "Text."
    assert first_paragraph("") == ""
    _write(tmp_path, "alex/long.md", _doc("Long", body="word " * 400))
    doc = (await _list(DocumentsSource(tmp_path, members=["alex"])))["documents/alex/long.md"]
    assert len(doc.text) <= SUMMARY_CHARS + 1


async def test_title_falls_back_to_heading_then_slug(tmp_path: Path) -> None:
    _write(tmp_path, "alex/a.md", "# From heading\n\nText.")
    _write(tmp_path, "alex/b-slug.md", "Text only.")
    docs = await _list(DocumentsSource(tmp_path, members=["alex"]))
    assert docs["documents/alex/a.md"].title == "From heading"
    assert docs["documents/alex/b-slug.md"].title == "b-slug"


async def test_an_unknown_kind_or_status_takes_the_default(tmp_path: Path) -> None:
    _write(tmp_path, "alex/x.md", _doc("X", extra="kind: poem\nstatus: Final\n"))
    doc = (await _list(DocumentsSource(tmp_path, members=["alex"])))["documents/alex/x.md"]
    assert dict(doc.frontmatter) == {"kind": "note", "status": "final"}


async def test_archived_html_and_other_files_are_skipped(tmp_path: Path) -> None:
    _write(tmp_path, "alex/old.md", _doc("Old", extra="status: archived\n"))
    _write(tmp_path, "alex/mock.md", _doc("Mock", extra="format: html\n"))
    _write(tmp_path, "alex/page.html", "<h1>Page</h1>")
    _write(tmp_path, "alex/party.comments.jsonl", "{}\n")
    _write(tmp_path, ".trash/20260314/alex/gone.md", _doc("Gone"))
    _write(tmp_path, "alex/.hidden/note.md", _doc("Hidden"))
    _write(tmp_path, "nobody/plan.md", _doc("Not a member"))
    _write(tmp_path, "sam/plan.md", _doc("A guest has no documents"))
    _write(tmp_path, "alex/kept.md", _doc("Kept"))
    source = DocumentsSource(tmp_path, members=["alex"])
    assert set(await _list(source)) == {"documents/alex/kept.md"}
    assert await source.fetch("documents/alex/old.md") is None
    assert await source.fetch("documents/alex/mock.md") is None
    assert await source.fetch("documents/alex/page.html") is None
    assert await source.fetch("documents/nobody/plan.md") is None
    assert await source.fetch("documents/alex/../alex/kept.md") is None
    assert await source.fetch("wiki/kept.md") is None
    fetched = await source.fetch("documents/alex/kept.md")
    assert fetched is not None and fetched.title == "Kept"


def test_the_viewer_path_of_a_document() -> None:
    assert viewer_path("documents/alex/party.md") == "/documents/alex/party"
    assert viewer_path("documents/alex/../mia/x.md") is None
    assert viewer_path("documents/x.md") is None
    assert viewer_path("wiki/x.md") is None


# ── the indexer ──────────────────────────────────────────────────────────────


class RecordingStore:
    def __init__(self) -> None:
        self.rows: dict[tuple[str | None, str], dict[str, Any]] = {}

    async def kb_index_state(self, source: str) -> dict[tuple[str | None, str], str]:
        return {key: row["rev"] for key, row in self.rows.items()}

    async def kb_replace_item(self, **kwargs: Any) -> int:
        chunks = list(kwargs["chunks"])
        self.rows[(kwargs["person_id"], kwargs["path"])] = {
            "rev": kwargs["file_sha256"],
            "kind": kwargs["kind"],
            "title": kwargs["title"],
            "chunks": [(heading, text) for heading, text, _ in chunks],
        }
        return len(chunks)

    async def kb_delete_item(self, source: str, person_id: str | None, path: str) -> int:
        self.rows.pop((person_id, path), None)
        return 1

    async def kb_purge_sources(self, keep: list[str]) -> int:
        return 0


async def test_one_row_per_document_and_never_the_body(tmp_path: Path, _const_embed) -> None:
    long_body = "Intro paragraph.\n\n" + "\n\n".join(
        f"## Part {i}\n\n{SECRET_BODY}" for i in range(9)
    )
    _write(tmp_path, "alex/plan.md", _doc("Plan", extra="kind: plan\n", body=long_body))
    store = RecordingStore()
    indexer = Indexer(
        store,  # type: ignore[arg-type]
        {"documents": DocumentsSource(tmp_path, members=["alex"])},
        embed_model="m",
        chunk_chars=100,
    )
    stats = (await indexer.reindex())["documents"]
    assert stats["indexed"] == 1
    row = store.rows[("alex", "documents/alex/plan.md")]
    assert row["kind"] == "document"
    assert row["title"] == "Plan"
    assert row["chunks"] == [("plan, draft", "Intro paragraph.")]
    assert all(SECRET_BODY not in text for text in _const_embed)
    assert _const_embed[-1] == "Plan\nplan, draft\nIntro paragraph."

    # Archive it: the next pass removes the row.
    _write(tmp_path, "alex/plan.md", _doc("Plan", extra="status: archived\n"))
    stats = (await indexer.reindex(source="documents"))["documents"]
    assert stats["removed"] == 1
    assert store.rows == {}


# ── who finds a document ─────────────────────────────────────────────────────


def _document(owner: str | None, slug: str = "plan", sim: float = 0.9) -> KbChunk:
    folder = owner or "shared"
    return KbChunk(
        person_id=owner,
        source="documents",
        kind="document",
        path=f"documents/{folder}/{slug}.md",
        title=f"{folder} {slug}",
        heading="plan, active",
        text="The summary.",
        similarity=sim,
    )


def _wiki(sim: float = 0.8) -> KbChunk:
    return KbChunk(
        source="files",
        kind="wiki",
        path="wiki/garden.md",
        title="Garden",
        heading="",
        text="Tomatoes.",
        similarity=sim,
    )


class FakeStore:
    """Returns every row it holds, whatever the filter, and records the call,
    so a test proves the code filters again."""

    def __init__(self, rows: list[KbChunk]) -> None:
        self.rows = rows
        self.calls: list[dict[str, Any]] = []

    async def kb_search(
        self, person_id, embedding, *, k, min_sim, sources=None, kinds=None, exclude_kinds=None
    ):
        self.calls.append(
            {"person_id": person_id, "sources": sources, "kinds": kinds, "exclude": exclude_kinds}
        )
        return list(self.rows)

    async def snapshot_times(self, names):  # type: ignore[no-untyped-def]
        return {}


def test_the_reader_is_a_member_only() -> None:
    assert document_reader(PEOPLE, "alex") == "alex"
    assert document_reader(PEOPLE, "robin") == "robin"  # an owner reads as a member
    assert document_reader(PEOPLE, "sam") is None  # a guest
    assert document_reader(PEOPLE, "nobody") is None
    assert document_reader(PEOPLE, None) is None  # a group conversation


def test_document_visible() -> None:
    assert document_visible(_wiki(), None)
    assert document_visible(_document("alex"), "alex")
    assert document_visible(_document(None), "alex")
    assert not document_visible(_document("mia"), "alex")
    assert not document_visible(_document(None), None)


async def test_a_search_with_no_reader_excludes_documents() -> None:
    store = FakeStore([_document("alex"), _document(None), _wiki()])
    rows = await search(store, "alex", [0.1], k=5, min_sim=0.5)  # type: ignore[arg-type]
    assert [r.path for r in rows] == ["wiki/garden.md"]
    assert store.calls[0]["exclude"] == ("document", "workspace")


async def test_a_reader_finds_own_and_shared_but_not_another_member() -> None:
    rows = [_document("alex"), _document("mia", sim=0.95), _document(None), _wiki()]
    store = FakeStore(rows)
    found = await search(store, "alex", [0.1], k=5, min_sim=0.5, document_reader="alex")  # type: ignore[arg-type]
    paths = {r.path for r in found}
    assert paths == {"documents/alex/plan.md", "documents/shared/plan.md", "wiki/garden.md"}
    assert store.calls[0]["person_id"] == "alex"


# ── recall and injection show a pointer ──────────────────────────────────────


def test_the_pointer_line() -> None:
    assert (
        document_pointer(_document("alex"))
        == "document: alex plan (documents/alex/plan.md, plan, active)"
    )


def _deps(person_id: str | None) -> ToolDeps:
    conv = Conversation(id="c1", channel_id="telegram:1", person_id=person_id)
    channel = Channel(id="telegram:1", channel_type="telegram")
    return ToolDeps(repo=None, conversation=conv, channel=channel, tz="UTC", person_id=person_id)


async def _recall(person_id: str | None, store: FakeStore) -> list[dict[str, Any]]:
    return await do_search_memory(
        _deps(person_id),
        store=store,  # type: ignore[arg-type]
        embed_model="m",
        min_sim=0.5,
        query="party",
        full=True,
        people=PEOPLE,
    )


async def test_recall_renders_a_document_as_one_line() -> None:
    store = FakeStore([_document("alex"), _document("mia"), _wiki()])
    results = await _recall("alex", store)
    doc = next(r for r in results if r["path"].startswith("documents/"))
    assert doc == {
        "title": "alex plan",
        "path": "documents/alex/plan.md",
        "text": "document: alex plan (documents/alex/plan.md, plan, active)",
        "score": 0.9,
    }
    assert not any(r["path"] == "documents/mia/plan.md" for r in results)


@pytest.mark.parametrize("person", ["sam", None, "nobody"])
async def test_recall_shows_no_document_to_a_guest_or_a_group(person: str | None) -> None:
    store = FakeStore([_document("alex"), _document(None), _wiki()])
    results = await _recall(person, store)
    assert [r["path"] for r in results] == ["wiki/garden.md"]


def _ctx(person_id: str | None, role: str) -> SimpleNamespace:
    return SimpleNamespace(
        text="what is the plan for the party",
        person_id=person_id,
        role=role,
        conversation=SimpleNamespace(id="c1", person_id=person_id),
        turn_id="t1",
    )


class Repo:
    async def add_kb_event(self, **kwargs: Any) -> None:
        return None


async def _inject(ctx: SimpleNamespace, rows: list[KbChunk], full_sim: float) -> str | None:
    return await injection.kb_context(
        ctx,
        store=FakeStore(rows),  # type: ignore[arg-type]
        repo=Repo(),  # type: ignore[arg-type]
        embed_model="m",
        full_sim=full_sim,
        hint_sim=0.5,
        top_k=3,
        max_chars=500,
        recency_bonus=0.0,
        recency_half_life_days=14.0,
        per_doc_cap=3,
        people=PEOPLE,
    )


async def test_injection_full_note_shows_the_pointer_not_the_text() -> None:
    note = await _inject(_ctx("alex", "member"), [_document("alex")], full_sim=0.8)
    assert note is not None
    assert note.startswith(injection.FULL_HEADER)
    assert (
        "document: alex plan (documents/alex/plan.md, plan, active), open with read_document"
        in note
    )
    assert "The summary." not in note


async def test_injection_hint_note_shows_the_pointer() -> None:
    note = await _inject(_ctx("alex", "member"), [_document("alex")], full_sim=0.99)
    assert note is not None
    assert "document: alex plan (documents/alex/plan.md, plan, active)" in note


async def test_injection_shows_a_document_to_an_owner() -> None:
    """An owner reads documents as a member does, so the injection names one."""
    note = await _inject(_ctx("robin", "owner"), [_document("robin")], full_sim=0.8)
    assert note is not None
    assert "document: robin plan (documents/robin/plan.md, plan, active)" in note


async def test_injection_shows_no_document_to_a_guest() -> None:
    note = await _inject(_ctx("sam", "guest"), [_document(None)], full_sim=0.8)
    assert note is None


# ── the search route ─────────────────────────────────────────────────────────


def _client(monkeypatch: pytest.MonkeyPatch, rows: list[KbChunk], *, enabled: bool = True):
    monkeypatch.setenv("JOSHUA_TOKEN_VIEWER", "viewer-token")
    store = FakeStore(rows)
    settings = SimpleNamespace(
        memory=Memory(
            search=MemorySearch(allowed_callers=["viewer"]),
            documents=MemoryDocuments(enabled=enabled),
        ),
        people=PEOPLE,
    )
    app = FastAPI()
    app.state.ctx = SimpleNamespace(settings=settings, memory=store)
    app.include_router(build_search_router())
    client = TestClient(app)
    client.store = store  # type: ignore[attr-defined]
    return client


_HEADERS = {"Authorization": "Bearer viewer-token"}
_ROWS = [_document("alex"), _document("mia"), _document(None), _wiki()]


def test_the_route_returns_own_and_shared_documents(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(monkeypatch, _ROWS)
    body = {"query": "party", "source": "documents", "person": "alex"}
    response = client.post(SEARCH_PATH, json=body, headers=_HEADERS)
    assert response.status_code == 200
    hits = response.json()["results"]
    assert {h["path"] for h in hits} == {"documents/alex/plan.md", "documents/shared/plan.md"}
    assert all(h["kind"] == "document" and h["heading"] == "plan, active" for h in hits)
    call = client.store.calls[0]
    assert call["sources"] == ("documents",) and call["kinds"] == ("document",)
    assert call["person_id"] == "alex"


def test_the_route_needs_a_person_for_documents(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(monkeypatch, _ROWS)
    response = client.post(
        SEARCH_PATH, json={"query": "party", "source": "documents"}, headers=_HEADERS
    )
    assert response.status_code == 400
    assert client.store.calls == []


@pytest.mark.parametrize("person", ["sam", "nobody"])
def test_the_route_gives_no_document_to_a_non_member(
    monkeypatch: pytest.MonkeyPatch, person: str
) -> None:
    client = _client(monkeypatch, _ROWS)
    body = {"query": "party", "source": "documents", "person": person}
    response = client.post(SEARCH_PATH, json=body, headers=_HEADERS)
    assert response.json() == {"results": []}
    assert client.store.calls == []


def test_the_route_gives_no_document_when_off(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(monkeypatch, _ROWS, enabled=False)
    body = {"query": "party", "source": "documents", "person": "alex"}
    assert client.post(SEARCH_PATH, json=body, headers=_HEADERS).json() == {"results": []}


def test_the_default_source_never_returns_a_document(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(monkeypatch, _ROWS)
    response = client.post(SEARCH_PATH, json={"query": "party"}, headers=_HEADERS)
    assert [h["path"] for h in response.json()["results"]] == ["wiki/garden.md"]
    assert client.store.calls[0]["exclude"] == ("document", "workspace")


def test_the_route_refuses_a_bad_source_or_person(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(monkeypatch, _ROWS)
    for body in (
        {"query": "party", "source": "memos"},
        {"query": "party", "source": "documents", "person": "../alex"},
    ):
        assert client.post(SEARCH_PATH, json=body, headers=_HEADERS).status_code == 422


def test_documents_are_on_by_default() -> None:
    assert Memory().documents.enabled is True
