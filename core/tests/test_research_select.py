"""Topic selection: each of the four rules, the seeded pick, and the journal map."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

from jobs_fakes import make_settings
from joshua_core.jobs.research.dossier import Dossier, Question
from joshua_core.jobs.research.select import (
    journal_counts,
    seed_ranks,
    select_topic,
)
from joshua_core.jobs.topics import Topic, list_topics
from research_fakes import POTHOS, ROSES, SNAKE, build_research_wiki

DAY = date(2026, 10, 19)
SETTINGS = make_settings()
TOPICS = [Topic(p, p.split("/")[0], p) for p in (ROSES, POTHOS, SNAKE)]
MANY = [Topic(f"plants/p{i:02d}.md", "plants", f"p{i}") for i in range(30)]


def _dossier(topic: str, answered: int, total: int, **kwargs) -> Dossier:
    questions = [
        Question(slug=f"q{i}", question=f"q{i}?", status="answered" if i < answered else "open")
        for i in range(total)
    ]
    return Dossier(topic=topic, questions=questions, **kwargs)


def test_rule_1_a_person_question_wins() -> None:
    dossiers = {
        POTHOS: _dossier(POTHOS, 3, 3),
        SNAKE: Dossier(topic=SNAKE, questions=[Question(slug="a", question="a?", asked_by="alex")]),
    }
    got = select_topic(TOPICS, dossiers, {ROSES: 5}, DAY, SETTINGS)
    assert (got.topic, got.rule) == (SNAKE, "person")
    assert got.runner_up == ROSES  # the next rule's first topic
    assert "person" in got.reason


def test_rule_1_ignores_a_closed_person_question() -> None:
    done = Question(slug="a", question="a?", asked_by="alex", status="answered")
    dossiers = {SNAKE: Dossier(topic=SNAKE, questions=[done])}
    got = select_topic(TOPICS, dossiers, {}, DAY, SETTINGS)
    assert got.rule != "person"


def test_rule_2_the_journal_topic_with_the_lowest_coverage() -> None:
    dossiers = {SNAKE: _dossier(SNAKE, 1, 2), POTHOS: _dossier(POTHOS, 1, 4)}
    got = select_topic(TOPICS, dossiers, {SNAKE: 3, POTHOS: 1}, DAY, SETTINGS)
    assert (got.topic, got.rule, got.runner_up) == (POTHOS, "journal", SNAKE)


def test_rule_2_ties_go_to_the_most_entries() -> None:
    got = select_topic(TOPICS, {}, {SNAKE: 3, POTHOS: 1}, DAY, SETTINGS)
    assert (got.topic, got.rule) == (SNAKE, "journal")


def test_rule_3_the_lowest_coverage_below_the_target() -> None:
    dossiers = {
        SNAKE: _dossier(SNAKE, 1, 2),
        POTHOS: _dossier(POTHOS, 4, 4),
        ROSES: _dossier(ROSES, 1, 4),
    }
    got = select_topic(TOPICS, dossiers, {}, DAY, SETTINGS)
    assert (got.topic, got.rule, got.runner_up) == (ROSES, "coverage", SNAKE)


def test_rule_4_the_oldest_refresh_before_the_day() -> None:
    old = DAY - timedelta(days=30)
    dossiers = {
        SNAKE: _dossier(SNAKE, 2, 2, refresh_after=DAY - timedelta(days=1)),
        POTHOS: _dossier(POTHOS, 2, 2, refresh_after=old),
        ROSES: _dossier(ROSES, 2, 2, refresh_after=DAY),
    }
    got = select_topic(TOPICS, dossiers, {}, DAY, SETTINGS)
    assert (got.topic, got.rule, got.runner_up) == (POTHOS, "refresh", SNAKE)


def test_no_rule_gives_no_topic() -> None:
    dossiers = {t.path: _dossier(t.path, 2, 2, refresh_after=DAY) for t in TOPICS}
    got = select_topic(TOPICS, dossiers, {}, DAY, SETTINGS)
    assert got.topic is None and got.rule is None
    assert select_topic([], {}, {}, DAY, SETTINGS).topic is None


def test_the_same_day_and_wiki_give_the_same_topic() -> None:
    first = select_topic(MANY, {}, {}, DAY, SETTINGS)
    again = select_topic(list(reversed(MANY)), {}, {}, DAY, SETTINGS)
    assert first == again
    assert first.rule == "coverage"


def test_another_day_can_pick_another_topic_among_ties() -> None:
    picks = {select_topic(MANY, {}, {}, DAY + timedelta(days=n), SETTINGS).topic for n in range(10)}
    assert len(picks) > 1


def test_the_seed_depends_on_the_topic_list() -> None:
    a = seed_ranks([t.path for t in MANY], DAY)
    b = seed_ranks([t.path for t in MANY[:-1]], DAY)
    assert sorted(a.values()) == list(range(30))
    assert a != b


def test_journal_counts_use_the_title_and_aliases_in_the_window(tmp_path: Path) -> None:
    wiki = build_research_wiki(tmp_path)
    (wiki / "journal/2026/10/18").mkdir(parents=True)
    (wiki / "journal/2026/10/18/hall.md").write_text("Snakey looks better.\n")
    (wiki / "journal/2026/10/18/run.md").write_text("---\nrun_id: 3\n---\nSnake plant.\n")
    (wiki / "journal/2026/10/01").mkdir(parents=True)
    (wiki / "journal/2026/10/01/old.md").write_text("The snake plant, long ago.\n")
    topics = list_topics(wiki, ["vocabulary.md"])
    counts = journal_counts(topics, wiki, DAY, 14)
    assert counts == {SNAKE: 2}
    assert journal_counts(topics, wiki, date(2025, 1, 1), 14) == {}
