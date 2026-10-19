"""In-process ``research`` tool server: start the researcher, and ask it a question.

``plan_research`` starts a ``plan`` run and ``run_research`` a ``run`` run of
the ``research`` job, through the job runner, so a run from chat is the same
as a run from the laptop: one row, one journal entry, one wiki commit.
Neither is a dry run. ``queue_question`` adds an open question that a person
asked to the dossier of a topic, and ``research_status`` gives the summary of
the last run.

Every tool gates on the current speaker being a member: a guest, or a speaker
core does not know, gets a "not available" reply and nothing changes.

``queue_question`` takes the lock that the jobs and the nightly reflection
share, writes the dossier and ``knowledge/README.md``, and commits the wiki
with the message ``research: question from <person>``. A person may ask a
general question: the question must have 20 to 300 characters, end with a
question mark, and name the topic, and no other rule of the gate applies.

Core registers this server only when ``jobs.enabled`` and
``jobs.research.enabled`` are both true.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from joshua_shared import frontmatter, layout, wikigit
from joshua_shared.config import JoshuaConfig
from joshua_shared.log import get_logger

from joshua_core.engine.agent import create_sdk_mcp_server, tool
from joshua_core.engine.tools import ToolDeps
from joshua_core.jobs.bundle import topic_aliases
from joshua_core.jobs.research.dossier import (
    Question,
    answers_on_disk,
    load_dossier,
    new_dossier,
    question_slug,
    save_dossier,
    write_index,
)
from joshua_core.jobs.research.gate import shape_reason
from joshua_core.jobs.research.job import local_day, researchable
from joshua_core.jobs.runner import Clock, JobArgsError, JobBusy, JobRunner, UnknownJob, utc_now
from joshua_core.jobs.topics import is_topic_page, load_topic, read_page, topic_names

logger = get_logger("tools.research")

JOB = "research"
NOT_AVAILABLE = "That is not available to you. Only a member can use the researcher."
BUSY = "A job or the nightly reflection is writing the wiki now. Try again in a few minutes."


@dataclass(frozen=True)
class ResearchContext:
    """What the tools need beyond the conversation: the config, the data root,
    the wiki lock, the job runner, and the clock."""

    settings: JoshuaConfig
    data_dir: Path
    lock: asyncio.Lock
    jobs: JobRunner
    clock: Clock = utc_now


def _reply(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}]}


async def _member_id(deps: ToolDeps) -> str | None:
    """The id of the current speaker if they are a member, else None."""
    pid = deps.person_id
    if not pid:
        return None
    person = await deps.repo.get_person(pid)
    if person is None or person.role != "member":
        return None
    return person.id


async def _start(ctx: ResearchContext, args: dict[str, Any]) -> dict[str, Any]:
    try:
        run_id = await ctx.jobs.start(JOB, args, dry_run=False)
    except JobBusy:
        return _reply(BUSY)
    except JobArgsError as exc:
        return _reply(f"I did not start the research: {exc}.")
    except UnknownJob:
        return _reply("The researcher is not turned on for this instance.")
    return _reply(
        f"Started research run {run_id} ({args['mode']}). It works in the background. "
        "When it ends, its report is a journal entry; call research_status to read "
        "the summary."
    )


async def do_plan_research(
    deps: ToolDeps, ctx: ResearchContext, *, topic: str | None = None
) -> dict[str, Any]:
    if await _member_id(deps) is None:
        return _reply(NOT_AVAILABLE)
    args: dict[str, Any] = {"mode": "plan"}
    if topic:
        args["topic"] = topic
    return await _start(ctx, args)


async def do_run_research(
    deps: ToolDeps,
    ctx: ResearchContext,
    *,
    topic: str | None,
    questions: Any = None,
    max_questions: Any = None,
) -> dict[str, Any]:
    if await _member_id(deps) is None:
        return _reply(NOT_AVAILABLE)
    args: dict[str, Any] = {"mode": "run"}
    if topic:
        args["topic"] = topic
    if questions:
        args["questions"] = questions
    if max_questions:
        args["max_questions"] = max_questions
    return await _start(ctx, args)


async def do_queue_question(
    deps: ToolDeps, ctx: ResearchContext, *, topic: str, question: str
) -> dict[str, Any]:
    member = await _member_id(deps)
    if member is None:
        return _reply(NOT_AVAILABLE)
    topic = (topic or "").strip()
    wiki = layout.wiki_root(ctx.data_dir)
    people = ctx.settings.people
    if not is_topic_page(
        topic, wiki, ctx.settings.jobs.topics.exclude, people=people
    ) or not researchable(topic, ctx.data_dir):
        return _reply(f"{topic or 'That'} is not a topic page, such as houseplants/snake-plant.md.")
    text = " ".join((question or "").split())
    page = load_topic(topic, wiki)
    meta, _ = frontmatter.split(read_page(wiki / topic))
    terms = topic_names(page, topic_aliases(page, meta, wiki), people=people)
    reason = shape_reason(text, terms)
    if reason:
        return _reply(
            f"I did not queue it: {reason}. Name {page.title} in the question, and end it "
            "with a question mark."
        )
    if ctx.lock.locked():
        return _reply(BUSY)
    async with ctx.lock:
        now = ctx.clock()
        dossier = load_dossier(topic, ctx.data_dir) or new_dossier(topic, page.title)
        dossier.title = page.title
        slug = question_slug(text, (q.slug for q in dossier.questions))
        dossier.questions.append(
            Question(
                slug=slug,
                question=text,
                asked_by=member,
                raised_at=local_day(now, ctx.settings),
            )
        )
        written = [
            save_dossier(dossier, ctx.data_dir, answers_on_disk(dossier, ctx.data_dir)),
            write_index(ctx.data_dir),
        ]
        if wikigit.is_enabled(ctx.settings):
            wikigit.commit(wiki, written, f"research: question from {member}")
    logger.info({"message": "research question queued", "person": member})
    return _reply(
        f"Queued the question as `{slug}` for {topic}. The next research run takes "
        "a topic with an open question from a person first."
    )


async def do_research_status(deps: ToolDeps, ctx: ResearchContext) -> dict[str, Any]:
    if await _member_id(deps) is None:
        return _reply(NOT_AVAILABLE)
    row = await ctx.jobs.last_run(JOB)
    if row is None:
        return _reply("The researcher has not run yet.")
    report = row.get("report") or {}
    if not row.get("finished_at"):
        return _reply(f"Research run {row.get('id')} is still working.")
    summary = report.get("summary") or "no summary"
    error = f" It failed: {row['error']}." if row.get("error") else ""
    return _reply(f"Research run {row.get('id')}: {summary}{error}")


# --- server ----------------------------------------------------------------

_PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "topic": {
            "type": "string",
            "description": "A topic page, such as houseplants/snake-plant.md. Leave it out "
            "to let the researcher choose.",
        }
    },
}

_RUN_SCHEMA = {
    "type": "object",
    "properties": {
        "topic": {"type": "string", "description": "A topic page, such as garden/roses.md."},
        "questions": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Question slugs of the dossier of the topic. Optional.",
        },
        "max_questions": {
            "type": "integer",
            "description": "The most questions to research in this run. Optional.",
        },
    },
    "required": ["topic"],
}

_QUEUE_SCHEMA = {
    "type": "object",
    "properties": {
        "topic": {"type": "string", "description": "A topic page, such as garden/roses.md."},
        "question": {
            "type": "string",
            "description": "The question, 20 to 300 characters, that names the topic and "
            "ends with a question mark.",
        },
    },
    "required": ["topic", "question"],
}


def build_research_server(deps: ToolDeps, ctx: ResearchContext) -> Any:
    """Build the in-process ``research`` MCP server bound to one conversation."""

    @tool(
        "plan_research",
        "Start a research plan: the researcher chooses a topic (or takes the one you "
        "give), and raises the questions it would research, with no lookup. The report "
        "is a journal entry. Only a member may call this.",
        _PLAN_SCHEMA,
    )
    async def plan_research(args: dict[str, Any]) -> dict[str, Any]:
        return await do_plan_research(deps, ctx, topic=args.get("topic"))

    @tool(
        "run_research",
        "Start a research run on one topic: the open questions of its dossier are "
        "looked up, challenged, and written as cited pages under knowledge/. Only a "
        "member may call this.",
        _RUN_SCHEMA,
    )
    async def run_research(args: dict[str, Any]) -> dict[str, Any]:
        return await do_run_research(
            deps,
            ctx,
            topic=args.get("topic"),
            questions=args.get("questions"),
            max_questions=args.get("max_questions"),
        )

    @tool(
        "queue_question",
        "Queue a factual question about a topic for the researcher. Use it when a "
        "person wants a question researched. Only a member may call this.",
        _QUEUE_SCHEMA,
    )
    async def queue_question(args: dict[str, Any]) -> dict[str, Any]:
        return await do_queue_question(
            deps, ctx, topic=args.get("topic") or "", question=args.get("question") or ""
        )

    @tool(
        "research_status",
        "Give the summary of the last research run. Only a member may call this.",
        {"type": "object", "properties": {}},
    )
    async def research_status(args: dict[str, Any]) -> dict[str, Any]:
        return await do_research_status(deps, ctx)

    return create_sdk_mcp_server(
        name="research",
        version="1.0.0",
        tools=[plan_research, run_research, queue_question, research_status],
    )


def register(
    manager: Any,
    *,
    settings: JoshuaConfig,
    data_dir: Path | str,
    lock: asyncio.Lock,
    jobs: JobRunner,
    clock: Clock | None = None,
) -> bool:
    """Wire the ``research`` builtin into the conversation manager when
    ``jobs.enabled`` and ``jobs.research.enabled`` are both true. Returns True
    when it did."""
    if not (settings.jobs.enabled and settings.jobs.research.enabled):
        return False
    ctx = ResearchContext(
        settings=settings,
        data_dir=Path(data_dir),
        lock=lock,
        jobs=jobs,
        clock=clock or utc_now,
    )
    manager.register_builtin("research", lambda deps: build_research_server(deps, ctx))
    return True
