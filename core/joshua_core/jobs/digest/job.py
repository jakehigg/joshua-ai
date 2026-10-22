"""The `digest` job: one journal entry for a run of near-duplicate entries.

Arguments:

- `mode`: `plan`, `run`, or `undo`. Required.
- `digest`: the path of a digest entry, relative to the wiki root. Only with
  `mode: undo`, and required there.

`plan` finds the candidates and the groups (see `groups.py`) and gives the
path that each digest would get. It calls no worker and writes no page.

`run` does the same, then asks the `digest` worker once for each group. For
each answer that passes the checks of `worker.py`, it writes the digest entry
(see `page.py`) and adds the one line `digest: <digest path>` to each covered
entry. A dry run calls the workers and writes nothing; its report holds the
full text of each digest.

`undo` removes the `digest:` line that names the digest from each entry that
it covers, and deletes the digest entry. A dry run writes nothing.

The runner commits every path of a run that is not a dry run in one commit.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path, PurePosixPath
from typing import Any

from joshua_shared import frontmatter, layout
from joshua_shared.config import JoshuaConfig
from joshua_shared.log import get_logger

from joshua_core.engine.ephemeral import Runner
from joshua_core.engine.usage import Usage, UsageLedger
from joshua_core.jobs.digest.groups import (
    COVERED_KEY,
    SKIP_NO_VECTOR,
    Entry,
    Group,
    Grouping,
    build_groups,
    find_candidates,
    is_digest_entry,
    mean_vector,
    read_exact,
    write_exact,
)
from joshua_core.jobs.digest.page import digest_path, mark, render_digest, unmark
from joshua_core.jobs.digest.worker import WORKER, digest_text, write_digest
from joshua_core.jobs.insights.job import usage_lines
from joshua_core.jobs.insights.page import local_day
from joshua_core.jobs.models import digest_model
from joshua_core.jobs.research.text import code_span, fenced
from joshua_core.jobs.runner import Clock, Job, JobArgsError, RunReport, current_run_id
from joshua_core.memory.indexer import embed_document

logger = get_logger("jobs.digest")

MODES = ("plan", "run", "undo")
OVER_MAX_GROUPS = "in a group over jobs.digest.max_groups"
IN_NO_GROUP = "in no group"
_ARGS = {"mode", "digest"}
_JOURNAL = "journal/"


@dataclass
class _Planned:
    group: Group
    path: Path
    rel: str
    text: str | None = None
    reason: str | None = None


@dataclass
class _Result:
    mode: str
    dry_run: bool
    planned: list[_Planned] = field(default_factory=list)
    skipped: dict[str, int] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    written: list[Path] = field(default_factory=list)
    back: list[str] = field(default_factory=list)


def _safe_journal_rel(value: Any) -> str | None:
    """`value` as a journal path relative to the wiki root, or None."""
    if not isinstance(value, str):
        return None
    rel = value.strip()
    if not rel.startswith(_JOURNAL) or not rel.endswith(".md") or "\\" in rel:
        return None
    parts = PurePosixPath(rel).parts
    if any(p in ("", ".", "..") or p.startswith(".") for p in parts):
        return None
    return rel


class DigestJob(Job):
    name = "digest"

    def __init__(
        self,
        *,
        settings: JoshuaConfig,
        data_dir: Path | str,
        runner: Runner | None = None,
    ) -> None:
        self._settings = settings
        self._data_dir = Path(data_dir)
        self._runner = runner

    @property
    def _wiki(self) -> Path:
        return layout.wiki_root(self._data_dir)

    # -- the arguments ------------------------------------------------------

    def check_args(self, args: dict[str, Any]) -> dict[str, Any]:
        unknown = sorted(set(args) - _ARGS)
        if unknown:
            raise JobArgsError(f"unknown argument: {', '.join(unknown)}")
        mode = args.get("mode")
        if mode not in MODES:
            raise JobArgsError("mode is required: plan, run, or undo")
        given = args.get("digest")
        if mode != "undo":
            if given not in (None, ""):
                raise JobArgsError("digest is only for mode undo")
            return {"mode": mode}
        rel = _safe_journal_rel(given)
        if rel is None:
            raise JobArgsError("digest is required: a journal path such as journal/2026/03/08/x.md")
        path = self._wiki / rel
        if not path.is_file():
            raise JobArgsError(f"no such digest entry: {rel}")
        meta, _ = frontmatter.split(read_exact(path))
        if not is_digest_entry(meta):
            raise JobArgsError(f"not a digest entry: {rel}")
        return {"mode": mode, "digest": rel}

    # -- the run --------------------------------------------------------------

    async def run_once(self, *, args: dict[str, Any], dry_run: bool, clock: Clock) -> RunReport:
        now = clock()
        ledger = UsageLedger()
        if args["mode"] == "undo":
            result, data = self._undo(args["digest"], dry_run)
        else:
            today = local_day(now, self._settings)
            result = await self._digest(args["mode"], dry_run, today, ledger)
            data = self._data(result)
        sections = [*self._sections(result), ("Usage", usage_lines(ledger))]
        return RunReport(
            job=self.name,
            started_at=now,
            dry_run=dry_run,
            args=args,
            summary=self._summary(result, data),
            sections=sections,
            calls=ledger.calls,
            tokens=ledger.tokens,
            written=list(result.written),
            data={**data, "usage": ledger.as_data()},
        )

    async def _embed(self, entry: Entry) -> list[float] | None:
        chunks = await embed_document(
            entry.title,
            entry.body,
            model=self._settings.memory.embed_model,
            chunk_chars=self._settings.memory.chunk_chars,
        )
        return mean_vector([vec for _, _, vec in chunks])

    async def _digest(self, mode: str, dry_run: bool, today: date, ledger: UsageLedger) -> _Result:
        cfg = self._settings.jobs.digest
        result = _Result(mode=mode, dry_run=dry_run)
        found = find_candidates(self._data_dir, today, cfg.min_age_days)
        grouping: Grouping = await build_groups(
            found.entries,
            embed=self._embed,
            sim=cfg.sim,
            min_entries=cfg.min_entries,
            max_entries=cfg.max_entries,
            max_groups=cfg.max_groups,
        )
        skipped = {reason: len(paths) for reason, paths in found.skipped.items()}
        if grouping.no_vector:
            skipped[SKIP_NO_VECTOR] = len(grouping.no_vector)
        over = sum(g.size for g in grouping.over)
        if over:
            skipped[OVER_MAX_GROUPS] = over
        if grouping.alone:
            skipped[IN_NO_GROUP] = len(grouping.alone)
        result.skipped = skipped
        result.notes.extend(grouping.notes)

        taken: list[Path] = []
        for group in grouping.groups:
            path = digest_path(group, self._data_dir, taken)
            taken.append(path)
            rel = path.relative_to(self._wiki).as_posix()
            result.planned.append(_Planned(group, path, rel))
        if mode == "plan":
            return result

        model = digest_model(self._settings)
        for item in result.planned:
            used: list[Usage] = []
            digest = await write_digest(
                item.group,
                user_text=digest_text(
                    item.group, timezone=self._settings.timezone, instance=self._settings.name
                ),
                model=model,
                runner=self._runner,
                usage_out=used,
            )
            ledger.add(WORKER, used, model=model)
            if digest.reason is not None:
                item.reason = digest.reason
                continue
            item.text = render_digest(
                group=item.group,
                digest_rel=item.rel,
                title=digest.title,
                summary=digest.summary,
                run_id=current_run_id(),
            )
            if dry_run:
                continue
            item.path.parent.mkdir(parents=True, exist_ok=True)
            write_exact(item.path, item.text)
            result.written.append(item.path)
            for entry in item.group.entries:
                target = self._wiki / entry.rel
                write_exact(target, mark(entry.text, item.rel))
                result.written.append(target)
        return result

    def _undo(self, digest_rel: str, dry_run: bool) -> tuple[_Result, dict[str, Any]]:
        result = _Result(mode="undo", dry_run=dry_run)
        path = self._wiki / digest_rel
        meta, _ = frontmatter.split(read_exact(path))
        covers = meta.get("covers")
        listed = [c for c in covers if isinstance(c, str)] if isinstance(covers, list) else []
        targets = list(dict.fromkeys([*listed, *self._marked_with(digest_rel)]))
        back = result.back
        for rel in targets:
            safe = _safe_journal_rel(rel)
            target = self._wiki / safe if safe else None
            if target is None or not target.is_file():
                result.notes.append(f"{code_span(rel)} is not on the disk.")
                continue
            restored = unmark(read_exact(target), digest_rel)
            if restored is None:
                result.notes.append(f"{code_span(rel)} has no `digest:` line for this digest.")
                continue
            back.append(rel)
            if not dry_run:
                write_exact(target, restored)
                result.written.append(target)
        if not dry_run:
            path.unlink()
            result.written.append(path)
        return result, {"mode": "undo", "digest": digest_rel, "back": back}

    def _marked_with(self, digest_rel: str) -> list[str]:
        """The journal entries whose `digest` key names `digest_rel`."""
        journal = layout.journal_root(self._data_dir)
        out: list[str] = []
        if not journal.is_dir():
            return out
        for page in sorted(journal.rglob("*.md")):
            if not page.is_file() or layout.is_hidden(page, journal):
                continue
            meta, _ = frontmatter.split(read_exact(page))
            if meta.get(COVERED_KEY) == digest_rel:
                out.append(page.relative_to(self._wiki).as_posix())
        return out

    # -- the report -----------------------------------------------------------

    def _data(self, result: _Result) -> dict[str, Any]:
        return {
            "mode": result.mode,
            "groups": [
                {
                    "key": p.group.key,
                    "by": p.group.by,
                    "size": p.group.size,
                    "first": p.group.first.isoformat(),
                    "last": p.group.last.isoformat(),
                    "digest": p.rel,
                    "entries": p.group.paths,
                    "rejected": p.reason,
                }
                for p in result.planned
            ],
            "skipped": dict(result.skipped),
        }

    def _summary(self, result: _Result, data: dict[str, Any]) -> str:
        prefix = "Dry run: " if result.dry_run else ""
        if result.mode == "undo":
            return f"{prefix}digest undo: {data['digest']} ({len(data['back'])} entries back)"
        entries = sum(p.group.size for p in result.planned)
        if result.mode == "plan":
            return f"{prefix}digest plan: {len(result.planned)} groups, {entries} entries"
        done = [p for p in result.planned if p.text is not None]
        covered = sum(p.group.size for p in done)
        rejected = len(result.planned) - len(done)
        return (
            f"{prefix}digest: {len(done)} digests, {covered} entries covered, {rejected} rejected"
        )

    def _sections(self, result: _Result) -> list[tuple[str, str]]:
        if result.mode == "undo":
            lines = [f"- `{rel}`" for rel in result.back]
            heading = "Would come back" if result.dry_run else "Entries back"
            out = [(heading, "\n".join(lines) or "None.")]
            if result.notes:
                out.append(("Notes", "\n".join(f"- {n}" for n in result.notes)))
            return out
        verb = "would write" if result.mode == "plan" or result.dry_run else "wrote"
        lines = []
        for p in result.planned:
            g = p.group
            head = (
                f"- `{g.key}` (by {g.by}, {g.size} entries, {g.first.isoformat()} to "
                f"{g.last.isoformat()})"
            )
            if p.reason is not None:
                lines.append(f"{head}: rejected, {p.reason}")
            else:
                lines.append(f"{head}: {verb} `{p.rel}`")
        skipped = [f"- {reason}: {count}" for reason, count in sorted(result.skipped.items())]
        out = [("Groups", "\n".join(lines) or "None."), ("Skipped", "\n".join(skipped) or "None.")]
        if result.mode == "run" and result.dry_run:
            pages = "\n\n".join(
                f"`{p.rel}`:\n\n{fenced(p.text)}" for p in result.planned if p.text is not None
            )
            if pages:
                out.append(("Digests", pages))
        if result.notes:
            out.append(("Notes", "\n".join(f"- {n}" for n in result.notes)))
        return out


__all__ = ["MODES", "DigestJob"]
