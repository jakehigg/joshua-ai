"""Read and update the YAML frontmatter of a Markdown page.

A frontmatter block is the text between a first line of exactly `---` and the
next line of exactly `---`. A trailing carriage return on a fence line is
allowed, so a page with CRLF line ends has a block too. Everything after the
closing fence is the body.

`split` reads the block. `update` adds or replaces top-level keys and keeps
everything else as it is: the other keys, their order, their comments, and the
body, byte for byte. A job adds keys to a page and never removes one, so a page
that a person wrote keeps its own text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import yaml

_FENCE = "---"
# A top-level key line: a plain or quoted key at column 0, then a colon that
# ends the line or comes before a space.
_KEY_LINE_RE = re.compile(
    r"""^(?P<key>"[^"]*"|'[^']*'|[^\s#'"\-?:][^:]*?|-[^\s:][^:]*?)\s*:(?:[ \t]|\r?\n|\Z)"""
)


@dataclass(frozen=True)
class _Block:
    """Where the frontmatter block of a text is."""

    newline: str
    # Offsets into the text: the first character of the block content, the
    # first character of the closing fence line, and the first body character.
    content_start: int
    content_end: int
    body_start: int


def _fence_line(line: str) -> bool:
    return line.rstrip("\r\n") == _FENCE


def _find(text: str) -> _Block | None:
    """The block of `text`, or None when the text has no closed block."""
    lines = text.splitlines(keepends=True)
    if not lines or not _fence_line(lines[0]) or not lines[0].endswith("\n"):
        return None
    newline = "\r\n" if lines[0].endswith("\r\n") else "\n"
    offset = len(lines[0])
    content_start = offset
    for line in lines[1:]:
        if _fence_line(line):
            return _Block(
                newline=newline,
                content_start=content_start,
                content_end=offset,
                body_start=offset + len(line),
            )
        offset += len(line)
    return None


def _load(content: str) -> dict[str, Any] | None:
    """The block content as a mapping, or None when it is not a valid one."""
    try:
        data = yaml.safe_load(content)
    except yaml.YAMLError:
        return None
    if data is None:
        return {}
    return data if isinstance(data, dict) else None


def split(text: str) -> tuple[dict[str, Any], str]:
    """Return `(frontmatter, body)` for a Markdown text.

    A text with no block gives `({}, text)`. A block that does not close, or
    that is not a YAML mapping, counts as no block, so the whole text is the
    body. The body is the exact text after the closing fence line.
    """
    block = _find(text)
    if block is None:
        return {}, text
    data = _load(text[block.content_start : block.content_end])
    if data is None:
        return {}, text
    return data, text[block.body_start :]


def _key_of(line: str) -> str | None:
    """The top-level key a block line starts, or None for a continuation line."""
    match = _KEY_LINE_RE.match(line)
    if match is None:
        return None
    key = match.group("key")
    if len(key) >= 2 and key[0] == key[-1] and key[0] in "\"'":
        return key[1:-1]
    return key


def _render(key: str, value: Any, newline: str) -> str:
    """One key and its value as block YAML, with the line ends of the page."""
    dumped = yaml.safe_dump(
        {key: value},
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
        width=10_000,
    )
    return dumped.replace("\n", newline)


def _trailing_comments(lines: list[str]) -> list[str]:
    """The comment and blank lines at column 0 at the end of an entry.

    Such a line is not part of the value. It is a person's note about the key
    that comes next, so a replaced value keeps it.
    """
    kept: list[str] = []
    for line in reversed(lines[1:]):
        if line.startswith("#") or not line.strip():
            kept.append(line)
        else:
            break
    return list(reversed(kept))


def update(text: str, **keys: Any) -> str:
    """Return `text` with each of `keys` set in its frontmatter.

    A key that is in the block already has its lines replaced where they are.
    A new key goes at the end of the block, in the order of the call. Every
    other line of the block and the whole body stay byte for byte as they were.
    A text with no block gets one at the top, and the text after it is the
    original text unchanged.

    Raises `ValueError` when the text has a block that is not a YAML mapping,
    because a second block on top of it would hide it.
    """
    if not keys:
        return text
    block = _find(text)
    if block is None:
        if text.startswith(_FENCE + "\n") or text.startswith(_FENCE + "\r\n"):
            raise ValueError("the frontmatter block does not close")
        newline = "\r\n" if text.split("\n", 1)[0].endswith("\r") else "\n"
        rendered = "".join(_render(k, v, newline) for k, v in keys.items())
        return f"{_FENCE}{newline}{rendered}{_FENCE}{newline}{text}"

    content = text[block.content_start : block.content_end]
    before = _load(content)
    if before is None:
        raise ValueError("the frontmatter block is not a YAML mapping")

    # Each entry is a top-level key and the lines that follow it, up to the
    # next top-level key. Lines before the first key stay where they are.
    preamble: list[str] = []
    entries: list[tuple[str, list[str]]] = []
    for line in content.splitlines(keepends=True):
        key = _key_of(line)
        if key is not None:
            entries.append((key, [line]))
        elif entries:
            entries[-1][1].append(line)
        else:
            preamble.append(line)

    pending = dict(keys)
    out: list[str] = list(preamble)
    for key, lines in entries:
        if key in pending:
            out.append(_render(key, pending.pop(key), block.newline))
            out.extend(_trailing_comments(lines))
        else:
            out.extend(lines)
    for key, value in pending.items():
        out.append(_render(key, value, block.newline))

    new_content = "".join(out)
    after = _load(new_content)
    expected = {**before, **keys}
    if after != expected:
        raise ValueError("the frontmatter block could not be updated in place")
    return text[: block.content_start] + new_content + text[block.content_end :]
