"""`memory.exclude_folders`: a page in an excluded wiki folder is not in the
index after a reindex, and no search returns it: not `search_memory`, not the
per-turn injection, not `POST /v1/memory/search`. The default excludes
`wiki/assignments/`, where the assignments write their plan pages."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from joshua_core.engine import injection
from joshua_core.engine.tools import recall
from joshua_core.engine.tools.recall import do_search_memory
from joshua_core.memory import embed as embed_module
from joshua_core.memory.indexer import Indexer
from joshua_core.memory.models import KbChunk
from joshua_core.memory.search import excluded_prefixes, is_excluded, search
from joshua_core.memory.sources.files import FilesSource
from joshua_core.search_api import SEARCH_PATH, build_search_router
from joshua_shared.config import ConfigError, Memory, parse
from starlette.testclient import TestClient
from test_indexer import FakeStore as IndexStore
from test_recall_tool import _deps

PLAN = "wiki/assignments/12-audit.md"
DEFAULT = Memory().exclude_folders


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _volume(root: Path) -> None:
    _write(root, "wiki/garden.md", "# Garden\n\nTomatoes need sun and water every day.\n")
    _write(root, PLAN, "# Audit\n\n## Plan\n\n1. [ ] Read the repo rules.\n")
    _write(root, "wiki/assignments/old/deep.md", "# Deep\n\nA nested plan page.\n")
    _write(root, "wiki/assignments-notes.md", "# Notes\n\nA page beside the folder.\n")
    _write(root, "wiki/notes/assignments/x.md", "# X\n\nA folder of the same name, deeper.\n")


def _chunk(path: str, kind: str = "wiki", sim: float = 0.9) -> KbChunk:
    return KbChunk(
        person_id=None,
        source="files",
        kind=kind,
        path=path,
        title=path.rsplit("/", 1)[-1],
        heading="",
        text=f"text of {path}",
        similarity=sim,
    )


ROWS = [_chunk(PLAN), _chunk("wiki/garden.md", sim=0.8)]


class SearchStore:
    """Returns every row it holds, whatever the filter, so a test proves the
    search code filters and does not trust the database."""

    def __init__(self, rows: list[KbChunk]) -> None:
        self.rows = rows

    async def kb_search(
        self, person_id, embedding, *, k, min_sim, sources=None, kinds=None, exclude_kinds=None
    ):
        return list(self.rows)


@pytest.fixture(autouse=True)
def _const_embed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(embed_module, "embed", lambda text, model="": [0.1] * 384)


# ── the config ───────────────────────────────────────────────────────────────


BASE = "name: T\ntimezone: UTC\npeople:\n  - id: alex\n    name: Alex\n"


def test_the_default_excludes_the_assignments_folder() -> None:
    assert DEFAULT == ["assignments"]
    assert excluded_prefixes(DEFAULT) == ("wiki/assignments/",)


def test_an_empty_value_excludes_nothing() -> None:
    cfg = parse(BASE + "memory:\n  exclude_folders:\n", {}, source="t")
    assert cfg.memory.exclude_folders == []


@pytest.mark.parametrize("name", ["a/b", "../x", ".git", "Upper", ""])
def test_a_folder_that_is_not_one_segment_is_refused(name: str) -> None:
    with pytest.raises(ConfigError, match="exclude_folders"):
        parse(BASE + f'memory:\n  exclude_folders:\n    - "{name}"\n', {}, source="t")


def test_is_excluded_matches_the_folder_only() -> None:
    prefixes = excluded_prefixes(["assignments"])
    assert is_excluded(PLAN, prefixes)
    assert not is_excluded("wiki/assignments-notes.md", prefixes)
    assert not is_excluded("wiki/notes/assignments/x.md", prefixes)
    assert not is_excluded(PLAN, ())


# ── the files source and the index ───────────────────────────────────────────


async def test_the_files_source_never_lists_an_excluded_page(tmp_path: Path) -> None:
    _volume(tmp_path)
    source = FilesSource(tmp_path, exclude_folders=DEFAULT)
    paths = {d.uri async for d in source.list_documents()}
    assert paths == {
        "wiki/garden.md",
        "wiki/assignments-notes.md",
        "wiki/notes/assignments/x.md",
    }
    assert await source.fetch(PLAN) is None
    assert await source.fetch("wiki/garden.md") is not None


async def test_with_no_exclusion_the_folder_is_listed(tmp_path: Path) -> None:
    _volume(tmp_path)
    paths = {d.uri async for d in FilesSource(tmp_path).list_documents()}
    assert PLAN in paths


async def test_a_reindex_removes_an_excluded_page_from_the_index(tmp_path: Path) -> None:
    _volume(tmp_path)
    store = IndexStore(state={(None, PLAN): "old-rev", (None, "wiki/garden.md"): "old-rev"})
    indexer = Indexer(
        store,  # type: ignore[arg-type]
        {"files": FilesSource(tmp_path, exclude_folders=DEFAULT)},
        embed_model="m",
        chunk_chars=1600,
    )
    await indexer.reindex()
    assert (None, PLAN) in store.deleted
    assert all(not path.startswith("wiki/assignments/") for _, path in store.replaced)
    assert (None, "wiki/garden.md") in store.replaced


# ── every search ─────────────────────────────────────────────────────────────


async def test_search_drops_an_excluded_row_that_the_index_still_holds() -> None:
    chunks = await search(
        SearchStore(ROWS),  # type: ignore[arg-type]
        None,
        [0.1] * 384,
        k=5,
        min_sim=0.5,
        exclude_folders=DEFAULT,
    )
    assert [c.path for c in chunks] == ["wiki/garden.md"]


async def test_search_memory_never_returns_an_excluded_page() -> None:
    results = await do_search_memory(
        _deps("alex"),
        store=SearchStore(ROWS),  # type: ignore[arg-type]
        embed_model="m",
        min_sim=0.5,
        query="audit plan",
        exclude_folders=DEFAULT,
    )
    assert [r["path"] for r in results] == ["wiki/garden.md"]


def test_the_recall_server_gets_the_config_folders(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}
    monkeypatch.setattr(recall, "build_recall_server", lambda deps, **kw: seen.update(kw))
    manager = SimpleNamespace(register_builtin=lambda name, factory: factory(None))
    recall.register(manager, SearchStore([]), SimpleNamespace(memory=Memory(), people=[]))  # type: ignore[arg-type]
    assert seen["exclude_folders"] == ["assignments"]


class AuditRepo:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def add_kb_event(self, **kwargs: Any) -> None:
        self.events.append(kwargs)


async def _inject(exclude: list[str]) -> tuple[str | None, AuditRepo]:
    store = SearchStore([_chunk(PLAN, sim=0.95)])
    repo = AuditRepo()
    ctx = SimpleNamespace(
        text="what is the plan of the audit",
        person_id="alex",
        conversation=SimpleNamespace(id="c1"),
        turn_id="t1",
    )
    note = await injection.kb_context(
        ctx,
        store=store,  # type: ignore[arg-type]
        repo=repo,  # type: ignore[arg-type]
        embed_model="m",
        full_sim=0.72,
        hint_sim=0.6,
        top_k=2,
        max_chars=1200,
        recency_bonus=0.08,
        recency_half_life_days=14.0,
        per_doc_cap=3,
        exclude_folders=exclude,
    )
    return note, repo


async def test_the_injection_never_returns_an_excluded_page() -> None:
    note, repo = await _inject(DEFAULT)
    assert note is None
    assert repo.events and repo.events[0]["decision"] == "none"
    assert all(PLAN not in repr(e) for e in repo.events)
    # The same row with no exclusion is injected, so the filter did the work.
    note, _ = await _inject([])
    assert note is not None


def test_the_injection_provider_gets_the_config_folders(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    async def fake_context(ctx: Any, **kw: Any) -> None:
        seen.update(kw)

    monkeypatch.setattr(injection, "kb_context", fake_context)
    providers: list[Any] = []
    manager = SimpleNamespace(add_context_provider=providers.append)
    settings = SimpleNamespace(memory=Memory(), timezone="UTC", people=[])
    injection.register(manager, SearchStore([]), settings, AuditRepo())  # type: ignore[arg-type]
    asyncio.run(providers[0](SimpleNamespace()))
    assert seen["exclude_folders"] == ["assignments"]


def test_the_search_route_never_returns_an_excluded_page(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JOSHUA_TOKEN_VIEWER", "viewer-token")
    app_settings = SimpleNamespace(memory=Memory())
    app = FastAPI()
    app.state.ctx = SimpleNamespace(settings=app_settings, memory=SearchStore(ROWS))
    app.include_router(build_search_router())
    response = TestClient(app).post(
        SEARCH_PATH,
        json={"query": "audit plan"},
        headers={"Authorization": "Bearer viewer-token"},
    )
    assert response.status_code == 200
    assert [hit["path"] for hit in response.json()["results"]] == ["wiki/garden.md"]
