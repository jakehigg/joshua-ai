"""Which URLs the agent may hand to the internet agent.

A URL the agent invented, or read on a page, is the shortest way for content
from outside to send something out of this instance. These tests pin the gate.
"""

from __future__ import annotations

from joshua_core.engine.url_grants import UrlGrants, canonical, find_urls


def test_a_url_a_person_wrote_is_found() -> None:
    text = "save this recipe for me https://example.com/ribs?x=1 thanks"
    assert find_urls(text) == ["https://example.com/ribs?x=1"]


def test_punctuation_after_a_url_is_not_part_of_it() -> None:
    assert find_urls("see https://example.com/page.") == ["https://example.com/page"]
    assert find_urls("(https://example.com/a)") == ["https://example.com/a"]


def test_several_urls_keep_their_order_and_do_not_repeat() -> None:
    text = "https://a.example/1 and https://b.example/2 and https://a.example/1"
    assert find_urls(text) == ["https://a.example/1", "https://b.example/2"]


def test_text_with_no_url_finds_none() -> None:
    assert find_urls("how do I braise short ribs?") == []
    assert find_urls("") == []


def test_a_granted_url_is_recognised_whatever_the_fragment() -> None:
    grants = UrlGrants()
    grants.grant_from_text("c1", "read https://example.com/page#top")

    assert grants.is_granted("c1", "https://example.com/page")
    assert grants.is_granted("c1", "https://EXAMPLE.com/page/")


def test_the_query_string_is_part_of_the_page() -> None:
    """`?id=1` and `?id=2` are different pages, so one does not grant the other."""
    grants = UrlGrants()
    grants.grant_from_text("c1", "https://example.com/p?id=1")

    assert grants.is_granted("c1", "https://example.com/p?id=1")
    assert not grants.is_granted("c1", "https://example.com/p?id=2")


def test_a_url_nobody_gave_is_not_granted() -> None:
    """The case that matters: a page tells the agent to fetch something."""
    grants = UrlGrants()
    grants.grant_from_text("c1", "what is a good recipe for short ribs?")

    assert not grants.is_granted("c1", "https://attacker.example/?data=secret")


def test_a_grant_belongs_to_one_conversation() -> None:
    grants = UrlGrants()
    grants.grant_from_text("c1", "https://example.com/a")

    assert grants.is_granted("c1", "https://example.com/a")
    assert not grants.is_granted("c2", "https://example.com/a")


def test_a_source_of_an_answer_can_be_granted_for_a_follow_up() -> None:
    """ "Look at that second page again" has to work."""
    grants = UrlGrants()
    grants.grant("c1", ["https://example.com/source-1", "https://example.com/source-2"])

    assert grants.is_granted("c1", "https://example.com/source-2")


def test_an_old_url_falls_out_when_a_conversation_holds_too_many() -> None:
    grants = UrlGrants(max_per_conversation=3)
    grants.grant("c1", [f"https://example.com/{n}" for n in range(5)])

    assert not grants.is_granted("c1", "https://example.com/0")
    assert grants.is_granted("c1", "https://example.com/4")
    assert len(grants.granted("c1")) == 3


def test_an_old_conversation_falls_out() -> None:
    grants = UrlGrants(max_conversations=2)
    for n in range(3):
        grants.grant(f"c{n}", ["https://example.com/x"])

    assert not grants.is_granted("c0", "https://example.com/x")
    assert grants.is_granted("c2", "https://example.com/x")


def test_canonical_drops_the_fragment_and_lowers_the_host() -> None:
    assert canonical("HTTPS://Example.COM/A/#frag") == "https://example.com/A"
    assert canonical("https://example.com") == "https://example.com/"


def test_a_url_that_cannot_be_read_is_never_granted() -> None:
    """A bad port or an open bracket makes `urlsplit` raise. It must not escape."""
    grants = UrlGrants()
    for url in ("http://example.com:99999/", "http://[::1/", "http:///nohost"):
        assert canonical(url) == ""
        assert grants.grant("c1", [url]) == 0
        assert grants.is_granted("c1", url) is False
