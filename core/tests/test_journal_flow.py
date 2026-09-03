"""Integration tests for on-demand journaling against a real pgvector database.

The stub backend writes a journal entry when a turn carries the ``[[journal]]``
marker (standing in for the SDK agent's ``write_journal_entry`` call). These
tests prove the whole flow: a chat turn writes an entry under today's journal
folder that cites its attachment, the indexer picks it up, and a later turn
recalls it through injection. They also check the group-turn routing and the
``journal: off`` preference.

Embedding is monkeypatched to a fixed non-zero vector, so every indexed row
scores cosine 1.0 and every document is shared scope, so the same seam the
injection integration tests use proves the entry is reachable regardless of
who asks.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from engine_fakes import FakeComposer, fake_derive_profile, make_channel
from joshua_core.engine.injection import kb_context
from joshua_core.engine.manager import ConversationManager
from joshua_core.engine.types import Attachment
from joshua_core.memory import embed as embed_module
from joshua_core.memory.indexer import Indexer
from joshua_core.memory.sources.files import FilesSource
from joshua_core.memory.store import MemoryStore
from joshua_shared import config as config_module
from joshua_shared import layout

pytestmark = pytest.mark.integration

VEC = [1.0] + [0.0] * 383


@pytest.fixture
def const_embed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(embed_module, "embed", lambda text, model="": list(VEC))


def _settings(*, journal: dict[str, str] | None = None) -> Any:
    journal = journal or {}
    people = "".join(
        f"  - id: {pid}\n    name: {pid.capitalize()}\n    journal: {journal.get(pid, 'auto')}\n"
        for pid in ("alex", "sam")
    )
    text = "name: Test House\ntimezone: America/New_York\npeople:\n" + people
    return config_module.parse(text, env={}, source="<test>")


def _manager(repo, settings, tmp_path: Path) -> ConversationManager:
    return ConversationManager(
        repo=repo,
        settings=settings,
        composer=FakeComposer(),
        derive_profile=fake_derive_profile,
        agent_backend="stub",
        data_dir=tmp_path,
    )


async def _index(pool, root: Path) -> MemoryStore:
    store = MemoryStore(pool)
    indexer = Indexer(store, {"files": FilesSource(root)}, embed_model="m", chunk_chars=1600)
    await indexer.reindex()
    return store


async def _recall(ctx: Any, store: MemoryStore, repo) -> str | None:
    return await kb_context(
        ctx,
        store=store,
        repo=repo,
        embed_model="m",
        full_sim=0.72,
        hint_sim=0.60,
        top_k=2,
        max_chars=1200,
        recency_bonus=0.08,
        recency_half_life_days=14.0,
        per_doc_cap=3,
    )


def _today_entries(tmp_path: Path) -> list[Path]:
    return list(layout.journal_day_dir(date.today(), tmp_path).glob("*-journal.md"))


async def test_journal_entry_is_written_indexed_and_recalled(
    db, repo, tmp_path: Path, const_embed
) -> None:
    await repo.upsert_person("alex", "Alex")
    await repo.upsert_channel("telegram:alex", "telegram", default_person_id="alex")
    conv = await repo.get_or_create_conversation("telegram:alex", "alex")
    manager = _manager(repo, _settings(), tmp_path)

    att = Attachment(
        path="attachments/2026/08/2026-08-27-143210-IMG_4471.jpg",
        mime="image/jpeg",
        name="2026-08-27-143210-IMG_4471.jpg",
        original_name="garden.jpg",
    )
    await manager.run_turn(
        make_channel("telegram:alex"),
        conv,
        "here is my garden, I started using a new fertilizer today [[journal]]",
        attachments=[att],
    )

    entries = _today_entries(tmp_path)
    assert len(entries) == 1
    body = entries[0].read_text()
    assert "attachments/2026/08/2026-08-27-143210-IMG_4471.jpg" in body
    assert "people: [alex]" in body

    store = await _index(db.pool, tmp_path)
    ctx = SimpleNamespace(
        text="what fertilizer did I start using",
        person_id="alex",
        conversation=SimpleNamespace(id=conv.id),
        turn_id="t-recall",
    )
    note = await _recall(ctx, store, repo)
    assert note is not None
    events = await repo.kb_events(1)
    uris = {r["uri"] for r in events[0]["results"]}
    assert any(u.startswith("wiki/journal/") and u.endswith("-journal.md") for u in uris)


async def test_journal_off_writes_no_entry(db, repo, tmp_path: Path) -> None:
    await repo.upsert_person("sam", "Sam")
    await repo.upsert_channel("telegram:sam", "telegram", default_person_id="sam")
    conv = await repo.get_or_create_conversation("telegram:sam", "sam")
    manager = _manager(repo, _settings(journal={"sam": "off"}), tmp_path)

    await manager.run_turn(
        make_channel("telegram:sam"),
        conv,
        "I painted the fence today [[journal]]",
    )

    session = manager._pool[conv.id].session
    assert session.journal_writes == 0
    assert _today_entries(tmp_path) == []


async def test_group_turn_journals_naming_the_speaker(db, repo, tmp_path: Path) -> None:
    await repo.upsert_person("alex", "Alex")
    await repo.upsert_channel(
        "telegram:everyone", "telegram", default_person_id=None, session_mode="shared"
    )
    conv = await repo.get_or_create_conversation("telegram:everyone", None)
    manager = _manager(repo, _settings(), tmp_path)

    await manager.run_turn(
        make_channel("telegram:everyone", session_mode="shared"),
        conv,
        "I ran my first 10k this morning [[journal]]",
        speaker="Alex",
        person_id="alex",
    )

    entries = _today_entries(tmp_path)
    assert len(entries) == 1
    assert "people: [alex]" in entries[0].read_text()
    # A group turn never writes to `shared/`.
    assert not list((tmp_path / "shared").glob("*.md"))


async def test_group_turn_without_speaker_writes_no_entry(db, repo, tmp_path: Path) -> None:
    await repo.upsert_channel(
        "telegram:everyone", "telegram", default_person_id=None, session_mode="shared"
    )
    conv = await repo.get_or_create_conversation("telegram:everyone", None)
    manager = _manager(repo, _settings(), tmp_path)

    await manager.run_turn(
        make_channel("telegram:everyone", session_mode="shared"),
        conv,
        "what a nice day [[journal]]",
    )

    session = manager._pool[conv.id].session
    assert session.journal_writes == 0
    assert _today_entries(tmp_path) == []
