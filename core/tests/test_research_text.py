"""The text helpers of the researcher: bundle parts, sentences, and links."""

from __future__ import annotations

from joshua_core.jobs.bundle import Bundle
from joshua_core.jobs.research.text import (
    Part,
    allowed_urls,
    bundle_parts,
    code_span,
    collapse,
    extract_sentences,
    fenced,
    find_phrase,
    inline,
    strip_links,
)
from joshua_core.jobs.topics import Topic


def _bundle() -> Bundle:
    text = (
        "## houseplants/snake-plant.md\n\n# Snake plant\n\nIt lives in the hall. "
        "We use a gritty soil mix. Water to runoff.\n\n"
        "## journal/2026/10/15/leaf.md\n\nThe leaves flopped.\n- tied the leaves\n- moved it\n"
    )
    return Bundle(
        topic=Topic("houseplants/snake-plant.md", "houseplants", "Snake plant"),
        paths=["houseplants/snake-plant.md", "journal/2026/10/15/leaf.md"],
        text=text,
        chars=len(text),
        truncated=False,
    )


def test_bundle_parts_follow_the_paths() -> None:
    parts = bundle_parts(_bundle())
    assert [p.path for p in parts] == ["houseplants/snake-plant.md", "journal/2026/10/15/leaf.md"]
    assert parts[0].text.startswith("# Snake plant")
    assert "## journal" not in parts[0].text
    assert parts[1].text.startswith("The leaves flopped.")


def test_the_sentence_of_a_phrase_and_its_page() -> None:
    parts = bundle_parts(_bundle())
    found = extract_sentences(["GRITTY  soil mix", "tied the leaves", "not there"], parts)
    assert [(g.path, g.sentence) for g in found] == [
        ("houseplants/snake-plant.md", "We use a gritty soil mix."),
        ("journal/2026/10/15/leaf.md", "- tied the leaves"),
    ]


def test_a_phrase_across_two_blocks_falls_back_to_itself() -> None:
    parts = [Part("a.md", "one block ends\n\nthe next one")]
    got = find_phrase("ends the next", parts)
    assert got is not None and got.sentence == "ends the next"
    assert find_phrase("   ", parts) is None


def test_a_long_sentence_is_cut_around_the_phrase() -> None:
    text = "word " * 200 + "the phrase here " + "word " * 200
    got = find_phrase("the phrase here", [Part("a.md", text)])
    assert got is not None
    assert "the phrase here" in got.sentence and len(got.sentence) <= 400


def test_strip_links_keeps_only_a_source_link() -> None:
    allowed = allowed_urls(["https://example.org/a"])
    text = (
        "See [ok](https://example.org/a), [bad](https://evil.example.com/b), "
        "![pic](https://example.org/a), [[secret]], [[page|label]], "
        "and <https://x.example.com> here."
    )
    assert strip_links(text, allowed) == (
        "See [ok](https://example.org/a), bad, pic, secret, label, and https://x.example.com here."
    )


def test_small_helpers() -> None:
    assert collapse("  A\n  b ") == "a b"
    assert inline("a\n\nb", 10) == "a b"
    assert code_span("a `b` c") == "`a 'b' c`"
    assert code_span("x" * 400, 10).endswith("…`")
    assert fenced("has ``` inside").startswith("````markdown\n")
