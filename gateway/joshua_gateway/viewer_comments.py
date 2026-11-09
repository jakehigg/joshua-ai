"""The comment gutter of the viewer: the "+" of a block, its form, and the cards.

A person comments on one line of a text: the definition, the plan, or the
report of an assignment, which the assignments manager stores, or the source
of a document, which the comments file of the document stores. Both use this one gutter. A comment
anchors to a line (``line_no``, from 1, and ``line_text``, the line as it
was), and each part of a rendered page that shows some lines of the text is a
*unit*: it has the "+" of its first line, and it shows the open comments on
any of its lines.

:class:`Gutter` holds the lines of the text, the comments, and where the forms
post. A renderer asks it for each unit in the order of the page, and a
comment shows under the first unit that holds its line. :meth:`Gutter.orphans`
gives the comments that no unit took, :meth:`Gutter.source` the whole text
with a "+" on each line, and :meth:`Gutter.resolved` the resolved comments,
folded. A gutter with no forms is read-only: it shows the cards and no "+".

Every form is plain HTML in a closed ``<details>``, so no script is needed.
Every string is escaped. The functions here build HTML only.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, tzinfo
from html import escape
from typing import Any, Protocol

# The longest comment, in characters.
MAX_COMMENT = 2000


class Comment(Protocol):
    """What the gutter reads of a comment."""

    @property
    def id(self) -> Any: ...
    @property
    def line_no(self) -> int: ...
    @property
    def line_text(self) -> str: ...
    @property
    def body(self) -> str: ...
    @property
    def created_at(self) -> str | None: ...
    @property
    def resolved_at(self) -> str | None: ...
    @property
    def resolution(self) -> str | None: ...
    @property
    def stale(self) -> bool: ...


# -- times ------------------------------------------------------------------


def moment(text: str | None) -> datetime | None:
    """An ISO time with an offset, or None."""
    if not text:
        return None
    try:
        when = datetime.fromisoformat(text)
    except ValueError:
        return None
    return when if when.tzinfo is not None else None


def ago(seconds: float) -> str:
    """A past interval as a person reads it: ``just now``, ``5m ago``,
    ``2h ago``, ``3d ago``. A time in the future is ``just now``."""
    if seconds < 60:
        return "just now"
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 48:
        return f"{hours}h ago"
    days = hours // 24
    if days < 60:
        return f"{days}d ago"
    return f"{days // 30}mo ago"


def time_html(text: str | None, *, tz: tzinfo, now: datetime) -> str:
    """A ``<time>`` element: the relative time, with the full time in the
    ``title`` attribute. "" when ``text`` is not a time with an offset."""
    when = moment(text)
    if when is None:
        return ""
    local = when.astimezone(tz)
    full = f"{local:%Y-%m-%d %H:%M} {local.tzname() or ''}".strip()
    label = ago((now - when).total_seconds())
    return (
        f'<time datetime="{escape(when.isoformat())}" title="{escape(full)}">{escape(label)}</time>'
    )


# -- the parts --------------------------------------------------------------


@dataclass(frozen=True)
class CommentForms:
    """Where the comment forms of a text post: ``action`` is the path of the
    comments in the viewer, ``csrf`` the token of the forms. A delete posts to
    ``<action>/<id>/delete``. ``target`` set, each form also sends it: the
    text of an assignment that the comment is on (``plan`` or ``report``)."""

    action: str
    csrf: str
    target: str = ""


def comments_badge(n: int) -> str:
    """The count of open comments: "3 comments for Joshua", or "" for none."""
    if n <= 0:
        return ""
    text = f"{n} comment{'' if n == 1 else 's'} for Joshua"
    return f'<span class="j-badge accent a-comments">{escape(text)}</span>'


def _csrf_input(forms: CommentForms) -> str:
    """The hidden fields of every form: the CSRF token and, when set, the target."""
    target = (
        f'<input type="hidden" name="target" value="{escape(forms.target)}">'
        if forms.target
        else ""
    )
    return f'<input type="hidden" name="csrf" value="{escape(forms.csrf)}">{target}'


def _dom_id(value: Any) -> str:
    """A comment id as a part of an element id: letters, digits, and dashes."""
    return "".join(ch if ch.isalnum() or ch == "-" else "-" for ch in str(value))[:64]


def comment_card(
    comment: Comment,
    *,
    forms: CommentForms | None,
    tz: tzinfo,
    now: datetime,
    quote: bool = False,
    can_delete: bool = True,
) -> str:
    """One open comment: the body, the age, a stale mark, and Delete. With
    ``quote``, the comment is on a line of a unit that is not its first line,
    and the card shows that line. Delete shows only with ``forms`` and
    ``can_delete``."""
    age = time_html(comment.created_at, tz=tz, now=now)
    stale = ""
    was = ""
    if comment.stale:
        stale = '<span class="j-badge warn">line changed</span>'
        was = (
            '<p class="a-was"><span class="j-faint">It was on:</span> '
            f"<code>{escape(comment.line_text)}</code></p>"
        )
    elif quote:
        was = (
            f'<p class="a-was"><span class="j-faint">Line {int(comment.line_no)}:</span> '
            f"<code>{escape(comment.line_text)}</code></p>"
        )
    delete = ""
    if forms is not None and can_delete:
        action = f"{forms.action}/{escape(str(comment.id))}/delete"
        delete = (
            f'<form class="a-del" method="post" action="{action}">'
            f'{_csrf_input(forms)}<button class="j-btn sm" type="submit">Delete</button></form>'
        )
    return (
        f'<div class="a-comment{" stale" if comment.stale else ""}" '
        f'id="C{_dom_id(comment.id)}">'
        f'<p class="a-comment-body">{escape(comment.body)}</p>{was}'
        f'<div class="a-comment-foot"><span class="j-faint">{age}</span>{stale}{delete}</div></div>'
    )


def add_form(n: int, text: str, forms: CommentForms, *, what: str = "") -> str:
    """The "+" of a line or a unit: a closed ``<details>`` with the form of a
    new comment on line ``n``, whose text is ``text``."""
    label = f"Comment on {what or f'line {n}'}"
    return (
        f'<details class="a-add"><summary class="a-plus" title="{escape(label)}" '
        f'aria-label="{escape(label)}">+</summary>'
        f'<form class="a-comment-form" method="post" action="{escape(forms.action)}">'
        f'{_csrf_input(forms)}<input type="hidden" name="line_no" value="{int(n)}">'
        f'<input type="hidden" name="line_text" value="{escape(text)}">'
        f'<div class="j-field"><textarea name="body" rows="3" maxlength="{MAX_COMMENT}" '
        f'required aria-label="Your {escape(label[0].lower() + label[1:])}" '
        'placeholder="Tell Joshua what to change"></textarea></div>'
        '<button class="j-btn sm" type="submit">Comment</button></form></details>'
    )


def _where(comment: Comment) -> str:
    """ "Line N", or "The document" for line 0, the document as a whole."""
    n = int(comment.line_no)
    return "The document" if n == 0 else f"Line {n}"


def resolved_html(comments: Sequence[Comment], *, tz: tzinfo, now: datetime) -> str:
    """The resolved comments, folded, each with the resolution of Joshua."""
    if not comments:
        return ""
    rows = "".join(
        f'<li class="a-comment resolved" id="C{_dom_id(c.id)}">'
        f'<p class="j-faint">{_where(c)}: <code>{escape(c.line_text)}</code></p>'
        f'<p class="a-comment-body">{escape(c.body)}</p>'
        f'<p class="a-resolution"><span class="j-label">Joshua</span> '
        f"{escape(c.resolution or '')}"
        f' <span class="j-faint">{time_html(c.resolved_at, tz=tz, now=now)}</span></p></li>'
        for c in comments
    )
    return (
        f'<details class="a-resolved"><summary>{len(comments)} resolved</summary>'
        f'<ul class="a-resolved-list">{rows}</ul></details>'
    )


def source_html(
    lines: Sequence[str],
    marked: set[int],
    forms: CommentForms | None,
    preamble: Sequence[str] = (),
) -> str:
    """The text as it is stored, in a closed ``<details>``: one row a line
    with its number and, with ``forms``, its "+". The ``preamble`` lines (the
    frontmatter of a document) come first, with no number and no "+"."""
    rows = [
        f'<li class="a-line a-pre"><span class="a-ln"></span>'
        f'<code class="a-code j-faint">{escape(line)}</code></li>'
        for line in preamble
    ]
    for n, line in enumerate(lines, start=1):
        plus = add_form(n, line, forms) if forms is not None else ""
        rows.append(
            f'<li class="a-line{" has-comments" if n in marked else ""}" id="S{n}">'
            f'<a class="a-ln" href="#S{n}">{n}</a>'
            f'<code class="a-code">{escape(line)}</code>{plus}</li>'
        )
    return (
        '<details class="a-source"><summary>Source</summary>'
        f'<ol class="a-lines">{"".join(rows)}</ol></details>'
    )


# -- the gutter -------------------------------------------------------------


@dataclass
class Gutter:
    """The comments of one text, given out to the units of a page.

    ``lines`` are the lines that a comment counts from 1: the definition of
    an assignment, or the body of a document. ``offset`` is added to each
    line number that a renderer gives. ``preamble`` are lines that the
    ``Source`` fold shows first with no number and no "+" (the frontmatter of
    a document). ``author`` set, only the comments whose ``author`` is that
    value get a Delete button. ``prefix`` starts the id of each unit and
    anchor (``L<n>``), so two texts on one page have no id in common."""

    lines: list[str]
    comments: Sequence[Comment]
    forms: CommentForms | None
    tz: tzinfo
    now: datetime
    offset: int = 0
    author: str | None = None
    preamble: Sequence[str] = ()
    prefix: str = "L"
    _placed: set[int] = field(default_factory=set)
    _answered: set[int] = field(default_factory=set)

    @property
    def open(self) -> list[Comment]:
        return [c for c in self.comments if c.resolved_at is None]

    def _can_delete(self, comment: Comment) -> bool:
        return self.author is None or getattr(comment, "author", None) == self.author

    def text(self, n: int) -> str:
        """Line ``n`` (from 1) of the stored text, or ""."""
        return self.lines[n - 1] if 1 <= n <= len(self.lines) else ""

    def plus(self, first: int, what: str) -> str:
        """The "+" of a unit that starts on line ``first`` (before the offset)."""
        if self.forms is None:
            return ""
        n = first + self.offset
        return add_form(n, self.text(n), self.forms, what=what)

    def take(self, first: int, last: int) -> str:
        """The cards of the open comments on lines ``first`` to ``last``
        (before the offset) that no unit took yet."""
        lo, hi = first + self.offset, last + self.offset
        cards = []
        for index, comment in enumerate(self.open):
            if index in self._placed or not lo <= comment.line_no <= hi:
                continue
            self._placed.add(index)
            cards.append(
                comment_card(
                    comment,
                    forms=self.forms,
                    tz=self.tz,
                    now=self.now,
                    quote=comment.line_no != lo,
                    can_delete=self._can_delete(comment),
                )
            )
        return "".join(cards)

    def anchors(self, first: int, last: int) -> str:
        """An ``L<n>`` anchor for each line after the first of a unit."""
        lo, hi = first + self.offset, last + self.offset
        return "".join(
            f'<span class="a-anchor" id="{self.prefix}{n}"></span>' for n in range(lo + 1, hi + 1)
        )

    def mark(self, first: int, last: int, what: str) -> tuple[str, str, str]:
        """The parts of a unit that a renderer places itself: the anchors, the
        "+", and the cards. The renderer gives its element ``id="L<first>"``."""
        return self.anchors(first, last), self.plus(first, what), self.take(first, last)

    def unit_id(self, first: int) -> str:
        return f"{self.prefix}{first + self.offset}"

    def can_delete(self, comment: Comment) -> bool:
        return self._can_delete(comment)

    def answers(self, first: int, last: int) -> tuple[list[Comment], list[Comment]]:
        """The open and the resolved comments on lines ``first`` to ``last``
        (before the offset) that no unit took yet. The caller shows them, so
        no other unit, :meth:`orphans`, or :meth:`resolved` shows them."""
        lo, hi = first + self.offset, last + self.offset
        open_, done = [], []
        for index, comment in enumerate(self.open):
            if index not in self._placed and lo <= comment.line_no <= hi:
                self._placed.add(index)
                open_.append(comment)
        for index, comment in enumerate(self.comments):
            if comment.resolved_at is None or index in self._answered:
                continue
            if lo <= comment.line_no <= hi:
                self._answered.add(index)
                done.append(comment)
        return open_, done

    def unit(self, first: int, last: int, body: str, css: str, what: str) -> str:
        """One unit: its anchors, the body, the "+" of its first line, and the
        open comments on any of its lines."""
        anchors, plus, cards = self.mark(first, last, what)
        cls = f"a-unit {css}" + (" has-comments" if cards else "")
        return (
            f'<div class="{cls}" id="{self.unit_id(first)}">{anchors}'
            f'<div class="a-unit-body">{body}</div>{plus}{cards}</div>'
        )

    def orphans(self) -> str:
        """The open comments that no unit took, under a label."""
        rest = [c for i, c in enumerate(self.open) if i not in self._placed]
        if not rest:
            return ""
        cards = "".join(
            comment_card(
                c, forms=self.forms, tz=self.tz, now=self.now, can_delete=self._can_delete(c)
            )
            for c in rest
        )
        return f'<div class="a-orphans"><p class="j-label">On lines that are gone</p>{cards}</div>'

    def source(self) -> str:
        """The whole text with a "+" on each line, folded."""
        return source_html(
            self.lines, {c.line_no for c in self.open}, self.forms, preamble=self.preamble
        )

    def resolved(self) -> str:
        """The resolved comments, folded."""
        done = [
            c
            for i, c in enumerate(self.comments)
            if c.resolved_at is not None and i not in self._answered
        ]
        return resolved_html(done, tz=self.tz, now=self.now)
