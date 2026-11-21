"""Turn a Joshua path in an outbound text into a link.

One filter runs over every text Joshua sends on a text channel, in
``engine/manager.py`` right before the reply is stored and delivered. It
rewrites a path of the data volume into a Markdown link whose visible text is
the path and whose address is the route of the viewer. A channel renders that
link in its own markup: the viewer shows the Markdown, Telegram sends a link
entity, iMessage puts the address on its own line. No channel holds this
pattern.

The rule is deterministic and reads nothing: it never asks the database
whether a path exists, so it links a path that was never written, and that
dead link opens the normal not-found page of the viewer. Only these shapes
match:

- ``documents/<owner>/<slug>.md`` and ``documents/<owner>/<slug>.html``
- ``wiki/<any path>.md``
- ``wiki/attachments/<any path>`` with an image or PDF extension
- ``people/<person-id>/attachments/<any path>``
- ``shared/<any path>``

A match starts at a word boundary and ends at the extension or at the end of
the last segment. A path inside a fenced code block, an inline code span, a
Markdown link, an HTML anchor, or a URL is left alone. A line that starts
with "Not verified:" follows the same rules as any other line.

A voice turn, and any text that goes to text to speech, never reaches this
filter. ``core.path_links: false`` turns it off everywhere.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# A person id is a slug, as everywhere else.
_PERSON = r"[a-z0-9][a-z0-9-]{0,31}"
# One or more path segments. A segment holds no space and no bracket, so a
# match never runs past the path into the sentence around it.
_SEGMENTS = r"[A-Za-z0-9._~-]+(?:/[A-Za-z0-9._~-]+)*"
# The extensions of a picture or a PDF under ``wiki/attachments/``.
_MEDIA_EXT = r"png|jpe?g|gif|webp|svg|bmp|tiff?|heic|pdf"

# The shapes of a path that becomes a link. Nothing else does.
PATH_RE = re.compile(
    r"(?<![A-Za-z0-9._~/\\:-])"
    r"(?:"
    rf"documents/(?:{_PERSON}|shared)/[A-Za-z0-9._~-]+\.(?:md|html)"
    rf"|wiki/attachments/{_SEGMENTS}\.(?:{_MEDIA_EXT})"
    rf"|wiki/{_SEGMENTS}\.md"
    rf"|people/{_PERSON}/attachments/{_SEGMENTS}"
    rf"|shared/{_SEGMENTS}"
    r")"
)

# The regions a match never starts in: an inline code span, a Markdown link
# or image, an HTML anchor, and a URL. A fenced code block is read line by
# line, in ``_fenced``.
_SKIP_RE = re.compile(
    r"(?P<code>`+[^`\n]*`+)"
    r"|(?P<link>!?\[[^\]\n]*\]\([^)\n]*\))"
    r"|(?P<anchor><a\b[^>]*>.*?</a\s*>|<a\b[^>]*>)"
    r"|(?P<url>[A-Za-z][A-Za-z0-9+.-]*://[^\s<>()\[\]\"'`]+)",
    re.DOTALL,
)

# The opener and the closer of a fenced code block.
_FENCE_RE = re.compile(r"^[ \t]*(?:```|~~~)")

# Punctuation a sentence puts after a path, which is not part of it.
_TRAILING = ".,;:!?'\")]}>"


@dataclass(frozen=True)
class PathLink:
    """One path of the text, and where it opens."""

    path: str
    url: str
    start: int
    end: int


def viewer_url(path: str, base_url: str | None = None) -> str:
    """The address of the viewer for one Joshua path.

    A document opens on its own page, which names the owner and the slug and
    carries no extension. Every other shape opens at its path. With
    ``base_url`` (``workspaces.viewer_url``, the public address of the viewer)
    the address is absolute, which a channel outside the viewer needs.
    """
    parts = path.split("/")
    if parts[0] == "documents" and len(parts) == 3:
        slug = parts[2].rsplit(".", 1)[0]
        route = f"/documents/{parts[1]}/{slug}"
    else:
        route = f"/{path}"
    return f"{base_url.rstrip('/')}{route}" if base_url else route


def _fenced(text: str) -> list[tuple[int, int]]:
    """The spans of the fenced code blocks of ``text``.

    A fence that is never closed holds every line after it, so a text that
    ends inside a block links nothing in it.
    """
    spans: list[tuple[int, int]] = []
    offset = 0
    open_at: int | None = None
    for line in text.splitlines(keepends=True):
        end = offset + len(line)
        if _FENCE_RE.match(line):
            if open_at is None:
                open_at = offset
            else:
                spans.append((open_at, end))
                open_at = None
        offset = end
    if open_at is not None:
        spans.append((open_at, len(text)))
    return spans


def _skipped(text: str) -> list[tuple[int, int]]:
    """The spans of the text a path is never matched in."""
    return _fenced(text) + [(m.start(), m.end()) for m in _SKIP_RE.finditer(text)]


def find_paths(text: str, base_url: str | None = None) -> list[PathLink]:
    """Every Joshua path of ``text`` that becomes a link, in order."""
    skipped = _skipped(text)
    links: list[PathLink] = []
    for match in PATH_RE.finditer(text):
        start, end = match.start(), match.end()
        if any(s < end and start < e for s, e in skipped):
            continue
        path = match.group(0).rstrip(_TRAILING)
        if path.endswith("/") or "/" not in path:
            continue
        links.append(PathLink(path, viewer_url(path, base_url), start, start + len(path)))
    return links


def link_paths(text: str, base_url: str | None = None) -> str:
    """``text`` with every Joshua path written as a Markdown link.

    The path stays the visible text. A text with no path comes back unchanged.
    """
    links = find_paths(text, base_url)
    if not links:
        return text
    out: list[str] = []
    cursor = 0
    for link in links:
        out.append(text[cursor : link.start])
        out.append(f"[{link.path}]({link.url})")
        cursor = link.end
    out.append(text[cursor:])
    return "".join(out)
