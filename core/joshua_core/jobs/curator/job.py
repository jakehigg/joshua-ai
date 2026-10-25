"""The `curator` job: one run of the scan, the workers, and the apply step.

A run does this, in order:

1. The scan (`scan.py`) over the whole wiki, every run.
2. The workers (`workers.py`), on the topic pages whose `curated` pair or
   `refreshed` pair is old or missing (see `track.py`), within the worker
   budget (`jobs.curator.max_worker_calls`). The budget goes in this order:
   1. `place` for each page that was never curated (it has no
      `curated_hash`);
   2. `compare` for each pair of similar pages, the near-duplicates first,
      except a pair that `compare` read before at the same two body hashes
      (a `curator_pair` row);
   3. `refresh` for each page whose `refreshed` pair is old or missing, the
      page that was never refreshed first, then the page with the oldest
      `refreshed` day. The worker reads the journal entries of the page since
      its `refreshed` day and the snapshot parts of its bundle;
   4. `place` for each page that was curated before and changed since.
3. The apply step (`apply.py`), when the run is not a dry run: every Tier A
   finding, and each Tier B finding whose action the run may apply. A
   dismissed finding is not applied.
4. The tracking keys, when the run is not a dry run: the `curated` pair on
   each page that `place` answered for, and on each page that needs no
   `place` (an index page, a skill, a journal entry); the `refreshed` pair on
   each topic page whose `refresh` answered or that had no journal entry and
   no snapshot part to read; and the old pairs again on each page the run changed that had
   current pairs before.
5. The findings, into `curator_finding`, and a `curator_pair` row for each
   pair that `compare` answered for, in a dry run too.

Each pass counts on its own. A page that the budget or a failed call left out
of one pass keeps the pair of that pass old, and a later run does only that
pass.

Arguments, each optional:

- `apply`: a list of actions, or the same as a string with commas, such as
  `move,merge,refresh`. It replaces `jobs.curator.apply` for the run.
- `max_worker_calls`: a whole number of 0 or more. It replaces
  `jobs.curator.max_worker_calls` for the run.
- `paths`: a list of wiki-relative prefixes, or the same as a string with
  commas, such as `houseplants/,garden/`. The workers then get only the pages
  whose path starts with a prefix, and only those pages are reviewed. A pair
  is compared when one of its pages is in scope. The scan still reads the
  whole wiki.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from joshua_shared import frontmatter, layout
from joshua_shared.config import CURATOR_ACTIONS, JoshuaConfig
from joshua_shared.log import get_logger

from joshua_core.engine.ephemeral import Runner
from joshua_core.engine.usage import Usage
from joshua_core.jobs.bundle import BundleStore, build_bundle, snapshot_summary
from joshua_core.jobs.curator import scan, workers
from joshua_core.jobs.curator.apply import ApplyRefused, Workspace, apply_patch, finds_once
from joshua_core.jobs.curator.findings import (
    KINDS_BY_WORKER,
    TIER_A,
    TIER_B,
    TIER_C,
    Finding,
    FindingStore,
    from_row,
    pair_fingerprint,
)
from joshua_core.jobs.curator.scan import Pair, Tree
from joshua_core.jobs.curator.track import (
    HASH_KEY,
    body_hash,
    curated_day,
    is_untouched,
    may_track,
    needs_place,
    needs_refresh,
    refreshed_day,
)
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


def parse_calls(value: Any) -> int:
    """The `max_worker_calls` argument as a count of 0 or more. Raises `JobArgsError`."""
    message = "max_worker_calls is a whole number of 0 or more"
    if isinstance(value, bool):
        raise JobArgsError(message)
    try:
        number = int(value.strip()) if isinstance(value, str) else int(value)
    except (TypeError, ValueError):
        raise JobArgsError(message) from None
    if number < 0 or (isinstance(value, float) and value != number):
        raise JobArgsError(message)
    return number


def parse_paths(value: Any) -> list[str]:
    """The `paths` argument as a list of wiki-relative prefixes. Raises `JobArgsError`."""
    if isinstance(value, str):
        items = [part.strip() for part in value.split(",") if part.strip()]
    elif isinstance(value, list) and all(isinstance(v, str) for v in value):
        items = [v.strip() for v in value if v.strip()]
    else:
        raise JobArgsError("paths is a list of wiki path prefixes, or a string with commas")
    if not items:
        raise JobArgsError("paths names no prefix")
    for item in items:
        parts = item.rstrip("/").split("/")
        if item.startswith("/") or "\\" in item or any(p in ("", ".", "..") for p in parts):
            raise JobArgsError(f"not a wiki path prefix: {item}")
        if any(p.startswith(".") for p in parts):
            raise JobArgsError(f"not a wiki path prefix: {item}")
    return list(dict.fromkeys(items))


def in_scope(rel: str, prefixes: list[str] | None) -> bool:
    """True when `prefixes` is None or `rel` starts with one of them."""
    return prefixes is None or any(rel.startswith(p) for p in prefixes)


@dataclass
class _Run:
    """The state of one run.

    `place_due`, `refresh_due`, and `compare_due` are the pages and the pairs
    that each worker was due to read. `placed` and `refreshed` are the pages
    that get the `curated` pair and the `refreshed` pair. `answered` maps
    `place` and `refresh` to the pages that worker answered for, and
    `compared` holds the pairs that `compare` answered for. `failed` maps
    `(worker, outcome)` to the pages or pairs of the calls that gave no usable
    answer.
    """

    tree: Tree
    day: date
    now: datetime
    apply: list[str]
    budget: workers.Budget
    findings: list[Finding] = field(default_factory=list)
    place_due: set[str] = field(default_factory=set)
    refresh_due: set[str] = field(default_factory=set)
    compare_due: set[frozenset[str]] = field(default_factory=set)
    placed: set[str] = field(default_factory=set)
    refreshed: set[str] = field(default_factory=set)
    answered: dict[str, set[str]] = field(
        default_factory=lambda: {"place": set(), "refresh": set()}
    )
    compared: list[tuple[str, str]] = field(default_factory=list)
    pairs_unchanged: int = 0
    skipped: list[str] = field(default_factory=list)
    failed: dict[tuple[str, str], list[str]] = field(default_factory=dict)
    unsent: int = 0
    scope: list[str] | None = None
    # `{page: snapshot summary}` of each page whose refresh read a snapshot.
    snapshots: dict[str, str] = field(default_factory=dict)

    def reviewed_again(self, kind: str, paths: list[str]) -> bool:
        """True when the worker that raises `kind` was due to read `paths` in
        this run."""
        if kind in KINDS_BY_WORKER["place"]:
            return bool(set(paths) & self.place_due)
        if kind in KINDS_BY_WORKER["refresh"]:
            return bool(set(paths) & self.refresh_due)
        if kind in KINDS_BY_WORKER["compare"]:
            return frozenset(paths) in self.compare_due
        return False

    def answered_again(self, kind: str, paths: list[str]) -> bool:
        """True when the worker that raises `kind` answered for `paths` in this run."""
        if kind in KINDS_BY_WORKER["place"]:
            return bool(set(paths) & self.answered["place"])
        if kind in KINDS_BY_WORKER["refresh"]:
            return bool(set(paths) & self.answered["refresh"])
        if kind in KINDS_BY_WORKER["compare"]:
            return frozenset(paths) in {frozenset(pair) for pair in self.compared}
        return False


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
        unknown = sorted(set(args) - {"apply", "max_worker_calls", "paths"})
        if unknown:
            raise JobArgsError(f"unknown argument: {', '.join(unknown)}")
        out: dict[str, Any] = {}
        if "apply" in args:
            out["apply"] = parse_apply(args["apply"])
        if args.get("max_worker_calls") not in (None, ""):
            out["max_worker_calls"] = parse_calls(args["max_worker_calls"])
        if args.get("paths") not in (None, "", []):
            out["paths"] = parse_paths(args["paths"])
        return out

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
            budget=workers.Budget(args.get("max_worker_calls", cfg.max_worker_calls)),
            findings=list(result.findings),
            scope=args.get("paths"),
        )
        scope = run.scope

        texts = {rel: tree.text(rel) for rel in tree.pages}
        topics = [
            t
            for t in list_topics(
                self._wiki, self._settings.jobs.topics.exclude, people=self._settings.people
            )
            if t.path in tree.pages
        ]
        topic_paths = {t.path for t in topics}
        tracked_pages = {
            rel for rel, text in texts.items() if in_scope(rel, scope) and may_track(rel, text)
        }
        place_due = {rel for rel in tracked_pages if needs_place(texts[rel])}
        refresh_due = {rel for rel in tracked_pages & topic_paths if needs_refresh(texts[rel])}
        for rel in sorted(place_due | refresh_due):
            if not _keys_fit(texts[rel]):
                place_due.discard(rel)
                refresh_due.discard(rel)
                run.skipped.append(
                    f"`{rel}` was not reviewed: its frontmatter is not a YAML mapping."
                )
        # A page that is not a topic page needs no `place`, so the scan places it.
        run.placed = place_due - topic_paths
        run.place_due = place_due & topic_paths
        run.refresh_due = refresh_due
        to_place = sorted(
            (t for t in topics if t.path in run.place_due),
            key=lambda t: _oldest_first(curated_day(texts[t.path]), t.path),
        )
        to_refresh = sorted(
            (t for t in topics if t.path in run.refresh_due),
            key=lambda t: _oldest_first(refreshed_day(texts[t.path]), t.path),
        )
        never = [t for t in to_place if not _was_curated(texts[t.path])]
        again = [t for t in to_place if _was_curated(texts[t.path])]

        sections = scan.section_map(tree)
        if not sections and to_place:
            run.skipped.append("No page was placed: `Home.md` has no `## Sections` list.")
        if sections:
            await self._place_pass(run, never, texts, sections)
        pairs = await self._pairs(run, topics, run.place_due | run.refresh_due)
        await self._compare_pass(run, pairs, texts)
        await self._refresh_pass(run, to_refresh, texts)
        if sections:
            await self._place_pass(run, again, texts, sections)
        if run.unsent:
            run.skipped.append(
                f"The budget of {run.budget.limit} worker calls ran out: "
                f"{run.unsent} pages or pairs were not sent to a worker."
            )

        run.findings = _unique(run.findings)
        to_curate = {rel for rel in run.placed if rel not in tree.strays}
        to_refresh_keys = {rel for rel in run.refreshed if rel not in tree.strays}
        dismissed = {
            row["fingerprint"]
            for row in await self._findings.list_findings("dismissed", ROW_LIMIT)
            if row.get("fingerprint")
        }
        open_rows = await self._findings.list_findings("open", ROW_LIMIT)

        applied: list[Finding] = []
        ws: Workspace | None = None
        tracked = _Tracked()
        if not dry_run:
            ws = Workspace(self._wiki, run.day)
            applied = self._apply(run, ws, dismissed, open_rows)
            for rel in sorted(to_curate | to_refresh_keys):
                current = ws.current(rel)
                if current is not None:
                    tracked.add(
                        current,
                        *_track(ws, current, rel in to_curate, rel in to_refresh_keys),
                    )
            ws.track_clean()
        else:
            for rel in to_curate | to_refresh_keys:
                tracked.add(rel, rel in to_curate, rel in to_refresh_keys)

        final_tree = scan.load_tree(self._wiki) if ws is not None else tree
        await self._record(run, applied, dismissed, open_rows, final_tree, ws)
        await self._record_pairs(run, final_tree, ws)
        return self._report(run, applied, dismissed, ws, tracked, dry_run)

    # -- workers ------------------------------------------------------------

    def _model(self, worker: str) -> str | None:
        return workers.worker_model(self._settings, worker)

    def _skip(self, run: _Run, worker: str, label: str, outcome: str) -> None:
        run.failed.setdefault((worker, outcome), []).append(label)

    async def _entries(
        self, run: _Run, topic: Topic, text: str
    ) -> tuple[list[tuple[str, str]], list[tuple[str, str]]] | None:
        """The journal entries about a topic since its `refreshed` day and the
        snapshot parts of its bundle, each `(path, text)`, or None when the
        bundle cannot be built."""
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
        since = refreshed_day(text)
        out: list[tuple[str, str]] = []
        for rel in bundle.paths:
            if scan.section_of(rel) != scan.JOURNAL:
                continue
            day = scan.journal_day(run.tree, rel)
            if since is not None and (day is None or day <= since):
                continue
            out.append((rel, run.tree.text(rel)))
        snapshots = [(part.path, part.text) for part in bundle.snapshots]
        if snapshots:
            run.snapshots[topic.path] = snapshot_summary(bundle)
        return out, snapshots

    async def _refresh_pass(self, run: _Run, review: list[Topic], texts: dict[str, str]) -> None:
        for topic in review:
            if run.budget.left == 0:
                run.unsent += 1
                continue
            text = texts[topic.path]
            found = await self._entries(run, topic, text)
            if found is None:
                run.skipped.append(f"The journal entries of `{topic.path}` could not be read.")
                continue
            entries, snapshots = found
            if not entries and not snapshots:
                run.refreshed.add(topic.path)
                continue
            run.budget.spend()
            used: list[Usage] = []
            got = await workers.refresh(
                topic.path,
                text,
                entries,
                snapshots=snapshots,
                model=self._model("refresh"),
                runner=self._runner,
                usage_out=used,
            )
            run.budget.usage.add("refresh", used, model=self._model("refresh"))
            if got.answer is None:
                self._skip(run, "refresh", f"`{topic.path}`", got.outcome)
                continue
            run.answered["refresh"].add(topic.path)
            run.refreshed.add(topic.path)
            self._on_refresh(
                run, topic.path, text, [p for p, _ in [*entries, *snapshots]], got.answer
            )

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
            if not run.budget.spend():
                run.unsent += 1
                continue
            used: list[Usage] = []
            got = await workers.place(
                topic.path,
                texts[topic.path],
                sections,
                model=self._model("place"),
                runner=self._runner,
                usage_out=used,
            )
            run.budget.usage.add("place", used, model=self._model("place"))
            if got.answer is None:
                self._skip(run, "place", f"`{topic.path}`", got.outcome)
                continue
            run.answered["place"].add(topic.path)
            run.placed.add(topic.path)
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

    async def _known_pairs(self, pairs: list[Pair]) -> dict[str, dict[str, Any]]:
        """The `curator_pair` rows of `pairs`. A store that fails gives none."""
        keys = sorted({pair_fingerprint(p.a, p.b) for p in pairs})
        if not keys:
            return {}
        try:
            return await self._findings.get_curator_pairs(keys)
        except Exception as exc:  # noqa: BLE001 — no rows means each pair is compared
            logger.warning({"message": "curator pairs not read", "error": str(exc)})
            return {}

    async def _compare_pass(self, run: _Run, pairs: list[Pair], texts: dict[str, str]) -> None:
        known = await self._known_pairs(pairs)
        for pair in pairs:
            row = known.get(pair_fingerprint(pair.a, pair.b))
            if row is not None and _pair_unchanged(row, pair.a, pair.b, texts):
                run.pairs_unchanged += 1
                continue
            run.compare_due.add(frozenset((pair.a, pair.b)))
            if not run.budget.spend():
                run.unsent += 1
                continue
            used: list[Usage] = []
            got = await workers.compare(
                (pair.a, texts[pair.a]),
                (pair.b, texts[pair.b]),
                model=self._model("compare"),
                runner=self._runner,
                usage_out=used,
            )
            run.budget.usage.add("compare", used, model=self._model("compare"))
            if got.answer is None:
                self._skip(run, "compare", f"`{pair.a}` and `{pair.b}`", got.outcome)
                continue
            run.compared.append((pair.a, pair.b))
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
    ) -> list[Finding]:
        """Apply the findings the run may apply. Returns the applied ones.

        An open Tier B finding of an earlier run applies too, unless the
        worker that raised it was due to read its pages again in this run.
        """
        seen = {f.fingerprint for f in run.findings}
        earlier = [
            from_row(row)
            for row in open_rows
            if row.get("tier") == TIER_B
            and row.get("fingerprint") not in seen
            and row.get("patch")
            and not run.reviewed_again(str(row.get("kind")), list(row.get("paths") or []))
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
            superseded = run.answered_again(str(row.get("kind")), paths)
            if superseded or _changed(row, tree):
                await self._findings.set_finding_status(rid, "stale", at=run.now)

    async def _record_pairs(self, run: _Run, tree: Tree, ws: Workspace | None) -> None:
        """A `curator_pair` row for each pair that `compare` answered for, with
        the body hashes after the apply step. A change that the run applied
        does not send the pair to `compare` again."""
        for a, b in run.compared:
            now_a, now_b = (ws.current(a), ws.current(b)) if ws is not None else (a, b)
            if now_a is None or now_b is None or not {now_a, now_b} <= set(tree.pages):
                continue
            first, second = sorted((now_a, now_b))
            try:
                await self._findings.upsert_curator_pair(
                    fingerprint=pair_fingerprint(first, second),
                    hash_a=body_hash(tree.text(first)),
                    hash_b=body_hash(tree.text(second)),
                    checked_at=run.now,
                )
            except Exception as exc:  # noqa: BLE001 — a lost row costs one more call later
                logger.warning({"message": "curator pair not recorded", "error": str(exc)})

    # -- report ---------------------------------------------------------------

    def _report(
        self,
        run: _Run,
        applied: list[Finding],
        dismissed: set[str],
        ws: Workspace | None,
        tracked: _Tracked,
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
        for (worker, outcome), labels in run.failed.items():
            skipped += [_failed_line(worker, outcome, label) for label in labels]
        skipped += [
            f"Finding #{f.id} ({f.kind}) is dismissed, so it was not applied." for f in held
        ]
        review_count = len(tracked.pages)

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
        if run.snapshots:
            sections.append(
                (
                    "Snapshots",
                    "\n".join(f"- `{rel}`: {line}" for rel, line in sorted(run.snapshots.items())),
                )
            )
        sections.append(("Skipped", _skipped_text(run, held)))
        verb = "would get" if dry_run else "got"
        sections.append(
            (
                "Reviewed",
                f"{len(tracked.curated)} pages {verb} `curated` keys, and "
                f"{len(tracked.refreshed)} pages {verb} `refreshed` keys.",
            )
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
            "curated": len(tracked.curated),
            "refreshed": len(tracked.refreshed),
            "pairs_unchanged": run.pairs_unchanged,
            "calls": run.budget.calls,
        }
        return RunReport(
            job=self.name,
            started_at=run.now,
            dry_run=dry_run,
            summary=summary,
            sections=sections,
            calls=run.budget.calls,
            tokens=run.budget.usage.tokens,
            written=ws.paths() if ws is not None else [],
            data={
                "apply": run.apply,
                "max_worker_calls": run.budget.limit,
                "paths": run.scope,
                "usage": run.budget.usage.as_data(),
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


@dataclass
class _Tracked:
    """The pages that got, or in a dry run would get, each pair of keys."""

    curated: set[str] = field(default_factory=set)
    refreshed: set[str] = field(default_factory=set)

    @property
    def pages(self) -> set[str]:
        return self.curated | self.refreshed

    def add(self, rel: str, curated: bool, refreshed: bool) -> None:
        if curated:
            self.curated.add(rel)
        if refreshed:
            self.refreshed.add(rel)


def _track(ws: Workspace, rel: str, curated: bool, refreshed: bool) -> tuple[bool, bool]:
    """Write the pairs asked for on a page. Returns which pairs it wrote."""
    if not (ws.root / rel).is_file():
        return False, False
    before = ws.read(rel)
    if not ws.track(rel, curated=curated, refreshed=refreshed):
        return False, False
    after = ws.read(rel)
    return (
        curated and needs_place(before) and not needs_place(after),
        refreshed and needs_refresh(before) and not needs_refresh(after),
    )


def _oldest_first(day: date | None, rel: str) -> tuple[bool, date, str]:
    """The sort key that puts a page with no day first, then the oldest day."""
    return (day is not None, day or date.min, rel)


def _pair_unchanged(row: dict[str, Any], a: str, b: str, texts: dict[str, str]) -> bool:
    """True when both pages of a pair have the body hashes of its `curator_pair` row."""
    first, second = sorted((a, b))
    return row.get("hash_a") == body_hash(texts[first]) and row.get("hash_b") == body_hash(
        texts[second]
    )


def _was_curated(text: str) -> bool:
    """True when the page carries a `curated_hash` from an earlier review."""
    meta, _ = frontmatter.split(text)
    return isinstance(meta.get(HASH_KEY), str)


_NOUN = {"compare": ("pair", "pairs")}


def _failed_line(worker: str, outcome: str, label: str) -> str:
    if outcome == "refused":
        return f"The `{worker}` answer for {label} was refused."
    return f"The `{worker}` worker gave no answer for {label}."


def _skipped_text(run: _Run, held: list[Finding]) -> str:
    """The Skipped section: one line for each note, and one line with a count
    for each worker and outcome, with its pages or pairs under it."""
    lines = [f"- {line}" for line in run.skipped]
    for (worker, outcome), labels in run.failed.items():
        one, many = _NOUN.get(worker, ("page", "pages"))
        noun = one if len(labels) == 1 else many
        if outcome == "refused":
            lines.append(f"- The `{worker}` answer was refused for {len(labels)} {noun}:")
        else:
            lines.append(f"- The `{worker}` worker gave no answer for {len(labels)} {noun}:")
        lines.extend(f"  - {label}" for label in labels)
    lines.extend(f"- Finding #{f.id} ({f.kind}) is dismissed, so it was not applied." for f in held)
    return "\n".join(lines)


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
    the report that shows it."""
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
    edits = "".join("\n\n" + patch_block(p) for p in patch.get("patch") or [])
    return f"{detail.get('status', 'stale')}, confidence {detail.get('confidence')}:{edits}"


def patch_block(pair: dict[str, Any], indent: str = "  ") -> str:
    """One find and replace pair as a fenced `text` block, byte for byte.

    The fence is longer than any run of backticks in the text, so no line of
    the text can close it, and the scan reads no link inside it. Each line is
    indented by `indent`, so the block stays inside its list item.
    """
    lines = [f"find: {pair.get('find', '')}", f"replace: {pair.get('replace', '')}"]
    if pair.get("because"):
        lines.append(f"because: {pair['because']}")
    body = "\n".join(lines)
    fence = "`" * max(3, _longest_backticks(body) + 1)
    block = [f"{fence}text", *body.split("\n"), fence]
    return "\n".join(indent + line if line else line for line in block)


def _longest_backticks(text: str) -> int:
    return max((len(run) for run in re.findall(r"`+", text)), default=0)


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
    Every text from a page or a worker is a code span, or, for a find and
    replace pair, a fenced block."""
    paths = ", ".join(_code(p) for p in finding.paths)
    head = f"#{finding.id} {finding.kind} (Tier {finding.tier}) {paths}"
    body = _patch_text(finding) if finding.patch else _detail_text(finding)
    refused = finding.detail.get("refused")
    tail = f" Not applied: {_code(refused)}." if refused else ""
    return f"- {head}: {body}{tail}"


def _lines(findings: list[Finding]) -> str:
    return "\n".join(describe(f) for f in findings)
