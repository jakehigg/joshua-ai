"""The memory bundle of one topic, built on a fixture wiki with a fake store."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest
from jobs_fakes import NOW, TOPIC, FakeStore, build_wiki, make_settings
from joshua_core.jobs import bundle as bundle_module
from joshua_core.jobs.bundle import build_bundle, topic_aliases, vocabulary_terms
from joshua_core.jobs.topics import load_topic
from joshua_core.memory import embed as embed_module

VEC = [1.0] + [0.0] * 383


@pytest.fixture
def fake_embed(monkeypatch):
    calls = {"n": 0}

    def _embed(text: str, model: str = "") -> list[float]:
        calls["n"] += 1
        return list(VEC)

    monkeypatch.setattr(embed_module, "embed", _embed)
    return calls


@pytest.fixture
def wiki(tmp_path: Path) -> Path:
    return build_wiki(tmp_path / "data")


async def _build(wiki: Path, store: FakeStore, extra: str = "", now: datetime = NOW):
    topic = load_topic(TOPIC, wiki)
    return await build_bundle(
        topic, wiki_root=wiki, repo=store, settings=make_settings(extra), now=now
    )


async def test_bundle_holds_the_parts_in_order(wiki: Path, fake_embed) -> None:
    store = FakeStore(
        {
            "wiki/journal/2026/10/08/bread.md": 0.81,
            "wiki/journal/2026/10/30/future.md": 0.9,
            "wiki/journal/2026/01/01/gone.md": 0.9,
            "shared/odd.md": 0.95,
        }
    )
    got = await _build(wiki, store)
    assert got.paths == [
        TOPIC,
        "houseplants/pothos.md",
        "houseplants/watering-schedule.md",
        "journal/2026/10/08/bread.md",
        "journal/2026/10/07/repot.md",
        "journal/2026/10/05/hall.md",
        "journal/2026/10/01/water.md",
        "knowledge/houseplants/snake-plant.md",
        "knowledge/houseplants/snake-plant/winter-watering.md",
        "insights/houseplants/too-wet.md",
    ]
    assert got.truncated is False
    assert got.chars == len(got.text)
    assert got.aliases == ["Sansevieria", "Snakey"]
    assert got.topic.title == "Snake plant"
    for path in got.paths:
        assert f"## {path}\n" in got.text
    assert got.text.startswith(f"## {TOPIC}\n\n---\naliases:")


async def test_bundle_vector_query_uses_the_journal_kind_and_the_floor(
    wiki: Path, fake_embed
) -> None:
    store = FakeStore()
    await _build(wiki, store, extra="jobs:\n  bundle:\n    sim: 0.65\n")
    assert store.calls == [{"count": 1, "kinds": ["journal"], "min_sim": 0.65, "source": "files"}]
    assert fake_embed["n"] == 1


async def test_bundle_leaves_out_what_is_not_about_the_topic(wiki: Path, fake_embed) -> None:
    got = await _build(wiki, FakeStore())
    for path in (
        "garden/irrigation.md",
        "vocabulary.md",
        "journal/2026/10/02/word.md",
        "journal/2026/10/08/bread.md",
        "journal/2026/10/09/pothos.md",
        "journal/2026/10/10/bundle-20261010-1.md",
        "journal/2026/10/30/future.md",
        "insights/houseplants/old.md",
        "insights/garden/dry.md",
    ):
        assert path not in got.paths


async def test_bundle_without_the_embedding_model_still_matches_by_text(
    wiki: Path, monkeypatch
) -> None:
    monkeypatch.setattr(embed_module, "embed", lambda text, model="": None)
    store = FakeStore({"wiki/journal/2026/10/08/bread.md": 0.9})
    got = await _build(wiki, store)
    assert store.calls == []
    assert "journal/2026/10/08/bread.md" not in got.paths
    assert "journal/2026/10/01/water.md" in got.paths


async def test_bundle_cuts_at_max_chars_but_never_the_topic_page(wiki: Path, fake_embed) -> None:
    got = await _build(wiki, FakeStore(), extra="jobs:\n  bundle:\n    max_chars: 10\n")
    assert got.paths == [TOPIC]
    assert got.truncated is True
    assert "Gritty mix, watered to runoff." in got.text
    assert got.chars > 10


async def test_bundle_cut_keeps_whole_parts(wiki: Path, fake_embed) -> None:
    full = await _build(wiki, FakeStore())
    limit = len("\n".join(full.text.split("\n## ")[0:1])) + 60
    got = await _build(wiki, FakeStore(), extra=f"jobs:\n  bundle:\n    max_chars: {limit}\n")
    assert got.truncated is True
    assert got.chars <= limit
    assert got.paths == [TOPIC, "houseplants/pothos.md"]


async def test_bundle_journal_is_newest_first_by_day(wiki: Path, fake_embed) -> None:
    got = await _build(wiki, FakeStore())
    days = [p.split("/")[1:4] for p in got.paths if p.startswith("journal/")]
    assert days == sorted(days, reverse=True)


async def test_bundle_now_sets_the_last_journal_day(wiki: Path, fake_embed) -> None:
    early = datetime(2026, 10, 6, 12, 0)
    got = await _build(wiki, FakeStore(), now=early)
    journal = [p for p in got.paths if p.startswith("journal/")]
    assert journal == ["journal/2026/10/05/hall.md", "journal/2026/10/01/water.md"]


async def test_bundle_refuses_a_path_that_is_not_a_wiki_root(tmp_path: Path, fake_embed) -> None:
    other = tmp_path / "notes"
    build_wiki(tmp_path / "data")
    (other / "houseplants").mkdir(parents=True)
    (other / TOPIC).write_text("# Snake plant\n")
    with pytest.raises(ValueError):
        await _build(other, FakeStore())


async def test_bundle_with_no_journal_knowledge_or_insights(tmp_path: Path, fake_embed) -> None:
    wiki = build_wiki(tmp_path / "data", {TOPIC: "# Snake plant\n\nAlone.\n"})
    got = await _build(wiki, FakeStore())
    assert got.paths == [TOPIC]
    assert got.aliases == []
    assert got.truncated is False


def test_vocabulary_rule(wiki: Path) -> None:
    topic = load_topic(TOPIC, wiki)
    assert vocabulary_terms(topic, wiki) == ["Snakey"]
    pothos = load_topic("houseplants/pothos.md", wiki)
    assert vocabulary_terms(pothos, wiki) == ["Pothy"]


def test_aliases_accept_a_string_and_drop_the_title_and_repeats(wiki: Path) -> None:
    topic = load_topic(TOPIC, wiki)
    meta = {"aliases": "Snake plant"}
    assert topic_aliases(topic, meta, wiki) == ["Snakey"]
    meta = {"aliases": ["snakey", " ", "Mother-in-law's tongue"]}
    assert topic_aliases(topic, meta, wiki) == ["snakey", "Mother-in-law's tongue"]


def test_short_terms_do_not_match() -> None:
    assert bundle_module._term_pattern(["ab", ""]) is None
    pattern = bundle_module._term_pattern(["fig", "ab"])
    assert pattern is not None
    assert pattern.search("Two figs.")
    assert not pattern.search("a configuration")


def test_resolve_link_rules(wiki: Path) -> None:
    resolve = bundle_module._resolve_link
    assert resolve("pothos.md#care", TOPIC, wiki) == "houseplants/pothos.md"
    assert resolve("pothos", TOPIC, wiki) == "houseplants/pothos.md"
    assert resolve("/garden/irrigation.md", TOPIC, wiki) == "garden/irrigation.md"
    assert resolve("[[garden/irrigation", TOPIC, wiki) == "garden/irrigation.md"
    assert resolve("mailto:a@example.com", TOPIC, wiki) is None
    assert resolve("../../../etc/passwd", TOPIC, wiki) is None
    assert resolve("a\\b.md", TOPIC, wiki) is None
    assert resolve("#top", TOPIC, wiki) is None
    assert resolve("snake%20plant.md", TOPIC, wiki) is None
