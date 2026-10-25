"""The job pages stay out of chat retrieval, and the topic hint takes their place.

The per-turn injection leaves out the kinds `knowledge`, `insight`, and
`question`. `search_memory` leaves out `insight` and `question` and keeps
`knowledge`. A turn that a person sent about a topic with due insights or open
questions gets one line with the counts only.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from joshua_core.engine import injection
from joshua_core.engine.injection import is_person_turn, kb_context
from joshua_core.engine.tools import ToolDeps
from joshua_core.engine.tools.recall import do_search_memory
from joshua_core.jobs.open_items import MAX_TOPICS, OpenItems, TopicItems, hint_line
from joshua_core.memory import embed as embed_module
from joshua_core.memory.models import KbChunk
from joshua_core.store.models import Channel, Conversation
from joshua_shared.config import Person

DAY = date(2026, 3, 14)
TOPIC = "houseplants/snake-plant.md"
OTHER = "garden/irrigation.md"
THIRD = "kitchen/water-filter.md"
CLAIM = "The snake plant gets the summer feed in a cold month."
QUESTION = "Should the feed reminders stop while the plant rests?"


def _write(root: Path, rel: str, text: str) -> None:
    path = root / "wiki" / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _insight(root: Path, slug: str, topic: str, *, status: str = "new", **extra: str) -> None:
    section = topic.split("/")[0]
    lines = [
        f"topic: {topic}",
        f"section: {section}",
        f"claim: {CLAIM}",
        f"status: {status}",
        "valid_from: 2026-03-01",
        "valid_until: 2026-04-30",
        "found_at: 2026-03-01",
        "seen_at: 2026-03-01",
        *(f"{k}: {v}" for k, v in extra.items()),
    ]
    _write(root, f"insights/{section}/{slug}.md", "---\n" + "\n".join(lines) + "\n---\n# x\n")


def _question(root: Path, slug: str, topic: str, *, status: str = "open") -> None:
    section = topic.split("/")[0]
    lines = [
        f"question: {QUESTION}",
        f"topic: {topic}",
        f"section: {section}",
        f"status: {status}",
        "raised_at: 2026-03-01",
    ]
    _write(root, f"questions/{section}/{slug}.md", "---\n" + "\n".join(lines) + "\n---\n# x\n")


@pytest.fixture
def data(tmp_path: Path) -> Path:
    _write(tmp_path, TOPIC, "---\naliases: [Sansevieria]\n---\n# Snake plant\n\nGritty mix.\n")
    _write(tmp_path, OTHER, "# Irrigation\n\nThe drip zones.\n")
    _write(tmp_path, THIRD, "# Water filter\n\nThe kitchen filter.\n")
    _insight(tmp_path, "summer-feed", TOPIC)
    _insight(tmp_path, "closed", TOPIC, status="dismissed")
    _insight(tmp_path, "snoozed", TOPIC, snoozed_until="2026-03-20")
    _question(tmp_path, "feed-reminders", TOPIC)
    _question(tmp_path, "rest-water", TOPIC)
    _question(tmp_path, "answered", TOPIC, status="answered")
    _question(tmp_path, "drip-winter", OTHER)
    _insight(tmp_path, "filter-alarm", THIRD)
    return tmp_path


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _items(data: Path, clock: Clock | None = None, **kwargs: Any) -> OpenItems:
    return OpenItems(
        data,
        today=lambda: DAY,
        people=[Person(id="alex", name="Alex")],
        monotonic=clock or Clock(),
        **kwargs,
    )


# --- OpenItems ---------------------------------------------------------------


def test_the_counts_take_only_due_insights_and_open_questions(data: Path) -> None:
    topics = _items(data).topics()
    assert set(topics) == {TOPIC, OTHER, THIRD}
    plant = topics[TOPIC]
    assert (plant.title, plant.insights, plant.questions) == ("Snake plant", 1, 2)
    assert (topics[OTHER].insights, topics[OTHER].questions) == (0, 1)


def test_a_kind_that_is_off_is_not_counted(data: Path) -> None:
    topics = _items(data, insights=False).topics()
    assert THIRD not in topics
    assert topics[TOPIC].insights == 0 and topics[TOPIC].questions == 2


def test_the_name_rule_finds_a_topic(data: Path) -> None:
    items = _items(data)
    found = items.about("How is the sansevieria doing this week?", [])
    assert [i.topic for i in found] == [TOPIC]
    assert items.about("What is on the list today?", []) == []


def test_the_ranked_page_rule_finds_a_topic(data: Path) -> None:
    found = _items(data).about("Is the hall dry?", ["wiki/notes/x.md", f"wiki/{OTHER}"])
    assert [i.topic for i in found] == [OTHER]


def test_the_rank_goes_first_and_two_topics_at_most(data: Path) -> None:
    text = "The snake plant, the irrigation, and the water filter."
    found = _items(data).about(text, [f"wiki/{THIRD}"])
    assert len(found) == MAX_TOPICS == 2
    assert found[0].topic == THIRD


def test_the_map_is_cached_for_sixty_seconds(data: Path) -> None:
    clock = Clock()
    items = _items(data, clock)
    assert items.topics()[TOPIC].questions == 2
    _question(data, "third-one", TOPIC)
    clock.now += 59
    assert items.topics()[TOPIC].questions == 2
    clock.now += 2
    assert items.topics()[TOPIC].questions == 3


def test_the_line_holds_the_counts_and_never_the_text(data: Path) -> None:
    line = hint_line(_items(data).about("the snake plant", []))
    assert line == (
        "[Open items about Snake plant (houseplants/snake-plant.md): 1 insight, "
        "2 questions. Read them with list_insights(topic=...) / "
        "list_questions(topic=...) before you mention one.]"
    )
    assert CLAIM not in line and QUESTION not in line
    only = hint_line([TopicItems(OTHER, "Irrigation", 0, 1)])
    assert "list_insights" not in only and "1 question." in only
    assert hint_line([]) == ""


# --- the injection -----------------------------------------------------------


class FakeRepo:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def add_kb_event(self, **kwargs: Any) -> None:
        self.events.append(kwargs)


def _chunk(kind: str, path: str, text: str = "Some text", sim: float = 0.9) -> KbChunk:
    return KbChunk(
        person_id=None,
        source="files",
        kind=kind,
        path=path,
        title=path,
        heading="",
        text=text,
        similarity=sim,
    )


JOB_CHUNKS = [
    _chunk("insight", "wiki/insights/houseplants/summer-feed.md", CLAIM, 0.95),
    _chunk("question", "wiki/questions/houseplants/feed-reminders.md", QUESTION, 0.94),
    _chunk("knowledge", "wiki/knowledge/houseplants/snake-plant/feed.md", "Feed in spring.", 0.93),
    _chunk("wiki", f"wiki/{TOPIC}", "Gritty mix.", 0.8),
]


class FakeStore:
    def __init__(self, chunks: list[KbChunk]) -> None:
        self.chunks = chunks
        self.excluded: list[Any] = []

    async def kb_search(  # type: ignore[no-untyped-def]
        self, person_id, embedding, *, k, min_sim, sources=None, kinds=None, exclude_kinds=None
    ):
        self.excluded.append(exclude_kinds)
        return [c for c in self.chunks if c.kind not in (exclude_kinds or ())]


@pytest.fixture(autouse=True)
def _const_embed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(embed_module, "embed", lambda text, model="": [0.1] * 384)


def _ctx(text: str, direction: str | None = "in") -> SimpleNamespace:
    ctx = SimpleNamespace(
        text=text, person_id="alex", conversation=SimpleNamespace(id="c1"), turn_id="t1"
    )
    if direction is not None:
        ctx.direction = direction
    return ctx


async def _inject(ctx: Any, store: Any, repo: Any, open_items: OpenItems | None) -> str | None:
    return await kb_context(
        ctx,
        store=store,
        repo=repo,
        embed_model="m",
        full_sim=0.72,
        hint_sim=0.60,
        top_k=3,
        max_chars=1200,
        recency_bonus=0.0,
        recency_half_life_days=14.0,
        per_doc_cap=3,
        open_items=open_items,
    )


async def test_the_injection_leaves_out_the_three_job_kinds() -> None:
    store, repo = FakeStore(JOB_CHUNKS), FakeRepo()
    note = await _inject(_ctx("tell me about the plant feed"), store, repo, None)
    assert store.excluded[0] == ("skill", "knowledge", "insight", "question")
    assert note is not None
    assert "Gritty mix." in note
    for text in (CLAIM, QUESTION, "Feed in spring."):
        assert text not in note
    assert {r["uri"] for r in repo.events[0]["results"]} == {f"wiki/{TOPIC}"}
    assert repo.events[0]["topics"] == []


async def test_a_person_turn_gets_the_hint_line_and_the_audit_names_it(data: Path) -> None:
    store, repo = FakeStore(JOB_CHUNKS), FakeRepo()
    note = await _inject(_ctx("is the hall dry enough?"), store, repo, _items(data))
    assert note is not None
    assert "[Open items about Snake plant (houseplants/snake-plant.md)" in note
    assert CLAIM not in note and QUESTION not in note
    assert repo.events[0]["topics"] == [{"topic": TOPIC, "insights": 1, "questions": 2}]


async def test_a_name_alone_gives_the_hint_with_no_search_hit(data: Path) -> None:
    store, repo = FakeStore([]), FakeRepo()
    note = await _inject(_ctx("how is the irrigation now?"), store, repo, _items(data))
    assert note == (
        "[Open items about Irrigation (garden/irrigation.md): 1 question. Read them "
        "with list_questions(topic=...) before you mention one.]"
    )


@pytest.mark.parametrize("direction", ["task", "inject"])
async def test_a_task_or_an_event_turn_gets_no_hint(data: Path, direction: str) -> None:
    store, repo = FakeStore(JOB_CHUNKS), FakeRepo()
    note = await _inject(_ctx("the snake plant feed", direction), store, repo, _items(data))
    assert note is not None and "Open items" not in note
    assert repo.events[0]["topics"] == []


async def test_a_broken_hint_never_breaks_the_turn(data: Path) -> None:
    class Broken(OpenItems):
        def topics(self) -> dict[str, TopicItems]:
            raise RuntimeError("disk gone")

    store, repo = FakeStore(JOB_CHUNKS), FakeRepo()
    note = await _inject(_ctx("the snake plant feed"), store, repo, Broken(data, today=lambda: DAY))
    assert note is not None and "Gritty mix." in note and "Open items" not in note


def test_is_person_turn() -> None:
    assert is_person_turn(_ctx("x", "in")) is True
    assert is_person_turn(_ctx("x", None)) is True
    assert is_person_turn(_ctx("x", "task")) is False
    assert is_person_turn(_ctx("x", "inject")) is False


def test_register_builds_the_open_items_only_with_a_job_that_writes_them() -> None:
    def settings(jobs: bool, insights: bool, research: bool) -> Any:
        return SimpleNamespace(
            jobs=SimpleNamespace(
                enabled=jobs,
                insights=SimpleNamespace(enabled=insights),
                research=SimpleNamespace(enabled=research),
            ),
            people=[],
            timezone="UTC",
        )

    now = lambda: None  # noqa: E731 — never called here
    assert injection._open_items(settings(False, True, True), "/x", now) is None
    assert injection._open_items(settings(True, False, False), "/x", now) is None
    assert isinstance(injection._open_items(settings(True, True, False), "/x", now), OpenItems)


# --- search_memory -----------------------------------------------------------


async def test_search_memory_keeps_knowledge_and_leaves_out_insights_and_questions() -> None:
    store = FakeStore(JOB_CHUNKS)
    conv = Conversation(id="c1", channel_id="telegram:1", person_id="alex")
    deps = ToolDeps(
        repo=None,
        conversation=conv,
        channel=Channel(id="telegram:1", channel_type="telegram"),
        tz="UTC",
        person_id="alex",
    )
    results = await do_search_memory(
        deps, store=store, embed_model="m", min_sim=0.5, query="the plant feed"
    )
    assert store.excluded[0] == ("skill", "insight", "question")
    paths = {r["path"] for r in results}
    assert paths == {"wiki/knowledge/houseplants/snake-plant/feed.md", f"wiki/{TOPIC}"}
