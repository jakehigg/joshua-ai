"""Topic selection: each of the four rules, the seeded pick, and the mention map."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

from jobs_fakes import make_settings
from joshua_core.jobs.research.dossier import Dossier, Question
from joshua_core.jobs.research.select import (
    PERSON_SECTION_TAIL,
    in_person_section,
    mention_counts,
    people_messages,
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


def test_rule_2_the_mentioned_topic_with_the_lowest_coverage() -> None:
    dossiers = {SNAKE: _dossier(SNAKE, 1, 2), POTHOS: _dossier(POTHOS, 1, 4)}
    got = select_topic(TOPICS, dossiers, {SNAKE: 3, POTHOS: 1}, DAY, SETTINGS)
    assert (got.topic, got.rule, got.runner_up) == (POTHOS, "mentioned", SNAKE)


def test_rule_2_ties_go_to_the_most_messages() -> None:
    got = select_topic(TOPICS, {}, {SNAKE: 3, POTHOS: 1}, DAY, SETTINGS)
    assert (got.topic, got.rule) == (SNAKE, "mentioned")


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


def test_mention_counts_use_the_title_and_aliases(tmp_path: Path) -> None:
    wiki = build_research_wiki(tmp_path)
    topics = list_topics(wiki, ["vocabulary.md"])
    counts = mention_counts(topics, wiki, ["Snakey looks better.", "Snake plant?", "Hello."])
    assert counts == {SNAKE: 2}
    assert mention_counts(topics, wiki, []) == {}


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


def test_the_mentioned_rule_prefers_a_topic_outside_a_person_section() -> None:
    # The coach topic has the lowest coverage and the most messages.
    dossiers = {SNAKE: _dossier(SNAKE, 1, 2)}
    got = select_topic(WITH_COACH, dossiers, {COACH: 9, SNAKE: 1}, DAY, SETTINGS)
    assert (got.topic, got.rule, got.runner_up) == (SNAKE, "mentioned", COACH)


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
    assert (got.topic, got.rule) == (COACH, "mentioned")
    assert "no topic outside a person section qualifies" in got.reason


def test_the_person_sections_come_from_the_config() -> None:
    settings = make_settings("jobs:\n  research:\n    person_sections:\n      - garden\n")
    got = select_topic(WITH_COACH, {}, {ROSES: 5, COACH: 1}, DAY, settings)
    assert (got.topic, got.rule) == (COACH, "mentioned")
    assert in_person_section(Topic(ROSES, "garden", "Roses"), ["garden"])
    assert not in_person_section(Topic("notes.md", "", "notes"), [""])


def test_mention_counts_give_0_for_a_message_with_only_the_name(tmp_path: Path) -> None:
    wiki = build_research_wiki(tmp_path)
    (wiki / "recipes").mkdir()
    (wiki / "recipes/caldo-verde.md").write_text(
        "---\naliases: [Alex, Bo]\n---\n# Alex's caldo verde\n\nKale soup.\n"
    )
    topics = list_topics(wiki, ["vocabulary.md"], people=ROSTER)
    soup = "recipes/caldo-verde.md"
    assert soup in [t.path for t in topics]
    run = ["Alex went for a run. Bo came too."]
    assert soup not in mention_counts(topics, wiki, run, people=ROSTER)
    both = [*run, "Made the caldo verde again."]
    assert mention_counts(topics, wiki, both, people=ROSTER)[soup] == 1
    # Without the roster, the name alone counts the run message too.
    assert mention_counts(topics, wiki, both)[soup] == 2


def test_mention_counts_for_a_possessive_title_count_only_the_thing(tmp_path: Path) -> None:
    wiki = build_research_wiki(tmp_path)
    (wiki / "recipes").mkdir()
    soup = "recipes/alex-s-caldo-verde.md"
    (wiki / soup).write_text("# Alex's Caldo Verde\n\nKale, potato, and chorizo.\n")
    messages = [
        "Alex is out today.",
        "Alex's dinner was pizza.",
        "Alex s list for the week.",
        "We made caldo verde for Bo.",
    ]
    topics = list_topics(wiki, ["vocabulary.md"], people=ROSTER)
    assert mention_counts(topics, wiki, messages, people=ROSTER)[soup] == 1


# -- only what people say counts ----------------------------------------------

FILTER = "home/air-filter.md"
BACKUP = "home/backup-check.md"
HOME = {
    FILTER: "# Air filter\n\nThe filter of the hall unit.\n",
    BACKUP: "# Backup check\n\nThe weekly check of the backup.\n",
}
HOME_TOPICS = [Topic(FILTER, "home", "Air filter"), Topic(BACKUP, "home", "Backup check")]


def _row(direction: str, content: str, **who: str) -> dict[str, str]:
    return {"direction": direction, "content": content, **who}


def test_only_inbound_rows_from_people_on_the_roster_count() -> None:
    rows = [
        _row("in", "Alex: is the air filter due?", person_id="alex"),
        _row("in", "Bo asks about the backup check.", speaker="Bo"),
        _row("out", "The air filter is due soon.", person_id="alex"),
        _row("task", "Check the air filter and report.", person_id="alex"),
        _row("inject", "Event: the backup check failed.", person_id="alex"),
        _row("system", "The air filter again.", person_id="alex"),
        _row("in", "A stranger names the air filter.", speaker="+15550100"),
        _row("in", "No sender names the backup check."),
        _row("in", "   ", person_id="alex"),
    ]
    assert people_messages(rows, ROSTER) == [
        "Alex: is the air filter due?",
        "Bo asks about the backup check.",
    ]
    assert people_messages(rows, []) == []


def test_joshua_task_and_event_rows_give_no_mention(tmp_path: Path) -> None:
    wiki = build_research_wiki(tmp_path, HOME)
    topics = list_topics(wiki, [])
    rows = [
        _row(direction, "The air filter is due. The backup check is late.", person_id="alex")
        for direction in ("out", "task", "inject")
        for _ in range(5)
    ]
    texts = people_messages(rows, ROSTER)
    assert texts == []
    assert mention_counts(topics, wiki, texts, people=ROSTER) == {}
    # With no mention, the rule gives nothing and coverage chooses.
    got = select_topic(HOME_TOPICS, {}, mention_counts(topics, wiki, texts), DAY, SETTINGS)
    assert got.rule == "coverage"


def test_a_guest_message_counts_like_a_member_message(tmp_path: Path) -> None:
    wiki = build_research_wiki(tmp_path, HOME)
    topics = list_topics(wiki, [])
    roster = [*ROSTER, Person(id="sam", name="Sam", role="guest")]
    rows = [_row("in", "Sam: when is the backup check?", speaker="Sam")]
    texts = people_messages(rows, roster)
    assert mention_counts(topics, wiki, texts, people=roster) == {BACKUP: 1}


def test_a_roster_name_is_never_a_topic_match(tmp_path: Path) -> None:
    desk = "home/desk-lamp.md"
    pages = {**HOME, desk: "---\naliases: [Alex, Bo]\n---\n# Desk lamp\n\nThe lamp.\n"}
    wiki = build_research_wiki(tmp_path, pages)
    topics = list_topics(wiki, [], people=ROSTER)
    texts = ["Alex says hello.", "Alex and Bo are out."]
    assert mention_counts(topics, wiki, texts, people=ROSTER) == {}
    # Without the roster, the alias alone is a match: the roster rule removes it.
    assert mention_counts(topics, wiki, texts) == {desk: 2}


def test_the_mentioned_rule_orders_by_coverage_then_messages_then_the_seed() -> None:
    dossiers = {FILTER: _dossier(FILTER, 1, 2), BACKUP: _dossier(BACKUP, 1, 2)}
    # Equal coverage: the topic with more messages wins.
    got = select_topic(HOME_TOPICS, dossiers, {FILTER: 1, BACKUP: 4}, DAY, SETTINGS)
    assert (got.topic, got.rule, got.runner_up) == (BACKUP, "mentioned", FILTER)
    # A lower coverage wins over more messages.
    dossiers[FILTER] = _dossier(FILTER, 0, 2)
    got = select_topic(HOME_TOPICS, dossiers, {FILTER: 1, BACKUP: 4}, DAY, SETTINGS)
    assert got.topic == FILTER
    # A full tie goes to the seeded pick.
    rank = seed_ranks([FILTER, BACKUP], DAY)
    got = select_topic(HOME_TOPICS, {}, {FILTER: 2, BACKUP: 2}, DAY, SETTINGS)
    assert got.topic == min((FILTER, BACKUP), key=rank.__getitem__)


def test_the_mentioned_reason_counts_messages_from_people() -> None:
    got = select_topic(HOME_TOPICS, {}, {FILTER: 3}, DAY, SETTINGS)
    days = SETTINGS.jobs.bundle.recent_days
    assert got.reason == (
        f"3 messages from people in the last {days} days name {FILTER}, and its "
        "coverage is 0.00, the lowest of those topics."
    )
    assert "journal" not in got.reason


def test_no_topic_reason_names_the_messages_of_people() -> None:
    dossiers = {t.path: _dossier(t.path, 2, 2, refresh_after=DAY) for t in HOME_TOPICS}
    got = select_topic(HOME_TOPICS, dossiers, {}, DAY, SETTINGS)
    assert got.topic is None
    assert "messages from people" in got.reason
