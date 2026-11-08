"""The spec model, and how resolve fills, clamps, and refuses."""

from __future__ import annotations

import pytest
from conftest import make_config
from joshua_assignments.spec import (
    AssignmentSpec,
    SpecRefused,
    check_duration,
    first_error,
    page_path,
    resolve,
    slug,
)
from pydantic import ValidationError


def section(extra: str = ""):
    return make_config(extra).assignments


def test_a_minimal_spec_takes_every_default() -> None:
    resolved = resolve(AssignmentSpec(goal="  Tidy the wiki  "), section())
    assert resolved.spec == {
        "goal": "Tidy the wiki",
        "model": "sonnet",
        "budget": {"five_hour_pct": 70, "seven_day_pct": 85, "tokens": None},
        "max_turns": 200,
        "working_time": "4h",
        "repos": [],
        "tools": None,
        "tasks_project": None,
        "report_to": "conversation",
        "dry_run": False,
    }
    assert resolved.model_id == "claude-sonnet-5"
    assert resolved.effort == "medium"
    assert resolved.notes == []


def test_a_full_spec_keeps_its_values() -> None:
    spec = AssignmentSpec(
        goal="Audit",
        model="opus",
        budget={"five_hour_pct": 80, "seven_day_pct": 90, "tokens": 2000000},
        max_turns=50,
        working_time="2h",
        repos=["https://gitlab.example.net/infra/gitops.git", "gitlab.example.net/infra/gitops"],
        tools=["gitlab", "tasks"],
        tasks_project="joshua",
        report_to="everyone",
        dry_run=True,
    )
    resolved = resolve(spec, section())
    assert resolved.spec["budget"] == {"five_hour_pct": 80, "seven_day_pct": 90, "tokens": 2000000}
    assert resolved.spec["repos"] == ["gitlab.example.net/infra/gitops"]
    assert resolved.spec["report_to"] == "everyone"
    assert resolved.model_id == "claude-opus-5" and resolved.effort == "high"


def test_both_ceilings_are_clamped_with_a_note_each() -> None:
    spec = AssignmentSpec(goal="x", budget={"five_hour_pct": 99, "seven_day_pct": 100})
    resolved = resolve(spec, section())
    assert resolved.spec["budget"]["five_hour_pct"] == 90
    assert resolved.spec["budget"]["seven_day_pct"] == 95
    assert resolved.notes == [
        "budget.five_hour_pct 99 is above the maximum 90, so it is 90",
        "budget.seven_day_pct 100 is above the maximum 95, so it is 95",
    ]


def test_an_unknown_model_is_refused() -> None:
    with pytest.raises(SpecRefused) as exc:
        resolve(AssignmentSpec(goal="x", model="haiku"), section())
    assert exc.value.reason == "unknown_model"
    assert "opus, sonnet" in exc.value.message


def test_a_repo_outside_the_allowlist_is_refused() -> None:
    with pytest.raises(SpecRefused) as exc:
        resolve(AssignmentSpec(goal="x", repos=["github.com/example-org/app"]), section())
    assert exc.value.reason == "repo_not_allowed"


def test_a_bad_repo_is_refused() -> None:
    with pytest.raises(SpecRefused) as exc:
        resolve(AssignmentSpec(goal="x", repos=["not a repo"]), section())
    assert exc.value.reason == "invalid_repo"


@pytest.mark.parametrize(
    "fields",
    [
        {"goal": ""},
        {"goal": "   "},
        {"goal": "x", "budget": {"five_hour_pct": 0}},
        {"goal": "x", "budget": {"five_hour_pct": 101}},
        {"goal": "x", "budget": {"tokens": 0}},
        {"goal": "x", "max_turns": 0},
        {"goal": "x", "working_time": "soon"},
        {"goal": "x", "working_time": "0h"},
        {"goal": "x", "tools": ["Not A Server"]},
        {"goal": "x", "unknown": 1},
        {"goal": "x", "repos": ["a"] * 21},
    ],
)
def test_a_bad_spec_is_refused(fields: dict) -> None:
    with pytest.raises(ValidationError):
        AssignmentSpec.model_validate(fields)


def test_first_error_names_the_place_and_not_the_value() -> None:
    with pytest.raises(ValidationError) as exc:
        AssignmentSpec.model_validate({"goal": "x", "budget": {"five_hour_pct": 1234}})
    text = first_error(exc.value)
    assert text.startswith("budget.five_hour_pct:")
    assert "1234" not in text
    assert first_error(ValueError("plain")) == "plain"


def test_check_duration() -> None:
    assert check_duration("90m") == 5400
    with pytest.raises(ValueError):
        check_duration("0s")


@pytest.mark.parametrize(
    ("goal", "expected"),
    [
        ("Audit kubernetes-gitops against its rules", "audit-kubernetes-gitops-against-its"),
        ("\n\n  Café déjà vu  \nsecond line", "cafe-deja-vu"),
        ("!!!", "assignment"),
        ("x" * 50, "assignment"),
    ],
)
def test_slug(goal: str, expected: str) -> None:
    assert slug(goal) == expected
    assert len(slug(goal)) <= 40


def test_page_path() -> None:
    assert page_path(12, "Audit kubernetes gitops") == "assignments/12-audit-kubernetes-gitops.md"
