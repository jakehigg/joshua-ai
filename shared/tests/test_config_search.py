"""Validation of `internet.search`: the config is the allowlist of the search
route, so each rejection here is a search that never runs."""

from __future__ import annotations

import textwrap

import pytest
from joshua_shared import config

BASE = """
name: Home
timezone: America/New_York
people:
  - id: alex
    name: Alex
mcp:
  searxng:
    type: http
    url: http://searxng:8080/mcp
    allow:
  files:
    kind: builtin
  mine:
    type: stdio
    command: search-mcp
    allow:
      - alex
    identities:
      alex:
        env:
          KEY: value
"""


def parse(search: str) -> config.JoshuaConfig:
    block = textwrap.indent(textwrap.dedent(search).strip("\n"), "    ")
    return config.parse(BASE + "internet:\n  search:\n" + block + "\n", {}, source="t.yaml")


def test_no_search_is_the_default() -> None:
    assert config.parse(BASE, {}, source="t.yaml").internet.search is None


def test_a_minimal_block_takes_the_defaults() -> None:
    search = parse("server: searxng\ntool: search\n").internet.search
    assert search is not None
    assert (search.server, search.tool, search.query_arg) == ("searxng", "search", "query")
    assert search.args == {}
    assert search.max_results == 10
    assert search.timeout_seconds == 20


def test_a_full_block_validates() -> None:
    search = parse(
        """
        server: searxng
        tool: searxng_web_search
        query_arg: q
        args:
          language: en
        max_results: 5
        timeout_seconds: 30
        """
    ).internet.search
    assert search is not None
    assert search.query_arg == "q"
    assert search.args == {"language": "en"}
    assert search.max_results == 5
    assert search.timeout_seconds == 30


def test_allow_with_no_value_admits_no_person() -> None:
    cfg = parse("server: searxng\ntool: search\n")
    assert cfg.mcp["searxng"].allow == []


@pytest.mark.parametrize(
    ("block", "message"),
    [
        ("server: nothing\ntool: search\n", "unknown server 'nothing'"),
        ("server: files\ntool: search\n", "is a builtin"),
        ("server: mine\ntool: search\n", "has identities"),
        ("server: searxng\n", "tool"),
        ("server: searxng\ntool: ' '\n", "must not be empty"),
        ("server: searxng\ntool: search\nmax_results: 0\n", "max_results"),
        ("server: searxng\ntool: search\nmax_results: 51\n", "max_results"),
        ("server: searxng\ntool: search\nmax_results: many\n", "max_results"),
        ("server: searxng\ntool: search\ntimeout_seconds: 0\n", "timeout_seconds"),
        ("server: searxng\ntool: search\nunknown: 1\n", "unknown"),
        ("server: searxng\ntool: search\nargs:\n  query: fixed\n", "query argument"),
        ("server: searxng\ntool: search\nquery_arg: q\nargs:\n  q: fixed\n", "query argument"),
    ],
)
def test_a_bad_block_is_refused(block: str, message: str) -> None:
    with pytest.raises(config.ConfigError, match=message):
        parse(block)
