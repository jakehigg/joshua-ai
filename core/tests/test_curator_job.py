"""The curator job on a fixture wiki: the plan, a real run, a second run, and
the rules it never breaks. No model and no database: the workers, the vectors,
and the finding table are fakes."""

from __future__ import annotations

from pathlib import Path

import pytest
from curator_fakes import (
    CACTUS,
    DOT_STRAY,
    EMPTY,
    HALL,
    KITCHEN_NOTES,
    MISC_NOTES,
    POTHOS,
    REPORT,
    ROSE_CARE,
    ROSES,
    SKILL,
    SNAKE,
    TOMATOES,
    WATER,
    FakeCuratorStore,
    FakeFindingStore,
    FakeWorkers,
    build_curator_wiki,
    changed,
    snapshot,
)
from jobs_fakes import NOW, fixed_clock, make_settings
from joshua_core.jobs import JobArgsError
from joshua_core.jobs.curator.job import CuratorJob, describe, local_day, parse_apply
from joshua_core.jobs.curator.track import body_hash, needs_place, needs_refresh, needs_review
from joshua_core.memory import embed as embed_module
from joshua_shared import frontmatter

CURATOR_ON = "jobs:\n  enabled: true\n  curator:\n    enabled: true\n"


@pytest.fixture(autouse=True)
def fake_embed(monkeypatch):
    monkeypatch.setattr(embed_module, "embed", lambda text, model="": [1.0] + [0.0] * 383)


class Harness:
    def __init__(self, data: Path, extra: str = "") -> None:
        self.data = data
        self.wiki = build_curator_wiki(data)
        self.settings = make_settings(CURATOR_ON + extra)
        self.store = FakeCuratorStore()
        self.findings = FakeFindingStore()
        self.workers = FakeWorkers()

    def job(self) -> CuratorJob:
        return CuratorJob(
            store=self.store,
            findings=self.findings,
            settings=self.settings,
            data_dir=self.data,
            runner=self.workers,
        )

    async def run(self, *, dry_run: bool = False, **args):
        job = self.job()
        return await job.run_once(args=job.check_args(args), dry_run=dry_run, clock=fixed_clock())

    def text(self, rel: str) -> str:
        return (self.wiki / rel).read_text(encoding="utf-8")

    def kinds(self) -> dict[str, list[dict]]:
        out: dict[str, list[dict]] = {}
        for row in self.findings.rows.values():
            out.setdefault(row["kind"], []).append(row)
        return out


@pytest.fixture
def h(tmp_path: Path) -> Harness:
    return Harness(tmp_path / "data")


def _row(h: Harness, kind: str, path: str) -> dict:
    rows = [r for r in h.findings.by_kind(kind) if path in r["paths"]]
    assert rows, f"no {kind} finding for {path}"
    return rows[0]


# -- plan -------------------------------------------------------------------


async def test_plan_finds_everything_and_writes_nothing(h: Harness) -> None:
    before = snapshot(h.wiki)
    report = await h.run(dry_run=True)
    assert snapshot(h.wiki) == before
    assert report.written == []
    assert report.dry_run is True

    assert _row(h, "broken-link", POTHOS)["tier"] == "A"
    ambiguous = _row(h, "broken-link", ROSES)
    assert ambiguous["tier"] == "C"
    assert sorted(ambiguous["detail"]["candidates"]) == [KITCHEN_NOTES, MISC_NOTES]
    assert _row(h, "orphan", CACTUS)["tier"] == "A"
    assert _row(h, "orphan", MISC_NOTES)["tier"] == "C"
    assert _row(h, "index-line", "houseplants.md")["patch"]["line"] == (
        "- [Fern](houseplants/fern.md)"
    )
    assert _row(h, "stray", DOT_STRAY)["tier"] == "A"
    assert _row(h, "stray", EMPTY)["tier"] == "A"
    assert _row(h, "journal-date", WATER)["patch"]["date"] == "2026-10-01"
    assert _row(h, "broken-link", HALL)["tier"] == "C"
    assert _row(h, "broken-link", REPORT)["tier"] == "C"
    merge = _row(h, "duplicate", ROSES)
    assert merge["tier"] == "B"
    assert merge["patch"] == {"action": "merge", "path": ROSE_CARE, "into": ROSES}
    move = _row(h, "misplaced", TOMATOES)
    assert move["tier"] == "B"
    assert move["patch"] == {"action": "move", "path": TOMATOES, "to": "garden/tomatoes.md"}
    stale = _row(h, "refresh", SNAKE)
    assert stale["tier"] == "B"
    assert stale["detail"]["entries"] == [HALL, WATER]
    assert _row(h, "no-section", MISC_NOTES)["tier"] == "C"

    # Nothing under people/ or joshua-docs/ is a finding.
    for row in h.findings.rows.values():
        assert not any(p.startswith(("people/", "joshua-docs/")) for p in row["paths"])
    # The job report is scanned for links, but no worker reads it.
    assert all(REPORT not in paths for _, paths in h.workers.calls)
    assert report.calls == h.workers.count
    assert h.workers.names("refresh") == [[SNAKE]]
    assert h.workers.names("compare") == [[ROSE_CARE, ROSES]]
    assert "Dry run" in report.summary
    assert [s[0] for s in report.sections] == [
        "To apply (Tier A)",
        "Proposed (Tier B)",
        "Proposed (Tier C)",
        "Skipped",
        "Reviewed",
    ]
    assert report.data["counts"]["todo_a"] == len(report.data["findings"]["todo_a"])


# -- a real run -------------------------------------------------------------


async def test_run_applies_tier_a_records_the_rest_and_lists_what_it_wrote(h: Harness) -> None:
    before = snapshot(h.wiki)
    report = await h.run()
    after = snapshot(h.wiki)

    written = {p.relative_to(h.wiki).as_posix() for p in report.written}
    assert written == changed(before, after)

    # Tier A.
    assert "[roses](../garden/roses.md)" in h.text(POTHOS)
    assert "Fern" not in h.text("houseplants.md")
    assert (
        h.text("houseplants.md").rstrip().endswith("- [Cactus](/wiki/houseplants/orphan-cactus.md)")
    )
    assert not (h.wiki / DOT_STRAY).exists()
    assert (h.wiki / ".trash" / "2026-10-19" / DOT_STRAY).is_file()
    assert (h.wiki / ".trash" / "2026-10-19" / EMPTY).is_file()
    meta, _ = frontmatter.split(h.text(WATER))
    assert str(meta["date"]) == "2026-10-01"

    # Tier B is proposed, not applied, with no `apply`.
    assert (h.wiki / TOMATOES).is_file()
    assert (h.wiki / ROSE_CARE).is_file()
    assert "every 2 weeks" in h.text(SNAKE)
    assert _row(h, "refresh", SNAKE)["status"] == "open"
    assert _row(h, "misplaced", TOMATOES)["status"] == "open"

    # Tier A is recorded as applied, Tier C as open.
    assert _row(h, "broken-link", POTHOS)["status"] == "applied"
    assert _row(h, "orphan", CACTUS)["status"] == "applied"
    assert _row(h, "orphan", MISC_NOTES)["status"] == "open"

    # The tracking keys.
    day = local_day(NOW, h.settings)
    for rel in (SNAKE, POTHOS, TOMATOES, ROSES, KITCHEN_NOTES, SKILL, WATER, HALL, "Home.md"):
        text = h.text(rel)
        meta, _ = frontmatter.split(text)
        assert meta["curated"] == day, rel
        assert meta["curated_hash"] == body_hash(text), rel
        assert not needs_review(text)
    assert "curated" not in frontmatter.split(h.text(REPORT))[0]
    assert report.data["counts"]["reviewed"] > 0
    assert report.data["counts"]["applied_a"] == len(report.data["findings"]["applied_a"])
    assert [s[0] for s in report.sections][:2] == ["Applied (Tier A)", "Applied (Tier B)"]
    assert "#" in report.sections[0][1]


async def test_move_applies_only_when_apply_names_it(h: Harness) -> None:
    report = await h.run(apply="move")
    assert not (h.wiki / TOMATOES).exists()
    moved = h.text("garden/tomatoes.md")
    assert "[pothos](../houseplants/pothos.md)" in moved
    assert "[tomatoes](../garden/tomatoes.md)" in h.text(SNAKE)
    assert "houseplants/tomatoes.md" not in h.text("houseplants.md")
    assert "- [Tomatoes](/wiki/garden/tomatoes.md)" in h.text("garden.md")
    assert not needs_review(moved)
    # The merge and the refresh stay proposed.
    assert (h.wiki / ROSE_CARE).is_file()
    assert "every 2 weeks" in h.text(SNAKE)
    assert _row(h, "misplaced", TOMATOES)["status"] == "applied"
    assert report.data["counts"]["applied_b"] == 1
    written = {p.relative_to(h.wiki).as_posix() for p in report.written}
    assert {TOMATOES, "garden/tomatoes.md", "garden.md"} <= written


async def test_the_run_argument_wins_over_the_config(tmp_path: Path) -> None:
    h = Harness(tmp_path / "data", "    apply:\n      - move\n      - merge\n      - refresh\n")
    await h.run(apply=[])
    assert (h.wiki / TOMATOES).is_file()
    assert "every 2 weeks" in h.text(SNAKE)


async def test_config_apply_merges_and_refreshes(tmp_path: Path) -> None:
    h = Harness(tmp_path / "data", "    apply:\n      - merge\n      - refresh\n")
    report = await h.run()
    assert not (h.wiki / ROSE_CARE).exists()
    assert (h.wiki / ".trash" / "2026-10-19" / ROSE_CARE).is_file()
    assert "garden/rose-care.md" not in h.text("garden.md")
    assert "every 4 weeks" in h.text(SNAKE)
    assert not needs_review(h.text(SNAKE))
    assert _row(h, "refresh", SNAKE)["status"] == "applied"
    assert _row(h, "duplicate", ROSES)["status"] == "applied"
    assert report.data["counts"]["applied_b"] == 2


# -- a second run -----------------------------------------------------------


async def test_a_second_run_sends_no_page_to_a_worker(h: Harness) -> None:
    await h.run()
    applied = {r["fingerprint"] for r in h.findings.rows.values() if r["status"] == "applied"}
    first_calls = h.workers.count
    assert first_calls > 0
    h.findings.upserts.clear()
    before = snapshot(h.wiki)

    report = await h.run()
    assert h.workers.count == first_calls
    assert report.calls == 0
    assert not {u["fingerprint"] for u in h.findings.upserts} & applied
    assert snapshot(h.wiki) == before
    assert report.written == []


async def test_a_body_edit_is_reviewed_again_and_a_frontmatter_edit_is_not(h: Harness) -> None:
    await h.run()
    pothos = h.wiki / POTHOS
    pothos.write_text(pothos.read_text() + "\nIt likes the shelf.\n")
    notes = h.wiki / KITCHEN_NOTES
    notes.write_text(frontmatter.update(notes.read_text(), tags=["food"]))
    start = h.workers.count

    await h.run()
    sent = [paths for _, paths in h.workers.calls[start:]]
    assert [POTHOS] in sent
    assert all(KITCHEN_NOTES not in paths for paths in sent)


async def test_a_plan_then_a_run_with_apply_applies_the_planned_move(h: Harness) -> None:
    await h.run(dry_run=True)
    await h.run(apply="move")
    assert (h.wiki / "garden/tomatoes.md").is_file()
    assert len(h.findings.by_kind("misplaced")) == 1


async def test_an_earlier_open_tier_b_finding_applies_in_a_later_run(h: Harness) -> None:
    await h.run()
    assert (h.wiki / TOMATOES).is_file()
    report = await h.run(apply="move")
    assert (h.wiki / "garden/tomatoes.md").is_file()
    assert _row(h, "misplaced", TOMATOES)["status"] == "applied"
    assert report.data["counts"]["applied_b"] == 1


# -- what it never does -----------------------------------------------------


async def test_people_docs_and_journal_bodies_are_untouched(tmp_path: Path) -> None:
    h = Harness(tmp_path / "data", "    apply:\n      - move\n      - merge\n      - refresh\n")
    journal = {rel: frontmatter.split(h.text(rel))[1] for rel in (WATER, HALL, REPORT)}
    report_bytes = (h.wiki / REPORT).read_bytes()
    skill_meta = frontmatter.split(h.text(SKILL))[0]
    untouched = {
        rel: data
        for rel, data in snapshot(h.wiki).items()
        if rel.startswith(("people/", "joshua-docs/"))
    }
    await h.run()
    await h.run()
    for rel, data in untouched.items():
        assert (h.wiki / rel).read_bytes() == data
    for rel, body in journal.items():
        assert frontmatter.split(h.text(rel))[1] == body
    assert (h.wiki / REPORT).read_bytes() == report_bytes
    meta = frontmatter.split(h.text(SKILL))[0]
    assert set(meta) - set(skill_meta) == {"curated", "curated_hash"}
    assert {k: v for k, v in meta.items() if k in skill_meta} == skill_meta


async def test_a_patch_whose_find_occurs_twice_is_not_applied(tmp_path: Path) -> None:
    h = Harness(tmp_path / "data", "    apply:\n      - refresh\n")
    h.workers.refresh[SNAKE]["patch"] = [
        {"find": "the hall", "replace": "the bedroom", "because": HALL}
    ]
    before = h.text(SNAKE)
    await h.run()
    assert frontmatter.split(h.text(SNAKE))[1] == frontmatter.split(before)[1]
    row = _row(h, "refresh", SNAKE)
    assert row["tier"] == "C"
    assert row["status"] == "open"
    assert row["patch"]["patch"][0]["find"] == "the hall"


async def test_a_refused_worker_answer_is_a_skip(h: Harness) -> None:
    h.workers.place[POTHOS] = {"section": "Houseplants"}
    h.workers.place[CACTUS] = {"section": "Attic", "folder": "attic", "confidence": "high"}
    h.workers.place[KITCHEN_NOTES] = None
    report = await h.run()
    skipped = dict(report.sections)["Skipped"]
    assert (
        f"- The `place` answer was refused for 2 pages:\n  - `{CACTUS}`\n  - `{POTHOS}`" in skipped
    )
    assert f"- The `place` worker gave no answer for 1 page:\n  - `{KITCHEN_NOTES}`" in skipped
    assert f"The `place` answer for `{POTHOS}` was refused." in report.data["skipped"]
    for rel in (POTHOS, KITCHEN_NOTES):
        assert needs_review(h.text(rel))
    assert not h.findings.by_kind("no-section") or all(
        POTHOS not in r["paths"] for r in h.findings.by_kind("no-section")
    )


async def test_a_refresh_with_a_long_replace_is_refused(h: Harness) -> None:
    h.workers.refresh[SNAKE]["patch"][0]["replace"] = "x" * 2001
    report = await h.run()
    assert "The `refresh` answer was refused for 1 page:" in dict(report.sections)["Skipped"]
    # The place of the page ran, so it keeps that pass, and the refresh is still due.
    assert not needs_place(h.text(SNAKE))
    assert needs_refresh(h.text(SNAKE))


async def test_the_budget_limits_the_calls(tmp_path: Path) -> None:
    h = Harness(tmp_path / "data", "    max_worker_calls: 2\n")
    report = await h.run()
    assert h.workers.count == 2
    assert report.calls == 2
    assert "budget of 2 worker calls ran out" in dict(report.sections)["Skipped"]
    # The place of a page that was never curated goes first.
    assert [name for name, _ in h.workers.calls] == ["place", "place"]
    assert h.workers.names("place") == [[ROSE_CARE], [ROSES]]
    assert not needs_place(h.text(ROSES)) and needs_refresh(h.text(ROSES))
    assert needs_place(h.text(SNAKE))


async def test_entries_before_refreshed_are_left_out_and_never_curated_goes_first(
    tmp_path: Path,
) -> None:
    h = Harness(tmp_path / "data")
    path = h.wiki / SNAKE
    path.write_text(
        frontmatter.update(path.read_text(), curated="2026-10-15", refreshed="2026-10-15")
    )
    await h.run()
    # Both entries about the snake plant are dated before 2026-10-15.
    assert h.workers.names("refresh") == []
    assert h.workers.calls[0] == ("place", [ROSE_CARE])
    assert not needs_refresh(h.text(SNAKE))


async def test_the_curated_day_does_not_hide_an_entry_from_refresh(tmp_path: Path) -> None:
    h = Harness(tmp_path / "data")
    path = h.wiki / SNAKE
    path.write_text(frontmatter.update(path.read_text(), curated="2026-10-15"))
    await h.run(dry_run=True)
    assert h.workers.names("refresh") == [[SNAKE]]


async def test_no_vectors_means_no_compare(h: Harness) -> None:
    h.store.fail = True
    report = await h.run(dry_run=True)
    assert h.workers.names("compare") == []
    assert "the index gave no vectors" in dict(report.sections)["Skipped"]


async def test_a_dismissed_finding_is_not_applied(h: Harness) -> None:
    await h.run(dry_run=True)
    row = _row(h, "broken-link", POTHOS)
    await h.findings.set_finding_status(row["id"], "dismissed", at=NOW, resolved_by="alex")
    report = await h.run()
    assert "[roses](roses.md)" in h.text(POTHOS)
    assert f"Finding #{row['id']} (broken-link) is dismissed" in dict(report.sections)["Skipped"]


async def test_an_open_finding_on_a_changed_page_becomes_stale(h: Harness) -> None:
    await h.run()
    row = _row(h, "orphan", MISC_NOTES)
    (h.wiki / MISC_NOTES).unlink()
    await h.run()
    assert h.findings.rows[row["id"]]["status"] == "stale"


async def test_trash_off_records_a_stray_as_tier_c(tmp_path: Path) -> None:
    h = Harness(tmp_path / "data", "    trash: false\n")
    await h.run()
    assert (h.wiki / DOT_STRAY).is_file()
    assert _row(h, "stray", DOT_STRAY)["tier"] == "C"


async def test_no_sections_list_means_no_place(h: Harness) -> None:
    (h.wiki / "Home.md").write_text("# Home\n\n- [Houseplants](houseplants.md)\n")
    report = await h.run(dry_run=True)
    assert h.workers.names("place") == []
    assert "no `## Sections` list" in dict(report.sections)["Skipped"]


def test_check_args(h: Harness) -> None:
    job = h.job()
    assert job.check_args({}) == {}
    assert job.check_args({"apply": "move, refresh"}) == {"apply": ["move", "refresh"]}
    assert job.check_args({"apply": ["merge"]}) == {"apply": ["merge"]}
    assert job.check_args({"apply": ""}) == {"apply": []}
    with pytest.raises(JobArgsError):
        job.check_args({"apply": "delete"})
    with pytest.raises(JobArgsError):
        job.check_args({"apply": 3})
    with pytest.raises(JobArgsError):
        job.check_args({"topic": "x.md"})
    assert parse_apply(["move", "move"]) == ["move"]


def test_describe_names_each_kind() -> None:
    from joshua_core.jobs.curator.findings import Finding

    cases = [
        Finding(
            "conflict", "C", ["a.md", "b.md"], detail={"claims": [{"a_says": "x", "b_says": "y"}]}
        ),
        Finding("orphan", "C", ["a.md"]),
        Finding("stray", "C", ["people/.DS_Store"]),
        Finding("duplicate", "C", ["a.md", "b.md"]),
        Finding("no-section", "C", ["a.md"], detail={"section": "none", "reason": "odd"}),
        Finding("other", "C", ["a.md"], detail={"x": 1}),
        Finding(
            "journal-date",
            "A",
            ["j.md"],
            patch={"action": "set-date", "path": "j.md", "date": "2026-10-01"},
        ),
        Finding(
            "misplaced",
            "B",
            ["a.md"],
            detail={"reason": "r", "refused": "taken"},
            patch={"action": "move", "path": "a.md", "to": "b/a.md"},
        ),
    ]
    for finding in cases:
        line = describe(finding)
        assert line.startswith("- #None ")
    assert "Not applied: `taken`." in describe(cases[-1])


async def test_two_same_links_are_one_finding_and_both_are_fixed(h: Harness) -> None:
    page = h.wiki / POTHOS
    page.write_text(page.read_text() + "Again [roses](roses.md).\n")
    report = await h.run()
    assert len(h.findings.by_kind("broken-link")) == 4
    assert h.text(POTHOS).count("[roses](../garden/roses.md)") == 2
    assert "Not applied" not in report.to_markdown()


async def test_a_page_whose_frontmatter_is_not_a_mapping_is_skipped(h: Harness) -> None:
    page = h.wiki / CACTUS
    page.write_text("---\n- a list\n---\n# Cactus\n")
    report = await h.run()
    assert f"`{CACTUS}` was not reviewed" in dict(report.sections)["Skipped"]
    assert all(CACTUS not in paths for _, paths in h.workers.calls)


async def test_the_report_holds_no_link_of_its_own(tmp_path: Path) -> None:
    from joshua_core.jobs.curator.scan import parse_links

    h = Harness(tmp_path / "data")
    h.workers.refresh[SNAKE]["patch"][0]["replace"] = "every [4 weeks](nowhere.md)"
    report = await h.run()
    assert parse_links(report.to_markdown()) == []


async def test_a_refresh_is_only_proposed_when_config_and_args_leave_it_out(h: Harness) -> None:
    report = await h.run(apply="move,merge")
    assert "every 2 weeks" in h.text(SNAKE)
    row = _row(h, "refresh", SNAKE)
    assert row["status"] == "open"
    assert row["id"] in report.data["findings"]["proposed_b"]


# -- the budget order, the run arguments, and the report ----------------------


def _order(h: Harness) -> list[str]:
    """The workers in call order, with one entry for each run of the same one."""
    out: list[str] = []
    for name, _ in h.workers.calls:
        if not out or out[-1] != name:
            out.append(name)
    return out


async def test_the_budget_goes_to_place_then_compare_then_refresh(h: Harness) -> None:
    await h.run(dry_run=True)
    assert _order(h) == ["place", "compare", "refresh"]


async def test_a_page_curated_before_is_placed_after_the_refresh(tmp_path: Path) -> None:
    h = Harness(tmp_path / "data")
    path = h.wiki / POTHOS
    old = frontmatter.update(path.read_text(), curated="2026-09-01", curated_hash="000000000000")
    path.write_text(old)
    await h.run(dry_run=True)
    assert _order(h) == ["place", "compare", "refresh", "place"]
    assert h.workers.calls[-1] == ("place", [POTHOS])


async def test_a_small_budget_is_spent_on_place_first(tmp_path: Path) -> None:
    h = Harness(tmp_path / "data")
    report = await h.run(dry_run=True, max_worker_calls="3")
    assert [name for name, _ in h.workers.calls] == ["place"] * 3
    assert report.calls == 3
    assert report.data["max_worker_calls"] == 3
    assert "budget of 3 worker calls ran out" in dict(report.sections)["Skipped"]


async def test_max_worker_calls_zero_runs_the_scan_only(h: Harness) -> None:
    report = await h.run(dry_run=True, max_worker_calls=0)
    assert h.workers.calls == []
    assert h.findings.by_kind("broken-link")
    assert report.calls == 0


async def test_paths_scope_the_workers_and_not_the_scan(h: Harness) -> None:
    report = await h.run(dry_run=True, paths="garden/")
    called = {p for _, paths in h.workers.calls for p in paths}
    assert called <= {ROSES, ROSE_CARE}
    assert h.workers.names("compare") == [[ROSE_CARE, ROSES]]
    assert h.workers.names("refresh") == []
    # The scan still reads every page.
    assert _row(h, "broken-link", POTHOS)["tier"] == "A"
    assert report.data["paths"] == ["garden/"]
    assert report.data["counts"]["reviewed"] == 2


async def test_a_scoped_run_tracks_only_the_pages_in_scope(h: Harness) -> None:
    await h.run(paths=["houseplants/"])
    assert not needs_review(h.text(SNAKE))
    assert needs_review(h.text(ROSES))
    assert needs_review(h.text("Home.md"))


def test_the_new_run_arguments(h: Harness) -> None:
    job = h.job()
    assert job.check_args({"max_worker_calls": "20"}) == {"max_worker_calls": 20}
    assert job.check_args({"max_worker_calls": 0}) == {"max_worker_calls": 0}
    assert job.check_args({"paths": "houseplants/, garden/"}) == {
        "paths": ["houseplants/", "garden/"]
    }
    assert job.check_args({"paths": ["garden/", "garden/"]}) == {"paths": ["garden/"]}
    assert job.check_args({"paths": "", "max_worker_calls": ""}) == {}
    for bad in (
        {"max_worker_calls": -1},
        {"max_worker_calls": "x"},
        {"max_worker_calls": True},
        {"max_worker_calls": 1.5},
        {"paths": ","},
        {"paths": 3},
        {"paths": "/garden/"},
        {"paths": "../garden/"},
        {"paths": "garden/../people/"},
        {"paths": ".trash/"},
        {"paths": "a\\b"},
    ):
        with pytest.raises(JobArgsError):
            job.check_args(bad)


async def test_failed_calls_are_grouped_by_worker_with_a_count(h: Harness) -> None:
    for rel in (SNAKE, POTHOS, CACTUS):
        h.workers.place[rel] = None
    h.workers.compare[(ROSE_CARE, ROSES)] = None
    report = await h.run(dry_run=True)
    skipped = dict(report.sections)["Skipped"]
    assert (
        "- The `place` worker gave no answer for 3 pages:\n"
        f"  - `{CACTUS}`\n  - `{POTHOS}`\n  - `{SNAKE}`"
    ) in skipped
    assert (
        f"- The `compare` worker gave no answer for 1 pair:\n  - `{ROSE_CARE}` and `{ROSES}`"
        in (skipped)
    )
    assert skipped.count("gave no answer for") == 2
    assert report.data["counts"]["skipped"] == len(report.data["skipped"])


def test_a_patch_pair_is_shown_byte_for_byte_in_a_fenced_block() -> None:
    from joshua_core.jobs.curator.findings import Finding
    from joshua_core.jobs.curator.scan import parse_links

    pair = {
        "find": "| `minio-data-vol` | [old](old.md) |",
        "replace": "| `minio-data` |\n```\nnot a fence end\n```",
        "because": "the entry of 2026-10-04",
    }
    finding = Finding(
        "refresh",
        "B",
        ["a.md"],
        detail={"status": "stale", "confidence": "high"},
        patch={"action": "refresh", "path": "a.md", "patch": [pair]},
    )
    text = describe(finding)
    assert "  find: | `minio-data-vol` | [old](old.md) |" in text
    assert "  replace: | `minio-data` |" in text
    assert "'minio-data-vol'" not in text
    assert "  ````text\n" in text and text.rstrip().endswith("````")
    assert parse_links("# r\n\n" + text + "\n\nAfter [x](x.md).\n")[0].target == "x.md"
    assert len(parse_links(text)) == 0


def test_patch_block_takes_a_three_backtick_fence_for_plain_text() -> None:
    from joshua_core.jobs.curator.job import patch_block

    block = patch_block({"find": "a", "replace": "b"}, indent="")
    assert block == "```text\nfind: a\nreplace: b\n```"


# -- the two passes and the pair table ----------------------------------------

PLANTS = ("houseplants/fern.md", "houseplants/ivy.md", "houseplants/moss.md")


def _passes_wiki() -> dict[str, str]:
    """Three topic pages, each with one journal entry that names it, and no pair."""
    pages = {
        "Home.md": "# Home\n\n## Sections\n\n- [Houseplants](houseplants.md): inside\n",
        "houseplants.md": "# Houseplants\n\nThe plants inside.\n\n"
        + "".join(f"- [{rel[12:-3].title()}]({rel})\n" for rel in PLANTS),
    }
    for day, rel in enumerate(PLANTS, start=1):
        name = rel[12:-3].title()
        pages[rel] = f"# {name}\n\nWater the {name.lower()} every week.\n"
        pages[f"journal/2026/10/0{day}/{name.lower()}.md"] = (
            f"---\ndate: 2026-10-0{day}\n---\nThe {name.lower()} is dry by day five.\n"
        )
    return pages


@pytest.fixture
def passes(tmp_path: Path) -> Harness:
    h = Harness(tmp_path / "data")
    for path in sorted(h.wiki.rglob("*"), reverse=True):
        if path.is_file():
            path.unlink()
    build_curator_wiki(h.data, _passes_wiki())
    h.store.vectors = {}
    for rel in PLANTS:
        h.workers.place[rel] = {
            "section": "Houseplants",
            "folder": "houseplants",
            "confidence": "high",
            "reason": "It fits.",
        }
    return h


async def test_a_budget_of_place_calls_keeps_the_place_pass(passes: Harness) -> None:
    h = passes
    report = await h.run(max_worker_calls=3)
    assert h.workers.names("place") == [[rel] for rel in PLANTS]
    assert h.workers.names("refresh") == []
    for rel in PLANTS:
        assert not needs_place(h.text(rel)), rel
        assert needs_refresh(h.text(rel)), rel
        assert "refreshed" not in frontmatter.split(h.text(rel))[0]
    reviewed = dict(report.sections)["Reviewed"]
    assert "pages got `curated` keys, and 0 pages got `refreshed` keys." in reviewed
    assert report.data["counts"]["refreshed"] == 0
    assert report.data["counts"]["curated"] >= 3

    # The next run with the same budget sends no `place` and spends it on `refresh`.
    start = h.workers.count
    report = await h.run(max_worker_calls=3)
    later = h.workers.calls[start:]
    assert [name for name, _ in later] == ["refresh"] * 3
    assert sorted(paths[0] for _, paths in later) == list(PLANTS)
    for rel in PLANTS:
        meta, _ = frontmatter.split(h.text(rel))
        assert meta["refreshed_hash"] == meta["curated_hash"] == body_hash(h.text(rel))
        assert meta["refreshed"] == local_day(NOW, h.settings)
    assert (
        "0 pages got `curated` keys, and 3 pages got `refreshed` keys."
        in (dict(report.sections)["Reviewed"])
    )

    # A third run has nothing to do.
    start = h.workers.count
    await h.run(max_worker_calls=3)
    assert h.workers.count == start


async def test_a_changed_body_resets_both_passes(passes: Harness) -> None:
    h = passes
    await h.run()
    fern = h.wiki / PLANTS[0]
    assert not needs_place(fern.read_text()) and not needs_refresh(fern.read_text())
    fern.write_text(fern.read_text() + "\nIt likes the bathroom.\n")
    assert needs_place(fern.read_text()) and needs_refresh(fern.read_text())
    start = h.workers.count
    await h.run()
    # The entry about the fern is older than its `refreshed` day, so the
    # refresh pass has nothing to read and costs no call.
    assert h.workers.calls[start:] == [("place", [PLANTS[0]])]
    assert not needs_place(fern.read_text()) and not needs_refresh(fern.read_text())


async def test_a_dry_run_counts_both_passes(passes: Harness) -> None:
    report = await passes.run(dry_run=True, max_worker_calls=4)
    assert report.data["counts"]["refreshed"] == 1
    assert (
        "pages would get `curated` keys, and 1 pages would get `refreshed` keys."
        in (dict(report.sections)["Reviewed"])
    )


async def test_a_page_with_no_entry_to_read_gets_the_refreshed_pair(passes: Harness) -> None:
    h = passes
    for path in (h.wiki / "journal").rglob("*.md"):
        path.unlink()
    await h.run()
    assert h.workers.names("refresh") == []
    for rel in PLANTS:
        assert not needs_refresh(h.text(rel)), rel


async def test_a_compare_pair_runs_once_while_it_is_unchanged(h: Harness) -> None:
    await h.run(dry_run=True)
    assert h.workers.names("compare") == [[ROSE_CARE, ROSES]]
    assert len(h.findings.pairs) == 1
    # The pages still need review after a dry run, but the pair is not read again.
    start = h.workers.count
    report = await h.run(dry_run=True)
    assert [name for name, _ in h.workers.calls[start:]].count("compare") == 0
    assert report.data["counts"]["pairs_unchanged"] == 1
    # A change to one page of the pair sends it to `compare` again.
    rose = h.wiki / ROSES
    rose.write_text(rose.read_text() + "\nFeed in May.\n")
    start = h.workers.count
    await h.run(dry_run=True)
    assert h.workers.names("compare")[-1] == [ROSE_CARE, ROSES]
    assert [name for name, _ in h.workers.calls[start:]].count("compare") == 1


async def test_a_pair_with_no_answer_is_not_recorded(h: Harness) -> None:
    h.workers.compare[(ROSE_CARE, ROSES)] = None
    await h.run(dry_run=True)
    assert h.findings.pairs == {}


async def test_a_pair_table_that_fails_means_each_pair_is_compared(h: Harness) -> None:
    async def broken(fingerprints: list[str]) -> dict:
        raise RuntimeError("no database")

    async def broken_upsert(**kwargs: object) -> None:
        raise RuntimeError("no database")

    h.findings.get_curator_pairs = broken  # type: ignore[method-assign]
    h.findings.upsert_curator_pair = broken_upsert  # type: ignore[method-assign]
    await h.run(dry_run=True)
    await h.run(dry_run=True)
    assert h.workers.names("compare") == [[ROSE_CARE, ROSES], [ROSE_CARE, ROSES]]


async def test_a_refresh_answer_does_not_make_a_place_finding_stale(h: Harness) -> None:
    entry = h.wiki / "journal/2026/10/03/tomatoes.md"
    entry.parent.mkdir(parents=True, exist_ok=True)
    entry.write_text("---\ndate: 2026-10-03\n---\nThe tomatoes need a stake.\n")
    await h.run(dry_run=True)
    row = _row(h, "misplaced", TOMATOES)
    # Only the refresh pass of the page is due now.
    page = h.wiki / TOMATOES
    page.write_text(frontmatter.update(page.read_text(), curated_hash=body_hash(page.read_text())))
    start = h.workers.count
    await h.run(dry_run=True)
    assert ("refresh", [TOMATOES]) in h.workers.calls[start:]
    assert ("place", [TOMATOES]) not in h.workers.calls[start:]
    assert h.findings.rows[row["id"]]["status"] == "open"
