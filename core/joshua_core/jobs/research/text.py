"""Text helpers of the researcher: the parts of a bundle, the sentence of a
phrase, the links a worker may keep, and one-line text for a table or a list.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Sequence
from dataclasses import dataclass

from joshua_shared.attachments import clean_text

from joshua_core.engine.url_grants import canonical
from joshua_core.jobs.bundle import Bundle

# `[text](target)` and `![text](target)`. The target stops at a space or `)`.
_MD_LINK_RE = re.compile(r"(?P<image>!?)\[(?P<text>[^\]\n]*)\]\((?P<target>[^)\s]*)(?:\s+[^)]*)?\)")
# `[[target]]` and `[[target|label]]`.
_WIKILINK_RE = re.compile(r"\[\[(?P<target>[^\]|\n]+)(?:\|(?P<label>[^\]\n]*))?\]\]")
# `<https://...>`.
_AUTOLINK_RE = re.compile(r"<(?P<url>[a-zA-Z][a-zA-Z0-9+.-]*:[^>\s]*)>")
# A sentence ends at `.`, `!`, or `?` before a space.
_SENTENCE_END_RE = re.compile(r"[.!?](?=\s)")
# A line that starts a new block: a list item or a heading.
_BLOCK_START_RE = re.compile(r"^\s*(?:[-*+]\s|\d+[.)]\s|#{1,6}\s)")
_MAX_SENTENCE = 400


def collapse(text: str) -> str:
    """`text` with each run of white space as one space, in lower case, for
    a verbatim match that does not care about case or line breaks."""
    return " ".join(text.split()).casefold()


def inline(text: str, limit: int = 1000) -> str:
    """`text` cleaned and on one line."""
    return " ".join(clean_text(str(text), limit=limit * 4).split())[:limit]


def strip_links(text: str, allowed: Collection[str]) -> str:
    """`text` with every link removed, except a Markdown link to a URL in
    `allowed` (canonical form). A removed link leaves its text. A picture, a
    wikilink, and an autolink are always removed, so a worker cannot plant a
    link that the curator follows."""

    def md(match: re.Match[str]) -> str:
        label = match.group("text")
        target = match.group("target")
        if not match.group("image") and target and canonical(target) in allowed:
            return match.group(0)
        return label

    def wiki(match: re.Match[str]) -> str:
        return match.group("label") or match.group("target")

    text = _MD_LINK_RE.sub(md, text)
    text = _WIKILINK_RE.sub(wiki, text)
    return _AUTOLINK_RE.sub(lambda m: m.group("url"), text)


def allowed_urls(urls: Sequence[str]) -> set[str]:
    """The canonical form of each of `urls`, for `strip_links`."""
    return {key for key in (canonical(u) for u in urls) if key}


# -- the bundle ---------------------------------------------------------------


@dataclass(frozen=True)
class Part:
    """One page of a bundle: its path relative to the wiki root, and its text."""

    path: str
    text: str


def bundle_parts(bundle: Bundle) -> list[Part]:
    """The pages of a bundle, in order, each with the text of its part.

    The parts of the bundle text start with `## <path>` in the order of
    `bundle.paths`, so each part is the text from its heading line to the
    heading line of the next.
    """
    text = bundle.text
    starts: list[tuple[str, int]] = []
    cursor = 0
    for path in bundle.paths:
        marker = f"## {path}\n"
        at = text.find(marker, cursor)
        if at < 0:
            continue
        starts.append((path, at + len(marker)))
        cursor = at + len(marker)
    parts: list[Part] = []
    for index, (path, begin) in enumerate(starts):
        end = len(text)
        if index + 1 < len(starts):
            next_path = starts[index + 1][0]
            end = text.rfind(f"## {next_path}\n", begin, starts[index + 1][1])
        parts.append(Part(path, text[begin:end].strip()))
    return parts


def _blocks(text: str) -> list[str]:
    """The blocks of a text: paragraphs, list items, and headings, each on one line."""
    blocks: list[list[str]] = []
    current: list[str] = []
    for line in text.splitlines():
        if not line.strip() or _BLOCK_START_RE.match(line):
            if current:
                blocks.append(current)
            current = [line] if line.strip() else []
            continue
        current.append(line)
    if current:
        blocks.append(current)
    return [" ".join(" ".join(b).split()) for b in blocks]


def _sentence(block: str, start: int, end: int) -> str:
    """The sentence of `block` that holds `block[start:end]`."""
    left = 0
    for match in _SENTENCE_END_RE.finditer(block, 0, start):
        left = match.end()
    right = len(block)
    found = _SENTENCE_END_RE.search(block, end)
    if found is not None:
        right = found.end()
    sentence = block[left:right].strip()
    if len(sentence) > _MAX_SENTENCE:
        middle = (start + end) // 2 - left
        half = _MAX_SENTENCE // 2
        lo = max(0, middle - half)
        sentence = sentence[lo : lo + _MAX_SENTENCE].strip()
    return sentence


@dataclass(frozen=True)
class Grounding:
    """Where a phrase occurs in a bundle: the path of the part and the sentence."""

    phrase: str
    path: str
    sentence: str


def find_phrase(phrase: str, parts: Sequence[Part]) -> Grounding | None:
    """The first part that holds `phrase` verbatim (case and white space aside),
    with the sentence around it, or None."""
    needle = collapse(phrase)
    if not needle:
        return None
    for part in parts:
        if needle not in collapse(part.text):
            continue
        for block in _blocks(part.text):
            at = block.casefold().find(needle)
            if at >= 0:
                return Grounding(phrase, part.path, _sentence(block, at, at + len(needle)))
        return Grounding(phrase, part.path, inline(phrase))
    return None


def extract_sentences(phrases: Sequence[str], parts: Sequence[Part]) -> list[Grounding]:
    """For each phrase that occurs in the bundle, its part and its sentence, in
    the order of `phrases`. A phrase that does not occur is left out."""
    out: list[Grounding] = []
    for phrase in phrases:
        found = find_phrase(phrase, parts)
        if found is not None:
            out.append(found)
    return out


def code_span(text: object, limit: int = 300) -> str:
    """`text`, on one line and short, as one code span, so that a link in it is
    not a link of the report that shows it."""
    one = inline(str(text), limit + 1)
    if len(one) > limit:
        one = one[: limit - 1] + "…"
    return "`" + one.replace("`", "'") + "`"


def fenced(text: str) -> str:
    """`text` in a fenced code block whose fence no line of the text can close."""
    longest = 0
    run = 0
    for ch in text:
        run = run + 1 if ch == "`" else 0
        longest = max(longest, run)
    fence = "`" * max(3, longest + 1)
    return f"{fence}markdown\n{text.rstrip()}\n{fence}"
