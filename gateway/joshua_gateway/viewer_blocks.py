"""Markdown and a definition, cut into blocks that each know their source lines.

A person reads the definition of an assignment as a document, not as YAML,
and comments on one block of it: a field, a paragraph, a heading, a list item,
a quote, a code block, or a table. A comment still anchors to one line of the
``definition`` text of the manager (``line_no``, ``line_text``), so each block
knows its first line and its last line in that text.

:func:`blocks` cuts Markdown at the block tokens of markdown-it (``map`` gives
the source lines of a token). A list item is a block of its own, also in a
nested list, and its nested list follows it as more blocks with a larger
``depth``. Each line of the text belongs to exactly one block: a block ends
on the line before the next block starts.

:func:`fields` cuts a definition at its top-level keys, in the order of the
text. The manager writes ``goal`` as a block scalar (``goal: |-``), so
:func:`goal_offset` gives the line of the text where line 1 of the goal is,
and the indent to remove.

The HTML of a block is from the wiki renderer, with raw HTML off. Each
function is pure.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import yaml
from markdown_it import MarkdownIt
from markdown_it.token import Token

# The wiki renderer: raw HTML is off, so a ``<script>`` renders as text.
MD = MarkdownIt("commonmark", {"html": False, "linkify": False}).enable(["table", "strikethrough"])

# The deepest nested list item that gets its own indent.
MAX_DEPTH = 5

_LISTS = frozenset({"bullet_list_open", "ordered_list_open"})
# The header of a block scalar: ``|``, an optional indent, an optional chomp.
_BLOCK_HEADER = re.compile(r"^\|([1-9])?[-+]?\s*$")


@dataclass(frozen=True)
class Block:
    """One block of Markdown: lines ``first`` to ``last`` (from 1) of its
    text, the HTML, the kind (``p``, ``h``, ``li``, ``quote``, ``code``,
    ``table``, ``hr``, ``other``), and the depth of a list item."""

    first: int
    last: int
    html: str
    kind: str
    depth: int = 0


def _close(tokens: Sequence[Token], start: int) -> int:
    """The index of the token that closes ``tokens[start]``."""
    level = 0
    for index in range(start, len(tokens)):
        level += tokens[index].nesting
        if level <= 0:
            return index
    return len(tokens) - 1


_KINDS = {
    "paragraph_open": "p",
    "heading_open": "h",
    "blockquote_open": "quote",
    "fence": "code",
    "code_block": "code",
    "table_open": "table",
    "hr": "hr",
}


def _render(tokens: Sequence[Token]) -> str:
    return MD.renderer.render(list(tokens), MD.options, {})


def _list_wrap(opening: Token, item: Token, inner: str) -> str:
    """One item in its own list element, so it keeps its marker and number."""
    if opening.type == "ordered_list_open":
        number = item.info if item.info.isdigit() else "1"
        start = f' start="{int(number)}"' if int(number) != 1 else ""
        return f"<ol{start}>\n{inner}</ol>\n"
    return f"<ul>\n{inner}</ul>\n"


@dataclass
class _Cut:
    first: int  # from 0
    html: str
    kind: str
    depth: int = 0


def _list(tokens: Sequence[Token], start: int, end: int, depth: int, out: list[_Cut]) -> None:
    """The items of the list ``tokens[start]`` to ``tokens[end]``."""
    opening = tokens[start]
    index = start + 1
    while index < end:
        # The children of a list are its items, one after the other.
        token = tokens[index]
        close = _close(tokens, index)
        own: list[Token] = [token]
        nested: list[tuple[int, int]] = []
        inner = index + 1
        while inner < close:
            if tokens[inner].type in _LISTS:
                stop = _close(tokens, inner)
                nested.append((inner, stop))
                inner = stop + 1
            else:
                own.append(tokens[inner])
                inner += 1
        own.append(tokens[close])
        line = token.map[0] if token.map else 0
        out.append(_Cut(line, _list_wrap(opening, token, _render(own)), "li", depth))
        for first, last in nested:
            _list(tokens, first, last, depth + 1, out)
        index = close + 1


def blocks(text: str) -> list[Block]:
    """The blocks of the Markdown ``text``, in order. Lines before the first
    block (blank lines) belong to no block."""
    tokens = MD.parse(text)
    cuts: list[_Cut] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        close = _close(tokens, index)
        if token.type in _LISTS:
            _list(tokens, index, close, 0, cuts)
        elif token.map is not None:
            kind = _KINDS.get(token.type, "other")
            cuts.append(_Cut(token.map[0], _render(tokens[index : close + 1]), kind))
        index = close + 1
    total = max(1, len(text.split("\n")))
    out = []
    for n, cut in enumerate(cuts):
        last = cuts[n + 1].first if n + 1 < len(cuts) else total
        out.append(Block(cut.first + 1, max(cut.first + 1, last), cut.html, cut.kind, cut.depth))
    return out


@dataclass(frozen=True)
class Field:
    """One top-level key of a definition: lines ``first`` to ``last`` (from
    1) of the text, the value as YAML reads it, and ``parsed`` False when
    YAML could not read the lines."""

    key: str
    first: int
    last: int
    value: Any = None
    parsed: bool = True
    lines: tuple[str, ...] = field(default_factory=tuple)


def _top(line: str) -> bool:
    return bool(line) and line[0] not in " \t#"


def fields(text: str) -> list[Field]:
    """The top-level keys of the definition ``text``, in order. Lines before
    the first key, if any, are a field with the key "" that is not parsed."""
    lines = text.split("\n") if text else []
    starts = [n for n, line in enumerate(lines) if _top(line)]
    if lines and (not starts or starts[0] != 0):
        starts.insert(0, 0)
    out = []
    for n, start in enumerate(starts):
        end = starts[n + 1] if n + 1 < len(starts) else len(lines)
        chunk = tuple(lines[start:end])
        key, value, parsed = _read(chunk)
        out.append(Field(key, start + 1, end, value, parsed, chunk))
    return out


def _read(chunk: tuple[str, ...]) -> tuple[str, Any, bool]:
    """The key and the value of one top-level key, as YAML reads them."""
    head = chunk[0]
    guess = head.split(":", 1)[0].strip() if _top(head) else ""
    try:
        loaded = yaml.safe_load("\n".join(chunk))
    except yaml.YAMLError:
        return guess, None, False
    if isinstance(loaded, dict) and len(loaded) == 1:
        [(key, value)] = loaded.items()
        return str(key), value, True
    return guess, None, False


@dataclass(frozen=True)
class Goal:
    """The goal of a definition: line ``header`` of the text is ``goal: |-``,
    and line ``n`` of ``markdown`` is line ``header + n`` of the text."""

    header: int
    last: int
    markdown: str


def goal_offset(item: Field) -> Goal | None:
    """The goal as Markdown, when the field is a block scalar, else None."""
    if not item.parsed or not isinstance(item.value, str) or not item.lines:
        return None
    _, _, rest = item.lines[0].partition(":")
    header = _BLOCK_HEADER.match(rest.strip())
    if header is None:
        return None
    body = list(item.lines[1:])
    if header.group(1):
        indent = int(header.group(1))
    else:
        first = next((line for line in body if line.strip()), "")
        indent = len(first) - len(first.lstrip(" "))
    text = "\n".join(line[indent:] if line[:indent].strip() == "" else line for line in body)
    return Goal(item.first, item.last, text)
