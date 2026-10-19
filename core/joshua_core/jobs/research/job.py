"""The `research` job: one topic, its dossier, and cited pages.

Arguments:

- `mode`: `plan` or `run`. Required.
- `topic`: a topic page, relative to the wiki root. When it is absent,
  `select.select_topic` chooses one.
- `questions`: slugs of the dossier, as a list or a string with commas. Only
  with `mode: run`, and only with `topic`.
- `max_questions`: a cap on `jobs.research.questions_per_run` for this run.
- `then_insights`: true or false, only with `mode: run`. It replaces
  `jobs.insights.after_research` for this run.

Both modes choose the topic, build its memory bundle, and load or start its
dossier. When the dossier has fewer than `questions_per_run` open questions,
and the run names no `questions`, the `raise` worker proposes new ones and the
gate decides which the dossier takes.

`plan` stops there. When it is not a dry run, it writes the dossier and
`knowledge/README.md`, so a later run can name the new slugs. It makes no
lookup and writes no page.

`run` then researches the questions: those that the run names, or else the
open questions that a person asked, the other open questions, and the held
questions, in dossier order, up to the cap. Each question goes to the crew.
A published answer becomes a page, and the dossier and `README.md` get their
new state. A dry run does all of this and writes nothing; its report holds
the full text of each page it would write.

When a `run` publishes a page, `jobs.insights.enabled` is true, and
`then_insights` (or else `jobs.insights.after_research`) is true, the run then
does the insight pass for the same topic (`insights.job.InsightPass`). Its
paths join the paths of the run, so one commit holds both, its worker call
counts under `insight`, and its report is the section `Insights`. On a dry run
the pass is a dry run too, and it reads the pages the run would publish. A
failure of the pass is a line in that section, not a failure of the run.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from joshua_shared import layout
from joshua_shared.config import JoshuaConfig
from joshua_shared.log import get_logger

from joshua_core.engine.ephemeral import Runner
from joshua_core.engine.usage import Usage, UsageLedger
from joshua_core.jobs.bundle import Bundle, BundleStore, build_bundle
from joshua_core.jobs.research import crew
from joshua_core.jobs.research.dossier import (
    Dossier,
    Question,
    answers_on_disk,
    load_dossiers,
    new_dossier,
    question_slug,
    save_dossier,
    write_index,
)
from joshua_core.jobs.research.gate import Candidate, Gate, Verdict, default_embedder
from joshua_core.jobs.research.raise_ import WORKER as RAISE
from joshua_core.jobs.research.raise_ import raise_questions
from joshua_core.jobs.research.select import Selection, journal_counts, select_topic
from joshua_core.jobs.research.text import (
    Grounding,
    bundle_parts,
    code_span,
    extract_sentences,
    fenced,
)
from joshua_core.jobs.research.write import Page, PageTooLong, page_for, render_page, write_page
from joshua_core.jobs.runner import Clock, Job, JobArgsError, RunReport, current_run_id
from joshua_core.jobs.topics import Topic, is_topic_page, list_topics, load_topic

logger = get_logger("jobs.research")

MODES = ("plan", "run")
RULE_GIVEN = "given"
_ARGS = {"mode", "topic", "questions", "max_questions", "then_insights"}
_TRUE = ("true", "yes", "1", "on")
_FALSE = ("false", "no", "0", "off")


def worker_model(settings: JoshuaConfig) -> str | None:
    """`jobs.research.model`, else `jobs.model`, else `core.reflection_model`."""
    jobs = settings.jobs
    return jobs.research.model or jobs.model or settings.core.reflection_model


def local_day(now: datetime, settings: JoshuaConfig) -> date:
    if now.tzinfo is None:
        return now.date()
    return now.astimezone(ZoneInfo(settings.timezone)).date()


def researchable(topic_rel: str, data_root: Path | str) -> bool:
    """False for a topic whose dossier path would be `knowledge/README.md`."""
    try:
        return layout.knowledge_dossier(topic_rel, data_root) != layout.knowledge_index(data_root)
    except ValueError:
        return False


def parse_slugs(value: Any) -> list[str]:
    """The `questions` argument as a list of slugs. Raises `JobArgsError`."""
    if isinstance(value, str):
        items = [part.strip() for part in value.split(",") if part.strip()]
    elif isinstance(value, list) and all(isinstance(v, str) for v in value):
        items = [v.strip() for v in value if v.strip()]
    else:
        raise JobArgsError("questions is a list of slugs, or a string with commas")
    if not items:
        raise JobArgsError("questions names no slug")
    for item in items:
        try:
            layout.knowledge_page("x.md", item)
        except ValueError:
            raise JobArgsError(f"not a question slug: {item}") from None
    return list(dict.fromkeys(items))


def parse_count(value: Any) -> int:
    """The `max_questions` argument as a count of 1 or more. Raises `JobArgsError`."""
    if isinstance(value, bool):
        raise JobArgsError("max_questions is a whole number of 1 or more")
    try:
        number = int(str(value).strip()) if isinstance(value, str) else int(value)
    except (TypeError, ValueError):
        raise JobArgsError("max_questions is a whole number of 1 or more") from None
    if number < 1 or (isinstance(value, float) and value != number):
        raise JobArgsError("max_questions is a whole number of 1 or more")
    return number


def parse_flag(value: Any, name: str) -> bool:
    """A true or false argument, as a bool or a string such as `false`.
    Raises `JobArgsError`."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in _TRUE + _FALSE:
        return value.strip().lower() in _TRUE
    raise JobArgsError(f"{name} is true or false")


@dataclass
class _Outcome:
    question: Question
    status: str
    reason: str = ""
    page: Page | None = None
    short: str = ""
    dropped_urls: list[str] = field(default_factory=list)


@dataclass
class _Run:
    """The state of one run."""

    mode: str
    day: date
    now: datetime
    selection: Selection
    ledger: UsageLedger = field(default_factory=UsageLedger)
    added: list[Question] = field(default_factory=list)
    rejected: list[Verdict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    outcomes: list[_Outcome] = field(default_factory=list)
    insights: Any = None
    insights_error: str = ""
    insights_skipped: str = ""


class ResearchJob(Job):
    name = "research"

    def __init__(
        self,
        *,
        store: BundleStore,
        settings: JoshuaConfig,
        data_dir: Path | str,
        runner: Runner | None = None,
        internet_runner: Runner | None = None,
    ) -> None:
        self._store = store
        self._settings = settings
        self._data_dir = Path(data_dir)
        self._runner = runner
        self._internet_runner = internet_runner

    @property
    def _wiki(self) -> Path:
        return layout.wiki_root(self._data_dir)

    # -- arguments ------------------------------------------------------------

    def check_args(self, args: dict[str, Any]) -> dict[str, Any]:
        unknown = sorted(set(args) - _ARGS)
        if unknown:
            raise JobArgsError(f"unknown argument: {', '.join(unknown)}")
        mode = args.get("mode")
        if mode not in MODES:
            raise JobArgsError("mode is required: plan or run")
        out: dict[str, Any] = {"mode": mode}
        if mode == "run" and not self._settings.internet.enabled:
            raise JobArgsError(
                "a research run needs the internet agent, and internet.enabled is false"
            )
        topic = args.get("topic")
        if topic is not None and topic != "":
            if not isinstance(topic, str) or not is_topic_page(
                topic.strip(), self._wiki, self._settings.jobs.topics.exclude
            ):
                raise JobArgsError(f"not a topic page: {topic}")
            topic = topic.strip()
            if not researchable(topic, self._data_dir):
                raise JobArgsError(f"this topic has no dossier path: {topic}")
            out["topic"] = topic
        if args.get("questions") not in (None, "", []):
            if mode != "run":
                raise JobArgsError("questions is only for mode run")
            if "topic" not in out:
                raise JobArgsError("questions needs topic: a slug belongs to one dossier")
            slugs = parse_slugs(args["questions"])
            dossier = load_dossiers(self._data_dir).get(out["topic"])
            known = {q.slug for q in dossier.questions} if dossier else set()
            missing = [s for s in slugs if s not in known]
            if missing:
                raise JobArgsError(f"not in the dossier of {out['topic']}: {', '.join(missing)}")
            out["questions"] = slugs
        if args.get("max_questions") not in (None, ""):
            out["max_questions"] = parse_count(args["max_questions"])
        if args.get("then_insights") not in (None, ""):
            if mode != "run":
                raise JobArgsError("then_insights is only for mode run")
            out["then_insights"] = parse_flag(args["then_insights"], "then_insights")
        return out

    # -- the run --------------------------------------------------------------

    async def run_once(self, *, args: dict[str, Any], dry_run: bool, clock: Clock) -> RunReport:
        now = clock()
        day = local_day(now, self._settings)
        topics = [
            t
            for t in list_topics(self._wiki, self._settings.jobs.topics.exclude)
            if researchable(t.path, self._data_dir)
        ]
        dossiers = load_dossiers(self._data_dir)
        selection = self._select(args, topics, dossiers, day)
        run = _Run(mode=args["mode"], day=day, now=now, selection=selection)
        if selection.topic is None:
            return self._empty_report(run, dry_run)

        topic = load_topic(selection.topic, self._wiki)
        bundle = await build_bundle(
            topic, wiki_root=self._wiki, repo=self._store, settings=self._settings, now=now
        )
        dossier = dossiers.get(topic.path) or new_dossier(topic.path, topic.title)
        dossier.title = topic.title
        if "questions" not in args:
            await self._raise(run, topic, bundle, dossier)

        answers = answers_on_disk(dossier, self._data_dir)
        if run.mode == "run":
            await self._research(run, topic, bundle, dossier, args)
            for outcome in run.outcomes:
                if outcome.page is not None:
                    answers[outcome.page.rel] = outcome.short

        written: list[Path] = []
        if not dry_run:
            for outcome in run.outcomes:
                if outcome.page is not None:
                    written.append(write_page(outcome.page, self._data_dir))
            written.append(save_dossier(dossier, self._data_dir, answers))
            written.append(write_index(self._data_dir))
        if run.mode == "run" and any(o.page is not None for o in run.outcomes):
            written.extend(await self._insights(run, topic, bundle, args, dry_run))
        return self._report(run, topic, bundle, dossier, written, dry_run)

    async def _insights(
        self, run: _Run, topic: Topic, bundle: Bundle, args: dict[str, Any], dry_run: bool
    ) -> list[Path]:
        """Run the insight pass for the topic when the config and the run ask
        for it, and return the paths it wrote."""
        cfg = self._settings.jobs.insights
        wanted = args.get("then_insights")
        if wanted is None:
            wanted = cfg.after_research
        if not wanted:
            return []
        if not cfg.enabled:
            if args.get("then_insights"):
                run.insights_skipped = "jobs.insights.enabled is false, so no insight pass ran."
            return []
        # A late import: the insights package reads the researcher's modules.
        from joshua_core.jobs.insights.job import InsightPass

        pages = {o.page.rel: o.page.text for o in run.outcomes if o.page is not None}
        try:
            run.insights = await InsightPass(
                settings=self._settings,
                data_dir=self._data_dir,
                ledger=run.ledger,
                runner=self._runner,
            ).run(topic, bundle, today=run.day, dry_run=dry_run, extra_knowledge=pages)
        except Exception as exc:  # noqa: BLE001 — the pass never fails the research run
            logger.warning({"message": "insight pass failed", "error": str(exc)})
            run.insights_error = str(exc) or repr(exc)
            return []
        return list(run.insights.written)

    def _select(
        self,
        args: dict[str, Any],
        topics: list[Topic],
        dossiers: dict[str, Dossier],
        day: date,
    ) -> Selection:
        if "topic" in args:
            return Selection(args["topic"], RULE_GIVEN, None, "The run named the topic.")
        counts = journal_counts(topics, self._wiki, day, self._settings.jobs.bundle.recent_days)
        return select_topic(topics, dossiers, counts, day, self._settings)

    async def _raise(self, run: _Run, topic: Topic, bundle: Bundle, dossier: Dossier) -> None:
        cfg = self._settings.jobs.research
        open_now = dossier.count("open")
        if open_now >= cfg.questions_per_run:
            return
        count = cfg.questions_per_topic - open_now
        if count <= 0:
            return
        used: list[Usage] = []
        raised = await raise_questions(
            bundle_text=bundle.text,
            title=topic.title,
            aliases=bundle.aliases,
            existing=[q.question for q in dossier.questions],
            count=count,
            model=worker_model(self._settings),
            runner=self._runner,
            usage_out=used,
        )
        run.ledger.add(RAISE, used)
        if raised.candidates is None:
            run.notes.append("The raise worker gave no answer, so no question was added.")
            return
        gate = Gate(
            bundle_text=bundle.text,
            terms=[topic.title, *bundle.aliases],
            existing=[q.question for q in dossier.questions],
            store=self._store,
            dup_sim=cfg.dup_sim,
            data_root=self._data_dir,
            embed=default_embedder(self._settings, topic.title),
        )
        for candidate in raised.candidates:
            verdict = await gate.check(candidate)
            if not verdict.accepted:
                run.rejected.append(verdict)
                continue
            question = Question(
                slug=question_slug(candidate.question, (q.slug for q in dossier.questions)),
                question=candidate.question,
                grounded_in=list(candidate.grounded_in),
                raised_at=run.day,
            )
            dossier.questions.append(question)
            run.added.append(question)
        run.notes.extend(gate.notes)

    def _chosen(self, run: _Run, dossier: Dossier, args: dict[str, Any]) -> list[Question]:
        cfg = self._settings.jobs.research
        limit = min(cfg.questions_per_run, args.get("max_questions") or cfg.questions_per_run)
        if "questions" in args:
            chosen: list[Question] = []
            for slug in args["questions"]:
                question = dossier.find(slug)
                if question is None:
                    run.notes.append(f"`{slug}` is not in the dossier.")
                elif question.status not in ("open", "held"):
                    run.notes.append(f"`{slug}` is {question.status}, so it was not researched.")
                else:
                    chosen.append(question)
            return chosen[:limit]
        first = [q for q in dossier.questions if q.status == "open" and q.by_person]
        rest = [q for q in dossier.questions if q.status == "open" and not q.by_person]
        held = [q for q in dossier.questions if q.status == "held"]
        return [*first, *rest, *held][:limit]

    async def _research(
        self,
        run: _Run,
        topic: Topic,
        bundle: Bundle,
        dossier: Dossier,
        args: dict[str, Any],
    ) -> None:
        cfg = self._settings.jobs.research
        parts = bundle_parts(bundle)
        chosen = self._chosen(run, dossier, args)
        for question in chosen:
            groundings = extract_sentences(question.grounded_in, parts)
            result = await crew.research_question(
                question.question,
                groundings,
                internet_settings=self._settings.internet,
                research_settings=cfg,
                editor_model=worker_model(self._settings),
                ledger=run.ledger,
                internet_runner=self._internet_runner,
                editor_runner=self._runner,
            )
            page = self._page(topic, question, groundings, result, run)
            if page is not None and result.page is not None:
                question.status = "answered"
                question.page = page.rel
                question.hold_reason = ""
                run.outcomes.append(
                    _Outcome(
                        question,
                        "answered",
                        page=page,
                        short=result.page.answer_short,
                        dropped_urls=result.dropped_urls,
                    )
                )
                continue
            question.holds += 1
            question.hold_reason = result.hold_reason or crew.NO_ANSWER
            question.status = "dropped" if question.holds >= cfg.max_holds else "held"
            run.outcomes.append(
                _Outcome(
                    question,
                    question.status,
                    reason=question.hold_reason,
                    dropped_urls=result.dropped_urls,
                )
            )
        if chosen:
            dossier.researched_at = run.day
            dossier.refresh_after = run.day + timedelta(days=cfg.refresh_days)
            dossier.runs += 1

    def _page(
        self,
        topic: Topic,
        question: Question,
        groundings: list[Grounding],
        result: crew.CrewResult,
        run: _Run,
    ) -> Page | None:
        """The page of a published answer, or None with a hold on `result`."""
        if not result.publish or result.page is None:
            return None
        context = list(dict.fromkeys(g.path for g in groundings)) or [topic.path]
        try:
            text = render_page(
                question=question.question,
                topic=topic.path,
                context=context,
                grounded_in=question.grounded_in,
                answer=result.page,
                day=run.day,
                refresh_days=self._settings.jobs.research.refresh_days,
                run_id=current_run_id(),
            )
            return page_for(topic.path, question.slug, self._data_dir, text)
        except (PageTooLong, ValueError) as exc:
            result.holds.append(str(exc))
            return None

    # -- the report -------------------------------------------------------------

    def _usage_lines(self, run: _Run) -> str:
        data = run.ledger.as_data()
        if not data:
            return "No worker call."
        lines = [
            f"- `{name}`: {v['calls']} calls, {v['tokens']} tokens" for name, v in data.items()
        ]
        lines.append(f"- Total: {run.ledger.calls} calls, {run.ledger.tokens} tokens")
        return "\n".join(lines)

    def _topic_lines(self, run: _Run) -> str:
        sel = run.selection
        runner_up = f"`{sel.runner_up}`" if sel.runner_up else "none"
        return "\n".join(
            [
                f"- Topic: `{sel.topic}`",
                f"- Rule: `{sel.rule}`. {sel.reason}",
                f"- Runner-up: {runner_up}",
            ]
        )

    def _empty_report(self, run: _Run, dry_run: bool) -> RunReport:
        return RunReport(
            job=self.name,
            started_at=run.now,
            dry_run=dry_run,
            summary=f"No topic to research. {run.selection.reason}",
            sections=[("Topic", run.selection.reason)],
            data={
                "mode": run.mode,
                "topic": None,
                "rule": None,
                "reason": run.selection.reason,
                "usage": {},
            },
        )

    def _report(
        self,
        run: _Run,
        topic: Topic,
        bundle: Bundle,
        dossier: Dossier,
        written: list[Path],
        dry_run: bool,
    ) -> RunReport:
        added = {q.slug for q in run.added}
        question_lines = [
            f"- `{q.slug}` ({q.status}{', new' if q.slug in added else ''}"
            f"{', asked by ' + q.asked_by if q.by_person else ''}): {code_span(q.question)}"
            for q in dossier.questions
        ]
        rejected_lines = []
        for verdict in run.rejected:
            line = f"- {code_span(verdict.candidate.question)}: {verdict.reason}"
            if verdict.answered_by:
                line += f"; answered_by: `{verdict.answered_by}`"
            rejected_lines.append(line)
        bundle_lines = [f"- `{p}`" for p in bundle.paths]
        cut = ", cut at jobs.bundle.max_chars" if bundle.truncated else ""
        bundle_lines.append(f"- {bundle.chars} characters{cut}")
        sections = [
            ("Topic", self._topic_lines(run)),
            ("Bundle", "\n".join(bundle_lines)),
            ("Questions", "\n".join(question_lines) or "None."),
            ("Rejected", "\n".join(rejected_lines) or "None."),
        ]
        published = [o for o in run.outcomes if o.page is not None]
        held = [o for o in run.outcomes if o.status == "held"]
        dropped = [o for o in run.outcomes if o.status == "dropped"]
        if run.mode == "run":
            result_lines = []
            for o in run.outcomes:
                if o.page is not None:
                    verb = "would write" if dry_run else "wrote"
                    line = f"- `{o.question.slug}`: published; {verb} `knowledge/{o.page.rel}`"
                else:
                    line = f"- `{o.question.slug}`: {o.status} ({o.question.holds}): {o.reason}"
                if o.dropped_urls:
                    urls = ", ".join(code_span(u, 200) for u in o.dropped_urls)
                    line += f". Sources no lookup returned, dropped: {urls}"
                result_lines.append(line)
            sections.append(("Researched", "\n".join(result_lines) or "No question to research."))
            if dry_run and published:
                pages = "\n\n".join(
                    f"`knowledge/{o.page.rel}`:\n\n{fenced(o.page.text)}"
                    for o in published
                    if o.page is not None
                )
                sections.append(("Pages", pages))
        if run.notes:
            sections.append(("Notes", "\n".join(f"- {n}" for n in run.notes)))
        insights = self._insights_section(run)
        if insights:
            sections.append(("Insights", insights))
        sections.append(("Usage", self._usage_lines(run)))

        prefix = "Dry run: " if dry_run else ""
        if run.mode == "plan":
            summary = (
                f"{prefix}research plan for {topic.path}, chosen by the rule "
                f"`{run.selection.rule}`. {len(run.added)} new questions, "
                f"{len(run.rejected)} rejected, {dossier.count('open')} open."
            )
        else:
            summary = (
                f"{prefix}research: {topic.path} ({len(published)} pages, {len(held)} held, "
                f"{len(dropped)} dropped). Coverage {dossier.compute_coverage():.2f}."
            )
        return RunReport(
            job=self.name,
            started_at=run.now,
            dry_run=dry_run,
            summary=summary,
            sections=sections,
            calls=run.ledger.calls,
            tokens=run.ledger.tokens,
            written=written,
            data={
                "mode": run.mode,
                "topic": topic.path,
                "rule": run.selection.rule,
                "runner_up": run.selection.runner_up,
                "reason": run.selection.reason,
                "bundle": {"paths": bundle.paths, "chars": bundle.chars},
                "slugs": [q.slug for q in dossier.questions],
                "questions": [{"slug": q.slug, "status": q.status} for q in dossier.questions],
                "added": [q.slug for q in run.added],
                "rejected": [
                    {
                        "question": v.candidate.question,
                        "reason": v.reason,
                        "answered_by": v.answered_by,
                    }
                    for v in run.rejected
                ],
                "pages": [f"knowledge/{o.page.rel}" for o in published if o.page is not None],
                "held": [o.question.slug for o in held],
                "dropped": [o.question.slug for o in dropped],
                "coverage": dossier.compute_coverage(),
                "usage": run.ledger.as_data(),
                "insights": self._insights_data(run),
            },
        )

    def _insights_section(self, run: _Run) -> str:
        """The body of the `Insights` section, or `""` when no pass ran."""
        if run.insights_error:
            return f"The insight pass failed: {run.insights_error}"
        if run.insights_skipped:
            return run.insights_skipped
        if run.insights is None:
            return ""
        lines = [run.insights.summary(), ""]
        for heading, body in run.insights.sections():
            lines += [f"### {heading}", "", body.strip() or "None.", ""]
        return "\n".join(lines).strip()

    def _insights_data(self, run: _Run) -> dict[str, Any] | None:
        if run.insights_error:
            return {"error": run.insights_error}
        if run.insights is None:
            return None
        return dict(run.insights.data())


__all__ = [
    "Candidate",
    "ResearchJob",
    "parse_count",
    "parse_flag",
    "parse_slugs",
    "worker_model",
]
