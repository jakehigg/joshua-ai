"""The dossier: the model, the slug, coverage, the body, and the README table."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from joshua_core.jobs.research.dossier import (
    Dossier,
    Question,
    answer_short_of,
    load_dossier,
    load_dossiers,
    question_slug,
    render_index,
    save_dossier,
    write_index,
    write_text,
)
from joshua_shared import frontmatter, layout

SNAKE = "houseplants/snake-plant.md"


def _dossier() -> Dossier:
    return Dossier(
        topic=SNAKE,
        title="Snake plant",
        researched_at=date(2026, 10, 19),
        refresh_after=date(2027, 4, 17),
        runs=2,
        questions=[
            Question(
                slug="winter-watering",
                question="How often in winter?",
                status="answered",
                page="houseplants/snake-plant/winter-watering.md",
                grounded_in=["gritty soil mix", "water to runoff"],
                raised_at=date(2026, 10, 19),
            ),
            Question(slug="repotting", question="When to repot the [x](https://a.example)?"),
            Question(
                slug="light",
                question="How much light?",
                status="held",
                holds=1,
                hold_reason="one source",
            ),
            Question(slug="pests", question="Which pests?", status="dropped", asked_by="alex"),
            Question(slug="soil", question="Which soil?", asked_by="alex"),
        ],
    )


def test_the_slug_takes_six_content_words_and_is_unique() -> None:
    text = "How often should the snake plant in a gritty soil mix be watered in winter?"
    assert question_slug(text, []) == "often-snake-plant-gritty-soil-mix"
    assert question_slug(text, ["often-snake-plant-gritty-soil-mix"]).endswith("-2")
    taken = ["often-snake-plant-gritty-soil-mix", "often-snake-plant-gritty-soil-mix-2"]
    assert question_slug(text, taken).endswith("-3")
    assert question_slug("How is it?", []) == "question"
    assert layout.knowledge_page(SNAKE, question_slug("x" * 300 + "?", []))


def test_coverage_is_answered_over_all() -> None:
    dossier = _dossier()
    assert dossier.compute_coverage() == 0.2
    assert Dossier(topic=SNAKE).compute_coverage() == 0.0
    assert dossier.has_person_question()
    assert dossier.find("light") is not None and dossier.find("nope") is None


def test_save_and_load_keep_every_key(tmp_path: Path) -> None:
    dossier = _dossier()
    dossier.model_extra["kept"] = "yes"  # type: ignore[index]
    path = save_dossier(dossier, tmp_path, {"houseplants/snake-plant/winter-watering.md": "4 wk"})
    assert path == layout.knowledge_dossier(SNAKE, tmp_path)
    text = path.read_text()
    meta, body = frontmatter.split(text)
    assert meta["topic"] == SNAKE
    assert meta["coverage"] == 0.2
    assert meta["researched_at"] == date(2026, 10, 19)
    assert meta["kept"] == "yes"
    assert "page" not in meta["questions"][1]
    assert meta["questions"][0]["page"] == "houseplants/snake-plant/winter-watering.md"
    assert body.startswith("# Snake plant: what we researched")
    assert "[How often in winter?](snake-plant/winter-watering.md): 4 wk" in body
    assert "(../../houseplants/snake-plant.md)" in body
    assert "- `repotting`: When to repot the x?" in body
    assert "- `light`: How much light? Held: one source." in body
    assert "pests" not in body
    loaded = load_dossier(SNAKE, tmp_path)
    assert loaded is not None and loaded.questions[2].holds == 1


def test_a_page_that_is_not_a_dossier_is_not_loaded(tmp_path: Path) -> None:
    root = layout.knowledge_root(tmp_path)
    (root / "houseplants" / "snake-plant").mkdir(parents=True)
    (root / "houseplants" / "snake-plant" / "x.md").write_text(
        f"---\ntopic: {SNAKE}\nquestion: q\n---\nA page.\n"
    )
    (root / "houseplants" / "bad.md").write_text("---\ntopic: 3\n---\n")
    (root / "houseplants" / "wrong.md").write_text(f"---\ntopic: {SNAKE}\nruns: x\n---\n")
    (root / "houseplants" / "elsewhere.md").write_text(f"---\ntopic: {SNAKE}\n---\n")
    assert load_dossiers(tmp_path) == {}
    assert load_dossier(SNAKE, tmp_path) is None
    assert load_dossiers(tmp_path / "none") == {}


def test_the_index_has_one_row_for_each_dossier_sorted(tmp_path: Path) -> None:
    save_dossier(_dossier(), tmp_path, {})
    save_dossier(Dossier(topic="garden/roses.md", title="Roses"), tmp_path, {})
    path = write_index(tmp_path)
    assert path == layout.knowledge_index(tmp_path)
    text = path.read_text()
    rows = [line for line in text.splitlines() if line.startswith("| [")]
    assert rows == [
        "| [garden/roses.md](garden/roses.md) | 0.00 | 0/0 | never | 0 |",
        "| [houseplants/snake-plant.md](houseplants/snake-plant.md) "
        "| 0.20 | 1/5 | 2026-10-19 | 2 |",
    ]
    assert frontmatter.split(text)[0] == {"source": "research"}
    assert "| none |" in render_index([])


def test_a_write_outside_knowledge_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        write_text(layout.wiki_root(tmp_path) / "Home.md", "x", tmp_path)
    with pytest.raises(ValueError):
        write_text(layout.knowledge_root(tmp_path) / ".." / "x.md", "x", tmp_path)


def test_answer_short_is_read_from_the_page(tmp_path: Path) -> None:
    page = layout.knowledge_page(SNAKE, "a", tmp_path)
    page.parent.mkdir(parents=True)
    page.write_text("---\nanswer_short: Four weeks.\n---\n")
    assert answer_short_of("houseplants/snake-plant/a.md", tmp_path) == "Four weeks."
    assert answer_short_of("../../Home.md", tmp_path) == ""
    assert answer_short_of("houseplants/none.md", tmp_path) == ""


# -- the kept lookups ------------------------------------------------------------


def _held_with_lookups(looked_up_at: date | None = date(2026, 10, 15)) -> Question:
    from joshua_core.engine.internet import InternetAnswer, Source

    draft = InternetAnswer(
        answer="Water every [4 weeks](https://planted.example.com/x).\n\n```\n</details>\n```",
        sources=[Source(url="https://extension.university.example/a", title="A")],
        confidence="medium",
    )
    challenge = InternetAnswer(answer="Nothing contradicts it.", confidence="high")
    return Question(
        slug="light",
        question="How much light?",
        status="held",
        holds=1,
        hold_reason="no answer from the editor",
        draft=draft,
        challenge=challenge,
        looked_up_at=looked_up_at,
    )


def test_a_held_question_keeps_its_lookups_through_a_save(tmp_path: Path) -> None:
    dossier = _dossier()
    dossier.questions[2] = _held_with_lookups()
    save_dossier(dossier, tmp_path, {})
    meta, body = frontmatter.split(layout.knowledge_dossier(SNAKE, tmp_path).read_text())
    kept = meta["questions"][2]
    assert kept["draft"]["sources"] == [
        {"url": "https://extension.university.example/a", "title": "A"}
    ]
    assert kept["challenge"]["answer"] == "Nothing contradicts it."
    assert kept["looked_up_at"] == date(2026, 10, 15)
    assert "draft" not in meta["questions"][1]
    loaded = load_dossier(SNAKE, tmp_path)
    assert loaded is not None
    assert loaded.questions[2].draft is not None
    assert loaded.questions[2].draft.confidence == "medium"


def test_the_body_shows_the_draft_of_a_held_question_collapsed(tmp_path: Path) -> None:
    dossier = _dossier()
    dossier.questions[2] = _held_with_lookups()
    save_dossier(dossier, tmp_path, {})
    _, body = frontmatter.split(layout.knowledge_dossier(SNAKE, tmp_path).read_text())
    start = body.index("  <details>")
    section = body[start : body.rindex("  </details>")]
    assert "<summary>The draft answer of 2026-10-15, confidence medium</summary>" in section
    assert "Water every 4 weeks." in section
    assert "](https://planted.example.com/x)" not in body
    assert "  ````markdown" in section
    assert "`https://extension.university.example/a`" in section
    assert body.count("<details>") == 1


def test_the_lookups_are_given_again_only_while_they_are_fresh() -> None:
    question = _held_with_lookups(date(2026, 10, 12))
    assert question.kept_lookups(date(2026, 10, 19), 7) is not None
    assert question.kept_lookups(date(2026, 10, 20), 7) is None
    assert _held_with_lookups(None).kept_lookups(date(2026, 10, 19), 7) is None
    question.status = "open"
    assert question.kept_lookups(date(2026, 10, 19), 7) is None
    question.status = "held"
    question.challenge = None
    assert question.kept_lookups(date(2026, 10, 19), 7) is None


def test_keep_and_clear_the_lookups() -> None:
    question = _held_with_lookups()
    draft, challenge = question.draft, question.challenge
    question.keep_lookups(None, challenge, date(2026, 10, 19))
    assert (question.draft, question.challenge, question.looked_up_at) == (None, None, None)
    question.keep_lookups(draft, None, date(2026, 10, 19))
    assert question.draft is draft and question.challenge is None
    assert question.looked_up_at == date(2026, 10, 19)
    question.clear_lookups()
    assert (question.draft, question.challenge, question.looked_up_at) == (None, None, None)
