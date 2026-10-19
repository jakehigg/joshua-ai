"""The `curator` job: one run of the scan, the workers, and the apply step.

A run does this, in order:

1. The scan (`scan.py`) over the whole wiki, every run.
2. The workers (`workers.py`), on the topic pages whose body changed since
   their last review, within `jobs.curator.max_worker_calls`: `refresh` for a
   page with journal entries about it, then `place` for each page, then
   `compare` for each pair of similar pages. A page that was never reviewed
   goes first, then the page with the oldest `curated` day.
3. The apply step (`apply.py`), when the run is not a dry run: every Tier A
   finding, and each Tier B finding whose action the run may apply. A
   dismissed finding is not applied.
4. The tracking keys, when the run is not a dry run: on each page the run
   reviewed, and on each page it changed that had current keys before.
5. The findings, into `curator_finding`, in a dry run too.

A page is reviewed when every worker call due for it answered. A page that is
not a topic page (an index page, a skill, a journal entry) needs no worker,
so the scan alone reviews it. A page that the budget or a failed call left
out keeps its old keys and is reviewed in a later run.

Arguments: `{"apply": ["move", "merge", "refresh"]}`, or the same as a string
with commas. It replaces `jobs.curator.apply` for the run.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from joshua_shared import frontmatter, layout
from joshua_shared.config import CURATOR_ACTIONS, JoshuaConfig
from joshua_shared.log import get_logger

from joshua_core.engine.ephemeral import Runner
from joshua_core.jobs.bundle import BundleStore, build_bundle
from joshua_core.jobs.curator import scan, workers
from joshua_core.jobs.curator.apply import ApplyRefused, Workspace, apply_patch, finds_once
from joshua_core.jobs.curator.findings import (
    TIER_A,
    TIER_B,
    TIER_C,
    WORKER_KINDS,
    Finding,
    FindingStore,
    from_row,
)
from joshua_core.jobs.curator.scan import Pair, Tree
from joshua_core.jobs.curator.track import body_hash, curated_day, is_untouched, may_track
from joshua_core.jobs.curator.track import needs_review as page_needs_review
from joshua_core.jobs.runner import Clock, Job, JobArgsError, RunReport, current_run_id
from joshua_core.jobs.topics import Topic, list_topics

logger = get_logger("jobs.curator")

# The rows a run reads back from `curator_finding` at most, per status.
ROW_LIMIT = 100_000
# The order in which a run applies its findings.
_APPLY_ORDER = (
    "trash",
    "set-date",
    "relink",
    "drop-line",
    "index-add",
    "refresh",
    "move",
    "merge",
)
# A patch key that names a wiki path, which a move earlier in the run can change.
_PATH_KEYS = ("path", "to", "into", "page")
_SHOW_CHARS = 300


class CuratorStore(BundleStore, Protocol):
    """The index queries of the curator. `MemoryStore` implements them."""

    async def kb_mean_vectors(
        self, paths: list[str], *, source: str = "files"
    ) -> dict[str, list[float]]: ...


def parse_apply(value: Any) -> list[str]:
    """The `apply` argument as a list of actions. Raises `JobArgsError`."""
    if isinstance(value, str):
        items = [part.strip() for part in value.split(",") if part.strip()]
    elif isinstance(value, list) and all(isinstance(v, str) for v in value):
        items = [v.strip() for v in value if v.strip()]
    else:
        raise JobArgsError("apply is a list of actions, or a string with commas")
    unknown = [item for item in items if item not in CURATOR_ACTIONS]
    if unknown:
        raise JobArgsError(
            f"unknown curator action: {', '.join(unknown)}; use {', '.join(CURATOR_ACTIONS)}"
        )
    return list(dict.fromkeys(items))


def local_day(now: datetime, settings: JoshuaConfig) -> date:
    """The day of `now` in `timezone`."""
    if now.tzinfo is None:
        return now.date()
    return now.astimezone(ZoneInfo(settings.timezone)).date()


@dataclass
class _Run:
    """The state of one run."""

    tree: Tree
    day: date
    now: datetime
    apply: list[str]
    budget: workers.Budget
    findings: list[Finding] = field(default_factory=list)
    incomplete: set[str] = field(default_factory=set)
    answered: set[str] = field(default_factory=set)
    skipped: list[str] = field(default_factory=list)
    unsent: int = 0


class CuratorJob(Job):
    name = "curator"

    def __init__(
        self,
        *,
        store: CuratorStore,
        findings: FindingStore,
        settings: JoshuaConfig,
        data_dir: Path | str,
        runner: Runner | None = None,
    ) -> None:
        self._store = store
        self._findings = findings
        self._settings = settings
        self._data_dir = Path(data_dir)
        self._runner = runner

    @property
    def _wiki(self) -> Path:
        return layout.wiki_root(self._data_dir)

    def check_args(self, args: dict[str, Any]) -> dict[str, Any]:
        unknown = sorted(set(args) - {"apply"})
        if unknown:
            raise JobArgsError(f"unknown argument: {', '.join(unknown)}")
        if "apply" in args:
            return {"apply": parse_apply(args["apply"])}
        return {}

    # -- the run ------------------------------------------------------------

    async def run_once(self, *, args: dict[str, Any], dry_run: bool, clock: Clock) -> RunReport:
        cfg = self._settings.jobs.curator
        now = clock()
        tree = scan.load_tree(self._wiki)
        result = scan.scan_tree(tree, trash=cfg.trash)
        run = _Run(
            tree=tree,
            day=local_day(now, self._settings),
            now=now,
            apply=list(args["apply"]) if "apply" in args else list(cfg.apply),
            budget=workers.Budget(cfg.max_worker_calls),
            findings=list(result.findings),
        )

        texts = {rel: tree.text(rel) for rel in tree.pages}
        due = {
            rel for rel, text in texts.items() if may_track(rel, text) and page_needs_review(text)
        }
        for rel in sorted(due):
            if not _keys_fit(texts[rel]):
                due.discard(rel)
                run.skipped.append(
                    f"`{rel}` was not reviewed: its frontmatter is not a YAML mapping."
                )
        topics = [
            t
            for t in list_topics(self._wiki, self._settings.jobs.topics.exclude)
            if t.path in tree.pages
        ]
        review = sorted(
            (t for t in topics if t.path in due),
            key=lambda t: (
                curated_day(texts[t.path]) is not None,
                curated_day(texts[t.path]) or date.min,
                t.path,
            ),
        )
        review_paths = {t.path for t in review}

        await self._refresh_pass(run, review, texts)
        sections = scan.section_map(tree)
        if sections:
            await self._place_pass(run, review, texts, sections)
        elif review:
            run.skipped.append("No page was placed: `Home.md` has no `## Sections` list.")
        pairs = await self._pairs(run, topics, review_paths)
        await self._compare_pass(run, pairs, review_paths, texts)
        if run.unsent:
            run.skipped.append(
                f"The budget of {run.budget.limit} worker calls ran out: "
                f"{run.unsent} pages or pairs were not sent to a worker."
            )

        run.findings = _unique(run.findings)
        reviewed = sorted(
            rel for rel in due if rel not in run.incomplete and rel not in tree.strays
        )
        dismissed = {
            row["fingerprint"]
            for row in await self._findings.list_findings("dismissed", ROW_LIMIT)
            if row.get("fingerprint")
        }
        open_rows = await self._findings.list_findings("open", ROW_LIMIT)

        applied: list[Finding] = []
        ws: Workspace | None = None
        tracked = 0
        if not dry_run:
            ws = Workspace(self._wiki, run.day)
            applied = self._apply(run, ws, dismissed, open_rows, review_paths)
            for rel in reviewed:
                current = ws.current(rel)
                if current is not None and ws.track(current):
                    tracked += 1
            for rel in sorted(ws.clean):
                ws.track(rel)

        final_tree = scan.load_tree(self._wiki) if ws is not None else tree
        await self._record(run, applied, dismissed, open_rows, final_tree, ws)
        return self._report(run, applied, dismissed, ws, tracked, reviewed, dry_run)

    # -- workers ------------------------------------------------------------

    def _model(self) -> str | None:
        return workers.worker_model(self._settings)

    def _skip(self, run: _Run, worker: str, label: str, outcome: str) -> None:
        if outcome == "refused":
            run.skipped.append(f"The `{worker}` answer for {label} was refused.")
        else:
            run.skipped.append(f"The `{worker}` worker gave no answer for {label}.")

    async def _entries(self, run: _Run, topic: Topic, text: str) -> list[tuple[str, str]] | None:
        """The journal entries about a topic since its `curated` day, or None
        when the bundle cannot be built."""
        try:
            bundle = await build_bundle(
                topic,
                wiki_root=self._wiki,
                repo=self._store,
                settings=self._settings,
                now=run.now,
            )
        except Exception as exc:  # noqa: BLE001 — one page must not stop the run
            logger.warning({"message": "curator bundle failed", "error": str(exc)})
            return None
        since = curated_day(text)
        out: list[tuple[str, str]] = []
        for rel in bundle.paths:
            if scan.section_of(rel) != scan.JOURNAL:
                continue
            day = scan.journal_day(run.tree, rel)
            if since is not None and (day is None or day <= since):
                continue
            out.append((rel, run.tree.text(rel)))
        return out

    async def _refresh_pass(self, run: _Run, review: list[Topic], texts: dict[str, str]) -> None:
        for topic in review:
            if run.budget.left == 0:
                run.incomplete.add(topic.path)
                run.unsent += 1
                continue
            text = texts[topic.path]
            entries = await self._entries(run, topic, text)
            if entries is None:
                run.incomplete.add(topic.path)
                run.skipped.append(f"The journal entries of `{topic.path}` could not be read.")
                continue
            if not entries:
                continue
            run.budget.spend()
            got = await workers.refresh(
                topic.path, text, entries, model=self._model(), runner=self._runner
            )
            if got.answer is None:
                run.incomplete.add(topic.path)
                self._skip(run, "refresh", f"`{topic.path}`", got.outcome)
                continue
            run.answered.add(topic.path)
            self._on_refresh(run, topic.path, text, [p for p, _ in entries], got.answer)

    def _on_refresh(
        self,
        run: _Run,
        rel: str,
        text: str,
        entries: list[str],
        answer: workers.RefreshAnswer,
    ) -> None:
        if answer.status == "current":
            return
        pairs = [p.model_dump() for p in answer.patch]
        patch = {"action": "refresh", "path": rel, "patch": pairs} if pairs else None
        good = (
            answer.status == "stale"
            and answer.confidence == "high"
            and bool(pairs)
            and finds_once(text, pairs)
        )
        detail = {"status": answer.status, "confidence": answer.confidence, "entries": entries}
        key = json.dumps(pairs, sort_keys=True) if pairs else answer.status
        tier = TIER_B if good else TIER_C
        run.findings.append(Finding("refresh", tier, [rel], key, detail, patch))

    async def _place_pass(
        self,
        run: _Run,
        review: list[Topic],
        texts: dict[str, str],
        sections: list[scan.Section],
    ) -> None:
        for topic in review:
            if topic.path in run.incomplete:
                continue
            if not run.budget.spend():
                run.incomplete.add(topic.path)
                run.unsent += 1
                continue
            got = await workers.place(
                topic.path, texts[topic.path], sections, model=self._model(), runner=self._runner
            )
            if got.answer is None:
                run.incomplete.add(topic.path)
                self._skip(run, "place", f"`{topic.path}`", got.outcome)
                continue
            run.answered.add(topic.path)
            self._on_place(run, topic.path, got.answer)

    def _on_place(self, run: _Run, rel: str, answer: workers.PlaceAnswer) -> None:
        detail = {
            "section": answer.section,
            "folder": answer.folder,
            "confidence": answer.confidence,
            "reason": answer.reason,
        }
        if answer.section == workers.NO_SECTION:
            run.findings.append(Finding("no-section", TIER_C, [rel], "", detail))
            return
        folder = workers.normalize_folder(answer.folder)
        current = PurePosixPath(rel).parent.as_posix()
        if folder == ("" if current == "." else current):
            return
        name = PurePosixPath(rel).name
        target = f"{folder}/{name}" if folder else name
        valid = (
            (folder == "" or folder in run.tree.dirs)
            and not is_untouched(target)
            and scan.section_of(target) not in scan.OUT_OF_SCOPE
            and target not in run.tree.files
        )
        patch = {"action": "move", "path": rel, "to": target} if valid else None
        tier = TIER_B if valid and answer.confidence == "high" else TIER_C
        run.findings.append(Finding("misplaced", tier, [rel], folder, detail, patch))

    async def _pairs(self, run: _Run, topics: list[Topic], review: set[str]) -> list[Pair]:
        cfg = self._settings.jobs.curator
        if not review:
            return []
        prefix = "wiki/"
        try:
            vectors = await self._store.kb_mean_vectors([prefix + t.path for t in topics])
        except Exception as exc:  # noqa: BLE001 — no vectors means no pairs, not a failed run
            logger.warning({"message": "curator vectors failed", "error": str(exc)})
            run.skipped.append("No page was compared: the index gave no vectors.")
            return []
        by_path = {p[len(prefix) :]: v for p, v in vectors.items() if p.startswith(prefix)}
        pairs = scan.similar_pairs(
            by_path, duplicate_sim=cfg.duplicate_sim, overlap_sim=cfg.overlap_sim
        )
        return [p for p in pairs if p.a in review or p.b in review]

    async def _compare_pass(
        self, run: _Run, pairs: list[Pair], review: set[str], texts: dict[str, str]
    ) -> None:
        for pair in pairs:
            members = {pair.a, pair.b} & review
            if members & run.incomplete:
                continue
            if not run.budget.spend():
                run.incomplete |= members
                run.unsent += 1
                continue
            got = await workers.compare(
                (pair.a, texts[pair.a]),
                (pair.b, texts[pair.b]),
                model=self._model(),
                runner=self._runner,
            )
            if got.answer is None:
                run.incomplete |= members
                self._skip(run, "compare", f"`{pair.a}` and `{pair.b}`", got.outcome)
                continue
            run.answered |= members
            self._on_compare(run, pair, got.answer)

    def _on_compare(self, run: _Run, pair: Pair, answer: workers.CompareAnswer) -> None:
        paths = sorted([pair.a, pair.b])
        claims = [c.model_dump() for c in answer.claims]
        detail = {
            "relation": answer.relation,
            "confidence": answer.confidence,
            "sim": round(pair.sim, 4),
            "claims": claims,
        }
        if answer.relation == "conflict":
            key = json.dumps(claims, sort_keys=True)
            run.findings.append(Finding("conflict", TIER_C, paths, key, detail))
            return
        if answer.relation != "duplicate" or answer.confidence != "high":
            return
        if answer.canonical not in ("a", "b"):
            run.findings.append(Finding("duplicate", TIER_C, paths, "", detail))
            return
        keep, drop = (pair.a, pair.b) if answer.canonical == "a" else (pair.b, pair.a)
        detail["canonical"] = keep
        patch = {"action": "merge", "path": drop, "into": keep}
        run.findings.append(Finding("duplicate", TIER_B, paths, keep, detail, patch))

    # -- apply ----------------------------------------------------------------

    def _apply(
        self,
        run: _Run,
        ws: Workspace,
        dismissed: set[str],
        open_rows: list[dict[str, Any]],
        review: set[str],
    ) -> list[Finding]:
        """Apply the findings the run may apply. Returns the applied ones."""
        seen = {f.fingerprint for f in run.findings}
        earlier = [
            from_row(row)
            for row in open_rows
            if row.get("tier") == TIER_B
            and row.get("fingerprint") not in seen
            and row.get("patch")
            and not set(row.get("paths") or []) & review
        ]
        for finding in earlier:
            finding.detail["earlier"] = True
        candidates = [
            f
            for f in [*run.findings, *earlier]
            if f.patch is not None
            and (f.tier == TIER_A or (f.tier == TIER_B and f.action in run.apply))
        ]
        candidates.sort(key=lambda f: _APPLY_ORDER.index(f.action or "merge"))
        applied: list[Finding] = []
        for finding in candidates:
            if finding.fingerprint in dismissed:
                continue
            assert finding.patch is not None
            patch = dict(finding.patch)
            gone = False
            for key in _PATH_KEYS:
                if isinstance(patch.get(key), str):
                    now = ws.current(patch[key])
                    if now is None:
                        gone = True
                    else:
                        patch[key] = now
            try:
                if gone:
                    raise ApplyRefused("a page of this finding went to the trash in this run")
                apply_patch(ws, patch)
            except ApplyRefused as exc:
                finding.detail["refused"] = str(exc)
                if finding.id is None:
                    finding.tier = TIER_C
                continue
            applied.append(finding)
        if earlier:
            run.findings.extend(f for f in earlier if f in applied)
        return applied

    # -- record ---------------------------------------------------------------

    async def _record(
        self,
        run: _Run,
        applied: list[Finding],
        dismissed: set[str],
        open_rows: list[dict[str, Any]],
        tree: Tree,
        ws: Workspace | None,
    ) -> None:
        run_id = current_run_id()
        applied_ids = {id(f) for f in applied}
        seen_ids: set[int] = set()
        for finding in run.findings:
            if finding.id is None:
                paths = [ws.current(p) or p for p in finding.paths] if ws else finding.paths
                finding.detail["hashes"] = {
                    p: body_hash(tree.text(p)) for p in paths if p in tree.pages
                }
                status = "applied" if id(finding) in applied_ids else "open"
                row = await self._findings.upsert_finding(
                    run_id=run_id,
                    kind=finding.kind,
                    tier=finding.tier,
                    fingerprint=finding.fingerprint,
                    paths=finding.paths,
                    detail=finding.detail,
                    patch=finding.patch,
                    status=status,
                    seen_at=run.now,
                )
                finding.id = int(row["id"])
                finding.status = row.get("status") or status
            seen_ids.add(finding.id)
            if id(finding) in applied_ids and finding.status != "applied":
                await self._findings.set_finding_status(
                    finding.id, "applied", at=run.now, resolved_by=self.name
                )
                finding.status = "applied"

        for row in open_rows:
            rid = int(row["id"])
            if rid in seen_ids:
                continue
            paths = list(row.get("paths") or [])
            superseded = row.get("kind") in WORKER_KINDS and bool(set(paths) & run.answered)
            if superseded or _changed(row, tree):
                await self._findings.set_finding_status(rid, "stale", at=run.now)

    # -- report ---------------------------------------------------------------

    def _report(
        self,
        run: _Run,
        applied: list[Finding],
        dismissed: set[str],
        ws: Workspace | None,
        tracked: int,
        reviewed: list[str],
        dry_run: bool,
    ) -> RunReport:
        applied_ids = {id(f) for f in applied}
        done_a = [f for f in applied if f.tier == TIER_A]
        done_b = [f for f in applied if f.tier == TIER_B]
        rest = [f for f in run.findings if id(f) not in applied_ids]
        held = [f for f in rest if f.fingerprint in dismissed or f.status == "dismissed"]
        open_ = [f for f in rest if f not in held]
        todo_a = [f for f in open_ if f.tier == TIER_A]
        todo_b = [f for f in open_ if f.tier == TIER_B]
        todo_c = [f for f in open_ if f.tier == TIER_C]
        skipped = list(run.skipped)
        skipped += [
            f"Finding #{f.id} ({f.kind}) is dismissed, so it was not applied." for f in held
        ]
        review_count = tracked if not dry_run else len(reviewed)

        if dry_run:
            summary = (
                f"Dry run: the curator scanned {len(run.tree.pages)} pages and wrote nothing. "
                f"It would apply {len(todo_a)} Tier A changes, found {len(todo_b)} Tier B and "
                f"{len(todo_c)} Tier C findings, skipped {len(skipped)} items, and would review "
                f"{review_count} pages. It made {run.budget.calls} worker calls."
            )
            sections = [
                ("To apply (Tier A)", _lines(todo_a)),
                ("Proposed (Tier B)", _lines(todo_b)),
                ("Proposed (Tier C)", _lines(todo_c)),
            ]
        else:
            summary = (
                f"The curator scanned {len(run.tree.pages)} pages. It applied {len(done_a)} "
                f"Tier A and {len(done_b)} Tier B changes, proposed {len(todo_b) + len(todo_c)} "
                f"findings, skipped {len(skipped)} items, and reviewed {review_count} pages. "
                f"It made {run.budget.calls} worker calls."
            )
            sections = [
                ("Applied (Tier A)", _lines(done_a)),
                ("Applied (Tier B)", _lines(done_b)),
                ("Proposed (Tier B)", _lines(todo_b)),
                ("Proposed (Tier C)", _lines(todo_c)),
            ]
        sections.append(("Skipped", "\n".join(f"- {line}" for line in skipped)))
        sections.append(
            ("Reviewed", f"{review_count} pages {'would get' if dry_run else 'got'} tracking keys.")
        )
        counts = {
            "pages": len(run.tree.pages),
            "applied_a": len(done_a),
            "applied_b": len(done_b),
            "todo_a": len(todo_a),
            "proposed_b": len(todo_b),
            "proposed_c": len(todo_c),
            "dismissed": len(held),
            "skipped": len(skipped),
            "reviewed": review_count,
            "calls": run.budget.calls,
        }
        return RunReport(
            job=self.name,
            started_at=run.now,
            dry_run=dry_run,
            summary=summary,
            sections=sections,
            calls=run.budget.calls,
            written=ws.paths() if ws is not None else [],
            data={
                "apply": run.apply,
                "counts": counts,
                "findings": {
                    "applied_a": [f.id for f in done_a],
                    "applied_b": [f.id for f in done_b],
                    "todo_a": [f.id for f in todo_a],
                    "proposed_b": [f.id for f in todo_b],
                    "proposed_c": [f.id for f in todo_c],
                    "dismissed": [f.id for f in held],
                },
                "skipped": skipped,
            },
        )


def _keys_fit(text: str) -> bool:
    """False for a page that starts a frontmatter block that is not a mapping,
    so the tracking keys cannot go on it."""
    return not (text.startswith("---") and frontmatter.split(text)[1] == text)


def _unique(findings: list[Finding]) -> list[Finding]:
    """The findings with one finding for each fingerprint, the first one."""
    seen: set[str] = set()
    out: list[Finding] = []
    for finding in findings:
        if finding.fingerprint not in seen:
            seen.add(finding.fingerprint)
            out.append(finding)
    return out


def _changed(row: dict[str, Any], tree: Tree) -> bool:
    """True when a path of a finding row is gone, or a page hash in it changed."""
    paths = list(row.get("paths") or [])
    if any(p not in tree.files for p in paths):
        return True
    hashes = (row.get("detail") or {}).get("hashes") or {}
    return any(p in tree.pages and body_hash(tree.text(p)) != h for p, h in hashes.items())


def _short(text: str) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= _SHOW_CHARS else text[: _SHOW_CHARS - 1] + "\u2026"


def _code(text: Any) -> str:
    """`text`, short, as one code span, so that a link in it is not a link of
    the journal entry that shows it."""
    return "`" + _short(str(text)).replace("`", "'") + "`"


def _patch_text(finding: Finding) -> str:
    patch = finding.patch or {}
    action = patch.get("action")
    detail = finding.detail
    if action == "relink":
        return f"change {_code(patch['old'])} to {_code(patch['new'])}"
    if action == "drop-line":
        return f"remove the line {_code(patch['line'])}"
    if action == "index-add":
        return f"add it to {_code(patch['path'])}"
    if action == "trash":
        return "move it to the trash"
    if action == "set-date":
        return f"set {_code('date: ' + str(patch['date']))}"
    if action == "move":
        return f"move it to {_code(patch['to'])}: {_code(detail.get('reason', ''))}"
    if action == "merge":
        return (
            f"point its links at {_code(patch['into'])} and move "
            f"{_code(patch['path'])} to the trash"
        )
    edits = "".join(
        f"\n  - find {_code(p.get('find', ''))}, replace with {_code(p.get('replace', ''))}"
        f" because {_code(p.get('because', ''))}"
        for p in patch.get("patch") or []
    )
    return f"{detail.get('status', 'stale')}, confidence {detail.get('confidence')}:{edits}"


def _detail_text(finding: Finding) -> str:
    detail = finding.detail
    if finding.kind == "conflict":
        claims = "".join(
            f"\n  - A says {_code(c.get('a_says', ''))}; B says {_code(c.get('b_says', ''))}"
            for c in detail.get("claims") or []
        )
        return claims or "the pages disagree"
    if finding.kind == "broken-link":
        cands = ", ".join(_code(c) for c in detail.get("candidates") or []) or "none"
        return f"{_code(detail.get('target', ''))} goes nowhere; candidates: {cands}"
    if finding.kind == "orphan":
        return "no page links to it, and its folder has no index page"
    if finding.kind == "stray":
        return "a stray file; the curator leaves it"
    if finding.kind in ("misplaced", "no-section"):
        return (
            f"section {_code(detail.get('section'))}, folder {_code(detail.get('folder'))}, "
            f"confidence {detail.get('confidence')}: {_code(detail.get('reason', ''))}"
        )
    if finding.kind == "duplicate":
        return "the pages are duplicates, but the worker named no page to keep"
    if finding.kind == "refresh":
        return f"{detail.get('status')}, confidence {detail.get('confidence')}, with no patch"
    return _code(json.dumps({k: v for k, v in detail.items() if k != "hashes"}, sort_keys=True))


def describe(finding: Finding) -> str:
    """One line, or a few, that say what a finding is and what its patch does.
    Every text from a page or a worker is a code span."""
    paths = ", ".join(_code(p) for p in finding.paths)
    head = f"#{finding.id} {finding.kind} (Tier {finding.tier}) {paths}"
    body = _patch_text(finding) if finding.patch else _detail_text(finding)
    refused = finding.detail.get("refused")
    tail = f" Not applied: {_code(refused)}." if refused else ""
    return f"- {head}: {body}{tail}"


def _lines(findings: list[Finding]) -> str:
    return "\n".join(describe(f) for f in findings)
