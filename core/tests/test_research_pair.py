"""The two parts of a web question in the `research` job: the general question
goes to the web, the local context stays inside the instance, a page of
another topic can answer a question, and the crew looks at the knowledge we
hold first. No model, no network, no database."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest
from jobs_fakes import CONFIG, fixed_clock
from joshua_core.jobs.research.dossier import (
    Question,
    load_dossier,
    new_dossier,
    parse_dossier,
    save_dossier,
)
from joshua_core.jobs.research.job import ResearchJob
from joshua_core.memory import embed as embed_module
from joshua_shared import config as config_module
from joshua_shared import frontmatter, layout
from research_fakes import (
    PRIMARY,
    RESEARCH_ON,
    SECONDARY,
    SNAKE,
    WIKI,
    FakeInternet,
    FakeStore,
    FakeWorkers,
    build_research_wiki,
)

ROSTER = CONFIG + "  - id: bo\n    name: Bo\n"
CALDO = "recipes/alex-s-caldo-verde.md"
CALDO_PAGE = (
    "# Alex's Caldo Verde\n\n"
    "Lacinato kale, Yukon Gold potatoes, and chorizo. The base is chorizo fat.\n"
    "The kale comes from the grow tent and comes out pale.\n"
)
PHRASES = ["Lacinato kale", "chorizo"]
# The five web questions of research run 12, each in its general form, with
# its local form beside it.
RUN12 = [
    (
        "How should lacinato kale be blanched to keep its colour for a soup like Caldo Verde?",
        "Alex's Caldo Verde uses lacinato kale that goes dull in the pot.",
    ),
    (
        "How long is a potato, kale, and chorizo soup such as Caldo Verde safe in the fridge?",
        "Alex's Caldo Verde is made on Sunday and eaten through the week.",
    ),
    (
        "Is chorizo fat or olive oil the better base for a Caldo Verde?",
        "Alex's Caldo Verde starts in chorizo fat, and Bo prefers olive oil.",
    ),
    (
        "How fine should lacinato kale be cut for Caldo Verde to cook in five minutes?",
        "Alex's Caldo Verde gets the kale cut in thick ribbons at the end.",
    ),
    (
        "Which potato variety gives a caldo verde a smooth, thick broth?",
        "Alex's Caldo Verde uses Yukon Gold potatoes, mashed in the pot.",
    ),
]
NAMED = "Which chorizo suits Bo for a Caldo Verde on a weeknight?"
LOCAL = "Our snake plant lives in the hall in a gritty soil mix, watered to runoff."
GENERAL = "How often should a snake plant in a gritty soil mix be watered in winter?"
OTHER = "knowledge/houseplants/zz-plant/winter-watering.md"


def _vector(text: str) -> list[float]:
    """One dimension for each text, so that only the same text is near."""
    vec = [0.0] * 1024
    vec[int(hashlib.sha256(text.encode()).hexdigest(), 16) % 1024] = 1.0
    return vec


@pytest.fixture(autouse=True)
def fake_embed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(embed_module, "embed", lambda text, model="": _vector(text))


def web(general: str, local: str, phrases: list[str], **extra: Any) -> dict[str, Any]:
    return {
        "general_question": general,
        "local_context": local,
        "grounded_in": phrases,
        "why": "It matters.",
        "audience": "web",
        "about_person": False,
        **extra,
    }


class Harness:
    def __init__(
        self,
        data: Path,
        *,
        raise_answer: dict[str, Any] | None = None,
        near: dict[str, float] | None = None,
        apply_answer: Any = None,
    ) -> None:
        self.data = data
        self.wiki = build_research_wiki(
            data, {**WIKI, "recipes.md": "# Recipes\n", CALDO: CALDO_PAGE}
        )
        self.settings = config_module.parse(ROSTER + RESEARCH_ON, env={}, source="<test>")
        self.workers = FakeWorkers(
            raise_answer=raise_answer or {"questions": []}, apply_answer=apply_answer
        )
        self.internet = FakeInternet()
        self.store = FakeStore(near)

    async def run(self, **args: Any):
        job = ResearchJob(
            store=self.store,
            settings=self.settings,
            data_dir=self.data,
            runner=self.workers,
            internet_runner=self.internet,
        )
        return await job.run_once(args=job.check_args(args), dry_run=False, clock=fixed_clock())

    def other_page(self, *, topic: str = "houseplants/zz-plant.md", status: str = "published"):
        path = self.wiki / OTHER
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            frontmatter.update(
                "# Winter watering\n\n## Answer\n\nEvery 4 to 6 weeks.\n",
                question="How often should a plant in a gritty mix be watered in winter?",
                answer_short="Every 4 to 6 weeks in winter.",
                topic=topic,
                confidence="high",
                status=status,
                sources=[
                    {"url": PRIMARY, "title": "Extension", "trust": "primary"},
                    {"url": SECONDARY, "title": "Guide", "trust": "secondary"},
                ],
            )
        )

    def queue(self, text: str, local: str = LOCAL, asked_by: str = "research") -> str:
        dossier = load_dossier(SNAKE, self.data) or new_dossier(SNAKE, "Snake plant")
        dossier.questions.append(
            Question(
                slug="winter",
                question=text,
                local_context=local,
                grounded_in=["gritty soil mix", "water to runoff"],
                asked_by=asked_by,
            )
        )
        save_dossier(dossier, self.data, {})
        return "winter"


def _section(report: Any, heading: str) -> str:
    return dict(report.sections)[heading]


# -- the gate on the pair ---------------------------------------------------------


async def test_the_run_12_questions_in_general_form_pass(tmp_path: Path) -> None:
    answer = {"questions": [web(g, local, PHRASES) for g, local in RUN12]}
    answer["questions"].append(web(NAMED, "Bo buys the chorizo for the soup each week.", PHRASES))
    h = Harness(tmp_path / "data", raise_answer=answer)
    report = await h.run(mode="plan", topic=CALDO)
    dossier = load_dossier(CALDO, h.data)
    assert dossier is not None
    assert [q.question for q in dossier.questions] == [g for g, _ in RUN12]
    assert [q.local_context for q in dossier.questions] == [local for _, local in RUN12]
    assert all("alex" not in q.question.casefold() for q in dossier.questions)
    assert all(q.subject.casefold() == "caldo verde" for q in dossier.questions)
    # A general question that holds a roster name is rejected, not routed.
    assert [r["question"] for r in report.data["rejected"]] == [NAMED]
    assert report.data["rejected"][0]["reason"].startswith("names a person: ")
    assert report.data["person"] == []
    questions = _section(report, "Questions")
    assert f"general: `{RUN12[0][0]}`; local: `{RUN12[0][1]}`" in questions


async def test_a_general_question_that_a_page_of_another_topic_answers_is_shared(
    tmp_path: Path,
) -> None:
    h = Harness(
        tmp_path / "data",
        raise_answer={"questions": [web(GENERAL, LOCAL, ["gritty soil mix", "water to runoff"])]},
        near={f"wiki/{OTHER}": 0.9},
    )
    h.other_page()
    report = await h.run(mode="plan", topic=SNAKE)
    dossier = load_dossier(SNAKE, h.data)
    assert dossier is not None
    [question] = dossier.questions
    assert (question.status, question.shared) == ("answered", True)
    assert question.page == "houseplants/zz-plant/winter-watering.md"
    assert dossier.coverage == 1.0 and report.data["shared"] == [question.slug]
    readme = layout.knowledge_index(h.data).read_text()
    assert "| Topic | Coverage | Answered | Shared | Last researched | Open |" in readme
    assert "| 1.00 | 1/1 | 1 | never | 0 |" in readme
    body = layout.knowledge_dossier(SNAKE, h.data).read_text()
    assert "(zz-plant/winter-watering.md) (shared with another topic): Every 4 to 6" in body
    line = _section(report, "Questions")
    assert "(answered, new, shared)" in line and f"answered by `{OTHER}`" in line
    # A shared question is not researched again.
    run = await h.run(mode="run", topic=SNAKE)
    assert h.internet.texts == [] and run.data["pages"] == []


async def test_a_page_of_the_same_topic_is_a_duplicate_not_a_shared_answer(
    tmp_path: Path,
) -> None:
    h = Harness(
        tmp_path / "data",
        raise_answer={"questions": [web(GENERAL, LOCAL, ["gritty soil mix", "water to runoff"])]},
        near={f"wiki/{OTHER}": 0.9},
    )
    h.other_page(topic=SNAKE)
    report = await h.run(mode="plan", topic=SNAKE)
    assert report.data["shared"] == []
    assert report.data["rejected"][0]["answered_by"] == OTHER


# -- the crew gets the pair split -------------------------------------------------


async def test_the_lookups_never_get_the_local_context(tmp_path: Path) -> None:
    h = Harness(tmp_path / "data")
    slug = h.queue(GENERAL)
    report = await h.run(mode="run", topic=SNAKE, questions=[slug])
    assert len(h.internet.texts) == 2
    assert all(LOCAL not in text and "hall" not in text for text in h.internet.texts)
    assert all(LOCAL not in str(k) for k in h.internet.kwargs)
    editor_text = h.workers.calls[-1][1]
    assert f"<local_context>\n{LOCAL}\n" in editor_text
    page = h.wiki / f"knowledge/houseplants/snake-plant/{slug}.md"
    meta, body = frontmatter.split(page.read_text())
    assert meta["local_context"] == LOCAL
    assert "## Where this differs for us" in body
    assert f"- `{slug}` (full crew): published" in _section(report, "Researched")


async def test_a_queued_question_that_names_a_person_is_held_not_sent(tmp_path: Path) -> None:
    h = Harness(tmp_path / "data")
    slug = h.queue("Should Alex water the snake plant less in winter?", local="", asked_by="alex")
    report = await h.run(mode="run", topic=SNAKE, questions=[slug])
    assert h.internet.texts == [] and h.workers.calls == []
    dossier = load_dossier(SNAKE, h.data)
    assert dossier is not None
    question = dossier.find(slug)
    assert question is not None and question.status == "held"
    assert question.hold_reason.startswith("not sent: the question names a person")
    assert f"- `{slug}` (not sent): held (1): not sent:" in _section(report, "Researched")
    assert report.data["not_sent"] == [slug]


# -- knowledge first --------------------------------------------------------------


def _apply(verdict: str, narrowed: str = "") -> dict[str, Any]:
    return {
        "verdict": verdict,
        "answer_short": "Every 4 to 6 weeks in winter.",
        "answer": "Water every 4 to 6 weeks from October to March.",
        "local_notes": "The hall is cool, so the longer end fits.",
        "cites": [OTHER],
        "narrowed_question": narrowed,
    }


async def test_a_question_that_we_already_know_is_applied_with_no_lookup(tmp_path: Path) -> None:
    h = Harness(tmp_path / "data", near={f"wiki/{OTHER}": 0.75}, apply_answer=_apply("answered"))
    h.other_page()
    slug = h.queue(GENERAL)
    report = await h.run(mode="run", topic=SNAKE, questions=[slug])
    assert h.internet.texts == []
    assert h.workers.names() == ["apply"]
    page = h.wiki / f"knowledge/houseplants/snake-plant/{slug}.md"
    meta, body = frontmatter.split(page.read_text())
    assert meta["applies"] == [OTHER] and meta["confidence"] == "high"
    assert [s["url"] for s in meta["sources"]] == [PRIMARY, SECONDARY]
    assert "Applied from: [knowledge/houseplants/zz-plant/winter-watering.md]" in body
    assert f"- `{slug}` (applied, no lookup): published" in _section(report, "Researched")
    assert report.data["applied"] == [slug]
    assert report.data["usage_by_question"][slug]["apply"] > 0
    assert f"- `{slug}`: apply 88 tokens, researcher 0 tokens" in _section(report, "Usage")


async def test_a_partial_answer_researches_the_narrowed_question(tmp_path: Path) -> None:
    narrowed = "Does a snake plant in a gritty soil mix need feed from October to March?"
    h = Harness(
        tmp_path / "data", near={f"wiki/{OTHER}": 0.75}, apply_answer=_apply("partial", narrowed)
    )
    h.other_page()
    slug = h.queue(GENERAL)
    report = await h.run(mode="run", topic=SNAKE, questions=[slug])
    assert [t.startswith(narrowed) or narrowed in t for t in h.internet.texts] == [True, True]
    meta, _ = frontmatter.split(
        (h.wiki / f"knowledge/houseplants/snake-plant/{slug}.md").read_text()
    )
    assert meta["applies"] == [OTHER] and meta["question"] == GENERAL
    assert f"- `{slug}` (narrowed): published" in _section(report, "Researched")
    assert report.data["narrowed"] == [slug]


async def test_no_published_page_runs_the_full_crew(tmp_path: Path) -> None:
    h = Harness(tmp_path / "data", near={f"wiki/{OTHER}": 0.75}, apply_answer=_apply("answered"))
    h.other_page(status="held")
    slug = h.queue(GENERAL)
    report = await h.run(mode="run", topic=SNAKE, questions=[slug])
    assert h.workers.names() == ["editor"] and len(h.internet.texts) == 2
    meta, _ = frontmatter.split(
        (h.wiki / f"knowledge/houseplants/snake-plant/{slug}.md").read_text()
    )
    assert "applies" not in meta
    assert f"- `{slug}` (full crew): published" in _section(report, "Researched")


# -- the dossier ------------------------------------------------------------------


def test_an_old_dossier_loads_with_an_empty_local_context() -> None:
    text = (
        "---\ntopic: houseplants/snake-plant.md\nquestions:\n"
        "  - slug: winter\n    question: How often in winter for the snake plant?\n"
        "    status: open\n---\nBody.\n"
    )
    dossier = parse_dossier(text)
    assert dossier is not None
    [question] = dossier.questions
    assert (question.local_context, question.subject, question.shared) == ("", "", False)
