"""Markdown → retrieval chunks.

Splits on h1–h3 headings so a chunk stays a coherent section, then splits
oversized sections on paragraph boundaries. Each chunk carries a breadcrumb
("Title > Heading > Subheading") that is prepended to the text at EMBED time
(embed_text) but stored separately — the breadcrumb is what makes a small
embedding model place "Guest Network" chunks near "guest wifi" queries even
when the chunk body never says "wifi". Chunking quality is the biggest
retrieval lever in this system; tune here before touching models or floors.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# ~400 tokens for typical English markdown. Sections up to this size stay whole.
DEFAULT_MAX_CHARS = 1600
# Sections shorter than this get merged into the previous chunk's tail rather
# than becoming a near-empty chunk of their own (e.g. a heading with one line).
MIN_CHARS = 80

_HEADING = re.compile(r"^(#{1,3})\s+(.*?)\s*#*\s*$")


@dataclass
class Chunk:
    heading: str  # breadcrumb below the title, e.g. "Networking > VLANs"
    text: str

    def embed_text(self, title: str) -> str:
        """What actually gets embedded: breadcrumb context + body."""
        crumb = " > ".join(p for p in (title, self.heading) if p)
        return f"{crumb}\n{self.text}" if crumb else self.text


def _split_paragraphs(text: str, max_chars: int) -> list[str]:
    """Greedily pack blank-line-separated paragraphs up to max_chars. A single
    paragraph longer than max_chars is emitted whole — never split mid-thought."""
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    out: list[str] = []
    buf = ""
    for p in paras:
        if buf and len(buf) + 2 + len(p) > max_chars:
            out.append(buf)
            buf = p
        else:
            buf = f"{buf}\n\n{p}" if buf else p
    if buf:
        out.append(buf)
    return out


def chunk_markdown(text: str, max_chars: int = DEFAULT_MAX_CHARS) -> list[Chunk]:
    """Split a markdown document into heading-scoped chunks.

    Heading levels 1–3 update a breadcrumb stack; deeper headings stay inline in
    the body (h4+ is usually list-like structure, not topical). Fenced code
    blocks are opaque — a ``` line toggles fence state and headings inside are
    body text.
    """
    lines = (text or "").splitlines()
    crumbs: list[str] = ["", "", ""]  # h1, h2, h3
    sections: list[tuple[str, list[str]]] = [("", [])]
    in_fence = False

    for line in lines:
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            sections[-1][1].append(line)
            continue
        m = None if in_fence else _HEADING.match(line)
        if not m:
            sections[-1][1].append(line)
            continue
        level = len(m.group(1))
        crumbs[level - 1] = m.group(2).strip()
        for i in range(level, 3):
            crumbs[i] = ""
        breadcrumb = " > ".join(c for c in crumbs if c)
        sections.append((breadcrumb, []))

    chunks: list[Chunk] = []
    carry = ""  # a tiny leading section held until a real chunk exists to fold
    # into — an undersized *leading* section (e.g. a nav breadcrumb before the
    # first heading) must never become a solo chunk.
    for heading, body_lines in sections:
        body = "\n".join(body_lines).strip()
        if not body:
            continue
        if len(body) < MIN_CHARS:
            snippet = f"## {heading}\n{body}" if heading else body
            if chunks:
                # Tiny section — fold into the previous chunk to avoid noise rows.
                prev = chunks[-1]
                chunks[-1] = Chunk(prev.heading, f"{prev.text}\n\n{snippet}")
            else:
                # No previous chunk yet — defer into the next one rather than
                # emitting the undersized leading section on its own.
                carry = f"{carry}\n\n{snippet}" if carry else snippet
            continue
        pieces = _split_paragraphs(body, max_chars)
        if carry:
            pieces[0] = f"{carry}\n\n{pieces[0]}"
            carry = ""
        for piece in pieces:
            chunks.append(Chunk(heading, piece))

    if carry:
        # Whole document was undersized — emit rather than drop its content.
        chunks.append(Chunk("", carry))
    return chunks
