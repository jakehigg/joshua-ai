"""The shared renderer of the viewer: Markdown as blocks, and a report as a showcase.

Two sections of the viewer show long Markdown that a person reads: the plan
and the report of an assignment, and a document. Both use this one renderer.

:func:`blocks_html` renders Markdown as a document of blocks, one element a
paragraph, heading, list item, quote, code block, or table, as
``viewer_blocks`` cuts it. With a :class:`viewer_comments.Gutter`, each block
is a unit with its own "+" and the comments on its lines.

A text with the headings of the report shape (``viewer_report``) shows as a
showcase: the highlights as cards, the decisions as numbered cards, the
outputs as cards by kind, what was asked as a quote, what was done as prose,
and the follow-ups as a checklist. With a gutter, each card, each heading, and
each block of prose is a unit, anchored to its line of the text. The hero and
the numbers at the top differ between an assignment and a document, so each
section builds its own from :func:`tile` and :func:`tiles`.

On the report of an assignment, each decision card takes an answer: a plain
form with a textarea and "Send to Joshua", which stores a comment on the
first line of the decision. The card then shows the answer, or the
resolution of Joshua. On a document, one small script, :data:`COPY_SCRIPT`,
shows the copy button of each decision. The policy of the viewer allows that
script by its hash and no other. With no script, the buttons stay hidden and
the page is the same.

Every string is untrusted and escaped. Raw HTML in Markdown is off, so a
``<script>`` renders as text. A reference is a link only when it is an
``http`` or ``https`` URL with a host, or a path of a document.
"""

from __future__ import annotations

import base64
import hashlib
import re
from collections.abc import Sequence
from dataclasses import dataclass
from html import escape
from typing import Any
from urllib.parse import urlparse

from joshua_gateway import viewer_blocks as vb
from joshua_gateway import viewer_docstore as ds
from joshua_gateway import viewer_report as rp
from joshua_gateway.viewer_comments import Comment, Gutter, applied_html, time_html

# The wiki renderer: raw HTML is off, and markdown-it refuses a
# ``javascript:`` link.
MD = vb.MD


def is_url(ref: str) -> bool:
    """True for an ``http`` or ``https`` URL with a host."""
    try:
        parsed = urlparse(ref.strip())
    except ValueError:
        return False
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def markdown(text: str) -> str:
    """Markdown as the wiki renders it."""
    return MD.render(text)


def inline(text: str) -> str:
    """Inline Markdown, escaped, as the wiki renders it."""
    return MD.renderInline(text)


def count(n: int, noun: str) -> str:
    return f"{n} {noun}" + ("" if n == 1 else "s")


def svg(paths: str, css: str = "j-icon") -> str:
    return f'<svg class="{css}" viewBox="0 0 24 24" aria-hidden="true">{paths}</svg>'


# -- blocks -----------------------------------------------------------------

# A task box of rendered Markdown: ``<li>[x] text`` or, in a loose list,
# ``<li><p>[x] text``. Raw HTML is off, so the box came from the text.
_RENDERED_BOX = re.compile(r"<li>(<p>)?\[([ xX])\] ")

# What the "+" of a block names.
BLOCK_NAMES = {
    "p": "paragraph",
    "h": "heading",
    "li": "list item",
    "quote": "quote",
    "code": "code block",
    "table": "table",
}


def boxes(html: str) -> str:
    """Draw each task box of rendered Markdown as a box."""

    def box(match: re.Match[str]) -> str:
        done = match.group(2) in "xX"
        css, label = ("a-task done", "done") if done else ("a-task", "to do")
        return (
            f'<li class="{css}">{match.group(1) or ""}'
            f'<span class="a-box" role="img" aria-label="{label}"></span>'
        )

    return _RENDERED_BOX.sub(box, html)


def blocks_html(
    text: str, *, start: int = 1, gutter: Gutter | None = None, draw_boxes: bool = False
) -> str:
    """Markdown as a document of blocks. ``start`` is the line of the text
    where line 1 of ``text`` is. With ``gutter``, each block is a unit with
    its "+" and its comments. With ``draw_boxes``, each task box is a box."""
    parts = []
    for block in vb.blocks(text):
        html = boxes(block.html) if draw_boxes else block.html
        css = f"a-block {block.kind} d{min(block.depth, vb.MAX_DEPTH)}"
        if gutter is None:
            parts.append(f'<div class="{css}">{html}</div>')
            continue
        first = start + block.first - 1
        what = f"the {BLOCK_NAMES.get(block.kind, 'block')} at line {first + gutter.offset}"
        parts.append(gutter.unit(first, start + block.last - 1, html, css, what))
    return "".join(parts)


def doc_html(text: str, *, draw_boxes: bool = False) -> str:
    """Markdown as a document of blocks, with no "+"."""
    return blocks_html(text, draw_boxes=draw_boxes)


def plan_html(text: str, gutter: Gutter | None = None) -> str:
    """The plan as a document of blocks, with each task box drawn as a box.
    With ``gutter``, each block has its "+" and its comments."""
    return blocks_html(text, gutter=gutter, draw_boxes=True)


def article(html: str) -> str:
    return f'<article class="j-prose a-blocks">{html}</article>'


# -- the frame of a section -------------------------------------------------


def section(
    title: str,
    body: str,
    *,
    stamp: str = "",
    badge: str = "",
    css: str = "a-section",
    head: str = "",
    head_id: str = "",
    sid: str = "",
) -> str:
    """A section with its heading. ``badge`` is a count beside the heading,
    ``head`` more HTML in the heading row (the "+" of the heading),
    ``head_id`` the anchor of the heading, and ``sid`` the id of the section."""
    when = f'<span class="j-meta">updated {stamp}</span>' if stamp else ""
    count_html = f'<span class="a-count">{escape(badge)}</span>' if badge else ""
    anchor = f' id="{escape(head_id)}"' if head_id else ""
    section_id = f' id="{escape(sid)}"' if sid else ""
    return (
        f'<section class="{css}"{section_id}><div class="a-head"{anchor}><h2>{escape(title)}</h2>'
        f"{count_html}{when}{head}</div>{body}</section>"
    )


def _heading_unit(gutter: Gutter | None, part: rp.Section) -> tuple[str, str, str]:
    """The "+" of the heading of a section, its anchor, and the cards of the
    lines between the heading and the body."""
    if gutter is None or not part.line:
        return "", "", ""
    anchors, plus, cards = gutter.mark(part.line, max(part.line, part.start - 1), "the heading")
    return plus + anchors, gutter.unit_id(part.line), cards


def fold(
    title: str,
    body: str,
    *,
    hint: str = "",
    css: str = "a-fold",
    open_: bool = False,
    fid: str = "",
) -> str:
    """A ``<details>`` with ``title`` and a short hint in its summary, closed
    unless ``open_``. ``fid`` is its id."""
    opened = " open" if open_ else ""
    ident = f' id="{escape(fid)}"' if fid else ""
    return (
        f'<details class="{css}"{opened}{ident}><summary>'
        f'<span class="a-fold-title">{escape(title)}</span>'
        f"{hint}</summary>"
        f'<div class="a-fold-body">{body}</div></details>'
    )


def tile(value: str, label: str, *, title: str = "", css: str = "") -> str:
    """One number with its label. ``value`` is HTML."""
    hint = f' title="{escape(title)}"' if title else ""
    cls = f"j-stat {css}".strip()
    return f'<div class="{cls}"{hint}><b>{value}</b><span>{escape(label)}</span></div>'


def tiles(parts: list[str], *, label: str) -> str:
    return f'<div class="j-stats a-stats" role="group" aria-label="{label}">{"".join(parts)}</div>'


# -- the cards of a list ----------------------------------------------------


def _item_ranges(part: rp.Section, items: Sequence[Any]) -> list[tuple[int, int]]:
    """The lines of each item of a section, as units: an item goes on to the
    line before the next item, and the last one to the end of the section."""
    firsts = [part.start + item.line for item in items]
    out = []
    for n, first in enumerate(firsts):
        last = firsts[n + 1] - 1 if n + 1 < len(firsts) else part.end
        out.append((first, max(first, last)))
    return out


def _card(
    gutter: Gutter | None, lines: tuple[int, int] | None, css: str, body: str, what: str
) -> str:
    """One ``<li>`` card. With a gutter, the card is a unit."""
    if gutter is None or lines is None or not lines[0]:
        return f'<li class="{css}">{body}</li>' if css else f"<li>{body}</li>"
    anchors, plus, cards = gutter.mark(lines[0], lines[1], what)
    cls = f"{css} a-card-unit".strip() + (" has-comments" if cards else "")
    return f'<li class="{cls}" id="{gutter.unit_id(lines[0])}">{anchors}{body}{plus}{cards}</li>'


def highlights_html(part: rp.Section | None, gutter: Gutter | None = None) -> str:
    """The highlights as cards: the bold lead large, the sentence under it."""
    if part is None or part.empty:
        return ""
    items = part.items
    ranges = _item_ranges(part, items) if gutter is not None and part.line else None
    # The heading is not shown, so a comment on it shows above the cards.
    gutter_head = gutter.take(part.line, ranges[0][0] - 1) if gutter and ranges else ""
    cards = []
    for n, entry in enumerate(items):
        if entry.lead:
            text = f'<p class="a-hl-text">{inline(entry.body)}</p>' if entry.body else ""
            body = f'<strong class="a-hl-lead">{inline(entry.lead)}</strong>{text}'
        else:
            body = f'<p class="a-hl-text">{inline(entry.text)}</p>'
        cards.append(_card(gutter, ranges[n] if ranges else None, "a-hl", body, "the highlight"))
    grid = f'<ul class="a-cards a-hls">{"".join(cards)}</ul>' if cards else ""
    rest = f'<div class="j-prose">{markdown(part.rest)}</div>' if part.rest else ""
    return (
        f'<section class="a-highlights" aria-label="Highlights">{gutter_head}{grid}{rest}</section>'
    )


def decision_items(part: rp.Section | None) -> list[rp.Item]:
    if part is None or part.empty:
        return []
    return [entry for entry in part.items if not rp.is_none(entry.text)]


def decision_ranges(part: rp.Section | None) -> list[tuple[int, int]]:
    """The lines of the text that each decision of ``part`` holds, from 1."""
    items = decision_items(part)
    if part is None or not part.line or not items:
        return []
    return _item_ranges(part, items)


# The words on the answer of a decision.
SENT = "Sent to Joshua"
SEND = "Send to Joshua"


def _answer_html(gutter: Gutter, open_: list[Comment], done: list[Comment]) -> str:
    """The answers of the person to one decision: each open one with "Sent to
    Joshua", its time, and Delete; each resolved one with the resolution of
    Joshua."""
    forms = gutter.forms
    parts = []
    for comment in done:
        when = time_html(comment.resolved_at, tz=gutter.tz, now=gutter.now)
        parts.append(
            f'<div class="a-answer resolved" id="C{escape(str(comment.id))}">'
            f'<p class="a-answer-body">{escape(comment.body)}</p>'
            f'<p class="a-resolution"><span class="j-label">Joshua</span> '
            f"{escape(comment.resolution or '')}"
            f' <span class="j-faint">{when}</span></p>{applied_html(comment)}</div>'
        )
    for comment in open_:
        when = time_html(comment.created_at, tz=gutter.tz, now=gutter.now)
        delete = ""
        if forms is not None and gutter.can_delete(comment):
            delete = (
                f'<form class="a-del" method="post" '
                f'action="{escape(forms.action)}/{escape(str(comment.id))}/delete">'
                f'<input type="hidden" name="csrf" value="{escape(forms.csrf)}">'
                f'<input type="hidden" name="target" value="{escape(forms.target)}">'
                '<button class="j-btn sm" type="submit">Delete</button></form>'
            )
        parts.append(
            f'<div class="a-answer" id="C{escape(str(comment.id))}">'
            f'<p class="a-answer-body">{escape(comment.body)}</p>'
            f'<div class="a-comment-foot"><span class="j-badge ok">{SENT}</span>'
            f'<span class="j-faint">{when}</span>{delete}</div></div>'
        )
    return "".join(parts)


def _answer_form(gutter: Gutter, first: int, number: int) -> str:
    """The form of an answer to decision ``number``, on line ``first``."""
    forms = gutter.forms
    if forms is None:
        return ""
    n = first + gutter.offset
    label = f"Your answer to decision {number}"
    return (
        f'<form class="a-answer-form" method="post" action="{escape(forms.action)}">'
        f'<input type="hidden" name="csrf" value="{escape(forms.csrf)}">'
        f'<input type="hidden" name="target" value="{escape(forms.target)}">'
        f'<input type="hidden" name="line_no" value="{int(n)}">'
        f'<input type="hidden" name="line_text" value="{escape(gutter.text(n))}">'
        f'<div class="j-field"><textarea name="body" rows="3" maxlength="2000" required '
        f'aria-label="{escape(label)}" placeholder="Your answer"></textarea></div>'
        f'<button class="j-btn sm primary" type="submit">{SEND}</button></form>'
    )


def decisions_html(
    copy_prefix: str,
    part: rp.Section | None,
    gutter: Gutter | None = None,
    *,
    answers: bool = False,
) -> str:
    """The decisions as numbered cards.

    With ``answers`` and a gutter, each card takes an answer: the answers of
    the person on its lines, and the form while no answer is open. With
    ``answers`` and no gutter, a card has the question only. Else each
    card has a copy button for Joshua, with the text "<copy_prefix>, decision
    <n>: <question>". That button is hidden; :data:`COPY_SCRIPT` shows it when
    the browser can copy, so the page works the same with no script."""
    items = decision_items(part)
    if part is None or (not items and part.empty):
        return ""
    ranges = _item_ranges(part, items) if gutter is not None and part.line else None
    answering = answers and gutter is not None and ranges is not None
    cards = []
    for number, entry in enumerate(items, start=1):
        question, context = rp.decision_question(entry)
        ctx = f'<div class="j-prose a-ctx">{markdown(context)}</div>' if context else ""
        lines = ranges[number - 1] if ranges else None
        if answering:
            assert gutter is not None and lines is not None
            open_, done = gutter.answers(*lines)
            form = "" if open_ else _answer_form(gutter, lines[0], number)
            foot = f'<div class="a-decision-answer">{_answer_html(gutter, open_, done)}{form}</div>'
        elif answers:
            foot = ""
        else:
            copy = f"{copy_prefix}, decision {number}: {rp.plain(question)}"
            foot = (
                '<div class="a-decision-foot"><span class="j-faint">Tell Joshua your answer.</span>'
                f'<button type="button" class="j-btn sm a-copy" data-copy="{escape(copy)}" hidden>'
                "Copy</button></div>"
            )
        body = (
            f'<span class="a-n" aria-hidden="true">{number}</span>'
            f'<div class="a-decision-main"><p class="a-q">{inline(question)}</p>{ctx}{foot}</div>'
        )
        if answering:
            assert gutter is not None and lines is not None
            answered = " answered" if gutter_answered(gutter, lines) else ""
            cards.append(
                f'<li class="a-decision{answered}" id="{gutter.unit_id(lines[0])}">'
                f"{gutter.anchors(*lines)}{body}</li>"
            )
        else:
            cards.append(_card(gutter, lines, "a-decision", body, f"decision {number}"))
    body = f'<ol class="a-decisions">{"".join(cards)}</ol>' if cards else ""
    if part.rest and not rp.is_none(part.rest):
        body += f'<div class="j-prose">{markdown(part.rest)}</div>'
    head, head_id, head_cards = _heading_unit(gutter, part)
    badge = count(len(items), "decision") if items else ""
    return section(
        "Decisions for you",
        head_cards + body,
        badge=badge,
        css="a-section a-decide",
        head=head,
        head_id=head_id,
        sid="decisions" if answers else "",
    )


def gutter_answered(gutter: Gutter, lines: tuple[int, int]) -> bool:
    """True when a comment of the gutter, open or resolved, is on ``lines``."""
    lo, hi = lines[0] + gutter.offset, lines[1] + gutter.offset
    return any(lo <= c.line_no <= hi for c in gutter.comments)


def decisions_waiting(part: rp.Section | None, gutter: Gutter) -> int:
    """The decisions of ``part`` that have no answer in ``gutter``."""
    return sum(1 for lines in decision_ranges(part) if not gutter_answered(gutter, lines))


# -- outputs ----------------------------------------------------------------

# The icon of each kind of output: name -> SVG path data on a 24px grid. A
# kind matches the first name whose words it holds; any other kind is
# "other".
KIND_ICONS: dict[str, str] = {
    "task": '<rect x="4" y="4" width="16" height="16" rx="3"/><path d="m8.5 12 2.5 2.5 4.5-5"/>',
    "page": '<path d="M7 3h7l5 5v13H7a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2z"/>'
    '<path d="M14 3v5h5M9 13h7M9 17h5"/>',
    "issue": '<circle cx="12" cy="12" r="8.5"/><circle cx="12" cy="12" r="1.6"/>',
    "merge request": '<circle cx="6" cy="5" r="2.2"/><circle cx="6" cy="19" r="2.2"/>'
    '<circle cx="18" cy="13" r="2.2"/><path d="M6 7.2v9.6M6 7.5c0 4 4 5.5 9.8 5.5"/>',
    "journal": '<path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20V3H6.5A2.5 2.5 0 0 0 4 5.5z"/>'
    '<path d="M4 19.5A2.5 2.5 0 0 0 6.5 22H20v-5"/>',
    "file": '<path d="M13 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V9z"/>'
    '<path d="M13 3v6h6"/>',
    "other": '<path d="m12 3 2.6 5.6 6.1.7-4.5 4.2 1.2 6L12 16.6 6.6 19.5l1.2-6L3.3 9.3l6.1-.7z"/>',
}
# The words of a kind that name each icon, in the order they are tried.
_KIND_WORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("merge request", ("merge request", "pull request", "merge", "mr", "pr")),
    ("issue", ("issue", "bug", "ticket")),
    ("task", ("task", "todo", "to-do")),
    ("journal", ("journal",)),
    ("page", ("page", "wiki", "doc")),
    ("file", ("file", "attachment")),
)
_SHORT_WORD = re.compile(r"[a-z]+")


def kind_icon(kind: str) -> str:
    """The icon name of a kind of output: a fixed name of :data:`KIND_ICONS`.

    A long word matches anywhere in the kind; ``mr`` and ``pr`` match only as
    a whole word, so "summary" is not a merge request."""
    text = kind.lower()
    words = set(_SHORT_WORD.findall(text))
    for name, needles in _KIND_WORDS:
        for needle in needles:
            if (needle in words) if len(needle) <= 2 else (needle in text):
                return name
    return "other"


def _ref_key(ref: str) -> str:
    return ref.strip().strip("<>").rstrip("/")


@dataclass(frozen=True)
class Card:
    """One output card: the output and the note from the report. ``line`` is
    the line of the item in the Outputs section, from 0, when the card came
    from the text."""

    kind: str
    ref: str
    title: str
    note: str = ""
    line: int | None = None


def output_cards(outputs: Sequence[Any], report: rp.Report | None) -> list[Card]:
    """The output cards. The recorded outputs (each with ``kind``, ``ref``,
    and ``title``) are the cards; a line of the Outputs section of the report
    gives the note of the card with the same ``ref``. With no recorded output,
    the lines of the section are the cards."""
    part = report.get(rp.OUTPUTS) if report is not None else None
    lines = rp.output_lines(part.body) if part is not None else []
    if outputs:
        notes = {_ref_key(line.ref): line.note for line in lines if line.ref and line.note}
        return [Card(o.kind, o.ref, o.title, notes.get(_ref_key(o.ref), "")) for o in outputs]
    return [Card(line.kind, line.ref, line.title, line.note, line.line) for line in lines]


def output_card_body(card: Card) -> str:
    """The inside of one output card: the icon of its kind, the title (a link
    when ``ref`` is an ``http`` or ``https`` URL, or a document of kind
    ``document``), any other ``ref`` as code, and the note."""
    icon = svg(KIND_ICONS[kind_icon(card.kind)], "j-icon a-out-icon")
    ref = card.ref.strip()
    title = card.title or ref
    document = ds.document_url(ref) if card.kind.strip().lower() == "document" else None
    if ref and is_url(ref):
        host = urlparse(ref).netloc
        head = (
            f'<a class="a-out-title" href="{escape(ref)}" rel="noreferrer">{escape(title)}</a>'
            f'<span class="a-out-ref">{escape(host)}</span>'
        )
    elif document is not None:
        head = (
            f'<a class="a-out-title" href="{escape(document)}">{escape(title)}</a>'
            f'<code class="a-out-ref">{escape(ref)}</code>'
        )
    elif card.title:
        code = f'<code class="a-out-ref">{escape(ref)}</code>' if ref else ""
        head = f'<span class="a-out-title">{escape(card.title)}</span>{code}'
    elif ref:
        head = f'<code class="a-out-title">{escape(ref)}</code>'
    else:
        head = ""
    note = f'<p class="a-out-note">{inline(card.note)}</p>' if card.note else ""
    return f'{icon}<div class="a-out-main">{head}{note}</div>'


def card_href(card: Card) -> str | None:
    """Where the Open link of an output goes: an ``http`` or ``https`` URL, the
    viewer page of a document of kind ``document``, or None."""
    ref = card.ref.strip()
    if ref and is_url(ref):
        return ref
    if card.kind.strip().lower() == "document":
        return ds.document_url(ref)
    return None


def lead_card(card: Card) -> str:
    """One output in the lead band: the icon, the title, the kind, the note,
    and an Open link when the output has a place to open."""
    icon = svg(KIND_ICONS[kind_icon(card.kind)], "j-icon a-out-icon")
    ref = card.ref.strip()
    href = card_href(card)
    title = escape(card.title or ref)
    kind = escape(card.kind.strip() or "other")
    code = f'<code class="a-out-ref">{escape(ref)}</code>' if ref and href is None else ""
    note = f'<p class="a-out-note">{inline(card.note)}</p>' if card.note else ""
    link = ""
    if href is not None:
        rel = ' rel="noreferrer"' if is_url(href) else ""
        link = f'<a class="j-btn sm a-open" href="{escape(href)}"{rel}>Open</a>'
    return (
        f'<li class="a-out a-lead-out">{icon}<div class="a-out-main">'
        f'<span class="a-out-title">{title}</span><span class="a-out-ref">{kind}</span>'
        f"{code}{note}</div>{link}</li>"
    )


def lead_html(cards: list[Card], *, waiting: int, decisions: int, extra: str = "") -> str:
    """The band at the top of a completed assignment: the decisions that wait
    for the person, with a link to them, and the outputs as cards, the
    documents first. ``extra`` is more HTML in the band (the comments on the
    Outputs section of the report). "" when there is nothing to show."""
    parts = []
    if waiting:
        parts.append(
            '<a class="a-lead-decisions" href="#decisions">'
            f"<b>{waiting}</b> {'decision waits' if waiting == 1 else 'decisions wait'} for you"
            "</a>"
        )
    elif decisions:
        parts.append(
            '<a class="a-lead-decisions done" href="#decisions">'
            f"{'The decision has' if decisions == 1 else f'All {decisions} decisions have'} "
            "an answer</a>"
        )
    if cards:
        ordered = sorted(cards, key=lambda c: c.kind.strip().lower() != "document")
        rows = "".join(lead_card(card) for card in ordered)
        parts.append(
            f'<div class="a-lead-outputs"><h2 class="j-label">{count(len(cards), "output")}</h2>'
            f'<ul class="a-cards a-outs">{rows}</ul>{extra}</div>'
        )
    elif extra:
        parts.append(extra)
    if not parts:
        return ""
    return (
        f'<section class="a-lead" id="lead" aria-label="What waits for you">{"".join(parts)}'
        "</section>"
    )


def output_card(card: Card) -> str:
    """One output card, with no "+"."""
    return f'<li class="a-out">{output_card_body(card)}</li>'


def outputs_html(
    cards: list[Card],
    *,
    title: str = "Outputs",
    part: rp.Section | None = None,
    gutter: Gutter | None = None,
) -> str:
    """The output cards grouped by kind, in the order each kind first comes.
    With ``gutter`` and the Outputs section ``part``, each card from the text
    is a unit."""
    if not cards:
        return ""
    ranges: dict[int, tuple[int, int]] = {}
    if gutter is not None and part is not None and part.line:
        placed = [c for c in cards if c.line is not None]
        spans = _item_ranges(part, [rp.Item("", line=c.line or 0) for c in placed])
        ranges = {id(c): span for c, span in zip(placed, spans, strict=True)}
    groups: dict[str, list[Card]] = {}
    for card in cards:
        groups.setdefault(card.kind.strip().lower(), []).append(card)
    body = "".join(
        '<div class="a-group">'
        f'<h3 class="j-label">{escape(group[0].kind.strip() or "other")}'
        f' <span class="j-count">{len(group)}</span></h3>'
        '<ul class="a-cards a-outs">'
        + "".join(
            _card(gutter, ranges.get(id(c)), "a-out", output_card_body(c), "the output")
            for c in group
        )
        + "</ul></div>"
        for group in groups.values()
    )
    head, head_id, head_cards = _heading_unit(gutter, part) if part else ("", "", "")
    return section(
        title, head_cards + body, badge=count(len(cards), "output"), head=head, head_id=head_id
    )


# -- prose ------------------------------------------------------------------

# The longest goal, in lines and in characters, that shows open.
_ASKED_LINES = 6
_ASKED_CHARS = 600


def asked_html(part: rp.Section | None, gutter: Gutter | None = None) -> str:
    """What was asked, as a quote. A long one is folded, unless it has a
    gutter: then a comment on it stays in sight."""
    if part is None or part.empty:
        return ""
    if gutter is not None and part.line:
        inner = blocks_html(part.body, start=part.start, gutter=gutter)
        quote = f'<blockquote class="j-prose a-quote a-blocks">{inner}</blockquote>'
        head, head_id, head_cards = _heading_unit(gutter, part)
        return section("What was asked", head_cards + quote, head=head, head_id=head_id)
    quote = f'<blockquote class="j-prose a-quote">{markdown(part.body)}</blockquote>'
    if part.body.count("\n") < _ASKED_LINES and len(part.body) <= _ASKED_CHARS:
        return section("What was asked", quote)
    hint = rp.plain(" ".join(part.body.split()))[:120]
    return (
        '<details class="a-fold"><summary><span class="a-fold-title">What was asked</span>'
        f'<span class="a-fold-hint">{escape(hint)}…</span></summary>'
        f'<div class="a-fold-body">{quote}</div></details>'
    )


def done_html(report: rp.Report, gutter: Gutter | None = None) -> str:
    """What was done as prose, with the text before the first section after
    the lede and each unknown section under it. A stop report holds the
    plan, so the task boxes are drawn here too."""
    parts = []
    head = ""
    head_id = ""
    head_cards = ""
    part = report.get(rp.DONE)
    if part is not None and not part.empty:
        head, head_id, head_cards = _heading_unit(gutter, part)
        parts.append(blocks_html(part.body, start=part.start, gutter=gutter, draw_boxes=True))
    if report.preamble:
        start = report.preamble_line or 1
        parts.append(blocks_html(report.preamble, start=start, gutter=gutter, draw_boxes=True))
    for extra in report.extra:
        heading = f"<h2>{escape(extra.title)}</h2>"
        if gutter is not None and extra.line:
            last = max(extra.line, extra.start - 1)
            parts.append(gutter.unit(extra.line, last, heading, "a-block h d0", "the heading"))
        else:
            parts.append(f'<div class="a-block h d0">{heading}</div>')
        if extra.body:
            parts.append(blocks_html(extra.body, start=extra.start, gutter=gutter, draw_boxes=True))
    if not parts:
        return ""
    return section(
        "What was done", head_cards + article("".join(parts)), head=head, head_id=head_id
    )


def follow_ups_html(part: rp.Section | None, gutter: Gutter | None = None) -> str:
    """The follow-ups as a static checklist."""
    if part is None or part.empty:
        return ""
    items = [entry for entry in part.items if not rp.is_none(entry.text)]
    ranges = _item_ranges(part, items) if gutter is not None and part.line else None
    body = ""
    if items:
        rows = "".join(
            _card(
                gutter,
                ranges[n] if ranges else None,
                "",
                f'<span class="a-box" aria-hidden="true"></span><span>{inline(entry.text)}</span>',
                "the follow-up",
            )
            for n, entry in enumerate(items)
        )
        body = f'<ul class="a-checklist">{rows}</ul>'
    if part.rest and not rp.is_none(part.rest):
        body += f'<div class="j-prose">{markdown(part.rest)}</div>'
    if not body:
        return ""
    head, head_id, head_cards = _heading_unit(gutter, part)
    return section("Follow-ups", head_cards + body, head=head, head_id=head_id)


def prose_section_html(title: str, part: rp.Section | None, gutter: Gutter | None = None) -> str:
    """A section shown as its blocks, such as the Cost of a document."""
    if part is None or part.empty:
        return ""
    head, head_id, head_cards = _heading_unit(gutter, part)
    body = article(blocks_html(part.body, start=part.start or 1, gutter=gutter))
    return section(title, head_cards + body, head=head, head_id=head_id)


# -- the script -------------------------------------------------------------

# The one script of the viewer: it shows the copy button of each decision and
# copies its text. With no script, or no clipboard, the buttons stay hidden.
COPY_SCRIPT = (
    "(function(){if(!navigator.clipboard)return;"
    'document.querySelectorAll("button.a-copy").forEach(function(b){'
    'b.hidden=false;b.addEventListener("click",function(){'
    "navigator.clipboard.writeText(b.dataset.copy).then(function(){"
    'b.textContent="Copied";setTimeout(function(){b.textContent="Copy";},2000);});});});})();'
)
COPY_SCRIPT_TAG = f"<script>{COPY_SCRIPT}</script>"
# The hash that the Content-Security-Policy of the viewer allows, and no other script.
_DIGEST = hashlib.sha256(COPY_SCRIPT.encode()).digest()
COPY_SCRIPT_HASH = "sha256-" + base64.b64encode(_DIGEST).decode()
