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
gate gives each one its route: a `web` question goes to the dossier, a
`person` question becomes a page under `wiki/questions/`, and a `record`
question that our records answer is closed in the report. Nobody gets a
question: a person answers one later, in chat.

`plan` stops there. When it is not a dry run, it writes the dossier,
`knowledge/README.md`, and each new question page with `questions/README.md`,
so a later run can name the new slugs. It makes no lookup and writes no
knowledge page.

`run` then researches the questions, up to the cap: those that the run
names, or else the questions in the order of `research_order`. The held
questions that need only the editor come first, then the open questions that
a person asked, the other open questions, and the other held questions. Each
question goes to the crew. A held question that kept its two lookups, not
older than `jobs.research.lookup_max_age_days`, goes to the editor alone. A held or
dropped question keeps the lookups of the run, and a published one loses
them. A published answer becomes a page, and the dossier and `README.md` get their
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
from joshua_core.jobs.research.gate import (
    NO_RECORD,
    PERSON,
    RECORD,
    AskedQuestion,
    Candidate,
    Gate,
    Verdict,
    default_embedder,
    roster_names,
)
from joshua_core.jobs.research.questions import (
    QuestionPage,
    counts,
    load_questions,
    new_slug,
    question_rel,
    question_section,
    roster,
    who_would_know,
    write_question,
)
from joshua_core.jobs.research.questions import write_index as write_questions_index
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
from joshua_core.jobs.topics import Topic, is_topic_page, list_topics, load_topic, topic_names

logger = get_logger("jobs.research")

MODES = ("plan", "run")
RULE_GIVEN = "given"
EDITOR_ONLY = "editor only"
FULL_CREW = "full crew"
_ARGS = {"mode", "topic", "questions", "max_questions", "then_insights"}
_TRUE = ("true", "yes", "1", "on")
_FALSE = ("false", "no", "0", "off")
# The `why` of a question for a person when the worker gave none.
_WHY_NO_RECORD = "The researcher looked for this in our pages and the journal and did not find it."
_WHY_ABOUT_US = "Only somebody here knows this: the web cannot answer a question about us."


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


def research_order(questions: list[Question], day: date, max_age_days: int) -> list[Question]:
    """The questions a run researches when it names none, in this order:

    1. The held questions with kept lookups of `max_age_days` or less, which
       need only the editor, the oldest `looked_up_at` first.
    2. The open questions that a person asked.
    3. The other open questions.
    4. The other held questions.

    Within a group, the order is the order of the dossier.
    """
    held = [q for q in questions if q.status == "held"]
    retry = sorted(
        (q for q in held if q.kept_lookups(day, max_age_days) is not None),
        key=lambda q: q.looked_up_at or day,
    )
    by_person = [q for q in questions if q.status == "open" and q.by_person]
    other = [q for q in questions if q.status == "open" and not q.by_person]
    rest = [q for q in held if q not in retry]
    return [*retry, *by_person, *other, *rest]


@dataclass
class _Outcome:
    question: Question
    status: str
    reason: str = ""
    page: Page | None = None
    short: str = ""
    dropped_urls: list[str] = field(default_factory=list)
    reused: bool = False
    usage: dict[str, Usage] = field(default_factory=dict)

    @property
    def crew(self) -> str:
        """`editor only` or `full crew`: which steps ran for the question."""
        return EDITOR_ONLY if self.reused else FULL_CREW

    def tokens(self) -> dict[str, int]:
        """The tokens of each step of the crew; 0 for a step that did not run."""
        return {step: self.usage.get(step, Usage()).total for step in crew.STEPS}


@dataclass
class _PersonQuestion:
    """A new question for a person: its id, its page, and the gate's verdict."""

    id: str
    page: QuestionPage
    verdict: Verdict


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
    person: list[_PersonQuestion] = field(default_factory=list)
    asked_before: list[Verdict] = field(default_factory=list)
    record: list[Verdict] = field(default_factory=list)
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
            for item in run.person:
                written.append(write_question(item.id, item.page, self._data_dir))
            if run.person:
                written.append(write_questions_index(self._data_dir))
            dossier.person_open, dossier.person_answered = counts(self._data_dir, topic.path)
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
            terms=topic_names(topic, bundle.aliases),
            existing=[q.question for q in dossier.questions],
            store=self._store,
            dup_sim=cfg.dup_sim,
            data_root=self._data_dir,
            embed=default_embedder(self._settings, topic.title),
            people=roster_names(self._settings),
            record_sim=cfg.record_sim,
            asked=[
                AskedQuestion(q.page.question, q.page.status, q.rel)
                for q in load_questions(self._data_dir, topic.path)
            ],
        )
        for candidate in raised.candidates:
            verdict = await gate.check(candidate)
            if not verdict.accepted:
                if verdict.route == PERSON:
                    run.asked_before.append(verdict)
                else:
                    run.rejected.append(verdict)
                continue
            if verdict.route == RECORD:
                run.record.append(verdict)
                continue
            if verdict.route == PERSON:
                run.person.append(self._person_question(run, topic, bundle, verdict))
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

    def _person_question(
        self, run: _Run, topic: Topic, bundle: Bundle, verdict: Verdict
    ) -> _PersonQuestion:
        """The page of a new question for a person, not yet written."""
        candidate = verdict.candidate
        groundings = extract_sentences(candidate.grounded_in, bundle_parts(bundle))
        section = question_section(topic.path)
        taken = [p.id.split("/", 1)[1] for p in run.person if p.id.startswith(f"{section}/")]
        slug = new_slug(section, candidate.question, self._data_dir, taken)
        why = candidate.why or (
            _WHY_NO_RECORD if verdict.route_reason == NO_RECORD else _WHY_ABOUT_US
        )
        page = QuestionPage.model_validate(
            {
                "question": candidate.question,
                "topic": topic.path,
                "section": section,
                "why": why,
                "for": who_would_know(
                    [*(g.sentence for g in groundings), *candidate.grounded_in],
                    roster(self._settings),
                ),
                "context": list(dict.fromkeys(g.path for g in groundings)) or [topic.path],
                "grounded_in": list(candidate.grounded_in),
                "route_reason": verdict.route_reason,
                "raised_at": run.day,
                "run_id": current_run_id(),
            }
        )
        return _PersonQuestion(f"{section}/{slug}", page, verdict)

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
        return research_order(dossier.questions, run.day, cfg.lookup_max_age_days)[:limit]

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
            kept = question.kept_lookups(run.day, cfg.lookup_max_age_days)
            result = await crew.research_question(
                question.question,
                groundings,
                internet_settings=self._settings.internet,
                research_settings=cfg,
                editor_model=worker_model(self._settings),
                ledger=run.ledger,
                internet_runner=self._internet_runner,
                editor_runner=self._runner,
                draft=kept[0] if kept else None,
                challenge=kept[1] if kept else None,
            )
            page = self._page(topic, question, groundings, result, run)
            if page is not None and result.page is not None:
                question.status = "answered"
                question.page = page.rel
                question.hold_reason = ""
                question.clear_lookups()
                run.outcomes.append(
                    _Outcome(
                        question,
                        "answered",
                        page=page,
                        short=result.page.answer_short,
                        dropped_urls=result.dropped_urls,
                        reused=kept is not None,
                        usage=result.usage,
                    )
                )
                continue
            question.holds += 1
            question.hold_reason = result.hold_reason or crew.NO_ANSWER
            question.status = "dropped" if question.holds >= cfg.max_holds else "held"
            if kept is None:
                question.keep_lookups(result.draft, result.challenge, run.day)
            run.outcomes.append(
                _Outcome(
                    question,
                    question.status,
                    reason=question.hold_reason,
                    dropped_urls=result.dropped_urls,
                    reused=kept is not None,
                    usage=result.usage,
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
        for o in run.outcomes:
            steps = ", ".join(f"{step} {n} tokens" for step, n in o.tokens().items())
            lines.append(f"- `{o.question.slug}`: {steps}")
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
            ("For a person", self._person_lines(run, dry_run)),
            ("Answered by our records", self._record_lines(run)),
            ("Rejected", "\n".join(rejected_lines) or "None."),
        ]
        published = [o for o in run.outcomes if o.page is not None]
        held = [o for o in run.outcomes if o.status == "held"]
        dropped = [o for o in run.outcomes if o.status == "dropped"]
        if run.mode == "run":
            result_lines = []
            for o in run.outcomes:
                head = f"- `{o.question.slug}` ({o.crew})"
                if o.page is not None:
                    verb = "would write" if dry_run else "wrote"
                    line = f"{head}: published; {verb} `knowledge/{o.page.rel}`"
                else:
                    line = f"{head}: {o.status} ({o.question.holds}): {o.reason}"
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
                f"{len(run.person)} for a person, {len(run.record)} answered by our "
                f"records, {len(run.rejected)} rejected, {dossier.count('open')} open."
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
                "person": [p.id for p in run.person],
                "asked_before": [
                    {"question": v.candidate.question, "reason": v.reason, "page": v.page}
                    for v in run.asked_before
                ],
                "record": [v.answered_by for v in run.record],
                "pages": [f"knowledge/{o.page.rel}" for o in published if o.page is not None],
                "held": [o.question.slug for o in held],
                "dropped": [o.question.slug for o in dropped],
                "editor_only": [o.question.slug for o in run.outcomes if o.reused],
                "coverage": dossier.compute_coverage(),
                "usage": run.ledger.as_data(),
                "usage_by_question": {o.question.slug: o.tokens() for o in run.outcomes},
                "insights": self._insights_data(run),
            },
        )

    def _person_lines(self, run: _Run, dry_run: bool) -> str:
        """The body of the `For a person` section."""
        verb = "would write" if dry_run else "wrote"
        lines = [
            f"- `{p.id}` (for {p.page.for_}; {p.verdict.route_reason}): "
            f"{code_span(p.page.question)}. Why: {code_span(p.page.why)}. "
            f"{verb} `{question_rel(p.id)}`"
            for p in run.person
        ]
        for v in run.asked_before:
            where = f" (`{v.page}`)" if v.page else ""
            lines.append(f"- {code_span(v.candidate.question)}: {v.reason}{where}")
        return "\n".join(lines) or "None."

    def _record_lines(self, run: _Run) -> str:
        """The body of the `Answered by our records` section."""
        lines = [
            f"- {code_span(v.candidate.question)}: answered_by: `{v.answered_by}`"
            for v in run.record
        ]
        return "\n".join(lines) or "None."

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
    "research_order",
    "worker_model",
]
