"""`joshua_shared.frontmatter`: the block is read, and an update changes only
the keys it names. Every other byte of the page stays as it was."""

from __future__ import annotations

from datetime import date

import pytest
from joshua_shared import frontmatter

PAGE = (
    "---\n"
    "title: Snake plant\n"
    "# a comment a person wrote\n"
    "tags:\n"
    "- houseplants\n"
    "- succulents\n"
    "aliases: [sansevieria, mother-in-law's tongue]\n"
    "nested:\n"
    "  a: 1\n"
    "  b: two\n"
    "---\n"
    "# Snake plant\n"
    "\n"
    "Water it once a month.\n"
)


def _body(text: str) -> str:
    return frontmatter.split(text)[1]


# -- split ------------------------------------------------------------------


def test_split_reads_the_block_and_the_body() -> None:
    data, body = frontmatter.split(PAGE)
    assert data["title"] == "Snake plant"
    assert data["tags"] == ["houseplants", "succulents"]
    assert data["aliases"] == ["sansevieria", "mother-in-law's tongue"]
    assert data["nested"] == {"a": 1, "b": "two"}
    assert body == "# Snake plant\n\nWater it once a month.\n"


def test_split_with_no_block_returns_the_text() -> None:
    text = "# Title\n\nNo frontmatter here.\n"
    assert frontmatter.split(text) == ({}, text)


@pytest.mark.parametrize(
    "text",
    [
        "---\ntitle: never closes\n",
        "---\n- a list\n- not a mapping\n---\nbody\n",
        "---\ntitle: [unclosed\n---\nbody\n",
        "",
        "---",
    ],
)
def test_split_treats_a_bad_block_as_body(text: str) -> None:
    assert frontmatter.split(text) == ({}, text)


def test_split_empty_block() -> None:
    assert frontmatter.split("---\n---\nbody\n") == ({}, "body\n")


def test_split_body_with_a_rule_line() -> None:
    text = "---\na: 1\n---\nabove\n---\nbelow\n"
    data, body = frontmatter.split(text)
    assert data == {"a": 1}
    assert body == "above\n---\nbelow\n"


def test_split_crlf() -> None:
    text = "---\r\na: 1\r\n---\r\nline one\r\nline two\r\n"
    data, body = frontmatter.split(text)
    assert data == {"a": 1}
    assert body == "line one\r\nline two\r\n"


def test_split_reads_a_date() -> None:
    data, _ = frontmatter.split("---\ndate: 2026-10-19\n---\n")
    assert data["date"] == date(2026, 10, 19)


# -- update -----------------------------------------------------------------


def test_update_replaces_a_key_in_place_and_keeps_every_other_byte() -> None:
    out = frontmatter.update(PAGE, title="Sansevieria")
    expected = PAGE.replace("title: Snake plant\n", "title: Sansevieria\n")
    assert out == expected


def test_update_replaces_a_multi_line_value_in_place() -> None:
    out = frontmatter.update(PAGE, tags=["plants"])
    expected = PAGE.replace("tags:\n- houseplants\n- succulents\n", "tags:\n- plants\n")
    assert out == expected


def test_update_appends_a_new_key_at_the_end_of_the_block() -> None:
    out = frontmatter.update(PAGE, curated=date(2026, 10, 19), curated_hash="7f3a9c1e")
    expected = PAGE.replace(
        "  b: two\n---\n", "  b: two\ncurated: 2026-10-19\ncurated_hash: 7f3a9c1e\n---\n"
    )
    assert out == expected
    data, body = frontmatter.split(out)
    assert list(data) == ["title", "tags", "aliases", "nested", "curated", "curated_hash"]
    assert body == _body(PAGE)


def test_update_existing_keys_keep_their_order() -> None:
    out = frontmatter.update(PAGE, nested={"c": 3}, title="T")
    data, _ = frontmatter.split(out)
    assert list(data) == ["title", "tags", "aliases", "nested"]
    assert data["nested"] == {"c": 3}
    assert "# a comment a person wrote\n" in out


def test_update_with_no_block_adds_one_and_keeps_the_text() -> None:
    text = "# Title\n\nBody text.\n"
    out = frontmatter.update(text, status="new", people=[])
    assert out == "---\nstatus: new\npeople: []\n---\n" + text
    assert out.endswith(text)
    assert frontmatter.split(out) == ({"status": "new", "people": []}, text)


def test_update_empty_text() -> None:
    out = frontmatter.update("", a=1)
    assert out == "---\na: 1\n---\n"
    assert frontmatter.split(out) == ({"a": 1}, "")


def test_update_leaves_crlf_alone() -> None:
    text = "---\r\ntitle: A\r\nkeep: yes\r\n---\r\nline one\r\nline two\r\n"
    out = frontmatter.update(text, title="B", added=2)
    assert out == "---\r\ntitle: B\r\nkeep: yes\r\nadded: 2\r\n---\r\nline one\r\nline two\r\n"
    assert out.encode().count(b"\r\n") == out.encode().count(b"\n")


def test_update_crlf_with_no_block() -> None:
    text = "line one\r\nline two\r\n"
    out = frontmatter.update(text, a=1)
    assert out == "---\r\na: 1\r\n---\r\n" + text


def test_update_body_with_a_rule_line_is_unchanged() -> None:
    text = "---\na: 1\n---\nabove\n---\nb: not a key\n---\nbelow"
    out = frontmatter.update(text, a=2, c=3)
    assert out == "---\na: 2\nc: 3\n---\nabove\n---\nb: not a key\n---\nbelow"
    assert _body(out) == _body(text)


def test_update_text_with_no_block_but_a_rule_line() -> None:
    text = "intro\n---\nb: 1\n---\n"
    out = frontmatter.update(text, a=1)
    assert out == "---\na: 1\n---\n" + text


def test_update_quoted_key() -> None:
    text = '---\n"odd key": 1\nother: 2\n---\nbody\n'
    out = frontmatter.update(text, **{"odd key": 5})
    assert out == "---\nodd key: 5\nother: 2\n---\nbody\n"


def test_update_with_nothing_returns_the_text() -> None:
    assert frontmatter.update(PAGE) == PAGE


def test_update_keeps_a_value_with_a_colon_and_unicode() -> None:
    out = frontmatter.update(PAGE, note="65–68°F: check first")
    data, _ = frontmatter.split(out)
    assert data["note"] == "65–68°F: check first"


def test_update_long_string_is_not_wrapped() -> None:
    long = "word " * 200
    out = frontmatter.update("---\n---\n", q=long.strip())
    assert frontmatter.split(out)[0]["q"] == long.strip()


@pytest.mark.parametrize(
    "text",
    [
        "---\n- a\n- b\n---\nbody\n",
        "---\ntitle: [unclosed\n---\nbody\n",
        "---\ntitle: never closes\n",
    ],
)
def test_update_refuses_a_bad_block(text: str) -> None:
    with pytest.raises(ValueError):
        frontmatter.update(text, a=1)


def test_update_refuses_a_block_it_cannot_change_in_place() -> None:
    # A flow mapping that spans lines starts with a line that is not a key,
    # so the update cannot find the key it must replace.
    text = "---\n{a: 1,\n b: 2}\n---\nbody\n"
    with pytest.raises(ValueError):
        frontmatter.update(text, a=5)


def test_update_keeps_a_comment_above_the_first_key() -> None:
    text = "---\n# kept\na: 1\n---\nbody\n"
    assert frontmatter.update(text, a=2) == "---\n# kept\na: 2\n---\nbody\n"
