"""Topic selection: each of the four rules, the seeded pick, and the journal map."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

from jobs_fakes import make_settings
from joshua_core.jobs.research.dossier import Dossier, Question
from joshua_core.jobs.research.select import (
    PERSON_SECTION_TAIL,
    in_person_section,
    journal_counts,
    seed_ranks,
    select_topic,
)
from joshua_core.jobs.topics import Topic, list_topics
from joshua_shared.config import Person
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


# -- person sections and the roster -------------------------------------------

COACH = "coach/walking.md"
ROSTER = [Person(id="alex", name="Alex"), Person(id="bo", name="Bo")]
WITH_COACH = [*TOPICS, Topic(COACH, "coach", "Walking")]


def test_the_selector_prefers_a_topic_outside_a_person_section_at_equal_coverage() -> None:
    for n in range(20):
        got = select_topic(WITH_COACH, {}, {}, DAY + timedelta(days=n), SETTINGS)
        assert got.rule == "coverage"
        assert got.topic != COACH
        assert PERSON_SECTION_TAIL not in got.reason


def test_the_journal_rule_prefers_a_topic_outside_a_person_section() -> None:
    # The coach topic has the lowest coverage and the most entries.
    dossiers = {SNAKE: _dossier(SNAKE, 1, 2)}
    got = select_topic(WITH_COACH, dossiers, {COACH: 9, SNAKE: 1}, DAY, SETTINGS)
    assert (got.topic, got.rule, got.runner_up) == (SNAKE, "journal", COACH)


def test_the_person_rule_prefers_a_topic_outside_a_person_section() -> None:
    asked = [Question(slug="a", question="a?", asked_by="alex")]
    dossiers = {
        COACH: Dossier(topic=COACH, questions=list(asked)),
        ROSES: Dossier(topic=ROSES, questions=list(asked)),
    }
    got = select_topic(WITH_COACH, dossiers, {}, DAY, SETTINGS)
    assert (got.topic, got.rule, got.runner_up) == (ROSES, "person", COACH)


def test_a_person_section_topic_is_picked_when_it_is_alone() -> None:
    alone = [Topic(COACH, "coach", "Walking")]
    got = select_topic(alone, {}, {}, DAY, SETTINGS)
    assert (got.topic, got.rule) == (COACH, "coverage")
    assert got.reason.endswith(PERSON_SECTION_TAIL)
    done = {t.path: _dossier(t.path, 2, 2, refresh_after=DAY) for t in TOPICS}
    got = select_topic(WITH_COACH, done, {COACH: 2}, DAY, SETTINGS)
    assert (got.topic, got.rule) == (COACH, "journal")
    assert "no topic outside a person section qualifies" in got.reason


def test_the_person_sections_come_from_the_config() -> None:
    settings = make_settings("jobs:\n  research:\n    person_sections:\n      - garden\n")
    got = select_topic(WITH_COACH, {}, {ROSES: 5, COACH: 1}, DAY, settings)
    assert (got.topic, got.rule) == (COACH, "journal")
    assert in_person_section(Topic(ROSES, "garden", "Roses"), ["garden"])
    assert not in_person_section(Topic("notes.md", "", "notes"), [""])


def test_journal_counts_give_0_for_an_entry_with_only_the_name(tmp_path: Path) -> None:
    wiki = build_research_wiki(tmp_path)
    (wiki / "recipes").mkdir()
    (wiki / "recipes/caldo-verde.md").write_text(
        "---\naliases: [Alex, Bo]\n---\n# Alex's caldo verde\n\nKale soup.\n"
    )
    day = wiki / "journal/2026/10/18"
    day.mkdir(parents=True)
    (day / "run.md").write_text("Alex went for a run. Bo came too.\n")
    topics = list_topics(wiki, ["vocabulary.md"], people=ROSTER)
    soup = "recipes/caldo-verde.md"
    assert soup in [t.path for t in topics]
    assert soup not in journal_counts(topics, wiki, DAY, 14, people=ROSTER)
    (day / "soup.md").write_text("Made the caldo verde again.\n")
    assert journal_counts(topics, wiki, DAY, 14, people=ROSTER)[soup] == 1
    # Without the roster, the name alone counts the run entry too.
    assert journal_counts(topics, wiki, DAY, 14)[soup] == 2


def test_journal_counts_for_a_possessive_title_count_only_the_thing(tmp_path: Path) -> None:
    wiki = build_research_wiki(tmp_path)
    (wiki / "recipes").mkdir()
    soup = "recipes/alex-s-caldo-verde.md"
    (wiki / soup).write_text("# Alex's Caldo Verde\n\nKale, potato, and chorizo.\n")
    day = wiki / "journal/2026/10/18"
    day.mkdir(parents=True)
    (day / "plants.md").write_text("Alex watered the plants.\n")
    (day / "dinner.md").write_text("Alex's dinner was pizza.\n")
    (day / "alexs.md").write_text("Alex s list for the week.\n")
    (day / "soup.md").write_text("We made caldo verde for Bo.\n")
    topics = list_topics(wiki, ["vocabulary.md"], people=ROSTER)
    assert journal_counts(topics, wiki, DAY, 14, people=ROSTER)[soup] == 1
