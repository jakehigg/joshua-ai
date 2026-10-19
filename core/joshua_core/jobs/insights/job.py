"""The `insights` job: where what we do disagrees with what is known, for one topic.

Arguments: `{"topic": "<path>"}`, a topic page relative to the wiki root.

A run does these steps in this order:

1. It loads the topic and builds its memory bundle.
2. It expires each open insight of the topic whose `valid_until` is before
   today (see `page.expire`).
3. It loads the published knowledge pages of the topic. With none, the
   report says `no knowledge for this topic` and the run stops. That is not
   an error.
4. The `insight` worker reads the bundle, the knowledge pages, the day, and
   the open insights.
5. The gate decides which insights are new, which refresh an open insight,
   and which are rejected. At most `jobs.insights.max_per_run` new insights
   are written.
6. It writes each new and refreshed page and `insights/README.md`.

A dry run does the worker call and the gate, and writes nothing. It does not
expire an insight either. Its report shows the full text of each page that a
run would write.

`InsightPass` is steps 2 to 6 for a topic and a bundle that the caller
gives. The research job runs it at the end of a research run.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from joshua_shared import layout
from joshua_shared.config import JoshuaConfig
from joshua_shared.log import get_logger

from joshua_core.engine.ephemeral import Runner
from joshua_core.engine.usage import Usage, UsageLedger
from joshua_core.jobs.bundle import Bundle, BundleStore, build_bundle
from joshua_core.jobs.insights.gate import InsightGate, Verdict, cap, we_do_parts
from joshua_core.jobs.insights.knowledge import KnownPage, published_pages
from joshua_core.jobs.insights.page import (
    HEADING_ACTION,
    HEADING_WHY,
    Body,
    Insight,
    InsightFile,
    body_sections,
    due_to_expire,
    expire,
    insight_path,
    insight_section,
    load_insights,
    render_insight,
    write_index,
    write_text,
)
from joshua_core.jobs.insights.worker import WORKER, insight_text, raise_insights
from joshua_core.jobs.research.dossier import question_slug
from joshua_core.jobs.research.gate import default_embedder
from joshua_core.jobs.research.text import bundle_parts, code_span, fenced
from joshua_core.jobs.runner import Clock, Job, JobArgsError, RunReport, current_run_id
from joshua_core.jobs.topics import Topic, is_topic_page, load_topic

logger = get_logger("jobs.insights")

NO_KNOWLEDGE = "no knowledge for this topic"


def insight_model(settings: JoshuaConfig) -> str | None:
    """`jobs.insights.model`, else `jobs.model`, else `core.reflection_model`."""
    jobs = settings.jobs
    return jobs.insights.model or jobs.model or settings.core.reflection_model


def local_day(now: datetime, settings: JoshuaConfig) -> date:
    if now.tzinfo is None:
        return now.date()
    return now.astimezone(ZoneInfo(settings.timezone)).date()


@dataclass(frozen=True)
class Written:
    """A page that the pass writes, or would write in a dry run."""

    id: str
    path: Path
    text: str
    insight: Insight


@dataclass
class PassResult:
    """What one insight pass did."""

    topic: str
    dry_run: bool
    no_knowledge: bool = False
    known: list[str] = field(default_factory=list)
    new: list[Written] = field(default_factory=list)
    refreshed: list[Written] = field(default_factory=list)
    expired: list[InsightFile] = field(default_factory=list)
    rejected: list[tuple[str, str]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    written: list[Path] = field(default_factory=list)

    def summary(self) -> str:
        prefix = "Dry run: " if self.dry_run else ""
        if self.no_knowledge:
            tail = f" {len(self.expired)} expired." if self.expired else ""
            return f"{prefix}insights: {self.topic}: {NO_KNOWLEDGE}.{tail}"
        return (
            f"{prefix}insights: {self.topic} ({len(self.new)} new, {len(self.refreshed)} "
            f"refreshed, {len(self.expired)} expired, {len(self.rejected)} rejected)"
        )

    def sections(self) -> list[tuple[str, str]]:
        """The report sections, without the usage."""
        verb = "would write" if self.dry_run else "wrote"
        new_lines = []
        for item in self.new:
            i = item.insight
            new_lines.append(
                f"- `{item.id}` ({i.severity}, {i.confidence}, valid {i.valid_from.isoformat()} "
                f"to {i.valid_until.isoformat()}): {code_span(i.claim)}; {verb} "
                f"`{layout.INSIGHTS}/{item.id}.md`\n"
                f"  - we do: {', '.join(code_span(p, 120) for p in i.we_do)}\n"
                f"  - knowledge says: {', '.join(f'`{p}`' for p in i.knowledge_says)}"
            )
        refreshed = [
            f"- `{item.id}` ({item.insight.status}): {code_span(item.insight.claim)}; seen "
            f"{item.insight.seen_at.isoformat()}"
            for item in self.refreshed
        ]
        expired = [
            f"- `{item.id}`: {code_span(item.insight.claim)}; valid until "
            f"{item.insight.valid_until.isoformat()}"
            for item in self.expired
        ]
        rejected = [f"- {code_span(claim)}: {reason}" for claim, reason in self.rejected]
        if self.no_knowledge:
            out = [("Knowledge", f"There is {NO_KNOWLEDGE}. No insight was raised.")]
        else:
            out = [
                ("Knowledge", "\n".join(f"- `knowledge/{p}`" for p in self.known)),
                ("New", "\n".join(new_lines) or "None."),
                ("Refreshed", "\n".join(refreshed) or "None."),
            ]
        out.append(
            ("Expired" if not self.dry_run else "Would expire", "\n".join(expired) or "None.")
        )
        if not self.no_knowledge:
            out.append(("Rejected", "\n".join(rejected) or "None."))
        if self.dry_run and (self.new or self.refreshed):
            pages = "\n\n".join(
                f"`{layout.INSIGHTS}/{item.id}.md`:\n\n{fenced(item.text)}"
                for item in [*self.new, *self.refreshed]
            )
            out.append(("Pages", pages))
        if self.notes:
            out.append(("Notes", "\n".join(f"- {n}" for n in self.notes)))
        return out

    def data(self) -> dict[str, Any]:
        return {
            "topic": self.topic,
            "no_knowledge": self.no_knowledge,
            "knowledge": self.known,
            "new": [item.id for item in self.new],
            "refreshed": [item.id for item in self.refreshed],
            "expired": [item.id for item in self.expired],
            "rejected": [{"claim": c, "reason": r} for c, r in self.rejected],
        }


class InsightPass:
    """Steps 2 to 6 of an insights run for one topic. `ledger` counts the
    worker call under the name `insight`."""

    def __init__(
        self,
        *,
        settings: JoshuaConfig,
        data_dir: Path | str,
        ledger: UsageLedger,
        runner: Runner | None = None,
    ) -> None:
        self._settings = settings
        self._data_dir = Path(data_dir)
        self._ledger = ledger
        self._runner = runner

    async def run(
        self,
        topic: Topic,
        bundle: Bundle,
        *,
        today: date,
        dry_run: bool,
        extra_knowledge: Mapping[str, str] | None = None,
    ) -> PassResult:
        """Run the pass. `extra_knowledge` maps a knowledge page path, relative
        to `wiki/knowledge/`, to its text, for a page that is not on the disk yet."""
        result = PassResult(topic=topic.path, dry_run=dry_run)
        cfg = self._settings.jobs.insights
        mine = load_insights(self._data_dir, topic.path)
        if dry_run:
            result.expired = due_to_expire(mine, today)
        else:
            result.expired, written = expire(self._data_dir, topic.path, today, dry_run=False)
            result.written.extend(written)
        gone = {item.id for item in result.expired}
        mine = [item for item in mine if item.id not in gone]

        known = published_pages(topic.path, self._data_dir, extra_knowledge)
        result.known = list(known)
        if not known:
            result.no_knowledge = True
            return result

        open_now = [item for item in mine if item.insight.is_open]
        dismissed = [item for item in mine if item.insight.status == "dismissed"]
        parts = we_do_parts(bundle_parts(bundle))
        used: list[Usage] = []
        raised = await raise_insights(
            user_text=insight_text(
                topic=topic.path,
                title=topic.title,
                parts=parts,
                known=list(known.values()),
                open_insights=open_now,
                today=today,
                timezone=self._settings.timezone,
                instance=self._settings.name,
            ),
            model=insight_model(self._settings),
            runner=self._runner,
            usage_out=used,
        )
        self._ledger.add(WORKER, used)
        if raised.candidates is None:
            result.notes.append("The insight worker gave no answer, so nothing changed.")
            return result
        result.rejected.extend(raised.invalid)
        gate = InsightGate(
            parts=parts,
            known=known,
            open_insights=open_now,
            dismissed=dismissed,
            today=today,
            default_valid_days=cfg.default_valid_days,
            dup_sim=cfg.dup_sim,
            embed=default_embedder(self._settings, topic.title),
        )
        verdicts: list[Verdict] = [await gate.check(c) for c in raised.candidates]
        result.notes.extend(gate.notes)
        for verdict in verdicts:
            if verdict.kind == "rejected":
                result.rejected.append((verdict.claim, verdict.reason))
        keep, over = cap(verdicts, cfg.max_per_run)
        for verdict in over:
            result.rejected.append(
                (verdict.claim, f"over jobs.insights.max_per_run ({cfg.max_per_run})")
            )

        refreshed: dict[str, Written] = {}
        for verdict in verdicts:
            if verdict.kind == "refresh" and verdict.target is not None:
                refreshed[verdict.target.id] = self._refresh(verdict, known, today)
        result.refreshed = list(refreshed.values())
        taken: dict[str, set[str]] = {}
        for verdict in keep:
            result.new.append(self._new(topic, verdict, known, today, taken))

        if not dry_run and (result.new or result.refreshed):
            for item in [*result.new, *result.refreshed]:
                result.written.append(write_text(item.path, item.text, self._data_dir))
            index = write_index(self._data_dir)
            if index not in result.written:
                result.written.append(index)
        return result

    def _body(
        self, verdict: Verdict, known: Mapping[str, KnownPage], why: str, action: str
    ) -> Body:
        assert verdict.checked is not None
        return Body(
            why=why,
            action=action,
            evidence=verdict.checked.evidence,
            phrase_paths=verdict.checked.phrase_paths,
            known={rel: page.answer_short for rel, page in known.items()},
        )

    def _new(
        self,
        topic: Topic,
        verdict: Verdict,
        known: Mapping[str, KnownPage],
        today: date,
        taken: dict[str, set[str]],
    ) -> Written:
        checked = verdict.checked
        assert checked is not None
        section = insight_section(topic.path)
        used = taken.setdefault(section, self._slugs(section))
        slug = question_slug(checked.claim, used)
        used.add(slug)
        insight_id = f"{section}/{slug}"
        insight = Insight(
            topic=topic.path,
            section=section,
            claim=checked.claim,
            status="new",
            severity=checked.severity,  # type: ignore[arg-type]
            valid_from=checked.valid_from,
            valid_until=checked.valid_until,
            we_do=checked.we_do,
            we_do_paths=checked.we_do_paths,
            knowledge_says=checked.knowledge_says,
            confidence=checked.confidence,  # type: ignore[arg-type]
            found_at=today,
            seen_at=today,
            run_id=current_run_id(),
        )
        text = render_insight(
            insight_id, insight, self._body(verdict, known, checked.why, checked.action)
        )
        return Written(insight_id, insight_path(insight_id, self._data_dir), text, insight)

    def _refresh(self, verdict: Verdict, known: Mapping[str, KnownPage], today: date) -> Written:
        checked = verdict.checked
        target = verdict.target
        assert checked is not None and target is not None
        old = body_sections(target.text)
        insight = target.insight.model_copy(
            update={
                "seen_at": today,
                "we_do": checked.we_do,
                "we_do_paths": checked.we_do_paths,
                "knowledge_says": checked.knowledge_says,
            }
        )
        body = self._body(
            verdict,
            known,
            old.get(HEADING_WHY, checked.why),
            old.get(HEADING_ACTION, checked.action),
        )
        text = render_insight(target.id, insight, body)
        return Written(target.id, target.path, text, insight)

    def _slugs(self, section: str) -> set[str]:
        folder = layout.insights_root(self._data_dir) / section
        if not folder.is_dir():
            return set()
        return {p.stem for p in folder.glob("*.md")}


def usage_lines(ledger: UsageLedger) -> str:
    data = ledger.as_data()
    if not data:
        return "No worker call."
    lines = [f"- `{name}`: {v['calls']} calls, {v['tokens']} tokens" for name, v in data.items()]
    lines.append(f"- Total: {ledger.calls} calls, {ledger.tokens} tokens")
    return "\n".join(lines)


class InsightsJob(Job):
    name = "insights"

    def __init__(
        self,
        *,
        store: BundleStore,
        settings: JoshuaConfig,
        data_dir: Path | str,
        runner: Runner | None = None,
    ) -> None:
        self._store = store
        self._settings = settings
        self._data_dir = Path(data_dir)
        self._runner = runner

    @property
    def _wiki(self) -> Path:
        return layout.wiki_root(self._data_dir)

    def check_args(self, args: dict[str, Any]) -> dict[str, Any]:
        unknown = sorted(set(args) - {"topic"})
        if unknown:
            raise JobArgsError(f"unknown argument: {', '.join(unknown)}")
        topic = args.get("topic")
        if not isinstance(topic, str) or not topic.strip():
            raise JobArgsError("topic is required: a wiki page path such as garden/irrigation.md")
        topic = topic.strip()
        if not is_topic_page(topic, self._wiki, self._settings.jobs.topics.exclude):
            raise JobArgsError(f"not a topic page: {topic}")
        return {"topic": topic}

    async def run_once(self, *, args: dict[str, Any], dry_run: bool, clock: Clock) -> RunReport:
        now = clock()
        today = local_day(now, self._settings)
        topic = load_topic(args["topic"], self._wiki)
        bundle = await build_bundle(
            topic, wiki_root=self._wiki, repo=self._store, settings=self._settings, now=now
        )
        ledger = UsageLedger()
        result = await InsightPass(
            settings=self._settings, data_dir=self._data_dir, ledger=ledger, runner=self._runner
        ).run(topic, bundle, today=today, dry_run=dry_run)
        sections = [*result.sections(), ("Usage", usage_lines(ledger))]
        return RunReport(
            job=self.name,
            started_at=now,
            dry_run=dry_run,
            args=args,
            summary=result.summary(),
            sections=sections,
            calls=ledger.calls,
            tokens=ledger.tokens,
            written=list(result.written),
            data={**result.data(), "usage": ledger.as_data()},
        )


__all__ = ["InsightPass", "InsightsJob", "PassResult", "insight_model", "usage_lines"]
