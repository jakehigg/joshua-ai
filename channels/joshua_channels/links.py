"""How a channel writes a link.

Core turns a Joshua path into a Markdown link, in one filter
(``path_links.py`` in core). A channel never holds that pattern: it
only renders the link it is given, in the markup its platform has.

``split_links`` takes the Markdown links out of the text and reports where
each one stands, for a platform that carries a link beside the text, such as
a Telegram entity. ``inline_addresses`` writes the address on its own line,
for a platform with no link markup, such as iMessage.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# One Markdown link: ``[visible text](address)``. An image (``![alt](src)``)
# is not a link and stays as it is.
_LINK_RE = re.compile(r"(?<!!)\[(?P<text>[^\]\n]*)\]\((?P<url>[^)\s]+)\)")


@dataclass(frozen=True)
class LinkSpan:
    """One link of a text: where its visible text stands, and where it opens."""

    start: int
    length: int
    url: str


def split_links(text: str) -> tuple[str, list[LinkSpan]]:
    """The text with each Markdown link reduced to its visible text, and one
    ``LinkSpan`` per link, in order, with offsets into that text."""
    out: list[str] = []
    spans: list[LinkSpan] = []
    cursor = 0
    length = 0
    for match in _LINK_RE.finditer(text):
        before = text[cursor : match.start()]
        out.append(before)
        length += len(before)
        visible = match.group("text")
        out.append(visible)
        if visible:
            spans.append(LinkSpan(length, len(visible), match.group("url")))
        length += len(visible)
        cursor = match.end()
    out.append(text[cursor:])
    return "".join(out), spans


def inline_addresses(text: str) -> str:
    """The text with each Markdown link written as its visible text and then
    its address on a line of its own."""
    return _LINK_RE.sub(lambda m: f"{m.group('text')}\n{m.group('url')}", text)
