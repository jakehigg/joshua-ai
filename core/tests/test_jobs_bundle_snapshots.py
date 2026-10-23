"""The snapshot block of the memory bundle, and what the job workers do with
it: the selection, the scope rule, the rendering, the report line, the
grounding, the research record step, and the curator refresh. No model, no
database: the store and the workers are fakes."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from jobs_fakes import NOW, TOPIC, FakeStore, build_wiki, fixed_clock, make_settings
from joshua_core.jobs.bundle import (
    NO_SNAPSHOT_MODEL,
    NO_SNAPSHOT_SEARCH,
    SNAPSHOT_LINE_CHARS,
    build_bundle,
    job_snapshots,
    snapshot_summary,
)
from joshua_core.jobs.bundle_job import BundleJob
from joshua_core.jobs.curator import prompts as curator_prompts
from joshua_core.jobs.curator import workers as curator_workers
from joshua_core.jobs.insights.gate import we_do_parts
from joshua_core.jobs.insights.prompts import INSIGHT_PROMPT
from joshua_core.jobs.research.gate import grounding_reason
from joshua_core.jobs.research.prompts import EDITOR_PROMPT, RAISE_PROMPT
from joshua_core.jobs.research.text import Part, bundle_parts, find_phrase
from joshua_core.jobs.topics import load_topic
from joshua_core.memory import embed as embed_module
from joshua_core.memory.models import KbChunk

VEC = [1.0] + [0.0] * 383
# 11:30 in New York, the timezone of the test config.
FETCHED = datetime(2026, 10, 19, 15, 30, tzinfo=UTC)
AS_OF = "as of 2026-10-19 11:30 EDT."

SNAPSHOTS = """memory:
  snapshots:
    - name: open-tasks
      server: scheduling
      tool: list_tasks
      every: 15m
    - name: alex-tasks
      server: scheduling
      tool: list_tasks
      every: 15m
      scope: alex
    - name: alex-open
      server: scheduling
      tool: list_tasks
      every: 15m
      scope: alex
      jobs: true
"""


def chunk(name: str, body: str, sim: float, person: str | None = None, key: str = "") -> KbChunk:
    return KbChunk(
        source="snapshots",
        kind="snapshot",
        path=f"snapshot/{name}/{int(key) if key else len(body):012d}",
        person_id=person,
        title=name,
        text=f"{name}:\n{body}",
        similarity=sim,
    )


class SnapStore(FakeStore):
    """`FakeStore` with the two snapshot queries. It answers every row whose
    name the caller asks for, with no floor, so the bundle's own checks run."""

    def __init__(
        self,
        rows: Sequence[KbChunk] = (),
        times: dict[str, datetime] | None = None,
        near: dict[str, float] | None = None,
        fail: bool = False,
    ) -> None:
        super().__init__(near)
        self.rows = list(rows)
        self.times = times if times is not None else {r.title: FETCHED for r in rows}
        self.fail = fail
        self.snapshot_calls: list[dict[str, Any]] = []

    async def kb_near_snapshots(
        self,
        embeddings: Sequence[list[float]],
        *,
        names: Sequence[str],
        min_sim: float,
        limit: int,
    ) -> list[KbChunk]:
        self.snapshot_calls.append({"names": list(names), "min_sim": min_sim, "limit": limit})
        if self.fail:
            raise RuntimeError("index gone")
        return [r for r in self.rows if r.title in names][:limit]

    async def snapshot_times(self, names: Sequence[str]) -> dict[str, datetime]:
        return {n: t for n, t in self.times.items() if n in names}


@pytest.fixture
def fake_embed(monkeypatch):
    monkeypatch.setattr(embed_module, "embed", lambda text, model="": list(VEC))


@pytest.fixture
def wiki(tmp_path: Path) -> Path:
    return build_wiki(tmp_path / "data")


ROWS = [
    chunk("open-tasks", "title: Check the backup\ndue: 2026-10-21", 0.70, key="1"),
    chunk("open-tasks", "title: Change the filter\ndue: 2026-10-30", 0.90, key="2"),
    chunk("open-tasks", "title: Far away", 0.40, key="3"),
    chunk("alex-open", "title: Water the hall plant", 0.80, person="alex", key="4"),
]


def _settings(text: str, strong: float | None = 0.0) -> Any:
    """The test config. Most tests check the floor, the order, and the caps, so
    they turn the name gate off with a strong floor of 0. `strong=None` keeps
    the default."""
    settings = make_settings(text)
    if strong is None:
        return settings
    bundle_cfg = settings.jobs.bundle.model_copy(update={"snapshot_strong_sim": strong})
    jobs_cfg = settings.jobs.model_copy(update={"bundle": bundle_cfg})
    return settings.model_copy(update={"jobs": jobs_cfg})


async def _build(
    wiki: Path,
    store: SnapStore,
    extra: str = "",
    snaps: str = SNAPSHOTS,
    strong: float | None = 0.0,
):
    settings = _settings(snaps + extra, strong)
    return await build_bundle(
        load_topic(TOPIC, wiki), wiki_root=wiki, repo=store, settings=settings, now=NOW
    )


# -- selection --------------------------------------------------------------


async def test_the_block_comes_after_the_links_and_before_the_journal(
    wiki: Path, fake_embed
) -> None:
    got = await _build(wiki, SnapStore(ROWS))
    assert got.paths[:5] == [
        TOPIC,
        "houseplants/pothos.md",
        "houseplants/watering-schedule.md",
        "snapshots/open-tasks",
        "snapshots/alex-open",
    ]
    assert got.paths[5].startswith("journal/")


async def test_the_floor_the_order_and_the_query(wiki: Path, fake_embed) -> None:
    store = SnapStore(ROWS)
    got = await _build(wiki, store)
    [call] = store.snapshot_calls
    assert call["min_sim"] == 0.55
    assert call["names"] == ["alex-open", "open-tasks"]
    [tasks, alex] = got.snapshots
    # The closest item first; the item under the floor is left out.
    assert list(tasks.items.values()) == [0.9, 0.7]
    assert "Far away" not in got.text
    assert alex.items == {"snapshot/alex-open/000000000004": 0.8}
    assert got.snapshot_via["snapshot/open-tasks/000000000002"] == 0.9


async def test_an_item_needs_the_topic_name_or_a_strong_match(wiki: Path, fake_embed) -> None:
    rows = [
        chunk("open-tasks", "title: Water the snake plant", 0.60, key="11"),
        chunk("open-tasks", "title: Buy milk", 0.70, key="12"),
        chunk("open-tasks", "title: Change the filter", 0.80, key="13"),
    ]
    got = await _build(wiki, SnapStore(rows), strong=None)
    [tasks] = got.snapshots
    assert sorted(tasks.items.values()) == [0.6, 0.8]
    assert "Buy milk" not in tasks.text


async def test_the_item_cap(wiki: Path, fake_embed) -> None:
    extra = "jobs:\n  bundle:\n    snapshot_items: 2\n"
    got = await _build(wiki, SnapStore(ROWS), extra)
    assert sum(len(p.items) for p in got.snapshots) == 2
    assert "Change the filter" in got.text and "Water the hall plant" in got.text
    assert "Check the backup" not in got.text


async def test_the_char_cap_stops_at_the_first_item_that_does_not_fit(
    wiki: Path, fake_embed
) -> None:
    extra = "jobs:\n  bundle:\n    snapshot_chars: 120\n"
    got = await _build(wiki, SnapStore(ROWS), extra)
    assert [p.path for p in got.snapshots] == ["snapshots/open-tasks"]
    assert len(got.snapshots[0].items) == 1
    assert "Change the filter" in got.text and "Check the backup" not in got.text


async def test_a_person_snapshot_needs_jobs_true(wiki: Path, fake_embed) -> None:
    rows = [*ROWS, chunk("alex-tasks", "title: A private task", 0.99, person="alex")]
    store = SnapStore(rows)
    got = await _build(wiki, store)
    assert "alex-tasks" not in store.snapshot_calls[0]["names"]
    assert "A private task" not in got.text
    assert "snapshots/alex-tasks" not in got.paths
    settings = make_settings(SNAPSHOTS)
    assert sorted(job_snapshots(settings)) == ["alex-open", "open-tasks"]


async def test_a_row_with_another_scope_never_comes_in(wiki: Path, fake_embed) -> None:
    # A shared snapshot never takes a row of one person, even with its name.
    rows = [chunk("open-tasks", "title: Not shared", 0.99, person="alex")]
    got = await _build(wiki, SnapStore(rows))
    assert got.snapshots == [] and "Not shared" not in got.text


async def test_a_snapshot_with_no_good_fetch_is_left_out(wiki: Path, fake_embed) -> None:
    got = await _build(wiki, SnapStore(ROWS, times={"alex-open": FETCHED}))
    assert [p.name for p in got.snapshots] == ["alex-open"]


async def test_no_embedding_model_means_no_block_and_a_note(wiki: Path, monkeypatch) -> None:
    monkeypatch.setattr(embed_module, "embed", lambda text, model="": None)
    store = SnapStore(ROWS)
    got = await _build(wiki, store)
    assert store.snapshot_calls == []
    assert got.snapshots == [] and got.notes == [NO_SNAPSHOT_MODEL]
    assert got.paths[0] == TOPIC


async def test_a_failed_search_is_a_note_not_a_failure(wiki: Path, fake_embed) -> None:
    got = await _build(wiki, SnapStore(ROWS, fail=True))
    assert got.snapshots == [] and got.notes == [NO_SNAPSHOT_SEARCH]
    assert any(p.startswith("journal/") for p in got.paths)


async def test_no_snapshot_config_makes_no_snapshot_query(wiki: Path, fake_embed) -> None:
    store = SnapStore(ROWS)
    got = await _build(wiki, store, snaps="")
    assert store.snapshot_calls == [] and got.snapshots == [] and got.notes == []


async def test_truncation_cuts_the_journal_before_the_block(wiki: Path, fake_embed) -> None:
    full = await _build(wiki, SnapStore(ROWS))
    journal_at = full.text.index("## journal/")
    extra = f"jobs:\n  bundle:\n    max_chars: {journal_at + 5}\n"
    got = await _build(wiki, SnapStore(ROWS), extra)
    assert got.truncated is True
    assert "snapshots/open-tasks" in got.paths and "snapshots/alex-open" in got.paths
    assert not any(p.startswith("journal/") for p in got.paths)
    # A part that the cut left out is not recorded.
    cut = f"jobs:\n  bundle:\n    max_chars: {full.text.index('## snapshots/alex-open') + 5}\n"
    short = await _build(wiki, SnapStore(ROWS), cut)
    assert [p.name for p in short.snapshots] == ["open-tasks"]


# -- rendering and the report -----------------------------------------------


async def test_the_part_starts_with_the_as_of_line(wiki: Path, fake_embed) -> None:
    got = await _build(wiki, SnapStore(ROWS))
    parts = {p.path: p.text for p in bundle_parts(got)}
    text = parts["snapshots/open-tasks"]
    lines = text.splitlines()
    assert lines[0] == f"Live snapshot of scheduling list_tasks {AS_OF}"
    assert lines[2:] == [
        "- title: Change the filter; due: 2026-10-30",
        "- title: Check the backup; due: 2026-10-21",
    ]
    # The `<name>:` first line of the indexed item is not shown.
    assert "open-tasks:" not in text


async def test_a_long_item_is_one_line_and_cut(wiki: Path, fake_embed) -> None:
    rows = [chunk("open-tasks", "note: " + "word " * 200 + "\nmore: x", 0.9)]
    got = await _build(wiki, SnapStore(rows))
    [line] = [x for x in got.snapshots[0].text.splitlines() if x.startswith("- ")]
    assert len(line) == SNAPSHOT_LINE_CHARS + 2


async def test_the_report_line(wiki: Path, fake_embed) -> None:
    got = await _build(wiki, SnapStore(ROWS))
    assert snapshot_summary(got) == "Snapshots: open-tasks (2 items), alex-open (1 item)."
    empty = await _build(wiki, SnapStore(ROWS), snaps="")
    assert snapshot_summary(empty) == "Snapshots: none."


async def test_the_bundle_job_reports_the_items(tmp_path: Path, fake_embed) -> None:
    data = tmp_path / "data"
    build_wiki(data)
    job = BundleJob(store=SnapStore(ROWS), settings=_settings(SNAPSHOTS), data_dir=data)
    report = await job.run_once(args={"topic": TOPIC}, dry_run=True, clock=fixed_clock())
    section = dict(report.sections)["Snapshots"]
    assert section.startswith("Snapshots: open-tasks (2 items)")
    assert "`snapshot/open-tasks/000000000002`: 0.90" in section
    assert report.data["snapshot_via"]["snapshot/alex-open/000000000004"] == 0.8


# -- grounding --------------------------------------------------------------


async def test_a_snapshot_phrase_grounds_an_insight_and_a_question(wiki: Path, fake_embed) -> None:
    got = await _build(wiki, SnapStore(ROWS))
    parts = we_do_parts(bundle_parts(got))
    assert "snapshots/open-tasks" in [p.path for p in parts]
    found = find_phrase("Change the filter", parts)
    assert found is not None and found.path == "snapshots/open-tasks"
    assert grounding_reason(["Change the filter", "Snake plant"], got.text) == ""


def test_we_do_parts_keep_a_snapshot_part() -> None:
    parts = [Part("snapshots/x", "a"), Part("knowledge/y.md", "b"), Part("insights/z.md", "c")]
    assert [p.path for p in we_do_parts(parts)] == ["snapshots/x"]


async def test_the_insight_gate_takes_a_snapshot_path(tmp_path: Path) -> None:
    from insights_fakes import AUTUMN, IRRIGATION, TODAY, build_insights_wiki, item
    from joshua_core.jobs.insights.gate import InsightGate
    from joshua_core.jobs.insights.knowledge import published_pages
    from joshua_core.jobs.insights.worker import RaisedInsight

    if True:
        data = tmp_path / "data"
        build_insights_wiki(data)

        async def embed(text: str) -> list[list[float]]:
            return [list(VEC)]

        gate = InsightGate(
            parts=we_do_parts(
                [
                    Part(IRRIGATION, "The garden page."),
                    Part(
                        "snapshots/zones",
                        f"Live snapshot of garden list_zones {AS_OF}\n\n"
                        "- zone: 1; program: summer 20 min",
                    ),
                ]
            ),
            known=published_pages(IRRIGATION, data),
            open_insights=[],
            dismissed=[],
            today=TODAY,
            default_valid_days=60,
            dup_sim=0.88,
            embed=embed,
        )
        raised = RaisedInsight.model_validate(
            item(
                we_do=["program: summer 20 min"],
                we_do_paths=["snapshots/zones"],
                knowledge_says=[AUTUMN],
            )
        )
        verdict = await gate.check(raised)
    assert verdict.kind == "new", verdict.reason
    assert verdict.checked is not None
    assert verdict.checked.we_do_paths == ["snapshots/zones"]


# -- the research record step -----------------------------------------------


async def test_a_record_question_that_a_snapshot_answers(tmp_path: Path, monkeypatch) -> None:
    from joshua_core.jobs.research.job import ResearchJob
    from joshua_core.jobs.research.questions import load_questions
    from joshua_shared import layout
    from research_fakes import (
        SNAKE,
        FakeInternet,
        FakeWorkers,
        build_research_wiki,
        research_settings,
        word_vector,
    )
    from research_fakes import FakeStore as ResearchStore

    monkeypatch.setattr(embed_module, "embed", lambda text, model="": word_vector(text))
    question = "When was the snake plant last tied up after the leaf flop in the hall?"

    class Store(ResearchStore):
        def __init__(self) -> None:
            super().__init__({})
            self.snap = SnapStore(
                [
                    chunk("open-tasks", "title: Tie up the snake plant", 0.92, key="9"),
                    chunk("alex-tasks", "title: A private task", 0.99, person="alex"),
                ]
            )

        async def kb_near_snapshots(self, embeddings, *, names, min_sim, limit):
            return await self.snap.kb_near_snapshots(
                embeddings, names=names, min_sim=min_sim, limit=limit
            )

        async def snapshot_times(self, names):
            return await self.snap.snapshot_times(names)

    data = tmp_path / "data"
    build_research_wiki(data)
    raised = {
        "questions": [
            {
                "question": question,
                "local_context": "",
                "grounded_in": ["leaf flop", "lives in the hall"],
                "why": "",
                "audience": "record",
                "about_person": False,
            }
        ]
    }
    store = Store()
    job = ResearchJob(
        store=store,
        settings=research_settings(SNAPSHOTS),
        data_dir=data,
        runner=FakeWorkers(raise_answer=raised),
        internet_runner=FakeInternet(),
    )
    report = await job.run_once(
        args=job.check_args({"mode": "plan", "topic": SNAKE}), dry_run=False, clock=fixed_clock()
    )
    assert report.data["record"] == ["snapshots/open-tasks"]
    assert "answered_by: `snapshots/open-tasks`" in dict(report.sections)["Answered by our records"]
    assert load_questions(data, SNAKE) == []
    assert not layout.questions_root(data).exists()
    # The bundle asks first, at `snapshot_sim`. The record step asks for the
    # allowed snapshots only, at `record_sim`.
    [bundle_call, record_call] = store.snap.snapshot_calls
    assert bundle_call["min_sim"] == 0.55
    assert record_call["names"] == ["alex-open", "open-tasks"]
    assert record_call["min_sim"] == 0.8
    assert "Snapshots: open-tasks (1 item)." in dict(report.sections)["Bundle"]


# -- the curator refresh ----------------------------------------------------


class Runner:
    def __init__(self, answer: dict[str, Any]) -> None:
        self.answer = answer
        self.kwargs: dict[str, Any] = {}

    async def __call__(self, **kwargs: Any) -> dict[str, Any]:
        self.kwargs = kwargs
        return self.answer


async def test_the_refresh_worker_reads_the_snapshot_part() -> None:
    runner = Runner({"status": "current", "patch": [], "confidence": "high"})
    part = ("snapshots/open-tasks", f"Live snapshot of scheduling list_tasks {AS_OF}\n\n- x")
    got = await curator_workers.refresh(
        "a.md", "x", [], snapshots=[part], model=None, runner=runner
    )
    assert got.outcome == "answered"
    text = runner.kwargs["user_text"]
    assert '<snapshot path="snapshots/open-tasks">' in text
    assert f"Live snapshot of scheduling list_tasks {AS_OF}" in text


async def test_the_curator_refreshes_a_page_with_only_a_snapshot(tmp_path: Path) -> None:
    from curator_fakes import (
        SNAKE,
        FakeCuratorStore,
        FakeFindingStore,
        FakeWorkers,
        build_curator_wiki,
    )
    from joshua_core.jobs.curator.job import CuratorJob
    from joshua_shared import frontmatter

    class Store(FakeCuratorStore):
        snap = SnapStore([chunk("open-tasks", "title: Water the snake plant", 0.9, key="7")])

        async def kb_near_snapshots(self, embeddings, *, names, min_sim, limit):
            return await self.snap.kb_near_snapshots(
                embeddings, names=names, min_sim=min_sim, limit=limit
            )

        async def snapshot_times(self, names):
            return await self.snap.snapshot_times(names)

    class Workers(FakeWorkers):
        texts: list[str] = []

        async def __call__(self, **kwargs: Any) -> dict[str, Any] | None:
            if kwargs["system_prompt"] == curator_prompts.REFRESH_PROMPT:
                self.texts.append(kwargs["user_text"])
            return await super().__call__(**kwargs)

    data = tmp_path / "data"
    wiki_root = build_curator_wiki(data)
    page = wiki_root / SNAKE
    # Every journal entry about the page is older than its `refreshed` day.
    page.write_text(
        frontmatter.update(page.read_text(), curated="2026-10-15", refreshed="2026-10-15")
    )
    workers = Workers()
    job = CuratorJob(
        store=Store(),
        findings=FakeFindingStore(),
        settings=make_settings(
            "jobs:\n  enabled: true\n  curator:\n    enabled: true\n" + SNAPSHOTS
        ),
        data_dir=data,
        runner=workers,
    )
    monkey = pytest.MonkeyPatch()
    monkey.setattr(embed_module, "embed", lambda text, model="": list(VEC))
    try:
        report = await job.run_once(args=job.check_args({}), dry_run=True, clock=fixed_clock())
    finally:
        monkey.undo()
    [text] = [t for t in workers.texts if t.startswith(f'<page path="{SNAKE}">')]
    assert '<snapshot path="snapshots/open-tasks">' in text
    assert "<entry " not in text
    snaps = dict(report.sections)["Snapshots"]
    assert f"`{SNAKE}`: Snapshots: open-tasks (1 item)." in snaps


# -- the prompts ------------------------------------------------------------


@pytest.mark.parametrize(
    "prompt",
    [INSIGHT_PROMPT, RAISE_PROMPT, EDITOR_PROMPT, curator_prompts.REFRESH_PROMPT],
)
def test_each_prompt_holds_the_snapshot_rule(prompt: str) -> None:
    flat = " ".join(prompt.split())
    assert "the snapshot is the current state" in flat
    assert "what a live system held at the time on its first line" in flat
    assert "is not the words of a person" in flat


def test_the_prompts_route_and_cite_a_snapshot() -> None:
    raise_flat = " ".join(RAISE_PROMPT.split())
    assert "A question that a snapshot answers is a `record` question" in raise_flat
    refresh_flat = " ".join(curator_prompts.REFRESH_PROMPT.split())
    assert "or the snapshot path and its time" in refresh_flat
    assert "never a sign that a person cares about something" in " ".join(INSIGHT_PROMPT.split())
