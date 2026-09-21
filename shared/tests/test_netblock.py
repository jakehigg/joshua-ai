"""The block list that keeps a web fetch off the private network.

Every case here is a negative that matters: a URL that reaches a house, a
router, a cluster service, or the address that holds cloud credentials.
"""

from __future__ import annotations

import pytest
from joshua_shared.netblock import DEFAULT_BLOCKED, block_reason

HOUSE = ["example.net", "10.0.0.0/8"]


def _blocked(url: str, blocked: list[str] | None = None, resolve: bool = False) -> bool:
    return block_reason(url, blocked or list(DEFAULT_BLOCKED), resolve=resolve) is not None


@pytest.mark.parametrize(
    "url",
    [
        "http://10.1.2.3/admin",
        "https://192.168.1.1/",
        "http://172.16.5.4:8080/status",
        "http://127.0.0.1:8000/",
        "http://localhost:8000/",
        "https://[::1]/",
        "http://169.254.169.254/latest/meta-data/",  # the cloud credential address
        "http://[fe80::1]/",
        "http://printer.local/",
        "https://service.namespace.svc.cluster.local/mcp",
        "https://api.internal/keys",
    ],
)
def test_a_private_address_is_refused(url: str) -> None:
    assert _blocked(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://www.seriouseats.com/braised-short-ribs",
        "https://en.wikipedia.org/wiki/Braising",
        "http://example.com/recipe?q=short+ribs",
        "https://8.8.8.8/",
    ],
)
def test_the_open_web_is_allowed(url: str) -> None:
    assert not _blocked(url)


def test_a_blocked_domain_covers_every_host_under_it() -> None:
    """One entry for the house, not one for each service in it."""
    assert _blocked("https://hub.example.net/api", HOUSE)
    assert _blocked("https://git.example.net/", HOUSE)
    assert _blocked("https://example.net/", HOUSE)
    assert not _blocked("https://example.net.other.example/", HOUSE)


def test_a_leading_dot_entry_covers_the_domain_itself() -> None:
    assert _blocked("https://printer.local/", [".local"])
    assert _blocked("https://local/", [".local"])


def test_a_plain_word_is_looked_for_in_the_url() -> None:
    """The simple case a person expects: text they do not want fetched."""
    assert _blocked("https://example.com/internal-wiki/page", ["internal-wiki"])
    assert not _blocked("https://example.com/public/page", ["internal-wiki"])


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://10.0.0.1/",
        "gopher://example.com/",
        "data:text/html,<script>",
    ],
)
def test_only_http_and_https_are_allowed(url: str) -> None:
    assert _blocked(url)


def test_an_empty_url_is_refused() -> None:
    assert _blocked("")
    assert _blocked("   ")


def test_a_url_with_no_host_is_refused() -> None:
    assert _blocked("http:///nohost")


def test_credentials_in_the_url_do_not_hide_the_host() -> None:
    """`user@host` is a classic way to make a URL read as another host."""
    assert _blocked("https://www.google.com@hub.example.net/api", HOUSE)
    assert _blocked("https://example.com@10.0.0.5/", HOUSE)


def test_a_port_does_not_hide_the_host() -> None:
    assert _blocked("http://10.0.0.5:9090/metrics", HOUSE)


def test_the_case_of_the_host_does_not_matter() -> None:
    assert _blocked("https://HUB.Example.NET/api", HOUSE)


def test_a_name_that_resolves_inside_is_refused() -> None:
    """A public name can point at a private address. That is the real attack."""
    assert block_reason("https://localtest.me/", [], resolve=True) is not None


def test_resolution_can_be_turned_off() -> None:
    assert block_reason("https://localtest.me/", [], resolve=False) is None


def test_a_host_that_does_not_resolve_is_allowed_by_the_lookup() -> None:
    """A lookup that fails is not a reason to refuse; the fetch will fail anyway."""
    url = "https://this-name-does-not-exist-91731.example/"
    assert block_reason(url, [], resolve=True) is None


def test_the_reason_never_holds_the_whole_url() -> None:
    """A log line says why, and not what a person was reading."""
    reason = block_reason("https://hub.example.net/secret/path?token=abc", HOUSE)
    assert reason is not None
    assert "secret/path" not in reason
    assert "token=abc" not in reason


def test_the_defaults_cover_every_private_range() -> None:
    for entry in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.0/8"):
        assert entry in DEFAULT_BLOCKED


def test_a_bad_entry_does_not_break_the_check() -> None:
    """A person writes what they write. A nonsense entry must not open the door."""
    assert _blocked("http://10.0.0.5/", ["not a cidr/", "10.0.0.0/8"])
    assert not _blocked("https://example.com/", ["300.300.300.300/8"])


# ── a pattern entry ──────────────────────────────────────────────────────────


def test_a_star_entry_covers_the_hosts_under_a_domain() -> None:
    """`*.example.net` is what a person writes, so it has to work."""
    assert _blocked("https://hub.example.net/api", ["*.example.net"])
    assert _blocked("https://a.b.example.net/", ["*.example.net"])


def test_a_star_entry_covers_the_domain_itself() -> None:
    """A list that quietly misses the apex is worse than one that says no twice."""
    assert _blocked("https://example.net/", ["*.example.net"])


def test_a_star_entry_covers_nobody_else() -> None:
    assert not _blocked("https://example.net.other.example/", ["*.example.net"])
    assert not _blocked("https://www.seriouseats.com/", ["*.example.net"])


def test_a_star_can_sit_anywhere_in_the_host() -> None:
    assert _blocked("https://hub.example.org/", ["hub.*"])
    assert _blocked("http://192.168.1.1/", ["192.168.*"])
    assert not _blocked("https://example.org/", ["hub.*"])


def test_a_question_mark_matches_one_character() -> None:
    assert _blocked("https://nas1.example.com/", ["nas?.example.com"])
    assert not _blocked("https://nas12.example.com/", ["nas?.example.com"])


def test_a_star_entry_is_matched_against_the_host_and_not_the_path() -> None:
    """A pattern names a host. It does not quietly block half the web by path."""
    assert not _blocked("https://example.com/hub.example.net/page", ["*.example.net"])
