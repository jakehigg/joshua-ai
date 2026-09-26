"""The block list that keeps a web fetch off the private network.

Every case here is a negative that matters: a URL that reaches a house, a
router, a cluster service, or the address that holds cloud credentials.
"""

from __future__ import annotations

import pytest
from joshua_shared.netblock import block_reason

HOUSE = ["example.net", "10.0.0.0/8"]

# What `joshua.example.yaml` ships. It is a starting point in a file a person
# owns, not a rule in the code, so these tests name it themselves.
EXAMPLE = [
    "0.0.0.0/8",
    "10.0.0.0/8",
    "100.64.0.0/10",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "127.0.0.0/8",
    "169.254.0.0/16",
    "::/128",
    "::1/128",
    "fc00::/7",
    "fe80::/10",
    "localhost",
    ".local",
    ".internal",
    ".home.arpa",
    ".svc",
    ".cluster.local",
]


def _blocked(url: str, blocked: list[str] | None = None, resolve: bool = False) -> bool:
    return block_reason(url, EXAMPLE if blocked is None else blocked, resolve=resolve) is not None


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


def test_a_name_that_resolves_into_a_blocked_network_is_refused() -> None:
    """A public name can point at a private address. That is the real attack."""
    assert block_reason("https://localtest.me/", ["127.0.0.0/8"], resolve=True) is not None


def test_resolution_can_be_turned_off() -> None:
    assert block_reason("https://localtest.me/", ["127.0.0.0/8"], resolve=False) is None


def test_resolution_refuses_nothing_when_the_list_names_no_network() -> None:
    """The list is the policy. With no CIDR in it, a lookup decides nothing."""
    assert block_reason("https://localtest.me/", ["example.net"], resolve=True) is None


def test_a_host_that_does_not_resolve_is_allowed_by_the_lookup() -> None:
    """A lookup that fails is not a reason to refuse; the fetch will fail anyway."""
    url = "https://this-name-does-not-exist-91731.example/"
    assert block_reason(url, ["10.0.0.0/8"], resolve=True) is None


# ── the list is the whole policy ─────────────────────────────────────────────


def test_an_empty_list_blocks_nothing() -> None:
    """A person may let Joshua read their own network. That is their call."""
    for url in (
        "http://10.0.0.5/",
        "http://127.0.0.1:8000/",
        "http://169.254.169.254/latest/meta-data/",
        "https://hub.example.net/",
        "http://localhost/",
    ):
        assert block_reason(url, [], resolve=True) is None, url


def test_a_scheme_that_is_not_http_is_refused_even_with_an_empty_list() -> None:
    """That rule is about the protocol, not about a place, so no entry moves it."""
    assert block_reason("file:///etc/passwd", []) is not None
    assert block_reason("ftp://example.com/", []) is not None


def test_one_entry_blocks_only_what_it_names() -> None:
    assert block_reason("http://10.0.0.5/", ["10.0.0.0/8"]) is not None
    assert block_reason("http://192.168.1.1/", ["10.0.0.0/8"], resolve=False) is None


def test_the_reason_never_holds_the_whole_url() -> None:
    """A log line says why, and not what a person was reading."""
    reason = block_reason("https://hub.example.net/secret/path?token=abc", HOUSE)
    assert reason is not None
    assert "secret/path" not in reason
    assert "token=abc" not in reason


def test_the_example_list_covers_every_private_range() -> None:
    """The file a person starts from refuses the networks they live on."""
    for url in ("http://10.1.1.1/", "http://172.20.0.1/", "http://192.168.1.1/"):
        assert _blocked(url)


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


# ── a URL the fetch reads as another host ────────────────────────────────────
#
# The fetch runs in Node and reads a URL by the WHATWG rules. Each URL below
# reads as a public host, or as nothing, to Python, and as a blocked address
# to Node. The check must refuse every one with the example list, and the ones
# that are about reading the URL must be refused with an empty list too.


@pytest.mark.parametrize(
    "url",
    [
        # Node reads a backslash as a slash, so the host is the metadata address.
        "http://169.254.169.254\\@example.com/latest/meta-data/",
        "http://127.0.0.1\\example.com/",
        # Node decodes a percent sign in the host.
        "http://127.0.0.%31/",
        "http://%6c%6fcalhost/",
        # A user name or a password: the classic way to make a host read as another.
        "http://example.com@127.0.0.1/",
        "http://user:pass@example.com/",
        # A tab or a newline, which Node drops and Python may not.
        "http://127.0.0.1\t/",
        "http://exa\nmple.com/",
        # A zone id.
        "http://[fe80::1%25eth0]/",
        # A port that is not a number, and a bracket that is never closed.
        "http://example.com:99999/",
        "http://[::1/",
        # Four parts where one is out of range, and too many parts.
        "http://256.1.1.1/",
        "http://1.2.3.4.5/",
    ],
)
def test_a_url_the_fetch_could_read_as_another_host_is_refused(url: str) -> None:
    assert block_reason(url, [], resolve=False) is not None


@pytest.mark.parametrize(
    "url",
    [
        # Every form of 127.0.0.1 that Node accepts.
        "http://2130706433/",
        "http://0x7f000001/",
        "http://0x7f.1/",
        "http://0177.0.0.1/",
        "http://127.1/",
        "http://127.0.0.1./",
        # A full-width digit, which IDNA folds to the plain one.
        "http://\uff11\uff12\uff17.0.0.1/",
        # The metadata address in the same forms.
        "http://2852039166/",
        "http://0xa9.0xfe.0xa9.0xfe/",
        # The zero address, which reaches this machine on Linux.
        "http://0/",
        "http://0.0.0.0:8080/",
        "http://[::]/",
        # An IPv6 address that carries a blocked IPv4 address.
        "http://[::ffff:127.0.0.1]/",
        "http://[::ffff:a9fe:a9fe]/",
        "http://[64:ff9b::7f00:1]/",
        "http://[2002:7f00:1::]/",
        "http://[::127.0.0.1]/",
        # Carrier-grade NAT, where a Tailscale address lives.
        "http://100.100.100.100/",
    ],
)
def test_every_form_of_a_blocked_address_is_refused(url: str) -> None:
    assert _blocked(url)


def test_an_ipv6_wrapper_is_checked_against_the_ipv4_entry() -> None:
    """One entry, `127.0.0.0/8`, covers the IPv6 forms of the same address."""
    assert _blocked("http://[::ffff:127.0.0.1]/", ["127.0.0.0/8"])
    assert _blocked("http://[::ffff:127.0.0.1]/", ["127.0.0.1"])
    assert not _blocked("http://[::ffff:8.8.8.8]/", ["127.0.0.0/8"])


def test_a_resolved_ipv6_wrapper_is_checked_as_ipv4(monkeypatch: pytest.MonkeyPatch) -> None:
    import ipaddress

    from joshua_shared import netblock

    monkeypatch.setattr(
        netblock, "_resolved", lambda host: [ipaddress.ip_address("::ffff:10.0.0.5")]
    )
    assert block_reason("https://inside.example.com/", ["10.0.0.0/8"], resolve=True) is not None


def test_a_name_that_only_looks_like_a_number_is_a_name() -> None:
    """`1.example.com` ends in a name, so it is a host, not an address."""
    assert not _blocked("https://1.example.com/")
    assert not _blocked("https://0x.example.com/")


def test_an_international_name_is_allowed() -> None:
    assert not _blocked("https://bücher.example/")


def test_the_check_never_raises() -> None:
    for url in ("http://[", "http://[::1", "http://:80", "http://a:b:c/", "\x00", "http://%"):
        assert block_reason(url, EXAMPLE, resolve=False) is not None
