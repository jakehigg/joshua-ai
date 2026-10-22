"""The `digest` job on a fixture wiki: the candidate rules, the slug stem, the
groups by meaning, the limits, plan, dry run, run, undo, the worker checks,
what the index and the bundle leave out, and the runner wiring. No model, no
network, no database."""

from __future__ import annotations

import asyncio
import copy
import subprocess
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest
from jobs_fakes import NOW, FakeJobRunStore, FakeStore, build_wiki, fixed_clock, make_settings
from joshua_core.engine.usage import Usage
from joshua_core.jobs import JobArgsError, build_job_runner
from joshua_core.jobs.bundle import journal_entries
from joshua_core.jobs.digest import groups as groups_module
from joshua_core.jobs.digest.groups import (
    BY_MEANING,
    BY_SLUG,
    SKIP_COVERED,
    SKIP_DAY_PAGE,
    SKIP_DIGEST,
    SKIP_FRONTMATTER,
    SKIP_NEW,
    SKIP_REPORT,
    Entry,
    Group,
    build_groups,
    find_candidates,
    slug_stem,
    split_runs,
)
from joshua_core.jobs.digest.job import DigestJob
from joshua_core.jobs.digest.page import digest_slug, mark, render_digest, unmark
from joshua_core.jobs.digest.prompts import DIGEST_PROMPT
from joshua_core.jobs.digest.worker import (
    MAX_WORDS,
    DigestAnswer,
    check_answer,
    digest_text,
    write_digest,
)
from joshua_core.jobs.models import digest_model
from joshua_core.memory import embed as embed_module
from joshua_core.memory.sources.files import not_memory
from joshua_shared import frontmatter, wikigit
from research_fakes import changed, snapshot

DIGEST_ON = "jobs:\n  enabled: true\n  digest:\n    enabled: true\n"
TODAY = date(2026, 10, 19)
USAGE = Usage(input=200, output=40)


def _entry(day: str, people: str = "[alex]", extra: str = "") -> str:
    return f"---\ndate: {day}\npeople: {people}\n{extra}---\n"


FILTER = [
    (
        "journal/2026/08/03/filter-check-monday-august-3.md",
        _entry("2026-08-03") + "# Filter check\n\nAlex checked the filter. Pressure 10 psi.\n",
    ),
    (
        "journal/2026/08/04/filter-check-tuesday-august-4.md",
        _entry("2026-08-04") + "Alex checked the filter again. Pressure 11 psi.\n",
    ),
    (
        "journal/2026/08/05/filter-check-wednesday-august-5.md",
        _entry("2026-08-05", "[alex, sam]") + "Sam changed the filter. Pressure 6 psi.\n",
    ),
    (
        "journal/2026/08/06/filter-check-6th.md",
        "The filter check was normal.\n",
    ),
    (
        "journal/2026/08/07/filter-check-thursday-aug-7.md",
        _entry("2026-08-07") + "The filter check was normal. Pressure 6 psi.\r\n",
    ),
]
BACKUP = [
    ("journal/2026/08/12/backup-ran.md", _entry("2026-08-12") + "The backup copy ran.\n"),
    ("journal/2026/08/13/nightly-copy-ok.md", _entry("2026-08-13") + "Backup copy ok.\n"),
    ("journal/2026/08/14/offsite-sync.md", _entry("2026-08-14") + "Offsite backup copy.\n"),
    ("journal/2026/08/15/archive-done.md", _entry("2026-08-15") + "Backup copy done.\n"),
]
OTHER = {
    "journal/2026/08/16/bread.md": _entry("2026-08-16") + "Baked bread.\n",
    "journal/2026/08/03/2026-08-03.md": "# The day\n\nThe filter check.\n",
    "journal/2026/10/10/filter-check-october-10.md": _entry("2026-10-10") + "Filter ok.\n",
    "journal/2026/08/09/curator-20260809-3.md": (
        "---\nsource: curator\nrun_id: 3\n---\nThe filter check report.\n"
    ),
    "journal/2026/08/10/filter-check-august-10.md": _entry(
        "2026-08-10", extra="digest: journal/2026/08/07/digest-old.md\n"
    )
    + "Covered.\n",
    "journal/2026/08/11/digest-old.md": _entry("2026-08-11", extra="source: digest\n") + "Old.\n",
    "journal/2026/08/17/filter-check-broken.md": "---\n- a list, not a mapping\n---\nBroken.\n",
}


def _pages() -> dict[str, str]:
    return {"Home.md": "# Home\n", **dict(FILTER), **dict(BACKUP), **OTHER}


def word_vector(text: str) -> list[float]:
    words = ("filter", "backup", "copy", "bread", "pressure")
    low = text.lower()
    return [*(1.0 if w in low else 0.0 for w in words), 0.1]


@pytest.fixture(autouse=True)
def fake_embed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(embed_module, "embed", lambda text, model="": word_vector(text))


def _answer(title: str = "The filter checks", summary: str | None = None) -> dict[str, Any]:
    return {
        "title": title,
        "summary": summary
        or "Alex checked the filter from 3 to 7 August. Sam changed it on 5 August.",
    }


class FakeDigestWorker:
    """The `digest` worker. `value` is the answer, or a callable of the user text."""

    def __init__(self, value: Any = None) -> None:
        self.value = value if value is not None else _answer()
        self.calls: list[str] = []
        self.kwargs: list[dict[str, Any]] = []

    async def __call__(self, **kwargs: Any) -> dict[str, Any] | None:
        if kwargs["system_prompt"] != DIGEST_PROMPT:
            raise AssertionError("unknown worker")
        self.calls.append(kwargs["user_text"])
        self.kwargs.append(kwargs)
        out = kwargs.get("usage_out")
        if out is not None:
            out.append(USAGE)
        value = self.value(kwargs["user_text"]) if callable(self.value) else self.value
        return copy.deepcopy(value)


class Harness:
    def __init__(self, data: Path, extra: str = "", value: Any = None) -> None:
        self.data = data
        self.wiki = build_wiki(data, _pages())
        self.settings = make_settings(DIGEST_ON + extra)
        self.worker = FakeDigestWorker(value)

    def job(self) -> DigestJob:
        return DigestJob(settings=self.settings, data_dir=self.data, runner=self.worker)

    async def run(self, mode: str, *, dry_run: bool = False, **args: Any):
        job = self.job()
        return await job.run_once(
            args=job.check_args({"mode": mode, **args}), dry_run=dry_run, clock=fixed_clock()
        )


@pytest.fixture
def h(tmp_path: Path) -> Harness:
    return Harness(tmp_path / "data")


def _make(rel: str, day: date, body: str = "") -> Entry:
    return Entry(rel, day, body, {}, body)


# -- candidates -------------------------------------------------------------------


def test_the_candidate_rules(h: Harness) -> None:
    found = find_candidates(h.data, TODAY, 30)
    rels = [e.rel for e in found.entries]
    assert rels == sorted(rels, key=lambda r: (r.split("/")[1:4], r))
    assert set(rels) == {p for p, _ in FILTER} | {p for p, _ in BACKUP} | {
        "journal/2026/08/16/bread.md"
    }
    assert found.skipped == {
        SKIP_DAY_PAGE: ["journal/2026/08/03/2026-08-03.md"],
        SKIP_NEW: ["journal/2026/10/10/filter-check-october-10.md"],
        SKIP_REPORT: ["journal/2026/08/09/curator-20260809-3.md"],
        SKIP_COVERED: ["journal/2026/08/10/filter-check-august-10.md"],
        SKIP_DIGEST: ["journal/2026/08/11/digest-old.md"],
        SKIP_FRONTMATTER: ["journal/2026/08/17/filter-check-broken.md"],
    }


def test_the_age_rule_is_strict(h: Harness) -> None:
    # An entry dated exactly min_age_days before today is too new.
    day = date(2026, 8, 16)
    found = find_candidates(h.data, day + timedelta(days=30), 30)
    assert "journal/2026/08/16/bread.md" in found.skipped[SKIP_NEW]
    found = find_candidates(h.data, day + timedelta(days=31), 30)
    assert "journal/2026/08/16/bread.md" in [e.rel for e in found.entries]


def test_no_journal_gives_no_candidate(tmp_path: Path) -> None:
    assert find_candidates(tmp_path / "none", TODAY, 30).entries == []


# -- the slug stem -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("slug", "stem"),
    [
        ("filter-check-monday-march-3", "filter-check"),
        ("filter-check-tuesday-march-4", "filter-check"),
        ("backup-check-2026-03-04", "backup-check"),
        ("weekly-review-3rd-sept", "weekly-review"),
        ("Water-Log-Fri-Jan-21st", "water-log"),
        ("20260304", ""),
        ("a-filter-change", "a-filter-change"),
    ],
)
def test_the_slug_stem(slug: str, stem: str) -> None:
    assert slug_stem(slug) == stem


# -- groups -----------------------------------------------------------------------


def _vectors(table: dict[str, list[float]]):
    async def embed(entry: Entry) -> list[float] | None:
        return table.get(entry.rel)

    return embed


async def test_the_stem_group_and_the_meaning_group(h: Harness) -> None:
    job = h.job()
    found = find_candidates(h.data, TODAY, 30)
    grouping = await build_groups(
        found.entries, embed=job._embed, sim=0.85, min_entries=4, max_entries=40, max_groups=5
    )
    [first, second] = grouping.groups
    assert first.by == BY_SLUG and first.key == "filter-check"
    assert first.paths == [p for p, _ in FILTER]
    assert second.by == BY_MEANING and second.key == "backup-ran"
    assert second.paths == [p for p, _ in BACKUP]
    assert grouping.alone == ["journal/2026/08/16/bread.md"]


async def test_vector_grouping_is_greedy_in_date_order() -> None:
    entries = [_make(f"journal/2026/01/0{i}/note-x{i}.md", date(2026, 1, i)) for i in range(1, 7)]
    table = {
        entries[0].rel: [1.0, 0.0],
        entries[1].rel: [0.0, 1.0],
        entries[2].rel: [0.99, 0.05],
        entries[3].rel: [0.05, 0.99],
        entries[4].rel: [0.95, 0.1],
        entries[5].rel: [0.7, 0.7],
    }
    grouping = await build_groups(
        entries, embed=_vectors(table), sim=0.9, min_entries=2, max_entries=40, max_groups=5
    )
    assert [g.paths for g in grouping.groups] == [
        [entries[0].rel, entries[2].rel, entries[4].rel],
        [entries[1].rel, entries[3].rel],
    ]
    assert grouping.alone == [entries[5].rel]


async def test_an_entry_with_no_vector_and_a_missing_model() -> None:
    entries = [_make(f"journal/2026/01/0{i}/n-x{i}.md", date(2026, 1, i)) for i in range(1, 4)]
    grouping = await build_groups(
        entries,
        embed=_vectors({entries[0].rel: [1.0], entries[1].rel: [1.0]}),
        sim=0.9,
        min_entries=2,
        max_entries=40,
        max_groups=5,
    )
    assert grouping.no_vector == [entries[2].rel]
    assert len(grouping.groups) == 1

    async def broken(entry: Entry) -> list[float] | None:
        raise RuntimeError("embedding unavailable")

    grouping = await build_groups(
        entries, embed=broken, sim=0.9, min_entries=2, max_entries=40, max_groups=5
    )
    assert grouping.groups == [] and "embedding unavailable" in grouping.notes[0]


def _stem_entries(stem: str, count: int, month: int = 1) -> list[Entry]:
    return [
        _make(f"journal/2026/{month:02d}/{i:02d}/{stem}-{i}.md", date(2026, month, i))
        for i in range(1, count + 1)
    ]


async def _no_vector(entry: Entry) -> list[float] | None:
    return None


async def test_min_entries_max_entries_and_max_groups() -> None:
    entries = [
        *_stem_entries("alpha", 9, 1),
        *_stem_entries("beta", 3, 2),
        *_stem_entries("gamma", 4, 3),
        *_stem_entries("delta", 5, 4),
    ]
    grouping = await build_groups(
        entries, embed=_no_vector, sim=0.9, min_entries=4, max_entries=4, max_groups=3
    )
    # alpha (9) splits into 3 + 3 + 3, and delta (5) into 3 + 2: each run is
    # below min_entries 4, and so is beta.
    assert [(g.key, g.size) for g in grouping.groups] == [("gamma", 4)]
    assert grouping.over == []

    grouping = await build_groups(
        entries, embed=_no_vector, sim=0.9, min_entries=3, max_entries=4, max_groups=3
    )
    sizes = [(g.key, g.size) for g in grouping.groups]
    assert sizes == [("gamma", 4), ("alpha", 3), ("alpha", 3)]
    assert [(g.key, g.size) for g in grouping.over] == [("alpha", 3), ("beta", 3), ("delta", 3)]
    # The run of 2 of delta is dropped.
    assert grouping.alone == ["journal/2026/04/04/delta-4.md", "journal/2026/04/05/delta-5.md"]
    runs = split_runs(_stem_entries("alpha", 9), 4)
    assert [len(r) for r in runs] == [3, 3, 3]
    assert [r[0].day.day for r in runs] == [1, 4, 7]


async def test_the_order_is_by_size_then_by_first_day() -> None:
    entries = [*_stem_entries("late", 4, 5), *_stem_entries("early", 4, 2)]
    entries += _stem_entries("big", 5, 9)
    grouping = await build_groups(
        entries, embed=_no_vector, sim=0.9, min_entries=4, max_entries=40, max_groups=5
    )
    assert [g.key for g in grouping.groups] == ["big", "early", "late"]


async def test_the_same_wiki_and_day_give_the_same_groups(h: Harness) -> None:
    first = await h.run("plan", dry_run=True)
    second = await h.run("plan", dry_run=True)
    assert first.data["groups"] == second.data["groups"]
    assert first.data["groups"]


# -- the arguments ------------------------------------------------------------------


def test_the_arguments(h: Harness) -> None:
    job = h.job()
    assert job.check_args({"mode": "plan"}) == {"mode": "plan"}
    assert job.check_args({"mode": "run"}) == {"mode": "run"}
    digest = "journal/2026/08/11/digest-old.md"
    assert job.check_args({"mode": "undo", "digest": f" {digest} "}) == {
        "mode": "undo",
        "digest": digest,
    }
    bad: list[dict[str, Any]] = [
        {},
        {"mode": "go"},
        {"mode": "plan", "x": 1},
        {"mode": "run", "digest": digest},
        {"mode": "undo"},
        {"mode": "undo", "digest": "journal/../Home.md"},
        {"mode": "undo", "digest": "/etc/passwd"},
        {"mode": "undo", "digest": "Home.md"},
        {"mode": "undo", "digest": "journal/2026/08/16/bread.md"},
        {"mode": "undo", "digest": "journal/2026/01/01/none.md"},
    ]
    for args in bad:
        with pytest.raises(JobArgsError):
            job.check_args(args)


def test_the_model_chain() -> None:
    def chain(extra: str) -> Any:
        return make_settings("core:\n  reflection_model: m-r\n" + DIGEST_ON + extra)

    assert digest_model(chain("    model: m-digest\n  model: m-jobs\n")) == "m-digest"
    assert digest_model(chain("  model: m-jobs\n")) == "m-jobs"
    assert digest_model(chain("")) == "m-r"


def test_max_entries_must_not_be_below_min_entries() -> None:
    with pytest.raises(Exception, match="max_entries"):
        make_settings(DIGEST_ON + "    min_entries: 5\n    max_entries: 4\n")


# -- plan, dry run, run -------------------------------------------------------------


async def test_plan_calls_no_worker_and_writes_nothing(h: Harness) -> None:
    before = snapshot(h.data)
    report = await h.run("plan", dry_run=False)
    assert changed(before, snapshot(h.data)) == set()
    assert h.worker.calls == [] and report.written == [] and report.calls == 0
    assert report.summary == "digest plan: 2 groups, 9 entries"
    groups = report.data["groups"]
    assert groups[0]["digest"] == "journal/2026/08/07/digest-filter-check.md"
    assert groups[1]["digest"] == "journal/2026/08/15/digest-backup-ran.md"
    assert groups[0]["first"] == "2026-08-03" and groups[0]["last"] == "2026-08-07"
    sections = dict(report.sections)
    assert "`filter-check` (by slug, 5 entries, 2026-08-03 to 2026-08-07)" in sections["Groups"]
    assert f"- {SKIP_DAY_PAGE}: 1" in sections["Skipped"]
    assert "- in no group: 1" in sections["Skipped"]
    assert report.data["skipped"][SKIP_REPORT] == 1


async def test_a_dry_run_calls_the_worker_and_writes_nothing(h: Harness) -> None:
    before = snapshot(h.data)
    report = await h.run("run", dry_run=True)
    assert changed(before, snapshot(h.data)) == set()
    assert report.written == []
    assert len(h.worker.calls) == 2 and report.calls == 2
    assert report.summary == "Dry run: digest: 2 digests, 9 entries covered, 0 rejected"
    sections = dict(report.sections)
    assert "would write `journal/2026/08/07/digest-filter-check.md`" in sections["Groups"]
    assert "source: digest" in sections["Digests"]
    assert report.data["usage"]["digest"]["calls"] == 2


async def test_the_worker_reads_the_entries_in_date_order(h: Harness) -> None:
    await h.run("run", dry_run=True)
    text = h.worker.calls[0]
    at = [text.index(f"## {p}") for p, _ in FILTER]
    assert at == sorted(at)
    assert "Date: 2026-08-03 (Monday)" in text and "People: alex, sam" in text
    assert "Instance: Test House" in text and "Entries: 5, from 2026-08-03 to 2026-08-07" in text
    assert h.worker.kwargs[0]["model"] == digest_model(h.settings)


async def test_a_real_run_writes_the_digest_and_one_line_per_entry(h: Harness) -> None:
    before = snapshot(h.wiki)
    report = await h.run("run")
    digest_rel = "journal/2026/08/07/digest-filter-check.md"
    covered = [p for p, _ in FILTER]
    assert report.summary == "digest: 2 digests, 9 entries covered, 0 rejected"
    after = snapshot(h.wiki)
    assert changed(before, after) == {
        digest_rel,
        "journal/2026/08/15/digest-backup-ran.md",
        *covered,
        *(p for p, _ in BACKUP),
    }
    meta, body = frontmatter.split(after[digest_rel].decode())
    assert meta["source"] == "digest" and meta["covers"] == covered
    assert meta["date"] == date(2026, 8, 7) and meta["people"] == ["alex", "sam"]
    assert "run_id" not in meta and "digest_run" not in meta
    assert body.startswith("\n# The filter checks\n\nAlex checked the filter")
    assert "Covers 5 entries from 2026-08-03 to 2026-08-07:" in body
    link = "../03/filter-check-monday-august-3.md"
    assert f"- [2026-08-03 filter-check-monday-august-3]({link})" in body
    for rel, original in FILTER:
        new = after[rel].decode()
        old_lines = original.encode().splitlines(keepends=True)
        new_lines = new.encode().splitlines(keepends=True)
        added = [line for line in new_lines if line not in old_lines]
        assert added in (
            [f"digest: {digest_rel}\n".encode()],
            [b"---\n", f"digest: {digest_rel}\n".encode(), b"---\n"],
        )
        assert frontmatter.split(new)[1] == frontmatter.split(original)[1] or (
            frontmatter.split(new)[1] == original
        )
        assert unmark(new, digest_rel) == original
    assert set(report.written) == {h.wiki / p for p in changed(before, after)}


async def test_a_second_run_finds_nothing_new(h: Harness) -> None:
    await h.run("run")
    report = await h.run("plan", dry_run=True)
    assert report.data["groups"] == []
    assert report.data["skipped"][SKIP_COVERED] == 10
    assert report.data["skipped"][SKIP_DIGEST] == 3


async def test_a_taken_slug_gets_a_number(h: Harness) -> None:
    taken = h.wiki / "journal/2026/08/07/digest-filter-check.md"
    taken.write_text("A page a person wrote.\n")
    report = await h.run("plan", dry_run=True)
    assert report.data["groups"][0]["digest"] == "journal/2026/08/07/digest-filter-check-2.md"


async def test_a_rejected_answer_writes_nothing_for_its_group(tmp_path: Path) -> None:
    long = " ".join(["word"] * (MAX_WORDS + 1))
    h = Harness(tmp_path / "data", value=lambda text: _answer(summary=long))
    before = snapshot(h.wiki)
    report = await h.run("run")
    assert changed(before, snapshot(h.wiki)) == set()
    assert report.summary == "digest: 0 digests, 0 entries covered, 2 rejected"
    assert f"more than {MAX_WORDS}" in dict(report.sections)["Groups"]

    h.worker.value = {"title": "no summary"}
    report = await h.run("run")
    assert "matches the schema" in report.data["groups"][0]["rejected"]


# -- the worker checks ----------------------------------------------------------------


def _group(text: str = "See https://example.com/a for it.") -> Any:
    entry = Entry("journal/2026/01/01/x-1.md", date(2026, 1, 1), text, {}, text)
    return Group("x", BY_SLUG, (entry,))


def test_the_answer_checks() -> None:
    group = _group()
    ok = check_answer(
        DigestAnswer(
            title="# A title\nwith two lines",
            summary="Kept [a](https://example.com/a), not [b](https://evil.example.org/b) "
            "and not [[wiki-page]].",
        ),
        group,
    )
    assert ok.reason is None and ok.title == "A title with two lines"
    assert "[a](https://example.com/a)" in ok.summary
    assert "evil.example.org" not in ok.summary and "[[" not in ok.summary
    assert check_answer(DigestAnswer(title=" ", summary="s"), group).reason
    assert check_answer(DigestAnswer(title="t", summary=" "), group).reason
    words = " ".join(["w"] * MAX_WORDS)
    assert check_answer(DigestAnswer(title="t", summary=words), group).reason is None


async def test_write_digest_with_the_stub_runner() -> None:
    group = _group()
    worker = FakeDigestWorker(_answer())
    used: list[Usage] = []
    digest = await write_digest(
        group,
        user_text=digest_text(group, timezone="UTC", instance="Test"),
        model="m",
        runner=worker,
        usage_out=used,
    )
    assert digest.title == "The filter checks" and used == [USAGE]
    worker.value = {"title": 5}
    assert (await write_digest(group, user_text="x", model="m", runner=worker)).reason


def test_a_long_entry_is_cut_for_the_worker() -> None:
    text = digest_text(_group("x" * 7000), timezone="UTC", instance="Test")
    assert "[the rest of this entry is cut]" in text


# -- the page --------------------------------------------------------------------------


def test_mark_and_unmark_keep_the_text() -> None:
    rel = "journal/2026/01/09/digest-x.md"
    for original in (
        "---\ndate: 2026-01-01\n# a comment\npeople: [alex]\n---\n# Body\n\n---\nmore\n",
        "No frontmatter here.\n",
        "---\r\ndate: 2026-01-01\r\n---\r\nCRLF body\r\n",
    ):
        marked = mark(original, rel)
        assert frontmatter.split(marked)[0]["digest"] == rel
        assert unmark(marked, rel) == original
    assert unmark("---\ndigest: other.md\n---\nx\n", rel) is None
    assert unmark("no block", rel) is None
    with pytest.raises(ValueError):
        mark("---\n- list\n---\nbody\n", rel)


def test_the_digest_slug() -> None:
    assert digest_slug("filter-check") == "digest-filter-check"
    assert digest_slug("A Weird_Key!") == "digest-a-weird-key"
    assert len(digest_slug("x" * 100)) == 64


def test_render_digest_names_the_run() -> None:
    group = _group()
    text = render_digest(
        group=group, digest_rel="journal/2026/01/01/digest-x.md", title="T", summary="S", run_id=7
    )
    meta, _ = frontmatter.split(text)
    assert meta["digest_run"] == 7 and "run_id" not in meta


# -- the index and the bundle ------------------------------------------------------------


async def test_the_index_and_the_bundle_leave_covered_entries_out(h: Harness) -> None:
    await h.run("run")
    digest_rel = "journal/2026/08/07/digest-filter-check.md"
    for rel, _ in FILTER:
        assert not_memory(f"wiki/{rel}", (h.wiki / rel).read_text())
    digest_text_ = (h.wiki / digest_rel).read_text()
    assert not not_memory(f"wiki/{digest_rel}", digest_text_)
    # A page outside the journal with the key is still memory.
    assert not not_memory("wiki/notes/a.md", "---\ndigest: x\n---\nA note.\n")
    rels = [e.rel for e in journal_entries(h.wiki, TODAY)]
    assert digest_rel in rels
    assert not any(rel in rels for rel, _ in FILTER)


async def test_the_files_source_lists_the_covered_entry_as_left_out(h: Harness) -> None:
    from joshua_core.memory.sources.files import FilesSource

    await h.run("run")
    source = FilesSource(h.data)
    uris = [doc.uri async for doc in source.list_documents()]
    assert "wiki/journal/2026/08/07/digest-filter-check.md" in uris
    assert "wiki/journal/2026/08/03/filter-check-monday-august-3.md" not in uris
    assert "wiki/journal/2026/08/03/filter-check-monday-august-3.md" in source.left_out()


# -- undo ------------------------------------------------------------------------------


async def test_undo_brings_the_entries_back_byte_for_byte(h: Harness) -> None:
    original = snapshot(h.wiki)
    await h.run("run")
    digest_rel = "journal/2026/08/07/digest-filter-check.md"

    before = snapshot(h.wiki)
    dry = await h.run("undo", dry_run=True, digest=digest_rel)
    assert changed(before, snapshot(h.wiki)) == set() and dry.written == []
    assert dry.summary == f"Dry run: digest undo: {digest_rel} (5 entries back)"

    report = await h.run("undo", digest=digest_rel)
    after = snapshot(h.wiki)
    assert digest_rel not in after
    for rel, _ in FILTER:
        assert after[rel] == original[rel]
    assert report.summary == f"digest undo: {digest_rel} (5 entries back)"
    assert h.wiki / digest_rel in report.written
    assert dict(report.sections)["Entries back"].count("- `journal/") == 5


async def test_undo_notes_an_entry_whose_line_is_gone(h: Harness) -> None:
    await h.run("run")
    digest_rel = "journal/2026/08/07/digest-filter-check.md"
    first = h.wiki / FILTER[0][0]
    first.write_text(FILTER[0][1])
    report = await h.run("undo", digest=digest_rel)
    assert report.data["back"] == [p for p, _ in FILTER[1:]]
    assert "has no `digest:` line" in dict(report.sections)["Notes"]


# -- the runner ------------------------------------------------------------------------


def _git_log(wiki: Path) -> list[str]:
    out = subprocess.run(
        ["git", "log", "--format=%s"], cwd=wiki, capture_output=True, text=True, check=True
    )
    return out.stdout.splitlines()


async def test_the_runner_runs_the_job_in_one_commit(tmp_path: Path) -> None:
    data = tmp_path / "data"
    wiki = build_wiki(data, _pages())
    wikigit.ensure_repo(wiki)
    wikigit.commit(wiki, None, "start")
    repo = FakeJobRunStore()
    settings = make_settings(DIGEST_ON)
    runner = build_job_runner(
        repo=repo,
        store=FakeStore(),
        settings=settings,
        data_dir=data,
        lock=asyncio.Lock(),
        clock=fixed_clock(),
    )
    assert "digest" in runner.names
    job = next(j for j in runner._jobs.values() if j.name == "digest")
    assert isinstance(job, DigestJob)
    job._runner = FakeDigestWorker()
    run_id = await runner.start("digest", {"mode": "run"}, dry_run=False)
    await runner.wait(run_id)
    row = repo.rows[run_id]
    assert row["error"] is None and row["commit_sha"] is not None
    assert _git_log(wiki)[0] == f"digest: run {run_id}"
    assert len(_git_log(wiki)) == 2
    digest = (wiki / "journal/2026/08/07/digest-filter-check.md").read_text()
    assert frontmatter.split(digest)[0]["digest_run"] == run_id
    assert {e.ref for e in repo.usage_events} == {f"job_run:{run_id}:digest"}
    day = NOW.date()
    report = wiki / f"journal/{day:%Y/%m/%d}/digest-{day:%Y%m%d}-{run_id}.md"
    assert report.is_file()
    # The report of the run is a job entry, not a digest entry.
    assert groups_module.is_digest_entry(frontmatter.split(report.read_text())[0]) is False
    assert not_memory(f"wiki/{report.relative_to(wiki).as_posix()}", report.read_text())


def test_the_job_is_off_by_default(tmp_path: Path) -> None:
    runner = build_job_runner(
        repo=FakeJobRunStore(),
        store=FakeStore(),
        settings=make_settings("jobs:\n  enabled: true\n"),
        data_dir=tmp_path,
        lock=asyncio.Lock(),
    )
    assert "digest" not in runner.names
