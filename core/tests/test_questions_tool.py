"""The in-process ``questions`` tool server: list, answer, and dismiss the
questions for a person. A guest gets nothing from any tool, and the answer of
a person reaches the next bundle of the topic."""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from jobs_fakes import fixed_clock
from joshua_core.engine.tools import ToolDeps
from joshua_core.engine.tools.questions import (
    BUSY,
    NOT_AVAILABLE,
    QuestionsContext,
    build_questions_server,
    do_answer_question,
    do_dismiss_question,
    do_list_questions,
    register,
)
from joshua_core.jobs.bundle import build_bundle
from joshua_core.jobs.research.dossier import load_dossier, new_dossier, save_dossier
from joshua_core.jobs.research.questions import QuestionPage, load_question, write_question
from joshua_core.jobs.research.questions import write_index as write_questions_index
from joshua_core.jobs.topics import load_topic
from joshua_core.memory import embed as embed_module
from joshua_core.store.models import Channel, Conversation, Person
from joshua_shared import frontmatter, layout, wikigit
from research_fakes import (
    POTHOS,
    SNAKE,
    FakeStore,
    build_research_wiki,
    research_settings,
    snapshot,
    word_vector,
)

OLD = "houseplants/product-dose"
NEW = "houseplants/which-pot"
ALEX = "houseplants/alex-feed"


@pytest.fixture(autouse=True)
def fake_embed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(embed_module, "embed", lambda text, model="": word_vector(text))


class People:
    def __init__(self) -> None:
        self.people = {
            "alex": Person(id="alex", display_name="Alex", role="member"),
            "gus": Person(id="gus", display_name="Gus", role="guest"),
        }

    async def get_person(self, pid: str) -> Any:
        return self.people.get(pid)


def _page(question: str, raised: date, topic: str = SNAKE, **extra: Any) -> QuestionPage:
    return QuestionPage.model_validate(
        {
            "question": question,
            "topic": topic,
            "section": "houseplants",
            "why": "The answer sets the next feed.",
            "context": [topic],
            "grounded_in": ["gritty soil mix", "water to runoff"],
            "route_reason": "names a record date",
            "raised_at": raised,
            **extra,
        }
    )


class World:
    def __init__(self, data: Path, git: bool = False) -> None:
        self.data = data
        self.wiki = build_research_wiki(data)
        self.settings = research_settings("" if git else "wiki:\n  git: false\n")
        self.lock = asyncio.Lock()
        self.ctx = QuestionsContext(
            settings=self.settings, data_dir=data, lock=self.lock, clock=fixed_clock()
        )
        self.repo = People()
        write_question(
            NEW, _page("Which pot does the snake plant sit in now?", date(2026, 10, 9)), data
        )
        write_question(
            OLD, _page("What dose went on the snake plant on 2026-10-04?", date(2026, 9, 1)), data
        )
        write_question(
            ALEX,
            _page("Which feed did Alex give the snake plant?", date(2026, 9, 5), **{"for": "alex"}),
            data,
        )
        write_question(
            "houseplants/pothos-light",
            _page("How much light does the pothos get?", date(2026, 9, 2), topic=POTHOS),
            data,
        )
        write_questions_index(data)
        save_dossier(new_dossier(SNAKE, "Snake plant"), data, {})

    def deps(self, person: str | None = "alex") -> ToolDeps:
        conv = Conversation(id="c1", channel_id="telegram:1", person_id=person)
        channel = Channel(id="telegram:1", channel_type="telegram")
        return ToolDeps(
            repo=self.repo,  # type: ignore[arg-type]
            conversation=conv,
            channel=channel,
            tz="UTC",
            person_id=person,
        )


def _text(reply: dict) -> str:
    return reply["content"][0]["text"]


@pytest.fixture
def w(tmp_path: Path) -> World:
    return World(tmp_path / "data")


async def test_list_questions_gives_the_open_ones_oldest_first(w: World) -> None:
    text = _text(await do_list_questions(w.deps(), w.ctx))
    lines = text.splitlines()[1:]
    assert [line.split("`")[1] for line in lines] == [
        OLD,
        "houseplants/pothos-light",
        ALEX,
        NEW,
    ]
    assert "Why: The answer sets the next feed." in lines[0]
    assert f"for everyone, topic {SNAKE}" in lines[0]
    assert "for alex" in lines[2]


async def test_list_questions_filters(w: World) -> None:
    by_topic = _text(await do_list_questions(w.deps(), w.ctx, topic=POTHOS))
    assert "pothos-light" in by_topic and OLD not in by_topic
    for_sam = _text(await do_list_questions(w.deps(), w.ctx, for_person="sam"))
    assert ALEX not in for_sam and OLD in for_sam
    for_alex = _text(await do_list_questions(w.deps(), w.ctx, for_person="Alex"))
    assert ALEX in for_alex and OLD in for_alex
    one = _text(await do_list_questions(w.deps(), w.ctx, limit=1))
    assert len(one.splitlines()) == 2
    assert "no answered" in _text(await do_list_questions(w.deps(), w.ctx, status="answered"))
    assert "must be one of" in _text(await do_list_questions(w.deps(), w.ctx, status="bogus"))
    assert OLD in _text(await do_list_questions(w.deps(), w.ctx, status="all", limit="x"))


async def test_answer_question(w: World) -> None:
    reply = _text(
        await do_answer_question(
            w.deps(),
            w.ctx,
            question_id=f"questions/{OLD}.md",
            answer="Half strength of a 10-10-10 liquid feed. "
            "See [the page](houseplants/snake-plant.md) and [a shop](https://example.com/feed).",
        )
    )
    assert reply == f"Question `{OLD}` is answered now."
    found = load_question(OLD, w.data)
    assert found is not None
    page = found.page
    assert page.status == "answered" and page.answered_by == "alex"
    assert page.answered_at == date(2026, 10, 19)
    assert page.answer is not None and page.answer.startswith("Half strength")
    _, body = frontmatter.split(found.text)
    answer = body.split("## Answer\n\n", 1)[1]
    assert "[the page](../../houseplants/snake-plant.md)" in answer
    assert "example.com" not in answer and "a shop" in answer
    index = layout.questions_index(w.data).read_text()
    assert index.index("## Answered and dismissed") < index.index("What dose went on")
    dossier = load_dossier(SNAKE, w.data)
    assert dossier is not None
    assert (dossier.person_open, dossier.person_answered) == (2, 1)
    assert "2 open, 1 answered" in layout.knowledge_dossier(SNAKE, w.data).read_text()
    again = _text(await do_answer_question(w.deps(), w.ctx, question_id=OLD, answer="x"))
    assert "is answered, so I did not change it" in again


async def test_an_answer_must_have_text_and_fit(w: World) -> None:
    assert "Give the answer" in _text(
        await do_answer_question(w.deps(), w.ctx, question_id=OLD, answer="  ")
    )
    long = _text(await do_answer_question(w.deps(), w.ctx, question_id=OLD, answer="x" * 4001))
    assert "Keep it to 4000" in long
    assert load_question(OLD, w.data).page.status == "open"  # type: ignore[union-attr]


async def test_dismiss_question(w: World) -> None:
    assert "Tell me why" in _text(
        await do_dismiss_question(w.deps(), w.ctx, question_id=OLD, why="")
    )
    reply = _text(
        await do_dismiss_question(w.deps(), w.ctx, question_id=OLD, why="We never track it.")
    )
    assert reply == f"Question `{OLD}` is dismissed now."
    found = load_question(OLD, w.data)
    assert found is not None and found.page.status == "dismissed"
    assert found.page.answer == "We never track it." and found.page.answered_by == "alex"
    assert "Dismissed: We never track it." in found.text
    assert OLD not in _text(await do_list_questions(w.deps(), w.ctx))
    dossier = load_dossier(SNAKE, w.data)
    assert dossier is not None and (dossier.person_open, dossier.person_answered) == (2, 0)


async def test_a_question_with_no_dossier_is_still_answered(w: World) -> None:
    reply = _text(
        await do_answer_question(
            w.deps(), w.ctx, question_id="houseplants/pothos-light", answer="Bright shade."
        )
    )
    assert "answered now" in reply
    assert load_dossier(POTHOS, w.data) is None


async def test_an_unknown_or_bad_slug(w: World) -> None:
    for bad in ("houseplants/nope", "../../people/alex", "nope", ""):
        assert "I found no question" in _text(
            await do_answer_question(w.deps(), w.ctx, question_id=bad, answer="x")
        )


async def test_an_answer_waits_for_the_lock(w: World) -> None:
    async with w.lock:
        assert _text(await do_answer_question(w.deps(), w.ctx, question_id=OLD, answer="x")) == BUSY
    assert load_question(OLD, w.data).page.status == "open"  # type: ignore[union-attr]


@pytest.mark.parametrize("person", ["gus", None, "stranger"])
async def test_a_guest_is_refused_by_each_tool(w: World, person: str | None) -> None:
    before = snapshot(w.wiki)
    deps = w.deps(person)
    for reply in (
        await do_list_questions(deps, w.ctx),
        await do_answer_question(deps, w.ctx, question_id=OLD, answer="Half strength."),
        await do_dismiss_question(deps, w.ctx, question_id=OLD, why="no"),
    ):
        assert _text(reply) == NOT_AVAILABLE
    assert snapshot(w.wiki) == before


async def test_the_answer_reaches_the_next_bundle(w: World) -> None:
    await do_answer_question(w.deps(), w.ctx, question_id=OLD, answer="Half strength 10-10-10.")
    bundle = await build_bundle(
        load_topic(SNAKE, w.wiki),
        wiki_root=w.wiki,
        repo=FakeStore(),
        settings=w.settings,
        now=fixed_clock()(),
    )
    assert f"questions/{OLD}.md" in bundle.paths
    assert f"questions/{NEW}.md" not in bundle.paths
    assert "Answer: Half strength 10-10-10." in bundle.text
    assert "Which pot does the snake plant" not in bundle.text


def _last_commit(wiki: Path) -> str:
    return subprocess.run(
        ["git", "show", "--name-only", "--format=%s", "HEAD"],
        cwd=wiki,
        capture_output=True,
        text=True,
    ).stdout


@pytest.mark.skipif(shutil.which("git") is None, reason="needs the git binary")
async def test_an_answer_commits_the_wiki(tmp_path: Path) -> None:
    w = World(tmp_path / "data", git=True)
    wikigit.ensure_repo(w.wiki)
    wikigit.commit(w.wiki, None, "start")
    await do_answer_question(w.deps(), w.ctx, question_id=OLD, answer="Half strength.")
    out = _last_commit(w.wiki)
    assert out.startswith(f"research: question {OLD} answered by alex")
    assert f"questions/{OLD}.md" in out and "questions/README.md" in out
    assert "knowledge/houseplants/snake-plant.md" in out
    await do_dismiss_question(w.deps(), w.ctx, question_id=NEW, why="Not needed.")
    assert _last_commit(w.wiki).startswith(f"research: question {NEW} dismissed by alex")


def test_the_server_and_register() -> None:
    settings = research_settings()

    class Manager:
        def __init__(self) -> None:
            self.names: list[str] = []

        def register_builtin(self, name: str, factory: Any) -> None:
            self.names.append(name)

    lock = asyncio.Lock()
    manager = Manager()
    assert register(manager, settings=settings, data_dir="/x", lock=lock)
    assert manager.names == ["questions"]
    off = research_settings()
    off.jobs.research.enabled = False
    assert not register(Manager(), settings=off, data_dir="/x", lock=lock)
    jobs_off = research_settings()
    jobs_off.jobs.enabled = False
    assert not register(Manager(), settings=jobs_off, data_dir="/x", lock=lock)
    ctx = QuestionsContext(settings=settings, data_dir=Path("/x"), lock=lock)
    deps = ToolDeps(
        repo=People(),  # type: ignore[arg-type]
        conversation=Conversation(id="c", channel_id="t:1", person_id="alex"),
        channel=Channel(id="t:1", channel_type="telegram"),
        tz="UTC",
        person_id="alex",
    )
    assert build_questions_server(deps, ctx) is not None


async def test_the_server_tools_call_through(w: World, monkeypatch: pytest.MonkeyPatch) -> None:
    from joshua_core.engine.tools import questions as questions_module

    monkeypatch.setattr(questions_module, "create_sdk_mcp_server", lambda **kw: kw)
    server = build_questions_server(w.deps(), w.ctx)
    handlers = {t.name: t.handler for t in server["tools"]}
    assert set(handlers) == {"list_questions", "answer_question", "dismiss_question"}
    listed = await handlers["list_questions"]({})
    assert OLD in _text(listed)
    answered = await handlers["answer_question"]({"slug": OLD, "answer": "Half strength."})
    assert "answered now" in _text(answered)
    dismissed = await handlers["dismiss_question"]({"slug": NEW, "why": "Not needed."})
    assert "dismissed now" in _text(dismissed)
