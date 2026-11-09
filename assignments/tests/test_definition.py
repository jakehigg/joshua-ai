"""The definition renderer: a fixed order, block scalars for the goal, and
stable line numbers."""

from __future__ import annotations

import json

import pytest
from joshua_assignments import definition


def test_the_keys_have_a_fixed_order_and_the_goal_is_a_block() -> None:
    spec = {
        "dry_run": False,
        "zeta": 1,
        "model": "sonnet",
        "goal": "Clean the docs.\nThen report.",
        "budget": {"tokens": None, "five_hour_pct": 50},
        "title": "Docs",
        "alpha": "x",
    }
    assert definition.lines(spec) == [
        "title: Docs",
        "goal: |-",
        "  Clean the docs.",
        "  Then report.",
        "model: sonnet",
        "budget:",
        "  five_hour_pct: 50",
        "  tokens: null",
        "dry_run: false",
        "alpha: x",
        "zeta: 1",
    ]


def test_the_same_spec_in_another_order_gives_the_same_text() -> None:
    spec = {"goal": "One line.", "model": "opus", "repos": ["a/b", "c/d"], "tools": None}
    again = dict(reversed(list(spec.items())))
    assert definition.render(spec) == definition.render(again)
    assert definition.render(json.loads(json.dumps(spec))) == definition.render(spec)


def test_a_one_line_goal_is_still_a_block() -> None:
    assert definition.lines({"goal": "Fix: the links"}) == ["goal: |-", "  Fix: the links"]


def test_an_empty_line_in_the_goal_is_an_empty_line() -> None:
    assert definition.lines({"goal": "a\n\nb"}) == ["goal: |-", "  a", "", "  b"]


def test_a_goal_that_starts_with_a_space_has_an_indent_mark() -> None:
    assert definition.lines({"goal": "  first\nnext"})[0] == "goal: |2-"


@pytest.mark.parametrize(
    ("value", "text"),
    [
        ("yes", '"yes"'),
        ("null", '"null"'),
        ("12", '"12"'),
        ("2026-01-01", '"2026-01-01"'),
        ("a: b", '"a: b"'),
        ("a #b", '"a #b"'),
        ("-x", '"-x"'),
        ("", '""'),
        (" padded", '" padded"'),
        ("telegram:dm:alex", "telegram:dm:alex"),
        ("gitlab.example.net/infra/docs", "gitlab.example.net/infra/docs"),
        (True, "true"),
        (None, "null"),
        (7, "7"),
    ],
)
def test_a_scalar_that_yaml_could_misread_is_quoted(value, text) -> None:
    assert definition.scalar(value) == text


def test_lists_and_empty_values() -> None:
    assert definition.lines({"repos": [], "budget": {}, "tools": ["a", "b"]}) == [
        "budget: {}",
        "repos: []",
        "tools:",
        "  - a",
        "  - b",
    ]


def test_a_nested_value_in_a_list_is_one_line() -> None:
    assert definition.lines({"x": [{"b": 1, "a": 2}]}) == ["x:", '  - {"a": 2, "b": 1}']


def test_line_at() -> None:
    lines = definition.lines({"title": "T", "model": "m"})
    assert definition.line_at(lines, 1) == "title: T"
    assert definition.line_at(lines, 2) == "model: m"
    assert definition.line_at(lines, 0) is None
    assert definition.line_at(lines, 3) is None
    assert definition.lines(None) == []
