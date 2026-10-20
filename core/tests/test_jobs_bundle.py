"""The memory bundle of one topic, built on a fixture wiki with a fake store."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest
from jobs_fakes import NOW, TOPIC, WIKI, FakeStore, build_wiki, make_settings
from joshua_core.jobs import bundle as bundle_module
from joshua_core.jobs.bundle import build_bundle, topic_aliases, vocabulary_terms
from joshua_core.jobs.topics import load_topic, names_pattern
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
    assert names_pattern(["ab", ""]) is None
    pattern = names_pattern(["fig", "ab"])
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


# -- the journal rules ----------------------------------------------------------


def _entry(day: int, slug: str, text: str, meta: str = "") -> tuple[str, str]:
    head = f"---\n{meta}---\n" if meta else ""
    return f"journal/2026/09/{day:02d}/{slug}.md", head + text


async def test_how_each_journal_entry_got_in(wiki: Path, fake_embed) -> None:
    store = FakeStore({"wiki/journal/2026/10/08/bread.md": 0.8123})
    got = await _build(wiki, store)
    assert got.journal_via == {
        "journal/2026/10/08/bread.md": "vector 0.81",
        "journal/2026/10/07/repot.md": "alias",
        "journal/2026/10/05/hall.md": "alias",
        "journal/2026/10/01/water.md": "title",
    }


async def test_vector_only_entries_are_capped_the_closest_first(tmp_path: Path, fake_embed) -> None:
    pages = dict(WIKI)
    near: dict[str, float] = {}
    for day in range(1, 11):
        rel, text = _entry(day, f"day-{day}", "Something else happened.\n")
        pages[rel] = text
        near[f"wiki/{rel}"] = 0.80 + day / 100
    named, text = _entry(11, "named", "Watered the snake plant.\n")
    pages[named] = text
    near[f"wiki/{named}"] = 0.79
    wiki = build_wiki(tmp_path / "data", pages)
    got = await _build(wiki, FakeStore(near), extra="jobs:\n  bundle:\n    max_vector_entries: 3\n")
    vector = [p for p, via in got.journal_via.items() if via.startswith("vector")]
    assert vector == [
        "journal/2026/09/10/day-10.md",
        "journal/2026/09/09/day-9.md",
        "journal/2026/09/08/day-8.md",
    ]
    assert got.journal_via[named] == "title"


async def test_the_default_cap_is_six(tmp_path: Path, fake_embed) -> None:
    pages = dict(WIKI)
    near = {}
    for day in range(1, 10):
        rel, text = _entry(day, f"day-{day}", "Other.\n")
        pages[rel] = text
        near[f"wiki/{rel}"] = 0.9
    got = await _build(build_wiki(tmp_path / "data", pages), FakeStore(near))
    assert sum(via.startswith("vector") for via in got.journal_via.values()) == 6


async def test_an_excluded_slug_is_left_out_even_when_it_names_the_topic(
    tmp_path: Path, fake_embed
) -> None:
    pages = dict(WIKI)
    weather, text = _entry(3, "garden-weather-log-2026-09-03", "Snake plant by the cold door.\n")
    pages[weather] = text
    kept, text = _entry(4, "weather-notes", "The snake plant.\n")
    pages[kept] = text
    wiki = build_wiki(tmp_path / "data", pages)
    store = FakeStore({f"wiki/{weather}": 0.99})
    got = await _build(
        wiki, store, extra='jobs:\n  bundle:\n    exclude_slugs:\n      - "garden-weather-log-*"\n'
    )
    assert weather not in got.paths
    assert kept in got.paths


@pytest.mark.parametrize("source", ["curator", "research", "insights"])
async def test_a_job_report_is_left_out_by_its_source(
    tmp_path: Path, fake_embed, source: str
) -> None:
    pages = dict(WIKI)
    report, text = _entry(5, f"{source}-report", "The snake plant.\n", f"source: {source}\n")
    pages[report] = text
    person, text = _entry(6, "chat", "The snake plant.\n", "source: chat\n")
    pages[person] = text
    got = await _build(build_wiki(tmp_path / "data", pages), FakeStore({f"wiki/{report}": 0.99}))
    assert report not in got.paths
    assert person in got.paths


async def test_a_short_name_of_the_title_brings_in_an_entry(tmp_path: Path, fake_embed) -> None:
    window = "houseplants/snake-plants-living-room-window.md"
    pages = {
        "Home.md": "# Home\n",
        "houseplants.md": "# Houseplants\n",
        window: "# Snake Plants (Living Room Window)\n\nGritty mix.\n",
    }
    one, text = _entry(2, "water", "Watered the snake plant by the window.\n")
    pages[one] = text
    other, text = _entry(3, "pothos", "Watered the plants.\n")
    pages[other] = text
    wiki = build_wiki(tmp_path / "data", pages)
    got = await build_bundle(
        load_topic(window, wiki),
        wiki_root=wiki,
        repo=FakeStore(),
        settings=make_settings(),
        now=NOW,
    )
    assert got.journal_via == {one: "title"}


def test_the_bundle_floor_and_cap_defaults() -> None:
    cfg = make_settings().jobs.bundle
    assert cfg.sim == 0.78
    assert cfg.max_vector_entries == 6
    assert cfg.exclude_slugs == []


async def test_the_bundle_job_reports_how_each_entry_got_in(wiki: Path, fake_embed) -> None:
    from joshua_core.jobs.bundle_job import BundleJob

    job = BundleJob(
        store=FakeStore({"wiki/journal/2026/10/08/bread.md": 0.8}),
        settings=make_settings(),
        data_dir=wiki.parent,
    )
    report = await job.run_once(args={"topic": TOPIC}, dry_run=True, clock=lambda: NOW)
    body = dict(report.sections)["Journal entries"]
    assert "- `journal/2026/10/08/bread.md`: vector 0.80" in body
    assert "- `journal/2026/10/07/repot.md`: alias" in body
    assert "- `journal/2026/10/01/water.md`: title" in body
    assert report.data["journal_via"]["journal/2026/10/05/hall.md"] == "alias"


async def test_bundle_holds_the_answered_questions_of_the_topic_only(
    wiki: Path, fake_embed
) -> None:
    folder = wiki / "questions" / "houseplants"
    folder.mkdir(parents=True)
    head = f"---\nquestion: Which feed went on at the October watering?\ntopic: {TOPIC}\n"
    (folder / "feed.md").write_text(
        head + "status: answered\nanswer: Half strength 10-10-10.\nwhy: Secret why.\n---\n"
        "# Which feed\n\n## Why Joshua wants to know\n\nSecret body.\n"
    )
    (folder / "open.md").write_text(head + "status: open\n---\nOpen body.\n")
    (folder / "dismissed.md").write_text(head + "status: dismissed\nanswer: No.\n---\n")
    (folder / "blank.md").write_text(head + "status: answered\nanswer: ' '\n---\n")
    (folder / "other.md").write_text(
        "---\nquestion: Q?\ntopic: garden/irrigation.md\nstatus: answered\nanswer: A.\n---\n"
    )
    got = await _build(wiki, FakeStore())
    questions = [p for p in got.paths if p.startswith("questions/")]
    assert questions == ["questions/houseplants/feed.md"]
    knowledge = got.paths.index("knowledge/houseplants/snake-plant/winter-watering.md")
    assert (
        knowledge
        < got.paths.index(questions[0])
        < got.paths.index("insights/houseplants/too-wet.md")
    )
    assert (
        "## questions/houseplants/feed.md\n\n"
        "Question: Which feed went on at the October watering?\n\n"
        "Answer: Half strength 10-10-10.\n"
    ) in got.text
    assert "Secret" not in got.text and "Open body" not in got.text


async def test_a_roster_name_is_no_alias_and_brings_no_entry(tmp_path: Path, fake_embed) -> None:
    pages = dict(WIKI)
    pages[TOPIC] = "---\naliases: [Sansevieria, Alex]\n---\n# Snake plant\n\nGritty mix.\n"
    pages["journal/2026/10/11/alex.md"] = "Alex went for a long run.\n"
    wiki = build_wiki(tmp_path / "data", pages)
    got = await _build(wiki, FakeStore())
    assert "Alex" not in got.aliases and "Sansevieria" in got.aliases
    assert "journal/2026/10/11/alex.md" not in got.paths
    assert "journal/2026/10/07/repot.md" in got.paths


async def test_a_possessive_title_brings_in_only_the_entries_about_the_thing(
    tmp_path: Path, fake_embed
) -> None:
    soup = "recipes/alex-s-caldo-verde.md"
    pages = {
        "Home.md": "# Home\n",
        "recipes.md": "# Recipes\n",
        soup: "# Alex's Caldo Verde\n\nKale, potato, and chorizo.\n",
    }
    made, text = _entry(2, "soup", "We made caldo verde again.\n")
    pages[made] = text
    plants, text = _entry(3, "plants", "Alex watered the plants.\n")
    pages[plants] = text
    dinner, text = _entry(4, "dinner", "Alex's dinner was pizza.\n")
    pages[dinner] = text
    wiki = build_wiki(tmp_path / "data", pages)
    got = await build_bundle(
        load_topic(soup, wiki), wiki_root=wiki, repo=FakeStore(), settings=make_settings(), now=NOW
    )
    assert got.journal_via == {made: "title"}
