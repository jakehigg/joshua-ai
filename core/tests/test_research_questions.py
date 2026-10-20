"""The question pages: the model, the body, the list, and who would know."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from joshua_core.jobs.research.questions import (
    EVERYONE,
    QuestionPage,
    counts,
    load_question,
    load_questions,
    new_slug,
    parse_question,
    person_text,
    question_path,
    question_section,
    render_index,
    roster,
    set_answer,
    who_would_know,
    write_index,
    write_question,
    write_text,
)
from joshua_shared import frontmatter, layout

TOPIC = "houseplants/snake-plants-living-room-window.md"
FEED = "journal/2026/10/04/snake-plants-aloe-watered-fed.md"
PEOPLE = [("alex", "Alex"), ("sam", "Sam Lee")]


def _page(question: str, raised: date, **extra: object) -> QuestionPage:
    return QuestionPage.model_validate(
        {
            "question": question,
            "topic": TOPIC,
            "section": "houseplants",
            "why": "The dose is not in the page.",
            "context": [TOPIC, FEED],
            "grounded_in": ["fed at the October watering", "Liquid rather than granular"],
            "route_reason": "names a record date",
            "raised_at": raised,
            "run_id": 6,
            **extra,
        }
    )


def _wiki(tmp_path: Path) -> Path:
    wiki = layout.wiki_root(tmp_path)
    for rel in (TOPIC, FEED):
        (wiki / rel).parent.mkdir(parents=True, exist_ok=True)
        (wiki / rel).write_text("# Page\n")
    return wiki


def test_the_paths() -> None:
    assert question_section(TOPIC) == "houseplants"
    assert question_section("Home Notes.md") == "general"
    assert question_section("My Plants/x.md") == "my-plants"
    assert question_path("houseplants/dose", "/d") == Path("/d/wiki/questions/houseplants/dose.md")
    for bad in ("dose", "a/b/c", "../x", "Bad/x"):
        with pytest.raises(ValueError):
            question_path(bad, "/d")
    assert layout.questions_index("/d") == Path("/d/wiki/questions/README.md")


def test_a_page_round_trips_with_the_for_key(tmp_path: Path) -> None:
    _wiki(tmp_path)
    page = _page(
        "What product and dose went on the snake plants at the October 4 feed?", date(2026, 10, 19)
    )
    path = write_question("houseplants/product-dose", page, tmp_path)
    assert path == layout.question_page("houseplants", "product-dose", tmp_path)
    text = path.read_text()
    meta, body = frontmatter.split(text)
    assert meta["for"] == EVERYONE and "for_" not in meta
    assert meta["status"] == "open" and meta["answered_at"] is None and meta["run_id"] == 6
    assert body.startswith("# What product and dose went on the snake plants")
    assert "- [houseplants/snake-plants-living-room-window.md](../../houseplants/" in body
    assert f"(../../{FEED})" in body
    loaded = load_question("houseplants/product-dose", tmp_path)
    assert loaded is not None and loaded.page == page
    assert loaded.rel == "questions/houseplants/product-dose.md"
    assert load_question("houseplants/none", tmp_path) is None
    assert load_question("bad", tmp_path) is None


def test_a_page_that_does_not_parse_is_left_out(tmp_path: Path) -> None:
    root = layout.questions_root(tmp_path) / "houseplants"
    root.mkdir(parents=True)
    (root / "x.md").write_text("---\nquestion: q\ntopic: t\n---\n")
    (root / "y.md").write_text(
        "---\nquestion: q\ntopic: t\nstatus: bogus\nsection: s\nraised_at: 2026-10-01\n---\n"
    )
    (root / ".hidden.md").write_text("x")
    (layout.questions_root(tmp_path) / ".git").mkdir()
    assert load_questions(tmp_path) == []
    assert parse_question("no frontmatter") is None


def test_a_write_outside_questions_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="not under wiki/questions"):
        write_text(layout.wiki_root(tmp_path) / "questionsx" / "a.md", "x", tmp_path)
    with pytest.raises(ValueError):
        write_text(layout.questions_root(tmp_path) / ".." / "a.md", "x", tmp_path)


def test_the_slug_is_unique_in_the_section(tmp_path: Path) -> None:
    _wiki(tmp_path)
    question = "What product and dose went on the snake plants?"
    first = new_slug("houseplants", question, tmp_path)
    write_question(f"houseplants/{first}", _page(question, date(2026, 10, 1)), tmp_path)
    second = new_slug("houseplants", question, tmp_path)
    assert second == f"{first}-2"
    assert new_slug("houseplants", question, tmp_path, [second]) == f"{first}-3"
    assert new_slug("garden", question, tmp_path) == first


@pytest.mark.parametrize(
    ("texts", "who"),
    [
        (["Alex fed the snake plants at the October watering."], "alex"),
        (["sam lee repotted them."], "sam"),
        (["Fed by sam."], "sam"),
        (["Alex and Sam Lee fed them."], EVERYONE),
        (["Fed at the October watering."], EVERYONE),
        (["Alexander fed them."], EVERYONE),
    ],
)
def test_who_would_know(texts: list[str], who: str) -> None:
    assert who_would_know(texts, PEOPLE) == who


def test_the_roster() -> None:
    from jobs_fakes import make_settings

    assert roster(make_settings()) == [("alex", "Alex")]
    assert roster(object()) == []


def test_an_answer_keeps_only_links_inside_the_wiki(tmp_path: Path) -> None:
    _wiki(tmp_path)
    me = "questions/houseplants/product-dose.md"
    text = (
        "## Heading\nHalf strength. "
        "See [the page](houseplants/snake-plants-living-room-window.md), "
        "[the feed](/journal/2026/10/04/snake-plants-aloe-watered-fed.md#top), "
        "[a site](https://example.com/x), [gone](houseplants/gone.md), "
        "[up](../../../etc/passwd.md), [proto](//example.com/a.md), ![img](x.png), [[secret]]"
    )
    out = person_text(text, me, tmp_path)
    assert "## Heading" not in out and out.startswith("Heading")
    assert "[the page](../../houseplants/snake-plants-living-room-window.md)" in out
    assert f"[the feed](../../{FEED})" in out
    assert "example.com" not in out
    assert "[gone]" not in out and "gone" in out
    assert "passwd" not in out.split("](")[-1]
    assert "[[" not in out and "secret" in out


def test_set_answer_writes_the_answer_section(tmp_path: Path) -> None:
    _wiki(tmp_path)
    page = _page("What product and dose went on the snake plants?", date(2026, 10, 1))
    write_question("houseplants/dose", page, tmp_path)
    current = load_question("houseplants/dose", tmp_path)
    assert current is not None
    done = set_answer(
        current,
        tmp_path,
        status="answered",
        answer="Half strength of a 10-10-10 liquid feed.",
        by="alex",
        day=date(2026, 10, 20),
    )
    assert done.page.status == "answered" and done.page.answered_by == "alex"
    assert done.text.rstrip().endswith("## Answer\n\nHalf strength of a 10-10-10 liquid feed.")
    gone = set_answer(
        done, tmp_path, status="dismissed", answer="Not tracked.", by="alex", day=date(2026, 10, 21)
    )
    assert gone.text.rstrip().endswith("Dismissed: Not tracked.")
    with pytest.raises(ValueError):
        set_answer(done, tmp_path, status="bogus", answer="x", by="alex", day=date(2026, 10, 21))
    assert counts(tmp_path, TOPIC) == (0, 0)


def test_the_index_order(tmp_path: Path) -> None:
    _wiki(tmp_path)
    write_question(
        "houseplants/new", _page("New question about snake plants?", date(2026, 10, 10)), tmp_path
    )
    write_question(
        "houseplants/old", _page("Old question about snake plants?", date(2026, 9, 1)), tmp_path
    )
    write_question(
        "houseplants/early",
        _page(
            "Early answered question about snake plants?",
            date(2026, 8, 1),
            status="answered",
            answer="Yes.",
            answered_by="alex",
            answered_at=date(2026, 9, 2),
        ),
        tmp_path,
    )
    write_question(
        "houseplants/late",
        _page(
            "Late dismissed question about snake plants?",
            date(2026, 8, 2),
            status="dismissed",
            answer="No.",
            answered_by="alex",
            answered_at=date(2026, 10, 2),
        ),
        tmp_path,
    )
    assert counts(tmp_path, TOPIC) == (2, 1)
    text = write_index(tmp_path).read_text()
    assert text == render_index(load_questions(tmp_path))
    meta, body = frontmatter.split(text)
    assert meta == {"source": "research", "index": False}
    first = body.split("\n\n")[1]
    assert "could not answer from the web or from our records" in first
    assert "Joshua does not send them" in first and "answers one in chat" in first
    order = [
        body.index(q) for q in ("Old question", "New question", "Late dismissed", "Early answered")
    ]
    assert order == sorted(order)
    assert (
        body.index("## Open") < body.index("Old question") < body.index("## Answered and dismissed")
    )
    assert "| [Old question about snake plants?](houseplants/old.md) | [" in body
    assert "| everyone | 2026-09-01 |" in body
    assert "| dismissed | [Late dismissed" in body and "| alex | 2026-10-02 |" in body


def test_an_empty_index(tmp_path: Path) -> None:
    body = render_index([])
    assert "| none | | | |" in body and "| none | | | | |" in body
