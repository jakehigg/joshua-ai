"""In-process ``questions`` tool server: read the questions for a person, and answer them.

The researcher keeps a question that only a person can answer as a page under
``wiki/questions/``. Nothing sends one: a person reads them in chat when they
ask, and answers there. ``list_questions`` lists them, ``answer_question``
writes a person's answer onto a page, and ``dismiss_question`` closes one that
does not apply. A tool names a question by ``<section>/<slug>``, its path
under ``wiki/questions/`` without ``.md``.

Every tool gates on the current speaker being a member: a guest, or a speaker
core does not know, gets a "not available" reply and nothing changes.

An answer or a dismissal takes the lock that the jobs and the nightly
reflection share, writes the page, ``questions/README.md``, and the counts of
the dossier of the topic, and commits the wiki with the message
``research: question <id> answered by <person>`` or
``research: question <id> dismissed by <person>``.

Core registers this server only when ``jobs.enabled`` and
``jobs.research.enabled`` are both true.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from joshua_shared import layout, wikigit
from joshua_shared.config import JoshuaConfig
from joshua_shared.log import get_logger

from joshua_core.engine.agent import create_sdk_mcp_server, tool
from joshua_core.engine.tools import ToolDeps
from joshua_core.jobs.research.dossier import answers_on_disk, load_dossier, save_dossier
from joshua_core.jobs.research.job import local_day
from joshua_core.jobs.research.questions import (
    EVERYONE,
    MAX_ANSWER,
    STATUSES,
    QuestionFile,
    closed_order,
    counts,
    load_question,
    load_questions,
    open_order,
    set_answer,
    write_index,
)
from joshua_core.jobs.research.text import inline
from joshua_core.jobs.runner import Clock, utc_now

logger = get_logger("tools.questions")

NOT_AVAILABLE = "That is not available to you. Only a member can use the questions."
BUSY = "A job or the nightly reflection is writing the wiki now. Try again in a few minutes."
MAX_LIST = 50
LIST_STATUSES = ("all", *STATUSES)


@dataclass(frozen=True)
class QuestionsContext:
    """What the tools need beyond the conversation: the config, the data root,
    the wiki lock, and the clock."""

    settings: JoshuaConfig
    data_dir: Path
    lock: asyncio.Lock
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


def _limit(value: Any) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = 20
    return max(1, min(number, MAX_LIST))


def _line(item: QuestionFile) -> str:
    q = item.page
    line = (
        f"- `{item.id}`: {inline(q.question, 300)} Why: {inline(q.why, 300)} "
        f"(for {q.for_}, topic {q.topic}, raised {q.raised_at.isoformat()}"
    )
    if q.status != "open":
        line += f", {q.status} by {q.answered_by or 'nobody'}: {inline(q.answer or '', 300)}"
    return line + ")"


async def do_list_questions(
    deps: ToolDeps,
    ctx: QuestionsContext,
    *,
    status: str | None = "open",
    topic: str | None = None,
    for_person: str | None = None,
    limit: Any = 20,
) -> dict[str, Any]:
    if await _member_id(deps) is None:
        return _reply(NOT_AVAILABLE)
    wanted = (status or "open").strip().lower()
    if wanted not in LIST_STATUSES:
        return _reply(f"The status must be one of {', '.join(LIST_STATUSES)}.")
    items = load_questions(ctx.data_dir, (topic or "").strip() or None)
    who = (for_person or "").strip().lower()
    if who:
        items = [i for i in items if i.page.for_ in (who, EVERYONE)]
    ordered = open_order(items) + closed_order(items)
    if wanted != "all":
        ordered = [i for i in ordered if i.page.status == wanted]
    ordered = ordered[: _limit(limit)]
    if not ordered:
        return _reply(f"There are no {wanted} questions.")
    return _reply("Questions:\n" + "\n".join(_line(i) for i in ordered))


def _clean_id(value: str) -> str:
    out = (value or "").strip()
    if out.startswith(f"{layout.QUESTIONS}/"):
        out = out[len(layout.QUESTIONS) + 1 :]
    return out[:-3] if out.endswith(".md") else out


async def _close(
    deps: ToolDeps,
    ctx: QuestionsContext,
    *,
    question_id: str,
    status: str,
    text: str,
) -> dict[str, Any]:
    member = await _member_id(deps)
    if member is None:
        return _reply(NOT_AVAILABLE)
    question_id = _clean_id(question_id)
    if load_question(question_id, ctx.data_dir) is None:
        return _reply(f"I found no question `{question_id}`. Give it as <section>/<slug>.")
    if ctx.lock.locked():
        return _reply(BUSY)
    async with ctx.lock:
        current = load_question(question_id, ctx.data_dir)
        if current is None:
            return _reply(f"I found no question `{question_id}`.")
        if current.page.status != "open":
            return _reply(
                f"Question `{question_id}` is {current.page.status}, so I did not change it."
            )
        day = local_day(ctx.clock(), ctx.settings)
        done = set_answer(current, ctx.data_dir, status=status, answer=text, by=member, day=day)
        written = [done.path, write_index(ctx.data_dir)]
        dossier = load_dossier(done.page.topic, ctx.data_dir)
        if dossier is not None:
            dossier.person_open, dossier.person_answered = counts(ctx.data_dir, dossier.topic)
            answers = answers_on_disk(dossier, ctx.data_dir)
            written.append(save_dossier(dossier, ctx.data_dir, answers))
        if wikigit.is_enabled(ctx.settings):
            wikigit.commit(
                layout.wiki_root(ctx.data_dir),
                written,
                f"research: question {question_id} {status} by {member}",
            )
    logger.info({"message": "question closed", "status": status, "person": member})
    return _reply(f"Question `{question_id}` is {status} now.")


def _text(value: str | None) -> str:
    return (value or "").replace("\r\n", "\n").strip()


async def do_answer_question(
    deps: ToolDeps, ctx: QuestionsContext, *, question_id: str, answer: str
) -> dict[str, Any]:
    if await _member_id(deps) is None:
        return _reply(NOT_AVAILABLE)
    text = _text(answer)
    if not text:
        return _reply("Give the answer, in the words of the person who knows it.")
    if len(text) > MAX_ANSWER:
        return _reply(f"The answer has {len(text)} characters. Keep it to {MAX_ANSWER}.")
    return await _close(deps, ctx, question_id=question_id, status="answered", text=text)


async def do_dismiss_question(
    deps: ToolDeps, ctx: QuestionsContext, *, question_id: str, why: str
) -> dict[str, Any]:
    if await _member_id(deps) is None:
        return _reply(NOT_AVAILABLE)
    reason = " ".join(_text(why).split())
    if not reason:
        return _reply("Tell me why, so the researcher does not ask it again.")
    return await _close(
        deps, ctx, question_id=question_id, status="dismissed", text=reason[:MAX_ANSWER]
    )


# --- server ----------------------------------------------------------------

_LIST_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "description": "open (the default), answered, dismissed, or all.",
        },
        "topic": {"type": "string", "description": "Only the questions of this topic page."},
        "for_person": {
            "type": "string",
            "description": "A person id: only the questions for that person or for everyone.",
        },
        "limit": {"type": "integer", "description": "How many questions, 1 to 50. Default 20."},
    },
}

_ANSWER_SCHEMA = {
    "type": "object",
    "properties": {
        "slug": {"type": "string", "description": "The question, as <section>/<slug>."},
        "answer": {"type": "string", "description": "The answer that the person gave."},
    },
    "required": ["slug", "answer"],
}

_DISMISS_SCHEMA = {
    "type": "object",
    "properties": {
        "slug": {"type": "string", "description": "The question, as <section>/<slug>."},
        "why": {"type": "string", "description": "Why the question does not apply."},
    },
    "required": ["slug", "why"],
}


def build_questions_server(deps: ToolDeps, ctx: QuestionsContext) -> Any:
    """Build the in-process ``questions`` MCP server bound to one conversation."""

    @tool(
        "list_questions",
        "List the questions that the researcher keeps for a person: what only somebody "
        "here can answer. The open ones are first, the oldest first. Use it when a person "
        "asks if you need anything from them. Only a member may call this.",
        _LIST_SCHEMA,
    )
    async def list_questions(args: dict[str, Any]) -> dict[str, Any]:
        return await do_list_questions(
            deps,
            ctx,
            status=args.get("status") or "open",
            topic=args.get("topic"),
            for_person=args.get("for_person"),
            limit=args.get("limit") or 20,
        )

    @tool(
        "answer_question",
        "Write the answer that a person gave onto one open question. The researcher reads "
        "it on its next run. Only a member may call this.",
        _ANSWER_SCHEMA,
    )
    async def answer_question(args: dict[str, Any]) -> dict[str, Any]:
        return await do_answer_question(
            deps, ctx, question_id=args.get("slug") or "", answer=args.get("answer") or ""
        )

    @tool(
        "dismiss_question",
        "Close one open question that does not apply, with the reason. The researcher does "
        "not ask it again. Only a member may call this.",
        _DISMISS_SCHEMA,
    )
    async def dismiss_question(args: dict[str, Any]) -> dict[str, Any]:
        return await do_dismiss_question(
            deps, ctx, question_id=args.get("slug") or "", why=args.get("why") or ""
        )

    return create_sdk_mcp_server(
        name="questions",
        version="1.0.0",
        tools=[list_questions, answer_question, dismiss_question],
    )


def register(
    manager: Any,
    *,
    settings: JoshuaConfig,
    data_dir: Path | str,
    lock: asyncio.Lock,
    clock: Clock | None = None,
) -> bool:
    """Wire the ``questions`` builtin into the conversation manager when
    ``jobs.enabled`` and ``jobs.research.enabled`` are both true. Returns True
    when it did."""
    if not (settings.jobs.enabled and settings.jobs.research.enabled):
        return False
    ctx = QuestionsContext(
        settings=settings, data_dir=Path(data_dir), lock=lock, clock=clock or utc_now
    )
    manager.register_builtin("questions", lambda deps: build_questions_server(deps, ctx))
    return True
