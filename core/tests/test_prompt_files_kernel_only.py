"""The packaged kernel prompt files: they exist, and they stay short.

Every file here is prepended to every turn, so length is a cost each turn
pays. The agent's lack of SDK built-in tools is not checked here: it is
asserted at runtime in ``engine.agent`` and tested in ``test_agent_options``,
where a widened tool list fails for real rather than by a grep over prose.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_PROMPTS_DIR = Path(__file__).parent.parent / "joshua_core" / "prompts" / "builtin"
_FILES = sorted(_PROMPTS_DIR.glob("*.md"))


def test_kernel_prompt_files_exist() -> None:
    names = {p.name for p in _FILES}
    assert {
        "base.md",
        "people.md",
        "chat.md",
        "group.md",
        "guest.md",
        "event.md",
        "scheduled.md",
    } <= names


# Every kernel file stays short, because the whole set is prepended to every
# turn. ``people.md`` carries the storage rules — which of the wiki, the
# journal, and a profile a thing belongs in — and is the one file where an
# extra line buys guidance the agent cannot work out for itself.
_LINE_BUDGET = 80
# people.md carries the skill format the agent writes: the trigger, ``for``,
# and ``kind``. The format grew when a skill gained an audience and a kind, so
# the budget grew with it. Every line here is paid on every turn: cut before
# you raise this again. The insight follow-up (when to ask, and when to snooze
# or dismiss) added its paragraph. The snapshot rule (offer the action, check
# the live tool) added three lines.
_BUDGETS = {"people.md": 110}


@pytest.mark.parametrize("path", _FILES, ids=lambda p: p.name)
def test_file_within_line_budget(path: Path) -> None:
    budget = _BUDGETS.get(path.name, _LINE_BUDGET)
    lines = len(path.read_text().splitlines())
    assert lines <= budget, f"{path.name} is {lines} lines, over its {budget}-line budget"


def test_a_scheduled_journal_entry_records_only_what_changed() -> None:
    flat = " ".join((_PROMPTS_DIR / "scheduled.md").read_text().split())
    assert "only what is new or changed since the last run" in flat
    assert "Do not carry a list of standing items forward" in flat
    assert "Standing state belongs on the wiki page of the thing." in flat


def test_a_snapshot_is_never_acted_on_alone() -> None:
    flat = " ".join((_PROMPTS_DIR / "people.md").read_text().split())
    assert "A snapshot line is what a live system held at the time it names." in flat
    assert "offer the action, such as marking a task done." in flat
    assert "Call the live tool to check and to act; never act on the snapshot alone." in flat


def test_a_journal_entry_records_a_change() -> None:
    text = (_PROMPTS_DIR / "people.md").read_text()
    assert "An entry records a change, not the same state again." in text


def test_the_insight_follow_up_is_in_people_md() -> None:
    flat = " ".join((_PROMPTS_DIR / "people.md").read_text().split())
    assert "When a live conversation turns to the topic of a due insight" in flat
    assert "Never ask in a scheduled task or an event message." in flat
    assert "If they say not now, call `snooze_insight`." in flat
    assert "call `dismiss_insight` with their reason" in flat
    assert "do not write the deferral to the journal: the insight holds it." in flat


@pytest.mark.parametrize(
    ("name", "line"),
    [
        (
            "scheduled.md",
            "A scheduled task does not raise an insight unless its instruction asks for insights.",
        ),
        (
            "event.md",
            "An event does not raise an insight unless its instruction asks for insights.",
        ),
    ],
)
def test_a_task_or_an_event_does_not_raise_an_insight(name: str, line: str) -> None:
    assert line in " ".join((_PROMPTS_DIR / name).read_text().split())
