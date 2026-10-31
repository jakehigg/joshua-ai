"""Validation of `internet.search`: the SearXNG instance of the internet agent."""

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
"""


def parse(search: str, env: dict[str, str] | None = None) -> config.JoshuaConfig:
    block = textwrap.indent(textwrap.dedent(search).strip("\n"), "    ")
    return config.parse(BASE + "internet:\n  search:\n" + block + "\n", env or {}, source="t.yaml")


def test_no_search_is_the_default() -> None:
    assert config.parse(BASE, {}, source="t.yaml").internet.search is None


def test_a_minimal_block_takes_the_defaults() -> None:
    search = parse("url: http://searxng:8080/\n").internet.search
    assert search is not None
    assert search.provider == "searxng"
    assert search.url == "http://searxng:8080"
    assert search.api_key is None
    assert (search.max_results, search.timeout_seconds) == (10, 20)
    assert (search.categories, search.language) == (None, None)


def test_a_full_block_validates() -> None:
    search = parse(
        """
        provider: searxng
        url: https://search.example
        api_key: ${SEARXNG_API_KEY:-}
        max_results: 5
        timeout_seconds: 30
        categories: general
        language: en
        """,
        {"SEARXNG_API_KEY": "proxy-secret"},
    ).internet.search
    assert search is not None
    assert search.api_key == "proxy-secret"
    assert (search.max_results, search.timeout_seconds) == (5, 30)
    assert (search.categories, search.language) == ("general", "en")


def test_an_unset_key_is_no_key() -> None:
    """Core and channels read the same file and do not get the variable."""
    search = parse("url: http://searxng:8080\napi_key: ${SEARXNG_API_KEY:-}\n").internet.search
    assert search is not None
    assert search.api_key is None


@pytest.mark.parametrize(
    ("block", "message"),
    [
        ("provider: searxng\n", "url"),
        ("url: ''\n", "url must be set"),
        ("url: searxng:8080\n", "url must be an http"),
        ("url: ftp://searxng\n", "url must be an http"),
        ("url: http://searxng:port\n", "url is not a valid URL"),
        ("url: http://searxng:8080\nprovider: brave\n", "provider"),
        ("url: http://searxng:8080\nmax_results: 0\n", "max_results"),
        ("url: http://searxng:8080\nmax_results: 51\n", "max_results"),
        ("url: http://searxng:8080\ntimeout_seconds: 0\n", "timeout_seconds"),
        ("url: http://searxng:8080\nserver: websearch\n", "server"),
        ("url: http://searxng:8080\ntool: search\n", "tool"),
    ],
)
def test_a_bad_block_is_refused(block: str, message: str) -> None:
    with pytest.raises(config.ConfigError, match=message):
        parse(block)
